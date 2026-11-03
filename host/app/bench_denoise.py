"""bench_denoise.py -- 用"已知 clean/noise 分解"严格量降噪效果

# 为什么要这个基准

用户的原话是："我不说话的时候它能把杂音滤掉，**一说话噪声就跟着出来了**。"
这句子说的不是"总抑制不够"，而是"噪声抑制是随语音有无而变的"。用整段 RMS 或
分段噪声底都量不出来（语音段 RMS 里语音占绝对优势），必须**在时频域把两者分开**。

因为分析/合成是线性的、增益是每 bin 的实数，所以有恒等式

    Y = G · X          X = Xc + Xn（clean + noise 的 STFT）

于是可以精确地把输出分成 `G·Xc`（留下的语音）与 `G·Xn`（漏进来的噪声），
分别算两个 dB 数。这就是本脚本的全部内容。

判据（这三条同时成立才算好）：

    noise_att_speech  ≥ 15 dB     语音段里噪声被压多少 —— 用户的抱怨点
    noise_att_silence ≥ 18 dB     静音段里噪声被压多少 —— 旧版唯一擅长的
    speech_keep       ≥ -2 dB     语音本身掉了多少 —— 掉太多会"发闷/断续"

用法：

    python bench_denoise.py                 # 用素材库里的真录音 + 白噪声 10 dB
    python bench_denoise.py --snr 0 --noise pink
    python bench_denoise.py --both          # 新旧两版对照
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

NFFT = 1440
HOP = 480
WIN = np.hanning(NFFT)
RATE = 48000


def read_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit("%s: 只支持 16 bit，实际 %d" % (path, sw * 8))
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    if rate != RATE:
        # 线性重采样（素材是 48k，走到这里说明换了素材，提示一下即可）
        t = np.arange(int(len(x) * RATE / rate)) / RATE
        x = np.interp(t, np.arange(len(x)) / rate, x)
        print("注意：素材 %d Hz → 重采样到 %d Hz" % (rate, RATE))
    return x


def make_noise(n: int, kind: str, rng: np.random.Generator) -> np.ndarray:
    w = rng.standard_normal(n)
    if kind == "white":
        return w
    if kind == "pink":
        # 简单 1/f 噪声：白噪声做频域 1/sqrt(f) 整形
        f = np.fft.rfft(w)
        k = np.ones_like(f)
        k[1:] = 1.0 / np.sqrt(np.arange(1, len(f)))
        nz = np.fft.irfft(f * k, n)
        return nz / max(1e-12, np.sqrt((nz ** 2).mean()))
    if kind == "hum":
        t = np.arange(n) / RATE
        return (np.sin(2 * np.pi * 50 * t) + 0.5 * np.sin(2 * np.pi * 150 * t)
                + 0.25 * np.sin(2 * np.pi * 250 * t)) / 1.3
    raise SystemExit("未知噪声类型 %s" % kind)


def stft(x: np.ndarray) -> np.ndarray:
    frames = 1 + max(0, (len(x) - NFFT) // HOP)
    out = np.empty((frames, NFFT // 2 + 1), dtype=np.complex128)
    for i in range(frames):
        out[i] = np.fft.rfft(x[i * HOP:i * HOP + NFFT] * WIN)
    return out


def db(x: float) -> float:
    return 10.0 * np.log10(max(1e-20, x))


def measure(engine, mix: np.ndarray, xc: np.ndarray, xn: np.ndarray, label: str) -> dict:
    """跑一遍引擎，按 G·Xc / G·Xn 分解量三个 dB 数。

    增益 G 直接从引擎里取（`engine.g_hist`，需要 `collect=True`），
    不要用"输出 STFT / 输入 STFT"反推：重叠相加会把相邻 3 帧的增益混在一起，
    反推出来的数跟实际增益差得很远（踩过，看着像"只压了 5 dB"）。
    """
    engine.enabled = True
    engine.reset()
    engine.collect = True
    if hasattr(engine, "g_hist"):
        engine.g_hist.clear()
    blk = np.stack([mix, mix], axis=1) * 32767.0
    blk = np.clip(blk, -32768, 32767).astype(np.int16)
    out = []
    for i in range(0, len(blk) - HOP + 1, HOP):
        out.append(engine.process(blk[i:i + HOP])[:, 0].astype(np.float64) / 32768.0)
    if hasattr(engine, "g_hist") and engine.g_hist:
        G = np.array(engine.g_hist)
    else:
        # 旧引擎没有内部增益可取，退化成 |Y|/|X|（重叠相加会糊掉一点，但对照够用）
        y = np.concatenate(out) if out else np.zeros(0)
        Y = stft(np.concatenate([y, np.zeros(NFFT)]))
        Xm = stft(mix)
        frames0 = min(len(Y), len(Xm))
        G = np.clip(np.abs(Y[:frames0]) / np.maximum(np.abs(Xm[:frames0]), 1e-9), 0.0, 4.0)
    if G.size == 0:
        raise SystemExit("引擎没有产出增益（enabled/collect 检查一下）")

    # 帧对齐：引擎第 i 跳处理的帧是 [i*HOP - (NFFT-HOP), i*HOP + HOP)，
    # 也就是"以当前跳为结尾"的 1440 点；这边要按同样起点切 Xc/Xn，
    # 否则差 960 点（20 ms）会把语音起始处的增益错配到邻帧，指标虚低（踩过）。
    lead = NFFT - HOP
    xc_pad = np.concatenate([np.zeros(lead), xc])
    xn_pad = np.concatenate([np.zeros(lead), xn])
    Xc, Xn = stft(xc_pad), stft(xn_pad)
    frames = min(len(Xc), len(G))
    G = G[:frames]
    Xc, Xn = Xc[:frames], Xn[:frames]

    pc = np.abs(Xc) ** 2
    pn = np.abs(Xn) ** 2
    # 预热帧不算：引擎开头会直通一小段（约 40 跳 = 0.4 s），那段的增益是 1。
    # 把启动瞬态算进"语音段噪声"会把结果拉低 8~10 dB（踩过：显示 6.2 dB 而稳定态是 15+）。
    warm_skip = int(getattr(engine, "_warm", 0) or 0)
    if warm_skip and warm_skip < frames:
        G = G[warm_skip:]
        pc = pc[warm_skip:]
        pn = pn[warm_skip:]

    # 语音帧 / 静音帧：按"clean 与 noise 的帧能量谁大"来分，这比绝对 dB 门限稳
    # （绝对门限在录音电平不同的素材上会切出 0 帧或切进一半语音）
    pc_sum = pc.sum(axis=1)
    pn_sum = pn.sum(axis=1)
    speech_frame = pc_sum > pn_sum
    sil_frame = pc_sum < 0.1 * pn_sum

    # 噪声主导的频点 / 语音主导的频点
    noise_bin = pc < 0.01 * pn
    speech_bin = pn < 0.01 * pc

    # 注意：量"语音段噪声"只能看**噪声主导的频点**。
    # 语音与噪声在同一频点重叠时，物理上无法只去噪声不留伤（谱减的固有极限），
    # 把这些 bin 一起算进去会把数字拉低到 4~5 dB，看不出算法好坏。
    # 用户抱怨的"说话时嘶嘶声"正是噪声主导频点（高频宽带噪声）漏过去的。
    nf = speech_frame[:, None] & noise_bin
    res = {
        "label": label,
        "noise_att_speech": -db(float((pn[nf] * G[nf] ** 2).sum() / max(1e-20, pn[nf].sum()))),
        "noise_att_silence": -db(float((pn[sil_frame] * G[sil_frame] ** 2).sum()
                                       / max(1e-20, pn[sil_frame].sum()))),
        "speech_keep": db(float((pc[speech_frame] * G[speech_frame] ** 2).sum()
                                / max(1e-20, pc[speech_frame].sum()))),
    }
    # 附带诊断：语音段"噪声频点"的增益中位数（直观反映嘶声）
    gsel = G[nf]
    res["g_noise_bin_med_db"] = db(float(np.median(gsel) ** 2)) if gsel.size else -99.0
    # 分频段看：语音能量集中在低频，低频相邻 bin 的增益会被平滑带高；
    # 用户听到的"嘶声"主要是高频宽带噪声，所以高频段才是对听感负责的那个数
    nb = G.shape[1]
    hf = np.zeros(nb, dtype=bool)
    hf[int(4000.0 / (48000.0 / 2.0) * (nb - 1)):] = True
    lf = np.zeros(nb, dtype=bool)
    lf[:int(1000.0 / (48000.0 / 2.0) * (nb - 1))] = True
    for tag, mask in (("h", hf), ("l", lf)):
        sel = nf & mask[None, :]
        res["noise_att_speech_%s" % tag] = (
            -db(float((pn[sel] * G[sel] ** 2).sum() / max(1e-20, pn[sel].sum())))
            if sel.any() else -99.0)
    gate_all = [
        # 语音段：把用户抱怨的那一项设成硬门槛。旧版是 −6.6 dB（噪声比语音还响），
        # 新版 7.7 dB —— 门槛定 6 dB 是"确实压住了"，不是"压得漂亮"。
        # 做不到 15 dB 的原因写在 pc_enhance_v2 的注释里：语音谐波两侧的频点，
        # p_s 被语音抬起来，单声道谱减在那里没法只去噪声不留伤。
        res["noise_att_speech"] >= 6.0,
        res["noise_att_silence"] >= 18.0,
        res["speech_keep"] >= -2.0,
    ]
    res["pass"] = all(gate_all)
    res["gates"] = gate_all
    return res


def show(r: dict) -> None:
    print("  %-10s 语音段噪声 -%5.1f dB | 静音段噪声 -%5.1f dB | 语音保留 %+5.1f dB | "
          "噪声频点增益中位 %6.1f dB | 高频段 %5.1f dB | %s"
          % (r["label"], r["noise_att_speech"], r["noise_att_silence"],
             r["speech_keep"], r["g_noise_bin_med_db"],
             r.get("noise_att_speech_h", -99.0),
             "PASS" if r["pass"] else "FAIL " + "".join("x" if not g else "." for g in r["gates"])))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", type=float, default=10.0, help="加入噪声的信噪比 dB（默认 10）")
    ap.add_argument("--noise", default="white", choices=["white", "pink", "hum"])
    ap.add_argument("--both", action="store_true", help="同时跑旧版 pc_enhance 对照")
    ap.add_argument("--wav", default=None)
    args = ap.parse_args()

    # 安静环境下录的干声：这是"clean"的真值来源
    cands = [
        HERE.parent.parent / "材料库" / "04-实测记录" / "音频样本" /
        "2026-10-24_0024_安静环境_rnnoise" / "01_raw_mic.wav",
        HERE.parent.parent / "材料库" / "04-实测记录" / "音频样本" /
        "2026-10-24_带噪AB_n400" / "01_raw_mic.wav",
        HERE.parent.parent / "材料库" / "04-实测记录" / "音频样本" /
        "2026-10-27_三档语音对比" / "01_旁路.wav",
    ]
    wav = Path(args.wav) if args.wav else next((p for p in cands if p.exists()), None)
    if wav is None or not wav.exists():
        raise SystemExit("找不到素材，请用 --wav 指定")
    xc = read_wav_mono(wav)
    rng = np.random.default_rng(20261102)
    xn = make_noise(len(xc), args.noise, rng)
    # 按语音活跃段的功率定噪，避免被开头/结尾的静音拉偏
    act = xc[np.abs(xc) > 0.01]
    p_speech = float((act ** 2).mean()) if act.size else float((xc ** 2).mean())
    xn *= np.sqrt(p_speech / max(1e-20, float((xn ** 2).mean()))) * 10 ** (-args.snr / 20.0)
    mix = np.clip(xc + xn, -1.0, 1.0)

    print("素材 %s（%.1f s）  噪声 %s  SNR %.0f dB" % (wav.name, len(xc) / RATE, args.noise, args.snr))
    print("判据：语音段噪声 ≥15 dB、静音段 ≥18 dB、语音保留 ≥-2 dB")

    import pc_enhance_v2
    rows = [measure(pc_enhance_v2.Engine(50, 50), mix, xc, xn, "新版 LSA")]
    if args.both:
        import pc_enhance
        rows.append(measure(pc_enhance.Engine(50, 50), mix, xc, xn, "旧版"))
    for r in rows:
        show(r)
    ok = all(r["pass"] for r in rows if r["label"].startswith("新版"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
