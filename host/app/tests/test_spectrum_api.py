"""频谱仪接进控制台的端到端验收（不碰 8770 / 50505）。

证明四件事：
  1. 没有音频时 /api/spectrum 是 `ready=False`，界面读数是 `—`（不报错）
  2. 灌 1 kHz 单音后：`peak_hz ≈ 1000`、`snr_db` 高、`thd_pct` 小、
     `band_mid` 占大头（能量落在 300–3k）
  3. 灌白噪声后：没有突出单音（`thd_pct == 0`、`snr_db` 低）、
     `rms_dbfs` 与 -26 dBFS 量级相符
  4. 关掉收听后再查：`ready` 回到 False（不会拿着上一轮的谱糊弄人）

用 50596 收 UDP、8783 起 HTTP，都不与真正在跑的控制台（50505 / 8770）冲突。
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

TEST_UDP = 50596
TEST_HTTP = 8783
LOG_PATH = os.path.join(tempfile.gettempdir(), "spectrum_api_result.txt")

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


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (TEST_HTTP, path), timeout=5) as r:
        return json.loads(r.read())


def spectrum(seconds=1.0):
    """取最后一份快照（等它至少算过几帧）。"""
    snap = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        snap = get("/api/spectrum")
        time.sleep(0.1)
    return snap


def pump_and_watch(blocks, seconds, fps=100):
    """一边灌包一边取谱。

    为什么必须同时进行：pull 循环按 100 fps 永远在跑，灌包一停它就开始拿
    静音块补（`JitterBuffer.pull()` 的隐藏逻辑），几秒后快照就变成 -140 dBFS 的
    静音谱 —— 只在灌完之后取样，量到的是"没有声音"，不是"这声音什么样"。
    判据取这期间"最强的那一份"（信噪比最大）。
    """
    out = {}
    stop = threading.Event()

    def worker():
        pump(blocks, fps=fps)

    def watcher():
        # 取"灌包稳定之后"的那一份：rms 高于 -60 dBFS 说明缓冲区里已经是真信号
        # （刚启动时环形缓冲里还混着上一轮的静音，中位数被拉到 -140，
        #  snr 会虚高到 99.9 —— 白噪声那一项就是被这个坑过一次）。
        last_signal = None
        best = None
        while not stop.is_set():
            try:
                s = get("/api/spectrum")
            except Exception:
                time.sleep(0.05)
                continue
            if s.get("ready"):
                if (s.get("rms_dbfs") or -140.0) > -60.0:
                    last_signal = s
                if best is None or (s.get("snr_db") or 0) > (best.get("snr_db") or 0):
                    best = s
            time.sleep(0.05)
        out["snap"] = last_signal or best

    tw = threading.Thread(target=watcher, daemon=True)
    tw.start()
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=seconds + 5)
    stop.set()
    tw.join(timeout=3)
    return out.get("snap") or {}


def frames_for(kind, seconds, tone_hz=1000.0, amp=0.25, seed=7):
    """按 10 ms 一块造 `(480, 2) int16` 帧。"""
    n = udp_audio.FRAME_SAMPLES
    total = int(udp_audio.SAMPLE_RATE * seconds)
    blocks = []
    rng = np.random.default_rng(seed)
    phase = 0
    while len(blocks) * n < total:
        idx = np.arange(n) + phase
        phase += n
        t = idx / udp_audio.SAMPLE_RATE
        if kind == "tone":
            s = amp * 32767 * np.sin(2 * np.pi * tone_hz * t)
        else:
            s = rng.normal(0, amp * 32767, n)
        v = np.clip(s, -32768, 32767).astype(np.int16)
        f = np.zeros((n, udp_audio.CHANNELS), dtype=np.int16)
        f[:, 0] = v
        f[:, 1] = v
        blocks.append(f)
    return blocks


def pump(blocks, fps=100):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    t0 = time.perf_counter()
    for i, frame in enumerate(blocks):
        raw = udp_audio.frame_to_bytes(frame)
        half = len(raw) // 2
        for chunk, body in ((0, raw[:half]), (1, raw[half:])):
            sock.sendto(udp_audio.pack_packet(body, seq, chunk), ("127.0.0.1", TEST_UDP))
        seq = (seq + 1) & 0xFFFF
        gap = (t0 + (i + 1) / fps) - time.perf_counter()
        if gap > 0:
            time.sleep(gap)
    sock.close()
    return len(blocks)


def main():
    emit("== 频谱仪 API 端到端 ==")
    threading.Thread(target=web_server.serve,
                     kwargs={"port": TEST_HTTP, "host": "127.0.0.1"},
                     daemon=True).start()
    time.sleep(0.6)

    # ---- 阶段 1：没收听 ----
    s0 = get("/api/spectrum")
    emit("阶段1 未收听: ready=%s bins=%d" % (s0.get("ready"), len(s0.get("bins") or [])))
    check(s0.get("ready") is False, "未收听时 ready=False（不拿旧谱糊弄）")
    check(not s0.get("bins"), "未收听时 bins 为空")

    # ---- 阶段 2：1 kHz 单音 ----
    post({"op": "listen", "v": True})
    time.sleep(0.3)
    s1 = pump_and_watch(frames_for("tone", 1.6), 1.6)
    emit("阶段2 单音: %s" % {k: s1.get(k) for k in
                          ("ready", "peak_hz", "rms_dbfs", "snr_db",
                           "thd_pct", "band_low", "band_mid", "band_high")})
    check(s1.get("ready") is True, "灌包后 ready=True")
    check(abs((s1.get("peak_hz") or 0) - 1000.0) < 25.0, "峰值频率 ≈ 1000 Hz")
    check((s1.get("snr_db") or 0) > 40.0, "单音信噪比 > 40 dB")
    thd1 = s1.get("thd_pct")
    check(thd1 is not None and thd1 < 2.0, "单音 THD < 2%")
    check((s1.get("band_mid") or 0) > 90.0, "能量 90% 以上落在中频（300–3k）")
    # 幅度 0.25 满幅的正弦，RMS = 0.25/√2 → 20log10(0.1768) = -15.0 dBFS
    check(abs((s1.get("rms_dbfs") or 0) - (-15.0)) < 3.0, "电平 ≈ -15 dBFS（0.25 满幅正弦）")
    check(len(s1.get("bins") or []) == 72, "72 条对数频带")
    check(len(s1.get("waterfall") or []) > 10, "瀑布已积累多行")

    # ---- 阶段 3：白噪声 ----
    s2 = pump_and_watch(frames_for("noise", 1.6), 1.6)
    emit("阶段3 白噪声: %s" % {k: s2.get(k) for k in
                           ("ready", "rms_dbfs", "snr_db", "thd_pct")})
    check(s2.get("snr_db") is not None and s2["snr_db"] < 25.0, "白噪声没有突出单音（snr < 25 dB）")
    check(s2.get("thd_pct") == 0.0, "非单音不报 THD（0，而不是乱算一个）")
    # 0.25σ 高斯白噪声的 RMS 就是 0.25 → -12.0 dBFS
    check(abs((s2.get("rms_dbfs") or 0) - (-12.0)) < 4.0, "噪声电平与 0.25σ 相符")

    # ---- 阶段 4：停止收听 ----
    post({"op": "listen", "v": False})
    time.sleep(0.5)
    s3 = get("/api/spectrum")
    state = get("/api/state")
    emit("阶段4 停止: listening=%s ready=%s" % (state.get("listening"), s3.get("ready")))
    check(state.get("listening") is False, "收听已关")
    check(s3.get("ready") is False, "停止收听会清空频谱（start_rx 里 spec.reset()）")

    emit()
    emit("RESULT: " + ("ALL PASS" if _ok else "FAIL"))
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(_out.getvalue())
    emit("结果已写入 %s" % LOG_PATH)
    return 0 if _ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
