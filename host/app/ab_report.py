"""ab_report.py -- 一键降噪 A/B 对比 + 自动生成可贴进文档的报告。

## 为什么要有这个

"换了降噪听起来更安静"是没法被验证的结论。答辩时评委只会问一句：**安静了多少 dB？**
而这个数字现场手搓不出来——要切档、要掐秒、要同时盯着帧率和丢包。

这个模块把三件事做成一件事：点一下按钮 →
  1. 依次切到 旁路 / 轻量门 / RNNoise 三档，每档先稳定 1.5 s 再测 8 s；
  2. 每档采集 帧率 / 丢帧 / 欠载 / RMS / 噪声底 / 语音峰值，并存一段 2 s WAV；
  3. 落一份 markdown 报告（含表格 + 结论 + 音频文件名）。

跑完 30 秒，手上就有"能贴进比赛文档的表"和"能直接放给评委听的三段音频"。

## 指标怎么算（口径要写清楚，否则数字没法复现）

- **帧率**：接收端 frame 计数增量 / 实测时长
- **噪声底**：把测量窗按 50 ms 切帧，取各帧 RMS(dBFS) 的 **p10**——语音只占一小部分时间，
  所以 p10 基本落在背景噪声上
- **语音峰值**：同样的帧 RMS 取 **p95**
- **抑制量**：`噪声底(旁路) − 噪声底(该档)`，正数表示压下去了
"""

from __future__ import annotations

import os
import time

import numpy as np

from udp_audio import SAMPLE_RATE, mono_mix, rms_dbfs, write_wav

# 三档：(发送给板子的名字, 显示名, 说明)
PHASES = (
    ("bypass", "旁路", "DSP 不处理，作为基准"),
    ("gate", "轻量门", "整帧幅度缩放，0.13 ms/帧"),
    ("rnnoise", "RNNoise", "神经网络，20 ms/帧（带噪时超预算）"),
)


def _frame_db(samples, frame_ms: float = 50.0) -> np.ndarray:
    """把一段单声道信号按 frame_ms 切帧，返回每帧的 dBFS。"""
    x = mono_mix(samples) if getattr(samples, "ndim", 1) > 1 else np.asarray(samples)
    n = int(SAMPLE_RATE * frame_ms / 1000.0)
    if x.size < n:
        return np.array([rms_dbfs(x)[1]]) if x.size else np.array([-120.0])
    m = x.size // n
    out = np.empty(m, dtype=np.float64)
    for i in range(m):
        out[i] = rms_dbfs(x[i * n:(i + 1) * n])[1]
    return out


class ABRunner:
    """跑一轮三档对比。阻塞式；调用方放进工作线程，用 log 回调刷界面。"""

    def __init__(self, send_mode, tap, stats_fn, out_dir: str,
                 log=None, dwell_s: float = 1.5, measure_s: float = 8.0,
                 phases=PHASES):
        self.send_mode = send_mode        # callable(name) -> None，发 '0'/'2'/'1'
        self.tap = tap                    # RingTap
        self.stats_fn = stats_fn          # callable() -> dict
        self.out_dir = out_dir
        self.log = log or (lambda s: None)
        self.dwell_s = float(dwell_s)
        self.measure_s = float(measure_s)
        self.phases = tuple(phases)
        self.rows: list[dict] = []
        self.report_path: str | None = None

    # -- 单档测量
    def _measure_one(self, key: str, label: str) -> dict:
        s0 = self.stats_fn()
        self.send_mode(key)
        time.sleep(self.dwell_s)            # 等门控/RNNoise 收敛（噪声底 300 ms、RNNoise 2~13 s）
        s1 = self.stats_fn()

        chunks = []
        t0 = time.monotonic()
        keep = max(1, int(SAMPLE_RATE * 0.1))
        while time.monotonic() - t0 < self.measure_s:
            blk = self.tap.read_last(keep)
            if blk is not None and len(blk):
                chunks.append(np.array(blk, copy=True))
            time.sleep(0.1)
        elapsed = max(1e-6, time.monotonic() - t0)
        s2 = self.stats_fn()

        if chunks:
            audio = np.concatenate(chunks, axis=0)
        else:
            audio = np.zeros((0, 2), dtype=np.int16)
        db = _frame_db(audio) if audio.size else np.array([-120.0])

        row = {
            "key": key,
            "label": label,
            "seconds": elapsed,
            "fps": (s2.get("frames", 0) - s1.get("frames", 0)) / elapsed,
            "lost": s2.get("lost", 0) - s1.get("lost", 0),
            "under": s2.get("under", 0) - s1.get("under", 0),
            "over": s2.get("over", 0) - s1.get("over", 0),
            "bad": s2.get("bad", 0) - s1.get("bad", 0),
            "rms": float(np.mean(db)),
            "floor": float(np.percentile(db, 10)),
            "peak": float(np.percentile(db, 95)),
            "wav": "",
        }

        if audio.size:
            name = "ab_%s_%s.wav" % (self.stamp, key)
            path = os.path.join(self.out_dir, name)
            try:
                write_wav(path, audio, SAMPLE_RATE)
                row["wav"] = name
            except Exception as exc:                     # 存音频失败不该毁掉整轮测量
                self.log("存 WAV 失败（%s）：%r" % (key, exc))

        self.log("%-6s fps=%.1f  噪声底=%.1f dBFS  峰值=%.1f dBFS  丢帧=%d"
                 % (label, row["fps"], row["floor"], row["peak"], row["lost"]))
        del s0
        return row

    # -- 整轮
    def run(self) -> str:
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(self.out_dir, exist_ok=True)
        self.log("开始 A/B：每档先稳定 %.1f s，再测 %.0f s，共 %d 档（约 %.0f s）"
                 % (self.dwell_s, self.measure_s, len(self.phases),
                    len(self.phases) * (self.dwell_s + self.measure_s)))
        for key, label, _desc in self.phases:
            self.rows.append(self._measure_one(key, label))

        base = next((r for r in self.rows if r["key"] == "bypass"), None)
        for r in self.rows:
            r["supp"] = (base["floor"] - r["floor"]) if base else 0.0

        path = self._write_report()
        self.report_path = path
        self.log("报告已生成：%s" % path)
        return path

    # -- 报告
    def _write_report(self) -> str:
        path = os.path.join(self.out_dir, "ab_report_%s.md" % self.stamp)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        lines.append("# 降噪 A/B 自动报告\n")
        lines.append("- 生成时间：%s" % now)
        lines.append("- 采样率：%d Hz，16 bit，立体声" % SAMPLE_RATE)
        lines.append("- 每档：先稳定 %.1f s，再测量 %.0f s（帧率/丢帧按测量窗增量统计）"
                     % (self.dwell_s, self.measure_s))
        lines.append("- 噪声底 = 50 ms 帧 RMS(dBFS) 的 p10；语音峰值 = p95；"
                     "抑制量 = 旁路噪声底 − 该档噪声底\n")
        lines.append("| 档位 | 帧率 fps | 丢帧 | 欠载 | 溢出 | RMS dBFS | 噪声底 dBFS | 语音峰值 dBFS | 抑制量 dB |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in self.rows:
            lines.append("| %s | %.1f | %d | %d | %d | %.1f | %.1f | %.1f | %+.1f |"
                         % (r["label"], r["fps"], r["lost"], r["under"], r["over"],
                            r["rms"], r["floor"], r["peak"], r.get("supp", 0.0)))
        lines.append("")
        if self.rows:
            best = max(self.rows, key=lambda r: r.get("supp", -999))
            gate = next((r for r in self.rows if r["key"] == "gate"), None)
            rnn = next((r for r in self.rows if r["key"] == "rnnoise"), None)
            lines.append("## 结论（自动生成，按实测数据）\n")
            lines.append("- 抑制量最大的档位：**%s（%+.1f dB）**" % (best["label"], best.get("supp", 0.0)))
            if gate:
                lines.append("- 轻量门：帧率 **%.1f fps**、抑制 **%+.1f dB**、丢帧 %d"
                             % (gate["fps"], gate.get("supp", 0.0), gate["lost"]))
            if rnn:
                lines.append("- RNNoise：帧率 **%.1f fps**、抑制 **%+.1f dB**、丢帧 %d"
                             % (rnn["fps"], rnn.get("supp", 0.0), rnn["lost"]))
                if gate and rnn["fps"] < gate["fps"] - 5:
                    lines.append("- → RNNoise 帧率比轻量门低 %.1f fps，"
                                 "与「20 ms/帧超出 10 ms 预算、只能隔帧处理」的结论一致"
                                 % (gate["fps"] - rnn["fps"]))
            lines.append("")
        wavs = [r for r in self.rows if r.get("wav")]
        if wavs:
            lines.append("## 同轮录音（可直接 A/B 试听）\n")
            for r in wavs:
                lines.append("- `%s` —— %s" % (r["wav"], r["label"]))
            lines.append("")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return path
