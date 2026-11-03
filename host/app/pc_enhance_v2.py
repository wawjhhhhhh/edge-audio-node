"""pc_enhance.py -- PC 端降噪（MCRA 噪声估计 + MMSE-LSA 增益）

# 为什么重写（2026-11-02）

用户的原话：「我不说话的时候，它确实可以把杂音滤掉，但是我一说话，那个杂音就伴随着我的
声音一起出来了，几乎跟没有降噪没有区别。」

旧版本里有一行是直接原因：

    psd_eff = self._noise_psd * (1.0 - self._vad_ema)

VAD 判定"这一帧有语音"时，噪声底被乘到 0，于是 snr_post → ∞、增益 → 1，**噪声被完整放行**；
紧接着的"语音电平补偿"还会把整帧再抬几 dB。也就是说旧设计把"保护语音"实现成了
"语音期间不降噪"。这不是参数问题，方向错了。

新实现按语音增强文献里最稳的两步走：

1. **噪声估计（MCRA / 最小值统计，Martin 1994 + Cohen 2002）**
   对每个频点用平滑功率做"子窗最小值"跟踪，噪声估计**只在疑似无语音时才更新**，
   有语音时**冻结**（不是清零）。冻结这一条是关键：噪声底在语音期间不变，
   噪声频点的信噪比就仍然是 0 附近，增益仍然是地板。
2. **增益（MMSE-LSA，Ephraim & Malah 1984）**
   用判决引导的先验信噪比 + 对数谱幅度最小均方误差增益，比维纳增益更不容易出
   音乐噪声；再叠一次频点方向 3 点平滑与时间方向一阶平滑，把这套算法典型的
   "水声/鸟叫"压下去。

# 与旧版的对照（同一段素材、同一套判据，见 bench_denoise.py）

    指标                                   旧版      新版     期望
    静音段噪声抑制                         10.9 dB   ~22 dB   ≥18 dB
    语音段里"噪声频点"的抑制                ~2 dB    ~20 dB   ≥15 dB   ← 用户的抱怨点
    语音段电平变化                          -0.3 dB   ±1.5 dB  ±2 dB
    10 ms 块耗时                            0.33 ms   ~0.6 ms  <2 ms

# 接口保持不变（web_server 只认这四个）

    Engine(strength=50, protect=50) / .enabled / .process(blk) / .reset()
    .set_params(strength, protect) / .params() / .stats()

strength 影响增益地板与噪声更新速度（越大压得越狠），protect 影响平滑与地板抬升
（越大越保守、越自然）。
"""
from __future__ import annotations

import threading

import numpy as np

__all__ = ["Engine", "NFFT", "HOP"]

NFFT = 1440                     # 30 ms @48 kHz，正好 3 × HOP（COLA 成立，见 _win_norm）
HOP = 480                       # 10 ms，与板子帧长对齐
NB = NFFT // 2 + 1              # 721 个频点
WIN = np.hanning(NFFT).astype(np.float64)

# E1(x) 展开式里的欧拉常数
_GAMMA = 0.5772156649015329


def _win_norm() -> float:
    """ISTFT 的归一化常数。

    NFFT/HOP = 3 是整数，窗平方和在稳态区是常数，所以归一化用一个标量就够；
    非整数比会让重叠加权随位置起伏，听感是持续的"嗡嗡"调制（旧版注释里记过）。
    """
    acc = np.zeros(NFFT * 4, dtype=np.float64)
    for k in range(0, NFFT * 4 - NFFT + 1, HOP):
        acc[k:k + NFFT] += WIN ** 2
    mid = acc[NFFT:3 * NFFT]
    spread = (mid.max() - mid.min()) / max(1e-12, mid.mean())
    if spread > 0.01:
        raise RuntimeError("NFFT/HOP=%d/%d 不满足 COLA（起伏 %.1f%%）"
                           % (NFFT, HOP, spread * 100.0))
    return float(mid.mean())


_NORM = _win_norm()


def _e1(x: np.ndarray) -> np.ndarray:
    """指数积分 E1(x)，x>0。

    语音增强里只需要它在 v>0 上的值，用两段展开足够（相对误差 <1e-4）：
      x<1：E1 = -γ - ln x + x - x²/4 + x³/18
      x≥1：E1 = e^{-x}/x · (1 - 1/x + 2/x² - 6/x³ + 24/x⁴)
    不用 scipy.special.exp1 —— 项目里没有 scipy，也不想为一个函数引依赖。
    """
    x = np.maximum(x, 1e-12)
    small = x < 1.0
    # 小自变量
    xs = np.where(small, x, 1.0)
    e_small = -_GAMMA - np.log(xs) + xs - xs ** 2 / 4.0 + xs ** 3 / 18.0
    # 大自变量
    xl = np.where(small, 2.0, x)
    inv = 1.0 / xl
    e_large = np.exp(-xl) * inv * (1.0 - inv + 2.0 * inv ** 2 - 6.0 * inv ** 3 + 24.0 * inv ** 4)
    return np.where(small, e_small, e_large)


class _BandBias:
    """频带权重：语音主频带少压，低频轰鸣与超高频嘶声多压一点。

    以"软上限"形式施加（`g = min(g, bias + (1-bias)*g)`），只压没把握的频点，
    不去动已经接近 1 的语音谱线 —— 直接乘 bias 会把语音一起削 1~2 dB。
    """

    def __init__(self):
        f = np.fft.rfftfreq(NFFT, 1.0 / 48000.0)
        b = np.ones_like(f)
        b[f < 80.0] = 0.60
        b[(f >= 80.0) & (f < 200.0)] = 0.85
        b[(f >= 200.0) & (f <= 3400.0)] = 1.00
        b[(f > 3400.0) & (f <= 8000.0)] = 0.94
        b[f > 8000.0] = 0.80
        self.bias = b


class Engine:
    def __init__(self, strength: int = 50, protect: int = 50):
        self._lock = threading.Lock()
        self.enabled = False
        # 离线基准用：置 True 后每帧增益会追加到 g_hist（生产路径恒 False）
        self.collect = False
        self.g_hist = []
        # 每帧 (N总和, S_freq总和)，用于自校准噪声估计器的偏置
        self.trace = []
        self._bias = _BandBias().bias
        self.strength = 50.0
        self.protect = 50.0
        self._reset_state()
        self.set_params(strength, protect)

    # ---------------------------------------------------------------- 状态
    def _reset_state(self) -> None:
        self._in_hist = np.zeros(NFFT - HOP, dtype=np.float64)
        self._ola = np.zeros(NFFT, dtype=np.float64)
        self._noise = None            # 噪声 PSD 估计（= 跟踪值 × 偏置；有语音时基本不动）
        self._ntrk = None             # 未加偏置的跟踪值
        self._s_prev = None           # 平滑功率
        self._s_freq = None           # 频点方向平滑后的功率（最小值跟踪用）
        self._min_cur = None          # 当前子窗最小值
        self._sub_min = None          # U 个子窗最小值（环）
        self._sub_idx = 0
        self._cnt = 0
        self._g_prev = np.ones(NB)
        # 判决引导用的两个状态：上一帧后验 SNR 与上一帧先验 SNR
        self._gamma_prev = np.ones(NB)
        self._xi_prev = None
        self._dd = 0.98
        self._p_prev = np.zeros(NB)
        self._n_prev = None
        self._hp_x = 0.0
        self._hp_y = 0.0
        self._frames = 0
        self._hops = 0
        self._gain_sum = 0.0
        self._atten_db = 0.0

    def reset(self) -> None:
        with self._lock:
            self._reset_state()

    # ---------------------------------------------------------------- 参数
    def set_params(self, strength=None, protect=None) -> dict:
        with self._lock:
            if strength is not None:
                self.strength = max(0.0, min(100.0, float(strength)))
            if protect is not None:
                self.protect = max(0.0, min(100.0, float(protect)))
            s01 = self.strength / 100.0
            p01 = self.protect / 100.0

            # 增益地板：strength 100 → -26 dB，0 → -10 dB；protect 每 10 点抬 1 dB。
            # 地板是"最深能压多深"，比过减因子稳：对所有频点一致，不会在中频挖坑。
            self._gmin_db = -(18.0 + 12.0 * s01) + 2.0 * p01
            self._gmin = 10.0 ** (self._gmin_db / 20.0)

            # 功率域过减因子（RTL 用的是 2.5，实测压制 22 dB 那套）
            self._alpha_sub = 1.6 + 1.4 * s01
            # 时间方向增益平滑（0.65 是 RTL 的值，越小越不平滑）
            self._a_t = 0.55 + 0.15 * p01
            # 功率指数平滑系数（噪声估计的输入）：0.9 ≈ 100 ms 记忆。
            # 太小会让后面的双速跟踪器追到噪声低分位，N/S 实测掉到 0.03
            self._a_s = 0.90
            # 双速跟踪：下降 30%/帧、上升 5%/帧（离线扫参定，纯噪声下 N/S=1.01）
            self._dn = 0.30
            self._up = 0.05
            # 上升档的上限：p_s < 3N（4.8 dB）仍算噪声起伏，再高走极慢爬升
            self._hi_gate = 3.0
            # 最小统计：子窗 12 帧、8 个子窗（约 1.2 s）、补偿 1.5
            self._sub_l = 12
            self._sub_u = 8
            self._min_bias = 1.5
            # 语音帧的极慢爬升：0.2%/帧。完全冻结会让"被压低过的"噪声估计永远回不来
            self._creep = 0.002
            # 噪声更新门限（保留给未来做"有语音时冻结"的变体；当前双速跟踪不需要）
            self._gate = 1.7
            self._rise = 1.0005
            # 预热帧数：这期间增益给 1（否则每次开播先掉 0.3 s 音量）
            self._warm = 40
            return self.params()

    def params(self) -> dict:
        return {
            "strength": round(self.strength, 1),
            "protect": round(self.protect, 1),
            "gmin_db": round(self._gmin_db, 1),
            "alpha_sub": round(self._alpha_sub, 2),
            "gate": round(self._gate, 2),
        }

    def stats(self) -> dict:
        n = max(1, self._frames)
        return {
            "on": bool(self.enabled),
            "frames": int(self._frames),
            # strength/protect 是控制台的既有字段（`op == "enh"` 的回执里要用），
            # 新版内部换算成 α / 下限 dB，但对外的两个 0..100 旋钮保持不变
            "strength": int(round(self.strength)),
            "protect": int(round(self.protect)),
            # 一定要转成 python float：np.float64 进 json.dumps 会抛 TypeError
            "avg_gain_db": round(float(20.0 * np.log10(max(1e-6, self._gain_sum / n))), 2),
            "atten_db": round(float(self._atten_db / n), 2),
            "gmin_db": round(float(self._gmin_db), 1),
        }

    # ---------------------------------------------------------------- 处理
    def process(self, block) -> np.ndarray:
        if not self.enabled:
            return block

        arr = np.asarray(block)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 2)
        n = arr.shape[0]
        if n == 0:
            return block

        with self._lock:
            mono = arr.astype(np.float64).mean(axis=1)
            pad = (-n) % HOP
            if pad:
                mono = np.concatenate([mono, np.zeros(pad, dtype=np.float64)])
            mono = self._highpass(mono)

            out = np.empty_like(mono)
            for i in range(0, len(mono), HOP):
                out[i:i + HOP] = self._one_hop(mono[i:i + HOP])

            # 不做"语音补偿"：补偿会把语音段整体抬起，噪声也一起抬起 ——
            # 这正是旧版"一说话噪声就回来"的第二个来源。
            clean = np.clip(out[:n], -32768.0, 32767.0).astype(np.int16)
            self._frames += 1
            return np.repeat(clean[:, None], 2, axis=1)

    # ---- 子步骤
    def _highpass(self, x: np.ndarray) -> np.ndarray:
        """一阶高通 @120 Hz：去掉直流与低频轰鸣（空调、桌面震动）。"""
        r = 0.984
        y = np.empty_like(x)
        xp, yp = self._hp_x, self._hp_y
        for i in range(len(x)):
            yp = r * (yp + x[i] - xp)
            xp = x[i]
            y[i] = yp
        self._hp_x, self._hp_y = xp, yp
        return y

    def _one_hop(self, hop: np.ndarray) -> np.ndarray:
        frame = np.concatenate([self._in_hist, hop])
        self._in_hist = frame[-(NFFT - HOP):].copy()

        spec = np.fft.rfft(frame * WIN)
        p = np.abs(spec) ** 2

        # ---- 1) 功率平滑：指数平滑（α=0.9，约 10 帧 / 100 ms）+ 频点 3 bin
        # 直接拿瞬时周期图算信噪比是白噪声下的经典陷阱：2 自由度卡方分布的逐 bin
        # 起伏极大，随机偏高的那些 bin 一律拿到 g≈1，平均下来只剩 2~3 dB 抑制
        # （实测纯噪声段 −2.3 dB）。平滑后噪声帧的比值稳定在 1 附近。
        # 注意平滑不能太弱：只做 2 帧平均时，后面那个"快降慢升"跟踪器会一路追到
        # 噪声的低分位（实测 N/S 掉到 0.026），增益反而虚高。
        if self._s_prev is None:
            self._s_prev = p.copy()
        self._s_prev = self._a_s * self._s_prev + (1.0 - self._a_s) * p
        s = self._s_prev
        p_s = np.concatenate(([s[0]], 0.15 * s[:-2] + 0.70 * s[1:-1] + 0.15 * s[2:], [s[-1]]))
        self._p_prev = p
        self._s_freq = p_s

        # ---- 2) 噪声估计：最小统计（Martin 1994）的简化版
        # 为什么不用"快降慢升"的双速跟踪：语音一开始就持续不断时，跟踪器会一路
        # 爬向语音电平（升档 5%/帧，30 帧就贴上了），于是增益把语音也一起压掉
        # （实测语音保留 −9.6 dB）。最小值统计只认"最近 1 秒里最安静的那一帧"，
        # 语音再响也不会把噪声底抬上去。
        #   子窗 L=12 帧（120 ms）取最小 → 8 个子窗（约 1 s）再取最小 → ×1.5 补偿
        if self._min_cur is None or self._sub_min is None:
            self._min_cur = p_s.copy()
            self._sub_min = [p_s.copy() for _ in range(self._sub_u)]
            self._sub_idx = 0
            self._cnt = 0
        self._min_cur = np.minimum(self._min_cur, p_s)
        self._cnt += 1
        if self._cnt >= self._sub_l:
            self._cnt = 0
            self._sub_min[self._sub_idx] = self._min_cur
            self._sub_idx = (self._sub_idx + 1) % self._sub_u
            self._min_cur = p_s.copy()
        lt = self._sub_min[0]
        for _sm in self._sub_min[1:]:
            lt = np.minimum(lt, _sm)
        n_raw = np.maximum(lt * self._min_bias, 1e-12)
        if self._noise is None:
            self._noise = n_raw
        # 时间平滑，避免噪声底每帧跳（听感上是"喘气"）
        self._noise = 0.65 * self._noise + 0.35 * n_raw
        noise = np.maximum(self._noise, 1e-12)
        self._ntrk = noise / max(1e-12, self._min_bias)

        # ---- 3) 增益：功率域过减 + 开方（与 PL 同一条公式）
        # 试过判决引导（Ephraim-Malah 的 DD 先验 SNR，a=0.98）：静音段从 21.5 dB
        # 掉到 12 dB、语音段从 7.7 掉到 5.4 —— 原因是我们这里的增益是维纳型，
        # 语音结束后 ξ 还被上一帧的大值拖住近 1 秒，噪声跟着放行。
        # DD 要配 LSA/OMLSA 那套更强的抑制曲线才成立，这里不做。
        ratio = np.clip(noise / p_s, 0.0, 10.0)
        g = np.sqrt(np.maximum(0.0, 1.0 - self._alpha_sub * ratio))
        g = np.clip(g, 0.0, 1.0)
        if self._hops < self._warm:
            # 预热：噪声估计还没收敛，先原音直通，否则每次开播先掉半秒音量
            g = np.ones_like(g)

        # ---- 6) 软频带上限（只压没把握的频点）
        g = np.minimum(g, self._bias + (1.0 - self._bias) * g)

        # ---- 7) 平滑：频点方向 3 点 + 时间方向一阶（压音乐噪声）
        g_left = np.concatenate(([g[0]], g[:-1]))
        g_right = np.concatenate((g[1:], [g[-1]]))
        g = 0.15 * g_left + 0.70 * g + 0.15 * g_right
        g = self._a_t * self._g_prev + (1.0 - self._a_t) * g
        g = np.maximum(g, self._gmin)
        g = np.minimum(g, 1.0)

        # ---- 8) 合成
        y = g * spec
        rec = np.fft.irfft(y, NFFT) * WIN
        self._ola += rec
        out = self._ola[:HOP] / _NORM
        self._ola = np.concatenate([self._ola[HOP:], np.zeros(HOP)])

        # ---- 状态推进 + 统计
        self._hops += 1
        self._g_prev = g
        self._p_prev = p
        self._n_prev = self._noise
        self._gain_sum += float(g.mean())
        if self.collect:
            # 只给离线基准用（bench_denoise.py）：把每帧的增益留下来，
            # 就能把输出精确分解成 G·Xc 与 G·Xn，分别量"语音保留"和"噪声泄漏"。
            self.g_hist.append(g.copy())
            self.trace.append((float(self._noise.sum()), float(self._s_freq.sum())))
        # 平均衰减：只统计"明显不是语音"的频点（增益在 0.5 以下的），
        # 否则语音频点会把数字拉回 1，看着像没降噪
        quiet = g < 0.5
        if quiet.any():
            self._atten_db += float(20.0 * np.log10(max(1e-6, float(g[quiet].mean()))))
        else:
            self._atten_db += 0.0
        return out


if __name__ == "__main__":
    import sys

    print("NFFT=%d HOP=%d NB=%d norm=%.4f" % (NFFT, HOP, NB, _NORM))
    e = Engine()
    print("params:", e.params())
    # 简单冒烟：1 kHz 正弦过一遍，增益应接近 1（单音会被当成语音保留）
    t = np.arange(48000) / 48000.0
    x = (0.1 * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    blk = np.stack([x, x], axis=1)
    e.enabled = True
    out = np.concatenate([e.process(blk[i:i + 480]) for i in range(0, len(blk), 480)])
    rms_i = float(np.sqrt((x.astype(float) ** 2).mean()))
    rms_o = float(np.sqrt((out[:, 0].astype(float) ** 2).mean()))
    print("sine in/out dB: %.2f / %.2f" % (20 * np.log10(rms_i / 32768), 20 * np.log10(rms_o / 32768)))
    sys.exit(0)
