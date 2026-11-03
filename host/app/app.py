#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py -- ZYNQ 以太网音频 PC 端桌面程序（tkinter）

功能
----
① 收听麦克风：收 50505 的 UDP 广播音频 -> 重组 10 ms 帧 -> 抖动缓冲 -> sounddevice 播放，
   同时显示帧率 / 丢帧 / 缓冲深度 / 欠载 / 溢出 / 实时电平。
② 发送 WAV：选一个 WAV（任意采样率/声道，自动转 48 kHz 16 bit 立体声），
   按 100 帧/秒推到 50506，板子从耳机口播放。
③ 发送测试音：440 Hz 正弦 10 s，同样走 50506。
④ 可视化（V1.2）：滚动 2 s 波形（每列 min/max）+ 64 段对数频谱（40 Hz~16 kHz，
   带峰值保持）+ 录音（把"正在播放的内容"写成 WAV）+ A/B 增益（1.0×/2.0×）。
⑤ 板子控制（真机协议）：旁路 / 轻量噪声门 / RNNoise 三档 + 底噪注入
   （0/300/800/1500）+ 440 Hz 自检音。报文是**单字节**发到 **50507**——
   udp_link.c 的 PORT_CTL。旧版的 b"CTL"+码 多字节帧发到 50506 是无效的
   （固件只认 b"AU" 音频头，会被丢掉），所以那一版按钮全都没反应。

V1.1 稳定性：目标抖动深度自适应（60~200 ms，每 10 s 最多 ±20 ms）、
丢帧/欠载时 5 ms 指数淡出 + 5 ms 淡入（不再硬切静音）、按到达速率估计
ppm 偏差并做每秒 ≤1 个样本的微调。

运行：python app.py
依赖：标准库 + numpy + sounddevice（协议/缓冲/DSP 逻辑都在 udp_audio.py）
"""

from __future__ import annotations

import argparse
import math
import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:            # 没装 sounddevice 也能开界面（只是不能出声）
    sd = None
    _SD_IMPORT_ERROR = exc
else:
    _SD_IMPORT_ERROR = None

import theme

from udp_audio import (
    BOARD_PORT, CHANNELS, FRAME_SAMPLES, SAMPLE_RATE,
    BoardCtl, BoardSender, JitterBuffer, MicReceiver, RingTap,
    board_bcast,
    SpectrumAnalyzer, WaveformView, WavRecorder, apply_gain, read_wav,
    tone, wav_info,
)

from ab_report import ABRunner

APP_TITLE = "ZYNQ 7020 以太网音频 · PC 端 v1.3"
# 底噪下拉：显示文本 -> 命令参数（固件只有 4 档，见 udp_link.c 的 'a'..'d'）
NOISE_LABELS = {"关": 0, "300": 300, "800": 800, "1500": 1500}
# v1.2 延迟档位：界面显示名 -> JitterBuffer.mode（顺序必须与 MODE_NAMES 一致）
MODE_CHOICES = (
    "auto 自适应 60~200 ms",
    "low 低延迟 45~90 ms",
    "stable 稳定 110~220 ms",
)
MODE_NAMES = ("auto", "low", "stable")
REC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")

STATS_TICK_MS = 150          # 统计刷新
VIZ_TICK_MS = 40             # 波形/频谱刷新（约 25 fps）
VIZ_SECONDS = 2.0            # 波形窗口 2 s

# ---- v1.3 配色（深色仪表盘风格；Canvas 用十六进制，ttk 用这些主色）
C_BG      = "#0f1419"        # 画布背景
C_GRID    = "#1b2532"        # 网格
C_AXIS    = "#2b3a4d"        # 中轴
C_WAVE    = "#3ddc84"        # 波形（正常电平）
C_WAVE_HI = "#ffb020"        # 波形（接近满幅）
C_WAVE_CL = "#e04040"        # 波形（削顶）
C_PEAK    = "#5b7cfa"        # 包络保持
C_BAR     = "#2f6fd0"        # 频谱柱
C_BAR_HI  = "#e04040"        # 频谱柱（近满幅）
C_SPEC_PK = "#ffb020"        # 频谱峰值线
C_TXT     = "#7f8c9b"        # 画布文字
C_PANEL   = "#eef2f6"        # ttk 面板底色
C_INK     = "#1b2733"        # 主文字
C_MUTED   = "#5c6b7a"        # 次要文字
C_OK      = "#1a7f4b"
C_ERR     = "#b00020"


class AudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1020x940")
        self.root.minsize(900, 820)

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.receiver: MicReceiver | None = None
        self.jitter: JitterBuffer | None = None
        self.stream = None
        self.stream_device: int | None = None
        self.pa_flags = 0
        self.cb_errors = 0

        self.sender: BoardSender | None = None
        self._send_thread: threading.Thread | None = None
        self._send_stop = threading.Event()
        self._sent_s = 0.0
        self._total_s = 0.0
        self._sending = False
        self._wav_path: str | None = None

        # V1.2 可视化 / 录音 / 增益
        self.tap = RingTap(seconds=4.0)
        self.spectrum = SpectrumAnalyzer()
        self.recorder = WavRecorder()
        self.gain = 1.0
        self._viz_t = time.monotonic()
        self._ab_thread = None
        # V1.3 波形重绘状态
        self._wf_size = None            # (w,h) 上次画网格时的尺寸
        self._sp_size = None            # 频谱网格重绘尺寸
        self._wf_peak = None            # 包络保持（每列 abs 峰值）
        self._wf_peak_t = 0.0

        # V2 控制面
        self._ctl: BoardCtl | None = None
        self._ctl_key: tuple | None = None

        self._build_ui()
        self._refresh_devices()
        self._log("就绪。板子 IP 169.254.191.50 / MAC 02:00:00:00:00:01，"
                  "PC 网卡 169.254.191.246；收发端口 50505 / 50506。")
        if sd is None:
            self._log("警告：sounddevice 不可用（%r），只能看统计，无法播放。" % (_SD_IMPORT_ERROR,))
        else:
            self._log("sounddevice %s / PortAudio %s" % (sd.__version__, sd.get_portaudio_version()[1]))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(STATS_TICK_MS, self._tick)
        self.root.after(VIZ_TICK_MS, self._draw_viz)

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        pal = theme.P()
        theme.apply(self.root)
        F, M = theme.FONT, theme.MONO
        self.root.geometry("1200x1010")
        self.root.minsize(1040, 780)

        outer = tk.Frame(self.root, bg=pal["bg"])
        outer.pack(fill="both", expand=True)
        theme.bind(outer, bg="bg")

        # ---------------- 顶栏
        head = tk.Frame(outer, bg=pal["panel"], height=46)
        head.pack(fill="x")
        head.pack_propagate(False)
        theme.bind(head, bg="panel")

        hl = tk.Frame(head, bg=pal["panel"])
        hl.pack(side="left", padx=14, pady=8)
        theme.bind(hl, bg="panel")
        dot = tk.Label(hl, text="●", bg=pal["panel"], fg=pal["accent"], font=(F, 10))
        dot.pack(side="left")
        theme.bind(dot, bg="panel", fg="accent")
        t = tk.Label(hl, text="ZYNQ 7020 · 端侧音频控制台", bg=pal["panel"],
                     fg=pal["text"], font=(F, 11, "bold"))
        t.pack(side="left", padx=(8, 0))
        theme.bind(t, bg="panel", fg="text")
        v = tk.Label(hl, text="v2.0", bg=pal["panel"], fg=pal["dim"], font=(F, 8))
        v.pack(side="left", padx=(8, 0))
        theme.bind(v, bg="panel", fg="dim")

        hr = tk.Frame(head, bg=pal["panel"])
        hr.pack(side="right", padx=12)
        theme.bind(hr, bg="panel")
        self.btn_theme = tk.Button(hr, text="浅色" if theme.name() == "dark" else "深色",
                                   command=self._toggle_theme, relief="flat", bd=0,
                                   bg=pal["panel3"], fg=pal["text"], font=(F, 8),
                                   activebackground=pal["panel2"], activeforeground=pal["text"],
                                   cursor="hand2", padx=10, pady=3)
        self.btn_theme.pack(side="right", padx=(8, 0))
        theme.bind(self.btn_theme, bg="panel3", fg="text",
                   activebackground="panel2", activeforeground="text")
        self.pill_net = theme.Pill(hr, "169.254.255.255", "accent", "panel2")
        self.pill_net.configure(font=(M, 8))
        self.pill_net.pack(side="right", padx=4)
        self.pill_state = theme.Pill(hr, "就绪", "neutral", "panel2")
        self.pill_state.pack(side="right", padx=4)

        tk.Frame(outer, bg=pal["border"], height=1).pack(fill="x")

        body = tk.Frame(outer, bg=pal["bg"])
        body.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        theme.bind(body, bg="bg")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        # ---------------- ① 收听（整行）
        s1 = theme.Section(body, "收听板载麦克风", "UDP 50505 → 本机声卡")
        s1.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        r0 = tk.Frame(s1.body, bg=pal["panel"])
        r0.pack(fill="x")
        theme.bind(r0, bg="panel")
        lb = tk.Label(r0, text="输出声卡", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        lb.pack(side="left")
        theme.bind(lb, bg="panel", fg="muted")
        self.dev_cb = ttk.Combobox(r0, width=42, state="readonly")
        self.dev_cb.pack(side="left", padx=(8, 6))
        ttk.Button(r0, text="刷新", width=5, command=self._refresh_devices).pack(side="left")
        self.btn_rx = ttk.Button(r0, text="开始收听", width=11, style="Accent.TButton",
                                 command=self._toggle_rx)
        self.btn_rx.pack(side="left", padx=8)
        self.rx_state = tk.Label(r0, text="未开始", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        self.rx_state.pack(side="left")
        theme.bind(self.rx_state, bg="panel", fg="muted")

        lb = tk.Label(r0, text="延迟档位", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        lb.pack(side="left", padx=(18, 0))
        theme.bind(lb, bg="panel", fg="muted")
        self.mode_cb = ttk.Combobox(r0, width=20, state="readonly", values=list(MODE_CHOICES))
        self.mode_cb.current(0)
        self.mode_cb.pack(side="left", padx=(6, 0))
        self.mode_cb.bind("<<ComboboxSelected>>", self._on_mode)
        self.pitch_lab = tk.Label(r0, text="基音 --", bg=pal["panel"], fg=pal["dim"], font=(M, 8))
        self.pitch_lab.pack(side="left", padx=10)
        theme.bind(self.pitch_lab, bg="panel", fg="dim")

        # 指标卡 + 电平，左右两块
        low = tk.Frame(s1.body, bg=pal["panel"])
        low.pack(fill="x", pady=(9, 0))
        theme.bind(low, bg="panel")

        mrow = tk.Frame(low, bg=pal["panel"])
        mrow.pack(side="left")
        theme.bind(mrow, bg="panel")
        self.stat_labels = {}
        for key, title in (("fps", "帧率 FPS"), ("lost", "丢帧"),
                           ("depth", "缓冲 ms"), ("under", "欠载")):
            card = theme.Metric(mrow, title)
            card.pack(side="left", padx=(0, 8))
            self.stat_labels[key] = card.value

        lvl = tk.Frame(low, bg=pal["panel"])
        lvl.pack(side="left", padx=(14, 0))
        theme.bind(lvl, bg="panel")
        lb = tk.Label(lvl, text="电平", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        lb.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
        theme.bind(lb, bg="panel", fg="muted")
        self.bar_rms = ttk.Progressbar(lvl, maximum=100, length=170)
        self.bar_rms.grid(row=0, column=1, sticky="w", pady=2)
        self.bar_db = ttk.Progressbar(lvl, maximum=100, length=170)
        self.bar_db.grid(row=1, column=1, sticky="w", pady=2)
        self.lvl_text = tk.Label(lvl, text="--", bg=pal["panel"], fg=pal["text"], font=(M, 8))
        self.lvl_text.grid(row=0, column=2, rowspan=2, sticky="w", padx=10)
        theme.bind(self.lvl_text, bg="panel", fg="text")

        # 其余统计：紧凑两行
        det = tk.Frame(s1.body, bg=pal["panel"])
        det.pack(fill="x", pady=(7, 0))
        theme.bind(det, bg="panel")
        fields = [("frames", "已收帧"), ("over", "溢出"), ("dups", "重复/乱序"),
                  ("bad", "非法包"), ("target", "目标/抖动"), ("drift", "漂移修正"),
                  ("conc", "隐藏帧")]
        for i, (key, title) in enumerate(fields):
            cell = tk.Frame(det, bg=pal["panel"])
            cell.grid(row=i // 6, column=i % 6, sticky="w", padx=(0, 16), pady=1)
            theme.bind(cell, bg="panel")
            k = tk.Label(cell, text=title, bg=pal["panel"], fg=pal["dim"], font=(F, 8))
            k.pack(side="left")
            theme.bind(k, bg="panel", fg="dim")
            lab = tk.Label(cell, text="--", bg=pal["panel"], fg=pal["text"], font=(M, 8))
            lab.pack(side="left", padx=(4, 0))
            theme.bind(lab, bg="panel", fg="text")
            self.stat_labels[key] = lab

        r3 = tk.Frame(s1.body, bg=pal["panel"])
        r3.pack(fill="x", pady=(8, 0))
        theme.bind(r3, bg="panel")
        self.btn_rec = ttk.Button(r3, text="开始录音", width=10, command=self._toggle_record)
        self.btn_rec.pack(side="left")
        self.btn_gain = ttk.Button(r3, text="增益 A/B：1.0×", width=14, command=self._toggle_gain)
        self.btn_gain.pack(side="left", padx=6)
        self.lbl_rec = tk.Label(r3, text="录音未开始（录的是正在播放的内容）",
                                bg=pal["panel"], fg=pal["dim"], font=(F, 8))
        self.lbl_rec.pack(side="left", padx=8)
        theme.bind(self.lbl_rec, bg="panel", fg="dim")

        # ---------------- 中段：左波形/频谱，右推送 + 控制
        mid = tk.Frame(body, bg=pal["bg"])
        mid.grid(row=1, column=0, sticky="nsew")
        theme.bind(mid, bg="bg")
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)
        mid.rowconfigure(0, weight=1)

        s2 = theme.Section(mid, "波形 · 频谱", "实时 25 fps")
        s2.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        viz = tk.Frame(s2.body, bg=pal["panel"])
        viz.pack(fill="both", expand=True)
        theme.bind(viz, bg="panel")
        viz.columnconfigure(0, weight=1)
        viz.rowconfigure(0, weight=1)
        viz.rowconfigure(1, weight=1)
        self.cv_wave = tk.Canvas(viz, width=600, height=96, background=pal["scope"],
                                 highlightthickness=0, bd=0)
        self.cv_wave.grid(row=0, column=0, sticky="nsew")
        c1 = tk.Label(viz, text="波形 2 s · 每列 min/max · 橙线=包络保持",
                      bg=pal["panel"], fg=pal["dim"], font=(F, 8), anchor="w")
        c1.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        theme.bind(c1, bg="panel", fg="dim")
        self.cv_spec = tk.Canvas(viz, width=600, height=76, background=pal["scope"],
                                 highlightthickness=0, bd=0)
        self.cv_spec.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        c2 = tk.Label(viz, text="频谱 64 段 40 Hz~16 kHz · 橙线=峰值",
                      bg=pal["panel"], fg=pal["dim"], font=(F, 8), anchor="w")
        c2.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        theme.bind(c2, bg="panel", fg="dim")
        viz.rowconfigure(2, weight=1)
        self._create_viz_items()

        rcol = tk.Frame(mid, bg=pal["bg"])
        rcol.grid(row=0, column=1, sticky="nsew")
        theme.bind(rcol, bg="bg")

        # ---------------- ③ 推送
        s3 = theme.Section(rcol, "推送网络音源", "UDP 50506", accent="ok")
        s3.pack(fill="x", pady=(0, 8))

        r4 = tk.Frame(s3.body, bg=pal["panel"])
        r4.pack(fill="x")
        theme.bind(r4, bg="panel")
        k = tk.Label(r4, text="地址", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="muted")
        self.ent_dest = ttk.Entry(r4, width=16)
        self.ent_dest.insert(0, board_bcast())
        self.ent_dest.pack(side="left", padx=(6, 8))
        k = tk.Label(r4, text="端口", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="muted")
        self.ent_port = ttk.Entry(r4, width=6)
        self.ent_port.insert(0, str(BOARD_PORT))
        self.ent_port.pack(side="left", padx=(6, 8))
        k = tk.Label(r4, text="网卡", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="muted")
        self.ent_bind = ttk.Entry(r4, width=13)
        self.ent_bind.pack(side="left", padx=6)

        r5 = tk.Frame(s3.body, bg=pal["panel"])
        r5.pack(fill="x", pady=(7, 0))
        theme.bind(r5, bg="panel")
        ttk.Button(r5, text="选择 WAV", width=9, command=self._pick_wav).pack(side="left")
        ttk.Button(r5, text="发送 WAV", width=9, command=self._send_wav).pack(side="left", padx=5)
        ttk.Button(r5, text="测试音 440Hz/10s", width=16,
                   command=self._send_tone).pack(side="left", padx=5)
        self.btn_send_stop = ttk.Button(r5, text="停止", width=6,
                                        command=self._stop_send, state="disabled")
        self.btn_send_stop.pack(side="left", padx=5)

        self.lbl_wav = tk.Label(s3.body, text="未选择文件（自动转 48k/16bit/立体声）",
                                bg=pal["panel"], fg=pal["dim"], font=(F, 8))
        self.lbl_wav.pack(fill="x", pady=(6, 0))
        theme.bind(self.lbl_wav, bg="panel", fg="dim")

        prow = tk.Frame(s3.body, bg=pal["panel"])
        prow.pack(fill="x", pady=(6, 0))
        theme.bind(prow, bg="panel")
        self.bar_send = ttk.Progressbar(prow, maximum=100, length=300)
        self.bar_send.pack(side="left", fill="x", expand=True)
        self.lbl_send = tk.Label(prow, text="未发送", bg=pal["panel"], fg=pal["muted"],
                                 font=(M, 8))
        self.lbl_send.pack(side="left", padx=8)
        theme.bind(self.lbl_send, bg="panel", fg="muted")

        # ---------------- ④ 板子控制
        s4 = theme.Section(rcol, "板子控制", "UDP 50507 · 单字节", accent="warn")
        s4.pack(fill="both", expand=True)

        r6 = tk.Frame(s4.body, bg=pal["panel"])
        r6.pack(fill="x")
        theme.bind(r6, bg="panel")
        k = tk.Label(r6, text="降噪档位", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(anchor="w")
        theme.bind(k, bg="panel", fg="muted")
        self.var_dsp = tk.StringVar(value="gate")
        self.seg_dsp = theme.Segmented(
            r6, (("bypass", "旁路"), ("gate", "轻量门"), ("rnnoise", "RNNoise")),
            self.var_dsp, command=self._send_dsp)
        self.seg_dsp.pack(anchor="w", pady=(4, 0))

        r6b = tk.Frame(s4.body, bg=pal["panel"])
        r6b.pack(fill="x", pady=(8, 0))
        theme.bind(r6b, bg="panel")
        k = tk.Label(r6b, text="底噪注入", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="muted")
        self.cb_noise = ttk.Combobox(r6b, width=6, state="readonly",
                                     values=list(NOISE_LABELS.keys()))
        self.cb_noise.current(0)
        self.cb_noise.pack(side="left", padx=6)
        self.cb_noise.bind("<<ComboboxSelected>>", self._send_noise)
        ttk.Button(r6b, text="自检音 440Hz", width=13,
                   command=self._send_tone).pack(side="left", padx=6)
        k = tk.Label(r6b, text="翻转，别连按", bg=pal["panel"], fg=pal["dim"], font=(F, 8))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="dim")

        # ---- 时长缩放（方案 B 的可调版）：50%..100%，100% = 恒等直通。
        # 语义是"保留全部内容、把内容放慢"，不是丢帧。所以它**只在 RNNoise
        # 模式下有可听效果** —— 固件把 ts_frame() 挂在 mode 1 的输出路径上，
        # 旁路和轻量门不走它。在旁路下拖这个滑块听不出变化是正常的。
        r6c = tk.Frame(s4.body, bg=pal["panel"])
        r6c.pack(fill="x", pady=(8, 0))
        theme.bind(r6c, bg="panel")
        k = tk.Label(r6c, text="时长缩放", bg=pal["panel"], fg=pal["muted"], font=(F, 9))
        k.pack(side="left")
        theme.bind(k, bg="panel", fg="muted")
        self.var_ts = tk.IntVar(value=100)
        self.sc_ts = tk.Scale(
            r6c, from_=100, to=50, orient="horizontal", length=130, width=10,
            showvalue=False, resolution=5, bd=0, highlightthickness=0,
            bg=pal["panel"], fg=pal["text"], troughcolor=pal["panel3"],
            activebackground=pal["accent"], sliderrelief="flat",
            command=self._on_ts_drag)
        self.sc_ts.set(100)
        self.sc_ts.pack(side="left", padx=6)
        theme.bind(self.sc_ts, bg="panel", fg="text", troughcolor="panel3")
        self.lbl_tsv = tk.Label(r6c, text="100%", width=13, anchor="w",
                                bg=pal["panel"], fg=pal["text"], font=(M, 9))
        self.lbl_tsv.pack(side="left")
        theme.bind(self.lbl_tsv, bg="panel", fg="text")
        ttk.Button(r6c, text="复位", width=5,
                   command=lambda: self.sc_ts.set(100)).pack(side="left", padx=(2, 0))

        self.lbl_ctl = tk.Label(s4.body, text="尚未发送 · 串口会回显 dsp= / noise=",
                                bg=pal["panel"], fg=pal["muted"], font=(M, 8), anchor="w")
        self.lbl_ctl.pack(fill="x", pady=(8, 0))
        theme.bind(self.lbl_ctl, bg="panel", fg="muted")
        self.btn_ab = ttk.Button(s4.body, text="一键 A/B 对比 · 自动出报告（约 30 s）",
                                 style="Accent.TButton", command=self._start_ab)
        self.btn_ab.pack(anchor="w", pady=(8, 0))
        self.lbl_ab = tk.Label(s4.body, text="三档各测 8 s，生成 markdown 表格 + 三段 WAV",
                               bg=pal["panel"], fg=pal["dim"], font=(F, 8), anchor="w")
        self.lbl_ab.pack(fill="x", pady=(4, 0))
        theme.bind(self.lbl_ab, bg="panel", fg="dim")

        # ---------------- ⑤ 日志（底部整行，固定高）
        s5 = theme.Section(body, "日志", "协议/时序问题先看这里", accent="muted")
        s5.grid(row=2, column=0, sticky="ew", pady=(8, 6))
        logw = tk.Frame(s5.body, bg=pal["panel"])
        logw.pack(fill="both", expand=True)
        theme.bind(logw, bg="panel")
        self.txt = tk.Text(logw, height=5, wrap="none", state="disabled",
                           font=(M, 9), bg=pal["scope"], fg=pal["text"],
                           insertbackground=pal["text"], relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=pal["hair"])
        theme.bind(self.txt, bg="scope", fg="text", insertbackground="text",
                   highlightbackground="hair")
        sb = ttk.Scrollbar(logw, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(4, 0))

        # ---------------- 底栏
        tk.Frame(outer, bg=pal["border"], height=1).pack(fill="x")
        foot = tk.Frame(outer, bg=pal["panel"], height=24)
        foot.pack(fill="x")
        foot.pack_propagate(False)
        theme.bind(foot, bg="panel")
        self.status = tk.Label(foot, text="提示：先刷新选对声卡，再开始收听。",
                               bg=pal["panel"], fg=pal["muted"], font=(F, 8), anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=12)
        theme.bind(self.status, bg="panel", fg="muted")

        # 固件上电默认旁路(0)，而界面默认勾「轻量门」——不同步的话界面在说谎。
        self.root.after(600, self._sync_board_on_start)

    # ------------------------------------------------------------ 主题
    def _sync_palette(self) -> None:
        """把主题色同步到 _draw_viz_once 使用的模块级常量。"""
        global C_BG, C_GRID, C_AXIS, C_WAVE, C_WAVE_HI, C_WAVE_CL, C_PEAK
        global C_BAR, C_BAR_HI, C_SPEC_PK, C_TXT
        p = theme.P()
        C_BG = p["scope"]
        C_GRID = p["grid"]
        C_AXIS = p["axis"]
        C_WAVE = p["wave"]
        C_WAVE_HI = p["wave_hi"]
        C_WAVE_CL = p["wave_cl"]
        C_PEAK = p["hold"]
        C_BAR = p["bar"]
        C_BAR_HI = p["bar_hi"]
        C_SPEC_PK = p["spec_pk"]
        C_TXT = p["scope_txt"]

    def _create_viz_items(self) -> None:
        """（重）建画布图元。主题切换时整体重建，避免逐项改颜色的碎片逻辑。"""
        p = theme.P()
        self.cv_wave.configure(bg=p["scope"])
        self.cv_spec.configure(bg=p["scope"])
        self.cv_wave.delete("all")
        self.cv_spec.delete("all")
        self.wf_fill = self.cv_wave.create_polygon(
            0, 70, 1, 70, 1, 71, fill=p["fill"], outline="")
        self.wf_mid = self.cv_wave.create_line(0, 70, 600, 70, fill=p["axis"])
        self.wf_line = self.cv_wave.create_line(0, 0, 1, 1, fill=p["wave"], width=1)
        self.wf_hold = self.cv_wave.create_line(*([0.0] * 4), fill=p["hold"], width=1)
        self.sp_bars = [
            self.cv_spec.create_rectangle(0, 140, 1, 140, fill=p["bar"], outline="")
            for _ in range(self.spectrum.bands)
        ]
        self.sp_peak = self.cv_spec.create_line(
            *([0.0] * (2 * self.spectrum.bands)), fill=p["spec_pk"], width=1)
        self._wf_size = None
        self._sp_size = None

    def _toggle_theme(self) -> None:
        nxt = "light" if theme.name() == "dark" else "dark"
        theme.set_name(nxt)
        theme.repaint(self.root)
        self._sync_palette()
        self._create_viz_items()
        try:
            self.seg_dsp.refresh()
            self.pill_state._apply()
            self.pill_net._apply()
        except Exception:
            pass
        self.btn_theme.configure(
            text="浅色" if nxt == "dark" else "深色",
            bg=theme.P("panel3"), fg=theme.P("text"))
        self._log("主题切换为 %s" % nxt)

    # ------------------------------------------------------------ 日志
    def _log(self, msg: str) -> None:
        self.log_q.put(time.strftime("[%H:%M:%S] ") + str(msg))

    def _drain_log(self) -> None:
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if not lines:
            return
        self.txt.configure(state="normal")
        for ln in lines[-200:]:
            self.txt.insert("end", ln + "\n")
        # 只保留最近 800 行
        total = int(self.txt.index("end-1c").split(".")[0])
        if total > 800:
            self.txt.delete("1.0", "%d.0" % (total - 800))
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ------------------------------------------------------------ 声卡
    def _refresh_devices(self) -> None:
        self._devices = []
        if sd is None:
            self.dev_cb["values"] = ["（sounddevice 不可用）"]
            self.dev_cb.current(0)
            return
        try:
            items = []
            for idx, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) >= 2:
                    items.append((idx, "%d: %s" % (idx, d["name"])))
            self._devices = items
            self.dev_cb["values"] = [t for _, t in items]
            if not items:
                self.dev_cb["values"] = ["（没有可用的双声道输出设备）"]
                self.dev_cb.current(0)
                return
            default_out = sd.default.device[1]
            sel = 0
            for i, (idx, _) in enumerate(items):
                if idx == default_out:
                    sel = i
                    break
            self.dev_cb.current(sel)
            self._log("检测到 %d 个双声道输出设备。" % len(items))
        except Exception as exc:
            self._log("枚举声卡失败：%r" % (exc,))

    def _selected_device(self) -> int | None:
        i = self.dev_cb.current()
        if not self._devices or i < 0 or i >= len(self._devices):
            return None
        return self._devices[i][0]

    # ------------------------------------------------------------ 接收
    def _toggle_rx(self) -> None:
        if self.receiver is not None:
            self._stop_rx()
        else:
            self._start_rx()

    def _start_rx(self) -> None:
        name = MODE_NAMES[max(0, min(len(MODE_NAMES) - 1, self.mode_cb.current()))]
        jitter = JitterBuffer(mode=name)
        jitter.prefill_to(max(60.0, jitter.target_ms))   # 先垫一层，避免第一块就欠载
        rx = MicReceiver(jitter=jitter)
        try:
            rx.start()
        except OSError as exc:
            self._log("启动接收失败：%s" % exc)
            messagebox.showerror("启动接收失败", str(exc))
            return
        self.receiver = rx
        self.jitter = jitter
        self._open_stream()
        self.btn_rx.configure(text="停止收听")
        self.rx_state.configure(text="收听中", foreground=theme.P("ok"))
        self.pill_state.set("收听中", "ok")
        self._log("开始收听 UDP 50505（档位 %s，目标 %.0f~%.0f ms，块 480 样本）。"
                  % (jitter.mode, jitter.min_ms, jitter.max_ms))

    def _on_mode(self, _evt=None) -> None:
        """切换延迟档位：auto / low / stable，运行中即时生效（深度平滑过渡）。"""
        idx = self.mode_cb.current()
        name = MODE_NAMES[max(0, min(len(MODE_NAMES) - 1, idx))]
        rx = self.receiver
        if rx is None:
            self._log("延迟档位先记下：%s（点“开始收听”后生效）" % name)
            return
        real = rx.set_mode(name)
        st = rx.snapshot()
        self._log("延迟档位 -> %s（目标 %.0f~%.0f ms，当前目标 %.0f ms，"
                  "深度 %.0f ms）" % (real, st["mode_min_ms"], st["mode_max_ms"],
                                      st["target_ms"], st["depth_ms"]))

    def _open_stream(self) -> None:
        if sd is None:
            self._log("没有 sounddevice，跳过播放（只统计）。")
            return
        dev = self._selected_device()
        last_exc = None
        for latency in ("low", None):
            try:
                kw = dict(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                          blocksize=FRAME_SAMPLES, callback=self._audio_cb)
                if dev is not None:
                    kw["device"] = dev
                if latency is not None:
                    kw["latency"] = latency
                self.stream = sd.RawOutputStream(**kw)
                self.stream.start()
                self.stream_device = dev
                self._log("播放流已打开（device=%s，latency=%s，480 样本/块）。"
                          % (dev, latency or "default"))
                return
            except Exception as exc:
                last_exc = exc
        self.stream = None
        self._log("打开播放流失败：%r（仍然会收包并显示统计）" % (last_exc,))
        messagebox.showwarning("无法播放",
                               "打开输出声卡失败：\n%r\n\n收流统计仍然可用，"
                               "可换一个声卡或关掉其他占用声卡的程序。" % (last_exc,))

    def _audio_cb(self, outdata, frames, time_info, status) -> None:
        """PortAudio 回调：只做取数据/写环形缓冲，绝不碰 tkinter。

        播放链：JitterBuffer -> (A/B 增益) -> 声卡，同时喂给波形环形缓冲与录音器，
        所以"录音"录的是真正放出来的内容（含增益与补静音）。
        """
        try:
            if status:
                self.pa_flags += 1
            jb = self.jitter
            block = None
            if jb is None:
                outdata.fill(0)
            else:
                block = jb.pull(frames)
                g = self.gain
                if g != 1.0:
                    block = apply_gain(block, g)
                outdata[:] = block
            if block is not None:
                tap = self.tap
                if tap is not None:
                    tap.write(block)
                rec = self.recorder
                if rec is not None and rec.recording:
                    rec.feed(block)
        except Exception:
            self.cb_errors += 1
            try:
                outdata.fill(0)
            except Exception:
                pass

    def _stop_rx(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None
        self.jitter = None
        self.btn_rx.configure(text="开始收听")
        self.rx_state.configure(text="已停止", foreground=theme.P("muted"))
        self.pill_state.set("已停止", "neutral")
        for lab in self.stat_labels.values():
            lab.configure(text="--")
        self.bar_rms["value"] = 0
        self.bar_db["value"] = 0
        self.lvl_text.configure(text="--")
        self._log("已停止收听。")

    # ------------------------------------------------------------ 发送
    def _pick_wav(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要发送到板子的 WAV",
            filetypes=[("WAV 音频", "*.wav *.WAV"), ("所有文件", "*.*")])
        if not path:
            return
        self._wav_path = path
        try:
            info = wav_info(path)
            self.lbl_wav.configure(
                text="%s ｜ %d Hz / %d ch / %d bit / %.1f s%s"
                     % (os.path.basename(path), info["rate"], info["channels"],
                        info["sampwidth"] * 8, info["duration"],
                        "" if info["rate"] == SAMPLE_RATE and info["channels"] == 2
                        else "（发送时自动转换）"))
            self._log("选择 WAV：%s（%d Hz/%d ch/%.2f s）"
                      % (path, info["rate"], info["channels"], info["duration"]))
        except Exception as exc:
            self.lbl_wav.configure(text=path)
            self._log("读取 WAV 头失败：%r" % (exc,))

    def _send_wav(self) -> None:
        if self._sending:
            self._log("正在发送中，先停止。")
            return
        if not self._wav_path:
            self._pick_wav()
            if not self._wav_path:
                return
        path = self._wav_path
        self._log("加载 WAV：%s" % path)
        try:
            audio, info = read_wav(path)
        except Exception as exc:
            self._log("解码失败：%r" % (exc,))
            messagebox.showerror("WAV 读取失败", "%r" % (exc,))
            return
        conv = []
        if info["resampled"]:
            conv.append("%d→48000 Hz" % info["rate"])
        if info["mono_to_stereo"]:
            conv.append("单声道→立体声")
        if info["trimmed_channels"]:
            conv.append("多声道→前两路")
        self._log("转码完成：%d 样本 / %.2f s%s"
                  % (info["frames_out"], info["duration"],
                     ("（" + "，".join(conv) + "）") if conv else "（无需转换）"))
        self._start_send(audio, "%d 帧 / %.1f s" % (audio.shape[0] // FRAME_SAMPLES, info["duration"]))

    def _send_tone(self) -> None:
        if self._sending:
            self._log("正在发送中，先停止。")
            return
        audio = tone(440.0, 10.0, 0.25)
        self._log("生成 440 Hz 测试音 10 s（%d 帧）。" % (audio.shape[0] // FRAME_SAMPLES))
        self._start_send(audio, "440 Hz 测试音 10 s")

    def _start_send(self, audio, label: str) -> None:
        try:
            dest = self.ent_dest.get().strip() or board_bcast()
            port = int(self.ent_port.get().strip() or BOARD_PORT)
            bind_ip = self.ent_bind.get().strip() or None
        except ValueError:
            messagebox.showerror("参数错误", "端口必须是数字。")
            return

        self._send_stop = threading.Event()
        self._sent_s = 0.0
        self._total_s = float(audio.shape[0]) / SAMPLE_RATE
        self._sending = True
        self.btn_send_stop.configure(state="normal")
        self.bar_send["value"] = 0
        self.lbl_send.configure(text="发送中 0.0 s / %.1f s" % self._total_s)
        self._log("开始发送 %s → %s:%d%s（预热 2 帧，之后 100 帧/秒）"
                  % (label, dest, port, ("，绑定 " + bind_ip) if bind_ip else ""))
        self.status.configure(text="发送中…")

        def worker():
            sender = BoardSender(dest=dest, port=port, bind_ip=bind_ip)
            try:
                res = sender.play_array(
                    audio, stop_event=self._send_stop,
                    progress=self._on_send_progress)
                self._log("发送结束：%d 帧 / %.2f s，错误 %d，重排 %d%s"
                          % (res["frames"], res["seconds"], res["errors"], res["resyncs"],
                             "（手动停止）" if res.get("stopped") else ""))
            except Exception as exc:
                self._log("发送异常：%r" % (exc,))
                self._log(traceback.format_exc(limit=2))
            finally:
                sender.close()
                self._sending = False

        self._send_thread = threading.Thread(target=worker, name="BoardSender", daemon=True)
        self._send_thread.start()

    def _on_send_progress(self, sent_s: float, total_s: float) -> None:
        # 从发送线程调用：只写普通浮点，由 _tick 读，避免跨线程碰 tkinter
        self._sent_s = sent_s
        self._total_s = total_s

    def _stop_send(self) -> None:
        if self._sending:
            self._send_stop.set()
            self._log("请求停止发送…")

    # ------------------------------------------------------------ 可视化
    def _draw_viz(self) -> None:
        """主线程 ~25 fps 定时器：从环形缓冲取数据画波形/频谱，与音频回调解耦。"""
        try:
            self._sync_palette()
            self._draw_viz_once()
        except Exception:
            self._log("绘图异常：\n" + traceback.format_exc(limit=2))
        finally:
            self.root.after(VIZ_TICK_MS, self._draw_viz)

    def _draw_viz_once(self) -> None:
        tap = self.tap
        if tap is None:
            return
        samples = tap.read_last(int(VIZ_SECONDS * SAMPLE_RATE))

        # ---- 波形：每列 min/max → 填充多边形 + 包络线（一次 coords 提交，不闪烁）
        w = max(160, int(self.cv_wave.winfo_width()))
        h = max(80, int(self.cv_wave.winfo_height()))
        mid = h / 2.0
        if self._wf_size != (w, h):
            self._wf_size = (w, h)
            self.cv_wave.delete("grid")
            for i in range(1, 8):                       # 8 等分 = 每格 0.25 s
                self.cv_wave.create_line(w * i / 8.0, 0, w * i / 8.0, h,
                                         fill=C_GRID, tags="grid")
            for frac in (0.25, 0.75):                   # ±半幅
                self.cv_wave.create_line(0, h * frac, w, h * frac,
                                         fill=C_GRID, tags="grid")
            self.cv_wave.create_text(6, 6, text="满幅", anchor="nw",
                                     fill=C_TXT, tags="grid")
            self.cv_wave.create_text(6, h - 16, text="−满幅", anchor="nw",
                                     fill=C_TXT, tags="grid")
            for i, lab in enumerate(("−2 s", "−1.5", "−1 s", "−0.5", "")):
                if lab:
                    self.cv_wave.create_text(w * i / 4.0, h - 4, text=lab,
                                             anchor="sw", fill=C_TXT, tags="grid")
            self.cv_wave.create_text(w - 6, h - 4, text="现在", anchor="se",
                                     fill=C_TXT, tags="grid")
            self.cv_wave.tag_lower("grid")
        self.cv_wave.coords(self.wf_mid, 0, mid, w, mid)

        now0 = time.monotonic()
        dt0 = min(0.5, max(0.005, now0 - self._viz_t))
        mins, maxs = WaveformView.columns(samples, w)
        n = int(mins.shape[0])
        if n:
            scale = (h / 2.0 - 8.0) / 32768.0
            step = w / float(n)
            top = [mid - float(maxs[i]) * scale for i in range(n)]
            bot = [mid - float(mins[i]) * scale for i in range(n)]
            xs = [i * step + step * 0.5 for i in range(n)]

            poly = []
            for i in range(n):
                poly += [xs[i], top[i]]
            for i in range(n - 1, -1, -1):
                poly += [xs[i], bot[i]]
            self.cv_wave.coords(self.wf_fill, *poly)

            line = []
            for i in range(n):
                line += [xs[i], top[i], xs[i], bot[i]]
            self.cv_wave.coords(self.wf_line, *line)

            # 电平着色：正常绿 / 接近满幅橙 / 削顶红
            pk = float(max(np.max(np.abs(maxs)), abs(np.min(mins))))
            if pk >= 32600:
                col, fill = C_WAVE_CL, "#331515"
            elif pk >= 30000:
                col, fill = C_WAVE_HI, "#33270f"
            else:
                col, fill = C_WAVE, "#12301f"
            self.cv_wave.itemconfigure(self.wf_line, fill=col)
            self.cv_wave.itemconfigure(self.wf_fill, fill=fill)

            # 包络保持：每列取 |max|,|min| 的较大者，0.5 s 指数衰减
            cur = np.maximum(np.abs(maxs.astype(np.int32)),
                             np.abs(mins.astype(np.int32))).astype(np.float64)
            if self._wf_peak is None or self._wf_peak.shape != cur.shape:
                self._wf_peak = cur
            else:
                decay = math.exp(-dt0 / 0.5)
                self._wf_peak = np.maximum(self._wf_peak * decay, cur)
            hold = []
            for i in range(n):
                y = mid - float(self._wf_peak[i]) * scale
                hold += [xs[i], max(1.0, min(h - 1.0, y))]
            self.cv_wave.coords(self.wf_hold, *hold)

        # ---- 频谱：峰值保持按实际经过时间衰减
        now = time.monotonic()
        dt = min(0.5, max(0.005, now - self._viz_t))
        self._viz_t = now
        nfft = min(2048, int(samples.shape[0]))
        chunk = samples[len(samples) - nfft:] if nfft else samples
        db, peaks = self.spectrum.analyze(chunk, dt)

        sw = max(160, int(self.cv_spec.winfo_width()))
        sh = max(80, int(self.cv_spec.winfo_height()))
        if self._sp_size != (sw, sh):
            self._sp_size = (sw, sh)
            self.cv_spec.delete("grid")
            for dbv in (-20.0, -40.0, -60.0):           # 频率轴的水平参考线
                y = sh - (dbv / -90.0) * (sh - 4.0)
                self.cv_spec.create_line(0, y, sw, y, fill=C_GRID, tags="grid")
                self.cv_spec.create_text(sw - 4, y - 1, text="%d" % int(dbv),
                                         anchor="se", fill=C_TXT, tags="grid")
            for f, lab in ((100, "100"), (1000, "1k"), (10000, "10k")):
                x = sw * (math.log10(f) - math.log10(40.0)) / (math.log10(16000.0) - math.log10(40.0))
                self.cv_spec.create_line(x, 0, x, sh, fill=C_GRID, tags="grid")
                self.cv_spec.create_text(x, sh - 2, text=lab, anchor="s",
                                         fill=C_TXT, tags="grid")
            self.cv_spec.tag_lower("grid")
        bands = int(db.shape[0])
        bw = sw / float(bands)
        floor = -90.0
        for i in range(bands):
            frac = min(1.0, max(0.0, (float(db[i]) - floor) / -floor))
            top = sh - frac * (sh - 4.0)
            self.cv_spec.coords(self.sp_bars[i], i * bw + 0.5, top,
                                (i + 1) * bw - 0.5, sh)
            self.cv_spec.itemconfigure(
                self.sp_bars[i], fill=C_BAR_HI if frac >= 0.85 else C_BAR)
        pcoords = []
        for i in range(bands):
            frac = min(1.0, max(0.0, (float(peaks[i]) - floor) / -floor))
            pcoords.append((i + 0.5) * bw)
            pcoords.append(sh - frac * (sh - 4.0))
        if len(pcoords) >= 4:
            self.cv_spec.coords(self.sp_peak, *pcoords)

    # ------------------------------------------------------------ 录音 / 增益
    def _toggle_record(self) -> None:
        rec = self.recorder
        if rec.recording:
            info = rec.stop()
            self.btn_rec.configure(text="开始录音")
            if info.get("error"):
                self.lbl_rec.configure(text="录音出错：%s" % info["error"],
                                       foreground=theme.P("bad"))
                self._log("录音出错：%s" % info["error"])
            else:
                self.lbl_rec.configure(
                    text="已保存 %s（%.1f s）"
                         % (os.path.basename(info["path"] or ""), info["seconds"]),
                    foreground="#087f23")
                self._log("录音已保存：%s（%d 帧 / %.2f s / %d 字节）"
                          % (info["path"], info["frames"], info["seconds"], info["bytes"]))
            return

        if self.stream is None:
            self._log("提示：现在没有打开播放流，录到的会是静音。")
        try:
            os.makedirs(REC_DIR, exist_ok=True)
        except OSError as exc:
            self._log("建录音目录失败：%r" % (exc,))
        path = os.path.join(REC_DIR, time.strftime("mic_%Y%m%d_%H%M%S.wav"))
        try:
            got = rec.start(path)
        except Exception as exc:
            self._log("开始录音失败：%r" % (exc,))
            messagebox.showerror("开始录音失败", "%r" % (exc,))
            return
        self.btn_rec.configure(text="停止录音")
        self.lbl_rec.configure(text="录音中 0.0 s（%s）" % os.path.basename(got),
                               foreground=theme.P("warn"))
        self._log("开始录音 → %s" % got)

    def _toggle_gain(self) -> None:
        self.gain = 2.0 if self.gain == 1.0 else 1.0
        self.btn_gain.configure(text="增益 A/B：%.1f×" % self.gain)
        self._log("播放增益切到 %.1f×（用于现场演示“能听出差别”）" % self.gain)

    # ------------------------------------------------------------ 板子控制
    def _board_ctl(self) -> BoardCtl:
        """固件控制面：固定 50507、单字节。

        目标地址留空（= 界面上的默认值）时交给 BoardCtl 自己挑：
        会同时发到 169.254.255.255 和 255.255.255.255 —— 前者才是真能到板子的那个
        （板子 IP 169.254.191.50，受限广播在有多块网卡的机器上会走错网卡）。
        手动填了地址就只发那个地址。
        """
        raw = self.ent_dest.get().strip()
        dest = None if (not raw or raw == board_bcast()) else raw
        bind_ip = self.ent_bind.get().strip() or None
        key = (dest, bind_ip)
        if self._ctl is None or self._ctl_key != key:
            if self._ctl is not None:
                try:
                    self._ctl.close()
                except Exception:
                    pass
            self._ctl = BoardCtl(dest=dest, bind_ip=bind_ip)
            self._ctl_key = key
        return self._ctl

    def _ctl_done(self, info: dict) -> None:
        self.lbl_ctl.configure(
            text="%s → %s（'%s'）发往 %s"
                 % (info["cmd"], info["hex"], info["char"], info.get("dest", "?")),
            foreground="#087f23")
        self._log("板子控制 %s：%d 字节 %s → %s"
                  % (info["cmd"], info["bytes"], info["hex"], info.get("dest", "?")))
        self.status.configure(text="已发 %s，板子串口会回显一行确认" % info["cmd"])

    def _send_dsp(self) -> None:
        try:
            self._ctl_done(self._board_ctl().dsp(self.var_dsp.get()))
        except Exception as exc:
            self.lbl_ctl.configure(text="发送失败", foreground=theme.P("bad"))
            self._log("切换降噪档失败：%r" % (exc,))

    def _send_noise(self, _evt=None) -> None:
        # 下拉里「关」是中文，早期版本直接 int() 它 → ValueError，选「关」必失败
        level = NOISE_LABELS.get(self.cb_noise.get(), 0)
        try:
            self._ctl_done(self._board_ctl().noise(level))
        except Exception as exc:
            self.lbl_ctl.configure(text="发送失败", foreground=theme.P("bad"))
            self._log("设置底噪失败：%r" % (exc,))

    def _send_tone(self) -> None:
        try:
            self._ctl_done(self._board_ctl().tone())
        except Exception as exc:
            self.lbl_ctl.configure(text="发送失败", foreground=theme.P("bad"))
            self._log("自检音失败：%r" % (exc,))

    # ---------------- 时长缩放（方案 B 的可调版）
    #
    # tk.Scale 的 command 在**拖动过程中持续触发**（每移动一档就一次）。
    # 控制报文是 UDP 广播，板子会收到 2~3 份，再叠上拖动产生的几十条命令，
    # 就变成了刷屏 —— 而板子每条都要走一遍 ts_reset()，声音会一路顿。
    # 所以这里做 320 ms 防抖：拖动只更新标签，停下来才真正下发一次。
    #
    # 这条路是绝对值和幂等：重复发同一个 pct，固件 ts_parse_rate() 里
    # `pct == g_ts_rate` 直接 return，不会重置状态、不会重复计数。
    def _on_ts_drag(self, val) -> None:
        try:
            v = int(round(float(val)))
        except (TypeError, ValueError):
            return
        if v >= 100:
            self.lbl_tsv.configure(text="100%", foreground=theme.P("text"))
        else:
            self.lbl_tsv.configure(text="%d%% · %.2f×" % (v, 100.0 / v),
                                   foreground=theme.P("warn"))
        job = getattr(self, "_ts_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)     # AudioApp 不是 tk.Tk 的子类，
            except Exception:                    # after/after_cancel 都在 self.root 上
                pass
        self._ts_job = self.root.after(320, self._send_ts)

    def _send_ts(self) -> None:
        self._ts_job = None
        pct = int(self.sc_ts.get())
        try:
            self._ctl_done(self._board_ctl().ts(pct))
            if pct < 100:
                self._log("时长缩放 %d%% —— 只有 RNNoise 档有可听效果（旁路/轻量门不走这个路径）" % pct)
        except Exception as exc:
            self.lbl_ctl.configure(text="发送失败", foreground=theme.P("bad"))
            self._log("设置时长缩放失败：%r" % (exc,))

    def _sync_board_on_start(self) -> None:
        """启动时把板子拉到界面勾选的档位。

        固件上电默认是旁路 `'0'`，而界面默认勾「轻量门」；不同步的话，
        界面上写着"轻量门"、板子其实在旁路，演示时一按别的按钮才发现对不上。
        """
        try:
            self._ctl_done(self._board_ctl().dsp(self.var_dsp.get()))
            self._log("启动同步：板子已切到界面档位「%s」" % self.var_dsp.get())
        except Exception as exc:
            self._log("启动同步档位失败：%r" % (exc,))

    # ------------------------------------------------------------ 一键 A/B
    def _rx_counters(self) -> dict:
        """给 ABRunner 用的计数器快照（只读整数，线程安全）。"""
        rx = self.receiver
        if rx is None:
            return {}
        s = rx.snapshot()
        return {"frames": s.get("frames", 0), "lost": s.get("lost", 0),
                "bad": s.get("bad_packets", 0),
                "under": self.jitter.underloads if self.jitter else 0,
                "over": self.jitter.overflows if self.jitter else 0}

    def _start_ab(self) -> None:
        if self.receiver is None:
            messagebox.showinfo("先开始收听",
                                "A/B 需要先点「开始收听」——\n它要靠收到的音频算噪声底和抑制量。")
            return
        if self._ab_thread is not None and self._ab_thread.is_alive():
            self._log("A/B 正在跑，等这一轮结束。")
            return

        # 控制对象必须在这里（主线程）建好：BoardCtl 会读界面上的目标地址输入框
        ctl = self._board_ctl()
        self.btn_ab.configure(state="disabled")
        self.lbl_ab.configure(text="测量中…（旁路 → 轻量门 → RNNoise）", foreground=theme.P("warn"))

        def worker():
            try:
                runner = ABRunner(send_mode=lambda key: ctl.dsp(key),
                                  tap=self.tap,
                                  stats_fn=self._rx_counters,
                                  out_dir=REC_DIR,
                                  log=self._log)
                path = runner.run()
                self.root.after(0, lambda: self._ab_done(path))
            except Exception as exc:
                self._log("A/B 失败：%r" % (exc,))
                self._log(traceback.format_exc(limit=3))
                self.root.after(0, self._ab_reset)

        self._ab_thread = threading.Thread(target=worker, name="ABRunner", daemon=True)
        self._ab_thread.start()

    def _ab_done(self, path: str) -> None:
        self.btn_ab.configure(state="normal")
        self.lbl_ab.configure(text="报告：%s" % os.path.basename(path), foreground=theme.P("ok"))
        self.status.configure(text="A/B 完成，报告与三段 WAV 已存到 recordings/")

    def _ab_reset(self) -> None:
        self.btn_ab.configure(state="normal")
        self.lbl_ab.configure(text="上一轮失败，看日志", foreground=theme.P("bad"))

    # ------------------------------------------------------------ 定时刷新
    def _tick(self) -> None:
        try:
            self._drain_log()
            self._update_rx_stats()
            self._update_send_stats()
            self._update_rec_status()
        except Exception:
            self._log("界面刷新异常：\n" + traceback.format_exc(limit=2))
        finally:
            self.root.after(STATS_TICK_MS, self._tick)

    def _update_rec_status(self) -> None:
        rec = self.recorder
        if rec is not None and rec.recording:
            self.lbl_rec.configure(
                text="录音中 %.1f s（%s）"
                     % (rec.seconds, os.path.basename(rec.path or "")),
                foreground=theme.P("warn"))

    def _update_rx_stats(self) -> None:
        rx = self.receiver
        if rx is None:
            return
        s = rx.snapshot()
        fps = s["fps"]
        self.stat_labels["fps"].configure(
            text="%.1f fps" % fps,
            foreground="#087f23" if 95.0 <= fps <= 105.0 or s["frames"] < 40 else "#b35c00")
        self.stat_labels["frames"].configure(text=str(s["frames"]))
        self.stat_labels["lost"].configure(
            text=str(s["lost"]),
            foreground=theme.P("bad") if s["lost"] else "#087f23")
        self.stat_labels["depth"].configure(
            text="%.0f ms" % s["depth_ms"],
            foreground=theme.P("warn") if s["depth_ms"] > s["trim_high_ms"] or (0 < s["depth_ms"] < 30)
            else "#087f23")
        self.stat_labels["under"].configure(text="%d" % s["underloads"])
        self.stat_labels["over"].configure(text="%d / 修剪 %d" % (s["overflows"], s["trims"]))
        self.stat_labels["dups"].configure(text="%d / %d" % (s["dups"], s["late"]))
        self.stat_labels["bad"].configure(text=str(s["bad_packets"]))
        self.stat_labels["target"].configure(
            text="%.0f / %.1f ms" % (s["target_ms"], s["jitter_ms"]))
        self.stat_labels["drift"].configure(
            text="%+.0f ppm / %d" % (s["drift_ppm"], s["corrections"]))
        self.stat_labels["conc"].configure(text="%d" % s["concealed"])
        # v1.2：基音与外推次数（丢包隐藏是否真的走了"延续基频"）
        if s.get("pitch_hz"):
            self.pitch_lab.configure(
                text="基音 %.0f Hz ｜ 外推 %d" % (s["pitch_hz"], s["extrapolated"]),
                foreground="#2a6fdb")
        else:
            self.pitch_lab.configure(
                text="基音 -- ｜ 外推 %d" % s["extrapolated"], foreground="#666")

        rms, dbfs = s["rms"], s["dbfs"]
        self.bar_rms["value"] = min(100.0, rms * 400.0)          # 0.25 满量程线性显示
        self.bar_db["value"] = max(0.0, min(100.0, (dbfs + 60.0) / 60.0 * 100.0))
        self.lvl_text.configure(text="RMS %.4f ｜ %.1f dBFS ｜ 峰值 %d/32767"
                                     % (rms, dbfs, s["peak"]))
        if s["sources"]:
            self.rx_state.configure(
                text="收听中 · %s%s" % (",".join(s["sources"]),
                                        "" if s["last_age"] is None or s["last_age"] < 0.5
                                        else "（%.1fs 无包）" % s["last_age"]),
                foreground="#087f23" if (s["last_age"] is not None and s["last_age"] < 0.5)
                else "#b35c00")

    def _update_send_stats(self) -> None:
        if not self._sending:
            if self.btn_send_stop["state"] == "normal":
                self.btn_send_stop.configure(state="disabled")
                self.bar_send["value"] = 100 if self._total_s and self._sent_s >= self._total_s else self.bar_send["value"]
                self.lbl_send.configure(text="已停止（最后 %.1f s）" % self._sent_s)
                self.status.configure(text="提示：先点“刷新”选对声卡，再“开始收听”。")
            return
        total = self._total_s if self._total_s > 0 else 1.0
        pct = max(0.0, min(100.0, self._sent_s / total * 100.0))
        self.bar_send["value"] = pct
        self.lbl_send.configure(text="已发送 %.1f s / %.1f s" % (self._sent_s, self._total_s))

    # ------------------------------------------------------------ 退出
    def _on_close(self) -> None:
        try:
            self._stop_send()
            if self._send_thread and self._send_thread.is_alive():
                self._send_thread.join(timeout=0.5)
            self._stop_rx()
        except Exception:
            pass
        try:
            if self.recorder is not None and self.recorder.recording:
                info = self.recorder.stop()
                self._log("退出前保存录音：%s" % (info.get("path"),))
        except Exception:
            pass
        try:
            if self._ctl is not None:
                self._ctl.close()
                self._ctl = None
        except Exception:
            pass
        self.root.destroy()


def _enable_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _list_devices() -> None:
    if sd is None:
        print("sounddevice 不可用：%r" % (_SD_IMPORT_ERROR,))
        return
    try:
        print(sd.query_devices())
        print("默认输入/输出：", sd.default.device)
    except Exception as exc:
        print("枚举失败：%r" % (exc,))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ZYNQ 以太网音频 PC 端")
    ap.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    args = ap.parse_args(argv)
    if args.list_devices:
        _list_devices()
        return 0
    _enable_dpi()
    root = tk.Tk()
    AudioApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
