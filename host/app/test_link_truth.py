"""链路真值回归测试：证伪旧 fps 字段，证实新的 link / rx_fps / under_rate。

背景（材料库/01-开发日志/2026-10-30_接手自检_板子断电与控制台假fps.md）：
板子断电 12 分钟，/api/state 仍报 fps=100.0008、lost=0，因为 fps 来自
WebServer._pull_loop 的自增计数，而 JitterBuffer.pull() 无数据也返回静音块。

本测试不碰 50505（本机控制台正占着），改用 50599，只动内存里的对象。
采样方式与浏览器一致（每 0.1 s 一次 snapshot），因为 under_rate 需要滑窗样本。
  阶段 1 静默 2.5 s  -> link 必须 offline、fps 必须 0.0（旧代码这里是 100.0）
  阶段 2 灌 3 s 真包  -> link 必须 ok、rx_fps ≈ 100、rx_frames 在涨
  阶段 3 断流 1.6 s   -> link 必须回落 offline、fps 必须回 0.0（不许留"余温"）
"""
import io
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import udp_audio
import web_server

TEST_PORT = 50599
LOG_PATH = os.path.join(tempfile.gettempdir(), "link_truth_result.txt")
_orig_receiver = udp_audio.MicReceiver
_out = io.StringIO()
_ok = True


def emit(line=""):
    _out.write(line + "\n")
    print(line.encode("utf-8", "replace").decode("utf-8", "replace"))


def _receiver_on_test_port(**kw):
    kw.pop("port", None)
    return _orig_receiver(port=TEST_PORT, **kw)


web_server.MicReceiver = _receiver_on_test_port

FIELDS = ("fps", "link", "rx_age", "rx_fps", "rx_frames", "packets", "under", "under_rate")


def facts(snap, name):
    v = snap.get(name)
    return "None" if v is None else ("%.3f" % v if isinstance(v, float) else str(v))


def show(tag, snap):
    emit("%-18s %s" % (tag, {k: snap.get(k) for k in FIELDS}))


def poll(hub, seconds, note=""):
    """像浏览器一样按 10 Hz 采样，返回最后一次快照。"""
    t0 = time.perf_counter()
    snap = None
    while time.perf_counter() - t0 < seconds:
        snap = hub.snapshot()
        time.sleep(0.1)
    return snap


def check(cond, what):
    global _ok
    emit(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        _ok = False


def pump(seconds, fps=100, state=None):
    """按真实 AU 协议往 TEST_PORT 灌音频：每帧 2 包 × 960 B，头 8 字节。"""
    import numpy as np
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame = np.zeros((udp_audio.FRAME_SAMPLES, udp_audio.CHANNELS), dtype=np.int16)
    frame[:, 0] = 1200                       # 非零，便于 dbfs 有反应
    raw = udp_audio.frame_to_bytes(frame)
    half = len(raw) // 2
    t0 = time.perf_counter()
    seq = 0
    sent = 0
    while time.perf_counter() - t0 < seconds:
        for chunk, body in ((0, raw[:half]), (1, raw[half:])):
            sock.sendto(udp_audio.pack_packet(body, seq, chunk), ("127.0.0.1", TEST_PORT))
        sent += 1
        seq = (seq + 1) & 0xFFFF
        gap = (t0 + sent / fps) - time.perf_counter()
        if gap > 0:
            time.sleep(gap)
    sock.close()
    if state is not None:
        state["sent"] = sent
    return sent


def main():
    h = web_server.Hub()
    h.start_rx()
    emit("测试端口: %d（本机控制台占的 50505 未受影响）" % h.rx.port)

    # ---- 阶段 1：静默
    s1 = poll(h, 2.5)
    show("阶段1 静默 2.5s", s1)
    check(s1["link"] == "offline", "静默时 link=offline（旧代码无从判断）")
    check(s1["fps"] == 0.0, "静默时 fps=0.0（旧代码恒报 100.0）")
    check(s1["rx_frames"] == 0, "静默时 rx_frames=0")
    check(s1["packets"] == 0, "静默时 packets=0")
    check(s1["under_rate"] is not None and s1["under_rate"] > 50,
          "静默时 under_rate>50/s（pull 循环 100/s 全成欠载，实测 %s）"
          % facts(s1, "under_rate"))

    # ---- 阶段 2：真包（先起灌包线程，再采样；采样窗 2.6 s < 灌包 3.5 s）
    th = threading.Thread(target=pump, args=(3.5, 100), daemon=True)
    th.start()
    s2 = poll(h, 2.6)
    show("阶段2 灌包 2.6s", s2)
    check(s2["link"] == "ok", "有包到达时 link=ok")
    check(s2["rx_fps"] is not None and 95 <= s2["rx_fps"] <= 105,
          "rx_fps 落在 95..105（实际 %s）" % facts(s2, "rx_fps"))
    check(s2["fps"] == s2["rx_fps"], "fps 字段 = rx_fps，不再自增")
    check((s2["rx_frames"] or 0) >= 200, "rx_frames>=200（收到 %s 帧）" % facts(s2, "rx_frames"))
    check(s2["under_rate"] is not None and s2["under_rate"] < 10,
          "灌包后 under_rate 掉到 <10/s（实际 %s）" % facts(s2, "under_rate"))
    th.join()                                 # 必须等灌包彻底结束，否则阶段 3 会混入残留包

    # ---- 阶段 3：断流
    n_before = h.snapshot()["rx_frames"]
    s3 = poll(h, 1.6)
    show("阶段3 断流 1.6s", s3)
    check(s3["link"] == "offline", "断流 1.6 s 后 link 回落 offline")
    check(s3["fps"] == 0.0, "断流后 fps=0.0（旧代码余温报 100.0）")
    check(s3["rx_frames"] == n_before, "断流后 rx_frames 不再增长（%s）" % facts(s3, "rx_frames"))
    check(s3["rx_age"] is not None and s3["rx_age"] > 1.0,
          "断流后 rx_age>1.0 s（实际 %s）" % facts(s3, "rx_age"))

    # ---- 阶段 4：停止收听
    h.stop_rx()
    s4 = h.snapshot()
    show("阶段4 停止收听", s4)
    check(s4["link"] == "offline" and s4["fps"] is None, "停止后 listening=False、fps=None")

    emit()
    emit("RESULT: " + ("ALL PASS" if _ok else "SOME FAILED"))
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(_out.getvalue())
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())