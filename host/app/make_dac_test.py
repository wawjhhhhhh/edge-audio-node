"""make_dac_test.py -- 生成双 DAC 模式用的左右可分辨测试音。

两种风格：
  tone  左 440 Hz 连续音 / 右 880 Hz 连续音（差一个八度，用来判"低 vs 高"）
  beep  左 440 Hz **连续**长音 / 右 880 Hz **断续**"滴滴滴"（用来判"连续 vs 断续"）
        —— 断续比音高更容易分辨：不用记住哪个音更高，只要听"是不是一顿一顿的"。

两种风格都是 10 s、48 kHz、16 bit 立体声，各声道 -13.5 dBFS 左右，留足余量不削波。
断续音用 5 ms 余弦斜坡收边，避免开关时的"咔"声被误听成故障。

用法：
    python make_dac_test.py                 # 两种都生成
    python make_dac_test.py --style beep    # 只生成断续版
"""
import argparse
import os
import wave

import numpy as np

RATE = 48000
SECS = 10
AMP = 0.30
BEEP_ON = 0.15      # 每声 150 ms
BEEP_OFF = 0.15     # 间隔 150 ms -> 每秒约 3.3 声
RAMP = 0.005        # 5 ms 斜坡

OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                       "材料库", "04-实测记录", "音频样本"))


def _fade(ch, secs=0.03):
    n = int(secs * RATE)
    if n * 2 >= len(ch):
        return ch
    ramp = np.linspace(0.0, 1.0, n)
    ch[:n] *= ramp
    ch[-n:] *= ramp[::-1]
    return ch


def _beep_gate(n):
    """返回 0/1 门控：150 ms 通、150 ms 断，边缘 5 ms 余弦斜坡。"""
    t = np.arange(n) / RATE
    period = BEEP_ON + BEEP_OFF
    phase = np.mod(t, period)
    g = (phase < BEEP_ON).astype(np.float64)
    # 斜坡：在 on 段的两端各加一个 RAMP 长的余弦过渡
    k = max(1, int(RAMP * RATE))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, k))
    idx_on = np.where(np.diff(np.concatenate(([0.0], g))) > 0)[0]
    idx_off = np.where(np.diff(np.concatenate((g, [0.0]))) < 0)[0]
    for i in idx_on:
        seg = g[i:i + k]
        m = min(k, len(seg))
        g[i:i + m] = np.maximum(g[i:i + m], ramp[:m])
    for i in idx_off:
        seg = g[i - k + 1:i + 1]
        m = min(k, len(seg))
        if m > 0:
            g[i - m + 1:i + 1] = np.minimum(g[i - m + 1:i + 1], ramp[::-1][:m])
    return g


def build(style):
    n = RATE * SECS
    t = np.arange(n, dtype=np.float64) / RATE
    left = AMP * np.sin(2 * np.pi * 440.0 * t)
    right = AMP * np.sin(2 * np.pi * 880.0 * t)
    if style == "beep":
        right = right * _beep_gate(n)
    left = _fade(left)
    right = _fade(right)

    pcm = np.empty((n, 2), dtype=np.int16)
    pcm[:, 0] = np.clip(left * 32767.0, -32768, 32767).astype(np.int16)
    pcm[:, 1] = np.clip(right * 32767.0, -32768, 32767).astype(np.int16)
    return pcm


def save(pcm, name):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    rms = [float(np.sqrt(np.mean(pcm[:, c].astype(np.float64) ** 2)))
           for c in (0, 1)]
    print("WROTE %s  %d bytes  %.1f s" % (path, os.path.getsize(path), SECS))
    print("  L rms %.1f dBFS | R rms %.1f dBFS"
          % (20 * np.log10(max(rms[0], 1) / 32768.0),
             20 * np.log10(max(rms[1], 1) / 32768.0)))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", choices=("tone", "beep", "both"), default="both")
    a = ap.parse_args()

    if a.style in ("tone", "both"):
        save(build("tone"), "2026-11-02_dac_L440_R880.wav")
    if a.style in ("beep", "both"):
        save(build("beep"), "2026-11-02_dac_L长音_R滴滴.wav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
