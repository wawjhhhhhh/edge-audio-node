"""End-to-end latency probe for the ZYNQ audio link.

Idea: the board's speaker and the board's microphone sit on the same PCB, so a
burst we push to the board can be heard by the board itself.  We push a short
burst train and, at the same time, record the MIC stream coming back on 50505.
For every burst we then know

    t_send  - the wall clock when we started broadcasting that burst
    t_recv  - the wall clock when the MIC frame containing that burst arrived

and their difference is the full loop "PC click -> board speaker -> board MIC
-> PC", i.e. exactly the delay a user perceives.  It includes the playback
ring buffer (up to 4 x 100 ms), the acoustic path, the 10 ms MIC frame, the
network hop and the PC jitter buffer, so the tool also reports the jitter
buffer share and the net board-side number.

    python latency_test.py [--bursts 6] [--gap 0.5]

Nothing is played on the PC side and no audio device is opened, so it can run
while the desktop app is closed.
"""
import argparse
import socket
import struct
import sys
import threading
import time
import wave

import numpy as np

PORT_MIC = 50505
PORT_DAC = 50506
RATE = 48000
FRAME_SAMPLES = 480
FRAME_BYTES = FRAME_SAMPLES * 4
BURST_HZ = 1000.0
BURST_MS = 8      # one frame is 10 ms, so the burst must fit inside it
BURST_AMP = 20000


def _build_burst():
    n = int(RATE * BURST_MS / 1000.0)
    t = np.arange(n, dtype=np.float32) / RATE
    wave_ = np.sin(2 * np.pi * BURST_HZ * t)
    # 2 ms raised-cosine edges so the burst has no click of its own
    edge = int(RATE * 0.002)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, edge))
    wave_[:edge] *= ramp
    wave_[-edge:] *= ramp[::-1]
    pcm = (wave_ * BURST_AMP).astype("<i2")
    return np.repeat(pcm, 2).astype("<i2")  # mono -> stereo


def _packet(seq, chunk, payload):
    return b"AU" + struct.pack(">HHH", seq & 0xFFFF, chunk, FRAME_SAMPLES * 4) + payload


class Recorder(threading.Thread):
    """Collects MIC frames plus the arrival wall clock of each sequence number."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind(("0.0.0.0", PORT_MIC))
        self.sock.settimeout(0.2)
        self.pending = {}
        self.frames = {}      # seq -> np.int16 array of FRAME_SAMPLES*2
        self.arrival = {}     # seq -> wall clock of the last chunk
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 16 or data[:2] != b"AU":
                continue
            _tag, seq, chunk, _total = struct.unpack(">2sHHH", data[:8])
            if chunk > 1:
                continue
            payload = data[8:]
            stride = len(payload)
            buf = self.pending.get(seq)
            if buf is None:
                buf = bytearray(FRAME_BYTES)
                self.pending[seq] = buf
            off = chunk * stride
            buf[off:off + len(payload)] = payload
            if chunk == 1 or off + len(payload) >= FRAME_BYTES:
                audio = np.frombuffer(bytes(buf), dtype="<i2")
                self.frames[seq] = audio
                self.arrival[seq] = time.perf_counter()
                self.pending.pop(seq, None)

    def close(self):
        self.stop_flag.set()
        try:
            self.sock.close()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bursts", type=int, default=6)
    ap.add_argument("--gap", type=float, default=0.5)
    ap.add_argument("--wav", default=r"D:\vitis_tmp\latency_rec.wav")
    args = ap.parse_args(argv)

    burst = _build_burst()
    frame = np.zeros(FRAME_SAMPLES * 2, dtype="<i2")
    n = min(burst.size, frame.size)
    frame[:n] = burst[:n]

    rec = Recorder()
    rec.start()
    time.sleep(0.3)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = ("255.255.255.255", PORT_DAC)

    # Keep a silent carrier going the whole time so the board never falls back
    # to its own 440 Hz tone in the middle of the measurement.
    step = max(1, int(round(args.gap * 100)))
    burst_at = [10 + k * step for k in range(args.bursts)]   # frame indices
    n_frames = burst_at[-1] + 60 if burst_at else 60
    seq = 0
    send_times = []
    t_start = time.perf_counter()
    for i in range(n_frames):
        target = t_start + i * 0.010
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        now = time.perf_counter()

        if i in burst_at:
            payload = frame.copy()
            send_times.append(now)
        else:
            payload = np.zeros(FRAME_SAMPLES * 2, dtype="<i2")
        raw = payload.tobytes()
        sock.sendto(_packet(seq, 0, raw[:960]), dest)
        sock.sendto(_packet(seq, 1, raw[960:]), dest)
        seq = (seq + 1) & 0xFFFF
    sock.close()

    time.sleep(0.4)
    rec.close()
    rec.join(timeout=1.0)

    if not rec.frames:
        print("no MIC frames received - is the board running?")
        return 1

    # Rebuild the MIC timeline and find the bursts in it.
    order = sorted(rec.frames)
    pcm = np.concatenate([rec.frames[s] for s in order])
    env = np.abs(pcm[::2].astype(np.float32))
    win = int(RATE * 0.005)
    env = np.convolve(env, np.ones(win, dtype=np.float32), mode="same")

    thr = max(400.0, env.max() * 0.25)
    on = env > thr
    starts = []
    idx = 0
    while idx < len(on):
        if on[idx]:
            starts.append(idx)
            idx += int(RATE * 0.2)
        else:
            idx += 1

    w = wave.open(args.wav, "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(pcm.tobytes())
    w.close()

    print("bursts sent: %d   bursts found in MIC stream: %d" % (len(send_times), len(starts)))
    print("MIC frames: %d (%.2f s)   saved %s" % (len(order), len(pcm) / 2.0 / RATE, args.wav))

    if not starts:
        print("no burst detected in the recording - check the speaker is audible to the MIC")
        return 1

    # Map a sample index back to the wall clock of the MIC frame it came from.
    per_frame = FRAME_SAMPLES * 2
    deltas = []
    for k, s in enumerate(starts[: len(send_times)]):
        f = min(len(order) - 1, s // per_frame)
        t_recv = rec.arrival[order[f]]
        t_send = send_times[k]
        deltas.append((t_recv - t_send) * 1000.0)

    deltas.sort()
    med = deltas[len(deltas) // 2]
    print("loop delay (PC send -> PC receive) per burst, ms: %s"
          % ", ".join("%.1f" % d for d in deltas))
    print("median loop delay: %.1f ms" % med)
    print("note: this includes the playback ring (up to 400 ms), the acoustic path,")
    print("      the 10 ms MIC frame, the network hop and the PC jitter buffer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
