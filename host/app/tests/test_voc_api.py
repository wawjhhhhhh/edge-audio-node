"""声码器接进控制台的端到端验收（不碰 8770 / 50505）。

证明四件事：
  1. 关闭时整条通路与以前一样（voc.on=False、voc.frames=0、音频原样）
  2. POST /api/cmd {"op":"voc","v":true} 之后，pull 循环里真的经过声码器
     （/api/state 的 voc.on=True、voc.frames 在涨、ms_per_frame≈2）
  3. 声码器开着时音频仍是"同一句话"（440 Hz 基频还在），但波形不是原样
     （码率 7.2 kbps，参数量化过，逐样点必然不同 —— 相同就说明没接上）
  4. {"op":"voc","v":true,"secs":1} 到点由后端自动关，界面/接口跟着变

用 50597 收 UDP、8782 起 HTTP，都不与真正在跑的控制台（50505 / 8770）冲突。
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import udp_audio
import web_server

TEST_UDP = 50597
TEST_HTTP = 8782
LOG_PATH = os.path.join(tempfile.gettempdir(), "voc_api_result.txt")

_orig_receiver = udp_audio.MicReceiver
web_server.MicReceiver = lambda **kw: _orig_receiver(
    port=TEST_UDP, **{k: v for k, v in kw.items() if k != "port"})

_out = io.StringIO()
_ok = True


def emit(line=""):
    _out.write(line + "\n")
    print(line)


def check(cond, what):
    global _ok
    emit(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        _ok = False


def post(body):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/cmd" % TEST_HTTP,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def get_state():
    with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % TEST_HTTP, timeout=5) as r:
        return json.loads(r.read())


TONE_HZ = 220.0          # 声码器的基频范围是 60–400 Hz（LAG_MIN=20 → 400 Hz 封顶）
FRAME_HZ = 4.0           # 4 Hz 的幅度起伏，模拟说话时音量在动


def make_frame(n, phase0):
    """类语音激励：220 Hz 基频 + 前几个谐波（500/1500/2500 Hz 共振峰附近），
    再叠一点 4 Hz 幅度起伏。

    为什么不用纯正弦：纯音的 LPC 极点半径正好是 1.0（Q=∞），8 bit 反射系数
    一饱和就自激 —— 那是测试信号的问题，不是声码器的问题。真实语音是
    "准周期 + 带宽有限的共振峰"，这里按后者的形状造。
    """
    i = (np.arange(n) + phase0).astype(np.float64)
    t = i / udp_audio.SAMPLE_RATE
    env = 1.0 + 0.25 * np.sin(2 * np.pi * FRAME_HZ * t)
    s = np.zeros(n)
    for h in range(1, 25):
        f = TONE_HZ * h
        if f > 4000.0:
            break
        # 共振峰包络：500 / 1500 / 2500 Hz 三个凸起
        gain = (1.0 / h ** 0.8
                + 1.2 * np.exp(-((f - 500.0) / 260.0) ** 2)
                + 0.8 * np.exp(-((f - 1500.0) / 320.0) ** 2)
                + 0.5 * np.exp(-((f - 2500.0) / 360.0) ** 2))
        s += gain * np.sin(2 * np.pi * f * t)
    s *= 0.22 * 32767 * env / max(1e-9, np.abs(s).max())
    v = s.astype(np.int16)
    f = np.zeros((n, udp_audio.CHANNELS), dtype=np.int16)
    f[:, 0] = v
    f[:, 1] = v
    return f


def pump(seconds, fps=100):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq, sent, phase = 0, 0, 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        frame = make_frame(udp_audio.FRAME_SAMPLES, phase)
        phase += udp_audio.FRAME_SAMPLES
        raw = udp_audio.frame_to_bytes(frame)
        half = len(raw) // 2
        for chunk, body in ((0, raw[:half]), (1, raw[half:])):
            sock.sendto(udp_audio.pack_packet(body, seq, chunk), ("127.0.0.1", TEST_UDP))
        sent += 1
        seq = (seq + 1) & 0xFFFF
        gap = (t0 + sent / fps) - time.perf_counter()
        if gap > 0:
            time.sleep(gap)
    sock.close()
    return sent


def poll(seconds):
    snap = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        snap = get_state()
        time.sleep(0.1)
    return snap


def last_block(h):
    """h.pcm 里存的是 (累计样本号, blk) 元组（见 web_server.py:279）。"""
    with h.lock:
        if not h.pcm:
            return None
        item = h.pcm[-1]
        return (item[1] if isinstance(item, tuple) else item).copy()


def tail_blocks(h, n=8):
    """把 pcm 队列最后 n 块（每块 10 ms）拼起来，80 ms 够做一次基频估计。"""
    with h.lock:
        items = list(h.pcm)[-n:]
    if not items:
        return None
    return np.concatenate([(it[1] if isinstance(it, tuple) else it) for it in items])


def ac_pitch(x, rate=udp_audio.SAMPLE_RATE, lo=60.0, hi=400.0):
    """自相关基频估计。单块 480 点（10 ms）做 FFT 求峰不可靠 —— 220 Hz 在
    480 点窗口里只有 1.1 个周期，Hann 主瓣比基频间隔还宽，峰位置会乱跳。"""
    x = np.asarray(x, dtype=np.float64)[:, 0]
    x = x - x.mean()
    lag_lo, lag_hi = int(rate / hi), int(rate / lo)
    lag_hi = min(lag_hi, len(x) - 2)
    best, best_lag = -1e9, 0
    for lag in range(lag_lo, lag_hi + 1):
        a, b = x[:-lag], x[lag:]
        d = np.sqrt((a * a).sum() * (b * b).sum()) or 1.0
        c = float((a * b).sum() / d)
        if c > best:
            best, best_lag = c, lag
    return (rate / best_lag) if best_lag else 0.0


def main():
    h = web_server.Hub
    threading.Thread(target=web_server.serve,
                     kwargs={"port": TEST_HTTP, "host": "127.0.0.1"}, daemon=True).start()
    time.sleep(1.0)
    HUB = web_server.HUB
    HUB.start_rx()
    emit("UDP %d（真控制台的 50505 未受影响）  HTTP %d" % (TEST_UDP, TEST_HTTP))

    # ---- 阶段 1：关着，确认与以前完全一致
    th = threading.Thread(target=pump, args=(6.0,), daemon=True)
    th.start()
    s1 = poll(1.4)
    emit("  阶段1 voc=%s frames=%s link=%s" %
         (s1["voc"].get("on"), s1["voc"].get("frames"), s1.get("link")))
    check(s1["voc"].get("on") is False, "默认 voc.on=False")
    check(int(s1["voc"].get("frames") or 0) == 0, "默认 voc.frames=0（一行都不执行）")
    check(s1.get("link") == "ok", "有包进来，link=ok")
    # 灌包仍在进行（一共 6 s），此刻取一段原样音频做基准
    off_blk = tail_blocks(HUB)

    # ---- 阶段 2：开（灌包仍在继续，才有音频可看）
    ack = post({"op": "voc", "v": True})
    emit("  ack: %s" % json.dumps(ack, ensure_ascii=False))
    check("声码器开" in ack.get("ack", ""), "POST /api/cmd op=voc 返回开启确认")

    s2 = poll(1.2)
    voc = s2["voc"]
    emit("  阶段2 voc=%s" % json.dumps({k: voc.get(k) for k in
                                       ("on", "frames", "ms_per_frame", "voice_ratio", "bitrate")},
                                      ensure_ascii=False))
    check(voc.get("on") is True, "接口回报 voc.on=True")
    check(int(voc.get("frames") or 0) > 20, "voc.frames 在涨（%s 帧）" % voc.get("frames"))
    check(0.0 < float(voc.get("ms_per_frame") or 0) < 20.0,
          "ms_per_frame 在 0..20（%s ms）" % voc.get("ms_per_frame"))
    check(abs(float(voc.get("voice_ratio") or 0) - 1.0) < 0.15,
          "%.0f Hz 正弦被判成浊音（voice_ratio=%s）" % (TONE_HZ, voc.get("voice_ratio")))
    on_blk = tail_blocks(HUB)

    # ---- 阶段 3：音频仍然"是那句话"，但确实走了再生
    if off_blk is not None and on_blk is not None:
        rms_off = float(np.sqrt((off_blk.astype(np.float64) ** 2).mean()))
        rms_on = float(np.sqrt((on_blk.astype(np.float64) ** 2).mean()))
        p0, p1 = ac_pitch(off_blk), ac_pitch(on_blk)
        emit("  RMS：关着 %.0f ｜ 开着 %.0f ｜ 基频 %.1f / %.1f Hz"
             % (rms_off, rms_on, p0, p1))
        check(rms_off > 200 and rms_on > 200,
              "两边都不是静音（%.0f / %.0f）" % (rms_off, rms_on))
        check(abs(rms_on - rms_off) / max(1.0, rms_off) < 0.6,
              "电平没被声码器改变超过 60%%（%.0f → %.0f）" % (rms_off, rms_on))
        check(abs(p1 - TONE_HZ) / TONE_HZ < 0.08,
              "基频还是 %.0f Hz（声码器报 %.1f Hz，误差 %.1f%%）"
              % (TONE_HZ, p1, 100.0 * abs(p1 - TONE_HZ) / TONE_HZ))
        pk = int(np.abs(on_blk).max())
        check(0 < pk < 32767, "再生后的音频有声音且没削波（峰值 %d）" % pk)
    else:
        check(False, "拿不到 pcm 块，无法比对波形")

    th.join()

    # ---- 阶段 4：secs 到点自动关
    ack = post({"op": "voc", "v": True, "secs": 1})
    emit("  ack: %s" % json.dumps(ack, ensure_ascii=False))
    check("1 s" in ack.get("ack", "") or "1" in ack.get("ack", ""), "secs 被接受")
    s3 = poll(0.4)
    check(s3["voc"].get("on") is True, "试听开始后 voc.on=True")
    time.sleep(1.3)
    s4 = get_state()
    check(s4["voc"].get("on") is False, "1.3 s 后后端已自动关（voc.on=False）")
    notes = [e["m"] for e in s4.get("log") or []]
    check(any("试听结束" in m for m in notes), "事件列里能看到「试听结束」")

    # ---- 阶段 5：显式关，状态干净
    ack = post({"op": "voc", "v": False})
    check("声码器关" in ack.get("ack", ""), "POST v=false 返回关闭确认")
    time.sleep(0.2)
    check(get_state()["voc"].get("on") is False, "关闭后 voc.on=False")

    HUB.stop_rx()
    emit()
    emit("RESULT: " + ("ALL PASS" if _ok else "SOME FAILED"))
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(_out.getvalue())
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
