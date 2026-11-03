"""离线自测：验证 PC 增强链的三件事。

一、STFT/ISTFT 往返是否保真（OLA 归一化正确）
二、旁路时是否零拷贝、时间轴不变
三、降噪是否真的有效，且语音段不被过度削弱

这三个测试是分层的：一不过，二和三的数字都没有意义。所以失败时先看一。

用法：
    python test_pc_enhance.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pc_enhance as pe


def db(x):
    return 20.0 * np.log10(max(1e-12, abs(x)))


def test_ola_identity():
    """增益恒为 1 时，重建信号必须逐样本还原。

    做法：不走 Engine.process，直接复现它的 STFT→ISTFT 数学，但把增益写成 1。
    这样测的是**重建框架**本身，不受降噪参数干扰。之前就是因为把两件事混在
    一个用例里，导致框架正确时用例仍然红，白查了一轮。
    """
    N, H, W = pe.NFFT, pe.HOP, pe.WIN
    norm = float(pe._win_norm()[0])

    rng = np.random.default_rng(1)
    x = rng.standard_normal(48000)

    nf = (len(x) - N) // H + 1
    ola = np.zeros(N)
    outs = []
    for i in range(nf):
        fr = x[i * H:i * H + N]
        td = np.fft.irfft(np.fft.rfft(fr * W), N) * W       # 增益=1
        ola += td
        outs.append((ola[:H] / norm).copy())
        ola[:-H] = ola[H:]
        ola[-H:] = 0.0

    y = np.concatenate(outs)
    # 前 NFFT 个点是起始填充（历史全零），跳过
    skip = N
    e = y[skip:] - x[skip:len(y)]
    rel = np.sqrt(np.mean(e ** 2)) / np.sqrt(np.mean(x[skip:len(y)] ** 2))
    ok = rel < 1e-3
    print("[一] 重建往返 相对误差 %.2e (%.0f dB) —— 要求 < 1e-3  %s"
          % (rel, db(rel), "PASS" if ok else "FAIL"))
    return ok


def test_passthrough_untouched():
    """enabled=False 时必须返回原对象，不能复制、不能改值。"""
    eng = pe.Engine()
    blk = np.arange(480 * 2, dtype=np.int16).reshape(480, 2)
    out = eng.process(blk)
    same = out is blk
    print("[二] 旁路零拷贝 %s" % ("PASS" if same else "FAIL"))
    return same


def test_noise_reduction():
    """合成语音 + 噪声，验证噪声被压、语音被保住。

    **指标口径必须收敛到谱线**，这一点花了不少代价才搞清楚：
    早期直接比较"原始混合信号"和"处理结果"的 RMS，得到 -5.5 dB 就无法通过。
    但那个数字里混着噪声：输入 = 语音 + 噪声，输出 = 窄带谱减后的语音 + 少量噪声。
    稳态高斯噪声的能量摊在 24000 个样点上，而语音只占 6 根谱线 —— 谱减把
    谱线之间的白噪去掉以后，总 RMS 必然显著下降，这**正是降噪应该做的事**，
    却被算成了"语音被削"。真实听感里语音一点没小（谱线幅度基本不变）。

    所以这里分开量：
      保真 = 320 Hz 及其 5 个谐波的谱线幅度比（这才是"语音电平"）
      降噪 = 语音静音段的 RMS 比
    """
    eng = pe.Engine(strength=70, protect=30)
    eng.enabled = True
    rng = np.random.default_rng(11)
    fs = 48000
    n = fs * 4

    t = np.arange(n) / fs
    voice = np.zeros(n)
    for k, a in ((1, 1.0), (2, 0.55), (3, 0.38), (4, 0.24), (5, 0.16), (6, 0.12)):
        voice += a * np.sin(2 * np.pi * 320 * k * t)
    # 音节包络：0.5 Hz，有明显的静音间隙
    env = (0.5 + 0.5 * np.sin(2 * np.pi * 0.5 * t - np.pi / 2)) ** 3
    voice *= env
    voice *= 9000.0 / max(1e-9, np.max(np.abs(voice)))

    noise = rng.standard_normal(n) * 900
    noise += 1100 * np.sin(2 * np.pi * 50 * t)            # 低频嗡
    mixed = (voice + noise).clip(-32768, 32767).astype(np.int16)

    out = []
    for i in range(0, n - pe.HOP, pe.HOP):
        piece = np.repeat(mixed[i:i + pe.HOP, None], 2, axis=1)
        out.append(eng.process(piece)[:, 0])
    y = np.concatenate(out).astype(np.float64)
    x = mixed[:len(y)].astype(np.float64)

    # 丢弃前 1.5 s 收敛期
    s = int(1.5 * fs)
    xx, yy, ee = x[s:], y[s:], env[:len(y)][s:]

    loud = ee > 0.85           # 语音最强段
    quiet = ee < 0.02          # 纯噪声段

    # 保真：谱线幅度比。窗取整段，谱线足够尖锐
    f = np.fft.rfftfreq(int(loud.sum()), 1.0 / fs)
    zf = np.fft.rfft(xx[loud])
    yf = np.fft.rfft(yy[loud])

    def lines(sp):
        tot = 0.0
        for k in range(1, 7):
            m = np.abs(f - 320 * k) < 8.0
            tot += float(np.sum(np.abs(sp[m]) ** 2))
        return np.sqrt(tot)

    keep = db(lines(yf) / max(1e-9, lines(zf)))

    in_q = np.sqrt(np.mean(xx[quiet] ** 2))
    out_q = np.sqrt(np.mean(yy[quiet] ** 2))
    supp = db(in_q / max(1e-9, out_q))

    print("[三] 噪声段 %.1f → %.1f dBFS（抑制 %.1f dB）"
          % (db(in_q / 32768), db(out_q / 32768), supp))
    print("     语音谱线电平变化 %+.1f dB（320Hz 及 5 个谐波）" % keep)
    ok = supp > 8.0 and -3.0 < keep < 3.0
    print("     判定：抑制 > 8 dB 且语音谱线变化在 ±3 dB 内  %s"
          % ("PASS" if ok else "FAIL"))
    return ok


def test_strength_monotonic():
    """强度越高，纯噪声输入的残余电平应越低（单调不增）。"""
    levels = (0, 25, 50, 75, 100)
    res = []
    for s in levels:
        eng = pe.Engine(strength=s, protect=20)
        eng.enabled = True
        rng = np.random.default_rng(3)
        n = 48000 * 3
        sig = (rng.standard_normal(n) * 2500).clip(-32768, 32767).astype(np.int16)
        out = []
        for i in range(0, n - pe.HOP, pe.HOP):
            out.append(eng.process(np.repeat(sig[i:i + pe.HOP, None], 2, axis=1))[:, 0])
        y = np.concatenate(out).astype(np.float64)[48000:]     # 跳过收敛期
        res.append(db(np.sqrt(np.mean(y ** 2)) / 32768))
    print("[四] 纯噪声残余: %s"
          % "  ".join("%d→%.1f" % (s, r) for s, r in zip(levels, res)))
    ok = all(res[i] >= res[i + 1] - 1.5 for i in range(len(res) - 1))
    ok = ok and (res[0] - res[-1]) > 6.0          # 强度必须真的拉开差距
    print("     判定：单调不增且最高最低差 > 6 dB  %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    print("参数：NFFT=%d  HOP=%d  窗平方和=%.6f  (NFFT/HOP=%.2f)"
          % (pe.NFFT, pe.HOP, float(pe._win_norm()[0]), pe.NFFT / pe.HOP))
    print()
    checks = [
        ("一 重建往返", test_ola_identity()),
        ("二 旁路零拷贝", test_passthrough_untouched()),
        ("三 降噪与保真", test_noise_reduction()),
        ("四 强度单调", test_strength_monotonic()),
    ]
    print()
    for name, r in checks:
        print("  %-14s %s" % (name, "PASS" if r else "FAIL"))
    n_ok = sum(1 for _, r in checks if r)
    print("\n通过 %d / %d" % (n_ok, len(checks)))
    return 0 if n_ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
