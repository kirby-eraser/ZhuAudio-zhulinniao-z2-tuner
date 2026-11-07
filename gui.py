"""
gui.py — 竹林鸟 霜华 Z2 PC 调音台（美化版 · 官方云端 EQ）
============================================================
6 段参数 EQ + 官方在线音效库（25 条，无需登录）+ 社区 EQ（需登录）+ 导入导出。
底层通信走 protocol.Z2Device，云端走 cloud_eq，二者互不阻塞。

运行：python gui.py
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
BG = "#F4F7F2"
CARD = "#FFFFFF"
GREEN = "#4E7A4C"
GREEN_DARK = "#2F5A38"
GREEN_LIGHT = "#E7F1E4"
BORDER = "#D8E4D5"
FG = "#26332A"
FG_SUB = "#6B7A6E"
WARN = "#B4472E"
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 17, "bold")
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


class RoundButton(tk.Canvas):
    """圆角按钮（tkinter 无原生圆角，用 Canvas 自绘，支持 hover / 按下 / 禁用）。"""

    def __init__(self, master, text, command=None, primary=False,
                 width=None, height=34, radius=11, bg=BG, font=None):
        if width is None:
            width = max(66, _text_width(text) + 30)
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._text = text
        self._cmd = command
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
        self.create_text(w / 2, h / 2, text=self._text, fill=fg,
                         font=self._font)

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
        self.geometry("1100x790")
        self.minsize(1020, 720)
        self.configure(bg=BG)
        try:
            self.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass

        self.device = Z2Device()
        self.rows: list[dict] = []
        self.write_both = tk.BooleanVar(value=False)   # 官方实测只写组 A
        self.official = tk.BooleanVar(value=False)
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.cloud_status = tk.StringVar(value="官方音效库：本地快照")
        self.token: str = ""
        self.presets: list[Preset] = []
        self.builtin: list[Preset] = []   # 内置预设 + 出厂默认，恒置顶
        self.community: list[Preset] = []
        self.current: Preset | None = None

        self.photos = []          # 防止 PhotoImage 被 GC
        self._jobs: queue.Queue = queue.Queue()
        self._pending = 0
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
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, font=FONT)
        s.configure("TFrame", background=BG)
        s.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER,
                    relief="solid", borderwidth=1, padding=10)
        s.configure("Card.TLabelframe.Label", background=CARD,
                    foreground=GREEN_DARK, font=FONT_BOLD)
        s.configure("Tool.TButton", padding=(9, 5), background=CARD,
                    foreground=FG, bordercolor=BORDER, relief="solid", borderwidth=1)
        s.map("Tool.TButton", background=[("active", GREEN_LIGHT)],
              bordercolor=[("active", GREEN)])
        s.configure("Accent.TButton", padding=(11, 6), background=GREEN,
                    foreground="#FFFFFF", bordercolor=GREEN, relief="flat")
        s.map("Accent.TButton", background=[("active", GREEN_DARK),
                                            ("disabled", "#A9C0A5")])
        s.configure("TNotebook", background=BG, bordercolor=BORDER)
        s.configure("TNotebook.Tab", padding=(14, 7), background=GREEN_LIGHT,
                    foreground=GREEN_DARK, font=FONT_BOLD)
        s.map("TNotebook.Tab", background=[("selected", CARD)])
        s.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=FG, rowheight=27, bordercolor=BORDER, font=FONT_SM)
        s.configure("Treeview.Heading", background=GREEN_LIGHT,
                    foreground=GREEN_DARK, font=FONT_BOLD, relief="flat")
        s.map("Treeview", background=[("selected", GREEN_LIGHT)],
              foreground=[("selected", GREEN_DARK)])
        s.configure("TCheckbutton", background=CARD, foreground=FG)
        s.configure("TEntry", fieldbackground="#FBFCFA", bordercolor=BORDER)
        s.configure("TScale", background=CARD, troughcolor=GREEN_LIGHT)

    # ------------------------------------------------------------------ 布局
    def _build_ui(self):
        self._build_banner()

        main = ttk.Frame(self, padding=(10, 4))
        main.pack(fill="both", expand=True)
        left = ttk.Frame(main, width=575)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        left.pack_propagate(False)
        right = ttk.Frame(main, width=493)
        right.pack(side="right", fill="both", expand=True, padx=(6, 0))
        right.pack_propagate(False)
        self._build_left(left)
        self._build_right(right)

        self._build_toolbar()
        self._build_notice()

        status = ttk.Frame(self, padding=(12, 5))
        status.pack(fill="x", side="bottom")
        ttk.Label(status, textvariable=self.status_var, foreground=FG_SUB,
                  font=FONT_SM).pack(side="left")
        ttk.Label(status, textvariable=self.cloud_status, foreground=GREEN,
                  font=FONT_SM).pack(side="right")

    def _build_banner(self):
        bar = tk.Frame(self, bg=CARD, height=112)
        bar.pack(fill="x", padx=10, pady=(10, 2))
        bar.pack_propagate(False)
        tk.Frame(bar, bg=GREEN, width=6).pack(side="left", fill="y")

        try:
            img = tk.PhotoImage(file=resource_path("earphone_banner.png"))
            self.photos.append(img)
            tk.Label(bar, image=img, bg=CARD).pack(side="right", padx=(6, 14))
        except Exception:
            pass

        right = tk.Frame(bar, bg=CARD)
        right.pack(side="right", padx=18)
        self.state_canvas = tk.Canvas(right, width=14, height=14, bg=CARD,
                                      highlightthickness=0)
        self.state_canvas.pack(side="left", padx=(0, 7))
        self.state_dot = self.state_canvas.create_oval(2, 2, 12, 12,
                                                        fill="#C9A227", outline="")
        self.state_lbl = tk.Label(right, text="未连接", bg=CARD, fg=FG_SUB,
                                  font=FONT_SM)
        self.state_lbl.pack(side="left")

        try:
            logo = tk.PhotoImage(file=resource_path("logo.png"))
            self.photos.append(logo)
            tk.Label(bar, image=logo, bg=CARD).pack(side="left", padx=(16, 12))
        except Exception:
            pass

        txt = tk.Frame(bar, bg=CARD)
        txt.pack(side="left", fill="y", pady=18)
        tk.Label(txt, text="竹林鸟 霜华 Z2 · 调音台", bg=CARD, fg=GREEN_DARK,
                 font=FONT_TITLE).pack(anchor="w")
        tk.Label(txt, text="KTMicro HID  ·  6 段参数 EQ  ·  官方云端音效库",
                 bg=CARD, fg=FG_SUB, font=FONT_SM).pack(anchor="w", pady=(4, 0))
        tk.Label(txt, text="协议来源：官方 APK 反编译 + 本机实机验证",
                 bg=CARD, fg="#9AA89C", font=("Microsoft YaHei UI", 8)).pack(
                     anchor="w", pady=(3, 0))

    def _set_device_state(self, ok: bool | None):
        color = "#3E9B54" if ok else ("#C9A227" if ok is None else "#C0553F")
        text = "已连接" if ok else ("连接中" if ok is None else "未连接")
        try:
            self.state_canvas.itemconfig(self.state_dot, fill=color)
            self.state_lbl.config(text=text)
        except Exception:
            pass

    def _build_left(self, parent):
        card = ttk.LabelFrame(parent, text="  参数均衡器 EQ  ·  组 A 0x35–0x40  ",
                              style="Card.TLabelframe")
        card.pack(fill="both", expand=True)

        hdr = ["段", "频率 Hz", "增益 (dB)", "", "Q", "类型"]
        for c, h in enumerate(hdr):
            ttk.Label(card, text=h, font=FONT_BOLD, foreground=GREEN_DARK).grid(
                row=0, column=c, sticky="w", padx=5, pady=(0, 8))
        tk.Frame(card, height=1, bg=BORDER).grid(
            row=1, column=0, columnspan=6, sticky="ew", pady=(0, 8))

        for i in range(BANDS_PER_GROUP):
            rr = i + 2
            r = {"i": i}
            badge = tk.Label(card, text=f"{i + 1}", bg=GREEN_LIGHT, fg=GREEN_DARK,
                             font=("Consolas", 9, "bold"), width=3, relief="flat")
            badge.grid(row=rr, column=0, padx=5, pady=4)
            r["freq"] = ttk.Entry(card, width=9, font=FONT_MONO, justify="center")
            r["freq"].insert(0, str(DEFAULT_BANDS[i][0]))
            r["freq"].grid(row=rr, column=1, padx=5)

            r["gain"] = tk.DoubleVar(value=0.0)
            r["scale"] = ttk.Scale(card, from_=GAIN_MIN, to=GAIN_MAX,
                                   orient="horizontal", variable=r["gain"],
                                   length=215,
                                   command=lambda _v, idx=i: self._update_gain(idx))
            r["scale"].grid(row=rr, column=2, padx=5, sticky="ew")
            r["gain_lbl"] = tk.Label(card, text="+0.0", width=6, bg=CARD,
                                     fg=GREEN_DARK, font=("Consolas", 10, "bold"))
            r["gain_lbl"].grid(row=rr, column=3, padx=3)

            r["q"] = ttk.Entry(card, width=8, font=FONT_MONO, justify="center")
            r["q"].insert(0, "0.700")
            r["q"].grid(row=rr, column=4, padx=5)
            r["type"] = ttk.Combobox(card, values=["PK", "LSQ", "HSQ"],
                                     width=6, state="readonly", font=FONT_MONO)
            r["type"].set("PK")
            r["type"].grid(row=rr, column=5, padx=5)
            self.rows.append(r)

        tk.Frame(card, height=1, bg=BORDER).grid(
            row=9, column=0, columnspan=6, sticky="ew", pady=10)
        pre = ttk.Frame(card)
        pre.grid(row=10, column=0, columnspan=6, sticky="w", pady=(0, 6))
        ttk.Label(pre, text="全局增益 Pregain", font=FONT_BOLD,
                  foreground=GREEN_DARK).pack(side="left")
        self.pregain = ttk.Entry(pre, width=7, font=FONT_MONO, justify="center")
        self.pregain.insert(0, "0")
        self.pregain.pack(side="left", padx=8)
        ttk.Label(pre, text="dB（为避免误写，只在数值变化时才提交）",
                  foreground=FG_SUB, font=FONT_SM).pack(side="left")

        opts = ttk.Frame(card)
        opts.grid(row=11, column=0, columnspan=6, sticky="w")
        ttk.Checkbutton(opts, text="两组寄存器都写 (0x35 / 0x42)",
                        variable=self.write_both).pack(anchor="w", pady=2)
        ttk.Checkbutton(opts, text="官方兼容模式（仅 5 段 · 只写组 A）",
                        variable=self.official).pack(anchor="w", pady=2)
        ttk.Label(card, text="手机官方 App 实测只改组 A 前 5 段，组 B 保持出厂；\n"
                             "默认不碰组 B。云端预设载入时会自动勾选官方兼容模式。",
                  foreground=FG_SUB, font=FONT_SM, justify="left").grid(
                      row=12, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _build_right(self, parent):
        self.nb = ttk.Notebook(parent)
        nb = self.nb
        nb.pack(fill="both", expand=True)
        tab1 = tk.Frame(nb, bg=BG, padx=8, pady=8)
        tab2 = tk.Frame(nb, bg=BG, padx=8, pady=8)
        nb.add(tab1, text="  官方音效库  ")
        nb.add(tab2, text="  社区 EQ  ")
        self._build_official_tab(tab1)
        self._build_community_tab(tab2)

    def _make_tree(self, parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, pady=(0, 6))
        cols = ("name", "bands", "gain")
        # height 只是最小请求高度；留小一点，底部按钮才不会被挤出可视区
        tv = ttk.Treeview(wrap, columns=cols, show="headings", height=6)
        tv.heading("name", text="音效名称")
        tv.heading("bands", text="段数")
        tv.heading("gain", text="全局增益")
        tv.column("name", width=250, anchor="w")
        tv.column("bands", width=62, anchor="center")
        tv.column("gain", width=82, anchor="center")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    def _build_official_tab(self, parent):
        # 先给底部区域分配空间（side=bottom 优先占位），再放可伸缩列表
        btns = tk.Frame(parent, bg=BG)
        btns.pack(fill="x", side="bottom")
        RoundButton(btns, "载入到滑块",
                    lambda: self._load_current(False)).pack(side="left", padx=(0, 5))
        RoundButton(btns, "应用并保存",
                    lambda: self._load_current(True), primary=True).pack(
                        side="left", padx=5)
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
        RoundButton(b, "载入到滑块",
                    lambda: self._load_current(False)).pack(side="right", padx=3)
        RoundButton(b, "查看参数", self._download_detail).pack(side="right", padx=3)

        self.com_tree = self._make_tree(parent)
        self.com_tree.bind("<<TreeviewSelect>>",
                           lambda _e: self._on_select(self.com_tree, self.community))

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG, padx=10, pady=5)
        bar.pack(fill="x")
        items = [
            ("连接设备", self._on_connect, False),
            ("重新连接", self._on_reconnect, False),
            ("读取 EQ", self._on_read, False),
            ("备份寄存器", self._on_backup, False),
            ("写入(RAM)", self._on_apply, False),
            ("应用并保存", self._on_burn, True),
            ("恢复备份", self._on_restore, False),
            ("全部归零", self._on_reset, False),
        ]
        for text, cmd, primary in items:
            RoundButton(bar, text, cmd, primary=primary).pack(side="left", padx=4)

    def _build_notice(self):
        """用一行友好提示替代工程日志；技术细节收进可展开的「详细记录」。"""
        wrap = tk.Frame(self, bg=BG, padx=10, pady=4)
        wrap.pack(fill="x")
        self.notice = tk.Canvas(wrap, height=40, bg=BG, highlightthickness=0)
        self.notice.pack(side="left", fill="x", expand=True)
        self.notice.bind("<Configure>", lambda _e: self._draw_notice())
        RoundButton(wrap, "详细记录", self._toggle_detail,
                    width=96, height=30).pack(side="right", padx=(8, 0))
        self.detail_box = scrolledtext.ScrolledText(
            self, height=7, wrap="word", bg="#FAFCF9", fg=FG,
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
            "info": (GREEN_LIGHT, GREEN),
            "ok": ("#E2F2E6", "#3E9B54"),
            "warn": ("#FBF0DC", "#C9902A"),
            "error": ("#FBEAE6", "#C0553F"),
        }
        fill, dot = palette.get(self.notice_kind, palette["info"])
        _round_polygon(c, 1, 1, w - 2, 38, 12, fill=fill, outline="")
        c.create_oval(15, 15, 25, 25, fill=dot, outline="")
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
            return "预设已载入滑块，确认后点「应用并保存」", "ok"
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
        return (0,) if (self.official.get() or not self.write_both.get()) else (0, 1)

    # ------------------------------------------------------------ 云端异步任务
    def _async(self, tag, fn, status):
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
                self._say(f"社区列表没拉到：{msg}", "error", msg)
            elif tag == "download":
                self._say(f"这条 EQ 没下载下来：{msg}", "error", msg)
            elif tag == "verify":
                self._say(f"验证码没发出去：{msg}", "error", msg)
            return
        if tag == "official":
            self.presets = self.builtin + res
            self._fill_tree(self.off_tree, self.presets, self.search_var.get())
            self.cloud_status.set(
                f"内置 {len(self.builtin)} 条 + 云端 {len(res)} 条")
            self._log(f"在线音效库已更新：{len(res)} 条（另有内置 {len(self.builtin)} 条）")
        elif tag == "login":
            self.token = res
            self.login_state.set("● 已登录")
            self._say("登录成功，正在拉取社区 EQ…", "ok",
                      "登录成功，token 仅保存在内存中（不显示、不写入磁盘）")
            self._refresh_community()
        elif tag == "verify":
            self._say("验证码已发送，收到后填进「验证码」框再点登录", "ok")
        elif tag == "community":
            self.community = res
            self._fill_tree(self.com_tree, self.community, "")
            self._log(f"社区 EQ：{len(res)} 条")
            if res:
                self._say(f"社区 EQ 拉到 {len(res)} 条，双击载入到滑块", "ok")
            else:
                self._say("社区列表是空的，可能账号没有可下载的音效", "warn")
        elif tag == "download":
            if res:
                self.current = res
                self._show_detail(res)
                self._log(f"已下载社区 EQ 详情：{res.name}")

    def _init_presets(self):
        self.builtin = cloud_eq.load_offline(
            resource_path("builtin_presets.json"), "builtin")
        local = cloud_eq.load_offline(resource_path("presets_offline.json"))
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
        kw = (keyword or "").strip().lower()
        for i, p in enumerate(presets):
            if kw and kw not in p.name.lower():
                continue
            tree.insert("", "end", iid=str(i), values=(
                p.name, p.band_count, f"{p.pregain:+.1f}"),
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

    def _show_detail(self, p: Preset):
        full = p.source == "builtin" and p.band_count == len(self.rows)
        if full:
            bands = list(zip(p.freq, p.gain, p.q))
            how = "原样（6 段全写，恢复默认用）"
        else:
            bands = p.to_bands(5)
            how = "重采样到官方 5 段" if p.band_count > 5 else "原样 5 段"
        lines = [f"{p.name}   ·   {p.band_count} 段   ·   "
                 f"来源: {p.source}   ·   pregain {p.pregain:+.1f} dB"]
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
        self._say("这条 EQ 的参数已完整显示，可直接「载入到滑块」", "ok")

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
        self.pregain.delete(0, "end")
        self.pregain.insert(0, str(int(round(p.pregain))))
        if full:
            self.official.set(False)      # 6 段全部写组 A
            self.write_both.set(False)    # 保持不碰组 B
            self._log(f"已载入「{p.name}」：6 段原样填入，关闭官方兼容模式，"
                      "点「应用并保存」即可恢复出厂音色。")
        else:
            self.official.set(True)       # 只写组 A 前 5 段，完全对齐官方
            if p.band_count > 5:
                self._log(f"「{p.name}」为 {p.band_count} 段，"
                          "已按对数频率重采样到 Z2 的 5 段。")
            self._log(f"已载入「{p.name}」；已勾选官方兼容模式，"
                      "第 6 段保持设备原值。")
        if abs(p.pregain - round(p.pregain)) > 0.01:
            self._log(f"  注意：官方 main_gain {p.pregain:+.1f} dB，"
                      "本工具按整数 dB 写入。")

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
            # 载入预设后 self.official 已被 _load_preset_into_rows 设好：
            # 普通官方预设 = True（5 段），出厂默认 = False（6 段全写）
            self.device.set_eq(self._bands_from_ui(), groups=self._groups(),
                               official=self.official.get())
            try:
                want = float(self.pregain.get())
                if abs(want - self.device.get_pregain()) > 0.01:
                    self.device.set_pregain(want)
            except ValueError:
                pass
            self.device.burn_eq()
            self._set_device_state(True)
            self._log(f"「{p.name}」已写入并 COMMIT。拔插耳机后可验证掉电保持。")
        except Exception as e:
            self._log(f"[应用失败] {e}")
            self._set_device_state(False)

    def _export_preset(self):
        try:
            n = len(self.rows) if not self.official.get() else 5
            bands = self._bands_from_ui()[:n]
        except ValueError as e:
            self._log(f"[导出失败] {e}")
            return
        name = self.current.name if self.current else "我的 EQ"
        p = Preset(name=name + " (导出)",
                   freq=[b.freq for b in bands],
                   gain=[b.gain for b in bands],
                   q=[b.q for b in bands],
                   pregain=float(self.pregain.get() or 0),
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
            self._log(f"已导入「{p.name}」，选中后可载入到滑块。")
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
            self.pregain.delete(0, "end")
            self.pregain.insert(0, f"{self.device.get_pregain():.0f}")
            self._set_device_state(True)
            self._log("已读取设备当前 EQ（组 A）与 pregain")
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
        if not messagebox.askyesno("确认", "将把当前 EQ 写入耳机寄存器（尚未提交生效）。\n"
                                       "建议已做备份。继续？"):
            return
        try:
            self.device.set_eq(self._bands_from_ui(), groups=self._groups(),
                               official=self.official.get())
            self._log("已写入寄存器（未提交）。点「应用并保存」执行 COMMIT(0x53)。")
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
            self.device.set_eq(self._bands_from_ui(), groups=self._groups(),
                               official=self.official.get())
            try:
                want = float(self.pregain.get())
                if abs(want - self.device.get_pregain()) > 0.01:
                    self.device.set_pregain(want)
            except ValueError:
                pass
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
