"""PL 谱减降噪引擎 —— 浮点参考模型（tuning + 目标数字）

这个文件的作用不是"再写一个降噪器"，而是先回答三个问题：

1. 512 点 / hop 256 / sqrt-Hann 这套参数下，谱减能把底噪压掉多少 dB？
2. 过减因子 alpha 与增益下限 floor 取多少，人声还能守住？
3. 固定点（24 bit Q1.23 + 块浮点）实现时，量化噪声会不会把这几 dB 吃掉？

前两个问题用浮点跑真录音回答（材料库/04-实测记录/音频样本），第三个问题由
`fx.py`（固定点逐位复刻）回答，两边必须落在同一个 dB 数上。

用法：
    python model.py --wav 材料库/04-实测记录/音频样本/2026-10-24_带噪AB_n400/01_noisy_mic_plus_noise.wav
    python model.py --selftest
"""

from __future__ import annotations

import argparse
import wave

import numpy as np

# ---- 与 RTL 约定的参数（改这里就等于改硬件，两边必须同步）----
FS = 48000
N = 512          # 帧长（10.67 ms）
HOP = 256        # 帧移（5.33 ms，50% 重叠）
ALPHA = 2.5      # 过减因子（功率域）
FLOOR = 0.05     # 增益下限（-26 dB）
SMOOTH = 0.65    # 增益时间平滑（上一帧权重）
NOISE_RATE = 0.05    # 噪声估计跟踪速度（只在"像噪声"的帧上走）
NOISE_GATE = 1.7     # 判"像噪声"的门限：mag < noise * GATE
NOISE_RISE = 1.0005  # 非噪声帧的极慢回升（防止估计永久冻死）
WARMUP = 60          # 前 60 帧（约 320 ms）把增益缓缓放开，避免开头"憋一口气"


def sqrt_hann(n: int = N) -> np.ndarray:
    """周期性 sqrt-Hann：分析窗与合成窗相乘 = Hann，50% 重叠时平方和恒为 1。"""
    k = np.arange(n)
    return np.sqrt(0.5 - 0.5 * np.cos(2.0 * np.pi * k / n))


def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit(f"只支持 16 bit PCM，这个文件是 {sw * 8} bit")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x / 32768.0, fs


def write_wav_mono(path: str, x: np.ndarray, fs: int) -> None:
    y = np.clip(x, -1.0, 32767.0 / 32768.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((y * 32768.0).astype("<i2").tobytes())


class Denoiser:
    """功率域谱减 + 噪声门控跟踪 + 增益时间平滑。

    逐帧状态只有三个数组：noise（噪声功率估计）、gain_prev（上一帧增益）、ola（重叠相加）。
    这三个数组在 RTL 里就是三块 BRAM，一帧 512 个 bin 读写各一遍。
    """

    def __init__(self, alpha: float = ALPHA, floor: float = FLOOR,
                 smooth: float = SMOOTH, rate: float = NOISE_RATE,
                 gate: float = NOISE_GATE, rise: float = NOISE_RISE,
                 warmup: int = WARMUP, init_scale: float = 0.5,
                 unity: bool = False):
        self.w = sqrt_hann()
        self.alpha = alpha
        self.floor = floor
        self.smooth = smooth
        self.rate = rate
        self.gate = gate
        self.rise = rise
        self.warmup = warmup
        self.init_scale = init_scale
        self.unity = unity
        self.noise = None
        self.init_sum = None
        self.init_n = 0
        self.gain_prev = None
        self.ola = np.zeros(N)
        self.n = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        n_frames = max(0, (len(x) - N) // HOP + 1)
        out = np.zeros(len(x) + N)
        for i in range(n_frames):
            seg = x[i * HOP: i * HOP + N] * self.w
            X = np.fft.rfft(seg, n=N)
            mag = np.abs(X)
            pw = mag * mag

            if self.unity:
                gain = np.ones_like(mag)
            else:
                if self.noise is None:
                    # 前 8 帧（43 ms）取每 bin 的**平均**功率当初始噪声。
                    # 用最小值会低估（实测压制从 11 dB 掉到 5 dB），用第一帧会高估。
                    self.init_sum = pw.copy() if self.init_sum is None else self.init_sum + pw
                    self.init_n += 1
                    if self.init_n >= 8:
                        self.noise = self.init_sum / 8.0 * self.init_scale
                    gain = np.ones_like(mag)
                else:
                    # 只在"比噪声估计还小或接近"的帧上更新，语音帧上几乎不动。
                    # 注意单位：noise 存的是功率，门限要平方过再比（这里踩过一次坑）。
                    quiet = pw < self.noise * (self.gate * self.gate)
                    est = np.where(
                        quiet,
                        (1.0 - self.rate) * self.noise + self.rate * pw,
                        np.minimum(self.noise * self.rise, pw),
                    )
                    self.noise = est
                    ratio = self.noise / np.maximum(pw, 1e-12)
                    g = np.sqrt(np.maximum(0.0, 1.0 - self.alpha * ratio))
                    gain = np.clip(g, self.floor, 1.0)

                if self.gain_prev is not None:
                    gain = self.smooth * self.gain_prev + (1.0 - self.smooth) * gain
                self.gain_prev = gain
                # 预热：噪声估计还没收敛时别一把掐死，否则开头会明显"憋一口气"
                if self.n < self.warmup:
                    w = self.n / float(self.warmup)
                    gain = 1.0 - (1.0 - gain) * w
            self.n += 1

            y = np.fft.irfft(X * gain, n=N) * self.w
            self.ola[:N] += y
            out[i * HOP: i * HOP + HOP] = self.ola[:HOP]
            self.ola[:-HOP] = self.ola[HOP:]
            self.ola[-HOP:] = 0.0
        return out[:len(x)]


def frame_db(x: np.ndarray, frame: int = 480) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.array([-140.0])
    seg = x[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(seg ** 2, axis=1) + 1e-20)
    return 20.0 * np.log10(rms)


def report(name: str, x: np.ndarray, y: np.ndarray) -> dict:
    dx = frame_db(x)
    dy = frame_db(y)
    # 底噪用 10 分位、人声用 90 分位；这两个数加起来才能回答"压了多少、伤了多少"
    floor_x, floor_y = np.percentile(dx, 10), np.percentile(dy, 10)
    loud_x, loud_y = np.percentile(dx, 90), np.percentile(dy, 90)
    print(f"  {name}: 底噪 {floor_x:7.2f} -> {floor_y:7.2f} dB "
          f"(压制 {floor_x - floor_y:5.2f} dB) | "
          f"人声 {loud_x:7.2f} -> {loud_y:7.2f} dB (变化 {loud_y - loud_x:+5.2f} dB)")
    return dict(floor_before=floor_x, floor_after=floor_y,
                loud_before=loud_x, loud_after=loud_y)


def selftest() -> int:
    rng = np.random.default_rng(7)
    t = np.arange(FS * 3) / FS
    # 语音替身：基频 120 Hz + 谐波 + 4 Hz 起伏；底噪 -46 dBFS 白噪声
    voice = np.zeros_like(t)
    for k in range(1, 25):
        amp = 0.18 / k ** 1.2 * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
        voice += amp * np.sin(2 * np.pi * 120 * k * t)
    voice *= (np.sin(2 * np.pi * 0.5 * t) > -0.2)  # 一半时间"不说话"
    noise = rng.standard_normal(len(t)) * 10 ** (-46 / 20)
    x = voice + noise

    dn = Denoiser()
    y = dn.process(x)
    r = report("合成 120 Hz 谐波 + -46 dBFS 噪声", x, y)

    # 0) 重建自检：增益全 1 时，分析/合成窗 + 重叠相加必须还原原信号
    u = Denoiser(unity=True).process(x)
    err = np.max(np.abs(u[2 * N:-2 * N] - x[2 * N:-2 * N]))
    print(f"  unity 重建误差 max|y-x| = {err:.3e}")
    ok = err < 1e-9
    if r["floor_before"] - r["floor_after"] < 8.0:
        print(f"  FAIL 底噪压制不足 8 dB")
        ok = False
    if abs(r["loud_after"] - r["loud_before"]) > 3.0:
        print("  FAIL 人声电平变化超过 3 dB")
        ok = False
    # 空输入、短输入不能崩
    assert len(Denoiser().process(np.zeros(10))) == 10
    assert len(Denoiser().process(np.zeros(N))) == N
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--alpha", type=float, default=ALPHA)
    ap.add_argument("--floor", type=float, default=FLOOR)
    ap.add_argument("--smooth", type=float, default=SMOOTH)
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.selftest or not a.wav:
        return selftest()

    x, fs = read_wav_mono(a.wav)
    if fs != FS:
        raise SystemExit(f"采样率 {fs} != {FS}，先重采样再跑")
    print(f"{a.wav}  {len(x)/FS:.2f} s  alpha={a.alpha} floor={a.floor} smooth={a.smooth}")
    dn = Denoiser(alpha=a.alpha, floor=a.floor, smooth=a.smooth)
    y = dn.process(x)
    report("整段", x, y)
    # 前 1 s 统计（噪声估计还没收敛，故意分开看）
    n = min(len(x), FS)
    report("前 1 s", x[:n], y[:n])
    if a.out:
        write_wav_mono(a.out, y, fs)
        print("写出", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
