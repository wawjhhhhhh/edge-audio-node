#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""包2「音色包」的端到端自检：HTTP 命令 + 音效链 + 推流预处理。

为什么单独写一个（而不是塞进 test_api_http.py）
------------------------------------------------
音效链有两个"只在真链路里才会暴露"的问题：
  1. `op: fx` 必须先于 `op: push` 生效 —— 顺序错了用户会觉得"预设没反应"；
  2. 推流里预处理的块长必须是 480（10 ms），否则压缩器的块级包络会失准。
所以这里直接起一个真 HTTP 服务、用真命令走一遍，并检查**预处理后的音频确实变了**。

端口：HTTP 8784，UDP 50595（都避开正式控制台的 8770 / 50505）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import udp_audio
import web_server

TEST_HTTP = 8784
TEST_UDP = 50595
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".tmp")

ok_count = 0
bad_count = 0


def check(name, cond, detail=""):
    global ok_count, bad_count
    if cond:
        ok_count += 1
        print("PASS  %s %s" % (name, detail))
    else:
        bad_count += 1
        print("FAIL  %s %s" % (name, detail))


def post(body, port=TEST_HTTP):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/cmd" % port,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def state(port=TEST_HTTP):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def make_wav(path, seconds=1.0, rate=48000):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 3000 * t)
    y = np.clip(x * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(y.tobytes())
    return path


def main():
    # 1) 起服务（换端口，避免和正式控制台打架）
    orig_rx = udp_audio.MicReceiver

    class _Rx(orig_rx):
        def __init__(self, *a, **kw):
            kw["port"] = TEST_UDP
            super().__init__(*a, **kw)

    web_server.MicReceiver = _Rx
    srv = threading.Thread(
        target=lambda: web_server.serve(port=TEST_HTTP, host="127.0.0.1"),
        daemon=True)
    srv.start()
    for _ in range(40):
        try:
            state()
            break
        except Exception:
            time.sleep(0.1)

    hub = web_server.HUB

    # 2) 默认关闭
    st = state()
    check("默认音效关闭", st["fx"]["on"] is False, "preset=%s" % st["fx"]["preset"])

    # 3) 切预设 + 打开
    r = post({"op": "fx", "on": True, "preset": "rock"})
    check("切预设 rock 并打开", "ack" in r and r["fx"]["on"] is True
          and r["fx"]["preset"] == "rock", "ack=%s" % r.get("ack"))
    st = state()
    check("状态里能读到 rock", st["fx"]["on"] and st["fx"]["preset"] == "rock")
    check("EQ 十段都在", len(st["fx"]["eq_db"]) == 10, str(st["fx"]["eq_db"]))
    check("rock 的低频被抬高（31.5 Hz > 0 dB）", st["fx"]["eq_db"][0] > 0.0,
          "31.5Hz=%s" % st["fx"]["eq_db"][0])

    # 4) 自定义 EQ（与预设互斥）
    eq = [0, 0, 0, 0, 0, 6, 0, 0, 0, 0]
    r = post({"op": "fx", "eq": eq})
    check("自定义 EQ 生效", r["fx"]["eq_db"] == eq, str(r["fx"]["eq_db"]))

    # 5) 预处理确实改了样点（用 hub 自己的方法，避开 UDP）
    wav = make_wav(os.path.join(TMP, "_fx_test.wav"))
    arr = hub._load_wav_stereo(wav)
    check("WAV 读进来是立体声 int16", arr.ndim == 2 and arr.shape[1] == 2
          and arr.dtype == np.int16, str(arr.shape))

    hub.fx_on = False
    dry = hub._apply_fx(arr)
    check("音效关闭时原样返回", np.array_equal(dry, arr))

    hub.fx_on = True
    hub.fx.set_preset("bass")
    wet = hub._apply_fx(arr)
    check("音效开启后样点被改动", not np.array_equal(wet, arr),
          "max|Δ|=%d" % int(np.max(np.abs(wet.astype(int) - arr.astype(int)))))
    check("长度不变", len(wet) == len(arr))
    peak = 20 * np.log10(max(np.max(np.abs(wet / 32768.0)), 1e-9))
    check("输出不超过天花板", peak <= web_server_ceiling() + 0.4, "%.2f dBFS" % peak)

    # 6) 推流命令（真发 UDP，但只发 0.3 s；板子不在也无所谓，UDP 是无连接的）
    r = post({"op": "push", "v": 0.3, "file": wav})
    check("推流命令被接受", "ack" in r, "ack=%s" % r.get("ack"))
    for _ in range(80):
        if not state()["pushing"]:
            break
        time.sleep(0.1)
    check("推流结束（pushing=False）", state()["pushing"] is False)
    time.sleep(0.2)
    logs = " ".join(x["m"] for x in state()["log"])
    check("日志里有音效预处理记录", "音效链" in logs, logs[-80:])

    # 7) 关闭
    r = post({"op": "fx", "on": False})
    check("能关掉音效", r["fx"]["on"] is False, "ack=%s" % r.get("ack"))

    print("\n结果: %d PASS / %d FAIL" % (ok_count, bad_count))
    print("RESULT: %s" % ("ALL PASS" if bad_count == 0 else "FAIL"))
    return 0 if bad_count == 0 else 1


def web_server_ceiling():
    from playfx import CEILING_DB
    return CEILING_DB


if __name__ == "__main__":
    sys.exit(main())
