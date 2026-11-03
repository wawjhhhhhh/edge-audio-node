"""gen_top_pcm.py --- 为 dn_top 造帧级测试信号，并用浮点模型给出期望数字

信号结构（48 kHz，16 bit 单声道，帧移 256 点 = 5.33 ms）：
  段 A  frames   0..129   纯噪声（给噪声估计器收敛的时间，约 0.7 s）
  段 B  frames 130..189   纯噪声（在这里量压制多少 dB）
  段 C  frames 190..249   语音 + 噪声（看语音被压坏多少）
  段 D  frames 250..309   纯语音（看电平有没有跑偏）

为什么段 A 要这么长：RTL 的噪声状态从一个很小的初值起步，按 0.05 的
时间常数往上追，20~60 帧才收敛；如果一开始就测，会得到"几乎没降噪"的
假数字（这是 2026-10-31 第一次跑 TB 时踩到的坑）。

输出：
  vec/top_in.hex      每行一个 16 bit 有符号样点（十进制，TB 用 $fscanf 读）
  vec/top_expect.txt  每行 "<段名> in=<输入 dBFS> out=<期望 dBFS> delta=<差值>"
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model  # noqa: E402

RATE = 48000
HOP = 256
SEGS = [("A", 0, 130), ("B", 130, 190), ("C", 190, 250), ("D", 250, 310)]
NFRAMES = SEGS[-1][2]
N = NFRAMES * HOP


def voiced(n, rng):
    """类语音：150 Hz 基频 + 谐波 + 300 ms 起伏，带轻微抖动"""
    t = np.arange(n) / RATE
    f0 = 150.0 * (1.0 + 0.02 * np.sin(2 * np.pi * 3.0 * t))
    ph = 2 * np.pi * np.cumsum(f0) / RATE
    x = np.zeros(n)
    for k in range(1, 25):
        amp = 1.0 / (k ** 1.2)
        # 三个共振峰附近加重
        fk = k * 150.0
        for fc in (500.0, 1500.0, 2500.0):
            if abs(fk - fc) < 150.0:
                amp *= 2.2
        x += amp * np.sin(k * ph + rng.uniform(0, 2 * np.pi))
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.3 * t) ** 2
    x *= env
    return x / np.max(np.abs(x))


def build(rng):
    x = np.zeros(N)
    speech = voiced(N, rng)
    # 语音只在 C/D 段出现，段边界做 20 ms 淡入淡出避免爆音
    gate = np.zeros(N)
    for name, a, b in SEGS:
        if name in ("C", "D"):
            gate[a * HOP:b * HOP] = 1.0
    win = int(0.02 * RATE)
    k = np.ones(win) / win
    gate = np.convolve(gate, k, mode="same")
    x += 0.35 * speech * gate
    # 稳态宽带噪声（约 −40 dBFS）
    noise = rng.normal(0, 0.006, N)
    x += noise
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16)


def seg_stats(sig, a, b):
    seg = sig[a * HOP:b * HOP].astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(seg ** 2)) + 1e-12
    return 20 * np.log10(rms)


def main():
    rng = np.random.default_rng(20261031)
    pcm = build(rng)

    here = os.path.dirname(os.path.abspath(__file__))
    vec = os.path.join(here, "vec")
    os.makedirs(vec, exist_ok=True)

    with open(os.path.join(vec, "top_in.hex"), "w", encoding="utf-8") as f:
        for v in pcm:
            f.write("%d\n" % int(v))

    # 浮点参考模型（同一套参数）
    d = model.Denoiser()
    out = d.process(pcm.astype(np.float64) / 32768.0)

    lines = []
    for name, a, b in SEGS:
        din = seg_stats(pcm, a, b)
        # OLA 有 2 帧延迟，输出整体后移 2*HOP 个样点再比
        o = np.concatenate([np.zeros(2 * HOP), out])[:len(out)]
        dout = seg_stats((o * 32767).astype(np.int16), a, b)
        lines.append("%s in=%.2f out=%.2f delta=%.2f"
                     % (name, din, dout, dout - din))
    with open(os.path.join(vec, "top_expect.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("samples=%d frames=%d" % (N, NFRAMES))


if __name__ == "__main__":
    main()
