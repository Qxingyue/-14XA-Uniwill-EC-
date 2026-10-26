# -*- coding: utf-8 -*-
"""风扇控制图形界面（机械革命无界14XA / Uniwill EC）。

必须以管理员权限运行，否则无法访问 inpoutx64 端口驱动。
界面关闭时会自动切换回 EC 原生策略（6~8 秒内生效）。
"""
import os
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fanctl import FanController, DUTY_MAX_RAW, MANUAL_FLOOR_PERCENT  # noqa: E402
from fandiag import DIAG_DIR  # noqa: E402

# 显示在标题栏副行：用来一眼分辨"窗口是旧进程"还是"改的代码没生效"
APP_VER = "v2.0 · 2026-10-27"

BG = "#f5f6f8"
CARD = "#ffffff"
INK = "#1f2430"
SUB = "#6b7280"
ACCENT = "#2563eb"
HOT = "#dc2626"
OKG = "#059669"

# 下限 5：2026-10-26 实测 3%~20% 全部稳定，只有 0% 会让两扇在 0~800 RPM 反复启停。
# 「静音」给 10%：table 后端下已近 1745 RPM 地板，比原来的 20% 安静一档。
PRESETS = [("静音", 10), ("安静", 25), ("日常", 35), ("均衡", 50), ("强冷", 75), ("全速", 100)]

# 「重置」键回填的出厂默认值（只填草稿，仍需点「应用曲线」才生效）
DEFAULT_CURVE = [(45, 20), (55, 30), (65, 45), (75, 65), (85, 100)]
DEFAULT_CURVE2 = [(45, 25), (55, 40), (65, 60), (75, 80), (85, 100)]
DEFAULT_PROTECT = 85
DEFAULT_CRITICAL = 95


class _Tooltip:
    """悬停显示全文的工具提示。

    给被省略号截断的底部状态条用：截断是为了保证版面不塌，
    但完整信息必须还能看到，否则等于把关键报错吞了。
    """

    def __init__(self, widget, get_text, delay=500):
        self.widget = widget
        self.get_text = get_text
        self.delay = delay
        self.win = None
        self.job = None
        widget.bind("<Enter>", self._enter)
        widget.bind("<Leave>", self._leave)
        widget.bind("<ButtonPress>", self._leave)

    def _enter(self, _event=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _leave(self, _event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        if self.win is not None:
            return
        text = self.get_text()
        if not text:
            return
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        self.win.wm_attributes("-topmost", True)
        self.win.wm_geometry(
            f"+{self.widget.winfo_rootx() + 8}+{self.widget.winfo_rooty() + 26}")
        tk.Label(self.win, text=text, bg="#1f2937", fg="#f9fafb", wraplength=560,
                 justify="left", anchor="w", padx=8, pady=6,
                 font=("Microsoft YaHei UI", 9)).pack()

    def _hide(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None


class App:
    def __init__(self, root):
        self.root = root
        root.title("风扇控制 FanCtl — 无界14XA / Uniwill EC")
        root.geometry("760x800")
        root.configure(bg=BG)
        root.resizable(False, False)

        self.controller = None
        self._ema = {}          # 转速读数平滑用的状态
        # 曲线输入框是「草稿」：改完只改文字，不下发。必须点「应用曲线」才生效。
        # 这一点由 _dirty_fields 记录，并在按钮上做视觉提示（未应用时按钮变蓝）。
        self._dirty_fields = set()
        # 短暂提示（应用成功/警告），tick 里优先显示几秒，然后回落到控制器状态
        self._flash_text = ""
        self._flash_color = OKG
        self._flash_until = 0.0
        try:
            self.controller = FanController(interval=0.5, backend="table")
            self.controller.start()
        except Exception as exc:
            messagebox.showerror("无法连接 EC", f"{exc}\n\n请右键本程序 → 以管理员身份运行。")
            root.destroy()
            return

        self._build_style()
        self._build_header()
        self._build_stats()
        self._build_chart()
        self._build_controls()
        self._build_footer()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    # ---------- 样式 ----------
    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 9))
        st.configure("Card.TLabel", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 9))
        st.configure("H1.TLabel", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 9))
        st.configure("Big.TLabel", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 20, "bold"))
        st.configure("Sub.TLabel", background=CARD, foreground=SUB, font=("Microsoft YaHei UI", 8))
        st.configure("CardSub.TLabel", background=CARD, foreground=SUB, font=("Microsoft YaHei UI", 8))
        st.configure("TButton", font=("Microsoft YaHei UI", 9))
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        st.configure("TCheckbutton", background=CARD)
        st.configure("TRadiobutton", background=CARD)
        st.configure("TLabelframe", background=CARD)
        st.configure("TLabelframe.Label", background=CARD, foreground=INK, font=("Microsoft YaHei UI", 9, "bold"))

    def _card(self, parent, w, h, x, y):
        c = tk.Frame(parent, bg=CARD, width=w, height=h, highlightthickness=1,
                     highlightbackground="#e5e7eb", highlightcolor="#e5e7eb")
        c.place(x=x, y=y, width=w, height=h)
        return c

    # ---------- 顶部 ----------
    def _build_header(self):
        head = self._card(self.root, 720, 56, 20, 16)
        tk.Label(head, text="风扇控制 FanCtl", bg=CARD, fg=INK,
                 font=("Microsoft YaHei UI", 13, "bold")).place(x=16, y=10)
        tk.Label(head, text=f"机械革命 无界14XA · Uniwill EC · 需管理员权限 · {APP_VER}", bg=CARD, fg=SUB,
                 font=("Microsoft YaHei UI", 8)).place(x=16, y=31)
        # 右上角状态：文字右对齐到一个固定宽度区（430~686），**右侧留出空白**
        # 给「EC 原生 · 低温」「曲线 65% / 副 80%」这类长文案。原来左对齐且不设宽度，
        # 文字一长就被卡片右边界直接裁掉（只显示成"EC 原生 · 低"）。
        self.dot = tk.Label(head, text="●", bg=CARD, fg=OKG, font=("Microsoft YaHei UI", 10))
        self.dot.place(x=692, y=26)
        self.mode_text = tk.Label(head, text="自动", bg=CARD, fg=OKG,
                                  font=("Microsoft YaHei UI", 10, "bold"), anchor="e")
        self.mode_text.place(x=424, y=26, width=262, height=24)

    # ---------- 状态卡片 ----------
    def _build_stats(self):
        self.stats = {}
        # 五张卡：主/副扇的转速与占空比各自独立显示。
        # EC 里是两条对称记录（0x1804 → FAN1、0x1809 → FAN2），勾了「副扇独立」
        # 之后两个占空比会不一样，只显示一个会让人误以为副扇也跟着主扇转。
        items = [("CPU 温度", "cpu_temp", "°C", 20, INK),
                 ("主风扇", "fan1_rpm", "RPM", 166, INK),
                 ("副风扇", "fan2_rpm", "RPM", 312, INK),
                 ("主占空比", "duty_percent", "%", 458, ACCENT),
                 ("副占空比", "duty_percent2", "%", 604, "#7c3aed")]
        for title, key, unit, x, color in items:
            c = self._card(self.root, 134, 84, x, 84)
            tk.Label(c, text=title, bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9)).place(x=14, y=12)
            v = tk.Label(c, text="--", bg=CARD, fg=color, font=("Microsoft YaHei UI", 22, "bold"))
            v.place(x=14, y=32)
            # y 从 64 提到 60：9pt 的实际高度是 23px（linespace 21 + 默认 pady），
            # 64+23=87 会伸出 84 高的卡片下沿 3px——单位标签被检查器抓到了。
            tk.Label(c, text=unit, bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9)).place(x=14, y=60)
            self.stats[key] = v

    # ---------- 曲线图 ----------
    def _build_chart(self):
        c = self._card(self.root, 720, 142, 20, 180)
        tk.Label(c, text="历史曲线", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).place(x=14, y=10)
        self.canvas = tk.Canvas(c, width=692, height=104, bg=CARD, highlightthickness=0)
        self.canvas.place(x=14, y=28)
        self.canvas.create_text(16, 6, anchor="nw", text="温度", fill=HOT, font=("Microsoft YaHei UI", 8))
        self.canvas.create_text(52, 6, anchor="nw", text="风扇1", fill=ACCENT, font=("Microsoft YaHei UI", 8))
        self.canvas.create_text(92, 6, anchor="nw", text="风扇2", fill="#7c3aed", font=("Microsoft YaHei UI", 8))
        self.canvas.create_text(132, 6, anchor="nw", text="占空比", fill="#9ca3af", font=("Microsoft YaHei UI", 8))

    def _draw_chart(self, hist):
        cv = self.canvas
        cv.delete("all")
        w, h = 692, 104
        cv.create_line(0, h - 1, w, h - 1, fill="#e5e7eb")
        if len(hist) < 2:
            return
        n = min(len(hist), 300)
        data = hist[-n:]
        def plot(values, vmin, vmax, color, dash=None):
            pts = []
            for i, v in enumerate(values):
                x = i / (len(data) - 1) * (w - 2) + 1
                ratio = (v - vmin) / max(1, vmax - vmin)
                y = (h - 14) - ratio * (h - 30)
                pts += [x, y]
            cv.create_line(*pts, fill=color, width=1.6, smooth=False, dash=dash)
        temps = [d[1] for d in data]
        f1 = [d[2] for d in data]
        f2 = [d[3] for d in data]
        dut = [d[4] for d in data]
        tmin, tmax = 30, max(80, max(temps) + 5)
        rmax = max(2000, max(max(f1), max(f2)) * 1.1, 6000 * 0.6)
        plot(temps, tmin, tmax, HOT)
        plot(f1, 0, rmax, ACCENT)
        plot(f2, 0, rmax, "#7c3aed")
        plot(dut, 0, 100, "#9ca3af", dash=(3, 2))
        cv.create_text(4, h - 10, anchor="nw", text=f"{tmin}°C", fill=SUB, font=("Microsoft YaHei UI", 8))
        cv.create_text(4, 2, anchor="nw", text=f"{int(tmax)}°C", fill=SUB, font=("Microsoft YaHei UI", 8))
        cv.create_text(w - 46, h - 10, anchor="nw", text=f"{int(rmax)} RPM", fill=SUB,
                       font=("Microsoft YaHei UI", 8))
        cv.create_text(w - 200, 2, anchor="nw", text="温度", fill=HOT, font=("Microsoft YaHei UI", 8))
        cv.create_text(w - 164, 2, anchor="nw", text="风扇1", fill=ACCENT, font=("Microsoft YaHei UI", 8))
        cv.create_text(w - 124, 2, anchor="nw", text="风扇2", fill="#7c3aed", font=("Microsoft YaHei UI", 8))
        cv.create_text(w - 84, 2, anchor="nw", text="占空比", fill="#9ca3af", font=("Microsoft YaHei UI", 8))

    # ---------- 控制区 ----------
    def _build_controls(self):
        c = self._card(self.root, 720, 406, 20, 330)
        self.mode_var = tk.StringVar(value="auto")
        self.pct_var = tk.IntVar(value=50)
        self.pct2_var = tk.IntVar(value=50)
        self.fan2_indep_var = tk.BooleanVar(value=False)
        self.curve_var = tk.BooleanVar(value=False)
        self.curve2_var = tk.BooleanVar(value=False)   # 副扇独立曲线；不勾则跟随主扇
        self.preset_buttons = []
        self._ema = {}          # 转速读数平滑用的状态

        # 已提交（真正下发给控制器）的曲线与阈值。输入框里的只是草稿，
        # 点「应用曲线」才把草稿写回这几个变量；apply_mode 永远用这里的值，
        # 所以"改了输入框却没点应用"绝不会影响控制器。
        self.curve_committed = list(DEFAULT_CURVE)
        self.curve2_committed = list(DEFAULT_CURVE2)
        self.protect_committed = DEFAULT_PROTECT
        self.critical_committed = DEFAULT_CRITICAL

        tk.Label(c, text="模式", bg=CARD, fg=INK, font=("Microsoft YaHei UI", 10, "bold")).place(x=16, y=14)
        rb1 = tk.Radiobutton(c, text="自动（交还给 EC 原生策略）", value="auto", variable=self.mode_var,
                             bg=CARD, fg=INK, selectcolor="#ffffff", font=("Microsoft YaHei UI", 9),
                             activebackground=CARD, command=self.apply_mode)
        rb2 = tk.Radiobutton(c, text="手动维持占空比", value="manual", variable=self.mode_var,
                             bg=CARD, fg=INK, selectcolor="#ffffff", font=("Microsoft YaHei UI", 9),
                             activebackground=CARD, command=self.apply_mode)
        rb3 = tk.Radiobutton(c, text="温度曲线自动调速", value="curve", variable=self.mode_var,
                             bg=CARD, fg=INK, selectcolor="#ffffff", font=("Microsoft YaHei UI", 9),
                             activebackground=CARD, command=self.apply_mode)
        # 原来把三条的 x 写死成 16/240/430，而文字宽度随字体和 DPI 变，
        # 结果第二、三条几乎贴在一起。改成按实际宽度均分（见 _layout_mode_row）。
        self.mode_radios = [rb1, rb2, rb3]
        self._layout_mode_row()
        self.root.after(60, self._layout_mode_row)   # 字体度量就绪后再校一次

        tk.Label(c, text="占空比", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9)).place(x=16, y=70)
        self.slider = tk.Scale(c, from_=MANUAL_FLOOR_PERCENT, to=100, orient="horizontal", length=430,
                               variable=self.pct_var, showvalue=0,
                               bg=CARD, fg=INK, troughcolor="#e5e7eb", highlightthickness=0,
                               activebackground=ACCENT, bd=0, command=self.on_slider)
        self.slider.place(x=70, y=58)
        self.pct_label = tk.Label(c, text="50%", bg=CARD, fg=ACCENT, font=("Microsoft YaHei UI", 12, "bold"))
        self.pct_label.place(x=512, y=66)

        # 副扇独立控制：0x1804 / 0x1809 是两条独立记录，EC 只是"习惯上"让副扇跟随主扇
        tk.Checkbutton(c, text="副扇独立", variable=self.fan2_indep_var,
                       bg=CARD, fg=INK, selectcolor="#ffffff", font=("Microsoft YaHei UI", 9),
                       activebackground=CARD, command=self.apply_fan2).place(x=16, y=90)
        self.slider2 = tk.Scale(c, from_=MANUAL_FLOOR_PERCENT, to=100, orient="horizontal", length=330,
                                variable=self.pct2_var, showvalue=0,
                                bg=CARD, fg="#7c3aed", troughcolor="#e5e7eb", highlightthickness=0,
                                activebackground="#7c3aed", bd=0, command=self.on_slider2)
        self.slider2.place(x=140, y=86)
        self.pct2_label = tk.Label(c, text="跟随", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 11, "bold"))
        self.pct2_label.place(x=482, y=94)

        tk.Label(c, text="快捷预设", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9)).place(x=16, y=140)
        for i, (name, pct) in enumerate(PRESETS):
            b = tk.Button(c, text=f"{name}\n{pct}%", width=8, height=2, bg="#eef2ff", fg=INK,
                          relief="flat", font=("Microsoft YaHei UI", 8),
                          command=lambda p=pct: self.apply_preset(p))
            b.place(x=80 + i * 84, y=136)
            self.preset_buttons.append(b)

        self.curve_chk = tk.Checkbutton(c, text="启用温度曲线（按 CPU 温度自动调速）",
                                        variable=self.curve_var,
                                        bg=CARD, fg=INK, selectcolor="#ffffff",
                                        font=("Microsoft YaHei UI", 9),
                                        activebackground=CARD, command=self.apply_curve)
        self.curve_chk.place(x=16, y=186)

        # 这行标签右对齐到 x=342：输入框从 344 开始，左对齐的标签一旦变长
        # 就会压住第一格的首位数字（"20" 看着像 "10"）——右对齐 + 固定宽度才是解。
        tk.Label(c, text="温度≥", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8),
                 anchor="e").place(x=290, y=190, width=52, height=14)
        self.curve_entries = []
        for i, (t0, p0) in enumerate(self.curve_committed):
            te = tk.Entry(c, width=4, justify="center", bg="#ffffff", relief="solid", bd=1,
                          font=("Microsoft YaHei UI", 8))
            te.place(x=344 + i * 62, y=186)
            te.insert(0, str(t0))
            pe = tk.Entry(c, width=4, justify="center", bg="#ffffff", relief="solid", bd=1,
                          font=("Microsoft YaHei UI", 8))
            pe.place(x=344 + i * 62, y=210)
            pe.insert(0, str(p0))
            self.curve_entries.append((te, pe))
        # 标签必须够短：x=344 是第一列输入框，标签压过去就会盖住数值（"20" 看着像 "10"）
        tk.Label(c, text="→主扇%", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8),
                 anchor="e").place(x=290, y=214, width=52, height=14)

        # 副扇独立曲线：不勾选时副扇沿用主扇的解算结果
        self.curve2_chk = tk.Checkbutton(c, text="副扇独立曲线（不勾则跟随主扇）",
                                         variable=self.curve2_var,
                                         bg=CARD, fg="#7c3aed", selectcolor="#ffffff",
                                         font=("Microsoft YaHei UI", 9),
                                         activebackground=CARD, command=self.apply_curve2)
        self.curve2_chk.place(x=16, y=236)
        tk.Label(c, text="温度≥", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8),
                 anchor="e").place(x=290, y=240, width=52, height=14)
        self.curve2_entries = []
        for i, (t0, p0) in enumerate(self.curve2_committed):
            te = tk.Entry(c, width=4, justify="center", bg="#ffffff", relief="solid", bd=1,
                          fg="#7c3aed", font=("Microsoft YaHei UI", 8))
            te.place(x=344 + i * 62, y=236)
            te.insert(0, str(t0))
            pe = tk.Entry(c, width=4, justify="center", bg="#ffffff", relief="solid", bd=1,
                          fg="#7c3aed", font=("Microsoft YaHei UI", 8))
            pe.place(x=344 + i * 62, y=260)
            pe.insert(0, str(p0))
            self.curve2_entries.append((te, pe))
        tk.Label(c, text="→副扇%", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8),
                 anchor="e").place(x=290, y=264, width=52, height=14)

        # 曲线区下面的常驻说明条。这条策略很容易被误解成「到 45℃ 风扇就停」，
        # 或者以为手动模式也会被抢，所以触发条件、回差、生效范围都写在界面上。
        # 字号：7pt 在 200% 缩放下发糊（linespace 只有 14px），改 8pt；
        # 正文灰度也从 #6b7280 加深到 #374151——原来的浅灰在浅底上对比度不够，
        # 放大后仍然看不清。框宽放到 688（整卡宽），8pt 最长一行 569px，够用。
        NOTE_BG, NOTE_FG = "#f7f9fc", "#374151"
        note = tk.Frame(c, bg=NOTE_BG, highlightthickness=1, highlightbackground="#dde3ec")
        note.place(x=16, y=286, width=688, height=60)
        self.note_box = note      # 测试用：核对字号/内容
        tk.Label(note, text="低温交还：曲线模式下 CPU 低于 45℃ 交还 EC，回升到 ≥ 48℃ 自动接管曲线。",
                 bg=NOTE_BG, fg=INK, font=("Microsoft YaHei UI", 8, "bold"), anchor="w",
                 justify="left").place(x=8, y=3, width=672)
        tk.Label(note, text="3℃ 回差（45 交还 / 48 接管）防止温度在阈值上抖动导致转速反复起落；交还后清锁存位、停止写入。",
                 bg=NOTE_BG, fg=NOTE_FG, font=("Microsoft YaHei UI", 8), anchor="w",
                 justify="left").place(x=8, y=21, width=672)
        tk.Label(note, text="手动模式不受影响（那是你明确下的指令）；停写后风扇完全按 EC 自己的策略走，低负载时通常比强制低速更安静。",
                 bg=NOTE_BG, fg=NOTE_FG, font=("Microsoft YaHei UI", 8), anchor="w",
                 justify="left").place(x=8, y=39, width=672)

        tk.Label(c, text="保护：温度≥", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).place(x=16, y=354)
        self.protect_var = tk.StringVar(value=str(self.protect_committed))
        pe = tk.Entry(c, width=4, textvariable=self.protect_var, justify="center", bg="#ffffff",
                      relief="solid", bd=1, font=("Microsoft YaHei UI", 8))
        pe.place(x=88, y=351)
        tk.Label(c, text="℃ 强制全速；≥", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).place(x=122, y=354)
        self.critical_var = tk.StringVar(value=str(self.critical_committed))
        ce = tk.Entry(c, width=4, textvariable=self.critical_var, justify="center", bg="#ffffff",
                      relief="solid", bd=1, font=("Microsoft YaHei UI", 8))
        ce.place(x=208, y=351)
        tk.Label(c, text="℃ 立即交还 EC", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).place(x=240, y=354)

        # 控制后端：老方案靠 25Hz 狂写压 EC，新方案写曲线表一次、由 EC 自己保持
        tk.Label(c, text="后端", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 8)).place(x=16, y=382)
        self.backend_var = tk.StringVar(value="table")
        tk.Radiobutton(c, text="曲线表+锁存（推荐，几乎零写入）", value="table",
                       variable=self.backend_var, bg=CARD, fg=INK, selectcolor="#ffffff",
                       font=("Microsoft YaHei UI", 8), activebackground=CARD,
                       command=self.apply_backend).place(x=60, y=380)
        tk.Radiobutton(c, text="高频写寄存器（旧行为）", value="duty",
                       variable=self.backend_var, bg=CARD, fg=INK, selectcolor="#ffffff",
                       font=("Microsoft YaHei UI", 8), activebackground=CARD,
                       command=self.apply_backend).place(x=330, y=380)

        # 「确定键」：曲线/保护温度的输入框只编辑草稿，点这里才校验并下发。
        # 下面还有一个「重置」：把草稿填回出厂默认值，同样要点这里才生效。
        self.apply_btn = tk.Button(c, text="应用\n曲线", bg="#eef2ff", fg=ACCENT, relief="flat",
                                   font=("Microsoft YaHei UI", 9, "bold"),
                                   activebackground="#dbeafe", activeforeground=ACCENT,
                                   cursor="hand2", command=self.apply_curve_settings)
        self.apply_btn.place(x=638, y=186, width=76, height=44)
        self.reset_btn = tk.Button(c, text="重置\n默认", bg="#f3f4f6", fg=INK, relief="flat",
                                   font=("Microsoft YaHei UI", 9),
                                   activebackground="#e5e7eb", activeforeground=INK,
                                   cursor="hand2", command=self.reset_curve_defaults)
        self.reset_btn.place(x=638, y=234, width=76, height=44)
        self.dirty_note = tk.Label(c, text="", bg=CARD, fg="#d97706",
                                   font=("Microsoft YaHei UI", 8))
        self.dirty_note.place(x=392, y=354)

        self._sync_mode_widgets()
        self._bind_edit_watchers()

    # ---------- 输入框变更监视（草稿模式） ----------
    def _bind_edit_watchers(self):
        """盯住所有输入框，但只在 UI 上标"有未应用修改"，绝不自动下发。

        设计约定（用户明确要求）：输入框改完不生效，必须点「应用曲线设置」。
        这样"改了数字却没反应"变成一个可见的状态，而不是隐藏的坑。
        """
        for i, (te, pe) in enumerate(self.curve_entries):
            self._bind_edit(te, f"主曲线温度{i + 1}")
            self._bind_edit(pe, f"主曲线占空比{i + 1}")
        for i, (te, pe) in enumerate(self.curve2_entries):
            self._bind_edit(te, f"副曲线温度{i + 1}")
            self._bind_edit(pe, f"副曲线占空比{i + 1}")
        try:
            self.protect_var.trace_add("write", lambda *_: self._mark_edit("保护温度"))
            self.critical_var.trace_add("write", lambda *_: self._mark_edit("临界温度"))
        except Exception:
            pass

    def _bind_edit(self, widget, key):
        def handler(_event=None):
            try:
                self._mark_edit(key)
            except Exception:
                pass
        try:
            widget.bind("<KeyRelease>", handler)
            # 粘贴/拖放不一定触发 KeyRelease，焦点离开再补一次
            widget.bind("<FocusOut>", handler)
        except Exception:
            pass

    def _mark_edit(self, key):
        if key in self._dirty_fields:
            return
        self._dirty_fields.add(key)
        self._refresh_apply_state()
        self._ev("edit", field=key)

    def _refresh_apply_state(self):
        """有草稿未应用 -> 按钮变实心蓝 + 提示；已应用 -> 恢复浅蓝。"""
        dirty = bool(self._dirty_fields)
        try:
            if dirty:
                self.apply_btn.config(bg=ACCENT, fg="#ffffff", activebackground="#1d4ed8",
                                      activeforeground="#ffffff")
                self.dirty_note.config(text=f"● {len(self._dirty_fields)} 处改动未应用")
            else:
                self.apply_btn.config(bg="#eef2ff", fg=ACCENT, activebackground="#dbeafe",
                                      activeforeground=ACCENT)
                self.dirty_note.config(text="")
        except tk.TclError:
            pass

    def _toast(self, text, color=OKG, secs=6.0):
        self._flash_text, self._flash_color, self._flash_until = text, color, time.time() + secs

    # ---------- 应用曲线设置（确定键） ----------
    def apply_curve_settings(self):
        """校验输入框并提交。这是曲线/保护阈值唯一的生效入口。"""
        problems = self._validate_entries()
        if problems:
            self._toast(f"未应用：{problems[0]}", HOT, 8)
            messagebox.showwarning("曲线设置有误，未应用", "\n".join(f"· {p}" for p in problems))
            return
        self.curve_committed = self._read_curve(self.curve_entries)
        self.curve2_committed = self._read_curve(self.curve2_entries)
        self.protect_committed = int(self.protect_var.get())
        self.critical_committed = int(self.critical_var.get())
        self._dirty_fields.clear()
        self._refresh_apply_state()
        self._ev("curve_apply", curve=self.curve_committed, curve2=self.curve2_committed,
                 protect=self.protect_committed, critical=self.critical_committed,
                 mode=self.mode_var.get())
        self.apply_mode()
        if self.mode_var.get() == "curve":
            self._toast("曲线设置已应用", OKG)
        else:
            # 不擅自替用户切模式，只说明"已保存、待生效"
            self._toast("曲线设置已保存；切到「温度曲线自动调速」后才生效", "#d97706", 8)

    def reset_curve_defaults(self):
        """把曲线/阈值输入框填回出厂默认值（只改草稿，点「应用曲线」才生效）。"""
        for (te, pe), (t, p) in zip(self.curve_entries, DEFAULT_CURVE):
            te.delete(0, "end")
            te.insert(0, str(t))
            pe.delete(0, "end")
            pe.insert(0, str(p))
        for (te, pe), (t, p) in zip(self.curve2_entries, DEFAULT_CURVE2):
            te.delete(0, "end")
            te.insert(0, str(t))
            pe.delete(0, "end")
            pe.insert(0, str(p))
        self.protect_var.set(str(DEFAULT_PROTECT))     # 会触发 trace 标脏
        self.critical_var.set(str(DEFAULT_CRITICAL))
        for i in range(len(DEFAULT_CURVE)):
            self._dirty_fields.add(f"主曲线温度{i + 1}")   # 逐格标脏，提示数字才对得上
        for i in range(len(DEFAULT_CURVE2)):
            self._dirty_fields.add(f"副曲线温度{i + 1}")
        self._refresh_apply_state()
        self._ev("curve_reset_defaults")
        self._toast("已填入默认值，点「应用曲线」才生效", "#d97706", 8)

    def _validate_entries(self):
        """把能想到的错都指出来：非整数、越界、温度不递增、保护阈值倒挂。"""
        problems = []
        for label, entries in (("主扇", self.curve_entries), ("副扇", self.curve2_entries)):
            temps, pcts, bad = [], [], False
            for i, (te, pe) in enumerate(entries):
                try:
                    t, p = int(te.get()), int(pe.get())
                except ValueError:
                    problems.append(f"{label}第{i + 1}组：温度和占空比都要填整数")
                    bad = True
                    continue
                if not 0 <= t <= 110:
                    problems.append(f"{label}第{i + 1}组温度 {t} 超出 0~110 ℃")
                    bad = True
                if not MANUAL_FLOOR_PERCENT <= p <= 100:
                    problems.append(f"{label}第{i + 1}组占空比 {p} 超出 "
                                    f"{MANUAL_FLOOR_PERCENT}~100%（低于 {MANUAL_FLOOR_PERCENT}% "
                                    f"扇子转不起来，还会被判堵转）")
                    bad = True
                temps.append(t)
                pcts.append(p)
            if not bad and any(temps[i] >= temps[i + 1] for i in range(len(temps) - 1)):
                problems.append(f"{label}曲线温度必须严格递增，现在是 {temps}")
            if not bad and any(pcts[i] > pcts[i + 1] for i in range(len(pcts) - 1)):
                problems.append(f"{label}曲线占空比不能随温度下降，现在是 {pcts}")
        try:
            prot, crit = int(self.protect_var.get()), int(self.critical_var.get())
        except ValueError:
            problems.append("保护温度 / 临界温度要填整数")
        else:
            if not 30 <= prot <= 105:
                problems.append(f"保护温度 {prot} 超出 30~105 ℃")
            if not prot < crit <= 115:
                problems.append(f"临界温度 {crit} 必须大于保护温度 {prot}，且不超过 115 ℃")
        return problems

    @staticmethod
    def _read_curve(entries):
        """按界面顺序读成 [(temp, pct), ...]（调用前已经过校验）。"""
        return [(int(te.get()), int(pe.get())) for te, pe in entries]

    # ---------- 底部 ----------
    def _build_footer(self):
        f = self._card(self.root, 720, 46, 20, 744)
        self.note = tk.Label(f, text="就绪", bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 9),
                             anchor="w")
        # 限宽，否则长消息会把右边的诊断复选框顶掉
        self.note.place(x=16, y=14, width=302, height=20)
        # 底部这条是唯一的"自由文本"出口：控制器的 note 可以是几十个字
        # （"副扇持续 0 RPM… | 已修复静态寄存器…"），tk.Label 超宽是**直接切字**
        # 不换行，所以这里统一走 _set_note：放不下就加省略号，全文挂 tooltip。
        self._note_full = ""
        self._note_shown = ""
        self._tip = _Tooltip(self.note, self._note_tip_text)
        # 诊断日志：默认关，勾上才写。日志存到 fanctl\diag\，一次会话一个文件。
        self.diag_var = tk.BooleanVar(value=False)
        tk.Checkbutton(f, text="诊断日志", variable=self.diag_var,
                       bg=CARD, fg=INK, selectcolor="#ffffff",
                       font=("Microsoft YaHei UI", 8), activebackground=CARD,
                       command=self.toggle_diag).place(x=326, y=12)
        tk.Button(f, text="打开日志目录", bg="#eef2ff", fg=INK, relief="flat",
                  font=("Microsoft YaHei UI", 8), width=12,
                  command=self.open_diag_dir).place(x=420, y=10)
        tk.Button(f, text="开机自启", bg="#eef2ff", fg=INK, relief="flat",
                  font=("Microsoft YaHei UI", 8), width=8,
                  command=self.install_autostart).place(x=524, y=10)
        tk.Button(f, text="恢复到自动", bg="#eef2ff", fg=INK, relief="flat",
                  font=("Microsoft YaHei UI", 8), width=10, command=self.reset_auto).place(x=596, y=10)

    # ---------- 底部状态条 ----------
    @staticmethod
    def _fit_text(text, max_px, measure):
        """按像素宽度截断，放不下就在尾部加「…」。

        不能用字符数估宽度：中英文混排（"副扇持续 0 RPM（疑似被 EC 判堵转）"）
        每个字宽度差一倍。tk.Label 超宽是**直接切字而不换行**，
        所以与其让它切得莫名其妙，不如主动截断并留全文在 tooltip 里。
        """
        if not text or measure(text) <= max_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:                      # 找能容纳 text[:lo] + "…" 的最大 lo
            mid = (lo + hi + 1) // 2
            if measure(text[:mid] + "…") <= max_px:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + "…") if lo else "…"

    def _note_tip_text(self):
        """只有文案真被截断了才弹提示，没截时弹出来是噪音。"""
        return self._note_full if self._note_shown != self._note_full else ""

    def _set_note(self, text, color=SUB):
        """底部状态条的唯一写入口——所有地方都走这里，版面就塌不了。"""
        self._note_full = text or ""
        avail = int(self.note.place_info()["width"])
        font = tkfont.Font(font=self.note.cget("font"))
        self._note_shown = self._fit_text(self._note_full, avail, font.measure)
        self.note.config(text=self._note_shown, fg=color)

    def _ev(self, action, **detail):
        """记一条用户操作。日志未开启时是空操作。

        第一个参数必须叫 action 而不是 name：调用方会在 detail 里传 name=
        （比如预设按钮的名字），重名会直接 TypeError。
        """
        try:
            self.controller.diag.event(action, **detail)
        except Exception:
            pass

    def toggle_diag(self):
        if self.diag_var.get():
            path = self.controller.diag.enable(reason="gui")
            if path:
                self._ev("gui_diag_on")
                self._set_note(f"诊断日志已开启：{os.path.basename(path)}", OKG)
            else:
                self.diag_var.set(False)
                self._set_note("诊断日志开启失败（无法创建 diag 目录）", HOT)
        else:
            self._ev("gui_diag_off")
            self.controller.diag.disable(reason="gui")
            self._set_note("诊断日志已关闭", SUB)

    def open_diag_dir(self):
        try:
            os.makedirs(DIAG_DIR, exist_ok=True)
            os.startfile(DIAG_DIR)
        except Exception as exc:
            messagebox.showerror("打不开日志目录", str(exc))

    # ---------- 交互 ----------
    def apply_curve(self):
        """勾选曲线必须真的切到 curve 模式，否则停在 auto 上一个字节都不会写出去。"""
        if self.curve_var.get():
            self.mode_var.set("curve")
        elif self.mode_var.get() == "curve":
            self.mode_var.set("manual")
        self._ev("curve_toggle", on=self.curve_var.get(), mode=self.mode_var.get())
        self.apply_mode()

    def _layout_mode_row(self):
        """把三个模式单选框按各自文字的实际宽度均匀铺开。

        写死 x 坐标的版本会随字体/DPI 变化而"挨边"甚至重叠（中文 9pt 在 200%
        缩放下每条宽度差 60px 以上），所以这里按 reqwidth 算：
        总宽已知 → 剩余空间等分 → 每条依次右移。
        """
        try:
            self.root.update_idletasks()
            widths = [r.winfo_reqwidth() for r in self.mode_radios]
        except tk.TclError:
            return
        # 度量还没就绪时（宽度明显偏小）先按经验值铺，等 after() 回调再校一次
        if min(widths) < 40:
            widths = [180, 120, 132][:len(widths)]
        left, right, y = 20, 708, 38
        n = len(widths)
        gap = (right - left - sum(widths)) / max(1, n - 1)
        gap = max(32.0, min(130.0, gap))
        x = float(left)
        for r, w in zip(self.mode_radios, widths):
            r.place(x=int(x), y=y)
            x += w + gap

    def _sync_mode_widgets(self):
        """曲线模式下滑块和预设不起作用，直接置灰，免得以为设了没反应。

        副扇滑块另有一层条件：只有勾选"副扇独立"才可用（曲线模式下若勾选了独立，
        主扇走曲线、副扇仍按滑块走）。副扇曲线的输入框只在"曲线模式 + 勾选独立曲线"
        双重条件满足时可用。
        """
        curve_mode = self.mode_var.get() == "curve"
        state = "disabled" if curve_mode else "normal"
        state2 = "normal" if self.fan2_indep_var.get() else "disabled"
        # 副扇曲线只在 curve 模式有意义；复选框本身保持可点，否则没法再勾回来
        state_c2 = "normal" if curve_mode else "disabled"
        state_c2v = "normal" if (curve_mode and self.curve2_var.get()) else "disabled"
        try:
            self.slider.config(state=state)
            self.slider2.config(state=state2)
            for b in self.preset_buttons:
                b.config(state=state)
            self.curve2_chk.config(state=state_c2)
            for te, pe in self.curve2_entries:
                te.config(state=state_c2v)
                pe.config(state=state_c2v)
        except tk.TclError:
            pass

    def apply_fan2(self):
        """勾选"副扇独立"：副扇按自己的滑块走；取消则跟随主扇（或曲线算出的值）。

        固定值与独立曲线互斥——两者同时勾会出现"设了没反应"（固定值优先级更高）。
        """
        if self.fan2_indep_var.get():
            self.curve2_var.set(False)
        indep = self.fan2_indep_var.get()
        self.pct2_label.config(text=f"{self.pct2_var.get()}%" if indep else "跟随",
                               fg="#7c3aed" if indep else SUB)
        self._sync_mode_widgets()
        self._ev("fan2_indep", on=indep, pct2=(self.pct2_var.get() if indep else None))
        self.apply_mode()

    def apply_curve2(self):
        """勾选副扇独立曲线：它只在 curve 模式下有意义，顺带把模式切过去。

        与"副扇独立（固定值）"互斥，勾这个就取消那个。
        """
        if self.curve2_var.get():
            self.fan2_indep_var.set(False)
            self.mode_var.set("curve")
        self._ev("curve2_toggle", on=self.curve2_var.get(),
                 curve2=self.curve2_committed if self.curve2_var.get() else None)
        self.apply_mode()

    def apply_backend(self):
        """切换控制后端。切到 duty 时要把锁存位交还，避免 EC 一直守着旧表。"""
        b = self.backend_var.get()
        self._ev("backend", value=b)
        try:
            self.controller.configure(backend=b)
        except Exception:
            pass

    def apply_mode(self):
        """把「已提交」的配置下发。绝不读输入框——草稿只有点应用才会进 committed。"""
        self.curve_var.set(self.mode_var.get() == "curve")
        self._sync_mode_widgets()
        curve = list(self.curve_committed)
        curve2 = list(self.curve2_committed) if self.curve2_var.get() else None
        try:
            self.controller.configure(mode=self.mode_var.get(),
                                      manual_percent=self.pct_var.get(),
                                      manual_percent2=(self.pct2_var.get()
                                                       if self.fan2_indep_var.get() else None),
                                      use_curve=self.curve_var.get(),
                                      curve=curve or None,
                                      curve2=curve2,
                                      protect_temp=self.protect_committed,
                                      critical_temp=self.critical_committed)
        except Exception as exc:
            self._toast(f"下发失败：{exc}", HOT, 8)

    def on_slider(self, value):
        self.pct_label.config(text=f"{int(float(value))}%")
        if self.mode_var.get() == "manual":
            self._ev("slider_fan1", percent=int(float(value)))
            self.controller.configure(mode="manual", manual_percent=int(float(value)))

    def on_slider2(self, value):
        if not self.fan2_indep_var.get():
            return
        self.pct2_label.config(text=f"{int(float(value))}%")
        self._ev("slider_fan2", percent=int(float(value)))
        self.controller.configure(manual_percent2=int(float(value)))

    def apply_preset(self, pct):
        name = next((n for n, p in PRESETS if p == pct), str(pct))
        self.pct_var.set(pct)
        self.pct_label.config(text=f"{pct}%")
        self.mode_var.set("manual")
        self._ev("preset", name=name, percent=pct)
        self.apply_mode()

    def reset_auto(self):
        self.mode_var.set("auto")
        self._ev("reset_auto")
        self.apply_mode()

    def install_autostart(self):
        self._ev("install_autostart")
        task = "JG_FanCtl"
        if getattr(sys, "frozen", False):
            # 单文件 EXE：直接登记 EXE 自身，不能再拼脚本路径
            # （打包后 __file__ 是临时目录，重启后路径已失效）。
            prog = f'"{sys.executable}"'
        else:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            prog = f'"{exe}" "{os.path.abspath(__file__)}"'
        cmd = ['schtasks', '/create', '/tn', task, '/tr', prog,
               '/sc', 'onlogon', '/rl', 'HIGHEST', '/f']
        try:
            out = subprocess.run(" ".join(cmd), shell=True, capture_output=True, text=True)
            if out.returncode == 0:
                messagebox.showinfo("完成", f"已注册开机自启任务 {task}。\n"
                                            f"如需卸载，在「任务计划程序」里删除该任务即可。")
            else:
                messagebox.showerror("失败", out.stderr or out.stdout)
        except Exception as exc:
            messagebox.showerror("失败", str(exc))

    def _smooth(self, key, value):
        """转速读数指数平滑（alpha=0.3）。撕裂读（>60000）直接丢弃。"""
        if value >= 60000:
            return self._ema.get(key, value)
        prev = self._ema.get(key)
        v = value if prev is None else prev + 0.3 * (value - prev)
        self._ema[key] = v
        return int(round(v))

    @staticmethod
    def _mode_label_text(st):
        """右上角状态文字 → (文案, 颜色)。纯函数，不碰控件。

        抽成纯函数是**为了能被穷举测试**：文案由 target/target2 拼出来，
        组合数很大（t1 整个合法区间 × t2 有无/是否相等 × 两种模式 × 有无低温交还）。
        之前手写 4 条样例去测，既漏组合、还把「/ 副 80%」打成了「/ 副副 80%」，
        长文案照样溢出到卡片外——这种错误只能靠穷举发现。
        """
        if st.get("target_percent") is None:
            # 曲线模式下温度低于 45℃，已交还 EC——别让用户以为是"自动"了
            return ("EC 原生 · 低温", OKG) if st.get("handoff_lowtemp") else ("自动", OKG)
        t1 = st["target_percent"]
        t2 = st.get("target_percent2")
        suffix = "" if (t2 is None or t2 == t1) else f" / 副 {t2}%"
        if st.get("mode") == "curve":
            return f"曲线 {t1}%{suffix}", "#7c3aed"
        return f"手动 {t1}%{suffix}", ACCENT

    def tick(self):
        st = self.controller.get_state()
        if time.time() < self._flash_until:
            self._set_note(self._flash_text, self._flash_color)
        elif st.get("error"):
            self._set_note(f"错误：{st['error']}", HOT)
        else:
            self._set_note(st.get("note", ""), SUB)
        temp = st.get("cpu_temp", 0)
        self.stats["cpu_temp"].config(text=f"{temp}",
                                      fg=HOT if temp >= self.protect_committed else INK)
        # 转速读数平滑：EC 约 0.5~1 秒才更新一次转速寄存器，且我们接管后副扇会有
        # ±3% 的固有游走。直接显示会让数字每秒跳几百转，看着像"抖动很大"，
        # 实际转速并没有那么剧烈地变化。这里做指数平滑，温度不做（温度要即时）。
        self.stats["fan1_rpm"].config(text=f"{self._smooth('fan1', st['fan1_rpm'])}")
        self.stats["fan2_rpm"].config(text=f"{self._smooth('fan2', st['fan2_rpm'])}")
        self.stats["duty_percent"].config(text=f"{st['duty_percent']}", fg=ACCENT)
        # 副扇占空比：EC 的 0x1809 是独立记录，勾「副扇独立」后会与主扇不同
        d2 = st.get("duty_percent2")
        self.stats["duty_percent2"].config(text="--" if d2 is None else f"{d2}", fg="#7c3aed")
        text, color = self._mode_label_text(st)
        self.mode_text.config(text=text, fg=color)
        self.dot.config(fg=color)
        self._draw_chart(self.controller.get_history())
        self.root.after(600, self.tick)

    def on_close(self):
        if self._dirty_fields:
            keep = messagebox.askokcancel(
                "曲线设置还没应用",
                f"有 {len(self._dirty_fields)} 处曲线/阈值改动还没点「应用曲线设置」，\n"
                f"现在退出这些改动会丢弃。\n\n仍要退出吗？")
            if not keep:
                self._toast("已取消退出；改完请点「应用曲线设置」", "#d97706", 8)
                return
        self._ev("close", dirty_fields=sorted(self._dirty_fields))
        try:
            self.controller.configure(mode="auto")
            time.sleep(1.2)
            self.controller.stop()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
