"""三档降噪 + 延迟档的对比指标表。

原来这个脚本把目录写死在文件里、文件名写死在循环里，只能跑一次。
现在改成命令行：既能分析已有 wav 目录，也能作为"采集验收"的统计后端。

指标口径（写进技术报告时照抄这几行）：

- 帧 = 480 采样 = 10 ms，48 kHz / 16 bit，取左声道
- 电平 = 每帧 RMS 转 dBFS
- 跳变 = 相邻帧电平差的绝对值 > 10 dB 的次数（泵动/爆音的代理指标，不是 THD）
- 削波 = |样本| >= 32700 的个数
- 10 s 录音帧数 < 950 说明有丢帧

用法：
    python noise_metrics.py                       # 分析默认样本目录
    python noise_metrics.py <目录>                 # 分析指定目录下所有 wav
    python noise_metrics.py <目录> --secs 10       # 只报前 N 秒
"""
import argparse
import os
import sys
import wave

import numpy as np

DEFAULT_DIR = (r"D:\deepseek herness\材料库\04-实测记录\音频样本"
               r"\2026-10-27_三档语音对比")


def load(path):
    w = wave.open(path)
    n = w.getnframes()
    ch = w.getnchannels()
    raw = w.readframes(n)
    sr = w.getframerate()
    w.close()
    x = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).astype(np.float64)
    return x[:, 0], sr


def metrics(x, sr, secs=None):
    if secs:
        x = x[: int(secs * sr)]
    nfr = len(x) // 480
    if nfr < 2:
        return None
    fr = x[: nfr * 480].reshape(-1, 480)
    e = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) / 32768 + 1e-12)
    clip = int((np.abs(x) >= 32700).sum())
    d2 = np.abs(np.diff(e))
    return {
        "frames": nfr,
        "secs": nfr * 0.01,
        "mu": float(e.mean()),
        "sd": float(e.std()),
        "jumps": int((d2 > 10).sum()),
        "maxjump": float(d2.max()) if len(d2) else 0.0,
        "clip": clip,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default=DEFAULT_DIR)
    ap.add_argument("--secs", type=float, default=None)
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print("目录不存在：%s" % args.dir)
        return 1

    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(".wav"))
    if not files:
        print("目录里没有 wav")
        return 1

    print("%-32s %6s %7s %8s %6s %10s %9s %6s"
          % ("文件", "帧数", "时长s", "电平mu", "sd", "跳变>10dB", "最大跳变", "削波"))
    print("-" * 94)
    for f in files:
        try:
            x, sr = load(os.path.join(args.dir, f))
        except Exception as exc:
            print("%-32s 读取失败：%r" % (f, exc))
            continue
        m = metrics(x, sr, args.secs)
        if m is None:
            print("%-32s 太短" % f)
            continue
        flag = ""
        if m["frames"] < 950 and (args.secs or 10) >= 9.9:
            flag = "  <- 疑似丢帧"
        print("%-32s %6d %7.2f %8.1f %6.1f %10d %9.1f %6d%s"
              % (f, m["frames"], m["secs"], m["mu"], m["sd"],
                 m["jumps"], m["maxjump"], m["clip"], flag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
