# -*- coding: utf-8 -*-
"""P0/P1 的离线验收：响度归一化 / 真峰值 / 交叉馈送 / 双 DAC 校准。

这里只验证"我们答应的四件事"，全部离线、可复现：

1. **响度归一化**：两段差 20 dB 的素材，归一后短时响度差应 ≤1.5 LU，
   且静音段不把底噪抬起来。
2. **真峰值**：0 dBFS 的 11 kHz 正弦（最坏过采样情形）过链后 ≤ 设定上限。
3. **交叉馈送**：低频两声道互相关显著上升，整体响度基本不变。
4. **双 DAC 校准**：人为给一路加 +3 dB@1 kHz，从两段录音反推的修正曲线
   应把这段差压到 ≤1.5 dB（理想 0，受 1/3 倍频程带宽限制）。

跑法：``python bench_quality.py``（约 10 s）。
"""
from __future__ import annotations

import io
import math
import os
import wave

import numpy as np

from dac_cal import BANDS, DacCal, _band_levels, curve_from_wavs
from loudnorm import LoudNorm, TruePeakMeter
from playfx import PlayFX, _peaking_sos
from scipy import signal

RATE = 48000


# ---------------------------------------------------------------- 素材

def tone(amp: float, secs: float, f: float = 440.0) -> np.ndarray:
    n = int(secs * RATE)
    t = np.arange(n) / RATE
    x = amp * np.sin(2 * np.pi * f * t)
    return np.repeat(x.reshape(-1, 1), 2, axis=1).astype(np.float32)


def pink(secs: float, seed: int = 3) -> np.ndarray:
    """粉红噪声（两声道独立），用来做 DAC 校准的"测量信号"。"""
    rng = np.random.default_rng(seed)
    n = int(secs * RATE)
    out = np.empty((n, 2), dtype=np.float64)
    for c in range(2):
        w = rng.standard_normal(n)
        # 1/f 整形：频域开方衰减即可，别用 IIR（会飘）
        W = np.fft.rfft(w)
        f = np.fft.rfftfreq(n, 1.0 / RATE)
        f[0] = f[1]
        W = W / np.sqrt(f)
        w = np.fft.irfft(W, n)
        out[:, c] = w / (np.max(np.abs(w)) + 1e-12) * 0.3
    return out


def write_wav(path: str, x: np.ndarray, rate: int = RATE) -> None:
    y = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    data = (y * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(y.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def short_lufs(x: np.ndarray) -> float:
    """借用 playfx 的响度计口径复核（喂 int16）。"""
    from loudness import LoudnessMeter
    m = LoudnessMeter()
    y = np.clip(np.asarray(x) * 32768.0, -32768, 32767).astype(np.int16)
    for i in range(0, len(y), 480):
        m.feed(y[i:i + 480])
    v = m.short_db()
    return -120.0 if v is None else float(v)


def run_chain(fx: PlayFX, x: np.ndarray) -> np.ndarray:
    """把 float 素材按 10 ms 块过一遍整条链，返回 float。"""
    out = np.empty_like(x)
    for i in range(0, len(x), 480):
        seg = x[i:i + 480]
        if len(seg) == 0:
            break
        blk = np.clip(seg * 32767.0, -32768, 32767).astype(np.int16)
        y = fx.process(blk)
        out[i:i + len(seg)] = y.astype(np.float64) / 32768.0
    return out


# ---------------------------------------------------------------- 判据

class Check:
    def __init__(self):
        self.ok = 0
        self.total = 0

    def __call__(self, name, cond, extra=""):
        self.total += 1
        if cond:
            self.ok += 1
            print("  [OK]   %-42s %s" % (name, extra))
        else:
            print("  [FAIL] %-42s %s" % (name, extra))
        return cond


def main() -> int:
    c = Check()
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp_bench")
    os.makedirs(tmp, exist_ok=True)

    # ---------------- 1. 响度归一化 ----------------
    print("== 1. 响度归一化（P0）")
    res = {}
    for tag, amp in (("loud", 0.5), ("quiet", 0.05)):
        fx = PlayFX(RATE)
        fx.enabled = True
        fx.set_preset("flat")
        fx.set_norm(on=True, target_lufs=-16.0)
        x = tone(amp, 8.0)
        y = run_chain(fx, x)
        res[tag] = short_lufs(y[-int(3.0 * RATE):])
    c("两段差 20 dB 的素材归一后 ≤1.5 LU",
      abs(res["loud"] - res["quiet"]) <= 1.5,
      "loud=%.2f quiet=%.2f" % (res["loud"], res["quiet"]))
    c("归一后贴近 −16 LUFS（±2）", abs(res["loud"] + 16.0) <= 2.0, "%.2f LUFS" % res["loud"])

    fx = PlayFX(RATE)
    fx.enabled = True
    fx.set_preset("flat")
    fx.set_norm(on=True, target_lufs=-16.0)
    silence = (1e-4 * np.random.default_rng(1).standard_normal((int(2 * RATE), 2))).astype(np.float32)
    run_chain(fx, silence)
    c("静音段增益不出正（不抬底噪）", fx.norm.stats()["gain_db"] <= 0.01,
      "gain=%.2f dB" % fx.norm.stats()["gain_db"])

    # ---------------- 2. 真峰值 ----------------
    print("== 2. 真峰值与削波保护（P0）")
    fx = PlayFX(RATE)
    fx.enabled = True
    fx.set_preset("flat")
    fx.set_tp_target(-1.0)
    hot = tone(1.0, 2.0, f=11025.0)       # 0 dBFS，采样间过冲最大
    y = run_chain(fx, hot)
    tp = fx.stats()["tp"]["db"]
    c("0 dBFS 11 kHz 过链后 ≤ −0.85 dBTP", tp <= -0.85, "tp=%.2f dBTP (pre %.2f)" % (tp, fx.stats()["tp"].get("pre_db", 0)))
    c("真峰值兜底被触发过（计数 >0）", fx.stats()["tp"]["clamps"] > 0,
      "clamps=%d" % fx.stats()["tp"]["clamps"])

    # ---------------- 3. 交叉馈送 ----------------
    print("== 3. 交叉馈送（P1）")
    # 判据用"单声道信号从 L 串到 R"的**幅度与频率选择性**，不用白噪声互相关 ——
    # 白噪声下互串功率只占 0.6%，相关系数天然看不出变化（踩过）。
    def tone_in_left(freq, secs=1.0, amp=0.3):
        n = int(secs * RATE)
        t = np.arange(n)
        s = amp * np.sin(2 * np.pi * freq * t / RATE)
        return np.stack([s, np.zeros(n)], axis=1)

    def rms_after(fx, x):
        y = run_chain(fx, x.astype(np.float32))
        return float(np.sqrt(np.mean(y[:, 1] ** 2))), float(np.sqrt(np.mean(y[:, 0] ** 2)))

    k = 0.35
    fx_cf = PlayFX(RATE)
    fx_cf.enabled = True
    fx_cf.set_preset("flat")
    fx_cf.set_crossfeed(on=True, amount=k)
    fx_no = PlayFX(RATE)
    fx_no.enabled = True
    fx_no.set_preset("flat")

    low = tone_in_left(200.0)
    r_no, l_lo = rms_after(fx_no, low)
    r_lo, l_lo2 = rms_after(fx_cf, low)
    # 理论值：R ≈ k/(1+k)·LP(200 Hz) ≈ 0.259 × 0.95 × 0.3/√2
    exp = k / (1.0 + k) * 0.95 * 0.3 / np.sqrt(2.0)
    c("200 Hz 单声道信号串到对侧（幅度接近理论值）",
      abs(r_lo - exp) <= 0.25 * exp and r_lo > 5 * r_no,
      "实测 %.4f 理论 %.4f 无馈送 %.5f" % (r_lo, exp, r_no))

    high = tone_in_left(6000.0)
    r_hi, _ = rms_after(fx_cf, high)
    c("6 kHz 不该被串到对侧（低通选择性）",
      r_hi <= 0.06 * r_lo, "6 kHz 串音 %.6f = %.1f%% of 200 Hz" % (r_hi, 100.0 * r_hi / max(r_lo, 1e-12)))

    # 用 3 s 音（LUFS 的短时窗就是 3 s，1 s 素材只会得到 -120 的地板值，那是假通过）
    low3 = tone_in_left(200.0, secs=3.2)
    loud_n = short_lufs(run_chain(fx_no, low3))
    loud_f = short_lufs(run_chain(fx_cf, low3))
    c("交叉馈送对单声道素材的响度影响 ≤3 LU", abs(loud_f - loud_n) <= 3.0,
      "%.2f → %.2f LUFS" % (loud_n, loud_f))

    # ---------------- 4. 双 DAC 校准 ----------------
    print("== 4. 双 DAC 校准（P1）")
    meas = pink(3.0, seed=11)
    # 假设外接 DAC 在 1 kHz 附近多 +3 dB（模拟"两个芯片频响不同"）
    peak = _peaking_sos(1000.0, 3.0, 1.1, RATE)
    ext = signal.sosfilt(peak, meas, axis=0)

    pb = os.path.join(tmp, "cal_board.wav")
    pe = os.path.join(tmp, "cal_ext.wav")
    write_wav(pb, meas)
    write_wav(pe, ext)

    cb, ce = curve_from_wavs(pb, pe)
    k = list(BANDS).index(1000.0)
    c("1 kHz 段修正量方向正确（板载抬高、外接压低）",
      cb[k] > 0.5 and ce[k] < -0.5,
      "board=%+.1f ext=%+.1f dB" % (cb[k], ce[k]))

    # 应用修正后再量一次两路的 1 kHz 段差
    cal = DacCal(RATE)
    cal.set_curves(cb, ce)
    cal.enabled = True
    both = np.stack([meas[:, 0], ext[:, 0]], axis=1).astype(np.float64)
    fixed = cal.process(both)
    b2, e2 = fixed[:, 0], fixed[:, 1]
    hb = _band_levels(b2, RATE)
    he = _band_levels(e2, RATE)
    diff = (hb - he) - np.mean(hb - he)
    c("校准后 1 kHz 段残差 ≤1.5 dB", abs(diff[k]) <= 1.5, "残差 %+.2f dB" % diff[k])

    print("\n%d/%d PASS" % (c.ok, c.total))
    return 0 if c.ok == c.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
