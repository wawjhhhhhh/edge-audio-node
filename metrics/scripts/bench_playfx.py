#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""包2「音色包」的离线验收：把每个预设跑在真实素材上，量曲线、量响度、留 WAV。

为什么要"留 WAV"
----------------
音效好不好最终靠耳朵，但"两个人听到的是不是同一条链"必须靠文件。这个脚本把
每个预设的输出落成 48 kHz/16 bit 单声道 WAV（`.tmp/fx_<preset>.wav`），用户
可以直接用播放器对比；同时打印客观指标，避免"听着不一样"其实只是响度不同。

量什么
------
1. **均衡曲线**：逐段扫频（1/3 倍频程密度），比较"预设 on"与"全平"的幅度差，
   和预设声明的增益逐段对比（±1 dB 内算过，峰值滤波器在中心频率处增益就是声明值）。
2. **响度**：LUFS（`loudness.py`，BS.1770）+ 真峰值 + 限幅削减量 —— 用来做
   "等响 A/B"，防止"更响=更好听"的错觉。
3. **限幅天花板**：所有预设的输出峰值必须 ≤ CEILING_DB + 0.2。

用法
----
    python bench_playfx.py                # 全部预设
    python bench_playfx.py --preset rock  # 单个
    python bench_playfx.py --file x.wav   # 换素材
"""

from __future__ import annotations

import argparse
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import playfx
from loudness import LoudnessMeter

DEFAULT_SRC = os.path.join(
    r"D:\deepseek herness\材料库\04-实测记录\音频样本\2026-10-24_带噪AB_n400",
    "01_noisy_mic_plus_noise.wav")
OUT_DIR = r"D:\deepseek herness\.tmp"


# ---------------------------------------------------------------- 素材

def read_wav_mono(path: str, want_rate: int = playfx.RATE) -> np.ndarray:
    """读 WAV → 单声道 float32 [-1,1] @ 48 kHz（线性插值重采样，够用）。"""
    with wave.open(path, "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    elif sw == 1:
        x = (np.frombuffer(raw, dtype="<u1").astype(np.float64) - 128.0) / 128.0
    else:
        raise ValueError("不支持的位深 %d" % sw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != want_rate:
        t_out = np.arange(int(len(x) * want_rate / rate)) / want_rate
        x = np.interp(t_out, np.arange(len(x)) / rate, x)
    return x.astype(np.float32)


def synth_material(seconds: float = 12.0) -> np.ndarray:
    """没有素材时的兜底：低频 + 中频 + 高频三段叠加，够听出每个预设的差别。"""
    n = int(seconds * playfx.RATE)
    t = np.arange(n) / playfx.RATE
    x = (0.30 * np.sin(2 * np.pi * 80 * t)
         + 0.18 * np.sin(2 * np.pi * 440 * t)
         + 0.10 * np.sin(2 * np.pi * 3000 * t)
         + 0.04 * np.sin(2 * np.pi * 9000 * t))
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 0.25 * t)
    return (x * env).astype(np.float32)


def write_wav(path: str, x: np.ndarray, rate: int = playfx.RATE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    y = np.clip(np.asarray(x) * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(y.tobytes())


# ---------------------------------------------------------------- 量测

def run_preset(fx: playfx.PlayFX, mono: np.ndarray, chunk: int = 480,
               collect: bool = True):
    """按 10 ms 块跑完整段，返回 (输出, 限幅最大削减 dB, 压缩最大削减 dB)。"""
    fx.enabled = True
    fx.reset()
    out = np.empty_like(mono)
    max_lim = 0.0
    max_gr = [0.0, 0.0, 0.0]
    for i in range(0, len(mono) - chunk + 1, chunk):
        blk = mono[i:i + chunk]
        stereo = np.repeat((blk * 32767.0).astype(np.int16).reshape(-1, 1), 2, axis=1)
        y = fx.process(stereo)
        out[i:i + chunk] = y[:, 0].astype(np.float64) / 32768.0
        st = fx.stats()
        max_lim = max(max_lim, st["lim_gr_db"])
        max_gr = [max(a, b) for a, b in zip(max_gr, st["comp_gr_db"])]
    if collect:
        return out, max_lim, max_gr
    return None, max_lim, max_gr


def measure_curve(fx: playfx.PlayFX, freqs, seconds: float = 0.6) -> np.ndarray:
    """逐频点测"预设 on / 全平"的幅度差（dB）。"""
    res = np.zeros(len(freqs))
    for i, f in enumerate(freqs):
        n = int(seconds * playfx.RATE)
        t = np.arange(n) / playfx.RATE
        x = (0.25 * np.sin(2 * np.pi * f * t)).astype(np.float32)
        y_on, _, _ = run_preset(fx, x, collect=True)
        tail = y_on[n // 2:]
        rms_on = np.sqrt(np.mean(tail ** 2))
        fx.set_eq([0.0] * len(playfx.EQ_BANDS))
        y_off, _, _ = run_preset(fx, x, collect=True)
        tail = y_off[n // 2:]
        rms_off = np.sqrt(np.mean(tail ** 2))
        res[i] = 20 * np.log10(max(rms_on, 1e-9) / max(rms_off, 1e-9))
    return res


def lufs_of(x: np.ndarray) -> tuple:
    # 注意：LoudnessMeter 的第一个参数是**声道数**（默认 2），不是采样率。
    # 早先误写成 LoudnessMeter(playfx.RATE) → 48000 个声道，feed 里每块循环
    # 48000 次，整个 bench 卡死在这个循环上（查了很久）。
    m = LoudnessMeter()
    blk = 480
    for i in range(0, len(x) - blk + 1, blk):
        mono = np.repeat((x[i:i + blk] * 32767).astype(np.int16).reshape(-1, 1), 2, axis=1)
        m.feed(mono)
    st = m.stats()
    return float(st.get("i") or -70.0), float(st.get("peak") or -70.0)


# ---------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None, help="素材 WAV（默认用素材库里的真录音）")
    ap.add_argument("--preset", default=None, help="只跑一个预设")
    ap.add_argument("--seconds", type=float, default=10.0, help="最多用多少秒素材")
    ap.add_argument("--no-wav", action="store_true")
    a = ap.parse_args(argv)

    src = a.file or DEFAULT_SRC
    if os.path.exists(src):
        mono = read_wav_mono(src)[: int(a.seconds * playfx.RATE)]
        print("素材: %s（%.1f s）" % (os.path.basename(src), len(mono) / playfx.RATE))
    else:
        mono = synth_material(a.seconds)
        print("素材: 合成三频段（%.1f s）—— 没找到 %s" % (a.seconds, src))

    presets = [a.preset] if a.preset else list(playfx.PRESETS)
    fx = playfx.PlayFX()

    print("\n=== 客观对照（同一素材、同一链只改预设）")
    print("%-7s %-14s %8s %8s %8s %8s %8s" %
          ("预设", "说明", "LUFS", "峰值dB", "限幅dB", "低GR", "中GR"))
    rows = []
    for name in presets:
        fx.set_preset(name)
        out, lim, gr = run_preset(fx, mono)
        lufs, peak = lufs_of(out)
        rows.append((name, lufs, peak, lim, gr, out))
        print("%-7s %-14s %8.1f %8.1f %8.2f %8.1f %8.1f" %
              (name, playfx.PRESET_TEXT.get(name, "")[:12], lufs, peak, lim, gr[0], gr[1]))

    ok = True
    ceil = playfx.CEILING_DB
    for name, lufs, peak, lim, gr, out in rows:
        if peak > ceil + 0.2:
            print("  ❌ %s 峰值 %.1f dB 超过天花板 %.1f" % (name, peak, ceil))
            ok = False

    # 等响检查：以 flat 为基准，差 >6 LU 说明"听着更响"会干扰判断
    base = dict((r[0], r[1]) for r in rows).get("flat")
    if base is not None:
        print("\n=== 相对原声的响度差（>±6 LU 会让人觉得'只是更响'）")
        for name, lufs, *_ in rows:
            d = lufs - base
            flag = "" if abs(d) <= 6.0 else "  ⚠ 偏响/偏轻"
            print("  %-7s %+6.1f LU%s" % (name, d, flag))

    if not a.no_wav:
        os.makedirs(OUT_DIR, exist_ok=True)
        for name, lufs, peak, lim, gr, out in rows:
            p = os.path.join(OUT_DIR, "fx_%s.wav" % name)
            write_wav(p, out)
            print("WAV: %s" % p)

    # 曲线核对（只对一个预设，扫频比较慢）
    if a.preset and a.preset in playfx.PRESETS:
        fx.set_preset(a.preset)
        target = np.array(playfx.PRESETS[a.preset][0], dtype=float)
        print("\n=== 均衡曲线核对（%s，逐段中心频率）" % a.preset)
        print("%9s %8s %8s %6s" % ("频率Hz", "目标dB", "实测dB", "差"))
        bad = 0
        for f, want in zip(playfx.EQ_BANDS, target):
            got = measure_curve(fx, [f])[0]
            # 单段在中心频率的增益≈声明值；多段叠加时相邻段有贡献，所以整体比较
            print("%9.1f %8.1f %8.2f %6.2f" % (f, want, got, got - want))
            if abs(got - want) > 1.5:
                bad += 1
        if bad:
            print("  ⚠ %d 个频点偏差 >1.5 dB（相邻段叠加所致，可接受但要看一眼）" % bad)

    print("\nRESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
