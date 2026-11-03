"""Zynq 音频板的控制面通道（UDP 50507 / 观测 50505 / 控制台 HTTP 8770）。

协议来源是固件源码，不是猜的：
  work/eth_audio/sw/app_ea_main.c:1518-1628  —— 单字节 / 双字节控制解析
  work/eth_audio/sw/app_ea_main.c:58-61      —— 端口定义

控制面约定（第一条成立的前提都写在固件里）：
  * 目标端口 50507，UDP 载荷长度必须 > 0（固件判的是 UDP 长度字段 > 8）；
  * 所有命令都是幂等的（重复收到同一个字节是 no-op），所以重发是安全的；
  * `'S'` 需要两个字节：`'S'` + 百分比（50..100），越界由固件拒绝、不做钳位；
  * 板子只认目的端口，不检查来源 IP，所以这里可以单播。

时基提醒：`'S'` 下发的比例只在 RNNoise 模式（`'1'`）下真正生效，因为
`if (g_ts_apply)` 嵌在 `if (g_dsp_mode == 1u)` 里面（app_ea_main.c:1764/1795）。
旁路模式下下发 `'S'` 只会置请求标志，不会拉伸任何音频。
"""

from __future__ import annotations

import json
import socket
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

BOARD_IP = "169.254.191.50"
BOARD_BCAST = "169.254.255.255"   # 控制面实际只认这个：单播会被板子丢掉
PORT_MIC = 50505   # 板子 → PC：麦克风 PCM（每帧 2 包 × 968 B）
PORT_IN = 50506    # PC → 板子：播放 PCM
PORT_CTL = 50507   # 控制面
PORT_STAT = 50508  # 板子 → PC：状态广播（v1 12 B / v2 15 B 载荷，'A''S' 头）
CONSOLE_URL = "http://127.0.0.1:8770"

_MODE_BY_CODE = {0: "bypass", 1: "denoise", 2: "gate", 3: "pl"}
_DAC_NAME = {0: "M0 同源", 1: "M1 左右分离", 2: "M2 外接独占"}


def parse_status(data: bytes) -> dict | None:
    """解析 50508 上的一帧状态包（与固件 send_status 一一对应）。

    两种载荷都要认（固件 v2 起是 15 字节）：
      v1（12 B）：'S', ver, mode, ts_rate, ts_apply, dly_frames, lat_off,
                  gain_hi, gain_lo, rn_target, tone, xor(前 11)
      v2（15 B）：v1 的 tone 之后追加 noise_hi, noise_lo, dac_mode，
                  xor 覆盖前 14 字节（前 11 字节的 XOR 与 v1 相同，
                  所以这里只需按长度判断再取尾部字段）
    """
    if not data or data[:2] != b"AS":
        return None
    p = data[8:] if len(data) > 12 else data
    if len(p) < 12 or p[0:1] != b"S":
        return None
    n = 11 if len(p) < 15 else 14     # v1 的校验覆盖 11 字节，v2 覆盖 14
    x = 0
    for b in p[:n]:
        x ^= b
    if x != p[n]:
        return None
    body = {
        "ver": p[1],
        "mode": _MODE_BY_CODE.get(p[2], "?"),
        "mode_code": p[2],
        "ts_rate": p[3],
        "ts_apply": bool(p[4]),
        "dly_frames": p[5],
        "lat_off": bool(p[6]),
        "gain": (p[7] << 8) | p[8],
        "rn_target": p[9],
        "tone": bool(p[10]),
        "noise": ((p[11] << 8) | p[12]) if len(p) >= 15 else None,
        "dac_mode": p[13] if len(p) >= 15 else None,
        "raw": data.hex(),
    }
    body["dac_name"] = _DAC_NAME.get(body["dac_mode"], "?")
    return body

# ---- 单字节命令表（照抄固件分支，顺序与源码一致）------------------------
MODE_BYTES = {"bypass": b"0", "denoise": b"1", "gate": b"2", "pl": b"3"}

# 'a'..'d' 显式噪声档（0 / 300 / 800 / 1500），'n'/'N' 是 800 / 0 的别名
NOISE_BYTES = {0: b"a", 300: b"b", 800: b"c", 1500: b"d"}

# 'e'..'h' 输出增益 Q15：32768=0 dB，24576=-2.5 dB，16384=-6 dB，8192=-12 dB
GAIN_BYTES = {32768: b"e", 24576: b"f", 16384: b"g", 8192: b"h"}

TONE_ON, TONE_OFF = b"t", b"T"
DELAY_ON, DELAY_OFF = b"k", b"K"
LATENCY_BYTES = {30: b"i", 20: b"j"}   # 延迟阶梯：帧数
TS_MIN, TS_MAX = 50, 100


@dataclass
class SendResult:
    payload: bytes
    target: str
    sent: int
    dry_run: bool
    note: str = ""

    def as_text(self) -> str:
        pkt = self.payload.hex()
        if self.dry_run:
            return f"[dry-run] 未发送 {pkt} → {self.target}{(' ' + self.note) if self.note else ''}"
        return f"已发送 {self.sent} 个 UDP 包 {pkt} → {self.target}{(' ' + self.note) if self.note else ''}"


class BoardLink:
    """控制面发送 + 上行链路观测 + 控制台查询。"""

    def __init__(self, ip: str = BOARD_IP, dry_run: bool = False, repeat: int = 2,
                 gap_s: float = 0.02) -> None:
        self.ip = ip
        self.dry_run = dry_run
        self.repeat = max(1, int(repeat))
        self.gap_s = gap_s

    # ---- 底层发送 --------------------------------------------------------
    def _send(self, payload: bytes, note: str = "") -> SendResult:
        """发控制字节。

        **必须发到子网广播 169.254.255.255，单播 169.254.191.50 板子收不到。**
        这条是 2026-10-30 23:5x 实测出来的：同一个 '0'，单播两次没反应，
        广播两次 `g_dsp_mode` 立刻 1→0（`.tmp/route_test.txt`）。之前文档里
        "可以单播"的说法是错的。
        """
        target = f"{BOARD_BCAST}:{PORT_CTL}"
        if self.dry_run:
            return SendResult(payload, target, 0, True, note)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for i in range(self.repeat):
                s.sendto(payload, (BOARD_BCAST, PORT_CTL))
                if i + 1 < self.repeat:
                    time.sleep(self.gap_s)
        return SendResult(payload, target, self.repeat, False, note)

    # ---- 命令 ------------------------------------------------------------
    def set_mode(self, mode: str) -> SendResult:
        key = mode.strip().lower()
        if key not in MODE_BYTES:
            raise ValueError(f"mode 只能是 {sorted(MODE_BYTES)}，收到 {mode!r}")
        note = "" if key == "denoise" else "（注意：时间缩放只在 denoise 模式下生效）"
        return self._send(MODE_BYTES[key], note)

    def set_time_scale(self, percent: int) -> SendResult:
        pct = int(percent)
        if not TS_MIN <= pct <= TS_MAX:
            # 固件对越界值是"拒绝并打印原因"，不钳位；这里先挡掉，避免静默无效
            raise ValueError(f"百分比必须在 {TS_MIN}..{TS_MAX}，收到 {pct}")
        return self._send(b"S" + bytes([pct]), f"（{pct}% 时长比例）")

    def set_gain(self, q15: int) -> SendResult:
        if int(q15) not in GAIN_BYTES:
            raise ValueError(f"增益只能是 {sorted(GAIN_BYTES)}（Q15），收到 {q15}")
        return self._send(GAIN_BYTES[int(q15)])

    def set_noise(self, level: int) -> SendResult:
        if int(level) not in NOISE_BYTES:
            raise ValueError(f"噪声档只能是 {sorted(NOISE_BYTES)}，收到 {level}")
        return self._send(NOISE_BYTES[int(level)])

    def set_latency(self, frames: int) -> SendResult:
        if int(frames) not in LATENCY_BYTES:
            raise ValueError(f"延迟阶梯只能是 {sorted(LATENCY_BYTES)} 帧，收到 {frames}")
        return self._send(LATENCY_BYTES[int(frames)])

    def tone(self, on: bool) -> SendResult:
        return self._send(TONE_ON if on else TONE_OFF)

    def delay(self, on: bool) -> SendResult:
        return self._send(DELAY_ON if on else DELAY_OFF)

    # ---- 观测 ------------------------------------------------------------
    def watch(self, seconds: float = 2.0) -> dict:
        """在 50505 上实际收包，给出链路真值。

        这是"板子到底在不在"的唯一硬证据：控制台 /api/state 的 fps 曾经是假的
        （JitterBuffer 没数据也返回静音），只有这里数到的包数不会骗人。
        """
        deadline = time.time() + max(0.3, float(seconds))
        packets = 0
        first_pkt_at = None
        sources: dict[str, int] = {}
        seqs: list[int] = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except OSError:
                pass
            s.bind(("0.0.0.0", PORT_MIC))
            s.settimeout(0.1)
            while time.time() < deadline:
                try:
                    data, addr = s.recvfrom(2048)
                except socket.timeout:
                    continue
                if len(data) < 8 or data[0:2] != b"AU":
                    continue
                packets += 1
                if first_pkt_at is None:
                    first_pkt_at = time.time()
                src = f"{addr[0]}:{addr[1]}"
                sources[src] = sources.get(src, 0) + 1
                if len(data) >= 4:
                    seqs.append(struct.unpack(">H", data[2:4])[0])
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if (b - a) & 0xFFFF not in (0, 1))
        window = max(0.001, (time.time() - first_pkt_at)) if first_pkt_at else 0.0
        return {
            "packets": packets,
            "window_s": round(window, 3),
            "frames_per_s": round(packets / 2.0 / window, 1) if window else 0.0,
            "sources": sources,
            "seq_gaps": gaps,
            "online": packets > 0,
        }

    def console_state(self, timeout: float = 1.5) -> dict | None:
        """读 PC 控制台的 /api/state；控制台没起来就返回 None（不是错误）。"""
        try:
            with urllib.request.urlopen(f"{CONSOLE_URL}/api/state", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    def console_cmd(self, op: str, **kw) -> dict | None:
        """给 PC 控制台下一条命令（POST /api/cmd）。控制台没起来返回 None。"""
        body = json.dumps(dict(op=op, **kw)).encode("utf-8")
        req = urllib.request.Request(
            f"{CONSOLE_URL}/api/cmd", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3.0) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    # ---- 板子自报的状态（50508）------------------------------------------
    # 50508 是**控制台独占**的端口：Windows 上两个 socket 带 SO_REUSEADDR 绑同一
    # 端口，包只会投给后绑的那个，先绑的不会在对方关闭后自动恢复接收。
    # 早先在工具层自己绑过一次，直接把控制台的板子状态喂死了（面板从此不再更新，
    # 小智还以为板子没动）。所以这里只问控制台，绝不自己抢端口。
    def status(self, seconds: float = 0.8) -> dict | None:
        """板子当前真实状态；来源永远是控制台转述（它一直在收 50508）。

        控制台没起来就返回 None —— 这时"板子到底什么状态"是真的不知道，
        不许猜，也不许拿本机上次发的命令冒充。
        """
        st = self.console_state()
        if not st or not isinstance(st.get("board"), dict):
            return None
        b = dict(st["board"])
        # 控制台沿用它自己的命名（rnnoise），工具层统一叫 denoise —— 否则
        # 回读确认会把"已经是 denoise"误判成"模式没变"。
        if b.get("mode") == "rnnoise":
            b["mode"] = "denoise"
        b["source"] = "console"
        b["listening"] = bool(st.get("listening"))
        b["console_dsp"] = st.get("dsp")
        return b
