# -*- coding: utf-8 -*-
"""
Uniwill / 同方(机械革命) EC 风扇控制核心库与命令行工具。

本机（MECHREVO WUJIE14XA，BIOS N.1.14MRO26）实测验证的寄存器：
    CPU 温度      0x043E       1 字节，摄氏度
    GPU 温度      0x044F       本机无独显，恒为 0
    风扇1转速     0x0464/65    大端 16 位，RPM
    风扇2转速     0x046C/6D    大端 16 位，RPM
    主扇占空比    0x1804       0..200 => 0..100%
    副扇占空比    0x1809       0..200 => 0..100%

0x1800-0x1809 是两条对称的 5 字节风扇记录，不是“寄存器+镜像”：
    A: 上限 0x1801=0xC8   目标 0x1804   -> FAN1
    B: 上限 0x1806=0xC8   目标 0x1809   -> FAN2
两路彼此独立，必须各自写，否则只会控住一把扇。

核心机制（2026-10-22 实测）：
    EC 的热管理任务会周期性把目标寄存器覆盖回它自己算出的值。
    因此“写一次”无效，必须以 >=1Hz 的频率持续写入才能维持目标占空比；
    一旦停止写入，EC 会按约 3 raw/s 的斜坡滑回自己的策略值，完全可逆。

    EC 内部会把副扇目标向主扇目标做软着陆派生（本项目实测），但有严重迟滞：
    只写 0x1804 时，EC 的斜坡会按它自己的策略把 0x1809 慢慢挪到 0，
    几十秒后副扇彻底停转（表现为 RPM 恒 0）。所以两条记录都要持续写。

用法：
    fanctl.py status            读取一次状态
    fanctl.py watch             持续监视（只读）
    fanctl.py set <百分比>       维持指定百分比，直到 Ctrl+C
    fanctl.py auto              解除控制，交还给 EC（并确认已接管）
"""
import argparse
import ctypes
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fandiag import DiagLog  # noqa: E402

# ---------- 端口驱动 IOCTL（与 inpoutx64 配套，同 JG_BAT\batteryctl.py） ----------
READ_PORT_IOCTL = (-1673519100) & 0xFFFFFFFF
WRITE_PORT_IOCTL = (-1673519096) & 0xFFFFFFFF
ADDR_HIGH, ADDR_LOW, DATA = 0x681, 0x682, 0x683

REG_CPU_TEMP = 0x043E
REG_GPU_TEMP = 0x044F
REG_FAN1_RPM = 0x0464
REG_FAN2_RPM = 0x046C
REG_DUTY_FAN1 = 0x1804
REG_DUTY_FAN2 = 0x1809
REG_I2EC_MODE = 0x200D

DUTY_MAX_RAW = 200          # 寄存器满量程 = 100%

# 写入周期必须远快于 EC 自身的覆写周期，否则两家用同一个寄存器互相抢，
# 副扇目标一会是 EC 的值、一会是我们的值，转速跟着乱跳甚至停在 0。
# 实测：2Hz 被 EC 压制（FAN2 2090~3433 爬升不收敛）；
#       10Hz 是拉锯（FAN2 2573~5417 剧烈摆动，最差）；
#       25Hz 完全压制（FAN2 5146~5324 稳定）。
WRITE_INTERVAL = 0.04       # 25Hz，写占空比
SAMPLE_INTERVAL = 0.5       # 2Hz，读状态给 GUI/历史，免得高频读端口

# 手动/曲线占空比下限。
# 2026-10-26 实测（fan_low_duty_test.py）推翻了旧的 20% 结论：
#   duty 后端：3%~20% 全部稳定（20%→2636/2789，5%→858/1633，3%→487/1493），
#              只有 0% 会让两扇在 0~800 RPM 之间反复启停；
#   table 后端：8% 以下被 EC 地板托住，恒定约 1745 RPM，根本压不到停转。
# 因此下限取 5：既留出静音空间，又避开 0% 的启停区。
MANUAL_FLOOR_PERCENT = 5

# 低温交还：曲线模式下 CPU 低于这个温度就把控制权交还 EC 原生策略。
# 低温段 EC 自己的曲线比我们更省电，没必要拿表压着它。
# 回差 3℃：交还/接管都要走 EC 的软着陆斜坡，卡在阈值上反复横跳会听到转速起伏。
HANDOFF_BELOW_TEMP = 45.0
HANDOFF_RESUME_HYST = 3.0

# 手动模式下副扇连续 0 RPM 多少秒就交还 EC。0.5s 采样 => 24 个采样 = 12 秒。
FAN2_ZERO_SAMPLES = 24

# 副扇看门狗的静默期：目标刚变化时 EC 要走软着陆斜坡（实测 2.8~3.3 raw/s），
# 满量程约 30 秒。这段时间内副扇 0 RPM 是正常的，不能算堵转。
FAN2_ZERO_GRACE = 30.0

# 温度合理区间。撕裂读会冒出 254 这种假值，若直接采信会误触发临界保护。
TEMP_SANITY = (0, 120)

# ---- 撕裂写防护 ----
# EC 端口写入是「写地址高位(0x681) -> 写地址低位(0x682) -> 写数据(0x683)」三步，
# 三步之间没有原子性。Windows 的 ACPI 电池驱动每秒都在轮询 0x04xx 电池信息块，
# 若它把地址窗口设成 0x04xx 正好插在我们的「写低位」和「写数据」之间，
# 我们要写给 0x1809 的数据就会落到 0x0409。
#
# 2026-10-24 实测事故：0x0409 被写成 0xA0（正好等于 80% 占空比的寄存器原值 160，
# 且低字节与 0x1809 一致），设计电压 0x0408/0x0409 由 15400mV 变成 41000mV，
# 导致 Windows 报告的设计容量变成 5200 x 41 = 213,200 mWh（正确应为 80,080）。
# 因此这里对这条静态寄存器做定期检查 + 自动修复。
REG_DESIGN_VOLTAGE = 0x0408      # 小端 16 位，单位 mV
DESIGN_VOLTAGE_MV = 15400        # 4 串锂电标称电压 4 x 3.85V
DESIGN_VOLTAGE_SANITY = (10000, 20000)

# 掉电相关的电源控制寄存器哨兵。
# 表驱动每次重锁写 96 字节，地址低字节覆盖 0x20~0x4F（表1）与 0x50~0x7F（表2），
# 而这些寄存器的低字节正好落在窗口内：
#   0x083A 输入限流  低字节 0x3A  ← 最危险：被写成极小值，EC 判定适配器无输出 → 掉电
#   0x0522 目标电压  低字节 0x22
#   0x0520 目标电流  低字节 0x20
# 跨进程撕裂写（约 0.005%/次读）砸中它们的概率虽低，后果却是整机断电，
# 所以每次写表后立即校验、越界即写回最近一次的良好值。
# 注意 0x0438 包电压是**测量值**不能写，故不在守卫列表内。
POWER_GUARD_REGS = (
    (0x083A, "输入限流", (500, 8000)),
    (0x0522, "目标电压", (10000, 19000)),
    (0x0520, "目标电流", (500, 12000)),
)
GUARD_CHECK_INTERVAL = 5.0       # 秒，静态寄存器巡检周期

# 另两条同样会被撕裂写误伤的风控寄存器（出厂值均取自 5 份历史快照）
REG_FAN_TABLE_LATCH = 0x0727     # bit6 = 启用 0x0F20/0x0F50 自定义曲线表；出厂 0x00
REG_FAN_ISOLATE = 0x07C5         # bit7 = 主副扇输出隔离；出厂 0x00
REG_FAN_TABLE_FAN1 = 0x0F20      # 曲线表 fan1
REG_FAN_TABLE_FAN2 = 0x0F50      # 曲线表 fan2
REG_FAN_TABLE_LEN = 48           # 每张表 48 字节（表区总长，仅作说明，不用于写入循环）
# 实际只需写前 8 字节：2026-10-26 实测 EC 取表**前 8 个字节的最大值**。
#  - 反向测试：索引0=30/其余=90 -> duty=90（否定"固定索引0"）；
#    索引0~7=30/其余=90 -> duty=30（用的是前 8 字节）。
#  - 温度依赖测试：前8=30/其余=140，CPU 45°C 升到 79°C 全程 duty 恒为 30，
#    索引不随温度变化 -> 高温下只写前 8 字节同样安全。
# 只写前 8 字节的地址低字节是 0x20~0x27 与 0x50~0x57，
# **正好避开 0x38(电池包电压) 与 0x3A(输入限流) 这两个最危险的电源寄存器**，
# 单次写入量也从 96 次降到 16 次，撕裂写窗口大幅收窄。
TABLE_ACTIVE_LEN = 8
CONTENTION_RELATCH = 5           # 连续几次"心跳发现偏离"就判定为端口争用
CONTENTION_HEARTBEAT = 30.0      # 判定争用后把重锁间隔拉长到 30 秒，避免跟着加码狂写
TABLE_HEARTBEAT = 10.0           # 秒，表驱动模式下的心跳检查周期
TABLE_DRIFT_TOL = 8              # raw，心跳时允许的目标偏差（超过才重锁）

# 哨兵：区分"没有传这个参数"和"显式传 None（副扇跟随主扇）"
_UNSET = object()


class PortIo:
    def __init__(self):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        self.k32.CreateFileW.restype = ctypes.c_void_p
        self.k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.k32.DeviceIoControl.restype = ctypes.c_int
        self.k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.k32.CloseHandle.restype = ctypes.c_int
        self.handle = None

    def open(self):
        self.handle = self.k32.CreateFileW(r"\\.\inpoutx64", 0xC0000000, 0, None, 3, 0x80, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise RuntimeError(f"无法打开 inpoutx64 驱动（错误码 {ctypes.get_last_error()}）。"
                               f"请以管理员权限运行，并确认驱动已安装。")

    def close(self):
        if self.handle is not None:
            self.k32.CloseHandle(self.handle)
            self.handle = None

    def _transfer(self, code, data, out_size):
        ibuf = ctypes.create_string_buffer(data)
        obuf = ctypes.create_string_buffer(out_size) if out_size else None
        returned = ctypes.c_uint32()
        if not self.k32.DeviceIoControl(self.handle, code, ibuf, len(data), obuf, out_size,
                                        ctypes.byref(returned), None):
            raise RuntimeError(f"端口操作失败（错误码 {ctypes.get_last_error()}）")
        return obuf.raw[:returned.value] if obuf is not None else b""

    def rd(self, port):
        return self._transfer(READ_PORT_IOCTL, struct.pack("<H", port), 1)[0]

    def wr(self, port, value):
        self._transfer(WRITE_PORT_IOCTL, struct.pack("<HB", port, value), 0)


class UniwillEC:
    """EC 读写。所有读写都通过端口 0x681/0x682/0x683 的 I2EC 窗口完成。

    ⚠️ 端口锁（2026-10-26 修复，这是本项目最重要的一条约束）
    ------------------------------------------------------------------
    一次 EC 访问是**三步非原子序列**：写地址高字节 → 写地址低字节 → 读/写数据。
    三步之间若被另一个线程插进来改写地址窗口，就会读到/写到**别的寄存器**。

    实测（并发读 0x0438 包电压，判据：偏离基准 >300mV）：
        单线程 纯读       0.00%
        单线程 混合读写    0.00%
        4 线程 纯读       4.35%    异常值 63222 = 0xF6F6（端口浮空默认值）
        4 线程 混合读写   98.75%    ← 几乎每次都错

    后果不只是读数错：撕裂**写**会把值落进错误地址。已发生过的事故——
      - 0x0409 设计电压被写成 0xA0（地址低字节被换成 0x1809 的 0x09）
      - 0x0727/0x07C5 被写进垃圾位，直接禁用 EC 风扇输出（两把扇 0 RPM）
      - 疑似把 0x083A 输入限流写坏，EC 判定供电异常 → 整机掉电（两次关机）

    锁必须是**类级**的：多个 UniwillEC 实例各自持有驱动句柄，但共享同一个
    物理 EC 端口，实例级锁挡不住跨实例并发。用 RLock 以便 _apply_table 这类
    批量操作整体持锁（内部再调 write 时重入）。
    """

    _port_lock = threading.RLock()

    def __init__(self):
        self.port = PortIo()

    def open(self):
        self.port.open()
        mode = self.read(REG_I2EC_MODE)
        if mode & 0x03 != 0x03:
            self.close()
            raise RuntimeError(f"I2EC 不是读写模式（0x200D=0x{mode:02X}）")
        return self

    def close(self):
        self.port.close()

    def read(self, address):
        with self._port_lock:
            self.port.wr(ADDR_HIGH, address >> 8)
            self.port.wr(ADDR_LOW, address & 0xFF)
            return self.port.rd(DATA)

    def write(self, address, value):
        with self._port_lock:
            self.port.wr(ADDR_HIGH, address >> 8)
            self.port.wr(ADDR_LOW, address & 0xFF)
            self.port.wr(DATA, value & 0xFF)

    def be16(self, address):
        """16 位大端读。整段持锁，保证高低字节来自同一个地址窗口。"""
        with self._port_lock:
            return self.read(address) << 8 | self.read(address + 1)

    # ---------- 语义化读取 ----------
    def cpu_temp(self):
        return self.read(REG_CPU_TEMP)

    def gpu_temp(self):
        return self.read(REG_GPU_TEMP)

    def fan_rpm(self, index=1):
        return self.be16(REG_FAN1_RPM if index == 1 else REG_FAN2_RPM)

    def duty_raw(self, index=1):
        return self.read(REG_DUTY_FAN1 if index == 1 else REG_DUTY_FAN2)

    def duty_percent(self, index=1):
        raw = self.duty_raw(index)
        # 撕裂读可能读到别的地址（比如 0x0409 的高字节），这一路只管显示，
        # 不参与控制判断，夹到量程内即可，别让界面上跳出 21000%。
        if not 0 <= raw <= DUTY_MAX_RAW:
            raw = DUTY_MAX_RAW if raw > DUTY_MAX_RAW else 0
        return round(raw / DUTY_MAX_RAW * 100)

    def snapshot(self):
        return {
            "cpu_temp": self.cpu_temp(),
            "gpu_temp": self.gpu_temp(),
            "fan1_rpm": self.fan_rpm(1),
            "fan2_rpm": self.fan_rpm(2),
            "duty_percent": self.duty_percent(1),
            "duty_percent2": self.duty_percent(2),
            "duty_raw": self.duty_raw(),
        }


class FanController:
    """后台控制线程：唯一持有 EC 句柄的对象，避免并发访问端口。

    mode: "auto" | "manual" | "curve"
        auto   —— 不写入任何寄存器，完全交还 EC
        manual —— 维持 self.manual_percent
        curve  —— 按 self.curve 的温度曲线自动解算目标
    manual_percent: 手动目标百分比
    curve: [(temp, percent), ...] 按温度升序的温度曲线（仅 curve 模式使用）
    curve2: 副扇独立曲线，同格式；None = 跟随主扇（沿用主扇解算结果）
    protect_temp / critical_temp: 温度保护阈值

    副扇目标三选一，优先级从高到低：
      1. manual_percent2 不为 None → 固定值（manual 语义，不受温度影响）
      2. curve 模式且 curve2 已设置   → 副扇按自己的曲线解算
      3. 其余                        → 跟随主扇
    两者互斥：GUI 里勾选其一会自动取消另一个，避免"设了没反应"。

    注意：曾经「温度曲线」只是 manual 模式下的一个开关，导致用户勾选了曲线、
    模式却停在 auto 时什么都不发生（auto 的判定早于曲线，直接 return）。
    现在曲线是独立模式，勾选后必须真正处于 curve 模式才会写入。
    """

    def __init__(self, interval=0.5, write_interval=WRITE_INTERVAL,
                 backend="duty"):
        self.ec = UniwillEC()
        self.interval = interval              # 状态采样周期
        self.write_interval = write_interval  # 占空比写入周期，必须远小于 interval
        # backend: "duty" = 高频持续写 0x1804/0x1809（老方案）
        #          "table"= 写曲线表一次 + 锁存，由 EC 自己保持（新方案，撕裂写风险低 2 个数量级）
        self.backend = backend
        self.lock = threading.RLock()
        self.mode = "auto"
        self.manual_percent = 50
        # 副扇目标。None = 跟随主扇（两把扇同一个值）；整数 = 独立控制。
        # 两条记录 0x1804 / 0x1809 物理上是独立的，EC 只是"习惯上"让副扇跟随主扇。
        self.manual_percent2 = None
        self.use_curve = False
        self.curve = [(45, 20), (55, 30), (65, 45), (75, 65), (85, 100)]
        self.curve_hyst = 3         # 曲线降档迟滞（°C），防边界抖动
        self._curve_last = None     # 曲线模式上一档目标
        # 副扇独立曲线：None = 跟随主扇曲线。主副扇各走一条曲线时，
        # 两把扇的迟滞状态必须分开存，否则会互相污染（副扇的低档位会把主扇也拽下来）。
        self.curve2 = None
        self._curve2_last = None
        self.protect_temp = 85
        self.critical_temp = 95
        # 低温交还状态机：curve 模式下温度跌破 HANDOFF_BELOW_TEMP 就交还 EC，
        # 回到 阈值+回差 才重新接管。_handoff_from 记住交还前的模式。
        self.handoff_lowtemp = True     # 是否启用这条策略
        self._lowtemp_handoff = False
        self._handoff_from = None
        self._handoff_check = 0.0
        self.running = False
        self.state = {"t": 0, "mode": "auto", "cpu_temp": 0, "gpu_temp": 0, "fan1_rpm": 0, "fan2_rpm": 0,
                      "duty_percent": 0, "duty_percent2": 0,
                      "target_percent": None, "target_percent2": None,
                      "note": "", "error": None, "handoff_lowtemp": False}
        self._thread = None
        self._history = []          # [(elapsed, temp, fan1, fan2, duty)]
        self._last = None           # 最近一次写入时的 (temp, target, note)
        self._last2 = None          # 最近一次写入时的副扇目标
        self._fan2_zero = 0         # 副扇连续读到 0 RPM 的采样计数
        self._fan2_downgraded = False  # 是否已因副扇停转而退回"跟随主扇"
        self._tear_repairs = 0      # 撕裂写自动修复次数
        self._power_base = {}       # 电源寄存器最近一次良好值，用于撕裂写兜底
        self._power_guards = 0      # 电源寄存器被写坏并修复的次数
        self._last_power_note = None  # 最近一次电源寄存器修复说明，供 note 显示
        self._last_guard = 0.0      # 上次静态寄存器巡检时间
        self._last_table = 0.0      # 表驱动模式下上次重锁时间
        self._latch_base = 0x00     # 0x0727 出厂基线（运行时采样，用于锁存脉冲）
        self._isolate_base = 0x00   # 0x07C5 出厂基线，同上
        self._table_last = None     # 上次写进曲线的 (raw1, raw2)，只在变化时重写
        self._isolate_needed = False  # 当前是否依赖 0x07C5 bit7 做主副扇独立
        self._last_target_change = 0.0   # 上次目标变化时间：给看门狗留出斜坡时间
        self._temp_good = None      # 最近一次合法温度，撕裂读时用它兜底
        self._contention = 0        # 连续"心跳发现偏离"的次数，用于检测端口争用
        self._table_interval = TABLE_HEARTBEAT   # 当前重锁间隔，争用时自动拉长
        # 诊断日志：默认关闭，由 GUI 开关启用。关闭时所有调用都是空操作。
        self.diag = DiagLog()
        self.diag.attach(self.ec)

    # ---------- 生命周期 ----------
    def start(self):
        self.ec.open()
        # 启动先巡检一次：上一轮运行可能已经把静态寄存器写坏了
        initial = self.check_static_registers()
        if initial:
            with self.lock:
                self.state["note"] = initial
        # 记下两个控制位的出厂基线：巡检已把锁存/隔离位清掉，这里读到的就是原生值。
        # 后续置位/清位一律用「基线 ± 目标位」的**绝对写入**，绝不 read-modify-write ——
        # 端口争用下读到的是撕裂值，RMW 会把垃圾整字节写回去（实测写出过 0x07/0x1D，
        # 直接导致 EC 停止风扇输出）。基线用众数采样，避免采样本身被撕裂读污染。
        try:
            self._latch_base = self._read_mode(REG_FAN_TABLE_LATCH) & ~0x40
            self._isolate_base = self._read_mode(REG_FAN_ISOLATE) & ~0x80
        except Exception:
            self._latch_base = 0x00
            self._isolate_base = 0x00
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.diag.event("stop", mode=self.mode, backend=self.backend)
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self.mode = "auto"
        # 交还 EC：清掉锁存位，否则 EC 会一直守着我们最后写进去的曲线表
        if self.backend == "table":
            self._release_table()
        try:
            self.ec.close()
        except Exception:
            pass
        # 关掉日志：EC 句柄已关闭，留着也读不到东西了
        self.diag.disable(reason="stop")

    # ---------- 外部配置 ----------
    def configure(self, mode=None, manual_percent=None, manual_percent2=_UNSET,
                  use_curve=None, curve=None, curve2=_UNSET,
                  protect_temp=None, critical_temp=None, backend=None):
        with self.lock:
            if backend in ("duty", "table") and backend != self.backend:
                # 离开表驱动模式：先解除锁存，把控制权干净地交还 EC，
                # 否则 EC 会一直守着我们最后写进去的那张表。
                if self.backend == "table" and self.running:
                    self._release_table()
                self.backend = backend
                self._table_last = None
            if mode in ("auto", "manual", "curve"):
                self.mode = mode
                # 用户显式切模式就覆盖低温交还，否则点了"曲线"还会被锁在 auto
                self._lowtemp_handoff = False
                self._handoff_from = None
                if mode != "curve":
                    self._curve_last = None
                    self._curve2_last = None
            if manual_percent is not None:
                self.manual_percent = max(0, min(100, int(manual_percent)))
            if manual_percent2 is not _UNSET:
                # 传 None 表示"副扇跟随主扇"，传整数表示独立控制
                self.manual_percent2 = (None if manual_percent2 is None
                                        else max(0, min(100, int(manual_percent2))))
                if self.manual_percent2 is not None:
                    self._fan2_downgraded = False   # 用户重新接管，清掉自动降级标记
            if curve2 is not _UNSET:
                # 用哨兵区分"没传这个参数"和"显式传 None 关闭副扇曲线"
                if curve2:
                    self.curve2 = sorted([(int(t), max(0, min(100, int(p))))
                                          for t, p in curve2])
                else:
                    self.curve2 = None
                self._curve2_last = None
            if use_curve is not None:
                # use_curve 是历史参数。现在曲线本身就是一种模式，这里做等价转换，
                # 避免旧调用 $(mode="manual", use_curve=True) 又掉回"看着勾了却不生效"的坑。
                self.use_curve = bool(use_curve)
                if self.use_curve and self.mode == "manual":
                    self.mode = "curve"
                elif not self.use_curve and self.mode == "curve":
                    self.mode = "manual"
            if curve:
                self.curve = sorted([(int(t), max(0, min(100, int(p)))) for t, p in curve])
                self._curve_last = None
                # 副扇若处于"跟随"，主扇曲线变了它自然跟着变，无需额外处理
            if protect_temp is not None:
                self.protect_temp = int(protect_temp)
            if critical_temp is not None:
                self.critical_temp = int(critical_temp)
            # 用户操作的最终收敛点：所有 GUI 交互最后都走到这里，
            # 在这里记一条事件就能完整覆盖"用户做了什么"。
            self.diag.event("configure", **{
                k: v for k, v in (
                    ("mode", mode),
                    ("manual_percent", manual_percent),
                    ("manual_percent2", None if manual_percent2 is _UNSET else manual_percent2),
                    ("use_curve", use_curve),
                    ("curve", curve),
                    ("curve2", None if curve2 is _UNSET else curve2),
                    ("protect_temp", protect_temp),
                    ("critical_temp", critical_temp),
                    ("backend", backend),
                ) if v is not None or k in ("manual_percent2", "curve2")
            })
            # 记下这次操作**下发后**的完整配置，避免事后要靠多条记录拼。
            self.diag.event("config_now", mode=self.mode, backend=self.backend,
                            manual_percent=self.manual_percent,
                            manual_percent2=self.manual_percent2,
                            curve=self.curve, curve2=self.curve2,
                            protect_temp=self.protect_temp,
                            critical_temp=self.critical_temp)

    # ---------- 目标解算 ----------
    def _curve_value(self, curve, temp):
        """按曲线查表：取最后一个 threshold <= temp 的档位；低于首档时用首档值。"""
        target = curve[0][1] if curve else 0
        for threshold, percent in curve:
            if temp >= threshold:
                target = percent
            else:
                break
        return max(10, min(100, target))

    # ---------- 抗撕裂读的采样原语 ----------
    def _read_mode(self, addr, n=5, sleep=0.004):
        """读 n 次取**众数**。

        单次读会被并发主设备撕裂（实测：读 0x00C0 却拿到 0x083A 的值）。
        取众数而不是中位数，是因为这类寄存器在正常态下是恒定的，
        偶尔插进来的脏值属于少数派；中位数在只有 2 个不同值时会偏向脏值。
        """
        vals = []
        for _ in range(n):
            try:
                vals.append(self.ec.read(addr))
            except Exception:
                pass
            time.sleep(sleep)
        if not vals:
            return 0
        return max(set(vals), key=vals.count)

    def _cpu_temp_safe(self, n=5):
        """温度采样：取众数 + 合理性过滤。

        撕裂读会冒出 254°C 这种假值，而主循环一旦采到它就会触发
        critical_temp 保护、把控制权交还 EC —— 表现为"跑着跑着突然失控"。
        过滤失败时退回上一次合法值，实在没有就用 50°C 这个中性值。
        """
        vals = []
        for _ in range(n):
            try:
                v = self.ec.cpu_temp()
            except Exception:
                v = None
            if v is not None and TEMP_SANITY[0] <= v <= TEMP_SANITY[1]:
                vals.append(v)
            time.sleep(0.004)
        if vals:
            self._temp_good = max(set(vals), key=vals.count)
            return self._temp_good
        if self._temp_good is not None:
            return self._temp_good
        return 50

    def _target_from_curve(self, temp):
        return self._curve_value(self.curve, temp)

    def _hyst_next(self, curve, temp, last):
        """迟滞：升温立即升档，降温要比阈值再低 curve_hyst°C 才降档。

        没有迟滞时，温度在某个档位边界 ±1°C 抖动会让目标占空比来回跳，
        风扇表现为"一下快一下慢"。
        """
        raw = self._curve_value(curve, temp)
        if last is None or raw > last:
            return raw                      # 升温：立即跟上，安全优先
        if raw < last and self._curve_value(curve, temp + self.curve_hyst) < last:
            return raw                      # 降温：确认跌破"阈值-迟滞带"才降档
        return last

    def _target_from_curve_hyst(self, temp):
        self._curve_last = self._hyst_next(self.curve, temp, self._curve_last)
        return self._curve_last

    def _target_from_curve2_hyst(self, temp):
        self._curve2_last = self._hyst_next(self.curve2, temp, self._curve2_last)
        return self._curve2_last

    def _fan2_independent(self):
        """副扇是否处于独立控制（固定值或自己的曲线，而非跟随主扇）。"""
        return self.manual_percent2 is not None or bool(self.curve2)

    def _resolve_target2(self, target1, temp=None):
        """副扇目标解算，优先级：固定值 > 独立曲线 > 跟随主扇。

        两种独立来源都要过下限保护——副扇比主扇更容易在低占空比下堵转。
        """
        if temp is not None and temp >= self.protect_temp:
            return 100                      # 保护温度：副扇同样强制全速
        if self.manual_percent2 is not None:
            return max(MANUAL_FLOOR_PERCENT, min(100, self.manual_percent2))
        if temp is not None and self.mode == "curve" and self.curve2:
            return max(MANUAL_FLOOR_PERCENT,
                       min(100, self._target_from_curve2_hyst(temp)))
        return target1

    def _resolve_target(self, temp):
        """返回 (目标百分比或 None, 说明)。None 表示不写入，交给 EC。"""
        if temp >= self.critical_temp:
            self.mode = "auto"
            return None, f"温度 {temp}°C 超过临界值 {self.critical_temp}°C，已强制交还 EC 自动控速"
        # 低温交还：曲线模式下低于阈值就不必我们管了，EC 自己的低温曲线更省电。
        # 只对 curve 生效——手动模式是用户的显式指令，不能被悄悄抢走。
        if (self.handoff_lowtemp and self.mode == "curve"
                and temp is not None and temp < HANDOFF_BELOW_TEMP):
            self._handoff_from = "curve"
            self._lowtemp_handoff = True
            self.mode = "auto"
            self.diag.event("lowtemp_handoff", temp=temp, threshold=HANDOFF_BELOW_TEMP)
            resume = HANDOFF_BELOW_TEMP + HANDOFF_RESUME_HYST
            return None, (f"{temp}°C < {HANDOFF_BELOW_TEMP:.0f}°C，已交还 EC 原生策略"
                          f"（升到 {resume:.0f}°C 自动接管）")
        if self.mode == "auto":
            return None, "自动（EC 原生策略）"
        if self.mode == "curve":
            target = self._target_from_curve_hyst(temp)
        else:
            target = self.manual_percent
        # 下限保护：低于副扇可用下沿会被 EC 判堵转并闭锁，宁可不给那么低。
        target = max(MANUAL_FLOOR_PERCENT, min(100, target))
        if temp >= self.protect_temp:
            return 100, f"温度 {temp}°C 超过保护阈值 {self.protect_temp}°C，强制全速"
        if self.mode == "curve":
            return target, f"温度曲线：{temp}°C → {target}%"
        return target, "手动维持"

    # ---------- 曲线表写入（表驱动模式）----------
    def _apply_table(self, raw1, raw2):
        """把目标占空比写进两张曲线表，并置锁存位让 EC 装载。

        要点（全部来自实测）：
          - 表在 0x0F20(fan1) / 0x0F50(fan2)，各 48 字节，但**只写前 8 字节即可**
            （常量 TABLE_ACTIVE_LEN，实测依据见其注释：EC 取前 8 字节最大值，
            且索引不随温度变化，45~79°C 全程命中同一段）。
          - 置 0x0727 bit6 后，EC 会把 0x1804/0x1809 沿软着陆斜坡挪到表里的值，
            之后**长期保持**，不需要我们持续写。
          - **副扇独立的关键**：不隔离时，EC 取 fan1 表 / fan2 表解算值的**较大者**，
            两把扇被强制同步（实测 A 组：表1=30% 表2=70% → 两扇都跑到 70%）。
            必须同时置 0x07C5 bit7，副扇才会走自己的 0x0F50 表
            （实测 B/C 组：主 30%/副 70% 与 主 70%/副 30% 均精确命中）。
            两扇目标相同时清掉该位，尽量少留非出厂状态。
        """
        # 整表写入 + 锁存脉冲是一组不可分割的操作，全程独占 EC 端口：
        # 若中途被别的线程插入写别的地址，锁存那一刻 EC 看到的表就是不完整的。
        # _port_lock 是 RLock，内部的 write/_set_isolate 重入没有问题。
        with UniwillEC._port_lock:
            r1 = int(max(1, min(DUTY_MAX_RAW, raw1)))
            r2 = int(max(1, min(DUTY_MAX_RAW, raw2)))
            # 只写前 TABLE_ACTIVE_LEN 字节：EC 取这段的最大值，写满 48 字节没有额外收益，
            # 反而把 0x38 / 0x3A 两个电源寄存器拖进撕裂写的射程里。
            for i in range(TABLE_ACTIVE_LEN):
                self.ec.write(REG_FAN_TABLE_FAN1 + i, r1)
            for i in range(TABLE_ACTIVE_LEN):
                self.ec.write(REG_FAN_TABLE_FAN2 + i, r2)
            # 锁存前先摆好隔离位，保证 0->1 跳变那一刻 EC 看到的就是最终状态
            need_iso = (r1 != r2)
            self._set_isolate(need_iso)
            # 锁存脉冲：先清后置，保证 EC 能看到一次 0->1 跳变
            base = self._latch_base
            self.ec.write(REG_FAN_TABLE_LATCH, base & ~0x40)
            time.sleep(0.05)
            self.ec.write(REG_FAN_TABLE_LATCH, base | 0x40)
        # 写表是撕裂写的主要来源，写完立刻校验电源寄存器。
        # 这是掉电的最后一道兜底：0x083A 若被写坏，EC 会判定适配器无输出而断电。
        pg = self.check_power_registers()
        self._last_power_note = "; ".join(pg) if pg else None
        # 记住这次写进去的值：目标不变时后续循环一律跳过写入
        self._table_last = (r1, r2)
        self._last_table = time.time()
        return r1, r2

    def _set_isolate(self, on):
        """置/清 0x07C5 bit7（主副扇输出隔离）。带一次回读校验。

        只在副扇目标与主扇不同时才置上：置上后 fan2 改走自己的 0x0F50 表，
        这是表驱动模式下唯一能实现主副扇独立控制的途径。
        """
        try:
            # 绝对写入：以出厂基线为底，只动 bit7。绝不 read-modify-write，
            # 否则一次撕裂读就会把垃圾字节写回这个寄存器（实测写出 0x1D，
            # 后果是 EC 停止风扇输出、两把扇长时间 0 RPM）。
            want = (self._isolate_base | 0x80) if on else (self._isolate_base & ~0x80)
            for _ in range(3):
                self.ec.write(REG_FAN_ISOLATE, want)
                time.sleep(0.02)
                if self._read_mode(REG_FAN_ISOLATE, n=3) == want:
                    break
            self._isolate_needed = bool(on)
            return self._isolate_needed
        except Exception:
            self._isolate_needed = False
            return False

    def _release_table(self):
        """交还 EC：清掉锁存位与隔离位，EC 回到原生温控。

        同样走绝对写入——`read() & ~0x40` 这种写法在撕裂读下会写出 0x07 这类垃圾
        （实测：读回 0x47 → 写回 0x07），而带脏值的 0x0727 会让 EC 停掉风扇输出。
        """
        try:
            want = self._latch_base & ~0x40
            for _ in range(3):
                self.ec.write(REG_FAN_TABLE_LATCH, want)
                time.sleep(0.02)
                if self._read_mode(REG_FAN_TABLE_LATCH, n=3) == want:
                    break
        except Exception:
            pass
        self._set_isolate(False)
        self._table_last = None

    # ---------- 静态寄存器巡检 ----------
    def check_power_registers(self):
        """校验掉电相关的电源控制寄存器，越界即写回最近一次良好值。

        这是**针对掉电机制本身**的兜底，不依赖任何关于表索引的假设：
        只要 0x083A / 0x0522 / 0x0520 被写坏，就会被发现并修回，
        无论它是被撕裂写、被别的程序改、还是别的原因破坏。

        返回说明文本列表，正常时返回 None。
        """
        notes = []
        for addr, name, (lo, hi) in POWER_GUARD_REGS:
            try:
                v = self.ec.read(addr) | (self.ec.read(addr + 1) << 8)
            except Exception:
                continue
            if lo <= v <= hi:
                self._power_base[addr] = v        # 合理值，滚动更新基线
                continue
            base = self._power_base.get(addr)
            if base is None:
                notes.append(f"{name}(0x{addr:04X}) 异常 {v}，尚无基线可回退")
                continue
            for _ in range(3):
                self.ec.write(addr, base & 0xFF)
                self.ec.write(addr + 1, (base >> 8) & 0xFF)
                time.sleep(0.02)
                now = self.ec.read(addr) | (self.ec.read(addr + 1) << 8)
                if lo <= now <= hi:
                    self._power_guards += 1
                    notes.append(f"{name}(0x{addr:04X}) 被改成 {v}，已修回 {base}")
                    self.diag.alert("power_guard",
                                    f"{name}(0x{addr:04X}) 被改成 {v}，已修回 {base}",
                                    detail={"addr": hex(addr), "bad": v, "restored": base})
                    break
            else:
                notes.append(f"{name}(0x{addr:04X}) 异常 {v} 且修复失败")
        return notes or None

    def check_static_registers(self):
        """巡检并修复被撕裂写破坏的静态寄存器。返回说明文本，正常时返回 None。

        三条被守护的都是出厂常量（5 份历史快照一致）：
          - 0x0408/0x0409 设计电压 15400 mV：被写坏会让系统报出离谱的设计容量。
          - 0x0727 bit6 风扇表锁存：本机出厂 0x00。撕裂写把它置上后，EC 会改用
            0x0F20/0x0F50 的自定义曲线表，而那块区域是未初始化的垃圾数据 → 需清除。
            但**表驱动模式下这个位是我们自己置的**，此时不能清。
          - 0x07C5 bit7 风扇输出隔离：本机出厂 0x00。置上后副扇改走自己的曲线表，
            而单温度机型该表查表温度恒 0 → 可能落到 0% 停转。
            同理，表驱动 + 副扇独立模式下这位是我们自己置的，必须保留。
        """
        notes = []
        # 表驱动模式下锁存位是我们主动置上的，巡检必须跳过，否则会跟自己打架
        table_active = (self.backend == "table"
                        and self.mode in ("manual", "curve"))
        # --- 设计电压 ---
        try:
            mv = (self.ec.read(REG_DESIGN_VOLTAGE)
                  | (self.ec.read(REG_DESIGN_VOLTAGE + 1) << 8))
        except Exception:
            mv = None
        if mv is not None and not (DESIGN_VOLTAGE_SANITY[0] <= mv <= DESIGN_VOLTAGE_SANITY[1]):
            for _ in range(3):
                self.ec.write(REG_DESIGN_VOLTAGE, DESIGN_VOLTAGE_MV & 0xFF)
                self.ec.write(REG_DESIGN_VOLTAGE + 1, (DESIGN_VOLTAGE_MV >> 8) & 0xFF)
                time.sleep(0.02)
                now = (self.ec.read(REG_DESIGN_VOLTAGE)
                       | (self.ec.read(REG_DESIGN_VOLTAGE + 1) << 8))
                if now == DESIGN_VOLTAGE_MV:
                    self._tear_repairs += 1
                    notes.append(f"设计电压被改成 {mv} mV，已修回 {DESIGN_VOLTAGE_MV} mV")
                    self.diag.alert("tear_repair",
                                    f"设计电压被改成 {mv} mV，已修回 {DESIGN_VOLTAGE_MV} mV",
                                    detail={"bad": mv, "restored": DESIGN_VOLTAGE_MV})
                    break
            else:
                notes.append(f"设计电压异常 {mv} mV 且修复失败，请运行 fix_design_voltage.py")
        # --- 风扇表锁存 0x0727 bit6 ---
        try:
            v = self._read_mode(REG_FAN_TABLE_LATCH)
            if v & 0x40 and not table_active:
                self.ec.write(REG_FAN_TABLE_LATCH, self._latch_base & ~0x40)
                time.sleep(0.02)
                if not (self._read_mode(REG_FAN_TABLE_LATCH, n=3) & 0x40):
                    self._tear_repairs += 1
                    notes.append(f"0x0727 锁存位被置上(0x{v:02X})，已清除")
                    self.diag.alert("tear_repair", f"0x0727 锁存位被置上(0x{v:02X})，已清除",
                                    detail={"reg": "0x0727", "bad": hex(v)})
        except Exception:
            pass
        # --- 风扇输出隔离 0x07C5 bit7 ---
        try:
            v = self._read_mode(REG_FAN_ISOLATE)
            # 表驱动 + 副扇独立时，隔离位是我们主动置上的，不能清
            if v & 0x80 and not (table_active and self._isolate_needed):
                self.ec.write(REG_FAN_ISOLATE, self._isolate_base & ~0x80)
                time.sleep(0.02)
                if not (self._read_mode(REG_FAN_ISOLATE, n=3) & 0x80):
                    self._tear_repairs += 1
                    notes.append(f"0x07C5 隔离位被置上(0x{v:02X})，已清除")
                    self.diag.alert("tear_repair", f"0x07C5 隔离位被置上(0x{v:02X})，已清除",
                                    detail={"reg": "0x07C5", "bad": hex(v)})
        except Exception:
            pass
        # 电源寄存器一并巡检（掉电兜底）
        pg = self.check_power_registers()
        if pg:
            notes.extend(pg)
        if not notes:
            return None
        return "撕裂写巡检：" + "；".join(notes) + "（系统电池信息需重启刷新）"

    def _maybe_resume_lowtemp(self, now):
        """低温交还的恢复：温度回到 阈值+回差 以上就重新接管曲线。

        每秒查一次即可（读 EC 有成本，3℃ 回差也不会一分钟内反复触发）。
        抽成独立方法是为了能离线测状态机，不必真的把 CPU 降到 45℃ 以下。
        """
        if not self._lowtemp_handoff or now - self._handoff_check < 1.0:
            return False
        self._handoff_check = now
        t_now = self._cpu_temp_safe()
        if t_now < HANDOFF_BELOW_TEMP + HANDOFF_RESUME_HYST:
            return False
        self.mode = self._handoff_from or "curve"
        self._lowtemp_handoff = False
        self.diag.event("lowtemp_resume", temp=t_now, mode=self.mode)
        return True

    def _loop(self):
        started = time.time()
        last_write = 0.0
        last_sample = 0.0
        prev_active = False
        while self.running:
            now = time.time()
            try:
                self._maybe_resume_lowtemp(now)
                active = self.mode in ("manual", "curve")
                # 从受控切回自动/保护态时，解除自定义曲线表，把控制权干净地交还 EC
                if prev_active and not active and self.backend == "table":
                    self._release_table()
                    self.diag.event("release_table",
                                    reason="切回自动/保护态", mode=self.mode)
                prev_active = active
                if active and now - last_write >= self.write_interval:
                    last_write = now
                    temp = self._cpu_temp_safe()
                    target, note = self._resolve_target(temp)
                    target2 = (None if target is None
                               else self._resolve_target2(target, temp))
                    if target is not None:
                        raw1 = max(1, min(DUTY_MAX_RAW, round(target / 100 * DUTY_MAX_RAW)))
                        raw2 = max(1, min(DUTY_MAX_RAW, round(target2 / 100 * DUTY_MAX_RAW)))
                        if self.backend == "table":
                            if (raw1, raw2) != self._table_last:
                                # 目标变了：重写整表 + 锁存（之后由 EC 自己保持），
                                # 再补一次直接占空比写，跳过约 3.4 raw/s 的软着陆斜坡
                                self._last_target_change = now
                                self._apply_table(raw1, raw2)
                                self.ec.write(REG_DUTY_FAN1, raw1)
                                self.ec.write(REG_DUTY_FAN2, raw2)
                            elif now - self._last_table >= self._table_interval:
                                # 心跳：先读一次，只有 EC 真的偏离目标才重锁 —— 稳态零写入。
                                # 两把扇都要查：隔离生效时它们各走各的表，可能只有一边漂。
                                self._last_table = now
                                cur1 = self._read_mode(REG_DUTY_FAN1, n=3)
                                cur2 = self._read_mode(REG_DUTY_FAN2, n=3)
                                if (abs(cur1 - raw1) > TABLE_DRIFT_TOL
                                        or abs(cur2 - raw2) > TABLE_DRIFT_TOL):
                                    # 连续多个心跳都发现偏离 = 有别的程序在跟我们抢 EC
                                    # （实测：L-Mechrevo 与 FanCtl 同时运行时，duty1 在
                                    #  51~113 之间锯齿振荡）。这时不能跟着加码狂写，
                                    #  否则撕裂写风险骤增 —— 改为告警 + 拉长重锁间隔。
                                    self._contention += 1
                                    self._apply_table(raw1, raw2)
                                else:
                                    self._contention = 0
                                if self._contention >= CONTENTION_RELATCH:
                                    self._table_interval = CONTENTION_HEARTBEAT
                                    note += (f" | 警告：检测到另一程序在争用 EC 端口"
                                             f"（目标已连续 {self._contention} 次被改回，"
                                             f"实测 0x1804={cur1}/0x1809={cur2}），"
                                             f"已放慢重锁；建议关闭 L-Mechrevo")
                        else:
                            # 两条记录都要写：只写主扇时，EC 会沿斜坡把副扇目标挪到 0，副扇停转。
                            self.ec.write(REG_DUTY_FAN1, raw1)
                            self.ec.write(REG_DUTY_FAN2, raw2)
                            self._table_last = None
                            if self._last2 != target2:
                                self._last_target_change = now
                    if target is not None and target2 != target:
                        src = "副曲线" if (self.curve2 and self.manual_percent2 is None) else "副固定"
                        note += f" | 主 {target}% / {src} {target2}%"
                    self._last = (temp, target, note)
                    self._last2 = target2

                if now - last_sample >= self.interval:
                    last_sample = now
                    if active:
                        temp, target, note = getattr(
                            self, "_last", (self.ec.cpu_temp(), None, "准备中"))
                        target2 = getattr(self, "_last2", target)
                    else:
                        temp = self._cpu_temp_safe()
                        if self._lowtemp_handoff:
                            target, target2 = None, None
                            resume = HANDOFF_BELOW_TEMP + HANDOFF_RESUME_HYST
                            # 温度可能已经回升到阈值附近但还没到恢复线，别写成 "56 < 56"
                            note = (f"低温交还中：{temp}°C，EC 原生策略"
                                    f"（{resume:.0f}°C 自动接管）")
                        else:
                            target, target2, note = None, None, "自动（EC 原生策略）"
                    snap = self.ec.snapshot()
                    # 静态寄存器巡检：撕裂写可能把数据写到任意地址，这里盯住已知会被误伤的那条
                    if now - self._last_guard >= GUARD_CHECK_INTERVAL:
                        self._last_guard = now
                        guard_note = self.check_static_registers()
                        if guard_note:
                            note = f"{note} | {guard_note}" if note else guard_note
                    # 副扇堵转看门狗：持续读不到转速，说明这一路很可能已被 EC 闭锁。
                    # 立刻停止写入并交还 EC，避免长时间带着停转的副扇跑。
                    #
                    # 两个误判来源必须挡掉，否则会"自己把自己交还出去"：
                    #   1. 目标刚变化后 EC 要走软着陆斜坡（满量程约 30s），期间 0 RPM 是正常的；
                    #   2. 副扇目标低于 20% 下限时 EC 本来就会停转它（原生节能策略）。
                    # 所以只在「静默期已过 + 目标确实要求转动」时才计数。
                    ramp_done = (now - self._last_target_change) >= FAN2_ZERO_GRACE
                    asked_spin = (target2 or 0) >= MANUAL_FLOOR_PERCENT
                    if active and ramp_done and asked_spin and snap["fan2_rpm"] == 0:
                        self._fan2_zero += 1
                    else:
                        self._fan2_zero = 0
                    if self._fan2_zero >= FAN2_ZERO_SAMPLES:
                        self._fan2_zero = 0
                        if self.backend == "table":
                            self._release_table()   # 交还前先解除自定义表
                        if active and self._fan2_independent() and not self._fan2_downgraded:
                            # 独立控制的副扇停转：先退回"跟随主扇"，保住主扇的控制权。
                            # 固定值和独立曲线都要清掉，否则降级不彻底、下一轮又被拉回独立。
                            self.manual_percent2 = None
                            self.curve2 = None
                            self._curve2_last = None
                            self._fan2_downgraded = True
                            target2 = target
                            note = "副扇持续 0 RPM，已退回跟随主扇"
                        else:
                            # 跟随模式下仍然停转，说明这一路被 EC 闭锁了，整体交还
                            self.mode = "auto"
                            target, target2, note = None, None, "副扇持续 0 RPM（疑似被 EC 判堵转），已交还 EC 自动控速"
                    with self.lock:
                        self.state.update({
                            "t": round(now - started, 1),
                            "mode": self.mode,
                            "cpu_temp": temp,
                            "gpu_temp": snap["gpu_temp"],
                            "fan1_rpm": snap["fan1_rpm"],
                            "fan2_rpm": snap["fan2_rpm"],
                            "duty_percent": snap["duty_percent"],
                            "duty_percent2": snap["duty_percent2"],
                            "target_percent": target,
                            "target_percent2": target2,
                            "note": note,
                            "handoff_lowtemp": self._lowtemp_handoff,
                            "tear_repairs": self._tear_repairs,
                            "power_guards": self._power_guards,
                            "error": None,
                        })
                        self._history.append((round(now - started, 1), temp, snap["fan1_rpm"],
                                              snap["fan2_rpm"], snap["duty_percent"]))
                        if len(self._history) > 1800:
                            self._history = self._history[-900:]
                    # 状态采样放在 lock 外面：日志内部会读 EC，
                    # 持着 self.lock 去读 EC 会和别的线程形成锁顺序反转。
                    self.diag.sample(
                        self.get_state(),
                        extra={"backend": self.backend,
                               "contention": self._contention,
                               "isolate": bool(self._isolate_needed)},
                    )
            except Exception as exc:                     # 出错即停止写入，让 EC 接管
                self.diag.alert("loop_exception", str(exc))
                with self.lock:
                    self.state["error"] = str(exc)
                    self.state["note"] = "发生错误，已停止写入，EC 自动接管"
                self.mode = "auto"
                time.sleep(1.0)
            time.sleep(min(0.02, self.write_interval / 2))

    def get_state(self):
        with self.lock:
            return dict(self.state)

    def get_history(self):
        with self.lock:
            return list(self._history)


# ---------- 命令行 ----------
def cmd_status(ec):
    s = ec.snapshot()
    print(f"CPU {s['cpu_temp']}°C   风扇1 {s['fan1_rpm']} RPM   风扇2 {s['fan2_rpm']} RPM   "
          f"占空比 {s['duty_percent']}% (raw 0x{s['duty_raw']:02X})")


def cmd_watch(ec, seconds=None):
    print(f"{'时间':>8}{'CPU°C':>7}{'风扇1':>8}{'风扇2':>8}{'占空比':>8}")
    start = time.time()
    try:
        while seconds is None or time.time() - start < seconds:
            s = ec.snapshot()
            print(f"{time.time()-start:>8.1f}{s['cpu_temp']:>7}{s['fan1_rpm']:>8}"
                  f"{s['fan2_rpm']:>8}{s['duty_percent']:>7}%")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已停止")


def _watch_loop(controller):
    """持续打印状态直到 Ctrl+C，然后干净地交还 EC。"""
    try:
        while True:
            st = controller.get_state()
            t2 = st.get("target_percent2")
            tgt = f"目标 {st['target_percent']}%/{t2}%" if t2 is not None else f"目标 {st['target_percent']}%"
            print(f"\r  CPU {st['cpu_temp']}°C  风扇1 {st['fan1_rpm']} RPM  风扇2 {st['fan2_rpm']} RPM  "
                  f"{tgt}  实到 {st['duty_percent']}%   [{st['note']}]   ", end="")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在交还 EC 控制…")
    finally:
        controller.configure(mode="auto")
        time.sleep(8)
        controller.stop()
        print("已退出，风扇回到 EC 自动策略。")


def parse_curve(text):
    """解析 "45:20,55:30,65:45" → [(45, 20), (55, 30), (65, 45)]"""
    pts = []
    for item in text.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        t, _, p = item.partition(":")
        pts.append((int(t), int(p)))
    return sorted(pts)


def _curve_desc(curve):
    return " ".join(f"{t}℃→{p}%" for t, p in curve)


def cmd_set(controller, percent, percent2=None):
    controller.start()
    controller.configure(mode="manual", manual_percent=percent, manual_percent2=percent2)
    desc = f"主 {percent}% / 副 {percent2}%" if percent2 is not None else f"{percent}%（副扇跟随）"
    print(f"正在维持 {desc} —— Ctrl+C 停止（停止后 6~8 秒 EC 自动接管）")
    _watch_loop(controller)


def cmd_curve(controller, curve, curve2=None):
    controller.start()
    controller.configure(mode="curve", curve=curve, curve2=curve2)
    desc = f"主 {_curve_desc(curve)}"
    desc += f" | 副 {_curve_desc(curve2)}" if curve2 else " | 副跟随主扇"
    print(f"按温度曲线调速：{desc} —— Ctrl+C 停止")
    _watch_loop(controller)


def cmd_auto(controller):
    ec = UniwillEC().open()
    print("确认当前已由 EC 接管：")
    try:
        cmd_watch(ec, 10)
    finally:
        ec.close()


def main():
    ap = argparse.ArgumentParser(description="Uniwill/同方 EC 风扇控制")
    ap.add_argument("command", choices=["status", "watch", "set", "curve", "auto", "release"])
    ap.add_argument("value", nargs="?", help="set 的百分比 / curve 的主扇曲线")
    ap.add_argument("--curve2", help="副扇独立曲线，如 45:25,60:40,70:60,85:100"
                                     "（不给则副扇跟随主扇）")
    ap.add_argument("--backend", choices=["duty", "table"], default="table",
                    help="控制后端：table=写曲线表+锁存（默认，几乎零写入）；"
                         "duty=25Hz 持续写占空比寄存器（旧行为）")
    args = ap.parse_args()

    if args.command == "release":
        print("本工具重启即失效；EC 会在 6~8 秒内自动接管，无需额外操作。")
        return 0

    try:
        ec = UniwillEC().open()
    except Exception as exc:
        print(f"错误：{exc}")
        return 1

    try:
        if args.command == "status":
            cmd_status(ec)
        elif args.command == "watch":
            cmd_watch(ec)
        elif args.command == "auto":
            cmd_auto(ec)
        elif args.command == "set":
            if args.value is None:
                print("请给出百分比，例如：fanctl.py set 60"
                      "\n分别控制两把扇：fanctl.py set 40:80（主 40% / 副 80%）"
                      "\n副扇跟随主扇：fanctl.py set 40:follow")
                return 1
            p1, p2 = (args.value.split(":") + ["follow"])[:2]
            percent = int(p1)
            percent2 = None if p2.strip().lower() in ("f", "follow", "跟随", "") else int(p2)
            ec.close()
            ctl = FanController(backend=args.backend)
            cmd_set(ctl, percent, percent2)
        elif args.command == "curve":
            if args.value is None:
                print("请给出主扇曲线，例如："
                      "fanctl.py curve 45:20,55:30,65:45,75:65,85:100"
                      "\n给副扇单独一条曲线："
                      "fanctl.py curve 45:20,55:30,65:45 --curve2 45:25,60:40,70:60")
                return 1
            ec.close()
            ctl = FanController(backend=args.backend)
            cmd_curve(ctl, parse_curve(args.value),
                      parse_curve(args.curve2) if args.curve2 else None)
    except Exception as exc:
        print(f"错误：{exc}")
        return 1
    finally:
        try:
            ec.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
