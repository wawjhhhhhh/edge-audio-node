"""把电脑正在播放的声音实时送给 Zynq 板子（WASAPI 环回）。

链路：默认可听设备 [Loopback] → 48 kHz 立体声 int16 → playfx 播放链 → UDP 169.254.255.255:50506

用法：
    python loopback.py --secs 15            # 推 15 秒
    python loopback.py --secs 15 --fx rock  # 带音效
    python loopback.py --secs 15 --board-only   # 同时把电脑端点静音（只让板子响）

注意：环回抓的是"混音器之后、端点音量之前"，所以端点静音不影响抓取；
      但如果把播放器自己的音量拉到 0，就会抓到静音。
"""
import argparse
import socket
import struct
import sys
import threading
import time

import numpy as np

BOARD_HOST = "169.254.255.255"
PORT_IN = 50506
RATE = 48000
BLOCK = 480          # 10 ms
CHUNK = BLOCK * 2 * 2  # 480 帧 × 2 声道 × int16 = 1920 字节
_HDR = struct.Struct(">2sHHH")   # 与 udp_audio.py 的包格式一致：b"AU" + seq + chunk + total


def _endpoint_mute(mute):
    """静音/恢复默认输出端点（pycaw）。返回 True 表示已生效。"""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as e:
        print("[mute] pycaw 不可用：%s" % e)
        return False
    try:
        dev = AudioUtilities.GetSpeakers()
        vol = dev.EndpointVolume if hasattr(dev, "EndpointVolume") else dev.Activate
        # 兼容不同 pycaw 版本
        if hasattr(dev, "EndpointVolume"):
            v = dev.EndpointVolume
        else:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume
            v = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                     POINTER(IAudioEndpointVolume))
        if mute:
            v.SetMute(1, None)
        else:
            v.SetMute(0, None)
        return True
    except Exception as e:
        print("[mute] 失败：%s" % e)
        return False


class LoopbackPusher:
    def __init__(self, host=BOARD_HOST, port=PORT_IN, fx=None, verbose=True, fx_provider=None):
        self.host, self.port, self.fx, self.verbose = host, port, fx, verbose
        self.fx_provider = fx_provider   # callable：每块现取，支持运行中开关/换预设
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sent = 0
        self.blocks = 0
        self.drops = 0
        self.seq = 0
        self.fx_gain_db = 0.0
        self.fx_on_last = False
        self.rms = 0.0
        self._stop = threading.Event()
        self._thr = None

    # ---- 采集 ----
    def _open(self):
        import pyaudiowpatch as pa
        self.pa = pa.PyAudio()
        spk = self.pa.get_default_wasapi_loopback()
        self.device_name = spk["name"]
        self.rate = int(spk["defaultSampleRate"])
        self.ch = min(2, spk["maxInputChannels"])
        self.stream = self.pa.open(format=pa.paInt16, channels=self.ch,
                                   rate=self.rate, input=True,
                                   input_device_index=spk["index"],
                                   frames_per_buffer=1024)
        return spk

    def _loop(self):
        carry = np.zeros((0, 2), dtype=np.int16)
        t0 = time.time()
        next_t = time.perf_counter()
        while not self._stop.is_set():
            raw = self.stream.read(1024, exception_on_overflow=False)
            a = np.frombuffer(raw, dtype="<i2").reshape(-1, self.ch)
            if self.ch == 1:
                a = np.repeat(a, 2, axis=1)
            if self.rate != RATE:                      # 非 48k 才重采样
                n = int(round(a.shape[0] * RATE / self.rate))
                idx = np.linspace(0, a.shape[0] - 1, n)
                a = np.stack([np.interp(idx, np.arange(a.shape[0]), a[:, c]) for c in range(2)], axis=1)
                a = a.astype(np.int16)
            self.rms = float(np.sqrt((a.astype(np.float64) ** 2).mean()))
            buf = np.concatenate([carry, a], axis=0)
            i = 0
            while i + BLOCK <= buf.shape[0]:
                blk = buf[i:i + BLOCK]
                i += BLOCK
                fx = self.fx_provider() if self.fx_provider is not None else self.fx
                self.fx_on_last = fx is not None
                if fx is not None:
                    try:
                        before = float(np.sqrt((blk.astype(np.float64) ** 2).mean()))
                        blk = fx.process(blk)
                        after = float(np.sqrt((blk.astype(np.float64) ** 2).mean()))
                        if before > 1.0:
                            g = 20.0 * float(np.log10(max(after, 1e-6) / before))
                            self.fx_gain_db = 0.9 * self.fx_gain_db + 0.1 * g
                    except Exception as e:
                        if self.verbose:
                            print("[fx] %s" % e)
                try:
                    # 板子只认 "AU" 开头的包：每 10 ms 拆成 2 包 × 960 字节，
                    # 头 8 字节 = MAGIC(2) + seq(2) + chunk(2) + total(2)，全大端。
                    raw = blk.astype("<i2").tobytes()
                    for c in (0, 1):
                        part = raw[c * 960:(c + 1) * 960]
                        hdr = _HDR.pack(b"AU", self.seq & 0xFFFF, c, 1920)
                        self.sock.sendto(hdr + part, (self.host, self.port))
                    self.seq += 1
                    self.sent += CHUNK
                    self.blocks += 1
                except OSError:
                    self.drops += 1
                # 用真实时间打拍子，避免把 100 ms 的突发塞给板子
                next_t += BLOCK / RATE
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()
            carry = buf[i:]
            if time.time() - t0 > 3600:
                break

    def start(self):
        self._open()
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=2)
        try:
            self.stream.stop_stream()
            self.stream.close()
            self.pa.terminate()
        except Exception:
            pass

    def stats(self):
        return {"device": getattr(self, "device_name", "?"), "rate": getattr(self, "rate", 0),
                "blocks": self.blocks, "sent": self.sent, "drops": self.drops,
                "rms": round(self.rms, 1), "fx_on": bool(self.fx_on_last),
                "fx_gain_db": round(self.fx_gain_db, 2)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=15)
    ap.add_argument("--fx", default=None, help="预设名（rock/vocal/...），不给则纯直通")
    ap.add_argument("--board-only", action="store_true", help="同时静音电脑端点")
    args = ap.parse_args(argv)

    fx = None
    if args.fx:
        sys.path.insert(0, ".")
        from playfx import PlayFX
        fx = PlayFX()
        fx.set_preset(args.fx)

    muted = False
    if args.board_only:
        muted = _endpoint_mute(True)
        print("[mute] 端点静音：%s" % ("已生效" if muted else "失败（不影响推流）"))

    p = LoopbackPusher(fx=fx).start()
    print("[loop] 抓 %s @ %d Hz" % (p.device_name, p.rate))
    t0 = time.time()
    try:
        while time.time() - t0 < args.secs:
            time.sleep(1.0)
            s = p.stats()
            print("[loop] %4.1fs blocks=%d sent=%d drops=%d rms=%.1f"
                  % (time.time() - t0, s["blocks"], s["sent"], s["drops"], s["rms"]))
    finally:
        p.stop()
        if muted:
            _endpoint_mute(False)
            print("[mute] 端点已恢复")
    print("[loop] DONE", p.stats())


if __name__ == "__main__":
    main()
