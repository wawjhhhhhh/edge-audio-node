"""响度（ITU-R BS.1770-4）—— 给控制台的"客观指标卡"和"等响 A/B"用。

为什么要它：
    调音效/降噪的时候，"听着更响了"和"真的更响了"经常不是一回事。人耳对 2–5 kHz
    最敏感，一段噪声被压掉 20 dB 低频能量，主观上可能只是"干净了一点"；反过来把
    某个中频抬高 3 dB，主观上会明显"变响"，但总 RMS 几乎不动。所以现场对比必须
    有一个和听感相关的数字 —— 广播行业的标准就是 LUFS。

实现要点（BS.1770-4 原文）：
    * K 加权 = 两级 IIR：先一个高架（+4 dB @ >2 kHz），再一个高通（去掉 20 Hz 以下），
      两级都用 48 kHz 的定系数（本项目的采样率固定 48 kHz）。
    * 400 ms 为一个"块"、75 % 重叠（即每 100 ms 一个块），块响度：
          L = -0.691 + 10*log10( sum_i G_i * z_i )
      其中 z_i 是该块内第 i 个声道的加权均方，立体声 G = 1.0 / 1.0
      （所以左右同时送同一个信号会比单声道高约 3 dB，这是标准如此，不是 bug）。
    * 整体响度（integrated）要做门限：先按 -70 LUFS 绝对门限，再用"未门限响度 - 10 LU"
      做相对门限，两次都要重算。
    * 峰值用 sample peak（dBFS）。真峰值（true peak）需要 4 倍升采样，这里不做 ——
      板子的链路是 48 kHz 同步时钟，sample peak 已经能说明削波风险。

用：
    m = LoudnessMeter()
    m.feed(blk)            # blk 形状 (n, 2) int16，10 ms 一块随便喂
    m.momentary_db()       # 最近 400 ms，LUFS
    m.short_db()           # 最近 3 s
    m.integrated_db()      # 会话累计（带门限）
    m.peak_db()            # 峰值
    m.reset()

自检：python loudness.py
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

try:                                     # scipy 装上之后走 C 实现，快两个数量级
    from scipy import signal as _signal
except Exception:                        # 没装就退回纯 Python 循环（结果一样）
    _signal = None

RATE = 48000
BLOCK_MS = 400
HOP_MS = 100
BLOCK = RATE * BLOCK_MS // 1000          # 19200
HOP = RATE * HOP_MS // 1000              # 4800
SHORT_BLOCKS = 30                        # 3 s

# 48 kHz 的 K 加权系数（BS.1770-4 表 1 / 表 2）
_SHELF_B = (1.53512485958697, -2.69169618940638, 1.19839281085285)
_SHELF_A = (1.0, -1.69065929318241, 0.73248077421585)
_HPF_B = (1.0, -2.0, 1.0)
_HPF_A = (1.0, -1.99004745483398, 0.99007225036621)


def _biquad(x: np.ndarray, b, a, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """单声道 biquad；z 是两状态，跨调用连续。

    有 scipy 时走 `lfilter`（C 实现）；没装时退回直接 II 型转置的 Python 循环。
    两条路结果一致（自检第 1 项用已知正弦校过）。

    历史：这段本来是纯 Python 循环，因为最早这个项目刻意不依赖 scipy。后来
    装 scipy 是为了播放音效（`playfx.py` 要 10 段 EQ），顺手把这里也换掉 ——
    离线跑 8 秒素材做等响 A/B 时，循环版本会把 bench 拖到几分钟。
    """
    if _signal is not None:
        y, zo = _signal.lfilter(b, a, x, zi=np.asarray(z, dtype=np.float64))
        return y, zo
    b0, b1, b2 = b
    _, a1, a2 = a
    y = np.empty_like(x)
    z1, z2 = float(z[0]), float(z[1])
    for i in range(x.size):
        xi = x[i]
        yi = b0 * xi + z1
        z1 = b1 * xi - a1 * yi + z2
        z2 = b2 * xi - a2 * yi
        y[i] = yi
    return y, np.array([z1, z2], dtype=np.float64)


class LoudnessMeter:
    """增量式响度计：随便喂多长的块，内部自己按 100 ms 对齐。"""

    def __init__(self, channels: int = 2):
        self.channels = channels
        self.reset()

    # ---------------------------------------------------------------- 状态
    def reset(self) -> None:
        self._z1 = [np.zeros(2) for _ in range(self.channels)]
        self._z2 = [np.zeros(2) for _ in range(self.channels)]
        self._acc = np.zeros(self.channels, dtype=np.float64)   # 当前 100 ms 子块的平方和
        self._acc_n = 0
        self._sub: deque[float] = deque(maxlen=BLOCK // HOP)    # 最近 4 个子块 → 400 ms
        self._blocks: deque[float] = deque(maxlen=SHORT_BLOCKS)  # 最近 30 个块 → 3 s
        self._loud_all: list[float] = []                         # 门限用：所有 400 ms 块
        self._peak = 0.0
        self._samples = 0

    # ---------------------------------------------------------------- 喂数
    def feed(self, blk: np.ndarray) -> None:
        """blk: (n, 2) int16，任意长度。"""
        if blk is None or len(blk) == 0:
            return
        x = np.asarray(blk, dtype=np.float64) / 32768.0
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] < self.channels:
            x = np.repeat(x[:, :1], self.channels, axis=1)

        self._peak = max(self._peak, float(np.abs(x).max()))
        self._samples += x.shape[0]

        # K 加权（逐声道、状态连续）
        w = np.empty_like(x)
        for c in range(self.channels):
            y1, self._z1[c] = _biquad(x[:, c], _SHELF_B, _SHELF_A, self._z1[c])
            y2, self._z2[c] = _biquad(y1, _HPF_B, _HPF_A, self._z2[c])
            w[:, c] = y2

        # 按 100 ms 子块累加加权均方
        i = 0
        n = w.shape[0]
        while i < n:
            take = min(HOP - self._acc_n, n - i)
            self._acc += (w[i:i + take] ** 2).sum(axis=0)
            self._acc_n += take
            i += take
            if self._acc_n == HOP:
                z = float(self._acc.sum()) / HOP            # 两声道相加（G=1）
                self._sub.append(z)
                self._acc[:] = 0.0
                self._acc_n = 0
                if len(self._sub) >= BLOCK // HOP:
                    block_z = sum(self._sub) / (BLOCK // HOP)
                    self._blocks.append(block_z)
                    self._loud_all.append(_lufs(block_z))
                    if len(self._loud_all) > 4000:          # 约 6.6 分钟，够用
                        del self._loud_all[:1000]

    # ---------------------------------------------------------------- 读数
    def momentary_db(self) -> float | None:
        if len(self._sub) < BLOCK // HOP:
            return None
        return _lufs(sum(self._sub) / (BLOCK // HOP))

    def short_db(self) -> float | None:
        if len(self._blocks) < SHORT_BLOCKS // 2:
            return None
        return _lufs(sum(self._blocks) / len(self._blocks))

    def integrated_db(self) -> float | None:
        """带 BS.1770 双重门限的整体响度。"""
        if not self._loud_all:
            return None
        vals = np.asarray(self._loud_all, dtype=np.float64)
        abs_gated = vals[vals > -70.0]
        if abs_gated.size == 0:
            return None
        # 相对门限是"未门限响度 - 10 LU"，两个量都必须在 LUFS 域里算。
        # （早些时候写成 mean(lin()) - 10 把线性域的值当 LUFS 用，门限落到 -10 LUFS
        #  附近，把所有块都筛掉了 —— 于是整体响度退化成不门限的算术结果。）
        rel = _lufs_from_lufs(abs_gated) - 10.0
        kept = abs_gated[abs_gated > rel]
        if kept.size == 0:
            kept = abs_gated
        return _lufs_from_lufs(kept)

    def peak_db(self) -> float:
        return 20.0 * math.log10(self._peak) if self._peak > 0 else -140.0

    def stats(self) -> dict:
        return {
            "m": _round(self.momentary_db()),
            "s": _round(self.short_db()),
            "i": _round(self.integrated_db()),
            "peak": round(self.peak_db(), 1),
            "sec": round(self._samples / RATE, 2),
        }


def _lufs(mean_square: float) -> float:
    if mean_square <= 1e-12:
        return -140.0
    return -0.691 + 10.0 * math.log10(mean_square)


def _lin(lufs_values: np.ndarray) -> np.ndarray:
    return 10.0 ** ((lufs_values + 0.691) / 10.0)


def _lufs_from_lufs(values: np.ndarray) -> float:
    return -0.691 + 10.0 * math.log10(float(np.mean(_lin(values))))


def _round(v):
    return None if v is None else round(float(v), 1)


# ------------------------------------------------------------------ 自检
def selftest() -> int:
    ok = True

    def check(name: str, got, want, tol, unit=""):
        nonlocal ok
        if want is None:
            good = got is None
        else:
            good = got is not None and abs(got - want) <= tol
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'}  {name}: {got} {unit} (expect {want} +-{tol})")

    # 1 kHz 正弦，幅度 -20 dBFS、双声道：总 RMS = -20.01 dBFS，而 K 加权在 1 kHz
    # 正好是 +0.69 dB（标准里那个 -0.691 常数就是补偿它的），所以 LUFS ≈ -20.0。
    t = np.arange(RATE * 2) / RATE
    a = 10 ** (-20 / 20.0)
    sine = (a * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    blk2 = np.stack([sine, sine], axis=1)
    m = LoudnessMeter()
    for i in range(0, len(blk2), 480):
        m.feed(blk2[i:i + 480])
    check("1 kHz @-20 dBFS 立体声 整体响度", m.integrated_db(), -20.0, 0.3, "LUFS")
    check("1 kHz @-20 dBFS 立体声 瞬时响度", m.momentary_db(), -20.0, 0.3, "LUFS")
    check("峰值", m.peak_db(), -20.0, 0.2, "dBFS")

    # 60 dB 动态：安静段 -60 dBFS + 响段 -20 dBFS，门限应主要看响段
    quiet = (10 ** (-60 / 20.0) * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    m2 = LoudnessMeter()
    for seg in (quiet, sine):
        b = np.stack([seg, seg], axis=1)
        for i in range(0, len(b), 480):
            m2.feed(b[i:i + 480])
    check("双段信号 整体响度贴近响段", m2.integrated_db(), -20.7, 1.0, "LUFS")

    # 静音
    m3 = LoudnessMeter()
    m3.feed(np.zeros((4800, 2), dtype=np.int16))
    check("静音 整体响度", m3.integrated_db(), None, 0, "")

    # 分块喂 vs 一次喂，结果一致
    m4 = LoudnessMeter()
    m4.feed(blk2)
    check("整块 vs 分块 一致性", m4.integrated_db(), m.integrated_db(), 0.05, "LUFS")

    print("RESULT:", "PASS" if ok else "FAIL", "all checks passed" if ok else "some checks failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
