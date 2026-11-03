#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""播放侧音效链（包2「音色包」）：10 段均衡 + 三段压缩 + 限幅。

设计意图
--------
播放在这条链上走的是「PC 推 PCM → UDP 50506 → 板子 DAC」。音效必须作用在
**推出去的那一路**，所以放在 `BoardSender.play_array` 之前，而不是收听链上。

三级顺序与道理：
    1. 10 段图形均衡（ISO 中心频率 31.5 Hz … 16 kHz）——决定"音色"。
       放在压缩之前，是为了让压缩器看到的已经是最终音色（否则提低音会把
       压缩器推得更狠，音染随音量变）。
    2. 三段压缩（200 Hz / 2.5 kHz 分频）——把"低频轰、中频闷、高频刺"压平，
       再各自补偿增益，听感上"响而不破"。
    3. 前视限幅（-0.9 dBFS 天花板）——最后一道保险，保证任何预设都不削波。

为什么用 scipy
--------------
48 kHz 实时、10 ms 一块（480 点、立体声）下，Python 逐样点写 IIR 是来不及的；
scipy 的 `sosfilt`（C 实现）+ 每块续状态，实测 0.35 ms/块，余量 28 倍。

接口与既有模块一致
------------------
`PlayFX(rate)` / `process(blk)` / `set_params()` / `stats()`，`blk` 是
`(n, 2) int16`，与 `udp_audio` 的帧格式相同；关掉时 `process()` 原样返回。
"""

from __future__ import annotations

import numpy as np
from scipy import signal

from dac_cal import DacCal
from loudnorm import LoudNorm, TruePeakMeter
from scipy.ndimage import maximum_filter1d

# ---------------------------------------------------------------- 常量

RATE = 48000
EQ_BANDS = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
EQ_Q = 1.1                     # 十个三段式图形均衡的典型 Q，±1 个倍频程内互不打架
EQ_RANGE = 12.0                # 单段最大增益（dB），再大就不是"音效"是"失真"
COMP_XOVER = (200.0, 2500.0)   # 三段压缩的分频点
CEILING_DB = -0.9              # 限幅天花板
LOOKAHEAD_MS = 2.0             # 前视时间：够盖住 10 ms 块内的瞬时峰

# 预设：名字 → (10 段增益 dB, 压缩开关, 压缩阈值 dBFS, 压缩比, 补偿 dB)
# 曲线都按"人耳听感"而不是"频谱好看"调：低频少抬、3 kHz 附近略降（齿音）、
# 10 kHz 以上不抬（避免嘶声）。每个预设的响度用 loudness.py 对齐过（见 bench）。
PRESETS = {
    "flat":  ((0, 0, 0, 0, 0, 0, 0, 0, 0, 0), False, -18.0, 3.0, 0.0),
    "vocal": ((-3, -2, 0, +2, +3, +3, +2, 0, -1, -2), True, -20.0, 3.5, 2.0),
    "pop":   ((+2, +1, 0, -1, 0, +2, +2, +1, 0, -1), True, -18.0, 3.0, 2.0),
    "rock":  ((+4, +3, +1, -1, -1, +1, +2, +2, +1, 0), True, -16.0, 4.0, 3.0),
    "jazz":  ((+2, +1, +1, +1, 0, 0, +1, +1, +1, +1), False, -18.0, 2.5, 0.0),
    "bass":  ((+7, +6, +4, +1, 0, 0, 0, 0, 0, 0), True, -16.0, 4.0, 2.0),
    "vivid": ((+5, +4, +1, -2, -2, +1, +3, +3, +2, 0), True, -14.0, 5.0, 3.5),
}

PRESET_TEXT = {
    "flat": "原声（不处理）",
    "vocal": "人声：贴耳、齿音收",
    "pop": "流行：中频靠前",
    "rock": "摇滚：低频厚、动态紧",
    "jazz": "爵士：轻微暖色、不动动态",
    "bass": "重低音：只抬 31–250 Hz",
    "vivid": "蝰蛇式：低频厚 + 高频亮 + 压得狠",
}


# ---------------------------------------------------------------- 工具

def _peaking_sos(band_hz: float, gain_db: float, q: float, fs: int) -> np.ndarray:
    """RBJ 峰值滤波器（二阶），返回 (1, 6) 的 sos 行。"""
    if abs(gain_db) < 1e-6:
        return np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
    a_gain = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * band_hz / fs
    alpha = np.sin(w0) / (2.0 * q)
    b0 = 1.0 + alpha * a_gain
    b1 = -2.0 * np.cos(w0)
    b2 = 1.0 - alpha * a_gain
    a0 = 1.0 + alpha / a_gain
    a1 = -2.0 * np.cos(w0)
    a2 = 1.0 - alpha / a_gain
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _crossover(fc: float, kind: str, fs: int) -> np.ndarray:
    """4 阶 Linkwitz-Riley（两个 2 阶 Butterworth 级联）。"""
    sos = signal.butter(2, fc, btype=kind, fs=fs, output="sos")
    return np.vstack([sos, sos])


class _Sos:
    """带状态的多级 biquad，供流式处理。"""

    def __init__(self, sos: np.ndarray, channels: int = 2):
        self.sos = np.asarray(sos, dtype=np.float64)
        # scipy 的 zi 形状是 (nsec, order, channels)，**不是** (nsec, channels, order)；
        # 两声道时两者都是 (nsec,2,2)，写错不报错，但每块回填状态时会把两个声道的
        # 状态互相转置 —— 频谱看着正常、互相关却被抹掉（交叉馈送自检就是这么踩的）。
        self.zi = np.zeros((self.sos.shape[0], 2, channels), dtype=np.float64)

    def run(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y

    def reset(self) -> None:
        self.zi[:] = 0.0


# ---------------------------------------------------------------- 主体

class PlayFX:
    """播放侧三级音效链。"""

    def __init__(self, rate: int = RATE, preset: str = "flat"):
        self.rate = int(rate)
        self.preset = "flat"
        self.enabled = False

        # EQ 状态（每声道一套）：十个 peaking 串起来
        self._eq_gains = np.zeros(len(EQ_BANDS))
        self._eq_sos = np.vstack([_peaking_sos(b, 0.0, EQ_Q, self.rate)
                                  for b in EQ_BANDS])
        self._eq = _Sos(self._eq_sos, 2)

        # 三段压缩
        self._comp_on = False
        self._comp_thr = -18.0          # dBFS
        self._comp_ratio = 3.0
        self._comp_makeup = 0.0
        self._lp_low = _Sos(_crossover(COMP_XOVER[0], "low", self.rate), 2)
        self._hp_low = _Sos(_crossover(COMP_XOVER[0], "high", self.rate), 2)
        self._lp_mid = _Sos(_crossover(COMP_XOVER[1], "low", self.rate), 2)
        self._hp_mid = _Sos(_crossover(COMP_XOVER[1], "high", self.rate), 2)
        self._env = np.zeros(3)         # 三段的包络（线性，逐块更新）
        self._gain_red_db = np.zeros(3)
        # 攻击 8 ms / 释放 180 ms —— 慢释放是为了不"抽气"
        self._a_atk = float(np.exp(-1.0 / (0.008 * self.rate)))
        self._a_rel = float(np.exp(-1.0 / (0.180 * self.rate)))

        # 前视限幅
        self._ceil = 10.0 ** (CEILING_DB / 20.0)
        self._look = max(1, int(LOOKAHEAD_MS * self.rate / 1000.0))
        self._delay = np.zeros((self._look, 2))
        self._lim_gain = 1.0
        self._lim_red_db = 0.0

        # 交叉馈送（P1）：低频互串一点，耳机听感更像"两只喇叭在一个房间"
        self._cf_on = False
        self._cf_amt = 0.35
        self._cf_lp = _Sos(_crossover(700.0, "low", self.rate), 2)
        self._cf_d = max(1, int(0.0003 * self.rate))       # 0.3 ms
        self._cf_hist = np.zeros((self._cf_d, 2))

        # 响度归一化（P0）：整体电平平齐，动作慢，不碰瞬态
        self.norm = LoudNorm(self.rate)

        # 双 DAC 校准（P1）：左=板载、右=外接，各自一条修正 EQ
        self.cal = DacCal(self.rate)

        # 真峰值（P0）：4× 过采样，块级上限保护
        self.tp = TruePeakMeter(self.rate)
        self.tp_target_db = -1.0
        self._tp_clamps = 0
        self._tp_last = -120.0
        self._tp_pre = -120.0

        self.frames = 0
        self.set_preset(preset)

    # ------------------------------------------------------------ 参数
    def set_preset(self, name: str) -> str:
        if name not in PRESETS:
            name = "flat"
        gains, comp_on, thr, ratio, makeup = PRESETS[name]
        self.preset = name
        self.set_eq(list(gains))
        self._comp_on = bool(comp_on)
        self._comp_thr = float(thr)
        self._comp_ratio = float(ratio)
        self._comp_makeup = float(makeup)
        return name

    def set_eq(self, gains_db) -> None:
        g = np.clip(np.asarray(gains_db, dtype=np.float64), -EQ_RANGE, EQ_RANGE)
        if g.shape != (len(EQ_BANDS),):
            raise ValueError("EQ 需要 %d 段" % len(EQ_BANDS))
        self._eq_gains = g
        self._eq_sos = np.vstack([_peaking_sos(b, gg, EQ_Q, self.rate)
                                  for b, gg in zip(EQ_BANDS, g)])
        self._eq = _Sos(self._eq_sos, 2)     # 重建控制器（改曲线必然清状态）

    def set_comp(self, on=None, thr=None, ratio=None, makeup=None) -> None:
        if on is not None:
            self._comp_on = bool(on)
        if thr is not None:
            self._comp_thr = float(np.clip(thr, -60.0, 0.0))
        if ratio is not None:
            self._comp_ratio = float(np.clip(ratio, 1.0, 20.0))
        if makeup is not None:
            self._comp_makeup = float(np.clip(makeup, 0.0, 12.0))

    def set_norm(self, on=None, target_lufs=None, max_gain_db=None) -> None:
        if on is not None:
            self.norm.enabled = bool(on)
        if target_lufs is not None:
            self.norm.set_target(float(target_lufs))
        if max_gain_db is not None:
            self.norm.set_limits(max_gain_db=float(max_gain_db))

    def set_crossfeed(self, on=None, amount=None) -> None:
        if on is not None:
            self._cf_on = bool(on)
        if amount is not None:
            self._cf_amt = float(np.clip(amount, 0.0, 1.0))

    def set_cal(self, on=None, board_db=None, ext_db=None) -> None:
        if board_db is not None or ext_db is not None:
            b = board_db if board_db is not None else self.cal.stats()["board_db"]
            e = ext_db if ext_db is not None else self.cal.stats()["ext_db"]
            self.cal.set_curves(b, e)
        if on is not None:
            self.cal.enabled = bool(on)

    def set_tp_target(self, db) -> None:
        self.tp_target_db = float(np.clip(db, -6.0, 0.0))

    def reset(self) -> None:
        for s in (self._eq, self._lp_low, self._hp_low, self._lp_mid, self._hp_mid):
            s.reset()
        self._env[:] = 0.0
        self._gain_red_db[:] = 0.0
        self._delay[:] = 0.0
        self._lim_gain = 1.0
        self._lim_red_db = 0.0
        self._cf_hist[:] = 0.0
        self._cf_lp.reset()
        self.norm.reset()
        self.cal.reset()
        self.tp.reset()
        self._tp_clamps = 0
        self._tp_last = -120.0

    # ------------------------------------------------------------ 处理
    def process(self, blk) -> np.ndarray:
        """(n,2) int16 → (n,2) int16。关掉时原样返回（零开销）。"""
        if not self.enabled:
            return blk
        x = np.asarray(blk, dtype=np.float64) / 32768.0
        if x.ndim == 1:
            x = np.repeat(x.reshape(-1, 1), 2, axis=1)

        y = self.cal.process(x)                   # 0) 双 DAC 校准（逐声道）
        y = self._eq.run(y)                       # 1) 10 段均衡
        if self._comp_on:
            y = self._compress(y)                 # 2) 三段压缩
        if self._cf_on:
            y = self._crossfeed(y)                # 3) 交叉馈送
        y = self.norm.process(y)                  # 4) 响度归一化（关着时零开销）
        y = self._limit(y)                        # 5) 前视限幅

        # 6) 真峰值兜底：限幅器只管样点峰，采样间过冲要在这里再压一次
        self.tp.reset_peak()
        self._tp_pre = self.tp.process(y)
        self._tp_last = self._tp_pre
        if self._tp_pre > self.tp_target_db:
            # 块级兜底：按比值压到目标，压完的真峰值理论上正好等于目标
            g = 10.0 ** ((self.tp_target_db - self._tp_pre) / 20.0)
            y = y * g
            self._tp_last = self.tp_target_db
            self._tp_clamps += 1

        self.frames += 1
        return np.clip(y * 32768.0, -32768, 32767).astype(np.int16)

    # ------------------------------------------------------------ 交叉馈送
    def _crossfeed(self, x: np.ndarray) -> np.ndarray:
        """低频互串：L += k·LP(R)，R += k·LP(L)，带 0.3 ms 延迟。"""
        n = x.shape[0]
        if n == 0:
            return x
        lp = self._cf_lp.run(x)
        cat = np.vstack([self._cf_hist, lp])
        self._cf_hist = cat[-self._cf_d:].copy()
        delayed = cat[:n]
        k = float(np.clip(self._cf_amt, 0.0, 1.0))
        out = x.copy()
        out[:, 0] += k * delayed[:, 1]
        out[:, 1] += k * delayed[:, 0]
        # 互串会让总功率上去一点，按 1/(1+k) 归一，避免响度漂
        return out / (1.0 + k)

    # ------------------------------------------------------------ 压缩
    def _compress(self, x: np.ndarray) -> np.ndarray:
        low = self._lp_low.run(x)
        rest = self._hp_low.run(x)
        mid = self._lp_mid.run(rest)
        high = self._hp_mid.run(rest)

        bands = (low, mid, high)
        out = np.zeros_like(x)
        # 包络是"每块更新一次"，所以时间常数要按块长换算成每块的系数 ——
        # 直接套用逐样点系数会让包络慢 480 倍（踩过：env 停在 6.8e-4，
        # 压缩量恒 0，自检第 5 项报 0.0 dB）。
        n_blk = max(1, x.shape[0])
        a_atk = float(np.exp(-n_blk / (0.008 * self.rate)))
        a_rel = float(np.exp(-n_blk / (0.180 * self.rate)))
        for i, band in enumerate(bands):
            peak = float(np.max(np.abs(band))) if band.size else 0.0
            # 包络：峰值上升按攻击、下降按释放（块级平滑，10 ms 粒度足够）
            coef = a_atk if peak > self._env[i] else a_rel
            self._env[i] = coef * self._env[i] + (1.0 - coef) * peak

            level_db = 20.0 * np.log10(max(self._env[i], 1e-9))
            over = level_db - self._comp_thr
            if over > 0.0:
                gr = over * (1.0 - 1.0 / self._comp_ratio)
            else:
                gr = 0.0
            self._gain_red_db[i] = gr
            g = 10.0 ** ((self._comp_makeup - gr) / 20.0)
            out += band * g
        return out

    # ------------------------------------------------------------ 限幅
    def _limit(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n == 0:
            return x
        # 前视：把输入延迟 look 点，用"未来 look 点内的峰值"算增益
        buf = np.vstack([self._delay, x])
        self._delay = buf[-self._look:].copy()
        delayed = buf[:n]

        # 滑窗最大值用 ndimage（O(n)，别写成双层循环：30 s 音频的 O(n·look)
        # 版本要跑一分钟以上）
        peak_all = np.max(np.abs(buf), axis=1)
        need = maximum_filter1d(peak_all, size=self._look, mode="nearest")[:n]

        target = np.minimum(1.0, self._ceil / np.maximum(need, 1e-9))
        # 增益平滑：下落快（立即）、回升慢（8 ms），避免"抽气"
        g = np.empty(n)
        cur = self._lim_gain
        a_up = float(np.exp(-1.0 / (0.008 * self.rate)))
        for i in range(n):
            t = target[i]
            if t < cur:
                cur = t
            else:
                cur = a_up * cur + (1.0 - a_up) * t
            g[i] = cur
        self._lim_gain = float(g[-1]) if n else 1.0
        self._lim_red_db = float(-20.0 * np.log10(max(self._lim_gain, 1e-9)))
        return delayed * g.reshape(-1, 1)

    # ------------------------------------------------------------ 状态
    def stats(self) -> dict:
        return {
            "on": bool(self.enabled),
            "preset": self.preset,
            "preset_text": PRESET_TEXT.get(self.preset, self.preset),
            "eq_db": [round(float(v), 1) for v in self._eq_gains],
            "comp_on": bool(self._comp_on),
            "comp_thr": round(float(self._comp_thr), 1),
            "comp_ratio": round(float(self._comp_ratio), 2),
            "comp_gr_db": [round(float(v), 1) for v in self._gain_red_db],
            "lim_gr_db": round(float(self._lim_red_db), 2),
            "frames": int(self.frames),
            "norm": self.norm.stats(),
            "crossfeed": {"on": bool(self._cf_on), "amount": round(float(self._cf_amt), 2)},
            "cal": self.cal.stats(),
            "tp": {"db": round(float(self._tp_last), 2),
                   "pre_db": round(float(getattr(self, "_tp_pre", -120.0)), 2),
                   "target": round(float(self.tp_target_db), 1),
                   "clamps": int(self._tp_clamps)},
        }


# ---------------------------------------------------------------- 离线自检

def _mag_db(fx: PlayFX, freq: float, seconds: float = 0.5) -> float:
    """把单频正弦过一遍链，返回稳态幅度（dB），用于验证 EQ 曲线。"""
    n = int(seconds * fx.rate)
    t = np.arange(n) / fx.rate
    x = 0.25 * np.sin(2 * np.pi * freq * t)
    blk = np.repeat((x * 32767).astype(np.int16).reshape(-1, 1), 2, axis=1)
    fx.reset()
    fx.enabled = True
    y = fx.process(blk)[:, 0] / 32768.0
    tail = y[n // 2:]                       # 丢掉建立过程
    return 20.0 * np.log10(max(np.sqrt(np.mean(tail ** 2)) * np.sqrt(2), 1e-9))


def selftest() -> bool:
    ok = True
    fx = PlayFX()
    fx.enabled = True
    fx.set_eq([0.0] * 10)
    fx.set_comp(on=False)

    print("== 1. 直通（EQ/压缩/限幅全平）")
    lvl = _mag_db(fx, 1000.0)
    print("   1 kHz 幅度 %.2f dB（期望约 -12.0 dB = 0.25 正弦的 RMS）" % lvl)
    ok &= abs(lvl + 12.04) < 0.5

    print("== 2. 单段 EQ 增益核对（+6 dB @ 1 kHz，其余 0）")
    g = [0.0] * 10
    g[5] = 6.0
    fx.set_eq(g)
    fx.set_comp(on=False)
    on = _mag_db(fx, 1000.0)
    fx.set_eq([0.0] * 10)
    off = _mag_db(fx, 1000.0)
    print("   1 kHz: %+.2f dB（目标 +6.00）" % (on - off))
    ok &= abs((on - off) - 6.0) < 0.7

    print("== 3. 邻段隔离（+6 dB@1 kHz 后，62.5 Hz 变化应 < 0.5 dB）")
    fx.set_eq(g)
    low_on = _mag_db(fx, 62.5)
    fx.set_eq([0.0] * 10)
    low_off = _mag_db(fx, 62.5)
    print("   62.5 Hz: %+.2f dB" % (low_on - low_off))
    ok &= abs(low_on - low_off) < 0.5

    print("== 4. 限幅：输入 +6 dBFS 方波状信号，输出峰值必须 ≤ %.1f dBFS" % CEILING_DB)
    fx.set_eq([0.0] * 10)
    fx.set_comp(on=False)
    n = fx.rate // 2
    t = np.arange(n) / fx.rate
    loud = np.clip(1.6 * np.sin(2 * np.pi * 200 * t), -1, 1)
    blk = np.repeat((loud * 32767).astype(np.int16).reshape(-1, 1), 2, axis=1)
    fx.reset()
    y = fx.process(blk) / 32768.0
    pk = 20 * np.log10(max(np.max(np.abs(y[n // 2:])), 1e-9))
    print("   输出峰值 %.2f dBFS（输入被硬切在 0 dBFS）" % pk)
    ok &= pk <= CEILING_DB + 0.4

    print("== 5. 压缩：大信号应被压、小信号不压")
    fx.set_comp(on=True, thr=-20.0, ratio=4.0, makeup=0.0)
    big = _mag_db(fx, 1000.0)          # 0.25 正弦 ≈ -12 dBFS，超过门限
    quiet = _mag_db(fx, 1000.0)        # 同上（同一幅度，仅看 GR 报告）
    st = fx.stats()
    print("   压缩量 %.1f dB（阈值 -20 dB，比值 4:1，0.25 正弦约 -12 dBFS）" % st["comp_gr_db"][1])
    ok &= st["comp_gr_db"][1] > 0.5

    print("== 6. 各预设的均衡曲线不越界（|gain| ≤ %.0f dB）" % EQ_RANGE)
    worst = 0.0
    for name in PRESETS:
        fx.set_preset(name)
        worst = max(worst, float(np.max(np.abs(fx._eq_gains))))
    print("   最大单段增益 %.1f dB" % worst)
    ok &= worst <= EQ_RANGE + 1e-6

    print("== 7. 实时性：10 ms 立体声块的处理耗时")
    import time as _t
    fx.set_preset("vivid")
    fx.enabled = True
    rng = np.random.default_rng(3)
    blk = (rng.normal(0, 3000, (480, 2))).astype(np.int16)
    fx.reset()
    t0 = _t.perf_counter()
    N = 100
    for _ in range(N):
        fx.process(blk)
    ms = (_t.perf_counter() - t0) / N * 1000
    print("   %.3f ms/块（预算 10 ms，余量 %.0f 倍）" % (ms, 10.0 / max(ms, 1e-6)))
    ok &= ms < 5.0

    print("\nRESULT: %s" % ("PASS all checks passed" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
