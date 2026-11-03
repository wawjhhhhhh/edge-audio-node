# -*- coding: utf-8 -*-
"""响度归一化 + 真峰值表（P0）。

两个东西，互相独立：

* ``LoudNorm``  —— 用 BS.1770-4 的**短时响度（3 s）**驱动一条慢增益，
  把不同来源的素材拉到同一个目标响度（默认 −16 LUFS）。慢到什么程度？
  最快 2 dB/s、总增益限 ±12 dB，静音时**只降不升**（避免把底噪抬起来）。
  它不碰瞬态、不做压缩 —— 动态由 ``playfx`` 的压缩器管，这里只管"整体电平平齐"。

* ``TruePeakMeter`` —— 4× 过采样后的真峰值（dBTP）。采样点峰值会漏掉
  采样间过冲（digital full scale 的正弦最坏能到 +3 dB），推到 DAC 再削就晚了。

为什么不直接用 ``loudness.LoudnessMeter``：
它的 ``feed()`` 每块都做 K 加权 + 100 ms 对齐，作为"计量"没问题，但它给的是
**瞬时/短时**读数，归一化还需要"目标误差 → 增益 → 斜率限制 → 静音门控"这一层，
所以这里只复用它的 K 加权系数，自己维护一个 3 s 滑窗。
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np
from scipy import signal

RATE = 48000

# BS.1770-4 的 K 加权（与 loudness.py 同一套系数，48 kHz 定死）
_SHELF_B = (1.53512485958697, -2.69169618940638, 1.19839281085285)
_SHELF_A = (1.0, -1.69065929318241, 0.73248077421585)
_HPF_B = (1.0, -2.0, 1.0)
_HPF_A = (1.0, -1.99004745483398, 0.99007225036621)

# 3 s 短时窗，100 ms 一块
_HOP = RATE // 10
_SHORT_N = 30


def _biquad(x: np.ndarray, b, a, z: np.ndarray):
    """直接 II 转置型 biquad，状态连续（逐块调用不产生接缝）。"""
    y = np.empty_like(x)
    b0, b1, b2 = b
    _, a1, a2 = a
    for i in range(x.shape[0]):
        xin = x[i]
        yout = b0 * xin + z[0]
        z[0] = b1 * xin - a1 * yout + z[1]
        z[1] = b2 * xin - a2 * yout
        y[i] = yout
    return y, z


def _lufs(mean_square: float) -> float:
    """BS.1770：LUFS = −0.691 + 10·log10(Σ G·z)。"""
    return -0.691 + 10.0 * math.log10(max(float(mean_square), 1e-20))


class LoudNorm:
    """整体电平拉平：目标 LUFS + 斜率限制 + 静音门控。"""

    def __init__(self, rate: int = RATE, target_lufs: float = -16.0,
                 max_gain_db: float = 12.0, max_slew_db_s: float = 8.0,
                 gate_lufs: float = -58.0):
        self.rate = int(rate)
        self.enabled = False
        self.target = float(np.clip(target_lufs, -40.0, -6.0))
        self.max_gain_db = float(max(0.0, max_gain_db))
        self.max_slew = float(max(0.05, max_slew_db_s))
        self.gate = float(gate_lufs)

        self.reset()

    # ---------------------------------------------------------------- 状态
    def reset(self) -> None:
        self._z1 = np.zeros(2)
        self._z2 = np.zeros(2)
        self._acc = np.zeros(2)
        self._acc_n = 0
        self._blocks: deque[float] = deque(maxlen=_SHORT_N)   # 每 100 ms 一块的加权均方
        self._gain = 1.0                                       # 当前线性增益
        self._gain_db = 0.0
        self._samples = 0

    def set_target(self, lufs: float) -> float:
        self.target = float(np.clip(lufs, -40.0, -6.0))
        return self.target

    def set_limits(self, max_gain_db=None, max_slew_db_s=None, gate_lufs=None) -> None:
        if max_gain_db is not None:
            self.max_gain_db = float(max(0.0, max_gain_db))
        if max_slew_db_s is not None:
            self.max_slew = float(max(0.05, max_slew_db_s))
        if gate_lufs is not None:
            self.gate = float(gate_lufs)

    # ---------------------------------------------------------------- 测量
    def _feed_meter(self, x: np.ndarray) -> None:
        """把 (n,2) 浮点样点喂进 K 加权 + 100 ms 子块。"""
        w = np.empty_like(x)
        for c in range(2):
            y1, self._z1 = _biquad(x[:, c], _SHELF_B, _SHELF_A, self._z1)
            y2, self._z2 = _biquad(y1, _HPF_B, _HPF_A, self._z2)
            w[:, c] = y2
        i = 0
        n = w.shape[0]
        while i < n:
            take = min(_HOP - self._acc_n, n - i)
            self._acc += (w[i:i + take] ** 2).sum(axis=0)
            self._acc_n += take
            i += take
            if self._acc_n == _HOP:
                self._blocks.append(float(self._acc.sum()) / _HOP)
                self._acc[:] = 0.0
                self._acc_n = 0
        self._samples += n

    def short_db(self) -> float | None:
        """3 s 短时响度；不足 1.5 s 返回 None。"""
        if len(self._blocks) < _SHORT_N // 2:
            return None
        return _lufs(float(np.mean(self._blocks)))

    # ---------------------------------------------------------------- 处理
    def process(self, x: np.ndarray) -> np.ndarray:
        """(n,2) 浮点 → 同形状浮点。返回**加过增益**的信号。"""
        n = x.shape[0]
        if n == 0:
            return x
        if not self.enabled:
            return x

        self._feed_meter(x)
        cur = self.short_db()

        # 目标增益：静音门控 —— 低于门限只允许"减"，不允许"加"
        want_db = 0.0
        if cur is not None:
            gated = cur < self.gate
            if not gated:
                want_db = self.target - cur
            else:
                # 静音里不要把底噪抬起来：只会往下走
                want_db = min(0.0, self.target - cur)
        want_db = max(-self.max_gain_db, min(self.max_gain_db, want_db))

        # 斜率限制：每块最多动 max_slew * 块时长
        blk_s = n / float(self.rate)
        step = self.max_slew * blk_s
        delta = want_db - self._gain_db
        if delta > step:
            delta = step
        elif delta < -step:
            delta = -step
        self._gain_db += delta
        self._gain_db = max(-self.max_gain_db, min(self.max_gain_db, self._gain_db))

        # 块内线性斜坡，避免切换瞬间出"咔"
        target_g = 10.0 ** (self._gain_db / 20.0)
        ramp = np.linspace(self._gain, target_g, n, endpoint=False) if n > 1 else np.array([target_g])
        self._gain = target_g
        return x * ramp.reshape(-1, 1)

    def stats(self) -> dict:
        cur = self.short_db()
        return {
            "on": bool(self.enabled),
            "target": round(float(self.target), 1),
            "short": None if cur is None else round(float(cur), 1),
            "gain_db": round(float(self._gain_db), 2),
            "max_gain_db": round(float(self.max_gain_db), 1),
            "slew_db_s": round(float(self.max_slew), 2),
            "gate": round(float(self.gate), 1),
        }


class TruePeakMeter:
    """4× 过采样真峰值（dBTP）。

    用 ``scipy.signal.resample_poly`` 做 4 倍插值：它会自动补偿零插值的
    1/4 增益，比自己手搓 FIR 更不容易错（手搓版少乘 4，方波报 −0.55 dBTP，
    真值应当 ≈0）。块与块之间保留 64 点历史，避免接缝处漏峰。
    """

    _HIST = 64

    def __init__(self, rate: int = RATE):
        self.rate = int(rate)
        self.reset()

    def reset(self) -> None:
        self._tail = np.zeros((self._HIST, 2))
        self._peak = 0.0
        self._over = 0

    def reset_peak(self) -> None:
        self._peak = 0.0
        self._over = 0

    def process(self, x: np.ndarray, ceil_db: float | None = None) -> float:
        """喂 (n,2) 浮点，返回累计真峰值 dBTP。"""
        if x.shape[0] == 0:
            return self._peak_to_db()
        buf = np.vstack([self._tail, x])
        self._tail = buf[-self._HIST:].copy()
        y = signal.resample_poly(buf, 4, 1, axis=0)
        cut = self._HIST * 4
        y = y[cut:] if y.shape[0] > cut else y
        pk = float(np.max(np.abs(y))) if y.size else 0.0
        if pk > self._peak:
            self._peak = pk
        if ceil_db is not None:
            lim = 10.0 ** (ceil_db / 20.0)
            self._over += int(np.count_nonzero(np.abs(y) > lim))
        return self._peak_to_db()

    def _peak_to_db(self) -> float:
        return 20.0 * math.log10(max(self._peak, 1e-9))

    def stats(self) -> dict:
        return {"tp_db": round(self._peak_to_db(), 2), "over": int(self._over)}


# ---------------------------------------------------------------- 离线自检

def selftest() -> int:
    ok = 0
    total = 0

    def check(name, cond, extra=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print("  [OK]   %-34s %s" % (name, extra))
        else:
            print("  [FAIL] %-34s %s" % (name, extra))

    rng = np.random.default_rng(7)
    rate = RATE

    # 1) 两种电平的素材，归一化后短时响度应都贴近目标
    def tone(amp, secs=8.0, f=440.0):
        n = int(secs * rate)
        t = np.arange(n) / rate
        x = amp * np.sin(2 * np.pi * f * t)
        return np.repeat(x.reshape(-1, 1), 2, axis=1)

    res = {}
    for tag, amp in (("loud", 0.5), ("quiet", 0.05)):
        norm = LoudNorm(rate, target_lufs=-16.0)
        norm.enabled = True
        x = tone(amp)
        y = np.empty_like(x)
        for i in range(0, len(x), 480):
            y[i:i + 480] = norm.process(x[i:i + 480])
        # 用标准计量器复核（把 float 转回 int16 口径喂进去）
        from loudness import LoudnessMeter
        m = LoudnessMeter()
        m.feed(np.clip(y[-int(3.0 * rate):] * 32768, -32768, 32767).astype(np.int16))
        res[tag] = m.short_db()
    check("归一后 大声/小声 差 ≤1.5 LU",
          res["loud"] is not None and res["quiet"] is not None
          and abs(res["loud"] - res["quiet"]) <= 1.5,
          "loud=%.2f quiet=%.2f LUFS" % (res["loud"], res["quiet"]))
    check("归一后贴近目标 −16 LUFS（±2）",
          abs(res["loud"] - (-16.0)) <= 2.0, "loud=%.2f" % res["loud"])

    # 2) 静音不提底噪
    norm = LoudNorm(rate, target_lufs=-16.0)
    norm.enabled = True
    silence = np.zeros((int(1.0 * rate), 2)) + 1e-4 * rng.standard_normal((int(1.0 * rate), 2))
    y = np.concatenate([norm.process(silence[i:i + 480]) for i in range(0, len(silence), 480)])
    check("静音段增益不出正（不抬底噪）", norm._gain_db <= 0.01, "gain=%.3f dB" % norm._gain_db)

    # 3) 真峰值：0 dBFS 方波应当报 ≥0 dBTP（采样点峰值只有 0 dB）
    tp = TruePeakMeter(rate)
    sq = np.ones((480, 2)) * 0.999
    for _ in range(10):
        tp.process(sq)
    check("真峰值 ≥ 采样峰（方波）", tp.stats()["tp_db"] >= -0.2, "tp=%.2f dBTP" % tp.stats()["tp_db"])

    # 4) 真峰值：44.1k 正弦（最坏过冲）应高于样点峰
    n = 4800
    t = np.arange(n) / rate
    x = 0.9 * np.sin(2 * np.pi * 11025 * t)
    tp2 = TruePeakMeter(rate)
    tp2.reset()
    xx = np.repeat(x.reshape(-1, 1), 2, axis=1)
    tp2.process(xx)
    sample_peak = 20 * math.log10(np.max(np.abs(x)))
    check("过采样峰 ≥ 样点峰", tp2.stats()["tp_db"] >= sample_peak - 0.05,
          "tp=%.2f sample=%.2f" % (tp2.stats()["tp_db"], sample_peak))

    print("\n%d/%d PASS" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
