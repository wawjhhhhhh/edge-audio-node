"""生成 PL 降噪引擎需要的 ROM 与测试向量。

输出（work/eth_audio/pl/denoise/vec/）：
    tw_cos.hex   512 × 24 bit  Q1.22   cos(2πk/512)
    tw_sin.hex   512 × 24 bit  Q1.22  -sin(2πk/512)   （前向 FFT 用，逆变换在 RTL 里取反）
    win.hex      512 × 16 bit  Q1.15   sqrt-Hann
    fft_in.hex   512 × 32 bit  Q1.22   测试输入（满幅 0.5 的多音信号，故意留 1 bit 余量）
    fft_ref.txt  512 × 2 个浮点       numpy 参考 FFT 结果（TB 用来算相对误差）

为什么输入只到 0.5 满幅：FFT 有 512 倍增益，32 bit 数据路径留 1 bit 余量就够，
再小就会牺牲量化信噪比；这个取舍写在设计文档 §5。
"""

from __future__ import annotations

import os

import numpy as np

N = 512
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vec")


def q(value: float, frac_bits: int, width: int) -> int:
    """浮点 -> 定点（四舍五入 + 饱和）。"""
    scale = 1 << frac_bits
    v = int(round(value * scale))
    lo, hi = -(1 << (width - 1)), (1 << (width - 1)) - 1
    return max(lo, min(hi, v))


def to_hex(v: int, width: int) -> str:
    return format(v & ((1 << width) - 1), "0%dx" % (width // 4))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    k = np.arange(N)

    # --- 旋转因子：W = exp(-j2πk/N)，k = 0..N-1
    ang = 2.0 * np.pi * k / N
    cos_q = [q(np.cos(a), 22, 24) for a in ang]
    sin_q = [q(-np.sin(a), 22, 24) for a in ang]

    # --- sqrt-Hann 窗（周期性，n/N 不是 n/(N-1)）
    win = np.sqrt(0.5 - 0.5 * np.cos(2.0 * np.pi * k / N))
    win_q = [q(w, 15, 16) for w in win]

    # --- 测试输入：三个单音 + 一点噪声，幅度 0.5 满幅，Q1.22
    rng = np.random.default_rng(11)
    t = k / N
    sig = (0.30 * np.sin(2 * np.pi * 37 * t)
           + 0.14 * np.sin(2 * np.pi * 101 * t + 0.7)
           + 0.05 * np.sin(2 * np.pi * 229 * t + 1.9)
           + 0.004 * rng.standard_normal(N))
    sig_q = [q(s, 22, 32) for s in sig]

    # --- 参考 FFT（对定点输入做，避免"浮点输入 vs 定点输入"的误差混淆）
    x_fixed = np.array(sig_q, dtype=np.float64) / (1 << 22)
    ref = np.fft.fft(x_fixed)

    with open(os.path.join(OUT, "tw_cos.hex"), "w", encoding="ascii") as f:
        f.writelines(to_hex(v, 24) + "\n" for v in cos_q)
    with open(os.path.join(OUT, "tw_sin.hex"), "w", encoding="ascii") as f:
        f.writelines(to_hex(v, 24) + "\n" for v in sin_q)
    with open(os.path.join(OUT, "win.hex"), "w", encoding="ascii") as f:
        f.writelines(to_hex(v, 16) + "\n" for v in win_q)
    with open(os.path.join(OUT, "fft_in.hex"), "w", encoding="ascii") as f:
        f.writelines(to_hex(v, 32) + "\n" for v in sig_q)
    with open(os.path.join(OUT, "fft_ref.txt"), "w", encoding="ascii") as f:
        for v in ref:
            f.write("%.9f %.9f\n" % (v.real, v.imag))

    # 参考峰值（TB 用相对误差判据）
    print("tw_cos[0..3] =", [hex(v & 0xFFFFFF) for v in cos_q[:4]])
    print("tw_sin[1..3] =", [hex(v & 0xFFFFFF) for v in sin_q[1:4]])
    print("win[0], win[256] =", win_q[0], win_q[256])
    print("input peak = %.4f (Q1.22 = %d)" % (max(abs(np.array(sig_q))) / (1 << 22), max(abs(np.array(sig_q)))))
    print("ref |X| max = %.3f" % np.max(np.abs(ref)))
    print("wrote 5 files to", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
