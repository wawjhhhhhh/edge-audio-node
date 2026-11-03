"""测 DSP 延迟档是否真的作用在音频上。

不用声学环路（麦克风听到扬声器再回来，变量太多），而是利用一个已知事实：
板子把 MIC 采样以 100 fps 连续上行，每个上行包带着"这一帧的序号"。
如果我们在 t0 时刻推一段脉冲给板子的播放通道，板子自己是听不到自己的播放的
（回环只在声学上成立），所以这条路也不通。

改成最直接的一条：**测上行流的相位**。
模块把上行帧数按 100 fps 累加，这个累加器就是板子的采样时钟。
延迟档改变的是"DSP 处理前的样本要在环形缓冲里待多久"，
在只考虑出口的时刻，buffer 越深，同一个瞬态事件出现在上行流里的位置越晚。
用一段有强瞬态的输入（拍手/敲桌）在 delay=200 与 delay=300 下各录 3 秒，
把两段录音的包络互相关，峰值偏移就是两者真实的延迟差。

用法：
    python delay_verify.py                 # 自动测 200 与 300
    python delay_verify.py --keep out.wav  # 同时保存两段录音
"""
import argparse
import json
import socket
import struct
import sys
import time
import urllib.request as U
import wave

import numpy as np

MIC_PORT = 50505
CTL_PORT = 50507
BOARD_IP = "169.254.191.50"
API = "http://127.0.0.1:8770"
_op = U.build_opener(U.ProxyHandler({}))


def post(body):
    req = U.Request(API + "/api/cmd", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
    return json.loads(_op.open(req, timeout=8).read())


def recv_pcm(seconds, port=MIC_PORT):
    """收 N 秒上行 PCM，返回 (int16 mono, 实际收到的帧数)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    except OSError:
        pass
    s.bind(("0.0.0.0", port))
    s.settimeout(0.5)
    chunks = []
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            data, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        if len(data) < 12:
            continue
        # 帧头 8 字节 + PCM；取低 16 位当左声道
        pay = data[8:]
        n = len(pay) // 4
        if not n:
            continue
        a = np.frombuffer(pay[:n * 4], dtype="<i2").reshape(n, 2)
        chunks.append(a[:, 0].copy())
    s.close()
    if not chunks:
        return np.zeros(0, dtype=np.int16), 0
    return np.concatenate(chunks), len(chunks)


def envelope(x, win=240):
    """240 点（5 ms）滑动 RMS 包络，去掉静音段的 0 电平影响。"""
    n = len(x) // win
    if n < 1:
        return np.zeros(0)
    f = x[:n * win].reshape(n, win).astype(np.float64)
    return np.sqrt((f ** 2).mean(axis=1))


def lag_ms(a, b, max_ms=600.0):
    """b 相对 a 的偏移（正数 = b 比 a 晚）。"""
    if len(a) < 8 or len(b) < 8:
        return None
    n = min(len(a), len(b))
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    c = np.correlate(b, a, mode="full")
    lag = int(np.argmax(c)) - (n - 1)
    return lag * 5.0            # 包络一格 = 5 ms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=4.0)
    ap.add_argument("--keep", default=None)
    args = ap.parse_args()

    print("提示：现在请对着麦克风持续说话或制造稳定噪声（敲桌子也行）")
    time.sleep(2.0)

    try:
        post({"op": "listen", "v": False})
    except Exception:
        pass
    time.sleep(0.5)

    results = {}
    for frames in (20, 30):
        post({"op": "dsp", "v": "bypass"})
        time.sleep(0.3)
        post({"op": "rntarget", "v": frames})
        time.sleep(0.6)
        print("  录制 %d ms 档 …" % (frames * 10))
        pcm, nf = recv_pcm(args.secs)
        results[frames] = pcm
        print("    收到 %d 块（%.2f s）" % (nf, len(pcm) / 48000.0))
        if args.keep:
            w = wave.open("%s_%dms.wav" % (args.keep, frames * 10), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(pcm.tobytes())
            w.close()

    e20 = envelope(results[20])
    e30 = envelope(results[30])
    d = lag_ms(e20, e30)
    print()
    if d is None:
        print("样本太短，无法比较")
        return 1
    print("200 ms 档 vs 300 ms 档 包络偏移：%.0f ms" % d)
    print("理论值应为 +100 ms（300 比 200 多攒 10 帧 = 100 ms）")
    if 40 <= abs(d) <= 160:
        print("结论：延迟档确实作用到了音频上")
        return 0
    print("结论：偏移不在合理范围，延迟档可能仍未生效")
    return 2


if __name__ == "__main__":
    sys.exit(main())
