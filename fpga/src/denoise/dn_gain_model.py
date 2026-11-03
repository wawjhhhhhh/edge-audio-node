"""dn_gain 的定点参考模型（与 RTL 逐位对齐）。

RTL 里每个 bin 顺序执行：功率 -> 噪声跟踪 -> 比值 -> 开方 -> 时间平滑。
本文件用同一套整数运算在 numpy 里复现，用来给 testbench 生成期望值。

定点约定（与设计文档 §5 一致）：
  X         : 复数 32 bit Q1.22，FFT 未归一（增益 512），输入预缩放使 |X| < 2^30
  功率域    : xr = X.re >> 8，P = xr^2 + xi^2（48 bit，Q? 只要比值对就行）
  噪声 N    : 48 bit，同一功率域
  增益      : Q1.15（32767 = 1.0）
  平滑      : g = (166*g + 90*g_prev) >> 8
"""
import numpy as np

POW_SHIFT = 8          # X >> 8 再平方
GATE_Q8 = 740          # 1.7^2 * 256 = 739.8 -> 740
A_Q8, B_Q8 = 243, 13   # 0.95/0.05
RISE_SHIFT = 11        # N + (N>>11) ≈ N*1.00049
ALPHA_NUM, ALPHA_DEN = 5, 2          # alpha = 2.5
SMOOTH_NEW, SMOOTH_OLD = 166, 90     # 0.65 / 0.35
FLOOR_Q15 = 1638       # 0.05
MASK48 = (1 << 48) - 1


def clz48(v):
    return 48 - int(v).bit_length() if v else 48


def bin_step(re, im, n_prev, g_prev, warmup, ps_prev=0, p_prev_bin=0):
    """返回 (n_new, g_new, p, q24, t24, ps_m) 全为整数。

    功率先平滑再算比值（与 RTL 的 S_CMP/S_PS 两级一致）：
        ps_c = (P[k] + P[k-1]) >> 1         1 bin 因果
        ps_m = (ps_prev + ps_c) >> 1        帧间 EMA 0.5（ps_prev 是上一帧同 bin 的值）
    这样逐 bin 功率的自由度从 2 提到 ~4，噪声估计才追得上。
    """
    xr = re >> POW_SHIFT
    xi = im >> POW_SHIFT
    p = (xr * xr + xi * xi) & MASK48          # 48 bit 饱和截断（实际不会溢出）
    ps_c = ((p + p_prev_bin) >> 1) & MASK48
    ps = ((ps_prev + ps_c) >> 1) & MASK48

    # ---- 噪声跟踪（预热期直接跟随；否则首帧用 P/4 起步）
    if warmup:
        n = ps & MASK48
    elif n_prev == 0:
        n = (ps >> 2) & MASK48
    else:
        thr = ((n_prev * GATE_Q8) >> 8) & MASK48
        if ps < thr:
            n = ((n_prev * A_Q8) + (ps * B_Q8)) >> 8
        else:
            rise = n_prev + (n_prev >> RISE_SHIFT) + 1
            n = min(rise, ps)
    n &= MASK48

    # ---- 比值 q = alpha*N/P，Q1.24，夹到 2^24
    num = (ALPHA_NUM * n) & MASK48
    den = (ALPHA_DEN * ps) & MASK48
    if den == 0:
        q = 0xFFFFFF
    else:
        # 把 den 的最高位对齐到 bit23：小的往左移（保精度），大的往右移
        s = 23 - (int(den).bit_length() - 1)
        if s >= 0:
            d24 = (den << s) & 0xFFFFFF          # RTL 的 d24_r 只有 24 位
            num2 = (num << s) & MASK48
        else:
            d24 = den >> (-s)
            num2 = num >> (-s)
        if d24 == 0:
            q = 0xFFFFFF
        elif num2 >= d24:
            q = 0xFFFFFF
        else:
            q = (num2 << 24) // d24
    q = min(q, 0xFFFFFF)          # RTL 的 q_r 只有 24 位

    # ---- g = sqrt(1 - q)，Q1.15
    t = 0xFFFFFF - q                     # RTL 用 24'hFFFFFF，不是 2^24
    g = isqrt((t << 6) & 0xFFFFFFFF)
    g = max(FLOOR_Q15, min(g, 32767))

    # ---- 预热期直通（RTL 与浮点模型在这里有意不同：模型用 floor，
    #      RTL 用直通，避免每次开播有 0.32 s 的近静音）
    if warmup:
        g = 32767
    gs = (SMOOTH_NEW * g + SMOOTH_OLD * g_prev) >> 8
    gs = max(FLOOR_Q15, min(gs, 32767))
    return n, gs, p, q, t, ps


def isqrt(x):
    s = int(x ** 0.5)
    while (s + 1) * (s + 1) <= x:
        s += 1
    while s * s > x:
        s -= 1
    return s


class GainModel:
    """逐帧处理一串 512 点的复数谱。"""

    def __init__(self, n=512):
        self.n = n
        self.noise = np.zeros(n, dtype=np.int64)
        self.gprev = np.zeros(n, dtype=np.int64)
        self.ps = np.zeros(n, dtype=np.int64)     # 每 bin 的平滑功率（帧间 EMA 状态）
        self.p_prev_bin = 0                       # 上一个 bin 的 P（跨帧保留，与 RTL 一致）
        self.frames = 0

    def reset(self):
        self.noise[:] = 0
        self.gprev[:] = 0
        self.ps[:] = 0
        self.p_prev_bin = 0
        self.frames = 0

    def process(self, spec_re, spec_im, warmup=60):
        """spec_re/im: 长度 512 的 int32 数组（Q1.22）。返回 (gain_q15, y_re, y_im)。"""
        g = np.zeros(self.n, dtype=np.int64)
        y_re = np.zeros(self.n, dtype=np.int64)
        y_im = np.zeros(self.n, dtype=np.int64)
        warm = self.frames < warmup
        for i in range(self.n):
            n_new, g_new, _, _, _, ps_new = bin_step(
                int(spec_re[i]), int(spec_im[i]),
                int(self.noise[i]), int(self.gprev[i]), warm,
                ps_prev=int(self.ps[i]), p_prev_bin=self.p_prev_bin)
            xr = int(spec_re[i]) >> POW_SHIFT
            xi = int(spec_im[i]) >> POW_SHIFT
            self.p_prev_bin = (xr * xr + xi * xi) & MASK48
            self.ps[i] = ps_new
            self.noise[i] = n_new
            self.gprev[i] = g_new
            g[i] = g_new
            # Y = X * g / 2^15，Q1.22
            y_re[i] = (int(spec_re[i]) * g_new) >> 15
            y_im[i] = (int(spec_im[i]) * g_new) >> 15
        self.frames += 1
        return g, y_re, y_im


if __name__ == "__main__":
    rng = np.random.default_rng(1)
    m = GainModel()
    # 用随机谱跑 3 帧，打印前 6 个 bin 方便人工核对
    for f in range(3):
        re = rng.integers(-5000, 5000, 512).astype(np.int64)
        im = rng.integers(-5000, 5000, 512).astype(np.int64)
        g, yr, yi = m.process(re, im)
        print(f"frame {f}: g[0:6]={g[:6].tolist()} n[0:6]={m.noise[:6].tolist()}")
    print("ok")
