"""udp_audio.py -- "AU" UDP 音频协议封装（48 kHz / 16 bit / 立体声 / 10 ms 一帧）

设计目标
--------
1. 只依赖 numpy + 标准库（不依赖 tkinter、不依赖 sounddevice），方便命令行/自测复用。
2. 板子固件（work/eth_audio/sw/udp_link.c 与 work/eth_axi/sw/udp_audio.c）可原样对接。
3. 收流侧带抖动缓冲与时钟漂移处理，长时间播放不会因板子与声卡 0.1% 的时钟差而无限增长。

报文格式（两个方向一致）
------------------------
    +------+------+---------+-----------+-----------+
    | 'A'  | 'U'  | seq(16) | chunk(16) | total(16) |  <- 8 字节，多字节字段大端
    +------+------+---------+-----------+-----------+
    | 960 字节 PCM（23040 bit 以外一律不解释）      |
    +------------------------------------------------+
    PCM: 小端 int16，每 4 字节一个立体声样本对：
         低 16 位 = 左声道，高 16 位 = 右声道。
    一帧 = 480 个立体声样本 = 1920 字节 = 10 ms @48 kHz，
    固件把一帧拆成 chunk=0/1 两个 240 样本（960 字节）的 UDP 包。

注意（与固件实测对齐）
--------------------
* `total` 字段在规范里是"整帧的样本数"=480，但两块固件实际都填入
  AUDIO_BYTES = 1920（字节数）。本模块**不校验** `total`，只在
  Packet.total 里原样给出，重组只依赖 chunk。
* 板子不回答 ARP（裸 TEMAC + 手写 IPv4），所以 PC 必须发**广播**；
  默认目的地址 255.255.255.255。
"""

from __future__ import annotations

import math
import os
import queue
import socket
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

__all__ = [
    "SAMPLE_RATE", "CHANNELS", "FRAME_SAMPLES", "CHUNK_SAMPLES", "FRAME_BYTES",
    "CHUNK_BYTES", "HEADER_BYTES", "MAGIC", "FRAME_MS", "MIC_PORT", "BOARD_PORT",
    "BOARD_IP", "BROADCAST", "ProtocolError", "Packet", "FrameAssembler",
    "JitterBuffer", "MicReceiver", "BoardSender", "pack_packet", "pack_frame",
    "unpack_packet", "frame_to_bytes", "bytes_to_frame", "rms_dbfs",
    "read_wav", "write_wav", "wav_info", "tone", "resample_linear",
    "CTL_MAGIC", "CTL_MUTE", "CTL_UNMUTE", "CTL_GAIN_UP", "CTL_GAIN_DOWN",
    "CTL_MODE", "CTL_TEXT", "CTL_NAMES", "pack_control", "unpack_control",
    "BoardControl", "RingTap", "WaveformView", "SpectrumAnalyzer",
    "WavRecorder", "apply_gain", "mono_mix",
]

# ---------------------------------------------------------------- 常量

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 480                       # 10 ms
CHUNK_SAMPLES = 240
BYTES_PER_SAMPLE = 2
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE      # 1920
CHUNK_BYTES = CHUNK_SAMPLES * CHANNELS * BYTES_PER_SAMPLE      # 960
HEADER_BYTES = 8
MAGIC = b"AU"
FRAME_MS = 1000.0 * FRAME_SAMPLES / SAMPLE_RATE                # 10.0

MIC_PORT = 50505                          # 板子 -> PC 音频流
BOARD_PORT = 50506                        # PC -> 板子 网络音源
BOARD_IP = "169.254.191.50"
BROADCAST = "255.255.255.255"

# 板子的 IP 写死在固件里（udp_link.c: BOARD_IP0..3 = 169,254,191,50），
# 它**不检查目的 IP**，只校验 EtherType=0x0800 / 协议=17 / 目的端口。
# 所以理论上任何广播都能到达它的网卡 —— 但前提是**包真的从那块网卡出去**。
#
# 坑（2026-10-27 实测，代价一整轮自检）：受限广播 255.255.255.255 由 Windows
# 按**默认路由**选网卡发出，机器上一旦有 Wi-Fi 或第二块网卡，包就走了错的口，
# 板子一个字节都收不到。这就是"界面上点降噪档/底噪/自检音全都没反应"的真因
# （而命令行 ctl.py 一直好用，因为它发的是 169.254.255.255）。
BOARD_IP = "169.254.191.50"
BOARD_BCAST = "169.254.255.255"      # 板子在 169.254/16 链路本地网段


def _local_ip_towards(ip: str, port: int) -> str:
    """让系统自己回答"去这个地址该用哪个本地网卡地址"。

    UDP connect() 不发任何包，只解析路由并把默认对端钉住，之后 getsockname()
    就是源地址。这是 Windows 上唯一不依赖第三方库、又能拿到 169.254 链路本地
    地址的办法——socket.getaddrinfo(hostname) 根本不会列出它（实测只回了
    26.28.x / 192.168.x / 10.82.x，没有 169.254），所以老实现永远退回到受限广播。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, int(port)))
        return s.getsockname()[0]
    finally:
        s.close()


def board_bcast() -> str:
    """给"发给板子"挑一个真能到达它的广播地址（音频用，必须唯一）。

    判据优先级：
    1. 朝板子 IP 探路得到的源地址若是 169.254.x.x → 用该网段子网广播；
    2. 主机名解析出的地址里若有 169.254.x.x → 同上（老路径，留作兜底）；
    3. 都不行才退回受限广播 255.255.255.255（单网卡机器上两者等价）。
    """
    try:
        if _local_ip_towards(BOARD_IP, BOARD_PORT).startswith("169.254."):
            return BOARD_BCAST
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("169.254.") and ip != BOARD_IP:
                return BOARD_BCAST
    except OSError:
        pass
    return BROADCAST


def ctl_targets() -> list:
    """控制命令的目标列表：都发一遍。

    控制命令是幂等的（固件把它们做成"设成某值"，不是翻转），重复投递本来
    就容忍（广播本身会到 2~3 份），所以多播一个地址是安全的；
    而音频包不能这么干——同一帧到两次会把包间节拍打乱。
    """
    out = [BOARD_BCAST]
    if BROADCAST not in out:
        out.append(BROADCAST)
    return out

_HDR = struct.Struct(">2sHHH")
assert _HDR.size == HEADER_BYTES

# 规范值（样本数）与固件实际值（字节数），send 时默认用规范值，
# 兼容发送用 1920 的老脚本。
TOTAL_SAMPLES = FRAME_SAMPLES
TOTAL_BYTES = FRAME_BYTES


class ProtocolError(ValueError):
    """报文不符合 AU 协议。"""


# ---------------------------------------------------------------- 打包 / 解包

def frame_to_bytes(frame) -> bytes:
    """把一帧（(480,2) int16 或等长 bytes）转成 960*2 字节小端 PCM。"""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        raw = bytes(frame)
        if len(raw) != FRAME_BYTES:
            raise ValueError("帧长度必须是 %d 字节，收到 %d" % (FRAME_BYTES, len(raw)))
        return raw
    arr = np.asarray(frame)
    if arr.dtype != np.int16:
        arr = np.clip(arr, -32768, 32767).astype(np.int16)
    if arr.shape != (FRAME_SAMPLES, CHANNELS):
        raise ValueError("帧形状必须是 (%d,%d)，收到 %r"
                         % (FRAME_SAMPLES, CHANNELS, (arr.shape,)))
    return np.ascontiguousarray(arr).astype("<i2").tobytes()


def bytes_to_frame(pcm: bytes) -> np.ndarray:
    """960*2 字节 PCM -> (480,2) int16 数组。"""
    if len(pcm) % (CHANNELS * BYTES_PER_SAMPLE):
        pcm = pcm[:len(pcm) - len(pcm) % (CHANNELS * BYTES_PER_SAMPLE)]
    return np.frombuffer(pcm, dtype="<i2").reshape(-1, CHANNELS).copy()


def pack_packet(pcm: bytes, seq: int, chunk: int, total: int = TOTAL_SAMPLES) -> bytes:
    """组一个 UDP 包：8 字节头 + PCM。"""
    if len(pcm) % 4:
        raise ValueError("PCM 长度必须是 4 的倍数（立体声 s16），收到 %d" % len(pcm))
    return _HDR.pack(MAGIC, seq & 0xFFFF, chunk & 0xFFFF, total & 0xFFFF) + pcm


def pack_frame(frame, seq: int, total: int = TOTAL_SAMPLES):
    """把整帧拆成两个 UDP 包，返回 (chunk0, chunk1)。"""
    pcm = frame_to_bytes(frame)
    return (
        pack_packet(pcm[:CHUNK_BYTES], seq, 0, total),
        pack_packet(pcm[CHUNK_BYTES:], seq, 1, total),
    )


@dataclass
class Packet:
    seq: int
    chunk: int
    total: int
    pcm: bytes

    @property
    def samples(self) -> int:
        return len(self.pcm) // (CHANNELS * BYTES_PER_SAMPLE)


def unpack_packet(data: bytes) -> Packet:
    """解析一个 UDP 载荷；不合法时抛 ProtocolError。"""
    if len(data) < HEADER_BYTES:
        raise ProtocolError("包太短：%d 字节" % len(data))
    magic, seq, chunk, total = _HDR.unpack_from(data, 0)
    if magic != MAGIC:
        raise ProtocolError("magic 不是 b'AU'：%r" % (magic,))
    pcm = data[HEADER_BYTES:]
    if chunk > 1:
        raise ProtocolError("chunk 只能是 0/1，收到 %d" % chunk)
    if not pcm:
        raise ProtocolError("没有 PCM 负载")
    # 尾部对齐到 4 字节（容忍某些工具多塞的填充）
    pcm = pcm[:len(pcm) - len(pcm) % 4]
    return Packet(seq=seq, chunk=chunk, total=total, pcm=pcm)


def rms_dbfs(frame) -> tuple:
    """返回 (rms_linear_0_1, dbfs)。dbfs 下限 -120。"""
    arr = np.asarray(frame)
    if arr.size == 0:
        return 0.0, -120.0
    x = arr.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    dbfs = 20.0 * np.log10(rms) if rms > 1e-6 else -120.0
    return rms, float(max(dbfs, -120.0))


# ---------------------------------------------------------------- 帧重组

class FrameAssembler:
    """把 chunk=0/1 两个包重组成帧，顺带统计丢帧/重复/乱序。"""

    def __init__(self, max_age_frames: int = 64):
        self.max_age_frames = max_age_frames
        self._pending: dict = {}          # seq -> {chunk: pcm}
        self._oldest = None               # 先进先出清理用
        self._order: deque = deque()
        self.last_seq: Optional[int] = None
        self.frames = 0
        self.lost = 0                    # 按 seq 间断估算的丢帧数
        self.dups = 0                    # 重复 chunk / 重复帧
        self.late = 0                    # 迟到（已发过的旧 seq）
        self.partial_dropped = 0         # 只有半个包、超时被丢

    # -- 内部
    def _evict(self, newest: Optional[int]) -> None:
        if newest is None:
            return
        keep = []
        for s in self._order:
            if s in self._pending and ((newest - s) & 0xFFFF) > self.max_age_frames:
                self._pending.pop(s, None)
                self.partial_dropped += 1
            else:
                keep.append(s)
        self._order = deque(keep)

    def reset(self) -> None:
        self._pending.clear()
        self._order.clear()
        self.last_seq = None

    def push(self, pkt: Packet) -> Optional[np.ndarray]:
        """喂一个包；凑齐一帧则返回 (480,2) int16，否则 None。"""
        self._evict(pkt.seq)
        slots = self._pending.get(pkt.seq)
        if slots is None:
            slots = {}
            self._pending[pkt.seq] = slots
            self._order.append(pkt.seq)
        if pkt.chunk in slots:
            self.dups += 1
            return None
        slots[pkt.chunk] = pkt.pcm
        if len(slots) < 2:
            return None

        pcm = slots.get(0, b"") + slots.get(1, b"")
        self._pending.pop(pkt.seq, None)
        try:
            self._order.remove(pkt.seq)
        except ValueError:
            pass
        if len(pcm) < FRAME_BYTES:
            self.partial_dropped += 1
            return None

        # seq 连续性（16 位回绕）
        if self.last_seq is not None:
            d = (pkt.seq - self.last_seq) & 0xFFFF
            if d == 0:
                self.dups += 1
                return None
            if d < 0x8000:
                self.lost += d - 1
                self.last_seq = pkt.seq
            else:
                self.late += 1
                return bytes_to_frame(pcm)          # 迟到帧仍交给上层，但不错位
        else:
            self.last_seq = pkt.seq
        self.frames += 1
        return bytes_to_frame(pcm[:FRAME_BYTES])


# ---------------------------------------------------------------- 抖动缓冲

class JitterBuffer:
    """以 10 ms 块为单位的播放缓冲：自适应目标 + 丢帧隐藏 + 漂移补偿。

    v1.0 行为
    ---------
    目标深度 60~120 ms；低于 30 ms 视为欠载（补静音），高于 400 ms 视为溢出
    （丢最旧）。125~400 ms 之间做温和修剪（一次丢一块），这样板子时钟比声卡
    快 0.1% 时深度会稳定在 120 ms 附近而不是一直涨。

    v1.1 变化
    ---------
    * 目标深度自适应：由实测到达间隔抖动推算（60 + 4×抖动 ms），限制在
      60~200 ms；每秒最多移动 2 ms（≈ 每 10 s ±20 ms），带 2 ms 死区，
      因此运行中不会来回震荡。修剪阈值 = 当前目标 + 25 ms。
    * 丢帧隐藏：欠载时用最后一个样本做 5 ms 指数淡出，恢复时 5 ms 线性淡入，
      不再硬切静音（避免"咔"声），计数 concealed。
    * 时钟漂移补偿：用到达间隔估计生产端相对 100 fps 的 ppm 偏差，按每秒
      最多 1 个样本的速率插值/删除样本做微调（计数 corrections）；0.5% 这种
      大偏差的大头仍由温和修剪兜住，保证不会出现连续溢出。
    """

    # v1.0 常量保留（外部与自测仍在引用），v1.1 用下面的自适应参数
    TARGET_MIN_MS = 60.0
    TARGET_MAX_MS = 120.0
    UNDERLOAD_MS = 30.0
    OVERFLOW_MS = 400.0
    TRIM_HIGH_MS = 125.0

    ADAPT_MIN_MS = 60.0
    ADAPT_MAX_MS = 200.0
    # v1.2：三档延迟档位（借 SonoBus 的"自动/低延迟/稳定"思路）。
    # low    —— 演示默认，目标 45~90 ms，抖动大时也不许涨太多，追求"听起来像直连"
    # stable —— 网络明显抖动时手动切过来，目标 110~220 ms，宁慢不卡
    # auto   —— 默认，按实测抖动在 60~200 ms 之间自己找位置
    MODE_PROFILES = {
        "auto":   (60.0, 200.0),
        "low":    (45.0, 90.0),
        "stable": (110.0, 220.0),
    }
    SLEW_MS_PER_S = 2.0          # 收敛速度：每 10 s 最多 ±20 ms
    DEADBAND_MS = 2.0            # 死区，防止在目标附近抖动
    JITTER_GAIN = 4.0            # 1 ms 到达抖动 -> 多留 4 ms
    FADE_MS = 5.0
    FADE_SAMPLES = int(SAMPLE_RATE * FADE_MS / 1000.0)      # 240
    TRIM_MARGIN_MS = 25.0
    MAX_CORRECTION_PER_S = 1.0   # 每秒最多修正 1 个样本
    # v1.2 丢包外推（学 WebRTC NetEq 的 expand）：基音周期搜索范围 80~400 Hz
    PITCH_MIN_HZ = 80.0
    PITCH_MAX_HZ = 800.0
    PITCH_MIN_CORR = 0.30        # 归一化自相关低于这个值就退回淡出
    HIST_SAMPLES = 4096          # 基音搜索用的历史长度
    MERGE_SAMPLES = 48           # 外推起手 1 ms 的接缝融合长度

    def __init__(self, block: int = FRAME_SAMPLES, target_ms: float = 80.0,
                 mode: str = "auto"):
        self.block = int(block)
        self._buf: deque = deque()
        self._samples = 0
        self._lock = threading.Lock()
        self.underloads = 0
        self.overflows = 0
        self.trims = 0
        self.concealed = 0
        self.extrapolated = 0
        self.corrections = 0
        self.pushed = 0
        self.pulled = 0
        # 延迟档位
        self.mode = mode if mode in self.MODE_PROFILES else "auto"
        self.min_ms, self.max_ms = self.MODE_PROFILES[self.mode]
        # 自适应目标
        self.target_ms = min(max(float(target_ms), self.min_ms), self.max_ms)
        self._desired_ms = self.target_ms
        self._jitter_ms = 0.0
        self._arrival_ema: Optional[float] = None
        self._last_arrival: Optional[float] = None
        self._adapt_at: Optional[float] = None
        self._win_under = 0
        self._win_trim = 0
        self._last_raise_push = -1000
        self.drift_ppm = 0.0
        # 丢帧隐藏 / 漂移修正状态
        self._concealing = False
        self._have_data = False
        self._last_sample = np.zeros(CHANNELS, dtype=np.float32)
        self._corr_debt = 0.0
        # 基音外推用的历史（环形，只存最近 HIST_SAMPLES 个立体声样本）
        self._hist = np.zeros((self.HIST_SAMPLES, CHANNELS), dtype=np.int16)
        self._hist_n = 0
        self.pitch_hz = 0.0
        self._gap_samples = 0

    # -- 属性
    @property
    def depth_ms(self) -> float:
        return self._samples * 1000.0 / SAMPLE_RATE

    @property
    def depth_blocks(self) -> int:
        return len(self._buf)

    @property
    def trim_high_ms(self) -> float:
        return self.target_ms + self.TRIM_MARGIN_MS

    @property
    def jitter_ms(self) -> float:
        return self._jitter_ms

    def set_mode(self, mode: str) -> str:
        """切换延迟档位（运行时生效，不中断播放）。

        只改目标的上下限，当前深度仍由 _adapt 以 2 ms/s 的速度平滑移动过去，
        所以切档时不会听到突变。返回实际生效的档位名。
        """
        if mode not in self.MODE_PROFILES:
            mode = "auto"
        self.mode = mode
        self.min_ms, self.max_ms = self.MODE_PROFILES[mode]
        # 新档位可能容不下当前目标，先夹一次（仍受 SLEW 限制）
        clamped = min(max(self.target_ms, self.min_ms), self.max_ms)
        self._desired_ms = clamped
        return self.mode

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._samples = 0
            self.underloads = self.overflows = self.trims = 0
            self.concealed = self.corrections = 0
            self.pushed = self.pulled = 0
            self._jitter_ms = 0.0
            self._arrival_ema = None
            self._last_arrival = None
            self._adapt_at = None
            self._win_under = self._win_trim = 0
            self._last_raise_push = -1000
            self.drift_ppm = 0.0
            self._concealing = False
            self._have_data = False
            self._last_sample = np.zeros(CHANNELS, dtype=np.float32)
            self._corr_debt = 0.0
            self._hist[:] = 0
            self._hist_n = 0
            self.pitch_hz = 0.0
            self._gap_samples = 0

    # -- 写入（接收线程）
    def push(self, frame, now: Optional[float] = None) -> None:
        arr = np.asarray(frame)
        if arr.dtype != np.int16:
            arr = np.clip(arr, -32768, 32767).astype(np.int16)
        if arr.ndim == 1:
            arr = arr.reshape(-1, CHANNELS)
        arr = np.ascontiguousarray(arr)
        if now is None:
            now = time.monotonic()
        with self._lock:
            self._note_arrival(now)
            self._buf.append(arr)
            self._samples += arr.shape[0]
            self.pushed += 1
            depth = self._samples * 1000.0 / SAMPLE_RATE
            # 溢出：一次丢到当前目标深度以内（按样本裁，突发后仍留满一个目标）
            if depth > self.OVERFLOW_MS:
                self.overflows += 1
                excess_ms = depth - self.target_ms
                if excess_ms > 0:
                    self._drop_samples(int(math.ceil(excess_ms * SAMPLE_RATE / 1000.0)))
            elif depth > self.trim_high_ms:
                self._pop_block()
                self.trims += 1
            # 自适应目标每 ≈1 s 调一次（限速 2 ms/s，带死区）
            if self._adapt_at is None:
                self._adapt_at = now
            elif now - self._adapt_at >= 1.0:
                self._adapt(now)

    # -- 自适应目标（每 ≈1 s 一次，带死区与限速）
    def _note_arrival(self, now: float) -> None:
        last = self._last_arrival
        self._last_arrival = now
        if last is None:
            return
        dt = now - last
        if dt <= 0.002 or dt > 1.0:      # 突发、停顿或时钟回跳，不参与估计
            return
        nominal = FRAME_MS / 1000.0
        dev = abs(dt - nominal) * 1000.0
        if self._arrival_ema is None:
            self._arrival_ema = dt
            self._jitter_ms = dev
        else:
            self._arrival_ema += 0.05 * (dt - self._arrival_ema)
            self._jitter_ms += 0.02 * (dev - self._jitter_ms)
        if self._arrival_ema and self._arrival_ema > 0:
            self.drift_ppm = (nominal / self._arrival_ema - 1.0) * 1e6

    def _adapt(self, now: float) -> None:
        self._adapt_at = now
        d_under = self.underloads - self._win_under
        d_trim = self.trims - self._win_trim
        self._win_under = self.underloads
        self._win_trim = self.trims
        desired = self.min_ms + self.JITTER_GAIN * self._jitter_ms
        if d_under and (self.pushed - self._last_raise_push) >= 1000:
            # 刚刚欠载过：往上抬 20 ms，但 10 s 内最多抬一次（防震荡）
            desired = max(desired, self.target_ms + 20.0)
            self._last_raise_push = self.pushed
        elif d_trim > 8:
            # 一直在修剪说明生产端偏快：目标往下压，少留一点延迟
            desired = min(desired, self.target_ms - 10.0)
        self._desired_ms = min(max(desired, self.min_ms), self.max_ms)
        diff = self._desired_ms - self.target_ms
        if abs(diff) <= self.DEADBAND_MS:
            return
        step = min(abs(diff), self.SLEW_MS_PER_S)
        self.target_ms += math.copysign(step, diff)

    def _drop_samples(self, n: int) -> None:
        """从最旧的一端丢掉 n 个样本（可切在块的中间）。"""
        while n > 0 and self._buf:
            b = self._buf[0]
            if b.shape[0] <= n:
                self._buf.popleft()
                self._samples -= b.shape[0]
                n -= b.shape[0]
            else:
                self._buf[0] = b[n:]
                self._samples -= n
                n = 0

    def _pop_block(self) -> None:
        if not self._buf:
            return
        b = self._buf.popleft()
        self._samples -= b.shape[0]

    # -- 读取（声卡回调线程）
    def pull(self, n: int) -> np.ndarray:
        n = int(n)
        out = np.zeros((max(0, n), CHANNELS), dtype=np.int16)
        if n <= 0:
            return out
        filled = 0
        with self._lock:
            while filled < n and self._buf:
                b = self._buf[0]
                take = min(n - filled, b.shape[0])
                out[filled:filled + take] = b[:take]
                if take == b.shape[0]:
                    self._buf.popleft()
                else:
                    self._buf[0] = b[take:]
                self._samples -= take
                filled += take
            self.pulled += 1
            if filled < n:
                self.underloads += 1
                self._conceal(out, filled, n)
            else:
                if self._concealing:
                    self._fade_in(out, n)
                    self._concealing = False
                    self._gap_samples = 0
                self._have_data = True
            self._drift_fix(out)
        self._last_sample = out[-1].astype(np.float32)
        self._record_hist(out, filled)
        return out

    # -- 历史记录（供基音外推用）
    def _record_hist(self, out: np.ndarray, n: int) -> None:
        if n <= 0:
            return
        seg = out[max(0, n - self.HIST_SAMPLES):n]
        k = seg.shape[0]
        if k >= self.HIST_SAMPLES:
            self._hist[:] = seg[-self.HIST_SAMPLES:]
            self._hist_n = self.HIST_SAMPLES
            return
        self._hist[:-k] = self._hist[k:]
        self._hist[-k:] = seg
        self._hist_n = min(self.HIST_SAMPLES, self._hist_n + k)

    # -- 丢帧隐藏
    def _estimate_period(self):
        """从历史里找基音周期（80~400 Hz），返回 (周期样本数, 归一化相关)。

        用 FFT 算自相关（O(n log n)），只在欠载的那一刻调用，不影响正常路径。
        找不到可信周期时返回 (0, 0.0)，调用方退回淡出。
        """
        n = self._hist_n
        lo = int(SAMPLE_RATE / self.PITCH_MAX_HZ)     # 400 Hz -> 120
        hi = int(SAMPLE_RATE / self.PITCH_MIN_HZ)     # 80 Hz  -> 600
        if n < hi * 2:
            return 0, 0.0
        seg = self._hist[n - hi * 2:n].astype(np.float32).mean(axis=1)
        seg = seg - seg.mean()
        if not np.any(seg):
            return 0, 0.0
        m = 1
        while m < 2 * seg.shape[0]:
            m <<= 1
        spec = np.fft.rfft(seg, m)
        ac = np.fft.irfft(spec * np.conj(spec), m)[:hi + 1]
        if ac[0] <= 0:
            return 0, 0.0
        ac = ac / ac[0]
        window = ac[lo:hi + 1]
        if window.size == 0:
            return 0, 0.0
        # 取「相关度接近最大值里最短的那个周期」，避免把 440 Hz 认成 220 Hz 的倍周期错误
        best = float(np.max(window))
        k = int(np.argmax(window)) + lo
        if best > 0:
            ok = np.nonzero(window >= best * 0.92)[0]
            if ok.size:
                k = int(ok[0]) + lo
        corr = float(ac[k])
        if corr < self.PITCH_MIN_CORR:
            return 0, corr
        # 抛物线插值，取亚样本精度，避免周期偏一点点导致音高发抖
        if 0 < k < hi:
            a, b, c = ac[k - 1], ac[k], ac[k + 1]
            den = a - 2 * b + c
            if abs(den) > 1e-9:
                k = int(round(k + 0.5 * (a - c) / den))
        return max(lo, min(hi, k)), corr

    def _conceal(self, out: np.ndarray, start: int, n: int) -> None:
        """欠载：优先用基音周期外推（NetEq expand 的简化版），否则退淡出。

        外推只补一个周期的整数倍，并且从断点算起 60 ms 内逐渐衰减到 0 —— 这样
        短丢包听不出来（延续基频），长丢包也不会一直"嗡嗡"重复。
        """
        if not self._have_data:
            self._concealing = True          # 还没播过任何东西，保持纯静音
            return
        if not self._concealing:
            self.concealed += 1
            self._concealing = True
        gap = n - start
        if gap <= 0:
            return
        period, corr = self._estimate_period()
        if period:
            self.pitch_hz = round(SAMPLE_RATE / float(period), 1)
            tail = self._hist[self._hist_n - period:self._hist_n].astype(np.float32)
            reps = int(math.ceil(gap / float(period))) + 1
            ext = np.tile(tail, (reps, 1))[:gap]
            # 衰减：60 ms 时间常数，短丢包（<30 ms）几乎听不出接缝
            t = (self._gap_samples + np.arange(gap, dtype=np.float32)) / float(SAMPLE_RATE)
            env = np.exp(-t / 0.060)
            ext = ext * env[:, None]
            # 接缝处理：起手 1 ms 从断点样本线性过渡到外推波形，避免相位误差造成的"咔"
            blend = min(self.MERGE_SAMPLES, gap)
            if blend > 0:
                prev = out[start - 1].astype(np.float32) if start > 0 else self._last_sample
                ramp = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None]
                ext[:blend] = prev[None, :] * (1.0 - ramp) + ext[:blend] * ramp
            out[start:start + gap] = np.clip(np.rint(ext), -32768, 32767).astype(np.int16)
            self.extrapolated += 1
            self._gap_samples += gap
            return
        # 找不到周期：退回原淡出（保证不会比之前更差）
        self.pitch_hz = 0.0
        fade = min(self.FADE_SAMPLES, gap)
        if fade <= 0:
            return
        last = out[start - 1].astype(np.float32) if start > 0 else self._last_sample
        idx = np.arange(fade, dtype=np.float32)
        env = np.exp(-4.0 * idx / float(self.FADE_SAMPLES))      # 尾部只剩 ~1.8%
        vals = last[None, :] * env[:, None]
        out[start:start + fade] = np.clip(np.rint(vals), -32768, 32767).astype(np.int16)

    def _fade_in(self, out: np.ndarray, n: int) -> None:
        """恢复：开头 5 ms 线性淡入，和欠载时留下的静音接上。"""
        fade = min(self.FADE_SAMPLES, n)
        if fade <= 0:
            return
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
        prev = self._last_sample
        out[:fade] = np.clip(np.rint(out[:fade].astype(np.float32) * ramp + prev * (1.0 - ramp)),
                             -32768, 32767).astype(np.int16)

    # -- 时钟漂移微调
    def _drift_fix(self, out: np.ndarray) -> None:
        """按 ppm 估计每秒最多插/删 1 个样本；0.5% 的大偏差由修剪兜底。"""
        n = out.shape[0]
        if n < 8 or not self.drift_ppm:
            return
        rate = self.drift_ppm / 1e6 * SAMPLE_RATE
        rate = max(-self.MAX_CORRECTION_PER_S, min(self.MAX_CORRECTION_PER_S, rate))
        self._corr_debt += rate * (n / float(SAMPLE_RATE))
        while self._corr_debt >= 1.0:
            out[:-1] = out[1:].copy()        # 生产端偏快：丢 1 个样本
            out[-1] = out[-2]
            self._corr_debt -= 1.0
            self.corrections += 1
        while self._corr_debt <= -1.0:
            out[1:] = out[:-1].copy()        # 生产端偏慢：插 1 个样本
            out[0] = out[1]
            self._corr_debt += 1.0
            self.corrections += 1

    def prefill_to(self, ms: float) -> None:
        """补静音到指定深度（开播时先垫一层，避免第一帧就欠载）。"""
        need = int(round(ms * SAMPLE_RATE / 1000.0 / self.block))
        with self._lock:
            while len(self._buf) < need:
                self._buf.append(np.zeros((self.block, CHANNELS), dtype=np.int16))
                self._samples += self.block

    def stats(self) -> dict:
        return {
            "depth_ms": round(self.depth_ms, 1),
            "blocks": len(self._buf),
            "underloads": self.underloads,
            "overflows": self.overflows,
            "trims": self.trims,
            "target_ms": round(self.target_ms, 1),
            "trim_high_ms": round(self.trim_high_ms, 1),
            "jitter_ms": round(self._jitter_ms, 2),
            "drift_ppm": round(self.drift_ppm, 1),
            "concealed": self.concealed,
            "corrections": self.corrections,
            "mode": self.mode,
            "mode_min_ms": self.min_ms,
            "mode_max_ms": self.max_ms,
            "pitch_hz": self.pitch_hz,
            "extrapolated": self.extrapolated,
        }


# ---------------------------------------------------------------- 接收端

class MicReceiver:
    """收 50505 的 UDP 音频，重组帧并放进 JitterBuffer。"""

    def __init__(self, port: int = MIC_PORT, bind_ip: str = "0.0.0.0",
                 jitter: Optional[JitterBuffer] = None, rcvbuf: int = 8 << 20):
        self.port = int(port)
        self.bind_ip = bind_ip
        self.rcvbuf = int(rcvbuf)
        self.jitter = jitter if jitter is not None else JitterBuffer()
        self.assembler = FrameAssembler()
        self.sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self.packets = 0
        self.frames = 0
        self.bad_packets = 0
        self.bytes = 0
        self.rms = 0.0
        self.dbfs = -120.0
        self.peak = 0
        self.sources: set = set()
        self._times: deque = deque(maxlen=120)
        self.last_rx = 0.0

    # -- 生命周期
    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        except OSError:
            pass
        try:
            s.bind((self.bind_ip, self.port))
        except OSError as exc:
            s.close()
            raise OSError(
                "绑定 UDP %s:%d 失败（%s）。常见原因：另一个程序（旧脚本/上一个实例）"
                "已占用该端口，或防火墙拦截。" % (self.bind_ip, self.port, exc)
            ) from exc
        s.settimeout(0.2)
        self.sock = s

    def start(self) -> None:
        self.open()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MicReceiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def close(self) -> None:
        self.stop()
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def reset(self) -> None:
        self.packets = self.frames = self.bad_packets = self.bytes = 0
        self.rms = 0.0
        self.dbfs = -120.0
        self.peak = 0
        self._times.clear()
        self.assembler.reset()
        self.jitter.reset()

    # -- 线程体
    def _reopen(self) -> None:
        """关掉当前 socket 再绑一次。失败就下次再说，绝不在这里抛出去。"""
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        try:
            self.open()
        except OSError:
            pass
        # 重置看门狗计时，避免刚重开就又被判成"没数据"而无限重开
        self.last_rx = time.monotonic()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.sock is None:
                try:
                    self.open()
                except OSError:
                    if self._stop.wait(1.0):
                        break
                    continue
                continue
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                # 看门狗：板子重烧/换口之后，旧 socket 会"活着但收不到任何东西"
                if self.last_rx and (time.monotonic() - self.last_rx) > 3.0:
                    self._reopen()
                continue
            except OSError:
                self._reopen()
                if self._stop.wait(0.3):
                    break
                continue
            if not data or len(data) < HEADER_BYTES or data[:2] != MAGIC:
                self.bad_packets += 1
                continue
            try:
                pkt = unpack_packet(data)
            except ProtocolError:
                self.bad_packets += 1
                continue
            self.packets += 1
            self.bytes += len(data)
            self.sources.add(addr[0])
            self.last_rx = time.monotonic()
            frame = self.assembler.push(pkt)
            if frame is None:
                continue
            self.frames += 1
            self._times.append(self.last_rx)
            rms, dbfs = rms_dbfs(frame)
            self.rms = rms
            self.dbfs = dbfs
            self.peak = int(np.max(np.abs(frame.astype(np.int32)))) if frame.size else 0
            self.jitter.push(frame, now=self.last_rx)

    # -- 统计
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        dt = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / dt if dt > 0 else 0.0

    def snapshot(self) -> dict:
        j = self.jitter.stats()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "frames": self.frames,
            "packets": self.packets,
            "bad_packets": self.bad_packets,
            "bytes": self.bytes,
            "fps": round(self.fps(), 2),
            "rms": round(self.rms, 5),
            "dbfs": round(self.dbfs, 1),
            "peak": self.peak,
            "lost": self.assembler.lost,
            "dups": self.assembler.dups,
            "late": self.assembler.late,
            "sources": sorted(self.sources),
            "last_age": round(time.monotonic() - self.last_rx, 2) if self.last_rx else None,
            "depth_ms": j["depth_ms"],
            "underloads": j["underloads"],
            "overflows": j["overflows"],
            "trims": j["trims"],
            "target_ms": j["target_ms"],
            "trim_high_ms": j["trim_high_ms"],
            "jitter_ms": j["jitter_ms"],
            "drift_ppm": j["drift_ppm"],
            "concealed": j["concealed"],
            "corrections": j["corrections"],
            "mode": j["mode"],
            "mode_min_ms": j["mode_min_ms"],
            "mode_max_ms": j["mode_max_ms"],
            "pitch_hz": j["pitch_hz"],
            "extrapolated": j["extrapolated"],
        }

    def set_mode(self, mode: str) -> str:
        """切换延迟档位（auto / low / stable），立即返回实际生效的档位名。"""
        return self.jitter.set_mode(mode)


# ---------------------------------------------------------------- 发送端

class BoardSender:
    """按 100 帧/秒把 PCM 推给板子（默认 50506）。"""

    def __init__(self, dest: str = None, port: int = BOARD_PORT,
                 bind_ip: Optional[str] = None, total: int = TOTAL_SAMPLES,
                 prime_frames: int = 2):
        # dest=None → 用 board_bcast() 解析出真正能到板子的广播地址。
        # 音频**只发一个**地址：同一帧到两次会把包间节拍打乱（固件按 5 ms 节拍判断 stall）。
        self.dest = dest or board_bcast()
        self.port = int(port)
        self.bind_ip = bind_ip
        self.total = int(total)
        self.prime_frames = max(0, int(prime_frames))
        self.sock: Optional[socket.socket] = None
        self.seq = 0
        self.frames_sent = 0
        self.packets_sent = 0
        self.bytes_sent = 0
        self.errors = 0
        self.resyncs = 0

    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.bind_ip:
            s.bind((self.bind_ip, 0))       # 绑本地 IP = 选网卡
        s.settimeout(0.5)
        self.sock = s

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_frame(self, frame, seq: Optional[int] = None) -> int:
        """发一帧（两个包），返回发出的字节数。"""
        self.open()
        if seq is None:
            seq = self.seq
        p0, p1 = pack_frame(frame, seq, self.total)
        n = 0
        for p in (p0, p1):
            try:
                n += self.sock.sendto(p, (self.dest, self.port))
                self.packets_sent += 1
            except OSError:
                self.errors += 1
                raise
        self.bytes_sent += n
        self.frames_sent += 1
        self.seq = (seq + 1) & 0xFFFF
        return n

    @staticmethod
    def seconds_to_frames(seconds: float) -> int:
        return int(round(seconds * SAMPLE_RATE / FRAME_SAMPLES))

    def play_array(self, audio, stop_event: Optional[threading.Event] = None,
                   progress: Optional[Callable[[float, float], None]] = None,
                   fps: float = 100.0, realtime: bool = True,
                   prime_frames: Optional[int] = None) -> dict:
        """按 100 帧/秒把 (N,2) int16 音频推给板子。

        progress(已发秒数, 总秒数) 每 5 帧回调一次（GUI 用）。
        落后超过 150 ms 时重排时间基准，而不是突发补发 —— 突发会把板子 4×100 ms
        的播放缓冲冲掉（固件在 pb_wr 回绕时不检查该块是否正在播放）。
        """
        arr = np.asarray(audio)
        if arr.dtype != np.int16:
            arr = np.clip(arr, -32768, 32767).astype(np.int16)
        if arr.ndim == 1:
            arr = np.repeat(arr.reshape(-1, 1), CHANNELS, axis=1)
        if arr.shape[1] != CHANNELS:
            raise ValueError("只支持立体声，收到 %d 声道" % arr.shape[1])
        n_frames = arr.shape[0] // FRAME_SAMPLES
        if n_frames == 0:
            return {"frames": 0, "seconds": 0.0, "errors": 0, "resyncs": 0}
        total_s = n_frames / (SAMPLE_RATE / FRAME_SAMPLES)
        stop_event = stop_event or threading.Event()
        prime = self.prime_frames if prime_frames is None else max(0, int(prime_frames))
        prime = min(prime, n_frames)

        self.open()
        t0 = time.perf_counter()
        period = 1.0 / float(fps)
        sent = 0

        # 预热：先把 prime 帧灌进去，让板子有 1~2 个 100 ms 缓冲垫底
        for i in range(prime):
            if stop_event.is_set():
                break
            self.send_frame(arr[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])
            sent += 1
        t0 = time.perf_counter()

        for i in range(prime, n_frames):
            if stop_event.is_set():
                break
            target = t0 + (i - prime) * period
            now = time.perf_counter()
            if now < target:
                stop_event.wait(target - now)      # 可被"停止"打断的 sleep
                now = time.perf_counter()
            elif now - target > 0.150:
                t0 = now - (i - prime) * period    # 落后太多就重排，不补发
                self.resyncs += 1
            try:
                self.send_frame(arr[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])
            except OSError:
                break
            sent += 1
            if progress is not None and (i % 5 == 0 or i + 1 == n_frames):
                try:
                    progress(sent / (SAMPLE_RATE / FRAME_SAMPLES), total_s)
                except Exception:
                    pass
        return {
            "frames": sent,
            "seconds": round(sent / (SAMPLE_RATE / FRAME_SAMPLES), 3),
            "total_seconds": round(total_s, 3),
            "errors": self.errors,
            "resyncs": self.resyncs,
            "stopped": stop_event.is_set(),
        }

    def play_wav(self, path: str, **kw) -> dict:
        audio, info = read_wav(path)
        kw.setdefault("progress", None)
        result = self.play_array(audio, **kw)
        result["wav"] = info
        return result

    def play_tone(self, seconds: float = 10.0, freq: float = 440.0,
                  amp: float = 0.25, **kw) -> dict:
        return self.play_array(tone(freq, seconds, amp), **kw)


# ---------------------------------------------------------------- WAV / 重采样

def wav_info(path) -> dict:
    """只读文件头（快，不加载 PCM）。"""
    with wave.open(str(path), "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        nf = w.getnframes()
    return {
        "path": str(path),
        "channels": nch,
        "sampwidth": sw,
        "rate": fr,
        "frames": nf,
        "duration": (nf / fr) if fr else 0.0,
    }


def _raw_to_int16(raw: bytes, sampwidth: int) -> np.ndarray:
    """各种位深的 PCM 转成 int16（多声道仍按原样交织）。"""
    if sampwidth == 1:
        u = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
        return ((u - 128) << 8).astype(np.int16)
    if sampwidth == 2:
        return np.frombuffer(raw, dtype="<i2").copy()
    if sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8)
        b = b[:len(b) - len(b) % 3].reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = (v << 8) >> 8                       # 符号扩展
        return np.clip(v >> 8, -32768, 32767).astype(np.int16)
    if sampwidth == 4:
        v = np.frombuffer(raw, dtype="<i4").astype(np.int64)
        return np.clip(v >> 16, -32768, 32767).astype(np.int16)
    raise ValueError("不支持的采样位宽：%d 字节" % sampwidth)


def _box_lowpass(x: np.ndarray, taps: int) -> np.ndarray:
    """降采样前的简易抗混叠：taps 点滑动平均（用累积和，O(n)）。"""
    if taps < 2:
        return x
    c = np.cumsum(x.astype(np.float64), axis=0)
    c = np.vstack([np.zeros((1, x.shape[1])), c])
    out = np.empty_like(x, dtype=np.float64)
    half = taps // 2
    n = x.shape[0]
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + taps - half)
        out[i] = (c[b] - c[a]) / (b - a)
    return out


def resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性插值重采样，分块处理以控制内存占用。x: (n, ch) int16 -> (m, ch) int16。"""
    if src_rate == dst_rate or x.size == 0:
        return x.astype(np.int16, copy=True)
    src_n, ch = x.shape
    dst_n = int(round(src_n * dst_rate / src_rate))
    if dst_n <= 0:
        return np.zeros((0, ch), dtype=np.int16)

    src = x
    if src_rate > dst_rate:                      # 先抗混叠
        taps = max(2, int(round(src_rate / dst_rate)))
        if taps > 2:
            src = _box_lowpass(x, taps)

    out = np.empty((dst_n, ch), dtype=np.int16)
    ratio = src_rate / float(dst_rate)
    step = 1 << 15                               # 每块 32768 输出样本
    for start in range(0, dst_n, step):
        end = min(dst_n, start + step)
        pos = np.arange(start, end, dtype=np.float64) * ratio
        idx = np.floor(pos).astype(np.int64)
        frac = (pos - idx)[:, None]
        i0 = np.clip(idx, 0, src_n - 1)
        i1 = np.clip(idx + 1, 0, src_n - 1)
        a = src[i0].astype(np.float32)
        b = src[i1].astype(np.float32)
        y = a * (1.0 - frac) + b * frac
        out[start:end] = np.clip(np.rint(y), -32768, 32767).astype(np.int16)
    return out


def read_wav(path, target_rate: int = SAMPLE_RATE) -> tuple:
    """读 WAV 并转成板子要的 48 kHz / 16 bit / 立体声，帧长对齐到 480 样本。

    返回 ((N,2) int16, info)。info 里记录原始参数与做过的转换。
    """
    with wave.open(str(path), "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        nf = w.getnframes()
        raw = w.readframes(nf)

    info = {
        "path": str(path), "channels": nch, "sampwidth": sw, "rate": fr,
        "frames": nf, "duration_src": (nf / fr) if fr else 0.0,
        "resampled": False, "mono_to_stereo": False, "trimmed_channels": False,
        "padded": False,
    }
    x = _raw_to_int16(raw, sw)
    x = x[:len(x) - len(x) % nch]
    x = x.reshape(-1, nch)

    if nch == 1:
        x = np.repeat(x, CHANNELS, axis=1)
        info["mono_to_stereo"] = True
    elif nch > CHANNELS:
        x = x[:, :CHANNELS]
        info["trimmed_channels"] = True

    if fr != target_rate:
        x = resample_linear(x, fr, target_rate)
        info["resampled"] = True
    else:
        x = x.astype(np.int16, copy=False)

    pad = (-x.shape[0]) % FRAME_SAMPLES
    if pad:
        x = np.vstack([x, np.zeros((pad, CHANNELS), dtype=np.int16)])
        info["padded"] = True

    info["rate_out"] = target_rate
    info["duration"] = x.shape[0] / float(target_rate)
    info["frames_out"] = x.shape[0]
    return np.ascontiguousarray(x), info


def write_wav(path, frames, rate: int = SAMPLE_RATE) -> str:
    """把 (N,2) int16 写成 48 kHz / 16 bit / 立体声 WAV。"""
    arr = np.asarray(frames)
    if arr.dtype != np.int16:
        arr = np.clip(arr, -32768, 32767).astype(np.int16)
    if arr.ndim == 1:
        arr = np.repeat(arr.reshape(-1, 1), CHANNELS, axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(arr.shape[1])
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(np.ascontiguousarray(arr).astype("<i2").tobytes())
    return str(path)


def tone(freq: float = 440.0, seconds: float = 10.0, amp: float = 0.25,
         rate: int = SAMPLE_RATE) -> np.ndarray:
    """生成正弦测试音，(N,2) int16，长度对齐到整帧。"""
    n = int(round(seconds * rate))
    n += (-n) % FRAME_SAMPLES
    t = np.arange(n, dtype=np.float64) / float(rate)
    v = np.sin(2.0 * np.pi * freq * t) * (amp * 32767.0)
    x = np.clip(np.rint(v), -32768, 32767).astype(np.int16)
    return np.repeat(x.reshape(-1, 1), CHANNELS, axis=1)


# ---------------------------------------------------------------- 控制面（V2）
#
# 报文：b"CTL" + 1 字节命令码 + 可选参数（MODE 1 字节，TEXT utf-8 文本）。
# 走和音频同一个端口 50506（广播）。板子固件目前只认 b"AU" 开头的包，
# 用 data[0:2] != b"AU" 判掉，所以这些包现在会被**安全忽略**，不会破坏音频。

CTL_MAGIC = b"CTL"
CTL_MUTE = 1
CTL_UNMUTE = 2
CTL_GAIN_UP = 3
CTL_GAIN_DOWN = 4
CTL_MODE = 5
CTL_TEXT = 6

CTL_NAMES = {
    CTL_MUTE: "MUTE",
    CTL_UNMUTE: "UNMUTE",
    CTL_GAIN_UP: "GAIN+",
    CTL_GAIN_DOWN: "GAIN-",
    CTL_MODE: "MODE",
    CTL_TEXT: "TEXT",
}

_CTL_ALIASES = {
    "MUTE": CTL_MUTE,
    "UNMUTE": CTL_UNMUTE,
    "GAIN+": CTL_GAIN_UP,
    "GAIN_UP": CTL_GAIN_UP,
    "GAIN-": CTL_GAIN_DOWN,
    "GAIN_DOWN": CTL_GAIN_DOWN,
    "MODE": CTL_MODE,
    "TEXT": CTL_TEXT,
    "MSG": CTL_TEXT,
}

CTL_TEXT_MAX = 32


def pack_control(cmd, arg=None) -> bytes:
    """组一条控制命令；cmd 可以是 "MUTE"/"GAIN+"/"MODE"/"TEXT" 或命令码。"""
    if isinstance(cmd, str):
        key = cmd.strip().upper()
        if key not in _CTL_ALIASES:
            raise ValueError("未知控制命令：%r（可用：%s）"
                             % (cmd, ", ".join(sorted(_CTL_ALIASES))))
        code = _CTL_ALIASES[key]
    else:
        code = int(cmd)
        if code not in CTL_NAMES:
            raise ValueError("未知控制命令码：%r" % (cmd,))

    payload = b""
    if code == CTL_MODE:
        v = 0 if arg is None else int(arg)
        if not 0 <= v <= 255:
            raise ValueError("MODE 参数必须在 0..255，收到 %r" % (arg,))
        payload = bytes([v])
    elif code == CTL_TEXT:
        payload = str("" if arg is None else arg).encode("utf-8")[:CTL_TEXT_MAX]
    elif arg is not None:
        payload = bytes([int(arg) & 0xFF])
    return CTL_MAGIC + bytes([code]) + payload


def unpack_control(data: bytes) -> tuple:
    """解析控制命令，返回 (code, arg)；不合法时抛 ProtocolError。"""
    if len(data) < 4:
        raise ProtocolError("控制包太短：%d 字节" % len(data))
    if data[:3] != CTL_MAGIC:
        raise ProtocolError("控制包 magic 不是 b'CTL'：%r" % (data[:3],))
    code = data[3]
    if code not in CTL_NAMES:
        raise ProtocolError("未知控制命令码：%d" % code)
    body = data[4:]
    if code == CTL_TEXT:
        return code, body.decode("utf-8", "replace")
    return code, (body[0] if body else None)


# ---------------------------------------------------------------- 固件控制面（真机协议）
#
# 上面那套 b"CTL" 帧是 PC 端早期拟的协议，**板子固件从来没有实现过**。
# udp_link.c 里 PORT_CTL = 50507，只解析 payload 的**首字节**：
#
#     '0' 旁路          '1' RNNoise        '2' 轻量噪声门
#     'a' 底噪关        'b' 300            'c' 800      'd' 1500
#     't' 自检音（440 Hz）翻转
#
# 两个真机约束，写代码时必须记住：
#   1) 控制报文是**广播**，会被重复投递 2~3 次 —— 所以命令必须**幂等**。
#      0/1/2 和 a~d 都是"设成某值"，重复投递无害；只有 't' 是翻转，
#      演示时别连按（这是固件里唯一没做成幂等的命令）。
#   2) 固件只看首字节，多余字节会被忽略，但不要依赖这个行为。

FW_CTL_PORT = 50507
FW_DSP = {"bypass": b"0", "rnnoise": b"1", "gate": b"2", "pl": b"3"}
FW_NOISE = {0: b"a", 300: b"b", 800: b"c", 1500: b"d"}
FW_TONE = b"t"
# 延迟档（2026-10-29 起三个模式共用同一个数字）：
#   'j' = 设成 20 帧（200 ms），'i' = 设成 30 帧（300 ms）。
#
# 注意这里**不再是"升一档/降一档"**。控制报文是 UDP 广播，板子会收到 2~3 份，
# 步进命令一次点击就被执行两三次、档位直接跳过目标值 —— 这是"延迟设置没反应"
# 的主要原因。固件侧 num_set_latency() 已经改成"设成某值 + 同值直接返回"，
# 所以重复投递变成空操作。新增档位时也只加字节，不要再加步进语义。
FW_RN_TARGET = {20: b"j", 30: b"i"}


def fw_ctl_frame(cmd, arg=None) -> bytes:
    """把命令编成固件认的单字节。

    cmd: "bypass" / "gate" / "rnnoise" / "pl" / "noise"(arg=0/300/800/1500) /
         "tone" / "rntarget"(arg=20 或 30，RNNoise 批处理的延迟档)
    """
    key = str(cmd).strip().lower()
    if key in FW_DSP:
        return FW_DSP[key]
    if key == "noise":
        level = 0 if arg is None else int(arg)
        if level not in FW_NOISE:
            raise ValueError("底噪只有 0/300/800/1500，收到 %r" % (arg,))
        return FW_NOISE[level]
    if key == "tone":
        return FW_TONE
    if key == "rntarget":
        step = int(arg)
        if step not in FW_RN_TARGET:
            raise ValueError("RNNoise 延迟档只有 20/30 帧，收到 %r" % (arg,))
        return FW_RN_TARGET[step]
    raise ValueError("未知固件命令：%r（可用：bypass/gate/rnnoise/pl/noise/tone/rntarget）" % (cmd,))


class BoardCtl:
    """固件控制面客户端：单字节 → 255.255.255.255:50507。

    与 BoardControl 的区别：BoardControl 发的是 b"CTL"+码 的**多字节帧**（PC 端 V2
    拟定，固件未实现，发过去会被当音频包丢掉）；这个类发的是固件真正解析的单字节。
    """

    def __init__(self, dest: Optional[str] = None, port: int = FW_CTL_PORT,
                 bind_ip: Optional[str] = None):
        # dest=None 表示"该发的地方都发一遍"（见 ctl_targets 的注释）
        self.dest = dest
        self.port = int(port)
        self.bind_ip = bind_ip
        self.sock: Optional[socket.socket] = None
        self.sent = 0
        self.errors = 0
        self.last_hex = ""
        self.last_dest: list = []

    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.bind_ip:
            s.bind((self.bind_ip, 0))
        self.sock = s

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_bytes(self, data: bytes) -> dict:
        data = bytes(data)
        self.open()
        targets = [self.dest] if self.dest else ctl_targets()
        ok = 0
        last_err: Optional[OSError] = None
        for t in targets:
            try:
                self.sock.sendto(data, (t, self.port))
                ok += 1
            except OSError as exc:
                # 单个目标发不出去不算失败：多目标就是为了"总有一个能到板子"，
                # 例如机器上没有 169.254 网段时那条会报 ENETUNREACH。
                last_err = exc
        if ok == 0:
            self.errors += 1
            raise last_err if last_err is not None else OSError("控制包发送失败")
        self.sent += 1
        self.last_dest = list(targets)
        self.last_hex = " ".join("%02X" % b for b in data)
        return {"bytes": len(data), "hex": self.last_hex,
                "byte": data[0] if data else None,
                "char": chr(data[0]) if data else "",
                "dest": ",".join(targets), "delivered": ok}

    def dsp(self, name: str) -> dict:
        info = self.send_bytes(fw_ctl_frame(name))
        info["cmd"] = "dsp=%s" % name
        return info

    def noise(self, level: int) -> dict:
        info = self.send_bytes(fw_ctl_frame("noise", level))
        info["cmd"] = "noise=%d" % int(level)
        return info

    def tone(self) -> dict:
        info = self.send_bytes(fw_ctl_frame("tone"))
        info["cmd"] = "tone(on)"
        return info

    def tone_off(self) -> dict:
        info = self.send_bytes(b"T")
        info["cmd"] = "tone(off)"
        return info

    # ---- 增益与延迟（11-02 补：固件早就有，PC 端一直没暴露）----
    GAIN = {0: b"e", -3: b"f", -6: b"g", -12: b"h"}   # Q15 32768/24576/16384/8192
    LAG = {30: b"i", 20: b"j"}                          # 延迟阶梯（帧）

    def gain(self, db: int) -> dict:
        """增益档位：0 / -3 / -6 / -12 dB（超出范围按最近档）。"""
        db = min(self.GAIN, key=lambda k: abs(k - int(db)))
        info = self.send_bytes(self.GAIN[db])
        info.update(gain_db=db)
        return info

    def lat(self, frames: int, on: bool = True) -> dict:
        """延迟开关 + 档位。

        固件语义（11-02 用探针实测确认）：'k' 开（恢复上次档位）、'K' 关（dly 归 0）、
        'i' = 30 帧、'j' = 20 帧；**关着的时候发 'i'/'j' 不生效**，所以必须先 'k' 再选档。
        """
        frames = min(self.LAG, key=lambda k: abs(k - int(frames)))
        if not on:
            info = self.send_bytes(b"K")
            info.update(lat_frames=0, lat_on=False)
            return info
        self.send_bytes(b"k")
        info = self.send_bytes(self.LAG[frames])
        info.update(lat_frames=frames, lat_on=True)
        return info

    def rntarget(self, frames: int) -> dict:
        """延迟档：20 帧（200 ms）或 30 帧（300 ms）。

        2026-10-29 起**三个模式共用**这个数字：RNNoise 拿它当批处理深度，
        旁路和轻量门拿它当共用延迟线的长度。所以"300 ms"在哪个档都是 300 ms，
        切模式不会把延迟重置成 0，三种模式才能在同等延迟下对比。
        """
        info = self.send_bytes(fw_ctl_frame("rntarget", frames))
        info["cmd"] = "lat=%d" % int(frames)
        return info

    def ts(self, pct: int) -> dict:
        """时长缩放比例（方案 B 的可调版），50..100，单位 %。

        **两字节命令**：`'S'` + 百分比。为什么不是单字节 —— 单字节空间已经
        被占满了（'0'..'2' 模式、'a'..'h' 增益与底噪、'i','j','k','K','n','N',
        't','T'），而直接用百分比字节会和 ASCII 数字撞车（'0'=48 就是模式 0）。

        范围检查故意只做"明显不可能"这一层：合法区间 50..100 由固件裁定，
        越界值会被 `ts_parse_rate()` 拒绝并打日志、保持原值、计数 +1。
        PC 侧不替它兜底 —— 边界只留一份，两边各写一个阈值迟早对不上。
        所以这里允许传 0..255，专门用来测拒绝分支。
        """
        v = int(pct)
        if not (0 <= v <= 255):
            raise ValueError("百分比必须是 0..255 的整数，收到 %r" % (pct,))
        info = self.send_bytes(bytes([ord("S"), v & 0xFF]))
        info["cmd"] = "ts=%d%%" % v
        return info


class BoardControl:
    """把控制命令发到板子（默认广播 50506），并回显实际字节便于对固件。"""

    def __init__(self, dest: str = BROADCAST, port: int = BOARD_PORT,
                 bind_ip: Optional[str] = None):
        self.dest = dest
        self.port = int(port)
        self.bind_ip = bind_ip
        self.sock: Optional[socket.socket] = None
        self.sent = 0
        self.errors = 0
        self.last_hex = ""

    def open(self) -> None:
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if self.bind_ip:
            s.bind((self.bind_ip, 0))
        self.sock = s

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_raw(self, data: bytes) -> dict:
        """发任意字节（调试用），返回 {byte_len, hex, ...}。"""
        data = bytes(data)
        self.open()
        try:
            self.sock.sendto(data, (self.dest, self.port))
        except OSError:
            self.errors += 1
            raise
        self.sent += 1
        self.last_hex = " ".join("%02X" % b for b in data)
        return {"bytes": len(data), "hex": self.last_hex}

    def send(self, cmd, arg=None) -> dict:
        """发一条控制命令，返回 {cmd, code, bytes, hex}。"""
        data = pack_control(cmd, arg)
        info = self.send_raw(data)
        name = CTL_NAMES.get(data[3], "?")
        if name == "MODE" and len(data) > 4:
            name = "MODE %d" % data[4]
        elif name == "TEXT":
            name = "TEXT %r" % data[4:].decode("utf-8", "replace")
        info.update({"cmd": name, "code": data[3]})
        return info


# ---------------------------------------------------------------- 显示 / 录音辅助
#
# 这些工具不依赖 tkinter：回调线程只做"拷贝进队列/环形缓冲"，主线程按
# 25 fps 取数据画图或写盘，保证音频回调不被 GUI 拖慢。

def mono_mix(frames) -> np.ndarray:
    """(N,2) 立体声 -> (N,) 单声道 int16（左/右取平均）。"""
    arr = np.asarray(frames)
    if arr.ndim == 1:
        return arr.astype(np.int16, copy=False)
    if arr.shape[1] == 1:
        return arr[:, 0].astype(np.int16, copy=False)
    l = arr[:, 0].astype(np.int32)
    r = arr[:, 1].astype(np.int32)
    return ((l + r) // 2).astype(np.int16)


def apply_gain(frames, gain: float) -> np.ndarray:
    """播放增益（A/B 演示用），结果饱和到 int16。"""
    arr = np.asarray(frames)
    if gain == 1.0:
        return arr
    y = arr.astype(np.float32) * float(gain)
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


class RingTap:
    """单声道环形缓冲：音频回调写，界面线程读（波形/频谱用）。"""

    def __init__(self, seconds: float = 4.0, rate: int = SAMPLE_RATE):
        self.rate = int(rate)
        self.size = max(1, int(seconds * self.rate))
        self._buf = np.zeros(self.size, dtype=np.int16)
        self._w = 0
        self._len = 0
        self._lock = threading.Lock()
        self.total = 0

    def write(self, frames) -> None:
        mono = mono_mix(frames)
        if mono.size == 0:
            return
        if mono.size >= self.size:
            mono = mono[-self.size:]
        mono = np.ascontiguousarray(mono, dtype=np.int16)
        n = int(mono.size)
        with self._lock:
            w = self._w
            end = w + n
            if end <= self.size:
                self._buf[w:end] = mono
            else:
                first = self.size - w
                self._buf[w:] = mono[:first]
                self._buf[:end - self.size] = mono[first:]
            self._w = end % self.size
            self._len = min(self.size, self._len + n)
            self.total += n

    def read_last(self, n: int) -> np.ndarray:
        """取最近 n 个样本（不足时前面补 0，保证界面不跳）。"""
        n = max(0, int(n))
        out = np.zeros(n, dtype=np.int16)
        if n == 0:
            return out
        with self._lock:
            avail = min(n, self._len)
            if avail <= 0:
                return out
            start = (self._w - avail) % self.size
            if start + avail <= self.size:
                chunk = self._buf[start:start + avail].copy()
            else:
                first = self.size - start
                chunk = np.concatenate([self._buf[start:], self._buf[:avail - first]])
        out[n - avail:] = chunk
        return out

    def clear(self) -> None:
        with self._lock:
            self._buf.fill(0)
            self._w = 0
            self._len = 0


class WaveformView:
    """把一段单声道数据压成 width 列的 min/max（每列一条竖线）。"""

    @staticmethod
    def columns(samples, width: int):
        width = max(1, int(width))
        x = np.asarray(samples)
        if x.ndim > 1:
            x = mono_mix(x)
        x = x.astype(np.int32, copy=False).ravel()
        k = max(1, int(math.ceil(x.size / float(width))))
        need = width * k
        if x.size < need:
            x = np.concatenate([np.zeros(need - x.size, dtype=np.int32), x])
        else:
            x = x[-need:]
        m = x.reshape(width, k)
        return m.min(axis=1), m.max(axis=1)


class SpectrumAnalyzer:
    """64 段对数频谱（40 Hz~16 kHz）+ 峰值保持（默认 6 dB/s 衰减）。"""

    def __init__(self, bands: int = 64, fmin: float = 40.0, fmax: float = 16000.0,
                 nfft: int = 2048, peak_decay_db_per_s: float = 6.0):
        self.bands = int(bands)
        self.fmin = float(fmin)
        self.fmax = float(fmax)
        self.nfft = int(nfft)
        self.peak_decay = float(peak_decay_db_per_s)
        self.edges = np.geomspace(self.fmin, self.fmax, self.bands + 1)
        self.freqs = np.fft.rfftfreq(self.nfft, 1.0 / SAMPLE_RATE)
        self.window = np.hanning(self.nfft).astype(np.float64)
        self._idx = []
        for i in range(self.bands):
            lo, hi = self.edges[i], self.edges[i + 1]
            idx = np.nonzero((self.freqs >= lo) & (self.freqs < hi))[0]
            if idx.size == 0:
                idx = np.array([int(np.argmin(np.abs(self.freqs - 0.5 * (lo + hi))))])
            self._idx.append(idx)
        self.peaks = np.full(self.bands, -120.0)
        self.last = np.full(self.bands, -120.0)

    def band_edges(self) -> np.ndarray:
        return self.edges.copy()

    def band_of(self, freq: float) -> int:
        return int(np.clip(np.searchsorted(self.edges, float(freq)) - 1,
                           0, self.bands - 1))

    def analyze(self, mono, dt_s: float = 0.04):
        """返回 (各段 dB, 峰值保持 dB)。dt_s 是距上次调用的秒数。"""
        x = np.asarray(mono).astype(np.float64).ravel()
        if x.size < self.nfft:
            x = np.concatenate([np.zeros(self.nfft - x.size), x])
        x = x[-self.nfft:] * self.window
        spec = np.abs(np.fft.rfft(x)) / (self.nfft * 0.5)
        db = np.full(self.bands, -120.0)
        for i, idx in enumerate(self._idx):
            db[i] = max(-120.0, 20.0 * math.log10(float(spec[idx].max()) + 1e-9))
        decay = self.peak_decay * max(0.0, float(dt_s))
        self.peaks = np.maximum(db, self.peaks - decay)
        self.last = db
        return db, self.peaks.copy()


class WavRecorder:
    """把"实际播放出去的 PCM"原样写 WAV：回调线程只入队，写盘在后台线程。"""

    def __init__(self, rate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.rate = int(rate)
        self.channels = int(channels)
        self.path: Optional[str] = None
        self.frames = 0
        self.bytes_written = 0
        self.error: Optional[BaseException] = None
        self._w = None
        self._q: "queue.Queue" = queue.Queue()
        self._th: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def recording(self) -> bool:
        return self._w is not None

    @property
    def seconds(self) -> float:
        return self.frames / float(self.rate)

    def start(self, path: Optional[str] = None) -> str:
        if self._w is not None:
            return self.path
        if not path:
            path = os.path.join(os.getcwd(),
                                time.strftime("pc_audio_%Y%m%d_%H%M%S.wav"))
        self._q = queue.Queue()
        self._stop.clear()
        self.frames = 0
        self.bytes_written = 0
        self.error = None
        w = wave.open(str(path), "wb")
        w.setnchannels(self.channels)
        w.setsampwidth(2)
        w.setframerate(self.rate)
        self._w = w
        self.path = str(path)
        self._th = threading.Thread(target=self._run, name="WavRecorder", daemon=True)
        self._th.start()
        return self.path

    def feed(self, frames) -> None:
        """回调线程调用；必须拷贝，声卡缓冲下一轮回调就会被覆盖。"""
        if self._w is None or frames is None:
            return
        arr = np.array(frames, dtype=np.int16, copy=True)
        if arr.shape[1] != self.channels:
            return
        self._q.put(arr)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            try:
                data = np.ascontiguousarray(item, dtype="<i2").tobytes()
                assert self._w is not None
                self._w.writeframesraw(data)
                self.frames += int(item.shape[0])
                self.bytes_written += len(data)
            except BaseException as exc:      # 写盘失败不要炸掉回调线程
                self.error = exc
                break

    def stop(self) -> dict:
        if self._w is None:
            return {"path": None, "seconds": 0.0, "frames": 0, "bytes": 0}
        self._q.put(None)
        self._stop.set()
        t = self._th
        if t and t.is_alive():
            t.join(timeout=2.0)
        w, self._w = self._w, None
        try:
            w.close()
        finally:
            pass
        return {"path": self.path, "seconds": round(self.seconds, 3),
                "frames": self.frames, "bytes": self.bytes_written,
                "error": str(self.error) if self.error else None}
