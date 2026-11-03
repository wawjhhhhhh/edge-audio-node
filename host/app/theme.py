"""主题引擎：两套配色 + 可热切换。

为什么要有这一层
----------------
这类软件（REW / Dirac Live / SDR++ / HEARD）几乎都提供明暗两套主题：
深色是长时间盯屏的默认，浅色是投屏/强光环境的备选。Dirac 2.2 甚至把
"高对比度主题"做成了卖点。所以配色不能写死在控件构造里，必须集中管理。

设计语言（参考上面几个实际产品）
- 中性底色，面板只比底色亮一档；分隔线是 1px 细线，不用凹槽边框
- 只有一个强调色，其余颜色都有语义（正常/警告/错误）
- 所有数字用等宽字体。Dirac 2.2 的一个改动就是把每个电平条都加上 dB 数值 ——
  光看一根条子读不出数，这是仪表盘的硬要求
- 画布（示波/频谱）比面板更深/更浅一档，让波形浮起来

用法
----
    theme.apply(root)                 # 建 ttk 样式
    theme.bind(widget, bg="panel", fg="text")   # 登记 + 立即上色
    theme.set("light") / theme.toggle(root)     # 热切换（自动遍历登记表重上色）
"""
import tkinter as tk
from tkinter import ttk

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

PALETTES = {
    "dark": {
        "bg":       "#15171B",
        "panel":    "#1C1F24",
        "panel2":   "#242830",
        "panel3":   "#2C323B",
        "border":   "#333942",
        "hair":     "#262B33",
        "text":     "#E7E9EC",
        "muted":    "#8A929D",
        "dim":      "#5B636E",
        "accent":   "#4C9AFF",
        "accent_fg": "#0A1626",
        "accent_d": "#2E6FD0",
        "ok":       "#3FBF7F",
        "warn":     "#E8A33D",
        "bad":      "#E0574B",
        # 画布
        "scope":    "#0E1013",
        "grid":     "#1E242C",
        "axis":     "#2A323C",
        "scope_txt": "#6B7480",
        "wave":     "#4C9AFF",
        "wave_hi":  "#E8A33D",
        "wave_cl":  "#E0574B",
        "fill":     "#16324F",
        "hold":     "#E8A33D",
        "bar":      "#2E6FD0",
        "bar_hi":   "#E0574B",
        "spec_pk":  "#E8A33D",
    },
    "light": {
        "bg":       "#EEF0F3",
        "panel":    "#FFFFFF",
        "panel2":   "#F4F6F8",
        "panel3":   "#E7EAEE",
        "border":   "#D8DCE2",
        "hair":     "#E6E9ED",
        "text":     "#1B2027",
        "muted":    "#5F6773",
        "dim":      "#98A0AC",
        "accent":   "#2563EB",
        "accent_fg": "#FFFFFF",
        "accent_d": "#1D4FBF",
        "ok":       "#12855B",
        "warn":     "#B45309",
        "bad":      "#C0392B",
        # 画布
        "scope":    "#FBFBFC",
        "grid":     "#E4E7EB",
        "axis":     "#C6CBD2",
        "scope_txt": "#8A929E",
        "wave":     "#2563EB",
        "wave_hi":  "#D97706",
        "wave_cl":  "#C0392B",
        "fill":     "#DCE9FB",
        "hold":     "#D97706",
        "bar":      "#6FA8F5",
        "bar_hi":   "#C0392B",
        "spec_pk":  "#D97706",
    },
}

_state = {"name": "dark"}
_registry = []          # [(widget, {attr: palette_key})]


def P(key=None):
    pal = PALETTES[_state["name"]]
    return pal if key is None else pal[key]


def name():
    return _state["name"]


def _paint(widget, roles):
    pal = P()
    kw = {}
    for attr, key in roles.items():
        if key in pal:
            kw[attr] = pal[key]
    if not kw:
        return
    try:
        widget.configure(**kw)
    except tk.TclError:
        pass


def bind(widget, **roles):
    """登记控件并立即上色。roles 形如 bg="panel", fg="text"。"""
    _registry.append((widget, roles))
    _paint(widget, roles)
    return widget


def style(root):
    """配置 ttk 控件样式（Combobox / Scrollbar / Progressbar 这些绕不开 ttk）。"""
    pal = P()
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    st.configure(".", background=pal["bg"], foreground=pal["text"], font=(FONT, 9))
    st.configure("TFrame", background=pal["bg"])
    st.configure("Panel.TFrame", background=pal["panel"])
    st.configure("TLabel", background=pal["bg"], foreground=pal["text"])
    st.configure("Panel.TLabel", background=pal["panel"], foreground=pal["text"])
    st.configure("Muted.Panel.TLabel", background=pal["panel"], foreground=pal["muted"])
    st.configure("Dim.Panel.TLabel", background=pal["panel"], foreground=pal["dim"])

    st.configure("TButton", background=pal["panel3"], foreground=pal["text"],
                 bordercolor=pal["border"], focuscolor=pal["accent"],
                 relief="flat", padding=(10, 4), font=(FONT, 9))
    st.map("TButton",
           background=[("pressed", pal["panel2"]), ("active", pal["panel2"])],
           foreground=[("disabled", pal["dim"])])

    st.configure("Accent.TButton", background=pal["accent"], foreground=pal["accent_fg"],
                 bordercolor=pal["accent"], relief="flat", padding=(12, 5),
                 font=(FONT, 9))
    st.map("Accent.TButton", background=[("active", pal["accent_d"])],
           foreground=[("disabled", pal["dim"])])

    st.configure("TCombobox",
                 fieldbackground=pal["panel2"], background=pal["panel2"],
                 foreground=pal["text"], arrowcolor=pal["muted"],
                 bordercolor=pal["border"], lightcolor=pal["panel2"],
                 darkcolor=pal["panel2"],
                 selectbackground=pal["panel2"], selectforeground=pal["text"],
                 padding=3)
    st.map("TCombobox",
           fieldbackground=[("readonly", pal["panel2"])],
           foreground=[("readonly", pal["text"])])

    st.configure("TEntry", fieldbackground=pal["panel2"], foreground=pal["text"],
                 bordercolor=pal["border"], lightcolor=pal["panel2"],
                 darkcolor=pal["panel2"], insertcolor=pal["text"], padding=3)

    st.configure("TProgressbar", troughcolor=pal["panel2"], background=pal["accent"],
                 bordercolor=pal["panel2"], lightcolor=pal["accent"],
                 darkcolor=pal["accent"], thickness=6)

    st.configure("Vertical.TScrollbar", background=pal["panel3"],
                 troughcolor=pal["panel"], bordercolor=pal["border"],
                 arrowcolor=pal["muted"], relief="flat")

    root.option_add("*TCombobox*Listbox.background", pal["panel2"])
    root.option_add("*TCombobox*Listbox.foreground", pal["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", pal["accent_fg"])
    root.option_add("*TCombobox*Listbox.font", (FONT, 9))


def apply(root):
    root.configure(bg=P("bg"))
    style(root)


def set_name(n):
    if n in PALETTES:
        _state["name"] = n


def current_palette():
    return _state["name"]


def repaint(root):
    """把登记表里的控件按当前配色重刷一遍。"""
    style(root)
    root.configure(bg=P("bg"))
    alive = []
    for w, roles in _registry:
        try:
            if w.winfo_exists():
                _paint(w, roles)
                alive.append((w, roles))
        except Exception:
            pass
    _registry[:] = alive


# ---------------------------------------------------------------- 复合控件


class Section(tk.Frame):
    """卡片式分区：1px 描边 + 左侧强调条 + 标题行。"""

    def __init__(self, master, title, hint="", accent="accent", **kw):
        super().__init__(master, bg=P("bg"), **kw)
        card = tk.Frame(self, bg=P("panel"),
                        highlightbackground=P("border"), highlightthickness=1, bd=0)
        card.pack(fill="both", expand=True)
        self._card = bind(card, bg="panel", highlightbackground="border")

        head = tk.Frame(card, bg=P("panel"))
        head.pack(fill="x", padx=10, pady=(7, 5))
        self._head = bind(head, bg="panel")

        bar = tk.Frame(head, bg=P(accent), width=3, height=13)
        bar.pack(side="left")
        bar.pack_propagate(False)
        self._bar = bind(bar, bg=accent)

        self._title = bind(tk.Label(head, text=title, bg=P("panel"), fg=P("text"),
                                    font=(FONT, 10, "bold")), bg="panel", fg="text")
        self._title.pack(side="left", padx=(8, 0))

        self._hint = None
        if hint:
            self._hint = bind(tk.Label(head, text=hint, bg=P("panel"), fg=P("dim"),
                                       font=(FONT, 8)), bg="panel", fg="dim")
            self._hint.pack(side="left", padx=(8, 0))

        self.body = tk.Frame(card, bg=P("panel"))
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        bind(self.body, bg="panel")


class Metric(tk.Frame):
    """指标卡：小字标签 + 等宽大数字。"""

    def __init__(self, master, label, width=104):
        super().__init__(master, bg=P("panel2"),
                         highlightbackground=P("hair"), highlightthickness=1, bd=0,
                         width=width, height=50)
        self.pack_propagate(False)
        bind(self, bg="panel2", highlightbackground="hair")
        cap = bind(tk.Label(self, text=label, bg=P("panel2"), fg=P("muted"),
                            font=(FONT, 8), anchor="w"), bg="panel2", fg="muted")
        cap.pack(fill="x", padx=8, pady=(6, 0))
        self.value = bind(tk.Label(self, text="--", bg=P("panel2"), fg=P("text"),
                                   font=(MONO, 13), anchor="w"), bg="panel2", fg="text")
        self.value.pack(fill="x", padx=8)


class Segmented(tk.Frame):
    """分段控件（三连按钮），替代单选圆点。"""

    def __init__(self, master, items, variable, command=None, seg_width=76):
        super().__init__(master, bg=P("panel"),
                         highlightbackground=P("border"), highlightthickness=1, bd=0)
        bind(self, bg="panel", highlightbackground="border")
        self.variable = variable
        self.command = command
        self.buttons = {}
        for idx, (key, text) in enumerate(items):
            b = tk.Label(self, text=text, bg=P("panel2"), fg=P("muted"),
                         font=(FONT, 9), width=0, padx=0, pady=5)
            b.configure(width=len(text) + 4)
            b.pack(side="left")
            if idx < len(items) - 1:
                sep = tk.Frame(self, bg=P("border"), width=1)
                sep.pack(side="left", fill="y")
                bind(sep, bg="border")
            b.bind("<Button-1>", lambda _e, k=key: self._pick(k))
            b.bind("<Enter>", lambda _e, w=b: self._hover(w, True))
            b.bind("<Leave>", lambda _e, w=b: self._hover(w, False))
            self.buttons[key] = b
        self.refresh()

    def _hover(self, w, on):
        if self.variable.get() != self._key_of(w):
            w.configure(bg=P("panel3") if on else P("panel2"))

    def _key_of(self, w):
        for k, b in self.buttons.items():
            if b is w:
                return k
        return None

    def _pick(self, key):
        self.variable.set(key)
        self.refresh()
        if self.command:
            self.command()

    def refresh(self):
        cur = self.variable.get()
        for k, b in self.buttons.items():
            if k == cur:
                b.configure(bg=P("accent"), fg=P("accent_fg"))
            else:
                b.configure(bg=P("panel2"), fg=P("muted"))


class Pill(tk.Label):
    """状态胶囊。tone: ok / warn / bad / accent / neutral"""

    def __init__(self, master, text, tone="neutral", bgkey=None):
        super().__init__(master, text=text, font=(FONT, 8), padx=8, pady=2)
        self._tone = tone
        self._bgkey = bgkey
        self._apply()

    def _apply(self):
        pal = P()
        tone_fg = {"ok": pal["ok"], "warn": pal["warn"], "bad": pal["bad"],
                   "accent": pal["accent"], "neutral": pal["muted"]}.get(self._tone, pal["muted"])
        bg = pal[self._bgkey] if self._bgkey else pal["panel2"]
        self.configure(bg=bg, fg=tone_fg)

    def set(self, text, tone=None):
        self.configure(text=text)
        if tone:
            self._tone = tone
        self._apply()
