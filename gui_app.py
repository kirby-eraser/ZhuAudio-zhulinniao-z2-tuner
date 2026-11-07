"""
gui_app.py — 竹林鸟 霜华 Z2 PC 调音台（官方 App 视觉风格版）
============================================================
6 段参数 EQ + 官方在线音效库（25 条，无需登录）+ 社区 EQ（需登录）+ 导入导出。
底层通信走 protocol.Z2Device，云端走 cloud_eq，二者互不阻塞。

运行：python gui_app.py   （稳定版仍为 gui.py，两者逻辑完全一致）
打包：PyInstaller --add-data "assets;assets" --icon assets/app.ico

⚠️ 第一次点「写入 / 应用保存」前，请先点「备份寄存器」留底。
"""
from __future__ import annotations

import json
import math
import os
import queue
import sys
import time
import unicodedata
import threading

def _enable_dpi_awareness():
    """必须在创建 Tk root 之前调用，否则 Windows 会拉伸窗口位图导致文字模糊。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()

import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, scrolledtext, filedialog, simpledialog

import cloud_eq
from cloud_eq import Preset
from protocol import (Z2Device, Band, DEFAULT_BANDS, TYPE_PK, TYPE_LSQ,
                      TYPE_HSQ, TYPE_NAMES, BANDS_PER_GROUP,
                      FREQ_MIN, FREQ_MAX, GAIN_MIN, GAIN_MAX, Q_MIN, Q_MAX)

BACKUP_FILE = "regs_backup.json"
RESTORE_REGS = list(range(0x34, 0x4E)) + [0x56, 0x66]

# ---- 配色（竹林鸟绿） ----
BG = "#F6F6F4"          # 官方 App 的暖白背景
CARD = "#FFFFFF"         # 纯白圆角卡片
GREEN = "#789262"        # 官方灰绿主色
GREEN_DARK = "#5C7548"
GREEN_LIGHT = "#EDF3E6"
BORDER = "#E8EBE4"
FG = "#1D211A"           # 近黑文字
FG_SUB = "#8C9286"
WARN = "#B4472E"
FONT = ("Microsoft YaHei", 10)
FONT_SM = ("Microsoft YaHei", 9)
FONT_BOLD = ("Microsoft YaHei", 10, "bold")
FONT_TITLE = ("Microsoft YaHei", 19, "bold")
FONT_MONO = ("Consolas", 9)


def _text_width(s: str) -> int:
    return sum(13 if unicodedata.east_asian_width(c) in ("W", "F") else 8
               for c in s)


def _round_polygon(canvas, x1, y1, x2, y2, r, **kw):
    """精确圆角矩形：四角各取 7 个圆弧点，避免 B 样条把直边也拉弯。"""
    pts = []
    for cx, cy, start in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0),
                          (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for i in range(7):
            a = math.radians(start + i * 15)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return canvas.create_polygon(pts, **kw)


def _draw_icon(c, kind, x, y, size, color):
    """官方 App 那种「灰绿细线描圆形图标」，全部用 Canvas 现画。"""
    import math
    cx, cy = x + size / 2, y + size / 2
    r = size / 2 - 1.5
    c.create_oval(cx - r, cy - r, cx + r, cy + r,
                  outline=color, width=1.3)
    q = r * 0.58
    if kind == "wave":
        pts = [cx - q, cy, cx - q * 0.5, cy - q * 0.6, cx, cy + q * 0.5,
               cx + q * 0.5, cy - q * 0.55, cx + q, cy]
        c.create_line(pts, fill=color, width=1.3, smooth=True)
    elif kind == "star":
        pts = []
        for i in range(10):
            a = math.radians(-90 + i * 36)
            rr = q * 0.92 if i % 2 == 0 else q * 0.42
            pts.extend((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        c.create_line(pts + pts[:2], fill=color, width=1.2)
    elif kind == "planet":
        c.create_oval(cx - q * 0.7, cy - q * 0.7,
                      cx + q * 0.7, cy + q * 0.7, outline=color, width=1.2)
        c.create_arc(cx - q, cy - q * 0.35, cx + q, cy + q * 0.35,
                     start=195, extent=150, style="arc",
                     outline=color, width=1.2)
    elif kind == "eq":
        for dx, dy in (-q * 0.55, -q * 0.3), (0, q * 0.35), (q * 0.55, -q * 0.55):
            c.create_line(cx + dx, cy - q * 0.7, cx + dx, cy + q * 0.7,
                          fill=color, width=1.2)
            c.create_oval(cx + dx - 2, cy + dy - 2, cx + dx + 2, cy + dy + 2,
                          fill=color, outline="")
    elif kind == "check":
        c.create_line(cx - q * 0.6, cy, cx - q * 0.1, cy + q * 0.5,
                      cx + q * 0.7, cy - q * 0.55, fill=color, width=1.5)
    elif kind == "refresh":
        c.create_arc(cx - q, cy - q, cx + q, cy + q, start=35, extent=285,
                     style="arc", outline=color, width=1.3)
        c.create_line(cx + q * 0.55, cy - q * 0.85,
                      cx + q * 0.95, cy - q * 0.45,
                      cx + q * 0.45, cy - q * 0.2, fill=color, width=1.2)
    elif kind == "link":
        c.create_line(cx - q * 0.7, cy, cx + q * 0.7, cy,
                      fill=color, width=1.3)
        c.create_oval(cx - q * 0.9, cy - 2.5, cx - q * 0.5, cy + 2.5,
                      outline=color, width=1.2)
        c.create_oval(cx + q * 0.5, cy - 2.5, cx + q * 0.9, cy + 2.5,
                      outline=color, width=1.2)
    elif kind == "save":
        c.create_rectangle(cx - q * 0.7, cy - q * 0.7,
                           cx + q * 0.7, cy + q * 0.7,
                           outline=color, width=1.2)
        c.create_line(cx - q * 0.25, cy - q * 0.7,
                      cx - q * 0.25, cy - q * 0.2, fill=color, width=1.1)
        c.create_line(cx - q * 0.25, cy - q * 0.2,
                      cx + q * 0.25, cy - q * 0.2, fill=color, width=1.1)
    elif kind == "list":
        for i, dy in enumerate((-q * 0.45, 0, q * 0.45)):
            c.create_oval(cx - q * 0.55, cy + dy - 1,
                          cx - q * 0.55 + 2, cy + dy + 1,
                          fill=color, outline="")
            c.create_line(cx - q * 0.25, cy + dy, cx + q * 0.65, cy + dy,
                          fill=color, width=1.1)
    elif kind == "chevron":
        c.create_line(cx - 2, cy - 4, cx + 2, cy, cx - 2, cy + 4,
                      fill=color, width=1.4)
    elif kind == "back":
        c.create_line(cx + 3, cy - 5, cx - 3, cy, cx + 3, cy + 5,
                      fill=color, width=1.5)
    elif kind == "home":
        c.create_line(cx - q * 0.7, cy, cx, cy - q * 0.65,
                      cx + q * 0.7, cy, fill=color, width=1.2)
        c.create_line(cx - q * 0.5, cy - q * 0.1, cx - q * 0.5, cy + q * 0.6,
                      cx + q * 0.5, cy + q * 0.6, cx + q * 0.5, cy - q * 0.1,
                      fill=color, width=1.2)
    elif kind == "user":
        c.create_oval(cx - q * 0.3, cy - q * 0.6,
                      cx + q * 0.3, cy, outline=color, width=1.2)
        c.create_arc(cx - q * 0.65, cy - q * 0.1, cx + q * 0.65, cy + q * 0.95,
                     start=0, extent=180, style="arc", outline=color, width=1.2)
    elif kind == "eye":
        c.create_arc(cx - q, cy - q * 0.55, cx + q, cy + q * 0.55,
                     start=25, extent=130, style="arc", outline=color, width=1.2)
        c.create_arc(cx - q, cy - q * 0.55, cx + q, cy + q * 0.55,
                     start=205, extent=130, style="arc", outline=color, width=1.2)
        c.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=color, outline="")


class AppCard(tk.Frame):
    """官方 App 风格：纯白大圆角卡片，可选标题行 + 线描图标。

    用法：wrap = AppCard(parent, title="当前 EQ", icon="wave")
          wrap.pack(...);  body = wrap.body   # 普通 Frame，可 grid / pack
    """
    def __init__(self, master, title="", icon=None, radius=20, pad=16, **kw):
        super().__init__(master, bg=BG, **kw)
        self.radius = radius
        self.cvs = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.cvs.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.cvs, bg=CARD)
        self._win = self.cvs.create_window(0, 0, anchor="nw",
                                           window=self.inner)
        self.cvs.bind("<Configure>", self._resize)
        if title:
            head = tk.Frame(self.inner, bg=CARD)
            head.pack(fill="x", padx=pad, pady=(pad - 3, 4))
            if icon:
                ic = tk.Canvas(head, width=26, height=26, bg=CARD,
                               highlightthickness=0)
                _draw_icon(ic, icon, 1, 1, 24, GREEN)
                ic.pack(side="left", padx=(0, 8))
            tk.Label(head, text=title, bg=CARD, fg=FG,
                     font=FONT_BOLD).pack(side="left")
            self.head = head
        self.body = tk.Frame(self.inner, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=pad, pady=(2, pad))

    def _resize(self, e):
        self.cvs.delete("cardbg")
        _round_polygon(self.cvs, 1, 1, e.width - 2, e.height - 2,
                       self.radius, fill=CARD, outline=BORDER,
                       width=1, tags="cardbg")
        self.cvs.tag_lower("cardbg")
        self.cvs.coords(self._win, 0, 0)
        self.cvs.itemconfig(self._win, width=e.width, height=e.height)


def _mix(c1, c2, t):
    """两个 #RRGGBB 按比例混合，用于曲线渐变。"""
    a = tuple(int(c1[i:i+2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i+2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(int(a[i] + (b[i]-a[i])*t)
                                   for i in range(3))


class PillEntry(tk.Canvas):
    """官方「自定义EQ」的白色胶囊输入框，内部是真正可编辑的 Entry。

    对外暴露 get/delete/insert，因此业务层可以像用 ttk.Entry 一样用它。
    """
    def __init__(self, master, value="", width=72, height=30, font=None, **kw):
        super().__init__(master, width=width, height=height, bg=CARD,
                         highlightthickness=0, bd=0, **kw)
        self.var = tk.StringVar(value=str(value))
        self.entry = tk.Entry(self, textvariable=self.var, bd=0,
                              relief="flat", bg="#FFFFFF", fg=FG,
                              justify="center", font=font or FONT_MONO,
                              highlightthickness=0,
                              insertbackground=GREEN_DARK)
        self._win = self.create_window(width//2, height//2,
                                       window=self.entry,
                                       width=width-14, height=height-8)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _draw(self):
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        tk.Canvas.delete(self, "pill")   # 不能走被代理的 self.delete
        _round_polygon(self, 1, 1, w-2, h-2, h//2, fill="#FFFFFF",
                       outline=BORDER, width=1, tags="pill")
        self.tag_lower("pill")
        self.coords(self._win, w//2, h//2)
        self.itemconfig(self._win, width=max(w-14, 10),
                        height=max(h-8, 10))

    def get(self):
        return self.var.get()

    def delete(self, *a):
        return self.entry.delete(*a)

    def insert(self, *a):
        return self.entry.insert(*a)


class PillVar:
    """增益胶囊对业务层伪装成 DoubleVar：get() 返回 float，set() 格式化写入。"""
    def __init__(self, pill):
        self.pill = pill

    def get(self):
        try:
            return float(self.pill.get())
        except (ValueError, TypeError):
            return 0.0

    def set(self, v):
        self.pill.delete(0, "end")
        self.pill.insert(0, f"{float(v):+.1f}")


class EqCurve(tk.Canvas):
    """官方那种绿色渐变 EQ 响应曲线（对数频率 20–20K，纵轴 ±12 dB）。

    app 不为 None 时每 450ms 自动从界面读取参数重绘，无需改动任何业务方法。
    """
    def __init__(self, master, app=None, width=500, height=150, **kw):
        super().__init__(master, width=width, height=height, bg=BG,
                         highlightthickness=0, bd=0, **kw)
        self.app = app
        self.bands = []
        self.bind("<Configure>", lambda _e: self.redraw())
        if app is not None:
            self.after(450, self.tick)

    def set_bands(self, bands):
        self.bands = list(bands)
        self.redraw()

    def tick(self):
        if self.app is not None:
            try:
                self.bands = [(x.freq, x.gain, x.q)
                              for x in self.app._bands_from_ui()]
            except Exception:
                pass
            try:
                self.redraw()
            except Exception:
                pass
        try:
            self.after(450, self.tick)
        except Exception:
            pass

    def redraw(self):
        import math
        w = max(self.winfo_width(), int(self.cget("width")))
        h = max(self.winfo_height(), int(self.cget("height")))
        self.delete("all")
        L, R, T, B = 38, w - 10, 8, h - 20
        if R <= L or B <= T:
            return
        for val in (12, 6, 0, -6, -12):
            y = T + (12 - val) / 24.0 * (B - T)
            self.create_text(L - 7, y,
                             text=(f"{val:+d}" if val else "0"),
                             anchor="e", fill=FG_SUB, font=FONT_SM)
            self.create_line(L, y, R, y, fill=BORDER, width=1)
        y0 = T + 0.5 * (B - T)
        for yy in range(int(y0), B, 2):
            t = (yy - y0) / max(1.0, B - y0)
            self.create_line(L, yy, R, yy, fill=_mix(GREEN_LIGHT, BG, t))
        for f, lab in ((20, "20"), (100, "100"), (1000, "1K"),
                       (10000, "10K"), (20000, "20K")):
            x = L + (math.log10(f) - math.log10(20)) / 3.0 * (R - L)
            self.create_text(x, B + 4, text=lab, anchor="n",
                             fill=FG_SUB, font=FONT_SM)

        def gain_at(f):
            total = 0.0
            for item in self.bands:
                try:
                    fi, gi, qi = (float(item[0]), float(item[1]),
                                  float(item[2]))
                except (ValueError, TypeError, IndexError):
                    continue
                if fi <= 0 or qi <= 0:
                    continue
                sigma = 0.42 / max(0.2, min(5.0, qi))
                d = math.log10(max(1.0, f)) - math.log10(fi)
                total += gi * math.exp(-(d*d) / (2*sigma*sigma))
            return max(-12.0, min(12.0, total))

        lo = math.log10(20)
        pts = []
        for px in range(L, R + 1, 2):
            f = 10 ** (lo + (px - L) / (R - L) * 3.0)
            g = gain_at(f)
            pts.append((px, T + (12 - g) / 24.0 * (B - T)))
        if pts:
            self.create_polygon([(L, y0)] + pts + [(R, y0)],
                                fill=GREEN, outline="", stipple="gray25")
            self.create_line(pts, fill=GREEN_DARK, width=2, smooth=True)


class RoundButton(tk.Canvas):
    """圆角按钮（tkinter 无原生圆角，用 Canvas 自绘，支持 hover / 按下 / 禁用）。"""

    def __init__(self, master, text, command=None, primary=False,
                 width=None, height=34, radius=15, bg=BG, font=None,
                 icon=None):
        self._icon = icon
        if width is None:
            width = max(66, _text_width(text) + 30)   # 不再为图标预留宽度
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._cmd = command
        self._icon = icon
        self._primary = primary
        self._radius = radius
        self._font = font or FONT_SM
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._draw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _colors(self):
        if not self._enabled:
            return "#E3E9E0", "#CBD8C7", "#9AA89C"
        if self._primary:
            base = (GREEN_DARK if self._pressed else
                    ("#3F6A3E" if self._hover else GREEN))
            return base, base, "#FFFFFF"
        return (GREEN_LIGHT if self._hover else CARD), BORDER, GREEN_DARK

    def _draw(self):
        self.delete("all")
        w, h = int(self.cget("width")), int(self.cget("height"))
        fill, outline, fg = self._colors()
        _round_polygon(self, 2, 2, w - 2, h - 2, self._radius,
                       fill=fill, outline=outline, width=1)
        # 自绘的 18px 线描图标在高 DPI 下容易发糊，全部去掉，只留文字
        self.create_text(w / 2, h / 2, text=self._text, fill=fg,
                         font=self._font)

    def set_primary(self, on):
        """供「自定义 1 / 自定义 2」切换选中态。"""
        self._primary = bool(on)
        self._draw()

    def _set_hover(self, on):
        if self._hover != on:
            self._hover = on
            self._draw()

    def _on_press(self, _e):
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, _e):
        if not self._enabled:
            return
        self._pressed = False
        self._draw()
        if self._cmd:
            self._cmd()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self._draw()


def resource_path(rel: str) -> str:
    """兼容 PyInstaller 的 _MEIPASS 与源码运行。"""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).parent
    return str(root / "assets" / rel)


class Z2Tuner(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("竹林鸟 霜华 Z2 · 调音台")
        self.geometry("1100x900")
        self.minsize(1020, 840)
        self.configure(bg=BG)
        try:
            self.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass

        self.device = Z2Device()
        self.rows: list[dict] = []
        # 只写「自定义一」(组 A 0x35)；组 B(0x42) 是「自定义二」，不覆盖。
        # 两个勾选项（官方兼容 / 两组都写）均已移除：前者只写 5 段且漏写
        # Q/type 导致 COMMIT 失败；后者会把自定义二一起覆盖，用户不需要。
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.cloud_status = tk.StringVar(value="官方音效库：本地快照")
        self.token: str = ""
        self.presets: list[Preset] = []
        # 注意：__init__ 里 _build_ui() 先于 _init_presets() 执行，预设页按钮在
        # 构建时就要读 self.builtin。这里提前加载（纯文件读取、不依赖任何 UI），
        # 否则按钮会整排消失——只剩硬编码的「恢复出厂默认」。
        self.builtin: list[Preset] = cloud_eq.load_offline(
            resource_path("builtin_presets.json"), "builtin")
        self.community: list[Preset] = []
        self.current: Preset | None = None

        self.photos = []          # 防止 PhotoImage 被 GC
        self._jobs: queue.Queue = queue.Queue()
        self._pending = 0
        self._running_tags = set()
        self._error_until = 0.0     # 错误提示保护期，避免被后台成功消息盖掉

        self._setup_style()
        self._build_ui()
        self._log("协议已由官方 APK 逐字节核对，云端签名算法已实机验证。")
        self._log(f"参数范围：频率 {FREQ_MIN}-{FREQ_MAX} Hz，增益 "
                  f"{GAIN_MIN:+.0f}~{GAIN_MAX:+.0f} dB，Q {Q_MIN}~{Q_MAX}。")
        self._log("建议：连接 → 备份 → 读取 EQ → 选预设 / 手动调 → 应用并保存。",
                  friendly="先连接并备份，再挑一条喜欢的音效")
        self._init_presets()

    # ------------------------------------------------------------------ 样式
    def _setup_style(self):
        # 高 DPI 屏按真实 DPI 调整 tk 缩放，配合模块级 DPI 感知让文字锐利
        try:
            dpi = self.winfo_fpixels("1i")
            if dpi > 96:
                self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, font=FONT)
        s.configure("TFrame", background=BG)
        s.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER,
                    relief="flat", borderwidth=0, padding=10)
        s.configure("Card.TLabelframe.Label", background=CARD,
                    foreground=GREEN_DARK, font=FONT_BOLD)
        s.configure("Tool.TButton", padding=(9, 5), background=CARD,
                    foreground=FG, bordercolor=BORDER, relief="flat",
                    borderwidth=1)
        s.map("Tool.TButton", background=[("active", GREEN_LIGHT)],
              bordercolor=[("active", GREEN)])
        s.configure("Accent.TButton", padding=(11, 6), background=GREEN,
                    foreground="#FFFFFF", bordercolor=GREEN, relief="flat")
        s.map("Accent.TButton", background=[("active", GREEN_DARK),
                                            ("disabled", "#B9C8AE")])
        s.configure("TNotebook", background=CARD, bordercolor=BORDER,
                    tabmargins=(6, 6, 6, 0))
        s.configure("TNotebook.Tab", padding=(20, 8), background=CARD,
                    foreground=FG_SUB, font=FONT_BOLD, borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", GREEN_LIGHT)],
              foreground=[("selected", GREEN_DARK)])
        s.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, rowheight=30, bordercolor=BORDER,
                    font=FONT_SM)
        s.configure("Treeview.Heading", background=CARD,
                    foreground=FG_SUB, font=FONT_SM, relief="flat")
        s.map("Treeview", background=[("selected", GREEN_LIGHT)],
              foreground=[("selected", GREEN_DARK)])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.configure("TEntry", fieldbackground="#F7F8F5",
                    bordercolor=BORDER, relief="flat")
        s.configure("TScale", background=CARD, troughcolor=GREEN_LIGHT)

    def _build_ui(self):
        self.configure(bg=BG)
        self._build_banner()

        main = tk.Frame(self, bg=BG, padx=14, pady=4)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg=BG, width=575)
        left.pack(side="left", fill="both", expand=True, padx=(0, 7))
        left.pack_propagate(False)
        right = tk.Frame(main, bg=BG, width=493)
        right.pack(side="right", fill="both", expand=True, padx=(7, 0))
        right.pack_propagate(False)
        self._build_left(left)
        self._build_right(right)

        self._build_toolbar()
        self._build_notice()
        self._build_bottom_nav()

    def _build_banner(self):
        bar = tk.Frame(self, bg=BG, height=166)
        bar.pack(fill="x", padx=14, pady=(10, 2))
        bar.pack_propagate(False)

        tk.Label(bar, text="‹", bg=BG, fg=GREEN,
                 font=("Microsoft YaHei", 24)).pack(
            side="left", anchor="n", padx=4)

        center = tk.Frame(bar, bg=BG)
        center.place(relx=0.5, rely=0, anchor="n")
        tk.Label(center, text="Z2", bg=BG, fg=FG,
                 font=FONT_TITLE).pack(pady=(0, 8))
        try:
            img = tk.PhotoImage(file=resource_path("earphone_banner.png"))
            self.photos.append(img)
            tk.Label(center, image=img, bg=BG).pack()
        except Exception:
            pass
        st = tk.Frame(center, bg=BG)
        st.pack(pady=(8, 0))
        self.state_canvas = tk.Canvas(st, width=12, height=12, bg=BG,
                                      highlightthickness=0)
        self.state_canvas.pack(side="left", padx=(0, 7))
        self.state_dot = self.state_canvas.create_oval(
            2, 2, 10, 10, fill="#B9BDB3", outline="")
        self.state_lbl = tk.Label(st, text="未连接", bg=BG, fg=GREEN,
                                  font=FONT_SM)
        self.state_lbl.pack(side="left")

        side = tk.Frame(bar, bg=BG)
        side.pack(side="right", anchor="n", pady=4)
        try:
            logo = tk.PhotoImage(file=resource_path("logo.png"))
            self.photos.append(logo)
            small = logo.subsample(3, 3)
            self.photos.append(small)
            tk.Label(side, image=small, bg=BG).pack(
                anchor="e", pady=(0, 6))
        except Exception:
            pass
        tk.Label(side, text="当前 EQ", bg=BG, fg=FG,
                 font=FONT_BOLD).pack(anchor="e")
        tk.Label(side, text="参数均衡器 · 6 段", bg=BG, fg=FG_SUB,
                 font=FONT_SM).pack(anchor="e", pady=(4, 0))

    def _set_device_state(self, ok: bool | None):
        color = "#3E9B54" if ok else ("#C9A227" if ok is None else "#C0553F")
        text = "已连接" if ok else ("连接中" if ok is None else "未连接")
        try:
            self.state_canvas.itemconfig(self.state_dot, fill=color)
            self.state_lbl.config(text=text)
        except Exception:
            pass

    def _build_left(self, parent):
        wrap = AppCard(parent, title="自定义 EQ", radius=20)
        wrap.pack(fill="both", expand=True)
        card = wrap.body

        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x")

        slots = tk.Frame(top, bg=CARD)
        slots.pack(fill="x", pady=(0, 10))
        self.custom_slot = 0
        self.custom_slots = [None, None]
        self.slot_btns = [
            RoundButton(slots, "自定义 1", lambda: self._on_custom_slot(0),
                        width=205, height=40, radius=16, primary=True),
            RoundButton(slots, "自定义 2", lambda: self._on_custom_slot(1),
                        width=205, height=40, radius=16),
        ]
        for i, btn in enumerate(self.slot_btns):
            btn.pack(side="left", padx=(0 if i == 0 else 10, 0))

        pills = {"freq": [], "q": [], "gain": []}
        for lab, key in (("频点", "freq"), ("Q值", "q"), ("增益", "gain")):
            line = tk.Frame(top, bg=CARD)
            line.pack(fill="x", pady=3)
            tk.Label(line, text=lab, bg=CARD, fg=FG_SUB, font=FONT_SM,
                     width=5, anchor="w").pack(side="left", padx=(0, 6))
            for i in range(BANDS_PER_GROUP):
                default = {"freq": str(DEFAULT_BANDS[i][0]),
                           "q": "1.00", "gain": "+0.0"}[key]
                p = PillEntry(line, value=default, width=86, height=30)
                # 官方自定义 EQ 是 5 段；第 6 段保留对象但不显示，
                # 这样「恢复出厂默认」仍能写满 6 段，业务方法一行都不用改。
                if i < 5:
                    p.pack(side="left", padx=3)
                pills[key].append(p)

        self.rows = []
        for i in range(BANDS_PER_GROUP):
            self.rows.append({
                "i": i,
                "freq": pills["freq"][i],
                "q": pills["q"][i],
                "gain": PillVar(pills["gain"][i]),
                "type": tk.StringVar(value="PK"),   # 官方界面无类型行，固定 PK
                "gain_lbl": tk.Label(card, text="+0.0"),  # 隐藏，供 _update_gain 使用
            })

        # Pregain 与音效名称控件已移除：工具不再读写 0x66，也不碰设备名称。

        # 曲线占满卡片中间剩余高度（上/下两段固定），按钮永远可见
        self.eq_curve = EqCurve(card, app=self, width=500, height=210)
        self.eq_curve.pack(fill="both", expand=True, pady=(6, 4))

        bottom = tk.Frame(card, bg=CARD)
        bottom.pack(fill="x")
        b1 = tk.Frame(bottom, bg=CARD)
        b1.pack(fill="x")
        RoundButton(b1, "重置", self._on_reset, width=205, height=36,
                    radius=18).pack(side="left", padx=(0, 10))
        RoundButton(b1, "导出", self._export_preset, width=205, height=36,
                    radius=18).pack(side="left")
        RoundButton(bottom, "保存", self._on_burn, width=430, height=36,
                    radius=18, primary=True).pack(fill="x", pady=(6, 0))

    def _build_right(self, parent):
        wrap = AppCard(parent, radius=20)
        wrap.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(wrap.body)
        nb = self.nb
        nb.pack(fill="both", expand=True)
        tab0 = tk.Frame(nb, bg=CARD, padx=12, pady=12)
        tab1 = tk.Frame(nb, bg=CARD, padx=10, pady=10)
        tab2 = tk.Frame(nb, bg=CARD, padx=10, pady=10)
        nb.add(tab0, text="  预设 EQ  ")
        nb.add(tab1, text="  游戏 EQ  ")
        nb.add(tab2, text="  社区 EQ  ")
        self._build_preset_tab(tab0)
        self._build_official_tab(tab1)
        self._build_community_tab(tab2)

    def _find_builtin(self, keyword):
        for p in getattr(self, "builtin", []):
            if keyword in p.name:
                return p
        # 兜底：内置预设若尚未加载/加载失败，即时再读一次，避免点了没反应
        fresh = cloud_eq.load_offline(
            resource_path("builtin_presets.json"), "builtin")
        if fresh:
            self.builtin = fresh
        for p in fresh:
            if keyword in p.name:
                return p
        return None

    def _build_preset_tab(self, parent):
        """官方「预设EQ」页：按 base.apk 内置预设生成按钮 + 恢复出厂 + 参数框。"""
        grid = tk.Frame(parent, bg=CARD)
        grid.pack(fill="x", pady=(2, 8))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        # self.builtin 已在 __init__ 提前加载；下面两级兜底保证即使资源读取
        # 出问题，预设按钮也不会整排消失（上一版正是栽在加载顺序上）。
        preset_names = [p.name for p in getattr(self, "builtin", [])
                        if "出厂" not in p.name]
        if not preset_names:
            preset_names = [p.name for p in cloud_eq.load_offline(
                resource_path("builtin_presets.json"), "builtin")
                if "出厂" not in p.name]
        if not preset_names:            # 极端兜底：文件读不到也要有按钮
            preset_names = ["澎湃低频", "清晰人声", "通透高频",
                            "均衡还原", "古典", "深度低频", "流行"]
        for i, n in enumerate(preset_names):
            # 按钮只显示去掉「★ 」装饰后的核心名；点击按子串匹配，稳定
            label = n.replace("★ ", "").strip()
            btn = RoundButton(grid, label,
                              lambda name=label: self._on_preset_button(name),
                              height=50, radius=16, width=190)
            btn.grid(row=i // 2, column=i % 2, padx=5, pady=5, sticky="ew")
        RoundButton(parent, "★ 恢复出厂默认",
                    lambda: self._on_preset_button("出厂默认"),
                    height=44, radius=18).pack(fill="x", pady=(2, 8))
        # 曲线只在左侧「自定义 EQ」画一张，这里用参数文字，避免两处重复
        self.preset_detail = tk.Text(parent, height=9, wrap="word",
                                     bg="#FAFCF8", fg=FG, font=FONT_MONO,
                                     relief="flat", highlightthickness=0, bd=0)
        self.preset_detail.pack(fill="both", expand=True, pady=(4, 0))
        self.preset_detail.insert("1.0", "点上方预设查看参数")
        self.preset_detail.config(state="disabled")
        tk.Label(parent, text="点预设即可载入到左侧参数区，再点「保存」写入耳机",
                 bg=CARD, fg=FG_SUB, font=FONT_SM).pack(pady=(6, 0))

    def _on_preset_button(self, name):
        p = self._find_builtin(name)
        if not p:
            self._say(f"「{name}」预设未加载，请检查 assets", "warn")
            return
        self.current = p
        self._load_preset_into_rows(p)
        self._show_detail(p)
        try:
            self.preset_detail.config(state="normal")
            self.preset_detail.delete("1.0", "end")
            lines = [f"{p.name}   {p.band_count} 段",
                     (p.desc or "")[:78], ""]
            for fr, ga, qq in zip(p.freq, p.gain, p.q):
                lines.append(f"  {fr:>7.0f} Hz   {ga:+5.2f} dB   Q={qq:.3f}")
            self.preset_detail.insert("1.0", "\n".join(lines))
            self.preset_detail.config(state="disabled")
        except Exception:
            pass
        self._say(f"已载入「{p.name}」，点左侧「保存」写入耳机", "ok")

    def _snapshot_rows(self):
        try:
            return [(r["freq"].get(), r["gain"].get(), r["q"].get(),
                     r["type"].get()) for r in self.rows]
        except Exception:
            return None

    def _restore_rows(self, snap):
        if not snap:
            return
        for r, (f, g, q, t) in zip(self.rows, snap):
            r["freq"].delete(0, "end")
            r["freq"].insert(0, str(f))
            r["gain"].set(g)
            r["q"].delete(0, "end")
            r["q"].insert(0, str(q))
            r["type"].set(t)

    def _on_custom_slot(self, slot):
        """自定义 1 / 2 是两套本地草稿；写设备固定只写组 A（自定义一）。"""
        if slot == self.custom_slot:
            return
        self.custom_slots[self.custom_slot] = self._snapshot_rows()
        self.custom_slot = slot
        snap = self.custom_slots[slot]
        if snap:
            self._restore_rows(snap)
        for i, btn in enumerate(self.slot_btns):
            btn.set_primary(i == slot)
        self._say(f"已切到自定义 {slot + 1}（本地草稿，写入仍按底部勾选）",
                  "info")

    def _make_tree(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, pady=(0, 6))
        cols = ("name", "bands")
        # height 只是最小请求高度；留小一点，底部按钮才不会被挤出可视区
        tv = ttk.Treeview(wrap, columns=cols, show="headings", height=6)
        tv.heading("name", text="音效名称")
        tv.heading("bands", text="段数")
        tv.column("name", width=320, anchor="w")
        tv.column("bands", width=62, anchor="center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    def _build_official_tab(self, parent):
        # 先给底部区域分配空间（side=bottom 优先占位），再放可伸缩列表
        # 点列表即载入左侧参数区并显示到曲线；写入统一走顶部工具栏「应用并保存」
        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill="x", side="bottom")
        RoundButton(btns, "导出", self._export_preset).pack(side="right", padx=3)
        RoundButton(btns, "导入", self._import_preset).pack(side="right", padx=3)

        self.detail = tk.Text(parent, height=4, wrap="word", bg="#FAFCF9",
                              fg=FG, font=FONT_MONO, relief="solid",
                              borderwidth=1, highlightthickness=0)
        self.detail.pack(fill="x", side="bottom", pady=(0, 8))
        self.detail.insert("1.0", "选择一条音效预设，查看频段参数。")
        self.detail.config(state="disabled")

        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", side="top", pady=(0, 8))
        tk.Label(top, text="搜索", bg=BG, fg=FG_SUB, font=FONT_SM).pack(side="left")
        ttk.Entry(top, textvariable=self.search_var, width=22).pack(side="left", padx=6)
        RoundButton(top, "刷新在线", self._refresh_official,
                    height=30).pack(side="right")

        self.off_tree = self._make_tree(parent)
        self.off_tree.bind("<<TreeviewSelect>>",
                           lambda _e: self._on_select(self.off_tree, self.presets))
        self.search_var.trace_add("write", lambda *_: self._fill_tree(
            self.off_tree, self.presets, self.search_var.get()))

    def _build_community_tab(self, parent):
        hint = tk.Label(parent, bg=GREEN_LIGHT, fg=GREEN_DARK, font=FONT_SM,
                        justify="left", anchor="w", padx=8, pady=7,
                        text="社区 EQ 需官方账号。已按官方 App 协议登录（区号 + 密码加密）。\n"
                             "账号密码只直连官方服务器，不写日志、不存盘，仅在本次运行内存中。")
        hint.pack(fill="x", side="top", pady=(0, 8))

        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", side="top", pady=(0, 6))
        tk.Label(f, text="账号", bg=BG, width=5).grid(row=0, column=0, padx=3, pady=2)
        self.user_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.user_var, width=20).grid(row=0, column=1, padx=3)
        tk.Label(f, text="区号", bg=BG, width=5).grid(row=0, column=2, padx=3)
        self.area_var = tk.StringVar(value="86")
        ttk.Entry(f, textvariable=self.area_var, width=7).grid(
            row=0, column=3, padx=3, sticky="w")
        RoundButton(f, "登录", self._on_login, height=30).grid(
            row=0, column=4, padx=5)

        self.pwd_label = tk.Label(f, text="密码", bg=BG, width=5)
        self.pwd_label.grid(row=1, column=0, padx=3, pady=2)
        self.pwd_var = tk.StringVar()
        pw_wrap = tk.Frame(f, bg=BG)
        pw_wrap.grid(row=1, column=1, padx=3, sticky="w")
        self.pwd_entry = ttk.Entry(pw_wrap, textvariable=self.pwd_var,
                                   width=11, show="*")
        self.pwd_entry.pack(side="left")
        self.show_pwd = tk.BooleanVar(value=False)
        tk.Checkbutton(pw_wrap, text="显示", variable=self.show_pwd,
                       command=self._on_show_pwd, bg=BG, font=FONT_SM,
                       activebackground=BG, highlightthickness=0).pack(side="left")
        self.login_mode = tk.StringVar(value="密码登录")
        mode = ttk.Combobox(f, textvariable=self.login_mode, width=11,
                            state="readonly", font=FONT_SM,
                            values=("密码登录", "验证码登录"))
        mode.grid(row=1, column=2, columnspan=2, padx=3, sticky="w")
        mode.bind("<<ComboboxSelected>>", self._on_login_mode)
        self.send_btn = RoundButton(f, "发验证码", self._on_send_vcode, height=30)
        self.send_btn.grid(row=1, column=4, padx=5)
        self.send_btn.set_enabled(False)

        tk.Label(f, text="状态", bg=BG, width=5).grid(row=2, column=0, padx=3, pady=2)
        self.login_state = tk.StringVar(value="○ 未登录")
        tk.Label(f, textvariable=self.login_state, bg=BG, fg=GREEN_DARK,
                 font=FONT_SM, anchor="w").grid(
            row=2, column=1, columnspan=3, padx=4, sticky="w")
        RoundButton(f, "手动填 Token", self._use_manual_token,
                    height=30).grid(row=2, column=4, padx=5)

        b = tk.Frame(parent, bg=BG)
        b.pack(fill="x", side="bottom")
        RoundButton(b, "刷新社区列表", self._refresh_community).pack(side="left")

        self.com_tree = self._make_tree(parent)
        self.com_tree.bind("<<TreeviewSelect>>",
                           lambda _e: self._on_select(self.com_tree, self.community))

    def _build_bottom_nav(self):
        """官方 App 底部 tab bar 的桌面版：状态 + 三个页面切换。"""
        bar = tk.Frame(self, bg=BG, height=44)
        bar.pack(fill="x", side="bottom", padx=14, pady=(0, 8))
        bar.pack_propagate(False)
        dot = tk.Canvas(bar, width=10, height=10, bg=BG,
                        highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=GREEN, outline="")
        dot.pack(side="left", padx=(4, 6))
        tk.Label(bar, textvariable=self.status_var, bg=BG, fg=FG_SUB,
                 font=FONT_SM).pack(side="left")
        tk.Label(bar, textvariable=self.cloud_status, bg=BG, fg=GREEN,
                 font=FONT_SM).pack(side="right", padx=4)
        center = tk.Frame(bar, bg=BG)
        center.place(relx=0.5, rely=0.5, anchor="center")
        for i, (t, ic) in enumerate((("预设 EQ", "star"),
                                     ("游戏 EQ", "wave"),
                                     ("社区 EQ", "planet"))):
            RoundButton(center, t, lambda idx=i: self.nb.select(idx),
                        icon=ic, height=30, radius=14, width=112).pack(
                side="left", padx=4)

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG, padx=14, pady=6)
        bar.pack(fill="x")
        items = [
            ("连接设备", self._on_connect, False, "link"),
            ("重新连接", self._on_reconnect, False, "refresh"),
            ("读取 EQ", self._on_read, False, "wave"),
            ("备份寄存器", self._on_backup, False, "save"),
            ("试听(写RAM)", self._on_apply, False, "eq"),
            ("应用并保存", self._on_burn, True, "check"),
            ("恢复备份", self._on_restore, False, "refresh"),
            ("全部归零", self._on_reset, False, "eq"),
        ]
        for text, cmd, primary, icon in items:
            RoundButton(bar, text, cmd, primary=primary, icon=icon,
                        height=32, radius=14).pack(side="left", padx=4)

    def _build_notice(self):
        """一行友好提示（白色圆角卡片）；技术细节收进「详细记录」。"""
        wrap = tk.Frame(self, bg=BG, padx=14, pady=4)
        wrap.pack(fill="x")
        self.notice = tk.Canvas(wrap, height=40, bg=BG,
                                highlightthickness=0)
        self.notice.pack(side="left", fill="x", expand=True)
        self.notice.bind("<Configure>", lambda _e: self._draw_notice())
        RoundButton(wrap, "详细记录", self._toggle_detail,
                    width=98, height=30, radius=14).pack(
            side="right", padx=(8, 0))
        self.detail_box = scrolledtext.ScrolledText(
            self, height=7, wrap="word", bg=CARD, fg=FG,
            font=FONT_MONO, relief="flat", highlightthickness=0)
        self.detail_shown = False
        self.notice_text = "准备就绪，连接 Z2 后即可开始调音"
        self.notice_kind = "info"
        self._draw_notice()

    def _draw_notice(self, _e=None):
        c = getattr(self, "notice", None)
        if c is None:
            return
        w = max(c.winfo_width(), 360)
        c.delete("all")
        palette = {
            "info": (CARD, GREEN),
            "ok": (CARD, "#4E9A5B"),
            "warn": (CARD, "#C9902A"),
            "error": (CARD, "#C0553F"),
        }
        fill, dot = palette.get(self.notice_kind, palette["info"])
        _round_polygon(c, 1, 1, w - 2, 38, 16, fill=fill,
                       outline=BORDER, width=1)
        c.create_oval(17, 16, 25, 24, fill=dot, outline="")
        c.create_text(36, 20, anchor="w", text=self.notice_text,
                      fill=FG, font=FONT)

    def _toggle_detail(self):
        self.detail_shown = not self.detail_shown
        if self.detail_shown:
            self.detail_box.pack(fill="x", padx=10, pady=(0, 4))
        else:
            self.detail_box.pack_forget()

    # ------------------------------------------------------------------ 工具
    def _friendly(self, msg):
        """把技术日志翻成一句人话；原文仍完整保留在「详细记录」里。"""
        m = str(msg)
        if any(k in m for k in ("失败", "错误", "无效", "不能为空")):
            return "刚才的操作没成功，点「详细记录」看原因", "error"
        if "连接成功" in m or "重连完成" in m:
            return "已连接 Z2，可以开始调音了", "ok"
        if "警告" in m:
            return "连接不太稳定，点「重新连接」再试一次", "warn"
        if "已读取" in m:
            return "已读出耳机里当前的 EQ", "ok"
        if "已备份" in m:
            return "寄存器已备份，放心调整", "ok"
        if "COMMIT" in m or "已写入并" in m:
            return "已保存到耳机，断电后依然生效", "ok"
        if "音效库已更新" in m:
            return "云端音效库已更新", "ok"
        if "已载入" in m:
            return "预设已载入，确认后点「应用并保存」写入耳机", "ok"
        if "已恢复" in m:
            return "已从备份恢复，点「读取 EQ」核对一下", "ok"
        if "正在" in m:
            return m.split("…")[0] + "…", "info"
        t = m.replace("[", "").replace("]", "").replace("\n", " ")
        return (t[:48] + "…") if len(t) > 48 else t, "info"

    def _apply_notice(self, text, kind):
        """错误提示在 12 秒内不被后台的 info/ok 消息盖掉。"""
        if kind in ("ok", "info") and time.time() < self._error_until:
            return
        if len(text) > 52:
            text = text[:50] + "…"
        self.notice_text, self.notice_kind = text, kind
        self._draw_notice()

    def _log(self, msg, friendly=None):
        try:
            self.detail_box.insert("end", str(msg) + "\n")
            self.detail_box.see("end")
        except Exception:
            pass
        if friendly:
            text, kind = friendly, "ok"
        else:
            text, kind = self._friendly(msg)
        if kind == "error":
            self._error_until = time.time() + 12
        self._apply_notice(text, kind)

    def _say(self, text, kind="info", detail=None):
        """在提示条显示一句人话，同时把技术原文写进「详细记录」。"""
        try:
            self.detail_box.insert("end", str(detail or text) + "\n")
            self.detail_box.see("end")
        except Exception:
            pass
        if kind == "error":
            self._error_until = time.time() + 12
        self._apply_notice(text, kind)

    def _update_gain(self, idx):
        r = self.rows[idx]
        v = r["gain"].get()
        r["gain_lbl"].config(text=f"{v:+.1f}",
                             fg=GREEN_DARK if v >= 0 else WARN)

    def _ensure(self) -> bool:
        try:
            if self.device.dev is None:
                self.device.connect()
            return True
        except Exception as e:
            self._log(f"[连接失败] {e}")
            self._set_device_state(False)
            return False

    def _bands_from_ui(self):
        out = []
        tmap = {"PK": TYPE_PK, "LSQ": TYPE_LSQ, "HSQ": TYPE_HSQ}
        for r in self.rows:
            try:
                out.append(Band(freq=float(r["freq"].get()), gain=r["gain"].get(),
                                q=float(r["q"].get()), type=tmap[r["type"].get()]))
            except ValueError as e:
                raise ValueError(f"第 {r['i'] + 1} 段输入无效：{e}")
        return out

    def _groups(self):
        return (0,)          # 只写自定义一（组 A），不碰自定义二（组 B）

    # ------------------------------------------------------------ 云端异步任务
    def _async(self, tag, fn, status):
        # 登录成功会自动拉社区列表，用户再点刷新就会重复请求；同 tag 直接忽略
        if tag in self._running_tags:
            return
        self._running_tags.add(tag)
        self._pending += 1
        self.status_var.set(status)

        def worker():
            try:
                res = fn()
            except Exception as exc:
                res = exc
            self._jobs.put((tag, res))

        threading.Thread(target=worker, daemon=True).start()
        self.after(120, self._poll_jobs)

    def _poll_jobs(self):
        try:
            while True:
                tag, res = self._jobs.get_nowait()
                self._pending -= 1
                self._running_tags.discard(tag)
                try:
                    self._on_job_done(tag, res)
                except Exception as e:
                    self._log(f"[内部错误] {tag}: {e}")
        except queue.Empty:
            pass
        if self._pending > 0:
            self.after(120, self._poll_jobs)
        else:
            self.status_var.set("就绪")

    def _on_job_done(self, tag, res):
        if isinstance(res, Exception):
            msg = str(res)
            self._log(f"[{tag} 失败] {msg}")
            if tag == "official":
                self.cloud_status.set("官方音效库：离线快照")
                self._say("云端暂时连不上，已用内置 25 条音效", "warn", msg)
            elif tag == "login":
                self._say(f"登录没成功：{msg}", "error", msg)
                self.status_var.set("登录失败")
            elif tag == "community":
                if any(k in msg for k in ("网络", "timed out", "SSL", "超时")):
                    self._say("网络不稳，社区 EQ 没拉到，点「刷新社区列表」重试",
                              "error", msg)
                else:
                    self._say(f"社区列表没拉到：{msg}", "error", msg)
            elif tag == "download":
                self._say(f"这条 EQ 没下载下来：{msg}", "error", msg)
            elif tag == "verify":
                self._say(f"验证码没发出去：{msg}", "error", msg)
            return
        if tag == "official":
            seen = set()
            res = [p for p in res if p.name not in seen and not seen.add(p.name)]
            self.presets = self.builtin + res
            self._fill_tree(self.off_tree, self.presets, self.search_var.get())
            self.cloud_status.set(
                f"内置 {len(self.builtin)} 条 + 云端 {len(res)} 条")
            self._log(f"在线音效库已更新：{len(res)} 条（另有内置 {len(self.builtin)} 条）")
        elif tag == "login":
            self.token = res
            self.login_state.set("● 已登录")
            self._say("登录成功，正在拉取社区 EQ…", "ok", "登录成功")
            self._refresh_community()
        elif tag == "verify":
            self._say("验证码已发送，收到后填进「验证码」框再点登录", "ok")
        elif tag == "community":
            self.community = res
            self._fill_tree(self.com_tree, self.community, "")
            if res:
                self._say(f"社区 EQ 拉到 {len(res)} 条，双击载入到左侧参数区",
                          "ok")
            else:
                self._say("社区列表是空的，可能账号没有可下载的音效", "warn")
        elif tag == "download":
            if res:
                self.current = res
                self._show_detail(res)
                self._log(f"已下载社区 EQ 详情：{res.name}")

    def _init_presets(self):
        if not self.builtin:           # __init__ 已提前加载，这里仅失败时补载
            self.builtin = cloud_eq.load_offline(
                resource_path("builtin_presets.json"), "builtin")
        local = cloud_eq.load_offline(resource_path("presets_offline.json"))
        # 离线快照里存有重名条目（如「无畏契约」出现两次），按名称去重
        seen = set()
        local = [p for p in local if p.name not in seen and not seen.add(p.name)]
        self.presets = self.builtin + local
        self._fill_tree(self.off_tree, self.presets, "")
        self.cloud_status.set(
            f"内置 {len(self.builtin)} 条 + 本地快照 {len(local)} 条")
        self._async("official", cloud_eq.fetch_official, "正在拉取官方音效库…")

    def _fill_tree(self, tree, presets, keyword=""):
        tree.delete(*tree.get_children())
        try:
            tree.tag_configure("builtin", foreground=GREEN_DARK,
                               background=GREEN_LIGHT)
        except Exception:
            pass
        only_game = tree is self.off_tree   # 内置预设不在游戏 EQ 页重复出现
        builtin_names = {p.name for p in getattr(self, "builtin", [])}
        kw = (keyword or "").strip().lower()
        seen = set()
        for i, p in enumerate(presets):
            if p.name in seen:          # 同一列表内按名称去重（离线快照确有重名）
                continue
            seen.add(p.name)
            if only_game and (p.source == "builtin" or p.name in builtin_names):
                continue
            if kw and kw not in p.name.lower():
                continue
            tree.insert("", "end", iid=str(i), values=(
                p.name, p.band_count),
                tags=("builtin",) if p.source == "builtin" else ())

    def _on_select(self, tree, presets):
        sel = tree.selection()
        if not sel:
            return
        try:
            p = presets[int(sel[0])]
        except (ValueError, IndexError):
            return
        self.current = p
        self._show_detail(p)
        # 点列表即把该音效载入左侧参数区，曲线自动刷新，无需单独的「载入到滑块」
        self._load_preset_into_rows(p)
        self._say(f"已载入「{p.name}」，确认后点顶部「应用并保存」写入耳机", "ok")

    def _show_detail(self, p: Preset):
        full = p.source == "builtin" and p.band_count == len(self.rows)
        if full:
            bands = list(zip(p.freq, p.gain, p.q))
            how = "原样（6 段全写，恢复默认用）"
        else:
            bands = p.to_bands(5)
            how = "重采样到官方 5 段" if p.band_count > 5 else "原样 5 段"
        lines = [f"{p.name}   ·   {p.band_count} 段   ·   来源: {p.source}"]
        if p.desc:
            lines.append(p.desc[:90])
        lines.append("写入 Z2：" + how)
        for f, g, q in bands:
            lines.append(f"   {f:>7.0f} Hz   {g:+5.2f} dB   Q={q:.3f}")
        text = "\n".join(lines)
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.config(state="disabled")

    def _refresh_official(self):
        self._async("official", cloud_eq.fetch_official, "正在刷新官方音效库…")

    def _on_show_pwd(self):
        vcode = self.login_mode.get() == "验证码登录"
        self.pwd_entry.config(
            show="" if (vcode or self.show_pwd.get()) else "*")

    def _on_login_mode(self, *_):
        vcode = self.login_mode.get() == "验证码登录"
        self.pwd_label.config(text="验证码" if vcode else "密码")
        self._on_show_pwd()
        self.send_btn.set_enabled(vcode)
        if vcode:
            self._say("验证码模式：点「发验证码」，收到后填入并登录", "info")

    def _on_send_vcode(self):
        u = self.user_var.get().strip()
        if not u:
            messagebox.showwarning("提示", "请先填写手机号。")
            return
        ac = self.area_var.get().strip() or "86"
        self._async("verify", lambda: cloud_eq.send_verify_code(u, ac),
                    "正在发送短信验证码…")

    def _on_login(self):
        u, pw = self.user_var.get().strip(), self.pwd_var.get().strip()
        ac = self.area_var.get().strip() or "86"
        vcode_mode = self.login_mode.get() == "验证码登录"
        if not u or not pw:
            messagebox.showwarning(
                "提示", f"请填写账号与{'短信验证码' if vcode_mode else '密码'}。")
            return
        self._async("login",
                    lambda: cloud_eq.login(u, pw, area_code=ac,
                                           use_vcode=vcode_mode, vcode=pw),
                    "正在登录官方服务器…")

    def _use_manual_token(self):
        t = simpledialog.askstring(
            "手动填 Token", "粘贴官方账号 token（仅保存在本次运行内存）：",
            parent=self)
        if not t or not t.strip():
            return
        self.token = t.strip()
        self.login_state.set("● 已登录（手动 token）")
        self._say("已使用手动填入的 token，正在拉取社区 EQ", "ok")
        self._refresh_community()

    def _refresh_community(self):
        if not self.token:
            self._log("社区 EQ 需要登录或手动填入 token；官方音效库无需登录即可使用。")
            return
        self._async("community", lambda: cloud_eq.fetch_community(self.token),
                    "正在拉取社区 EQ…")

    def _download_detail(self):
        if not self.current:
            self._say("请先在列表里选中一条音效", "warn")
            return
        # 实测：eq/list 返回的每条记录已含完整 freq/q/sound，
        # eq/download 接口只回空 data，无需再请求一次。
        self._show_detail(self.current)
        self._say("这条 EQ 的参数已完整显示，点列表即已载入左侧参数区", "ok")

    # ------------------------------------------------------------ 预设应用
    def _load_preset_into_rows(self, p: Preset):
        full = p.source == "builtin" and p.band_count == len(self.rows)
        if full:
            bands = list(zip(p.freq, p.gain, p.q))
        else:
            bands = list(p.to_bands(5))
            while len(bands) < 5:
                bands.append((1000.0, 0.0, 1.0))
        n = len(self.rows) if full else 5
        for i, (f, g, q) in enumerate(bands[:n]):
            r = self.rows[i]
            r["freq"].delete(0, "end")
            r["freq"].insert(0, str(int(round(f))))
            r["gain"].set(g)
            self._update_gain(i)
            r["q"].delete(0, "end")
            r["q"].insert(0, f"{q:.3f}")
            r["type"].set("PK")
        if full:
            self._log(f"已载入「{p.name}」：6 段原样填入，"
                      "点「应用并保存」即可写入耳机。")
        else:
            if p.band_count > 5:
                self._log(f"「{p.name}」为 {p.band_count} 段，"
                          "已按对数频率重采样到 Z2 的 5 段。")
            self._log(f"已载入「{p.name}」：前 5 段为预设值，"
                      "第 6 段保留设备原值，保存时 6 段一起写。")

    def _load_current(self, write_device: bool):
        if not self.current:
            self._log("请先在列表中选择一条预设。")
            return
        p = self.current
        if write_device:
            if not self._ensure():
                return
            if not messagebox.askyesno(
                    "确认写入", f"将把「{p.name}」写入耳机并执行 COMMIT。\n"
                                "会改变音色，建议先备份寄存器。继续？"):
                return
        self._load_preset_into_rows(p)
        if not write_device:
            self._log("已载入滑块，检查后点「应用并保存」即可写入。")
            return
        try:
            # 统一写满 6 段（第 6 段为设备原值），避免只写 5 段导致 COMMIT 失败
            self.device.set_eq(self._bands_from_ui(), groups=self._groups())
            self.device.burn_eq()
            self._set_device_state(True)
            self._log(f"「{p.name}」已写入并 COMMIT。拔插耳机后可验证掉电保持。")
        except Exception as e:
            self._log(f"[应用失败] {e}")
            self._set_device_state(False)

    def _export_preset(self):
        try:
            n = len(self.rows)
            bands = self._bands_from_ui()[:n]
        except ValueError as e:
            self._log(f"[导出失败] {e}")
            return
        name = self.current.name if self.current else "我的 EQ"
        p = Preset(name=name + " (导出)",
                   freq=[b.freq for b in bands],
                   gain=[b.gain for b in bands],
                   q=[b.q for b in bands],
                   pregain=0.0,
                   source="local")
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("EQ 预设", "*.json")],
            initialfile="z2_eq_preset.json", title="导出 EQ 预设")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(p.to_dict(), ensure_ascii=False,
                                             indent=2), encoding="utf-8")
            self._log(f"已导出 → {path}")
        except Exception as e:
            self._log(f"[导出失败] {e}")

    def _import_preset(self):
        path = filedialog.askopenfilename(
            filetypes=[("EQ 预设", "*.json"), ("所有文件", "*.*")],
            title="导入 EQ 预设")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            p = cloud_eq.parse_preset(data, "local")
            if not p:
                raise ValueError("文件里没有可解析的 freq / gain 数据")
            self.presets.append(p)
            self._fill_tree(self.off_tree, self.presets, self.search_var.get())
            self.current = p
            self._show_detail(p)
            self._log(f"已导入「{p.name}」，选中后点一下即可载入左侧参数区。")
        except Exception as e:
            self._log(f"[导入失败] {e}")

    # ------------------------------------------------------------ 设备按钮
    def _verify(self, tag):
        if self.device.ping():
            self._set_device_state(True)
            self._log(f"{tag}：HID 接口 3 已接管，读写验证通过")
        else:
            self._set_device_state(None)
            self._log(f"[警告] {tag}，但试读无应答。请点「重新连接」；"
                      "若仍无应答，暂停音乐或关闭其它占用 HID 的程序。")

    def _on_connect(self):
        try:
            if self.device.dev is None:
                self._set_device_state(None)
                self.device.connect()
            self._verify("连接成功")
        except Exception as e:
            self._set_device_state(False)
            self._log(f"[连接失败] {e}")

    def _on_reconnect(self):
        self._set_device_state(None)
        self._log("正在强制重连（释放旧 USB 句柄并重新枚举）…")
        try:
            self.device.reconnect()
            self._verify("重连完成")
        except Exception as e:
            self._set_device_state(False)
            self._log(f"[重连失败] {e}")

    def _on_read(self):
        if not self._ensure():
            return
        try:
            bands = self.device.get_eq()
            for r, b in zip(self.rows, bands):
                r["freq"].delete(0, "end")
                r["freq"].insert(0, str(int(b.freq)))
                r["gain"].set(b.gain)
                self._update_gain(r["i"])
                r["q"].delete(0, "end")
                r["q"].insert(0, f"{b.q:.3f}")
                r["type"].set(TYPE_NAMES.get(b.type, "PK"))
            self._set_device_state(True)
            self._log("已读取设备当前 EQ（组 A）")
        except Exception as e:
            self._set_device_state(False)
            self._log(f"[读取失败] {e}")
            self._log("→ 通信层已自动清端点并重连；仍失败请点「重新连接」或拔插耳机。")

    def _on_backup(self):
        if not self._ensure():
            return
        try:
            d = self.device.backup(BACKUP_FILE)
            ok = sum(1 for v in d.values() if v is not None)
            self._log(f"已备份 {ok}/256 个寄存器 → {BACKUP_FILE}（请妥善保存）")
        except Exception as e:
            self._log(f"[备份失败] {e}")

    def _on_apply(self):
        if not self._ensure():
            return
        if not messagebox.askyesno("试听(写入RAM)",
                                   "把当前 EQ 写入耳机寄存器并立即试听：\n"
                                   "• 仅写入 RAM，DSP 立刻生效，但断电/重插即丢失；\n"
                                   "• 不执行 COMMIT，所以不会永久保存；\n"
                                   "• 想永久保存请点顶部「应用并保存」。\n继续试听？"):
            return
        try:
            self.device.set_eq(self._bands_from_ui(), groups=self._groups())
            self._log("已写入 RAM（未提交）。点「应用并保存」执行 COMMIT(0x53) 才会永久保存。")
        except Exception as e:
            self._log(f"[写入失败] {e}")

    def _on_burn(self):
        if not self._ensure():
            return
        if not messagebox.askyesno(
                "确认应用保存", "将写入当前 EQ 并执行 COMMIT(0x53)。\n"
                                "官方 App 正是用这一步应用并保存，无独立烧录命令。\n"
                                "确认已备份？"):
            return
        try:
            self.device.set_eq(self._bands_from_ui(), groups=self._groups())
            self.device.burn_eq()
            self._set_device_state(True)
            self._log("已 COMMIT。拔插耳机后重新点「读取 EQ」可核对掉电保持。")
        except Exception as e:
            self._set_device_state(False)
            self._log(f"[应用保存失败] {e}")

    def _on_restore(self):
        if not os.path.exists(BACKUP_FILE):
            self._log(f"[恢复失败] 找不到 {BACKUP_FILE}，请先备份。")
            return
        if not messagebox.askyesno(
                "危险操作", f"将用 {BACKUP_FILE} 覆盖 EQ 相关寄存器并提交。\n"
                           "仅恢复 0x34-0x4D、0x56、0x66，不碰固件信息区。继续？"):
            return
        if not self._ensure():
            return
        try:
            n = self.device.restore(BACKUP_FILE, regs=RESTORE_REGS)
            self._log(f"已恢复 {n} 个寄存器并 COMMIT。点「读取 EQ」核对。")
        except Exception as e:
            self._log(f"[恢复失败] {e}")

    def _on_reset(self):
        for r in self.rows:
            r["gain"].set(0.0)
            self._update_gain(r["i"])
        self._log("界面增益已归零（尚未写入设备）")

    def destroy(self):
        try:
            self.device.disconnect()
        except Exception:
            pass
        super().destroy()


if __name__ == "__main__":
    Z2Tuner().mainloop()
