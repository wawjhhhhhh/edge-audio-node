"""板载频谱仪 / 声学测量仪（B 方案，PC 端实现）。

数据从哪来
----------
控制台的 `Hub._pull_loop()` 每 10 ms 收到一块 `(480, 2) int16`（板子 ES8388 采的
48 kHz 立体声，UDP 50505）。这里在"声码器之后、写任何下游之前"旁接一份，
所以看到的就是控制台正在播的那一路（开了降噪就看降噪后的谱）。

算什么
------
1. **谱**：4096 点 Hann 窗 FFT（85 ms，分辨率 11.7 Hz），折成 72 条对数频带
   （20 Hz–20 kHz，每倍频程约 6 条），用频带内最大值，给人看更接近"柱状图"直觉。
2. **瀑布**：最近 96 帧的频带谱（每 20 ms 一帧，约 2 s 历史）。
3. **读数**：
   - `rms_dbfs`：整段 RMS（满幅 32768 记 0 dBFS）
   - `peak_hz` / `peak_db`：最强频点
   - `floor_db`：谱的中位数（噪声底）
   - `snr_db`：`peak_db - floor_db`（粗估，够用）
   - `thd_pct`：基频确定时，2..5 次谐波能量占比（纯音测试用）
   - `clip`：接近满幅的样点数（削波告警）
4. **常见测量**：`tone_hz`（自动找 200 Hz–8 kHz 内的最强单音）、
   `band_energy_low/mid/high`（低频 <300、中频 300–3k、高频 >3k 的能量占比）。

为什么不做 A 计权
-----------------
A 计权要按 IEC 61672 的曲线逐频点加权，属于"声级计"范畴；这里用 dBFS 表达
相对电平，配合频带谱已经够判断"降噪前后差多少 dB"。真要声压级还得校准麦克风。
"""

from __future__ import annotations

from collections import deque

import numpy as np

RATE = 48000
NFFT = 4096                     # 85 ms 窗，11.7 Hz 分辨率
BANDS = 72                      # 对数频带条数
F_LO, F_HI = 20.0, 20000.0
WATERFALL_ROWS = 96             # 约 2 s 历史（20 ms/行）
FULL_SCALE = 32768.0
DB_FLOOR = -140.0


def _log_edges(n: int = BANDS, lo: float = F_LO, hi: float = F_HI) -> np.ndarray:
    return np.geomspace(lo, hi, n + 1)


class Analyzer:
    """把 10 ms 音频块喂进来，随时取一份快照。"""

    def __init__(self, nfft: int = NFFT, bands: int = BANDS,
                 waterfall: int = WATERFALL_ROWS) -> None:
        self.nfft = int(nfft)
        self.bands = int(bands)
        self._win = np.hanning(self.nfft).astype(np.float32)
        # rfft 的频率坐标（只用来查峰和找谐波）
        self._freqs = np.fft.rfftfreq(self.nfft, 1.0 / RATE)
        self._edges = _log_edges(self.bands)
        self._band_idx = self._edge_to_bins(self._edges)
        self._buf = np.zeros(self.nfft, dtype=np.float32)
        self._fill = 0
        self._rows: deque[np.ndarray] = deque(maxlen=int(waterfall))
        self._since_fft = 0
        self._every = 2                 # 每 2 块（20 ms）算一次
        self.frames = 0
        self.ffts = 0
        self._snap: dict = self._empty()

    # ---- 内部 -----------------------------------------------------------
    def _edge_to_bins(self, edges: np.ndarray) -> list[tuple[int, int]]:
        """每个对数频带对应 rfft 的 [i0, i1) 区间（至少 1 个 bin）。"""
        idx = np.searchsorted(self._freqs, edges)
        out = []
        for a, b in zip(idx[:-1], idx[1:]):
            a = int(a)
            b = max(int(b), a + 1)
            b = min(b, self._freqs.size)
            if a >= b:
                a = max(0, b - 1)
            out.append((a, b))
        return out

    @staticmethod
    def _empty() -> dict:
        return {"ready": False, "bins": [], "freqs": [], "waterfall": [],
                "peak_hz": 0.0, "peak_db": DB_FLOOR, "rms_dbfs": DB_FLOOR,
                "floor_db": DB_FLOOR, "snr_db": 0.0, "thd_pct": 0.0,
                "clip": 0, "tone_hz": 0.0,
                "band_low": 0.0, "band_mid": 0.0, "band_high": 0.0,
                "frames": 0}

    def _spectrum_db(self) -> np.ndarray:
        seg = self._buf * self._win
        spec = np.fft.rfft(seg)
        # 单边幅度归一：正弦 x[n]=A·sin 时 |X[k0]| = (A/2)·Σw，故 A = 2|X|/Σw。
        # 再除以满幅 32768，得到"相对满幅的幅度"，dB 就是 dBFS。
        mag = 2.0 * np.abs(spec) / (float(self._win.sum()) * FULL_SCALE)
        return 20.0 * np.log10(np.maximum(mag, 10.0 ** (DB_FLOOR / 20.0)))

    def _band_db(self, db: np.ndarray) -> np.ndarray:
        out = np.empty(self.bands, dtype=np.float32)
        for i, (a, b) in enumerate(self._band_idx):
            out[i] = float(np.max(db[a:b])) if b > a else DB_FLOOR
        return out

    def _metrics(self, db: np.ndarray, band_db: np.ndarray) -> dict:
        # 只看 20 Hz–20 kHz
        lo = int(np.searchsorted(self._freqs, F_LO))
        hi = int(np.searchsorted(self._freqs, F_HI))
        hi = max(hi, lo + 1)
        seg = db[lo:hi]
        k = int(np.argmax(seg))
        peak_bin = lo + k
        peak_db = float(seg[k])
        peak_hz = float(self._freqs[peak_bin])

        floor_db = float(np.median(seg))
        snr_db = min(99.9, max(0.0, peak_db - floor_db))

        thd = 0.0
        tone_hz = 0.0
        # 门限 25 dB：白噪声的谱峰只比中位数高 12 dB 左右，不能当单音算 THD。
        if snr_db >= 25.0 and 200.0 <= peak_hz <= 8000.0:
            tone_hz = peak_hz
            p0 = float(np.sum(10.0 ** (db[peak_bin - 2:peak_bin + 3] / 10.0)))
            harm = 0.0
            for n in (2, 3, 4, 5):
                fc = peak_hz * n
                if fc >= F_HI:
                    break
                c = int(round(fc * self.nfft / RATE))
                if c + 2 >= db.size:
                    break
                harm += float(np.sum(10.0 ** (db[c - 2:c + 3] / 10.0)))
            if p0 > 0:
                thd = 100.0 * np.sqrt(harm / p0)

        # 三段的能量占比（按频带线性功率求和）
        def band_share(f_lo: float, f_hi: float) -> float:
            a = int(np.searchsorted(self._freqs, f_lo))
            b = int(np.searchsorted(self._freqs, f_hi))
            b = max(b, a + 1)
            return float(np.sum(10.0 ** (db[a:b] / 10.0)))

        e_lo = band_share(F_LO, 300.0)
        e_mid = band_share(300.0, 3000.0)
        e_hi = band_share(3000.0, F_HI)
        tot = e_lo + e_mid + e_hi
        if tot <= 0:
            tot = 1.0

        rms = float(np.sqrt(np.mean(self._buf.astype(np.float64) ** 2)))
        rms_dbfs = 20.0 * np.log10(max(rms, 1e-6) / FULL_SCALE)

        return {"peak_hz": round(peak_hz, 1), "peak_db": round(peak_db, 1),
                "rms_dbfs": round(float(max(rms_dbfs, DB_FLOOR)), 1),
                "floor_db": round(floor_db, 1), "snr_db": round(snr_db, 1),
                "thd_pct": round(float(thd), 2), "tone_hz": round(tone_hz, 1),
                "band_low": round(100.0 * e_lo / tot, 1),
                "band_mid": round(100.0 * e_mid / tot, 1),
                "band_high": round(100.0 * e_hi / tot, 1)}

    # ---- 对外 -----------------------------------------------------------
    def feed(self, blk) -> None:
        """喂一块 `(n, 2) int16`（或 `(n,)`）。多余的样点丢弃最老的。"""
        arr = np.asarray(blk)
        if arr.ndim == 2:
            mono = arr.astype(np.float32).mean(axis=1)
        else:
            mono = arr.astype(np.float32)
        n = mono.size
        if n <= 0:
            return
        self.frames += 1
        if n >= self.nfft:
            self._buf = mono[-self.nfft:].copy()
            self._fill = self.nfft
        else:
            self._buf = np.concatenate([self._buf[n:], mono])
            self._fill = min(self.nfft, self._fill + n)

        self._since_fft += 1
        if self._fill < self.nfft or self._since_fft < self._every:
            return
        self._since_fft = 0
        self._compute()

    def _compute(self) -> None:
        db = self._spectrum_db()
        band_db = self._band_db(db)
        self._rows.append(band_db)
        self.ffts += 1
        clip = int(np.sum(np.abs(self._buf) > FULL_SCALE * 0.98))
        snap = self._empty()
        snap.update(self._metrics(db, band_db))
        snap.update({"ready": True, "frames": self.frames, "clip": clip,
                     "bins": [round(float(v), 1) for v in band_db],
                     "freqs": [round(float(f), 1) for f in
                               np.sqrt(self._edges[:-1] * self._edges[1:])],
                     "waterfall": [[round(float(v), 1) for v in row] for row in self._rows]})
        self._snap = snap

    def snapshot(self) -> dict:
        return self._snap

    def reset(self) -> None:
        self._buf = np.zeros(self.nfft, dtype=np.float32)
        self._fill = 0
        self._rows.clear()
        self._since_fft = 0
        self.frames = 0
        self.ffts = 0
        self._snap = self._empty()


# ---------------------------------------------------------------------------
def _selftest() -> int:
    """离线自检：纯音、白噪声、静音三种输入。"""
    import math
    print("== 频谱分析仪自检 ==")

    def tone(freq: float, secs: float = 1.0, amp: float = 0.3, rate: int = RATE):
        t = np.arange(int(rate * secs)) / rate
        x = (amp * FULL_SCALE * np.sin(2 * math.pi * freq * t)).astype("<i2")
        return np.stack([x, x], axis=1)

    ok = True
    an = Analyzer()
    for blk in np.array_split(tone(1000.0), 100):
        an.feed(blk)
    s = an.snapshot()
    print("纯音 1 kHz:", {k: s[k] for k in ("peak_hz", "peak_db", "rms_dbfs", "snr_db", "thd_pct")})
    ok &= abs(s["peak_hz"] - 1000.0) < 15.0
    ok &= s["snr_db"] > 40.0
    ok &= s["thd_pct"] < 1.0

    rng = np.random.default_rng(7)
    an.reset()
    noise = (rng.normal(0, 0.05, RATE) * FULL_SCALE).clip(-32768, 32767).astype("<i2")
    noise = np.stack([noise, noise], axis=1)
    for blk in np.array_split(noise, 100):
        an.feed(blk)
    s2 = an.snapshot()
    print("白噪声  :", {k: s2[k] for k in ("peak_db", "rms_dbfs", "snr_db", "thd_pct")})
    ok &= s2["snr_db"] < 25.0            # 平谱：没有突出单音
    ok &= s2["thd_pct"] < 5.0

    an.reset()
    an.feed(np.zeros((480, 2), dtype="<i2"))
    for _ in range(300):
        an.feed(np.zeros((480, 2), dtype="<i2"))
    s3 = an.snapshot()
    print("静音    :", {k: s3[k] for k in ("rms_dbfs", "floor_db", "snr_db")})
    ok &= s3["rms_dbfs"] < -100.0
    ok &= s3["clip"] == 0

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
