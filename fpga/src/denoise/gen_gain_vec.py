"""生成 dn_gain 的三帧向量：输入谱 + 期望输出（Y、噪声、增益）。

期望值来自 dn_gain_model.py 的整数实现，与 RTL 逐位对齐。

用法：python gen_gain_vec.py
输出：vec/gain_stim_f<f>.hex  每行 "re im"（8 位十六进制，补码）
      vec/gain_ref_f<f>.txt   每行 "yre yim n g"（Y 为 8 位补码，n 为 12 位，g 为 4 位）
"""
import os

import numpy as np

from dn_gain_model import GainModel

N = 512
FRAMES = 3
here = os.path.dirname(os.path.abspath(__file__))
vec = os.path.join(here, "vec")
os.makedirs(vec, exist_ok=True)

rng = np.random.default_rng(20261031)


def hex32(v):
    return f"{int(v) & 0xFFFFFFFF:08x}"


def hex48(v):
    return f"{int(v) & ((1 << 48) - 1):012x}"


def hex16(v):
    return f"{int(v) & 0xFFFF:04x}"


model = GainModel(N)
for f in range(FRAMES):
    # 幅度随帧变化，模拟"先安静后有声音"
    amp = [20000, 90000, 300000][f]
    re = rng.integers(-amp, amp, N).astype(np.int64)
    im = rng.integers(-amp, amp, N).astype(np.int64)

    with open(os.path.join(vec, f"gain_stim_f{f}.hex"), "w", encoding="ascii") as fh:
        for i in range(N):
            fh.write(f"{hex32(re[i])} {hex32(im[i])}\n")

    g, y_re, y_im = model.process(re, im, warmup=0)

    with open(os.path.join(vec, f"gain_ref_f{f}.txt"), "w", encoding="ascii") as fh:
        for i in range(N):
            fh.write(f"{hex32(y_re[i])} {hex32(y_im[i])} "
                     f"{hex48(model.noise[i])} {hex16(g[i])}\n")

    print(f"frame {f}: amp={amp}  g[0:4]={g[:4].tolist()}  n[0:4]={model.noise[:4].tolist()}  "
          f"y_re[0:2]={y_re[:2].tolist()}")

print("done ->", vec)
