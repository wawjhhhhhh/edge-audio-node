# -*- coding: utf-8 -*-
"""双 DAC 校准（P1）：把 ES8388 与外接 ES9038Q2M 的频响差抹平。

为什么需要它
------------
M0（同源）时两个耳机收到的是**同一串数字**，但听到的不一样 —— 因为两个
DAC 芯片 + 各自模拟通路（滤波、耦合电容、负载）的频响不同。要比"数字链路
干不干净"，得先把这个硬件差抹平，否则听到的是"DAC 差"而不是"链路差"。

能不能抹平
----------
* **M1（左右分离）**：能。左声道只去板载、右声道只去外接，所以可以给
  两个声道各挂一条独立的修正 EQ，这正是本模块做的事。
* **M0（同源）**：不能。同一串数字进两个 DAC，任何数字修正都会让两路
  一起变，无法分别补偿。M0 下本模块只能给"平均曲线"（默认关闭）。

怎么得到曲线
------------
两条路，任选：

1. **实测**（推荐）：对每个 DAC 放同一段扫频/粉噪，分别录下来，
   用 ``curve_from_wavs(board_wav, ext_wav)`` 算两条修正曲线。
   要求两次录音用**同一个麦克风、同一个位置、同一个音量**。
2. **手填**：``DacCal.set_curves(board_db, ext_db)`` 直接给 10 段 dB。

修正量按"把两者拉到均值"来分（各修一半，总修正量最小）：
``corr_board = -(h_board - h_mean)``、``corr_ext = -(h_ext - h_mean)``。
"""
from __future__ import annotations

import json
import math

import numpy as np
from scipy import signal

RATE = 48000
# 与 playfx 的 EQ 段一致，方便复用同一套 UI
BANDS = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
Q = 1.1
MAX_CORR_DB = 12.0


def _peaking_sos(band_hz: float, gain_db: float, q: float, fs: int) -> np.ndarray:
    if abs(gain_db) < 1e-6:
        return np.array([[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]])
    a_gain = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * band_hz / fs
    alpha = np.sin(w0) / (2.0 * q)
    return np.array([[
        (1.0 + alpha * a_gain) / (1.0 + alpha / a_gain),
        (-2.0 * np.cos(w0)) / (1.0 + alpha / a_gain),
        (1.0 - alpha * a_gain) / (1.0 + alpha / a_gain),
        1.0,
        (-2.0 * np.cos(w0)) / (1.0 + alpha / a_gain),
        (1.0 - alpha / a_gain) / (1.0 + alpha / a_gain),
    ]])


def _band_levels(x: np.ndarray, rate: int = RATE) -> np.ndarray:
    """把单声道信号按 BANDS 切 10 段，返回每段的平均幅度（dB）。"""
    n = len(x)
    if n < 512:
        raise ValueError("录音太短")
    # 1 s 窗、50% 重叠，取平均功率谱
    win = int(min(n, 1.0 * rate))
    hop = win // 2
    acc = None
    cnt = 0
    w = np.hanning(win)
    for i in range(0, max(1, n - win + 1), hop):
        seg = x[i:i + win]
        if len(seg) < win:
            break
        mag = np.abs(np.fft.rfft(seg * w)) ** 2
        acc = mag if acc is None else acc + mag
        cnt += 1
    if acc is None or cnt == 0:
        raise ValueError("录音太短")
    psd = acc / cnt
    freqs = np.fft.rfftfreq(win, 1.0 / rate)

    out = np.zeros(len(BANDS))
    edges = list(BANDS)
    for k, fc in enumerate(edges):
        lo = fc / (2 ** 0.5)
        hi = fc * (2 ** 0.5)
        m = (freqs >= lo) & (freqs < hi)
        if not m.any():
            m = np.argmin(np.abs(freqs - fc)) + np.zeros(1, dtype=bool)
        out[k] = 10.0 * math.log10(max(float(psd[m].mean()), 1e-20))
    return out


def load_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """读 WAV → (单声道浮点 −1..1, 采样率)。"""
    import wave
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("只支持 16 bit WAV，实际 %d bit" % (sw * 8))
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def curve_from_wavs(board_wav: str, ext_wav: str) -> tuple[np.ndarray, np.ndarray]:
    """两段"同一扫频/粉噪"录音 → (板载修正 dB[10], 外接修正 dB[10])。

    修正按"拉到两者均值"分配：每边只需修一半，总修正量最小。
    """
    b, rb = load_wav_mono(board_wav)
    e, re_ = load_wav_mono(ext_wav)
    if rb != re_:
        raise ValueError("两段录音采样率不同：%d vs %d" % (rb, re_))
    hb = _band_levels(b, rb)
    he = _band_levels(e, re_)
    # 先各自对齐总电平，只留"形状差"
    hb -= np.mean(hb)
    he -= np.mean(he)
    diff = hb - he
    corr_b = np.clip(-diff / 2.0, -MAX_CORR_DB, MAX_CORR_DB)
    corr_e = np.clip(+diff / 2.0, -MAX_CORR_DB, MAX_CORR_DB)
    return corr_b, corr_e


class DacCal:
    """两条独立的 10 段修正 EQ（左=板载、右=外接）。"""

    def __init__(self, rate: int = RATE):
        self.rate = int(rate)
        self.enabled = False
        self.reset()

    def reset(self) -> None:
        self._b_gains = np.zeros(len(BANDS))
        self._e_gains = np.zeros(len(BANDS))
        self._b_sos = np.vstack([_peaking_sos(f, 0.0, Q, self.rate) for f in BANDS])
        self._e_sos = self._b_sos.copy()
        self._b_zi = np.zeros((self._b_sos.shape[0], 2, 1))
        self._e_zi = np.zeros_like(self._b_zi)

    def set_curves(self, board_db, ext_db) -> None:
        b = np.clip(np.asarray(board_db, dtype=np.float64), -MAX_CORR_DB, MAX_CORR_DB)
        e = np.clip(np.asarray(ext_db, dtype=np.float64), -MAX_CORR_DB, MAX_CORR_DB)
        if b.shape != (len(BANDS),) or e.shape != (len(BANDS),):
            raise ValueError("需要 %d 段" % len(BANDS))
        self._b_gains, self._e_gains = b, e
        self._b_sos = np.vstack([_peaking_sos(f, g, Q, self.rate) for f, g in zip(BANDS, b)])
        self._e_sos = np.vstack([_peaking_sos(f, g, Q, self.rate) for f, g in zip(BANDS, e)])
        self._b_zi = np.zeros((self._b_sos.shape[0], 2, 1))
        self._e_zi = np.zeros_like(self._b_zi)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not self.enabled or x.shape[0] == 0:
            return x
        y = x.copy()
        y[:, 0:1], self._b_zi = signal.sosfilt(self._b_sos, x[:, 0:1], axis=0, zi=self._b_zi)
        y[:, 1:2], self._e_zi = signal.sosfilt(self._e_sos, x[:, 1:2], axis=0, zi=self._e_zi)
        return y

    def stats(self) -> dict:
        return {
            "on": bool(self.enabled),
            "board_db": [round(float(v), 1) for v in self._b_gains],
            "ext_db": [round(float(v), 1) for v in self._e_gains],
        }

    # ------------------------------------------------------------ 存取
    @staticmethod
    def save(path: str, board_db, ext_db) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"bands": list(BANDS), "board_db": list(map(float, board_db)),
                       "ext_db": list(map(float, ext_db))}, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> tuple[list, list]:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d["board_db"], d["ext_db"]


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="双 DAC 校准：从两段扫频录音算修正曲线")
    ap.add_argument("--board", help="板载 ES8388 的录音 WAV")
    ap.add_argument("--ext", help="外接 ES9038Q2M 的录音 WAV")
    ap.add_argument("--out", default="dac_cal.json", help="输出 JSON")
    a = ap.parse_args(argv)
    if not a.board or not a.ext:
        ap.error("需要 --board 与 --ext 两条录音")
    cb, ce = curve_from_wavs(a.board, a.ext)
    print("板载修正 dB: " + " ".join("%+5.1f" % v for v in cb))
    print("外接修正 dB: " + " ".join("%+5.1f" % v for v in ce))
    DacCal.save(a.out, cb, ce)
    print("已写 %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
