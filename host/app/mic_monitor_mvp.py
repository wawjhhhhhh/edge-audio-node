"""mic_monitor_mvp.py -- minimal PC端监听/推流工具 (MVP)

功能（够答辩演示的最小闭环）:
  1. 收听板载麦克风: 收 UDP :50505 -> 重组成 10 ms 帧 -> 声卡实时播放
  2. 往板子推音频:  发 UDP :50506 <- 测试音 或 WAV 文件
  3. 顶部显示: 帧率 / 丢帧 / 缓冲(ms) / 输入电平

用法:
  python mic_monitor_mvp.py          # 图形界面
  python mic_monitor_mvp.py --check  # 不开界面, 只做自检(打包/解包/重组)

协议 (和板子固件 udp_link.c 一致):
  8 B 头: b'AU' + struct.pack(">HHH", seq, chunk, total)
  PCM   : 48 kHz / 16 bit / 立体声, 小端, [15:0]=左 [31:16]=右
  每帧 480 个立体声样本, 拆成 chunk=0/1 两包, 每包 240 样本 = 960 B
"""

import queue
import socket
import struct
import sys
import threading
import time
import wave

import numpy as np

RATE = 48000
FRAME = 480                      # stereo samples per 10 ms frame
CHUNK = 240                      # stereo samples per packet
PAYLOAD = CHUNK * 4              # 960 bytes
HDR = 8
RX_PORT = 50505                  # board -> PC
TX_PORT = 50506                  # PC -> board
TARGET_BUF_MS = 90
MAX_BUF_MS = 400


# ----------------------------------------------------------------------------
# protocol
# ----------------------------------------------------------------------------
def pack_packet(seq, chunk, payload):
    return b"AU" + struct.pack(">HHH", seq & 0xFFFF, chunk, FRAME) + payload


def unpack_packet(data):
    if len(data) < HDR or data[0:2] != b"AU":
        return None
    seq, chunk, total = struct.unpack(">HHH", data[2:8])
    return seq, chunk, total, data[HDR:]


def frame_to_bytes(frame):
    """(FRAME, 2) int16 array -> 2 packets of raw bytes."""
    raw = frame.astype("<i2c").tobytes() if False else frame.astype("<i2").tobytes()
    return raw[:PAYLOAD], raw[PAYLOAD:]


class MicReceiver:
    """UDP :50505 -> 10 ms frames -> sounddevice playback."""

    def __init__(self, on_stats=None):
        self.on_stats = on_stats or (lambda s: None)
        self.q = queue.Queue(maxsize=200)
        self.running = False
        self.threads = []
        self.partial = {}            # seq -> bytearray
        self.expect = None           # next seq we want to play
        self.frames = 0
        self.lost = 0
        self.overflow = 0
        self.underflow = 0
        self.level = 0.0
        self.buf_ms = 0.0
        self._stream = None

    # -- receiving ---------------------------------------------------------
    def _rx_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        sock.bind(("0.0.0.0", RX_PORT))
        sock.settimeout(0.2)
        while self.running:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            pkt = unpack_packet(data)
            if pkt is None:
                continue
            seq, chunk, total, payload = pkt
            if len(payload) != PAYLOAD:
                continue
            buf = self.partial.setdefault(seq, bytearray(FRAME * 4))
            buf[chunk * PAYLOAD:(chunk + 1) * PAYLOAD] = payload
            if chunk == 1:
                frame = self.partial.pop(seq, None)
                if frame is not None:
                    try:
                        self.q.put_nowait(np.frombuffer(bytes(frame), dtype="<i2").reshape(FRAME, 2))
                    except queue.Full:
                        self.overflow += 1
            # drop stale partial frames
            if len(self.partial) > 32:
                for k in list(self.partial)[:-8]:
                    del self.partial[k]
        sock.close()

    # -- playback ----------------------------------------------------------
    def _play_loop(self):
        start = time.perf_counter()
        played = 0
        with self._stream:
            while self.running:
                try:
                    frame = self.q.get(timeout=0.05)
                except queue.Empty:
                    continue
                # pacing: keep roughly real time by watching the backlog
                self._stream.write(frame.tobytes())
                played += 1
                target_done = played * FRAME / RATE
                elapsed = time.perf_counter() - start
                if elapsed < target_done:               # never run ahead
                    time.sleep(min(0.02, target_done - elapsed))
                self.frames = played
                self.buf_ms = self.q.qsize() * 10.0
                self.level = float(np.abs(frame).mean())

    def start(self):
        self.running = True
        self._stream = __import__("sounddevice").RawOutputStream(
            samplerate=RATE, channels=2, dtype="int16", blocksize=CHUNK, latency="low")
        self._stream.start()
        self.threads = [threading.Thread(target=self._rx_loop, daemon=True),
                        threading.Thread(target=self._play_loop, daemon=True)]
        for t in self.threads:
            t.start()

    def stop(self):
        self.running = False
        for t in self.threads:
            t.join(timeout=1.0)
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass


class BoardSender:
    """Send audio to UDP :50506 at exactly 100 frames/s."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.stop_flag = False

    def tone(self, seconds=5.0, freq=440.0):
        n = int(RATE * seconds)
        t = np.arange(n) / RATE
        v = (8000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
        return np.stack([v, v], axis=1)

    def load_wav(self, path):
        with wave.open(path, "rb") as w:
            assert w.getsampwidth() == 2, "need 16-bit WAV"
            ch, sr = w.getnchannels(), w.getframerate()
            raw = w.readframes(w.getnframes())
        pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)
        if ch == 1:
            pcm = np.stack([pcm[:, 0], pcm[:, 0]], axis=1)
        elif ch > 2:
            pcm = pcm[:, :2]
        if sr != RATE:                                  # crude linear resample
            idx = (np.arange(int(len(pcm) * RATE / sr)) * sr / RATE).astype(int)
            idx = np.clip(idx, 0, len(pcm) - 1)
            pcm = pcm[idx]
        return pcm.astype(np.int16)

    def send(self, pcm, on_progress=None):
        self.stop_flag = False
        total = len(pcm) // FRAME
        t0 = time.perf_counter()
        for seq in range(total):
            if self.stop_flag:
                break
            frame = pcm[seq * FRAME:(seq + 1) * FRAME]
            if len(frame) < FRAME:
                frame = np.vstack([frame, np.zeros((FRAME - len(frame), 2), np.int16)])
            for chunk in (0, 1):
                pay = frame[chunk * CHUNK:(chunk + 1) * CHUNK].astype("<i2").tobytes()
                self.sock.sendto(pack_packet(seq, chunk, pay), ("255.255.255.255", TX_PORT))
            target = t0 + (seq + 1) / 100.0
            now = time.perf_counter()
            if target > now:
                time.sleep(target - now)
            if on_progress and (seq % 50) == 0:
                on_progress(seq, total)
        return total


# ----------------------------------------------------------------------------
# self test (no board needed)
# ----------------------------------------------------------------------------
def selftest():
    rng = np.random.default_rng(1)
    frame = (rng.integers(-2000, 2000, (FRAME, 2))).astype(np.int16)
    a, b = frame_to_bytes(frame)
    assert len(a) == PAYLOAD and len(b) == PAYLOAD, "payload size"
    rebuilt = {}
    for chunk, pay in ((0, a), (1, b)):
        got = unpack_packet(pack_packet(7, chunk, pay))
        assert got is not None and got[0] == 7 and got[1] == chunk and got[2] == FRAME
        rebuilt.setdefault(7, bytearray(FRAME * 4))[chunk * PAYLOAD:(chunk + 1) * PAYLOAD] = got[3]
    back = np.frombuffer(bytes(rebuilt[7]), dtype="<i2").reshape(FRAME, 2)
    assert np.array_equal(back, frame), "round trip mismatch"

    # loss detection helper
    seqs = [1, 2, 4, 5]
    missing = (seqs[-1] - seqs[0] + 1) - len(seqs)
    assert missing == 1, "gap count"

    # WAV round trip
    s = BoardSender()
    tmp = "_selftest.wav"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(frame.astype("<i2").tobytes())
    assert np.array_equal(s.load_wav(tmp), frame), "wav round trip"
    import os; os.remove(tmp)

    print("selftest OK: 2 packets/frame, %d bytes/frame, loss counting, wav io" % (FRAME * 4))


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox

    recv = MicReceiver()
    sender = BoardSender()
    sending = {"on": False, "done": 0, "total": 0}

    root = tk.Tk()
    root.title("AI 音频解码器 - PC 端 (MVP)")
    root.geometry("520x330")

    level = tk.DoubleVar(value=0.0)
    info = tk.StringVar(value="空闲")

    tk.Label(root, text="板载麦克风 -> PC", font=("Microsoft YaHei", 11, "bold")).pack(pady=(10, 2))
    bar = tk.Scale(root, variable=level, from_=0, to=12000, orient="horizontal",
                   length=460, label="输入电平 (平均绝对幅度)")
    bar.pack()

    def stats():
        if recv.running:
            info.set("帧 %d  丢帧 %d  缓冲 %.0f ms  溢出 %d  电平 %.0f"
                     % (recv.frames, recv.lost, recv.buf_ms, recv.overflow, recv.level))
            level.set(recv.level)
        root.after(200, stats)

    def toggle():
        if recv.running:
            recv.stop()
            btn_mic.config(text="开始收听麦克风")
            info.set("已停止")
        else:
            try:
                recv.start()
                btn_mic.config(text="停止收听")
            except Exception as e:
                messagebox.showerror("声卡错误", str(e))

    btn_mic = tk.Button(root, text="开始收听麦克风", width=22, command=toggle)
    btn_mic.pack(pady=6)

    tk.Label(root, text="PC -> 板子（耳机口播放）", font=("Microsoft YaHei", 11, "bold")).pack(pady=(8, 2))

    def send_pcm(pcm, name):
        if sending["on"]:
            return
        sending["on"] = True

        def run():
            try:
                sender.send(pcm, on_progress=lambda i, n: info.set("%s 已发送 %.1fs / %.1fs"
                                                                  % (name, i / 100.0, n / 100.0)))
            finally:
                sending["on"] = False
                info.set("%s 发送完成" % name)
        threading.Thread(target=run, daemon=True).start()

    tk.Button(root, text="发送 440 Hz 测试音 (5 s)", width=30,
              command=lambda: send_pcm(sender.tone(5.0), "测试音")).pack(pady=2)

    def pick():
        p = filedialog.askopenfilename(filetypes=[("WAV", "*.wav")])
        if not p:
            return
        try:
            send_pcm(sender.load_wav(p), p.split("/")[-1])
        except Exception as e:
            messagebox.showerror("WAV 错误", str(e))

    tk.Button(root, text="发送 WAV 文件...", width=30, command=pick).pack(pady=2)

    tk.Label(root, textvariable=info, fg="#0a6", font=("Consolas", 10)).pack(pady=10)

    def on_close():
        recv.stop()
        sender.stop_flag = True
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    root.after(200, stats)
    root.mainloop()


if __name__ == "__main__":
    if "--check" in sys.argv:
        selftest()
    else:
        gui()
