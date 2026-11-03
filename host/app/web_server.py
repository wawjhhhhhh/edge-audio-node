"""Web 版控制台后端。

为什么不用 Electron：Electron 要打包整个 Chromium（100MB+），而我们已经有 Python
后端。这里只用标准库起本地 HTTP 服务，界面用 Edge/Chrome 的 `--app` 模式打开，
得到的是没有地址栏的独立窗口，零额外运行时。

复用：协议 / 抖动缓冲 / 频谱全部来自 udp_audio.py，本文件只做「状态聚合 + 命令分发」。
"""
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 目录布局兼容 ------------------------------------------------------
# 原始工程里语音助手在 work/xiaozhi/，交付包里在 host/app/xiaozhi/。
# 这里把 host/app/xiaozhi 注册成 work.xiaozhi 的别名，两种布局都能 import。
try:
    import types as _types

    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
    import xiaozhi as _xz_pkg  # noqa: F401

    if "work" not in sys.modules:
        _work = _types.ModuleType("work")
        _work.__path__ = [_APP_DIR]
        sys.modules["work"] = _work
    sys.modules.setdefault("work.xiaozhi", _xz_pkg)
except Exception:  # 拿不到就按原样走，真的缺包时下面的 import 会给出明确报错
    pass

import numpy as np

from udp_audio import (
    BOARD_PORT, SAMPLE_RATE, BoardCtl, JitterBuffer, MicReceiver,
    RingTap, SpectrumAnalyzer,
)
from pc_enhance_v2 import Engine as EnhanceEngine   # 2026-11-02 换引擎，见 pc_enhance_v2.py 顶部说明
from vocoder import VocoderStream
from spectrum import Analyzer as MeasAnalyzer
from loudness import LoudnessMeter
from dac_cal import DacCal
from loopback import LoopbackPusher
from playfx import PlayFX, PRESET_TEXT as PlayFX_PRESET_TEXT   # 包2 音色包：EQ+压缩+限幅

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# 打包成 EXE 时打开：页面关掉（不再有心跳）就退出，避免留下占端口的僵尸进程。
EXIT_ON_IDLE = False
IDLE_LIMIT = 45.0
HOST, PORT = "0.0.0.0", 8770

GATE_MS = 0.127          # 实测（材料库/04-实测记录/2026-10-27_轻量噪声门上板…）
RNN_MS = 20.07           # 实测，超 10 ms 帧预算 -> 49 fps
# 五档 A/B 的顺序与显示名（键同时也是固件的模式名，enh 是 PC 侧虚拟档）
AB_MODES = (("bypass", "旁路"), ("gate", "轻量门"), ("pl", "PL 谱减"),
            ("rnnoise", "RNNoise"), ("enh", "PC 增强"))
AB_LABEL = dict(AB_MODES)

DSP_KEY = {"bypass": "0", "rnnoise": "1", "gate": "2", "pl": "3"}
DSP_NAME = {"0": "旁路", "1": "RNNoise", "2": "轻量门", "3": "PL 谱减"}

# ---- 包6：双 DAC 输出模式 --------------------------------------------------
# 固件把这三个字节翻成 AXI GPIO 第二通道（0x41210000）的低两位，
# PL 里的 dac_split 再按 LRC 门控两条 I2S 数据线：
#   0 M0 两口同源 / 1 M1 板载只左、外接只右 / 2 M2 板载静音、外接完整
# 注意：板子的 12 字节状态包里没有这个字段（协议是定长的），
# 所以这里显示的是"本机下发的意图"，要核对真值看串口状态行的 dm=。
DAC_KEY = {0: b"m", 1: b"o", 2: b"p"}
DAC_NAME = {0: "M0 同源", 1: "M1 左右分离", 2: "M2 外接独占",
            3: "未生效（这块 bit 里没有 dac_split）"}

# ---- 板子上行状态（固件 send_status，每 250 ms 一包）------------------------
BOARD_STATUS_PORT = 50508
_MODE_BY_CODE = {0: "bypass", 1: "rnnoise", 2: "gate", 3: "pl"}


def parse_status(data):
    """把 50508 上的一帧解析成状态字典；格式不对返回 None。

    帧 = 以太网/IP/UDP 头 42 字节 + 8 字节 'A''S' 头 + 载荷，
    载荷 v2（15 字节，2026-11-02 起）：
        'S', ver=2, mode, ts_rate, ts_apply, dly_frames, lat_off,
        gain_hi, gain_lo, rn_target, tone, noise_hi, noise_lo, dac_mode, xor
    载荷 v1（12 字节，老固件）仍兼容：读前 11 字节、XOR 对 p[11]。
    校验不过一律丢包 —— 宁可界面显示"板子状态未知"，也不显示错位状态。
    """
    if not data or data[:2] != b"AS":
        return None
    p = data[8:] if len(data) >= 20 else data
    if len(p) < 12 or p[0:1] != b"S":
        return None
    ver = p[1]
    if ver >= 2:
        if len(p) < 15:
            return None
        body = 14
    else:
        body = 11
    x = 0
    for b in p[:body]:
        x ^= b
    if x != p[body]:
        return None                      # 校验不过就丢掉，绝不显示半截状态
    mode = _MODE_BY_CODE.get(p[2], "?")
    out = {
        "raw": data.hex(),
        "ver": ver,
        "mode": mode,
        "mode_code": p[2],
        "dsp_name": DSP_NAME.get(str(p[2]), "—"),
        "ts_rate": p[3],
        "ts_apply": bool(p[4]),
        "dly_frames": p[5],
        "lat_off": bool(p[6]),
        "gain": (p[7] << 8) | p[8],
        "rn_target": p[9],
        "tone": bool(p[10]),
        "rx_time": time.strftime("%H:%M:%S"),
    }
    if ver >= 2:
        out["noise"] = (p[11] << 8) | p[12]
        out["dac_mode"] = p[13] & 0x03
        out["dac_name"] = DAC_NAME.get(out["dac_mode"], "—")
    else:
        # 老固件没有这两项：显式给 None，界面据此显示"—"而不是 0
        out["noise"] = None
        out["dac_mode"] = None
        out["dac_name"] = None
    return out



class Hub:
    def __init__(self):
        # RLock 而非 Lock：start_rx/stop_rx 在持锁期间会调用 note()，
        # 而 note() 也要锁 —— 用普通 Lock 会在第一次点「开始收听」时自死锁。
        self.lock = threading.RLock()
        self.log = deque(maxlen=200)
        self.status = "就绪"
        # 固件上电默认是旁路（main.c 里 g_dsp_mode = 0）。以前这里写死 "gate"，
        # 于是面板上「轻量门」高亮、板子却在跑旁路 —— 显示和后端各说各话。
        # 现在默认值跟固件一致，真正的选择由界面在启动时下发（见 /api/state）。
        self.dsp = "bypass"
        self.dac_mode = 0          # 包6：双 DAC 输出模式（0=M0 同源，默认）
        self.device = None

        self.rx = None
        self.jitter = None
        self.tap = RingTap(seconds=2.0)
        # overview 那 64 段柱状图用的老分析仪（带 analyze(mono, dt)）
        self.spec = SpectrumAnalyzer()
        self._puller = None
        self._stop = threading.Event()
        self._frames = 0
        self._t0 = time.time()
        self._last_spec = time.time()
        self._fps = None
        # ---- 链路真值：fps 必须来自"到达的帧"，不能来自 pull 循环 ----
        #
        # 以前 fps = _frames / el，而 _frames 是 _pull_loop 每次 pull(480) 就 +1，
        # 且 JitterBuffer.pull() 在完全没数据时也返回长度 480 的静音块 ——
        # 于是板子断电 12 分钟，接口照样报 100.0 fps / 丢包 0。
        # 证据见 材料库/01-开发日志/2026-10-30_接手自检_板子断电与控制台假fps.md
        # 现在只认两条：rx.fps()（收到帧的时间窗）与 rx.last_rx（最后一个包多久以前）。
        self._under_hist = deque()      # (monotonic, underloads)，2.5 s 滑窗
        self.noise = 0
        self.tone_on = False
        self.rn_target = 30
        # 时长缩放（方案 B 的可调版）。100 = 恒等直通，与固件 TS_RATE_DEF 一致。
        # 这个值只在这里记录"我发过什么"，板子真正是多少要靠 /api/state 的
        # ts_rejects 或串口日志交叉确认 —— 控制面是 UDP，发出去不等于被接受。
        self.ts = 100
        self.pushing = False
        # 浏览器播放用的滚动缓冲：key=累计样本号，value=该块 int16 (n,2)
        self.pcm = deque(maxlen=600)      # 600 块 × 480 点 = 6 s
        self.pcm_total = 0
        self.seen = False
        self.last_beat = time.time()
        # 无板子演示：本地合成 440 Hz + 噪声，走同一条 tap/pcm 管线
        self.sim = False
        self._sim_thread = None
        self._sim_stop = threading.Event()
        self._push_stop = threading.Event()
        self._push_thread = None
        # ---- PC 增强模式（并联支路，默认关闭）
        #
        # 与 dsp 三档是**正交**的两件事：
        #   dsp   = 板子怎么处理上行音频（旁路 / RNNoise / 轻量门）
        #   enh   = PC 拿到音频后要不要再叠一层自己的谱减
        # 所以 enh 不改任何固件命令，也不参与 DSP_KEY 映射；关掉它，
        # 全部行为与之前逐样点一致（process() 旁路时返回原对象）。
        self.enh = EnhanceEngine(strength=50, protect=50)
        self.enh_on = False
        # ---- 声码器（并联支路，默认关闭）----
        #
        # 与 enh 位置相同（jb.pull 之后、tap/pcm 之前），但做的是完全不同的事：
        # enh 是"再叠一层谱减"，声码器是**参数再生** —— 每 20 ms 只保留
        # 16 个反射系数 + 基频 + 增益（144 bit，7.2 kbps），再用声道模型把声音
        # 重新造出来。噪声没有稳定的基频/共振峰，进不了这几十个参数，所以原理上
        # 被丢掉；代价是 7 kbps 级别的"机器人腔"（详见 vocoder.py 文件头）。
        # 固件侧同一套算法在 work/eth_audio/sw/vocoder.h（QEMU 自测 PASS），
        # 这里先让 PC 端能直接听、直接对比，板级接入留到明天。
        self.voc = VocoderStream()
        # ---- 五档 A/B 的常驻结果（每档一行，重跑同一档就替换那一行）----
        #
        # 以前这里是"一键 A/B"：切三档、录完打一张表，然后表就留在服务端没人看。
        # 用户要的是"每种情况各录一份、结果常驻可比"，所以改成：
        #   rows 按 AB_MODES 的顺序排，每档一条 dict（含波形包络），页面直接画。
        # ---- 语音助手（懒加载；没点过就不 import llm/tools，也不去连板子）----
        self.chat_log = []          # [{role, text, t, ms, steps}]
        self.chat_busy = False
        self.chat_voice = False     # True = 回答同时念到板子喇叭上
        self.chat_board_ip = None   # 板子喇叭地址，第一次用到时确认（必要时自动扫网段）
        self.chat_board_ok = False
        self._xz = None
        self._xz_voice = None       # 上次构建时的 voice 设置（变了就重建）
        self.ab_rows = {}
        self.ab_running = False
        self.ab_last = 0.0
        self.voc_on = False
        self._voc_timer = None
        # ---- 频谱分析仪（B 方案：板载频谱仪 / 声学测量仪）----
        #
        # 只读旁路：喂的是"声码器之后、写 tap/pcm 之前"的同一块数据，
        # 不改变任何样点，关掉只是不再刷新 /api/spectrum。
        # 72 条对数频带 + 96 行瀑布 + 电平/信噪比/THD 读数，见 spectrum.py。
        # 注意：名字不能叫 spec —— overview 的 64 段柱状图还要用 udp_audio 里
        # 那个老 SpectrumAnalyzer（带 analyze(mono, dt)），两个混用会让 /api/state 崩。
        self.meas = MeasAnalyzer()
        # ---- 板子自己的状态（50508 'A''S' 状态包，固件每 250 ms 发一次）----
        # 以前面板上的模式/比例/增益全是"我发出去的最后一个命令"，板子到底
        # 有没有接受、有没有被别人改掉，界面一概不知 —— 于是出现"小智说切好了，
        # 面板也显示降噪，实际板子还在旁路"。现在有这条上行，界面显示板子的
        # 真实状态；本机命令只记在 *_intent 里（见 snapshot）。
        self.board = None            # 最近一次解析成功的状态字典
        self._board_t = 0.0          # 收到它的 monotonic 时间
        # ---- 响度计（BS.1770-4）----
        # 给"客观指标卡"和"等响 A/B"用。喂的是**已经过 enh/voc 之后**的那一路，
        # 也就是用户耳朵听到的东西 —— 指标必须和听感对齐，不能只报板的原始流。
        self.loud = LoudnessMeter()
        self._loud_by_mode = {}      # 每个模式最近一次的响度，供"等响 A/B"用
        # ---- 播放侧音效（包2）----
        # 只作用在"推给板子的那一路"（50506），与收听链（enh/voc/meas/loud）完全
        # 分开：收听链是"从板子拿声音"，音效链是"往板子送声音"，两条路互不影响。
        self.fx = PlayFX()
        self.fx.enabled = False     # 与 fx_on 保持一致
        self.loop = None            # 系统声音环回推流
        self.fx_on = False
        self._push_opts = {}         # 本次推流的来源（tone / file）
        self._board_thr = threading.Thread(target=self._board_listener, daemon=True)
        self._board_thr.start()
        self.note("服务已启动。板子 IP 169.254.191.50，上行 50505 / 下行 50506 / 控制 50507 / 状态 50508")

    # ---------------------------------------------------------------- 日志
    def note(self, msg):
        with self.lock:
            self.log.append({"t": time.strftime("%H:%M:%S"), "m": str(msg)})

    # ------------------------------------------------------ 板子状态（50508）
    def _board_listener(self):
        """收固件每 250 ms 发一次的状态包，存成 self.board。

        这条路是单向的、只读的：PC 端不改板子，只是把板子自己的说法抄下来。
        所以面板上显示的模式不会再和板子不一致 —— 除非板子真的没在跑
        （那时 board.online=False，界面显示"板子状态未知"）。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", BOARD_STATUS_PORT))
        except OSError as exc:
            self.note("状态口 %d 打不开（%r）——界面将无法显示板子真实状态"
                      % (BOARD_STATUS_PORT, exc))
            return
        s.settimeout(0.5)
        while True:
            try:
                data, _addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                continue
            st = parse_status(data)
            if st is None:
                continue
            with self.lock:
                self.board = st
                self._board_t = time.monotonic()

    def board_state(self, max_age=1.2):
        """最近一次板子状态；超过 max_age 没更新就标 online=False。"""
        with self.lock:
            if self.board is None:
                return None
            st = dict(self.board)
            age = time.monotonic() - self._board_t
            st["age"] = round(age, 2)
            st["online"] = age <= max_age
            return st

    # ---------------------------------------------------------------- 收听
    def start_rx(self):
        with self.lock:
            if self.rx is not None:
                return
            try:
                self.jitter = JitterBuffer(mode="auto")
                self.rx = MicReceiver(jitter=self.jitter)
                self.rx.start()
                # 每轮收听都从干净状态开始：声码器的预加重/合成器历史留着上一轮的
                # 尾巴，第一帧会"咔"一下。
                self.voc.reset()
                self.meas.reset()
                # 响度计同理：上一轮的 integral 值会污染这一轮的指标卡
                self.loud.reset()
                self._frames = 0
                self._t0 = time.time()
                self._under_hist.clear()
                self.pcm.clear()
                self.pcm_total = 0
                self._stop.clear()
                self._puller = threading.Thread(target=self._pull_loop, daemon=True)
                self._puller.start()
                self.status = "收听中 · 板子 100 fps 上行；浏览器可直接播放"
                self.note("开始接收 UDP 50505")
            except Exception as exc:
                self.rx = None
                self.note("启动接收失败：%r" % (exc,))

    def stop_rx(self):
        with self.lock:
            # 「停止收听」必须把模拟信号一起停掉。
            # 2026-11-02 实测 bug：模拟在跑时点「停止收听」，listening 由
            # `self.rx is not None or self.sim` 决定，sim 还亮着 → 界面永远显示
            # "收听中"，用户看到的就是"按了停不下来"。
            if self.sim:
                try:
                    self._sim_stop.set()
                except Exception:
                    pass
                self.sim = False
                self.note("停止模拟（随停止收听）")
            if self.rx is None:
                return
            self._stop.set()
            try:
                self.rx.stop()
            except Exception:
                pass
            self.rx = None
            self.jitter = None
            self._under_hist.clear()
            # 频谱仪跟着一起清：停止收听之后如果还留着上一轮的谱，
            # 界面上就是个"板子还在出声"的假象（这一条已被 test_spectrum_api 抓到）。
            self.meas.reset()
            self.loud.reset()
            self.status = "已停止"
            self.note("已停止接收")

    def _pull_loop(self):
        """按 100 fps 抽干抖动缓冲（原先是音频回调干的活；浏览器播放接上后改由回调驱动）。"""
        period = 1.0 / 100.0
        next_t = time.perf_counter()
        while not self._stop.is_set():
            jb = self.jitter
            if jb is None:
                break
            try:
                blk = jb.pull(480)
            except Exception:
                blk = None
            if blk is not None and len(blk):
                # PC 增强：位置刻意选在 jb.pull 之后、写任何下游之前。
                # 这样 tap（波形/频谱）、pcm（浏览器播放）、_frames 三处的口径
                # 完全一致 —— 开关增强前后，样本号与时间轴都不变，
                # 浏览器 from= 续取不会因为多/少几个样点而错位。
                blk = self.enh.process(blk)
                # 声码器接在增强之后、任何下游之前（与 enh 同位置，口径一致）。
                # 打开时每块多花 ~2 ms（Python 复刻实测 2.15 ms/帧 = 20 ms 预算的 1/9），
                # 关着时一行都不执行。
                if self.voc_on:
                    blk = self.voc.process(blk)
                # 频谱仪只读旁路（不拷贝、不改样点）
                self.meas.feed(blk)
                # 响度计也要看"用户实际听到的那一路"，所以放在 enh/voc 之后、
                # 写盘之前。它同样只读，不改样点。
                self.loud.feed(blk)
                self.tap.write(blk)
                with self.lock:
                    self.pcm.append((self.pcm_total, blk))
                    self.pcm_total += int(len(blk))
                self._frames += 1
            else:
                time.sleep(0.002)
            next_t += period
            dt = next_t - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            else:
                next_t = time.perf_counter()

    # ---------------------------------------------------------------- 推流
    def start_push(self, secs=30.0, src="tone", path=None):
        # 上一段还在推：先打断它再开新的（原来直接回"已在推流"，导致 A/B 永远听到同一段）
        interrupted = False
        if self.pushing:
            self._push_stop.set()
            old_thr = getattr(self, "_push_thread", None)
            if old_thr is not None and old_thr.is_alive():
                old_thr.join(timeout=1.5)
            self.pushing = False
            interrupted = True
        with self.lock:
            self._push_stop.clear()
            self.pushing = True
            self._push_opts = {"src": src, "path": path}
        self._push_thread = threading.Thread(
            target=self._push_loop, args=(float(secs),), daemon=True)
        self._push_thread.start()
        return {"ack": "开始推流（打断上一段）" if interrupted else "开始推流"}

    def stop_push(self):
        self._push_stop.set()
        return {"ack": "正在停止"}

    @staticmethod
    def _load_wav_stereo(path, want_rate=SAMPLE_RATE):
        """读 WAV → (n, 2) int16 @48 kHz。单声道复制成双声道，其它采样率线性重采样。"""
        import wave as _wave
        with _wave.open(path, "rb") as w:
            ch, sw, rate, n = (w.getnchannels(), w.getsampwidth(),
                               w.getframerate(), w.getnframes())
            raw = w.readframes(n)
        if sw == 2:
            x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        elif sw == 4:
            x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
        elif sw == 1:
            x = (np.frombuffer(raw, dtype="<u1").astype(np.float64) - 128.0) / 128.0
        else:
            raise ValueError("不支持的位深 %d" % sw)
        if ch > 1:
            x = x.reshape(-1, ch)
            if ch == 2:
                st = x
            else:
                st = np.repeat(x.mean(axis=1, keepdims=True), 2, axis=1)
        else:
            st = np.repeat(x.reshape(-1, 1), 2, axis=1)
        if rate != want_rate:
            t_out = np.arange(int(len(st) * want_rate / rate)) / want_rate
            t_in = np.arange(len(st)) / rate
            st = np.stack([np.interp(t_out, t_in, st[:, c]) for c in range(2)], axis=1)
        return np.clip(st * 32767.0, -32768, 32767).astype(np.int16)

    def _apply_fx(self, arr):
        """把整段音频过一遍音效链（480 点一块，与实时口径一致）。"""
        self.fx.enabled = bool(self.fx_on)     # 引擎自己的开关跟着 hub 走
        if not self.fx_on:
            return arr
        self.fx.reset()
        out = np.empty_like(arr)
        n = len(arr)
        for i in range(0, n, 480):
            seg = arr[i:i + 480]
            out[i:i + 480] = self.fx.process(seg)
        return out

    def _push_loop(self, secs):
        """把音源推给板子。默认 440 Hz 正弦（1 秒 440 个整周期，循环无接缝）；
        也可以推 WAV 文件，先过音效链再发 —— 这是"播放侧功能"的演示通路。"""
        try:
            from udp_audio import BoardSender, tone
            snd = BoardSender()
            opts = dict(self._push_opts)
            if opts.get("path"):
                blk = self._load_wav_stereo(opts["path"])
                label = os.path.basename(opts["path"])
            else:
                blk = tone(440.0, 1.0, 0.25)
                label = "440 Hz"
            if self.fx_on:
                t0 = time.time()
                blk = self._apply_fx(blk)
                self.note("音效链：%s（%s）预处理 %.0f ms"
                          % (self.fx.preset, PlayFX_PRESET_TEXT.get(self.fx.preset, ""),
                             (time.time() - t0) * 1000))
            self.note("推送 %s %.1f s × %d → %s" % (label, secs, len(blk), snd.dest))
            t_end = time.time() + secs
            n = 0
            while time.time() < t_end and not self._push_stop.is_set():
                # 必须把 stop 事件传进去：否则 play_array 会把当前这一整段
                # （最长 60 s）推完才回来，"打断上一段"实际打断不了 ——
                # 于是多个推流线程同时往板子灌，实测 3.3 倍实时，
                # 板子的 4×100 ms 环被反复覆盖，听感就是"一顿一顿、噔噔噔"。
                snd.play_array(blk, stop_event=self._push_stop)
                n += 1
            snd.close()
            self.note("推流结束：%d s" % n)
        except Exception as exc:
            self.note("推流失败：%r" % (exc,))
        finally:
            self.pushing = False

    # ---------------------------------------------------------------- 模拟信号
    def start_sim(self):
        """没有板子时用本地合成信号喂满同一条管线（答辩兜底 / 界面自测）。

        两个坑（10-29 实测）：① 上一轮的线程还没退出就 clear() 同一个 Event，
        旧线程被"复活"，于是两条线程一起灌数据，fps reported 200 而不是 100；
        ② 与 UDP 收听同时开时，两边的 _frames 一起涨，也会翻倍。
        所以这里：先 join 旧线程、每次换一个新的 Event、并把收听停掉（模式互斥）。
        """
        self.stop_rx()
        old = getattr(self, "_sim_thread", None)
        if old is not None and old.is_alive():
            try:
                self._sim_stop.set()
            except Exception:
                pass
            old.join(timeout=1.0)
        with self.lock:
            if self.sim:
                return {"ack": "模拟已在跑"}
            self.sim = True
            self._frames = 0
            self._t0 = time.time()
            self.pcm.clear()
            self.pcm_total = 0
            self._sim_stop = threading.Event()     # 新对象：旧线程手里的旧 Event 仍是 set
            self._sim_thread = threading.Thread(target=self._sim_loop, daemon=True)
        self._sim_thread.start()
        self.note("开始模拟信号：440 Hz 调制 + 稳态噪声（不需要板子）")
        return {"ack": "模拟中"}

    def stop_sim(self):
        self._sim_stop.set()
        with self.lock:
            self.sim = False
            self._frames = 0
            self._t0 = time.time()
            self.status = "已停止"
        self.note("停止模拟")
        return {"ack": "已停止模拟"}

    def _sim_loop(self):
        """100 帧每秒，每帧 480 点立体声。前 1.2 s 只有噪声，之后叠加“说话”包络。"""
        n = 480
        period = 1.0 / 100.0
        nxt = time.perf_counter()
        t = 0.0
        ph = 0.0
        rng = np.random.default_rng(7)
        while not self._sim_stop.is_set():
            t0 = time.time() - self._t0
            idx = np.arange(n, dtype=np.float64)
            noise = rng.normal(0.0, 150.0, n)          # 底噪 ≈ -47 dBFS，静音段看得见
            env = 1.0 if (t0 % 4.0) > 1.2 else 0.0          # 4 s 一个来回：1.2 s 静音
            tone = 9000.0 * env * (1.0 + 0.25 * np.sin(2 * np.pi * 3.0 * (t + idx / 48000.0)))
            ph = (ph + 440.0 * n / 48000.0) % 1.0
            sig = tone * np.sin(2 * np.pi * (ph + 440.0 * idx / 48000.0)) + noise
            mono = np.clip(sig, -32768, 32767).astype(np.int16)
            blk = np.repeat(mono.reshape(n, 1), 2, axis=1)
            # 模拟源同样过增强链：这样没有板子也能演示 PC 增强的效果
            # （答辩兜底场景）。关闭时 process() 原样返回，零开销。
            blk = self.enh.process(blk)
            # 11-02 修：模拟源也要走同一条下游链，否则「频谱·测量」页在模拟模式下
            # 永远 ready=False（meas/loud 之前只在 _pull_loop 里接，模拟线程是另开的一条）。
            if self.voc_on:
                blk = self.voc.process(blk)
            self.meas.feed(blk)
            self.loud.feed(blk)
            self.tap.write(blk)
            with self.lock:
                self.pcm.append((self.pcm_total, blk))
                self.pcm_total += n
            self._frames += 1
            t += n / 48000.0
            nxt += period
            dt = nxt - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            else:
                nxt = time.perf_counter()

    # ---------------------------------------------------------------- 语音助手
    def _xz_mod(self, name):
        """按需导入 work.xiaozhi 里的模块（web_server 自己不在包里，没有相对导入）。"""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        return __import__("work.xiaozhi." + name, fromlist=["_"])

    def chat_ensure_board(self):
        """确认板子 9200 通；不通就在本网段扫一遍（热点会给板子换地址）。"""
        ba = self._xz_mod("board_audio")
        if self.chat_board_ip and ba.reachable(self.chat_board_ip):
            self.chat_board_ok = True
            return True
        found = ba.discover()
        self.chat_board_ok = bool(found)
        if found:
            if found[0] != self.chat_board_ip:
                self.note("[小智] 板子喇叭 → %s:9200" % found[0])
            self.chat_board_ip = found[0]
        else:
            self.chat_board_ip = self.chat_board_ip or getattr(ba, "BOARD_HOST", None)
            self.note("[小智] 没扫到板子（9200 不通），这轮只出文字")
        return self.chat_board_ok

    def _chat_agent(self):
        """按当前 voice 设置拿到（必要时构建）小智实例。"""
        want = bool(self.chat_voice)
        if self._xz is None or self._xz_voice != want:
            self._xz_mod("agent")
            from work.xiaozhi.agent import build
            self._xz = build(speak_board=want, board_host=self.chat_board_ip,
                             log=lambda m: self.note("[小智] %s" % m))
            self._xz_voice = want
            self.note("语音助手就绪（输出：%s）" % ("语音" if want else "文字"))
        return self._xz

    def chat_ask(self, text):
        text = (text or "").strip()
        if not text:
            return {"ack": "说点什么", "ok": False}
        if self.chat_busy:
            return {"ack": "上一句还没答完", "ok": False}
        self.chat_busy = True
        self.chat_log.append({"role": "user", "text": text,
                              "t": time.strftime("%H:%M:%S")})
        threading.Thread(target=self._chat_job, args=(text,), daemon=True).start()
        return {"ack": "已发送", "ok": True}

    def _chat_job(self, text):
        t0 = time.time()
        try:
            if self.chat_voice:
                self.chat_ensure_board()          # 语音档先确认喇叭这条路通
            xz = self._chat_agent()
            reply = xz.ask(text)
            steps = [{"name": st.name, "args": st.args, "result": st.result}
                     for st in reply.steps]
            self.chat_log.append({"role": "assistant", "text": reply.text,
                                  "t": time.strftime("%H:%M:%S"),
                                  "ms": int((time.time() - t0) * 1000),
                                  "steps": steps, "voice": bool(self.chat_voice)})
            for st in steps:
                self.note("[小智] 工具 %s → %s" % (st["name"], str(st["result"])[:120]))
        except Exception as exc:                       # 没网/没 key/板子掉线都不该把页面搞死
            self.chat_log.append({"role": "error",
                                  "text": "%s: %s" % (type(exc).__name__, exc),
                                  "t": time.strftime("%H:%M:%S")})
            self.note("[小智] 出错：%r" % (exc,))
        finally:
            self.chat_busy = False
            del self.chat_log[:-80]                    # 只留最近 80 条

    def chat_snapshot(self):
        return {"busy": bool(self.chat_busy), "voice": bool(self.chat_voice),
                "board_ip": self.chat_board_ip, "board_ok": bool(self.chat_board_ok),
                "log": list(self.chat_log[-40:])}

    def chat_set_voice(self, want):
        """切换 AI 的输出方式：False = 只出文字，True = 文字 + 板子喇叭念出来。"""
        self.chat_voice = bool(want)
        if self.chat_voice:
            self.chat_ensure_board()
        self.note("语音输出 → %s" % ("开" if self.chat_voice else "关"))
        return {"ack": "语音输出 %s" % ("开" if self.chat_voice else "关"),
                "board": self.chat_board_ip, "board_ok": bool(self.chat_board_ok)}

    def chat_voice_test(self):
        """不经过大模型，直接让板子说一句：确认喇叭这条路（合成 → 9200 → I2S）通。"""
        if not self.chat_ensure_board():
            return {"ack": "板子 9200 连不上", "ok": False, "board": self.chat_board_ip}
        ba = self._xz_mod("board_audio")
        host = self.chat_board_ip

        def job():
            try:
                info = ba.speak("我在，板子的喇叭正常。", host=host)
                self.note("[小智] 试听 %s 字节 / %ss" % (info.get("bytes"),
                                                        info.get("seconds_audio")))
            except Exception as exc:
                self.note("[小智] 试听失败：%r" % (exc,))
        threading.Thread(target=job, daemon=True).start()
        return {"ack": "让板子说一句", "ok": True, "board": host}

    # ---------------------------------------------------------------- 五档 A/B
    def run_ab(self, seconds=6.0, modes=None, reset=False):
        """对指定档位各录一段「用户实际听到的那一路」，结果常驻在 self.ab_rows。"""
        if not (self.rx is not None or self.sim):
            return {"ack": "先点「开始收听」再跑 A/B", "ok": False}
        if self.ab_running:
            return {"ack": "A/B 正在跑，等这一档录完", "ok": False}
        keys = [m for m in (modes or [k for k, _ in AB_MODES]) if m in AB_LABEL]
        if not keys:
            return {"ack": "没有可录的档位", "ok": False}
        if reset:
            self.ab_rows = {}
        self.ab_running = True
        threading.Thread(target=self._ab_job, args=(float(seconds), keys),
                         daemon=True).start()
        names = " / ".join(AB_LABEL[k] for k in keys)
        return {"ack": "%s 各录 %.0f s" % (names, seconds), "ok": True}

    def _ab_job(self, seconds, keys):
        import os
        import wave as _wave

        outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ab_out")
        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception:
            outdir = os.path.join(os.path.expanduser("~"), "ab_out")
            os.makedirs(outdir, exist_ok=True)

        keep_dsp = self.dsp
        keep_enh = bool(self.enh_on)
        try:
            for m in keys:
                # 板子侧：PC 增强这一档固定挂 bypass（增强是 PC 算的，板子不该再动）
                want_dsp = "bypass" if m == "enh" else m
                self.dsp = want_dsp
                try:
                    BoardCtl().dsp(want_dsp)
                except Exception as exc:
                    self.note("A/B %s：切板子失败 %r（板子可能不在线）" % (m, exc))
                # PC 侧：只有 enh 档打开，其它档必须关掉，否则"旁路"里混着 PC 增强
                self._set_enh(m == "enh")
                time.sleep(1.2)                      # 等切档后的旧缓冲排空
                start = self.pcm_total
                time.sleep(seconds)
                with self.lock:
                    blocks = [b for (off, b) in self.pcm if off >= start]
                if not blocks:
                    self.note("A/B %s：缓冲区没数据（先收听）" % m)
                    continue
                x = np.concatenate(blocks, axis=0)
                mono = x.mean(axis=1).astype(np.float64) / 32768.0
                if mono.size < 4800:
                    self.note("A/B %s：只录到 %.2f s，跳过" % (m, mono.size / 48000.0))
                    continue
                rms = float(np.sqrt(np.mean(mono ** 2)) + 1e-12)
                fr = np.lib.stride_tricks.sliding_window_view(mono, 960)[::480]
                pw = (fr ** 2).mean(axis=1) + 1e-12
                floor = float(np.sqrt(np.percentile(pw, 10)))     # 10 % 分位当噪声底
                # 96 点峰值包络：波形对比用，别把原始样点塞进 JSON
                seg = max(1, mono.size // 96)
                body = mono[:seg * 96].reshape(96, seg)
                env = np.abs(body).max(axis=1)
                peak = float(env.max())
                peak_db = 20 * np.log10(peak) if peak else -140.0
                path = os.path.join(outdir, "ab_%s_%s.wav"
                                    % (m, time.strftime("%H%M%S")))
                with _wave.open(path, "wb") as w:
                    w.setnchannels(2)
                    w.setsampwidth(2)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes(x.astype("<i2").tobytes())
                self.ab_rows[m] = {
                    "mode": m,
                    "label": AB_LABEL[m],
                    "rms_db": round(20 * np.log10(rms), 2),
                    "floor_db": round(20 * np.log10(floor), 2),
                    "peak_db": round(peak_db, 2),
                    "secs": round(mono.size / 48000.0, 2),
                    "file": os.path.basename(path),
                    "env": [round(float(v), 4) for v in env],
                }
                self.ab_last = time.time()
                self.note("A/B %s：%.1f dBFS · 噪声底 %.1f dBFS · %s"
                          % (m, 20 * np.log10(rms), 20 * np.log10(floor),
                             os.path.basename(path)))
        finally:
            self.dsp = keep_dsp
            self._set_enh(keep_enh)
            try:
                BoardCtl().dsp("bypass" if (keep_dsp == "enh") else keep_dsp)
            except Exception:
                pass
            self.ab_running = False
            self.note("A/B 完成：%d 档有结果（存 %s）" % (len(self.ab_rows), outdir))

    def _set_enh(self, want):
        """A/B 内部用：直接开关 PC 增强，并同步引擎的 enabled 与状态。"""
        self.enh_on = bool(want)
        self.enh.enabled = bool(want)
        if want:
            self.enh.reset()

    def ab_snapshot(self):
        rows = [self.ab_rows[k] for k, _ in AB_MODES if k in self.ab_rows]
        return {"running": bool(self.ab_running), "rows": rows}

    # ---------------------------------------------------------------- 播放取数
    def pcm_slice(self, frm, max_samples=9600):
        """取 [frm, now) 的 int16 立体声，供浏览器 WebAudio 续播。

        返回 (实际起点, numpy 数组)。请求的起点太旧（已被 maxlen 挤掉）时，
        直接从当前最老的一块开始，调用方通过「实际起点 != 请求起点」判断需要重新对时。
        """
        with self.lock:
            if not self.pcm:
                return 0, None
            blocks = list(self.pcm)
            total = self.pcm_total
        oldest = blocks[0][0]
        start = max(int(frm), oldest)
        if start >= total:
            return total, None
        out = []
        got = 0
        for idx, blk in blocks:
            end = idx + int(len(blk))
            if end <= start:
                continue
            lo = max(0, start - idx)
            take = blk[lo:]
            if got + len(take) > max_samples:
                take = take[:max_samples - got]
            out.append(take)
            got += len(take)
            if got >= max_samples:
                break
        if not out:
            return start, None
        return start, np.concatenate(out, axis=0)

    # ---------------------------------------------------------------- 快照
    def _under_rate(self, under, window=2.5, min_dt=0.5):
        """欠载增速（次/秒），2.5 s 滑窗；样本不足返回 None。

        为什么单独算它：板子只产出 48.9 帧/秒、PC 按 100 帧/秒消费时，差值
        51 次/秒全部落成欠载 —— 这个数就是"算力天花板"的另一种写法，
        而 fps 字段在旧实现里恒等于 100，看不出来。实测 +51.6/s 对 48.9 fps。
        """
        now = time.monotonic()
        hist = self._under_hist
        if not hist or now - hist[-1][0] >= 0.25:
            hist.append((now, under))
        while len(hist) > 2 and now - hist[0][0] > window:
            hist.popleft()
        if len(hist) < 2:
            return None
        t0, u0 = hist[0]
        dt = now - t0
        if dt < min_dt:
            return None
        return (under - u0) / dt

    def snapshot(self):
        self.seen = True
        self.last_beat = time.time()
        wave, bands = [], []
        listening = self.rx is not None or self.sim
        # 每个模式各记一份整体响度，供"等响 A/B"补偿增益时用（键是板子报的 dsp）
        _loud = self.loud.stats()
        if listening and _loud.get("i") is not None:
            self._loud_by_mode[str(self.dsp)] = _loud["i"]
        depth = lost = under = dbfs = None
        fps = None
        packets = bad = over = target = None
        rx_fps = rx_age = under_rate = None
        link = "offline"

        if listening:
            buf = self.tap.read_last(int(1.0 * SAMPLE_RATE))
            if buf is not None and buf.size:
                n = 96
                step = max(1, buf.size // n)
                for i in range(0, buf.size - step + 1, step):
                    seg = buf[i:i + step]
                    wave += [float(np.max(seg)) / 32768.0, float(np.min(seg)) / 32768.0]
                now = time.time()
                dt = max(0.01, now - self._last_spec)
                self._last_spec = now
                # SpectrumAnalyzer 的口径是 ±1.0 归一化样本（满幅正弦 = 0 dB），
                # 直接把 int16 喂进去会得到 +90 dB，64 段全部顶到红色。
                mono = buf.mean(axis=1) if buf.ndim > 1 else buf
                db, _peaks = self.spec.analyze(mono.astype(np.float64) / 32768.0, dt)
                bands = [float(min(1.0, max(0.0, (v + 90.0) / 90.0))) for v in db]
                rms = float(np.sqrt(np.mean(buf.astype(np.float64) ** 2)))
                dbfs = 20.0 * np.log10(max(1e-9, rms / 32768.0))
            jb = self.jitter
            if jb is not None:
                depth = jb.depth_ms
                lost = getattr(jb, "lost", 0)
                under = jb.underloads
                under_rate = self._under_rate(jb.underloads)
                over = getattr(jb, "overflows", None)
                target = getattr(jb, "target_ms", None)
            elif self.sim:
                depth, lost, under, over, target = 100.0, 0, 0, 0, 100.0
            packets = getattr(self.rx, "packets", None)
            bad = getattr(self.rx, "bad_packets", None)
            if self.sim:
                packets, bad = self._frames * 2, 0

            # ---- 链路真值：只认"到达的帧"和"最后一个包多久以前"
            if self.rx is not None:
                last = getattr(self.rx, "last_rx", 0.0)
                age = (time.monotonic() - last) if last else None
                rx_age = age
                link = "ok" if (age is not None and age <= 1.0) else "offline"
                # 断流时 rx._times 里全是旧时间戳，rx.fps() 会给出一个"看起来正常"
                # 的假值（比如 12 分钟前那一秒算出来的 100），所以离线一律归零。
                rx_fps = 0.0 if link != "ok" else self.rx.fps()
            elif self.sim:
                rx_fps = self._frames / max(0.2, time.time() - self._t0)
                rx_age, link = 0.0, "ok"
            fps = rx_fps

        # ---- 板子真实状态：有新鲜状态包就以它为准，否则退回本机意图 ----
        # 这一条是整个"小智说切好了、面板也显示切好了、板子其实没动"的解法：
        # 面板显示的是板子自己报的 mode，而不是本机最后一次发出去的命令。
        bst = self.board_state()
        dsp_truth = self.dsp
        dsp_source = "intent"
        if bst is not None and bst.get("online"):
            dsp_truth, dsp_source = bst["mode"], "board"

        return {
            "listening": listening,
            "fps": fps, "depth_ms": depth, "lost": lost, "under": under, "dbfs": dbfs,
            # 链路健康：link=ok 才代表"最近 1 秒真的收到过包"。
            # 判板子死活请看 link / rx_age / rx_frames，不要看 fps 是不是 100。
            "link": link, "rx_age": rx_age, "rx_fps": rx_fps,
            "under_rate": under_rate,
            "rx_frames": getattr(self.rx, "frames", None),
            # dsp = 板子报的模式（真值）；dsp_intent = 本机上次下发的命令
            "dsp": dsp_truth,
            "dsp_name": DSP_NAME.get(DSP_KEY.get(dsp_truth, "2"), "—"),
            "dsp_source": dsp_source,
            "dsp_intent": self.dsp,
            "board": bst,
            "board_ip": "169.254.191.50", "pc_ip": _pc_ip(),
            "gate_ms": GATE_MS, "rnn_ms": RNN_MS,
            # 底噪注入电平：状态包 v2 起带 noise，在线时以板子为准；
            # 老固件（v1）没有，退回本机意图。
            "noise": (int(bst["noise"]) if (bst is not None and bst.get("online")
                                            and bst.get("noise") is not None)
                      else self.noise),
            "noise_source": ("board" if (bst is not None and bst.get("online")
                                         and bst.get("noise") is not None) else "intent"),
            "pushing": self.pushing, "sim": self.sim,
            "frames": self._frames, "packets": packets, "bad": bad,
            "pcm_total": self.pcm_total,
            "over": over, "target_ms": target,
            # 时长缩放：ts=100 表示恒等（不加延迟、无静音前奏）
            "ts": self.ts, "ts_apply": self.ts < 100,
            "ts_min": 50, "ts_max": 100,
            # PC 增强状态：独立于 dsp 字段，界面据此画开关与滑块
            "ab": self.ab_snapshot(),
            "chat": self.chat_snapshot(),
            "enh": self.enh.stats() if self.enh_on else {"on": False},
            "enh_on": bool(self.enh_on),
            # 声码器状态：on + 实时统计（帧数/浊音比/每帧耗时/当前增益）
            "voc": dict(self.voc.stats(), on=bool(self.voc_on)),
            # 响度（BS.1770-4）：m = 瞬时(400 ms) / s = 短时(3 s) / i = 整体(带门限)
            # 单位 LUFS，peak 是采样峰值 dBFS。指标卡与"等响 A/B"都用它。
            "loud": dict(_loud, by_mode=dict(self._loud_by_mode)),
            # 播放侧音效（包2）：只作用在推给板子的那一路，与上面的收听链无关
            "loop": (dict(self.loop.stats(), on=True, board_only=bool(getattr(self, "_lb_muted", False))) if self.loop is not None else {"on": False, "board_only": False}),
            "fx": dict(self.fx.stats(), on=bool(self.fx_on)),
            # 双 DAC 输出模式（包6）：状态包 v2 起带 dac_mode，在线时以板子为准；
            # 老固件（v1）没有这个字段，退回本机意图。
            "dac": ({"mode": int(bst["dac_mode"]),
                     "name": DAC_NAME.get(int(bst["dac_mode"]), "—"),
                     "source": "board"}
                    if (bst is not None and bst.get("online") and bst.get("dac_mode") is not None)
                    else {"mode": int(self.dac_mode),
                          "name": DAC_NAME.get(int(self.dac_mode), "—"),
                          "source": "intent"}),
            "wave": wave, "spec": bands,
            "log": list(self.log)[-40:],
            "status": self.status,
        }

    # ---------------------------------------------------------------- 命令
    def command(self, body):
        op = (body or {}).get("op")
        if op == "dsp":
            self.dsp = body.get("v", "gate")
            info = BoardCtl().dsp(self.dsp)
            self.note("切档 → %s（%s）" % (self.dsp, info.get("hex")))
            return {"ack": "已发 %s" % info.get("char")}
        if op == "dac":
            # 包6：双 DAC 输出模式。m 显式取值 0/1/2（不做翻转语义），
            # 固件侧收到重复字节不会重复写 GPIO（幂等），广播重发安全。
            m = int(body.get("m", 0))
            if m not in DAC_KEY:
                return {"error": "m 只能是 0(M0) / 1(M1) / 2(M2)"}
            info = BoardCtl().send_bytes(DAC_KEY[m])
            self.dac_mode = m
            self.note("双 DAC 模式 → %s" % DAC_NAME[m])
            return {"ack": "已发 %s（%s）" % (DAC_KEY[m].decode(), DAC_NAME[m]),
                    "delivered": info.get("delivered", 0), "mode": m}
        if op == "gain":
            db = int(body.get("db", 0))
            info = BoardCtl().gain(db)
            self.note("增益 → %d dB" % info.get("gain_db", db))
            return {"ack": "增益 %d dB" % info.get("gain_db", db),
                    "delivered": info.get("delivered", 0), "db": info.get("gain_db", db)}
        if op == "lat":
            frames = int(body.get("frames", 30) or 30)
            on = body.get("on")
            on = True if on is None else bool(on)
            info = BoardCtl().lat(frames, on)
            lat_frames = info.get("lat_frames", frames)
            msg = ("延迟 %d 帧开" % lat_frames) if on else "延迟关"
            self.note("延迟 → " + msg)
            return {"ack": msg, "delivered": info.get("delivered", 0),
                    "frames": lat_frames, "on": on}
        if op == "noise":
            v = int(body.get("v", 0))
            BoardCtl().noise(v)
            self.noise = v
            self.note("底噪注入 → %s" % v)
            return {"ack": "底噪 %s" % v}
        if op == "sim":
            return self.start_sim() if body.get("v") else self.stop_sim()
        if op == "loop":
            want = bool(body.get("on", False))
            if not want:
                if self.loop is not None:
                    st = self.loop.stats(); self.loop.stop(); self.loop = None
                    if getattr(self, "_lb_muted", False):
                        try:
                            from loopback import _endpoint_mute; _endpoint_mute(False)
                        except Exception: pass
                        self._lb_muted = False
                    self.note("系统声音同步 → 停（共 %d 块）" % st.get("blocks", 0))
                    return {"ack": "已停止同步", "loop": st}
                return {"ack": "本来就没在同步", "loop": None}
            alive = (self.loop is not None
                     and getattr(self.loop, "_thr", None) is not None
                     and self.loop._thr.is_alive())
            if self.loop is not None and not alive:
                # 线程已退出（异常/停止后没清），先把残骸收掉，否则下面会误报"已在同步"
                try:
                    self.loop.stop()
                except Exception:
                    pass
                self.loop = None
            if self.loop is None:
                lb = LoopbackPusher(fx_provider=(lambda: (self.fx if self.fx_on else None))).start()
                self.loop = lb
                if body.get("board_only"):
                    try:
                        from loopback import _endpoint_mute
                        self._lb_muted = _endpoint_mute(True)
                    except Exception: self._lb_muted = False
                self.note("系统声音同步 → 开（%s）" % getattr(lb, "device_name", "?"))
                return {"ack": "已开始同步电脑声音", "loop": lb.stats()}
            if body.get("board_only") is not None:
                try:
                    from loopback import _endpoint_mute
                    self._lb_muted = _endpoint_mute(bool(body.get("board_only")))
                except Exception: self._lb_muted = False
                return {"ack": "只让板子响", "loop": self.loop.stats()}
            return {"ack": "已在同步", "loop": self.loop.stats()}
        if op == "push":
            secs = float(body.get("v") or 0)
            if secs <= 0:
                return self.stop_push()
            return self.start_push(secs, src=body.get("src") or "tone",
                                   path=body.get("file"))
        if op == "fx":
            # 播放侧音效：on 显式开关（不做翻转语义），preset 切预设，
            # eq 直接写 10 段增益（dB），与预设互斥（写了 eq 就以 eq 为准）。
            changed = []
            if body.get("preset"):
                name = self.fx.set_preset(str(body["preset"]))
                changed.append("预设 %s" % name)
            if isinstance(body.get("eq"), (list, tuple)):
                self.fx.set_eq(list(body["eq"]))
                changed.append("EQ 自定义")
            if body.get("on") is not None:
                self.fx_on = bool(body["on"])
                self.fx.enabled = self.fx_on   # 关键：process() 靠这个开关，不设等于整条链空转
                self.fx.enabled = self.fx_on      # 引擎开关与 hub 同步（两处都是真值）
                if self.fx_on:
                    self.fx.reset()
                changed.append("开" if self.fx_on else "关")
            # ---- P0/P1 的新级：响度归一化 / 交叉馈送 / 双 DAC 校准 / 真峰值 ----
            if body.get("norm") is not None:
                self.fx.set_norm(on=bool(body["norm"]),
                                 target_lufs=body.get("target"),
                                 max_gain_db=body.get("max_gain"))
                changed.append("响度归一化")
            elif body.get("target") is not None or body.get("max_gain") is not None:
                self.fx.set_norm(target_lufs=body.get("target"),
                                 max_gain_db=body.get("max_gain"))
                changed.append("归一化参数")
            if body.get("crossfeed") is not None:
                self.fx.set_crossfeed(on=True, amount=float(body["crossfeed"]))
                changed.append("交叉馈送 %.2f" % float(body["crossfeed"]))
            if body.get("crossfeed") == 0:
                self.fx.set_crossfeed(on=False)
            if body.get("cal") is not None:
                self.fx.set_cal(on=bool(body["cal"]),
                                board_db=body.get("cal_board"),
                                ext_db=body.get("cal_ext"))
                changed.append("双 DAC 校准")
            elif body.get("cal_board") is not None or body.get("cal_ext") is not None:
                self.fx.set_cal(board_db=body.get("cal_board"),
                                ext_db=body.get("cal_ext"))
                changed.append("校准曲线")
            if body.get("cal_file"):
                try:
                    b, e = DacCal.load(str(body["cal_file"]))
                    self.fx.set_cal(on=True, board_db=b, ext_db=e)
                    changed.append("校准（%s）" % body["cal_file"])
                except Exception as exc:                  # noqa: BLE001
                    return {"ack": "校准文件读不了：%s" % exc}
            if body.get("tp") is not None:
                self.fx.set_tp_target(float(body["tp"]))
                changed.append("真峰值目标 %.1f dBTP" % float(body["tp"]))
            if not changed:
                return {"ack": "音效 %s" % ("开" if self.fx_on else "关"),
                        "fx": dict(self.fx.stats(), on=bool(self.fx_on))}
            self.note("播放音效 → %s" % "、".join(changed))
            return {"ack": "音效 %s" % "、".join(changed),
                    "fx": dict(self.fx.stats(), on=bool(self.fx_on))}
        if op == "tone":
            # v 缺省按 True 处理（老页面仍发 {op:'tone'}）—— 显式开/关，
            # 不再发翻转字节：广播重复投递会让"切换"变成"开了又关"。
            want = bool(body.get("v", True))
            ctl = BoardCtl()
            ctl.tone() if want else ctl.tone_off()
            self.tone_on = want
            self.note("自检音 → %s" % ("开" if want else "关"))
            return {"ack": "自检音已开启" if want else "自检音已关闭"}
        if op == "rntarget":
            v = int(body.get("v", 30))
            BoardCtl().rntarget(v)
            self.rn_target = v
            self.note("延迟档 → %d 帧（%d ms，三个模式共用）" % (v, v * 10))
            return {"ack": "延迟档 %d ms" % (v * 10)}
        if op == "ts":
            # 时长缩放比例。合法区间在固件里判定（ts_parse_rate），PC 侧只做
            # "明显不可能"的前置拦截，不替板子决定边界 —— 边界只有一份。
            v = int(body.get("v", 100))
            if not (0 <= v <= 255):
                return {"ack": "比例必须是 0..255 的整数（越界值用来测拒绝分支）"}
            ctl = BoardCtl()
            # 两字节命令：'S' + 百分比。单字节空间已被占满（0-2/a-h/i,j,k,K,n,N,t,T）。
            ctl.send_bytes(bytes([ord("S"), v & 0xFF]))
            self.ts = v
            self.note("时长缩放 → %d%%%s" % (v, "" if v >= 100 else "（放慢 %.2f×）" % (100.0 / v)))
            return {"ack": "时长缩放 %d%%" % v}
        if op == "listen":
            if body.get("v"):
                self.start_rx()
                # 板子自报的状态是唯一真值：在线就以它为准，把本机意图对齐过去。
                # 绝不能再"重发旧意图"——2026-10-30 实测过这个坑：小智刚把板子切到
                # denoise，用户说"打开接收"，这里把界面记着的 bypass 重发了一次，
                # 板子当场被打回 bypass，而小智那边还以为是成功的。
                bst = self.board_state()
                if bst is not None and bst.get("online"):
                    if bst["mode"] in DSP_KEY:
                        self.dsp = bst["mode"]
                    self.note("收听开始；板子当前 %s，界面按板子状态对齐" % bst["mode"])
                else:
                    # 板子状态未知时才重发——这时是"猜"，日志里必须写明
                    try:
                        info = BoardCtl().dsp(self.dsp)
                        self.note("板子状态未知，重发本机模式 → %s（%s）"
                                  % (self.dsp, info.get("hex")))
                    except Exception as exc:
                        self.note("工作模式重发失败：%r（板子可能不在线）" % (exc,))
            else:
                self.stop_rx()
            return {"ack": "收听" if body.get("v") else "已停止"}
        if op == "device":
            self.device = int(body.get("v", -1))
            self.note("选择输出声卡 #%s" % self.device)
            return {"ack": "声卡 %s" % self.device}
        if op == "chat":
            return self.chat_ask(body.get("text", ""))
        if op == "chat_voice":
            return self.chat_set_voice(body.get("v"))
        if op == "chat_voice_test":
            return self.chat_voice_test()
        if op == "chat_clear":
            self.chat_log = []
            if self._xz is not None:
                self._xz.history = self._xz.history[:1]
            self.note("会话已清空")
            return {"ack": "会话已清空"}
        if op == "ab":
            return self.run_ab(float(body.get("secs", 6.0)),
                               body.get("modes"), bool(body.get("reset")))
        if op == "enh":
            # 独立开关 + 两个参数，一条命令全包（页面上是「开关 + 两个滑块」）。
            # 注意没有任何"翻转"语义：v 必须显式给 bool，重复投递也幂等。
            if "v" in body:
                want = bool(body.get("v"))
                self.enh_on = want
                self.enh.enabled = want
                if want:
                    # 打开时清状态：上一次关之前残留的噪声底/OLA 累积器
                    # 如果被接着用，头 100~200 ms 会有一下轻的"扑"。
                    self.enh.reset()
                self.note("PC 增强 → %s" % ("开" if want else "关"))
            s = body.get("strength")
            p = body.get("protect")
            if s is not None or p is not None:
                self.enh.set_params(s, p)
            st = self.enh.stats()
            return {"ack": "PC 增强 %s / 强度 %.0f / 保护 %.0f"
                           % ("开" if self.enh_on else "关", st["strength"], st["protect"])}
        if op == "voc":
            # 显式 on/off（不做翻转语义：控制面是广播，重复投递是常态）。
            # secs > 0 时到点自动关 —— 界面上的「试听」就是这一条。
            want = bool(body.get("v", False))
            try:
                secs = float(body.get("secs") or 0)
            except (TypeError, ValueError):
                secs = 0.0
            if self._voc_timer is not None:
                self._voc_timer.cancel()
                self._voc_timer = None
            self.voc_on = want
            self.voc.reset()
            if want:
                if secs > 0:
                    t = threading.Timer(secs, self._voc_off)
                    t.daemon = True
                    self._voc_timer = t
                    t.start()
                self.note("声码器 → 开（LPC-16 / 20 ms / 7.2 kbps%s）"
                          % ("，%.0f s 后自动关" % secs if secs > 0 else ""))
                return {"ack": "声码器开%s" % (" %.0f s" % secs if secs > 0 else "")}
            self.note("声码器 → 关")
            return {"ack": "声码器关"}
        return {"ack": "未知命令"}

    def _voc_off(self):
        """定时器回调：试听到点自动关（不关的话用户会以为界面卡了）。"""
        with self.lock:
            if not self.voc_on:
                self._voc_timer = None
                return
            self.voc_on = False
            self._voc_timer = None
            self.note("声码器 → 关（试听结束）")


def _pc_ip():
    """朝板子探路取本地源地址（比 getaddrinfo 可靠，APIPA 不会出现在主机名解析里）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("169.254.191.50", BOARD_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "—"


def _list_devices():
    out = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0:
                out.append({"idx": i, "name": d["name"]})
    except Exception:
        pass
    return out


HUB = Hub()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, path):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._static(os.path.join(WEB_DIR, "index.html"))
        if p == "/api/state":
            return self._json(HUB.snapshot())
        if p == "/api/devices":
            return self._json({"devices": _list_devices()})
        if p == "/api/pcm":
            return self._pcm()
        if p == "/api/diag":
            return self._diag()
        if p == "/api/spectrum":
            # 顺带把响度带回去：指标卡在同一张卡上，多一次 /api/state 请求没必要。
            return self._json(dict(HUB.meas.snapshot(), loud=HUB.loud.stats()))
        return self._static(os.path.join(WEB_DIR, p.lstrip("/")))

    def _diag(self):
        """诊断包：状态快照 + 频谱 + 最近事件 + 运行环境，一份 JSON 全带走。"""
        import platform
        import time as _t

        pack = {
            "kind": "eth-audio-diag",
            "ver": 1,
            "when": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "machine": platform.machine(),
            },
            "state": HUB.snapshot(),
            "spectrum": dict(HUB.meas.snapshot(), loud=HUB.loud.stats()),
            "events": list(HUB.log)[-80:],
            "ab": HUB.ab_snapshot(),
        }
        raw = json.dumps(pack, ensure_ascii=False, indent=1).encode("utf-8")
        name = "eth-audio-diag-%s.json" % _t.strftime("%Y%m%d-%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _pcm(self):
        q = {}
        try:
            for kv in (self.path.split("?", 1)[1].split("&") if "?" in self.path else []):
                k, _, v = kv.partition("=")
                q[k] = int(v)
        except Exception:
            q = {}
        frm = q.get("from", 0)
        start, data = HUB.pcm_slice(frm)
        if data is None:
            raw = b""
            count = 0
        else:
            raw = np.ascontiguousarray(data, dtype="<i2").tobytes()
            count = int(data.shape[0])
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Pcm-Start", str(start))
        self.send_header("X-Pcm-Count", str(count))
        self.send_header("X-Pcm-Total", str(HUB.pcm_total))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/cmd":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            return self._json(HUB.command(body))
        except Exception:
            HUB.note("命令失败：" + traceback.format_exc(limit=1))
            return self._json({"ack": "失败"}, 500)


def _idle_watchdog():
    while True:
        time.sleep(5.0)
        if HUB.seen and (time.time() - HUB.last_beat) > IDLE_LIMIT:
            print("页面已离线 %.0fs，退出。" % IDLE_LIMIT, flush=True)
            os._exit(0)


def serve(port=PORT, host=HOST):
    if EXIT_ON_IDLE:
        threading.Thread(target=_idle_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), Handler)
    print("serving http://127.0.0.1:%d/" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
