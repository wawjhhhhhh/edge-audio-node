"""把 PCM 从 PC 推到 ESP32 播放，并把它做成板子上的常驻服务。

两条路：
- `main.py`（板子侧）：上电后起 TCP 服务（默认 9200），收到 PCM 就写 I2S，播完回去等下一次连接。
- `play_pcm()`（PC 侧）：连上去、推完、关闭。

码率与格式固定为 **16 kHz / 16-bit / 单声道**：这套参数实测 15.3 kSa/s 播放（目标 16 k），
且 32 KB/s 远低于热点实测能力；换 44.1k 立体声前先确认扬声器与 I2S 缓冲。
"""

from __future__ import annotations

import array
import os
import socket
import sys
import time
import wave
from pathlib import Path

BOARD_PORT = 9200
RATE = 16000
# 手机热点 8848 上板子的地址（boot.py 连的就是 8848）。
# 热点每次分到的网段/DHCP 可能变，所以允许覆盖：环境变量 > --board-ip > 这里。
# 真值来源：串口里 boot.py 打印的那行 `boot wifi OK <ip>`。
BOARD_HOST = os.environ.get("XIAOZHI_BOARD_HOST", "172.24.23.228")


def pcm_from_wav(src: str | Path) -> bytes:
    """WAV → 16 kHz/16-bit/单声道裸 PCM（线性重采样，够用且无依赖）。"""
    with wave.open(str(src), "rb") as w:
        nch = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"只支持 16-bit PCM WAV，实际 {width * 8}-bit")
    samples = array.array("h", raw)
    if nch == 2:
        samples = array.array("h", [(samples[i] + samples[i + 1]) // 2
                                    for i in range(0, len(samples) - 1, 2)])
    if rate != RATE:
        n_out = int(len(samples) * RATE / rate)
        out = array.array("h", [0] * n_out)
        for i in range(n_out):
            pos = i * rate / RATE
            i0 = int(pos)
            frac = pos - i0
            s0 = samples[i0] if i0 < len(samples) else 0
            s1 = samples[i0 + 1] if i0 + 1 < len(samples) else s0
            out[i] = int(s0 + (s1 - s0) * frac)
        samples = out
    return samples.tobytes()


def play_pcm(data: bytes, host: str, port: int = BOARD_PORT,
             chunk: int = 4096, timeout: float = 15.0) -> dict:
    """推一段 PCM 给板子播放；返回统计数据（用于日志/自检）。"""
    t0 = time.time()
    s = socket.create_connection((host, port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sent = 0
    while sent < len(data):
        n = s.send(data[sent:sent + chunk])
        if n == 0:
            time.sleep(0.005)
            continue
        sent += n
    s.close()
    el = max(time.time() - t0, 1e-6)
    return {
        "bytes": sent,
        "seconds_audio": sent / 2 / RATE,
        "seconds_wall": round(el, 2),
        "kb_s": round(sent / el / 1024, 1),
    }


def play_file(path: str | Path, host: str, port: int = BOARD_PORT) -> dict:
    return play_pcm(pcm_from_wav(path), host, port)


def reachable(host: str, port: int = BOARD_PORT, timeout: float = 0.6) -> bool:
    """板子的 9200 通不通（连上立刻关掉，不写数据，不会触发播放）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_ipv4() -> str | None:
    """本机出网那块网卡的地址（UDP connect 不发包，连不到也不报错）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def discover(host: str | None = None, port: int = BOARD_PORT,
             timeout: float = 0.3, workers: int = 64) -> list[str]:
    """找板子：先试已知地址，再在本机 /24 网段里扫一遍 9200。

    热点每次分的 IP 可能不一样（今天就从 10.55.67.228 变成了 172.24.23.228），
    把地址写死就会莫名其妙“只有文字没声音”。热点上只有板子开 9200，扫一遍最省事。
    """
    if reachable(host or BOARD_HOST, port, timeout):
        return [host or BOARD_HOST]
    me = local_ipv4()
    if not me:
        return []
    prefix = me.rsplit(".", 1)[0]
    addrs = ["%s.%d" % (prefix, i) for i in range(1, 255) if "%s.%d" % (prefix, i) != me]
    from concurrent.futures import ThreadPoolExecutor

    found = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for addr, ok in zip(addrs, ex.map(lambda a: reachable(a, port, timeout), addrs)):
            if ok:
                found.append(addr)
    return found


def speak(text: str, host: str = BOARD_HOST, port: int = BOARD_PORT) -> dict:
    """让小智用板子的喇叭说话：SAPI 合成 → 16 kHz PCM → 推给板子。

    这个函数直接当 `XiaoZhi(speak=...)` 的回调用：签名只吃一个 str，
    返回统计信息（调用方不看也无所谓）。
    """
    from . import tts

    if not text.strip():
        return {"bytes": 0, "seconds_audio": 0.0, "text": ""}
    wav = tts.synthesize(text)
    pcm = pcm_from_wav(wav)
    info = play_pcm(pcm, host=host, port=port)
    info["text"] = text
    return info


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    args = sys.argv[1:]
    if args[0] == "--pcm":
        pcm = Path(args[1]).read_bytes()
        host = args[2]
        info = play_pcm(pcm, host)
    else:
        host = args[1]
        info = play_file(args[0], host)
    print(info)
