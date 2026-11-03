"""生成 dn_div24 / dn_isqrt 的仿真向量。

用法：python gen_div_vec.py  （在 denoise 目录下运行）
输出：vec/div_ref.txt  每行 "n d q rem"（十六进制，q/rem 为期望值）
      vec/sqrt_ref.txt 每行 "x s"（十六进制）
"""
import os
import random

random.seed(20261031)
here = os.path.dirname(os.path.abspath(__file__))
vec = os.path.join(here, "vec")
os.makedirs(vec, exist_ok=True)

div_rows = []
# 边界：最小、最大、除尽、商 1、商满
cases = [
    (0, 1),
    (1, 1),
    (0xFFFFFF * 0x1000000 - 1, 0xFFFFFF),
    (0xFFFFFF * 0xFFFFFF, 0xFFFFFF),
    (0x800000, 0x800000),
    (0x7FFFFF, 0xFFFFFF),
]
for _ in range(200):
    d = random.randint(1, 0xFFFFFF)
    n = random.randint(0, min(0xFFFFFFFFFFFF, d * 0xFFFFFF - 1))
    cases.append((n, d))

for n, d in cases:
    q, rem = divmod(n, d)
    assert q < (1 << 24), (n, d, q)
    div_rows.append(f"{n:012x} {d:06x} {q:06x} {rem:06x}")

with open(os.path.join(vec, "div_ref.txt"), "w", encoding="ascii") as f:
    f.write("\n".join(div_rows) + "\n")

sqrt_rows = []
xcases = [0, 1, 2, 3, 4, 15, 16, 17, (1 << 32) - 1, (1 << 31), (1 << 30),
          0xFFFFFF, 0x1000000, 0x3FFFFFFF]
for _ in range(200):
    xcases.append(random.randint(0, 0xFFFFFFFF))
for x in xcases:
    s = int(x ** 0.5)
    while (s + 1) * (s + 1) <= x:
        s += 1
    while s * s > x:
        s -= 1
    sqrt_rows.append(f"{x:08x} {s:04x}")

with open(os.path.join(vec, "sqrt_ref.txt"), "w", encoding="ascii") as f:
    f.write("\n".join(sqrt_rows) + "\n")

print(f"div  向量 {len(div_rows)} 条 -> vec/div_ref.txt")
print(f"sqrt 向量 {len(sqrt_rows)} 条 -> vec/sqrt_ref.txt")
