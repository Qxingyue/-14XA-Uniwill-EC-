# -*- coding: utf-8 -*-
"""风扇控制诊断日志。

把「用户做了什么操作」和「当时软件输出的数据值」绑在同一条时间线上记下来，
供事后排查 BUG、疑问和待改点。

设计要点（按 2026-10-26 与用户确认的方案）：

- 采样：**变化驱动 + 10 秒心跳**。温度/目标/占空比/转速/提示任一变化就记一条，
  长时间平稳时每 10 秒补一条。既能抓住瞬时跳变，文件又不会爆。
- 深度：**平时记关键项，异常时自动加深**。出现撕裂修复 / 电源守卫 / 端口争用 /
  目标与实到不符等情况时，自动附一份 EC 原始寄存器快照。
- 开关：**默认关闭**，由 GUI 上的复选框开启（本模块不自行启动）。
- 轮转：**按会话新建**（每次开启建一个新文件）+ 单文件超过 8MB 再切；
  目录下最多保留 20 个文件，超出删最老的。
- 落盘：**每条立即 flush**。这台机器有过异常掉电，缓存里的日志会丢。

日志格式：JSONL（每行一个 JSON 对象），kind 分四种：
  session / event / tick / alert
"""
import json
import os
import platform
import sys
import threading
import time
from datetime import datetime

if getattr(sys, "frozen", False):
    # 打包成单文件 EXE 后 __file__ 指向临时解包目录，退出即删。
    # 日志必须落在 EXE 旁边，否则用户永远找不到（也留不住）。
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIAG_DIR = os.path.join(BASE_DIR, "diag")
MAX_FILES = 20                       # 目录里最多保留多少个日志文件
MAX_FILE_BYTES = 8 * 1024 * 1024     # 单文件超过 8MB 就切一个新的
HEARTBEAT_SEC = 10.0                 # 平稳期的心跳间隔

# 目标与实到允许的正常偏差（%）：软着陆斜坡期本身就对不上
DUTY_GAP_TOL = 8
DUTY_GAP_HOLD = 30.0                 # 持续偏多久才判为可疑
FAN2_ZERO_HOLD = 20.0                # 副扇有目标却持续 0 RPM 多久判为可疑
TEMP_JUMP = 20.0                     # 一秒内温度跳变超过多少度判为撕裂假值
TEMP_VALID = (0, 120)

# 异常时加深快照要读的寄存器：地址 -> 名字。
# 全部是只读访问，且都走 UniwillEC._port_lock，不会引入新的撕裂。
DEEP_REGS = (
    (0x0727, "latch"),          # bit6 = 曲线表锁存
    (0x07C5, "isolate"),        # bit7 = 主副扇隔离
    (0x1804, "duty1_raw"),
    (0x1809, "duty2_raw"),
    (0x0408, "design_voltage"), # 小端 16 位
    (0x083A, "input_current_limit"),
    (0x0522, "target_voltage"),
    (0x0520, "target_current"),
    (0x0438, "pack_voltage"),
    (0x04AB, "soc"),
    (0x0F20, "table1_0"),
    (0x0F50, "table2_0"),
)
DEEP_16BIT = {0x0408, 0x083A, 0x0522, 0x0520, 0x0438}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"


def _read_mode(ec, addr, n=5):
    """众数采样：撕裂读会冒出孤立假值，读多次取出现最多的那个。"""
    vals = []
    for _ in range(n):
        try:
            vals.append(ec.read(addr))
        except Exception:
            pass
    if not vals:
        return None
    return max(set(vals), key=vals.count)


class DiagLog:
    """诊断日志。线程安全；未开启时所有方法都是空操作（开销约等于零）。"""

    def __init__(self, diag_dir=DIAG_DIR, max_files=MAX_FILES,
                 max_bytes=MAX_FILE_BYTES):
        self.dir = diag_dir
        self.max_files = max_files
        self.max_bytes = max_bytes

        self.enabled = False
        self.path = None
        self._fh = None
        self._lock = threading.Lock()
        self._seq = 0
        self._bytes = 0
        self.ec = None              # 由控制器 attach，用于异常加深快照

        # 变化驱动：上一次采样出的"指纹"，一样就跳过
        self._sig = None
        self._last_beat = 0.0
        # 标疑用的跨帧状态
        self._prev = None
        self._gap_since = None
        self._fan2_zero_since = None

    # ---------- 生命周期 ----------
    def attach(self, ec):
        """把 EC 句柄交给日志，异常时才能读寄存器做加深快照。"""
        self.ec = ec

    def enable(self, reason="gui"):
        """开启日志：建目录、轮转、开新会话文件。"""
        with self._lock:
            if self.enabled:
                return self.path
            try:
                os.makedirs(self.dir, exist_ok=True)
                self._rotate()
                self.path = self._new_path()
                self._fh = open(self.path, "a", encoding="utf-8")
                self._bytes = 0
                self._seq = 0
                self.enabled = True
            except Exception:
                self.enabled = False
                self._fh = None
                return None
            self._write({
                "kind": "session",
                "name": "start",
                "reason": reason,
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "os": platform.platform(),
                "file": os.path.basename(self.path),
                "ec": self.deep_ec(),
            })
            return self.path

    def disable(self, reason="gui"):
        with self._lock:
            if not self.enabled:
                return
            self._write({"kind": "session", "name": "stop", "reason": reason})
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self.enabled = False
            self._fh = None
            self._sig = None
            self._prev = None

    def _rotate(self):
        """保留最近 max_files 个文件，删掉更老的。"""
        try:
            names = [n for n in os.listdir(self.dir) if n.startswith("diag_")]
            names.sort()
            while len(names) >= self.max_files:
                try:
                    os.remove(os.path.join(self.dir, names[0]))
                except Exception:
                    pass
                names.pop(0)
        except Exception:
            pass

    def _new_path(self):
        """生成一个绝不重名的日志文件名。

        文件名只精确到秒是不够的：同一秒内切分（或同一秒内两次开启）
        会算出同一个名字，结果就是续写同一个文件，切分形同虚设。
        这里同名就自动加序号。
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self.dir, f"diag_{ts}")
        path = base + ".jsonl"
        n = 1
        while os.path.exists(path):
            n += 1
            path = f"{base}_{n}.jsonl"
        return path

    def _cut_if_large(self):
        """单文件超过上限就切一个新的（同一会话内继续记）。"""
        if self._bytes < self.max_bytes or not self._fh:
            return
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        self.path = self._new_path()
        self._bytes = 0
        try:
            self._fh = open(self.path, "a", encoding="utf-8")
            self._write({"kind": "session", "name": "rotate",
                         "reason": f"超过 {self.max_bytes} 字节，切新文件",
                         "file": os.path.basename(self.path)})
        except Exception:
            self.enabled = False

    def _write(self, rec):
        if not self._fh:
            return
        rec.setdefault("ts", _now())
        self._seq += 1
        rec["seq"] = self._seq
        try:
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            self._fh.write(line)
            self._fh.flush()          # 掉电不丢日志
            self._bytes += len(line.encode("utf-8"))
            self._cut_if_large()
        except Exception:
            pass

    # ---------- 记录接口 ----------
    def event(self, name, **detail):
        """用户操作。detail 里放本次操作的参数值。"""
        if not self.enabled:
            return
        with self._lock:
            self._write({"kind": "event", "name": name, "detail": detail})

    def alert(self, name, msg, detail=None):
        """异常/告警：自动附 EC 寄存器快照。"""
        if not self.enabled:
            return
        # EC 快照必须在取日志锁**之前**读完：否则会和"持端口锁时写日志"互相等待。
        snap = self.deep_ec()
        with self._lock:
            rec = {"kind": "alert", "name": name, "msg": msg}
            if detail:
                rec["detail"] = detail
            rec["ec"] = snap
            self._write(rec)

    def sample(self, state, extra=None, force=False):
        """状态采样：变化驱动 + 心跳。state 是 FanController.get_state() 的返回值。"""
        if not self.enabled:
            return
        now = time.time()
        sig = self._signature(state)
        with self._lock:
            changed = (sig != self._sig)
            heartbeat = (now - self._last_beat) >= HEARTBEAT_SEC
            if not (changed or heartbeat or force):
                return
            self._sig = sig
            self._last_beat = now
            flags = self._detect(state, extra)
            rec = {
                "kind": "tick",
                "changed": changed,
                "heartbeat": heartbeat and not changed,
                "mode": state.get("mode"),
                "backend": (extra or {}).get("backend"),
                "cpu_temp": state.get("cpu_temp"),
                "target": state.get("target_percent"),
                "target2": state.get("target_percent2"),
                "duty": state.get("duty_percent"),
                "duty2": state.get("duty_percent2"),
                "fan1_rpm": state.get("fan1_rpm"),
                "fan2_rpm": state.get("fan2_rpm"),
                "note": state.get("note"),
                "tear_repairs": state.get("tear_repairs"),
                "power_guards": state.get("power_guards"),
                "contention": (extra or {}).get("contention"),
            }
            if flags:
                rec["flags"] = flags
            if state.get("error"):
                rec["error"] = state.get("error")
                rec.setdefault("flags", []).append("控制器报错")
            need_deep = bool(flags) or bool(state.get("error"))
        # 异常时自动加深：读 EC 放在锁外，避免与端口锁形成死锁
        if need_deep:
            rec["ec"] = self.deep_ec()
        with self._lock:
            self._write(rec)

    # ---------- 内部 ----------
    @staticmethod
    def _signature(state):
        return (
            state.get("mode"),
            state.get("cpu_temp"),
            state.get("target_percent"),
            state.get("target_percent2"),
            state.get("duty_percent"),
            state.get("duty_percent2"),
            state.get("fan1_rpm"),
            state.get("fan2_rpm"),
            state.get("note"),
            state.get("tear_repairs"),
            state.get("power_guards"),
        )

    def _detect(self, state, extra):
        """自动标疑：把"看着不对劲"的地方直接标出来，省得事后逐行翻。"""
        flags = []
        now = time.time()
        prev = self._prev or {}
        cur = dict(state)

        # 1) 撕裂修复 / 电源守卫 新增
        for key, label in (("tear_repairs", "撕裂写自愈"), ("power_guards", "电源寄存器被修复")):
            p, c = prev.get(key), cur.get(key)
            if p is not None and c is not None and c > p:
                flags.append(f"{label} +{c - p}（累计 {c}）")
        # 2) 端口争用
        note = cur.get("note") or ""
        if "争用" in note:
            flags.append("检测到端口争用（可能有别的程序在写 EC）")
        if extra:
            cont = extra.get("contention")
            if cont and cont != prev.get("_contention"):
                if cont > (prev.get("_contention") or 0):
                    flags.append(f"争用计数上升至 {cont}")

        # 3) 温度假值（撕裂读）
        t, pt = cur.get("cpu_temp"), prev.get("cpu_temp")
        if isinstance(t, int):
            if not (TEMP_VALID[0] <= t <= TEMP_VALID[1]):
                flags.append(f"温度读数越界 {t}℃（撕裂读假值）")
            elif isinstance(pt, int) and TEMP_VALID[0] <= pt <= TEMP_VALID[1]:
                if abs(t - pt) > TEMP_JUMP:
                    flags.append(f"温度 1 秒内跳变 {pt}→{t}℃")

        # 4) 目标与实到长期不符（排除斜坡期）
        tgt, duty = cur.get("target_percent"), cur.get("duty_percent")
        if tgt is not None and isinstance(duty, int) and cur.get("mode") != "auto":
            if abs(tgt - duty) > DUTY_GAP_TOL:
                if self._gap_since is None:
                    self._gap_since = now
                elif now - self._gap_since > DUTY_GAP_HOLD:
                    flags.append(f"目标 {tgt}% 实到 {duty}%，已持续 "
                                 f"{now - self._gap_since:.0f} 秒未追上")
            else:
                self._gap_since = None
        else:
            self._gap_since = None

        # 5) 副扇有目标却长时间 0 RPM
        t2, r2 = cur.get("target_percent2"), cur.get("fan2_rpm")
        if t2 is not None and t2 >= 20 and r2 == 0:
            if self._fan2_zero_since is None:
                self._fan2_zero_since = now
            elif now - self._fan2_zero_since > FAN2_ZERO_HOLD:
                flags.append(f"副扇目标 {t2}% 但 0 RPM 已持续 "
                             f"{now - self._fan2_zero_since:.0f} 秒")
        else:
            self._fan2_zero_since = None

        # 6) 两扇目标不同却没有隔离位（独立控制会失效）
        if t2 is not None and tgt is not None and t2 != tgt and extra:
            if extra.get("backend") == "table" and not extra.get("isolate"):
                flags.append("主副扇目标不同但隔离位未置起（副扇会跟随主扇）")

        # 7) 非用户操作导致的交还 EC
        #    注意：用户自己点"自动/恢复到自动"是正常的，不算异常。
        #    只有控制器**自己**交还（临界温度、看门狗）才值得标出来，
        #    判据是提示语里带"交还/临界/超过/停转"这类字样。
        if cur.get("mode") == "auto" and prev.get("mode") not in (None, "auto"):
            if any(k in note for k in ("交还", "临界", "超过", "停转", "错误")):
                flags.append(f"自动交还 EC：{note}")

        # 8) 两扇目标相同，转速却差很多（一个转一个不转）
        r1 = cur.get("fan1_rpm")
        if (tgt is not None and t2 is not None and tgt == t2
                and isinstance(r1, int) and isinstance(r2, int)):
            if abs(r1 - r2) > 1500:
                flags.append(f"两扇目标同为 {tgt}%，但转速 {r1}/{r2} RPM 差异过大")

        # 记住这一帧，供下一帧比对
        cur["_contention"] = (extra or {}).get("contention")
        self._prev = cur
        return flags

    # ---------- EC 加深快照 ----------
    def deep_ec(self):
        """读一份 EC 关键寄存器快照。全部只读，且走端口锁。"""
        ec = self.ec
        if ec is None:
            return None
        out = {}
        for addr, name in DEEP_REGS:
            try:
                if addr in DEEP_16BIT:
                    lo = _read_mode(ec, addr)
                    hi = _read_mode(ec, addr + 1)
                    out[name] = None if (lo is None or hi is None) else lo | (hi << 8)
                else:
                    out[name] = _read_mode(ec, addr)
            except Exception:
                out[name] = None
        # 位域单独拆出来，省得看日志时还要自己算
        try:
            out["latch_bit6"] = (out.get("latch") or 0) >> 6 & 1
            out["isolate_bit7"] = (out.get("isolate") or 0) >> 7 & 1
        except Exception:
            pass
        return out
