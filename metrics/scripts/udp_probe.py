"""Ground-truth probe over the network, independent of the PC console UI state.

Sends the control words by UDP broadcast (169.254.255.255:50507), listens for
the mic stream (50505) and the board status broadcast (50508), and prints
packet rate + rms/floor numbers computed locally, so a console stuck in
simulation mode cannot fake the result.

floor_db here = 10*log10(mean power of the quietest 20 % of FFT bins), which is
the same idea as the console's spectrum floor: it follows the broadband noise
floor and ignores the tone/voice bins.
"""
import argparse
import json
import math
import select
import socket
import struct
import time

PORT_MIC = 50505
PORT_STAT = 50508
CTRL_ADDR = ("169.254.255.255", 50507)

MODE_BYTE = {"bypass": b"0", "rnnoise": b"1", "gate": b"2", "pl": b"3"}
NOISE_BYTE = {0: b"a", 300: b"b", 800: b"c", 1500: b"d"}


def ctrl(b):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(b, CTRL_ADDR)
    s.close()


def fft_pow(x):
    """Iterative radix-2 FFT -> power spectrum (list of float)."""
    n = len(x)
    j = 0
    a = [complex(v, 0.0) for v in x]
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    ln = 2
    while ln <= n:
        ang = -2.0 * math.pi / ln
        wl = complex(math.cos(ang), math.sin(ang))
        for i in range(0, n, ln):
            w = 1 + 0j
            for k in range(i, i + ln // 2):
                u = a[k]
                v = a[k + ln // 2] * w
                a[k] = u + v
                a[k + ln // 2] = u - v
                w *= wl
        ln <<= 1
    return [abs(v) ** 2 for v in a[: n // 2]]


def floor_db_of(frames):
    """Median of per-chunk spectral floor (quietest 20 % of bins).

    A chunk is 480 stereo samples (960 B), so the FFT is 256 points per
    channel: enough to see the broadband floor between 0 and 24 kHz.
    """
    if not frames:
        return -140.0, -140.0
    rms_acc = 0.0
    floors = []
    for fr in frames:
        n = len(fr)
        rms_acc += sum(v * v for v in fr) / n
        seg = fr[0::2][:256]          # left channel of this chunk
        if len(seg) < 256:
            continue
        m = len(seg)
        w = [seg[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (m - 1.0))) for i in range(m)]
        p = sorted(fft_pow(w)[1:])
        k = max(1, len(p) // 5)
        q = sum(p[:k]) / k
        floors.append(10.0 * math.log10(q / (m * m) + 1e-24))
    rms = math.sqrt(rms_acc / len(frames)) / 32768.0
    rms_db = 20.0 * math.log10(rms + 1e-12)
    floors.sort()
    fl = floors[len(floors) // 2] if floors else -140.0
    return rms_db, fl


def measure(secs):
    m = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    m.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        m.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    m.bind(("0.0.0.0", PORT_MIC))
    m.settimeout(0.3)
    st = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    st.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    st.bind(("0.0.0.0", PORT_STAT))
    st.settimeout(0.05)
    t0 = time.time()
    frames = []
    nbytes = 0
    npkt = 0
    nstat = 0
    seqs = set()
    chunks = set()
    last_stat = None
    while time.time() - t0 < secs:
        # Wait on BOTH sockets at once.  Reading them one after the other with a
        # blocking timeout caps the mic rate at 1/timeout -- that bug made a
        # healthy 100 fps stream look like 10 fps.
        rd, _, _ = select.select([m, st], [], [], 0.2)
        for sock in rd:
            if sock is st:
                try:
                    s, _ = st.recvfrom(2048)
                except socket.timeout:
                    continue
                last_stat = s
                nstat += 1
                continue
            try:
                d, _ = m.recvfrom(4096)
            except socket.timeout:
                continue
            npkt += 1
            nbytes += len(d)
            if len(d) < 8 or d[0:1] != b"A" or d[1:2] != b"U":
                continue
            seq = (d[2] << 8) | d[3]
            ch = (d[4] << 8) | d[5]
            seqs.add(seq)
            chunks.add((seq, ch))
            body = d[8:]
            ns = len(body) // 2
            if ns >= 32:
                frames.append(list(struct.unpack("<%dh" % ns, body[: ns * 2])))
    m.close()
    st.close()
    el = time.time() - t0
    rms_db, fl = floor_db_of(frames)
    seq_lo = min(seqs) if seqs else 0
    seq_hi = max(seqs) if seqs else 0
    # frames per second according to the board's own sequence numbers: immune
    # to a lossy local socket, which matters because the PC console may also be
    # bound to 50505.
    fps_seq = ((seq_hi - seq_lo + 1) / el) if el and seqs else 0.0
    return {
        "elapsed_s": round(el, 2),
        "packets": npkt,
        "fps_rx": round(npkt / el, 1) if el else 0.0,
        "frames_unique": len(seqs),
        "chunks": len(chunks),
        "seq_lo": seq_lo,
        "seq_hi": seq_hi,
        "fps_seq": round(fps_seq, 1),
        "bytes": nbytes,
        "stat_pkts": nstat,
        "frames_parsed": len(frames),
        "rms_dbfs": round(rms_db, 2),
        "floor_db": round(fl, 2),
        "stat_hex": last_stat.hex() if last_stat else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(MODE_BYTE), default=None)
    ap.add_argument("--noise", type=int, choices=[0, 300, 800, 1500], default=None)
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--secs", type=float, default=4.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    if a.mode:
        ctrl(MODE_BYTE[a.mode])
        time.sleep(0.15)
    if a.noise is not None:
        ctrl(NOISE_BYTE[a.noise])
    time.sleep(a.settle)
    r = measure(a.secs)
    r["mode"] = a.mode
    r["noise"] = a.noise
    print(json.dumps(r, ensure_ascii=False))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
