"""vocoder.py -- LPC 7.2 kbps 参数声码器（`work/eth_audio/sw/vocoder.h` 的 Python 复刻）

为什么要它
----------
板子上跑的那版是裸机 C（QEMU 自测已 PASS）。这个文件把同一套算法搬到 PC，
让**控制台可以直接调用**：把上行音频实时过一遍"只传骨架"的 7.2 kbps 编解码，
用来演示/对比"参数再生"到底听到什么，也给明天板级联调留一个对照实现。

算法与 C 版逐项对应（同一份参数表、同一套量化、同一组门限）：
  分析：预加重 0.9375 → 汉明窗 → 16 阶自相关 → Levinson → 反射系数 8 bit
        → LPC 残差基频（6:1 抽取）→ 清浊门限 0.45 → 整帧 RMS 8 bit
  合成：k→a → 脉冲串/白噪声 → 1/A(z) → 去加重 → 帧级归一化 → 0.85 软限幅
  帧长 20 ms（48 kHz = 960 点），144 bit/帧 = 7200 bps

接口
----
    v = VocoderStream()             # 面向控制台：吃 (n, 2) int16 的 10 ms 块
    out = v.process(block)          # 吐同形状、同长度；延迟正好 1 帧（20 ms）
    v.reset()                       # 停止收听时清状态

    p = Vocoder().analyze(x)        # 单帧分析（x 为 -1..1 的 float，960 点）
    y = voc.synthesize(p)           # 单帧合成

    python -m work.pc_app.vocoder --selftest     # 自检（和 QEMU 那份对齐）
"""

from __future__ import annotations

import math
import time

import numpy as np

RATE = 48000
FRAME = 960                 # 20 ms
ORDER = 16
DEC = 6                     # 基频搜索抽取比：48k / 6 = 8k
LAG_MIN = 20                # 8 kHz 下 20 点 = 400 Hz
LAG_MAX = 133               # 8 kHz 下 133 点 = 60 Hz
GAIN_SCALE = 255.0
VOICED_THRESHOLD = 0.45
BITS_FRAME = 144            # 16×8 + 8 + 8
BITRATE = BITS_FRAME * (RATE // FRAME)      # 7200 bps

_PI = 3.141592653589793
_TWO_PI = 6.283185307179586


# ----------------------------------------------------------------- 参数与工具
class VocParams:
    """一帧的量化参数（结构对应 C 的 VocParams）。"""

    __slots__ = ("k", "gain", "pitch")

    def __init__(self, k=None, gain=0, pitch=0):
        self.k = np.zeros(ORDER, dtype=np.uint8) if k is None else k
        self.gain = int(gain)
        self.pitch = int(pitch)

    @property
    def voiced(self) -> bool:
        return self.pitch > 0

    def bits(self) -> int:
        return BITS_FRAME

    def __repr__(self):
        return "VocParams(gain=%d, pitch=%d%s)" % (
            self.gain, self.pitch, "" if self.pitch else " (unvoiced)")


def _a_to_k(a: np.ndarray) -> np.ndarray:
    """阶降：a[0..16] → k[1..16]（a[0] 必须为 1）。"""
    a = np.array(a, dtype=np.float64, copy=True)
    k = np.zeros(ORDER + 1)
    for m in range(ORDER, 0, -1):
        km = float(a[m])
        if abs(km) > 0.999:
            km = 0.999 if km > 0 else -0.999
        k[m] = km
        d = 1.0 - km * km
        if d < 1e-5:
            d = 1e-5
        for i in range(1, m):
            a[i] = (a[i] - km * a[m - i]) / d
    return k


def _k_to_a(k: np.ndarray) -> np.ndarray:
    """阶升：k[1..16] → a[0..16]，a[0] = 1。"""
    a = np.zeros(ORDER + 1)
    tmp = np.zeros(ORDER + 1)
    a[0] = 1.0
    for m in range(1, ORDER + 1):
        for i in range(1, m):
            tmp[i] = a[i] + k[m] * a[m - i]
        for i in range(1, m):
            a[i] = tmp[i]
        a[m] = k[m]
    return a


def _levinson(r: np.ndarray):
    """Levinson-Durbin。返回 (a, err)；err<=0 时返回 (a, None) 由调用方兜底。"""
    a = np.zeros(ORDER + 1)
    tmp = np.zeros(ORDER + 1)
    e = float(r[0])
    if not (e > 0.0):
        return a, None
    a[0] = 1.0
    for m in range(1, ORDER + 1):
        acc = float(r[m])
        for i in range(1, m):
            acc += a[i] * r[m - i]
        k = -acc / e
        k = max(-0.995, min(0.995, k))
        for i in range(1, m):
            tmp[i] = a[i] + k * a[m - i]
        for i in range(1, m):
            a[i] = tmp[i]
        a[m] = k
        e *= (1.0 - k * k)
        if e < 1e-10:
            e = 1e-10
            break
    return a, e


class _Rand:
    """xorshift32，与 C 版 voc_rand 同序列。"""

    def __init__(self, seed=0x2545F491):
        self.x = seed & 0xFFFFFFFF

    def next(self) -> float:
        x = self.x
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.x = x & 0xFFFFFFFF
        # 与 C 一致：取高 24 bit 映射到 [-1, 1)
        return ((x >> 8) / 8388608.0) - 1.0


# ----------------------------------------------------------------- 单帧编解码
class Vocoder:
    """逐帧分析/合成。状态跨帧保留（预加重、逆滤波历史、合成滤波器、相位）。"""

    def __init__(self):
        self.x_prev = 0.0
        self.hp_hist = np.zeros(ORDER)
        self.syn = np.zeros(ORDER)
        self.de_prev = 0.0
        self.phase = 0.0
        self.rnd = _Rand()

    # ---- 分析 ----
    def analyze(self, x: np.ndarray) -> VocParams:
        x = np.asarray(x, dtype=np.float64)
        if x.shape[0] != FRAME:
            raise ValueError("frame must be %d samples, got %d" % (FRAME, x.shape[0]))

        # 预加重
        prev = np.concatenate(([self.x_prev], x[:-1]))
        hp = x - 0.9375 * prev
        self.x_prev = float(x[-1])

        # 汉明窗 + 自相关
        n = np.arange(FRAME, dtype=np.float64)
        win = 0.54 - 0.46 * np.cos(_TWO_PI * n / (FRAME - 1))
        w = hp * win
        r = np.empty(ORDER + 1)
        for lag in range(ORDER + 1):
            r[lag] = float(np.dot(w[lag:], w[:FRAME - lag])) if lag else float(np.dot(w, w))
        r[0] *= 1.0001

        a, err = _levinson(r)
        if err is None:
            a = np.zeros(ORDER + 1)
            a[0] = 1.0
            err = r[0] if r[0] > 0 else 1e-10

        # 基频：先对预加重信号做 LPC 逆滤波（用上一帧历史补齐），再 6:1 抽取
        hist = np.concatenate((self.hp_hist, hp))
        res = np.array(hp, copy=True)
        for k in range(1, ORDER + 1):
            res -= a[k] * hist[ORDER - k: ORDER - k + FRAME]

        nd = FRAME // DEC
        d = res.reshape(nd, DEC).sum(axis=1)

        best = 0.0
        best_lag = 0
        sc = np.full(LAG_MAX + 1, -1.0)
        hi = min(LAG_MAX, nd - 2)
        for lag in range(LAG_MIN, hi + 1):
            left = d[lag:]
            right = d[:nd - lag]
            s = float(np.dot(left, right))
            e1 = float(np.dot(left, left))
            e2 = float(np.dot(right, right))
            if e1 > 1e-9 and e2 > 1e-9:
                v = s / (0.5 * (e1 + e2))
                sc[lag] = v
                if v > best:
                    best = v
                    best_lag = lag

        # 倍频纠偏：取"最小的强峰"，不是"最高分"。
        # 8 kHz 下 220 Hz 的周期是 36.4 点（非整数），分数会出现
        # lag=36 → 0.86、lag=73 → 0.93、lag=109 → 0.98 这种"倍数越高分越高"的
        # 现象，只取最大值必掉八度（实测合成出来是 110 Hz）。任何周期的整数倍
        # 都是峰，所以规则改成：在最高分 0.80 倍以上的**局部峰**里取最小 lag。
        if best_lag > 0:
            thr = max(0.5, 0.80 * best)
            for l2 in range(LAG_MIN + 1, best_lag):
                if (sc[l2] >= thr and sc[l2] >= sc[l2 - 1]
                        and sc[l2] >= sc[l2 + 1]):
                    best_lag = l2
                    best = float(sc[l2])
                    break

        self.hp_hist = hp[FRAME - ORDER:].copy()

        # 量化
        # 量化：先夹到 ±0.99。反射系数正好到 ±1 时合成滤波器极点落在单位圆上
        # （纯音、长音的极点半径本来就是 1.0），8 bit 一饱和就变成自激振荡，
        # 听感是一层满幅噪波。真实语音很少超过 0.99，夹住没有可听损失。
        kf = _a_to_k(a)
        kq = np.clip(np.rint((np.clip(kf[1:], -0.99, 0.99) + 1.0) * 127.5),
                     0, 255).astype(np.uint8)

        pitch = 0
        if best > VOICED_THRESHOLD and best_lag > 0:
            lag48 = best_lag * DEC
            q = int(round((lag48 - 120) * 255.0 / 680.0))
            pitch = max(0, min(255, q)) + 1        # 0 保留给清音

        rms = float(np.sqrt(np.mean(x * x)))
        gain = int(min(255, max(0, round(rms * GAIN_SCALE))))

        return VocParams(kq, gain, pitch)

    # ---- 合成 ----
    def synthesize(self, p: VocParams) -> np.ndarray:
        kf = np.empty(ORDER + 1)
        kf[0] = 1.0
        kf[1:] = p.k.astype(np.float64) / 127.5 - 1.0
        a = _k_to_a(kf)
        # 带宽展宽 0.996^i：把极点集体往圆内挪一点。纯音/长音的 LPC 极点几乎
        # 就在单位圆上（Q=∞），不挪的话 16 阶直接型会一直共振，最后靠防爆缩回
        # 压成 ±40 的方波 —— 听感就是一层 Nyquist 噪波。0.996 对共振峰位置几乎
        # 无影响，但让衰减变成有限值。
        for _i in range(1, ORDER + 1):
            a[_i] *= 0.996 ** _i
        a1, a2, a3, a4 = (a[1:5].tolist() + [0.0] * 4)[:4]
        a5, a6, a7, a8 = (a[5:9].tolist() + [0.0] * 4)[:4]
        a9, a10, a11, a12 = (a[9:13].tolist() + [0.0] * 4)[:4]
        a13, a14, a15, a16 = (a[13:17].tolist() + [0.0] * 4)[:4]

        target = p.gain / GAIN_SCALE
        if p.pitch > 0:
            lag = 120.0 + ((p.pitch - 1) / 255.0) * 680.0
            amp = 1.0
        else:
            lag = 0.0
            amp = 0.5

        s = self.syn
        s0 = float(s[0]); s1 = float(s[1]); s2 = float(s[2]);  s3 = float(s[3])
        s4 = float(s[4]); s5 = float(s[5]); s6 = float(s[6]);  s7 = float(s[7])
        s8 = float(s[8]); s9 = float(s[9]); s10 = float(s[10]); s11 = float(s[11])
        s12 = float(s[12]); s13 = float(s[13]); s14 = float(s[14]); s15 = float(s[15])

        phase = self.phase
        de_prev = float(self.de_prev)
        rnd = self.rnd
        y = np.empty(FRAME)

        for i in range(FRAME):
            if lag > 0.0:
                phase += 1.0
                if phase >= lag:
                    phase -= lag
                exc = amp if phase < 1.0 else 0.0
            else:
                exc = rnd.next() * amp

            yy = (exc
                  - a1 * s0 - a2 * s1 - a3 * s2 - a4 * s3
                  - a5 * s4 - a6 * s5 - a7 * s6 - a8 * s7
                  - a9 * s8 - a10 * s9 - a11 * s10 - a12 * s11
                  - a13 * s12 - a14 * s13 - a15 * s14 - a16 * s15)

            # 数值防爆：16 阶直接型在高 Q 下会共振，超 40 就把整组状态等比缩回
            if yy > 40.0 or yy < -40.0:
                g = 40.0 / abs(yy)
                s0 *= g; s1 *= g; s2 *= g; s3 *= g
                s4 *= g; s5 *= g; s6 *= g; s7 *= g
                s8 *= g; s9 *= g; s10 *= g; s11 *= g
                s12 *= g; s13 *= g; s14 *= g; s15 *= g
                yy *= g

            s15 = s14; s14 = s13; s13 = s12; s12 = s11
            s11 = s10; s10 = s9; s9 = s8; s8 = s7
            s7 = s6; s6 = s5; s5 = s4; s4 = s3
            s3 = s2; s2 = s1; s1 = s0; s0 = yy

            de_prev = yy + 0.9375 * de_prev
            y[i] = de_prev

        s[:] = (s0, s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14, s15)
        self.phase = phase
        self.de_prev = de_prev

        # 帧级归一化到目标 RMS + 0.85 软限幅
        rms = float(np.sqrt(np.mean(y * y)))
        scale = (target / rms) if rms > 1e-6 else 0.0
        scale = min(scale, 1e4)
        out = y * scale
        absv = np.abs(out)
        over = absv > 0.85
        if np.any(over):
            head = 0.15
            out_abs = absv[over]
            compressed = 0.85 + head * ((out_abs - 0.85) / (out_abs - 0.85 + head))
            out[over] = np.sign(out[over]) * compressed
        return out

    def reset(self):
        self.x_prev = 0.0
        self.hp_hist[:] = 0.0
        self.syn[:] = 0.0
        self.de_prev = 0.0
        self.phase = 0.0
        self.rnd = _Rand()


# ----------------------------------------------------------------- 流式封装
class VocoderStream:
    """控制台用：吃 (n, 2) int16 的 10 ms 块，吐同形状同长度。

    延迟正好 1 帧（20 ms）：第一块进去只够填满缓冲，第二块才开始出结果。
    单声道处理、双声道复制输出 —— 和板子上 RNNoise 走单声道是同一个道理。
    """

    def __init__(self, channels=2):
        self.channels = channels
        self.voc = Vocoder()
        self.inbuf = np.zeros(0)
        self.outbuf = np.zeros(0)
        self.frames = 0
        self.voiced = 0
        self.ms_per_frame = 0.0
        self.last_gain_db = -120.0

    # ---- 主入口 ----
    def process(self, block) -> np.ndarray:
        blk = np.asarray(block)
        if blk.ndim == 1:
            blk = blk.reshape(-1, 1)
        n = blk.shape[0]
        if n == 0:
            return blk.astype(np.int16, copy=False)

        mono = blk.astype(np.float32).mean(axis=1) / 32768.0
        self.inbuf = np.concatenate([self.inbuf, mono.astype(np.float64)])

        produced = []
        while self.inbuf.shape[0] >= FRAME:
            frame = self.inbuf[:FRAME]
            self.inbuf = self.inbuf[FRAME:]
            t0 = time.perf_counter()
            p = self.voc.analyze(frame)
            produced.append(self.voc.synthesize(p))
            dt = (time.perf_counter() - t0) * 1000.0
            self.ms_per_frame = dt if not self.frames else (0.9 * self.ms_per_frame + 0.1 * dt)
            self.frames += 1
            if p.voiced:
                self.voiced += 1
            self.last_gain_db = (20.0 * math.log10(p.gain / GAIN_SCALE)) if p.gain > 0 else -120.0

        if produced:
            self.outbuf = np.concatenate([self.outbuf] + produced)

        if self.outbuf.shape[0] < n:
            self.outbuf = np.concatenate([np.zeros(n - self.outbuf.shape[0]), self.outbuf])

        out = self.outbuf[:n]
        self.outbuf = self.outbuf[n:]
        out16 = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
        if blk.shape[1] == 1:
            return out16.reshape(-1, 1)
        return np.repeat(out16.reshape(-1, 1), blk.shape[1], axis=1)

    def reset(self):
        self.voc.reset()
        self.inbuf = np.zeros(0)
        self.outbuf = np.zeros(0)
        self.frames = 0
        self.voiced = 0
        self.ms_per_frame = 0.0

    def stats(self) -> dict:
        return {
            "frames": self.frames,
            "voiced": self.voiced,
            "bitrate": BITRATE,
            "ms_per_frame": round(self.ms_per_frame, 2),
            "gain_db": round(self.last_gain_db, 1),
            "voice_ratio": round(self.voiced / self.frames, 3) if self.frames else 0.0,
        }


# ----------------------------------------------------------------- 离线编解码
def vocode(x, rate=RATE):
    """离线：对一整段 -1..1 的单声道 float 做 编码+解码，返回同长度波形。"""
    sig = np.asarray(x, dtype=np.float64)
    if rate != RATE:
        # 最近邻重采样到 48 kHz（够 demo 用；正式路径不做重采样）
        idx = (np.arange(int(len(sig) * RATE / rate)) * rate / RATE).astype(int)
        sig = sig[np.clip(idx, 0, len(sig) - 1)]
    pad = (-len(sig)) % FRAME
    if pad:
        sig = np.concatenate([sig, np.zeros(pad)])
    voc = Vocoder()
    out = np.zeros_like(sig)
    for off in range(0, len(sig), FRAME):
        p = voc.analyze(sig[off:off + FRAME])
        out[off:off + FRAME] = voc.synthesize(p)
    return out


# ----------------------------------------------------------------- 自检
def _gen_voiced(nframes, f0=120.0):
    """与 C 版 gen_voiced 一致的测试信号：33 个谐波 + 三个共振峰 + 4 Hz 包络。"""
    n = nframes * FRAME
    t = np.arange(n) / RATE
    sig = np.zeros(n)
    for k in range(1, 41):
        f = f0 * k
        if f > 4000.0:
            break
        d1 = (f - 500.0) / 220.0
        d2 = (f - 1500.0) / 320.0
        d3 = (f - 2500.0) / 420.0
        w = 0.55 + 1.10 / (1 + d1 * d1) + 0.70 / (1 + d2 * d2) + 0.40 / (1 + d3 * d3)
        amp = w / (k * math.sqrt(k))
        sig += amp * np.sin(_TWO_PI * f * t)
    sig *= (0.6 + 0.4 * np.sin(_TWO_PI * 4.0 * t)) * 0.22
    return np.clip(sig, -0.95, 0.95)


def _gen_noise(nframes, amp=0.25, seed=0x12345678):
    r = _Rand(seed)
    n = nframes * FRAME
    return np.array([r.next() * amp for _ in range(n)])


def _harmonic_similarity(a, b, nframes, f0=120.0, nharm=24):
    idx = np.arange(FRAME)
    best = 0.0
    ea = np.zeros((nframes, nharm))
    eb = np.zeros((nframes, nharm))
    for t in range(nframes):
        seg_a = a[t * FRAME:(t + 1) * FRAME]
        seg_b = b[t * FRAME:(t + 1) * FRAME]
        for j in range(nharm):
            ph = np.exp(-2j * _PI * f0 * (j + 1) * idx / RATE)
            ea[t, j] = abs(np.dot(seg_a, ph))
            eb[t, j] = abs(np.dot(seg_b, ph))
    dot = float(np.sum(ea * eb))
    na = float(np.sqrt(np.sum(ea * ea)))
    nb = float(np.sqrt(np.sum(eb * eb)))
    if na <= 0 or nb <= 0:
        return 0.0
    best = dot / (na * nb)
    return best


def selftest(verbose=True):
    """与 QEMU 的 voc_test 同一套判据，用来确认 PC 复刻与 C 版行为一致。"""
    nframes = 50
    ok = True
    lines = []

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        lines.append("  %s %s%s" % ("OK  " if cond else "FAIL", name, extra))

    # 1) 浊音：电平与谐波结构
    src = _gen_voiced(nframes)
    out = vocode(src)
    rin = float(np.sqrt(np.mean(src ** 2)))
    rout = float(np.sqrt(np.mean(out ** 2)))
    gdb = 20.0 * math.log10(rout / rin) if rin > 0 and rout > 0 else -120.0
    sim = _harmonic_similarity(src, out, nframes)
    check("voiced level within +-6 dB", -6.0 < gdb < 6.0, " (%.3f dB)" % gdb)
    check("voiced harmonic sim > 0.65", sim > 0.65, " (%.3f)" % sim)
    check("voiced no clipping", float(np.max(np.abs(out))) < 1.0)

    # 2) 噪声：应被判成清音
    noise = _gen_noise(nframes)
    voc = Vocoder()
    voiced = 0
    for t in range(nframes):
        p = voc.analyze(noise[t * FRAME:(t + 1) * FRAME])
        voiced += 1 if p.voiced else 0
    ratio = 1.0 - voiced / float(nframes)
    check("noise unvoiced > 80%", ratio > 0.80, " (%.3f)" % ratio)

    # 3) 码率
    check("bitrate is 7.2 kbps", BITRATE == 7200, " (%d bps)" % BITRATE)

    # 4) 流式接口：长度守恒、延迟一帧
    st = VocoderStream()
    blk = (np.sin(2 * _PI * 440.0 * np.arange(480) / RATE) * 8000).astype(np.int16)
    blk = np.repeat(blk.reshape(-1, 1), 2, axis=1)
    out_len = 0
    first_nonzero = None
    for i in range(20):
        o = st.process(blk)
        out_len += o.shape[0]
        if first_nonzero is None and np.any(o):
            first_nonzero = i
    check("stream length preserved", out_len == 20 * 480, " (%d)" % out_len)
    check("stream latency == 1 frame", first_nonzero == 1, " (block %s)" % first_nonzero)
    ms = st.ms_per_frame
    check("stream realtime (ms/frame < 20)", 0 < ms < 20.0, " (%.2f ms)" % ms)

    if verbose:
        print("=== vocoder.py selftest (LPC16 / 20 ms / 48 kHz) ===")
        for ln in lines:
            print(ln)
        print("RESULT:", "PASS all checks passed" if ok else "FAIL")
    return ok, {"gain_db": gdb, "sim": sim, "unvoiced": ratio, "ms": ms}


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        good, _ = selftest()
        raise SystemExit(0 if good else 1)
    print(__doc__)
