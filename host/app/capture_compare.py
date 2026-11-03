"""批量采集：三模式 × 延迟档，产出可直接进报告的 wav 目录。

为什么需要它：报告里「RNNoise 攒帧后抑制多少 dB」这个数字
**必须在 100 fps 满帧、且与轻量门等延迟的条件下重测**。
旧数据（48 fps 丢帧）已经作废，不能沿用。

本脚本只做一件事：按顺序设定"模式 + 延迟档"，在每个组合下录固定时长，
用板子自己的上行流，不经过任何 PC 侧处理。

用法（需先启动控制台 :8770，且板子在线）：
    python capture_compare.py --secs 10 --out "D:/.../2026-10-30_三档等延迟对比"

采集期间请保持同一个声源与同一个说话内容，否则三份录音不可比。
脚本会在每个档位间留 0.8 s 让环形缓冲重建，并在结束时打印每个文件的指标。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request as U
import wave

import numpy as np

MIC_PORT = 50505
API = "http://127.0.0.1:8770"
_op = U.build_opener(U.ProxyHandler({}))

# (标签, 模式, 延迟帧)；延迟 30 帧 = 300 ms
PLAN = [
    ("01_旁路_300ms", "bypass", 30),
    ("02_轻量门_300ms", "gate", 30),
    ("03_RNNoise_300ms", "rnnoise", 30),
    ("04_轻量门_200ms", "gate", 20),
    ("05_RNNoise_200ms", "rnnoise", 20),
]


def post(body):
    req = U.Request(API + "/api/cmd", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
    return json.loads(_op.open(req, timeout=8).read())


def record(seconds):
    """收 50505 的上行 PCM，返回 int16 立体声 array。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    except OSError:
        pass
    s.bind(("0.0.0.0", MIC_PORT))
    s.settimeout(0.5)
    out = []
    frames = 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            data, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        if len(data) < 12:
            continue
        pay = data[8:]
        n = len(pay) // 4
        if not n:
            continue
        out.append(np.frombuffer(pay[:n * 4], dtype="<i2").reshape(n, 2).copy())
        frames += n
    s.close()
    if not out:
        return np.zeros((0, 2), dtype=np.int16), 0
    return np.concatenate(out), frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # 先停 PC 侧接收，避免两个 socket 抢同一个端口拿不到完整的流
    try:
        post({"op": "listen", "v": False})
        time.sleep(0.4)
    except Exception as exc:
        print("控制台未响应：%r" % (exc,))
        print("请先启动 run_web.py，或确认 8770 在线")
        return 1

    print("采集 %d 秒 / 组，共 %d 组。" % (args.secs, len(PLAN)))
    print("请保持同一音源、同一说话内容，中途不要改音量。\n")
    for i in range(3, 0, -1):
        sys.stdout.write("\r  %d ..." % i)
        sys.stdout.flush()
        time.sleep(1)
    print("\r  开始\n")

    rows = []
    for tag, mode, lat in PLAN:
        post({"op": "dsp", "v": mode})
        time.sleep(0.3)
        post({"op": "rntarget", "v": lat})
        time.sleep(0.8)                      # 等环形缓冲重建

        pcm, n = record(args.secs)
        path = os.path.join(args.out, tag + ".wav")
        if n:
            w = wave.open(path, "wb")
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(pcm.tobytes())
            w.close()
        rows.append((tag, n, path))
        print("  %-20s %6d 采样 (%.2f s)  -> %s"
              % (tag, n, n / 48000.0, os.path.basename(path)))

    print("\n完成。下一步：")
    print('  python tools/noise_metrics.py "%s" --secs %g' % (args.out, args.secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
