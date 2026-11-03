"""环境自检：把"能不能干活"一次问清楚，而不是等对话到一半才失败。

每个探针只回答三件事：能不能用、证据是什么、不能用时下一步做什么。
"""

from __future__ import annotations

import importlib
import socket
import sys

from .board import BOARD_IP, PORT_MIC


def _pkg(name: str) -> str:
    try:
        m = importlib.import_module(name)
        return f"ok ({getattr(m, '__version__', '?')})"
    except Exception as e:  # noqa: BLE001
        return f"缺：{type(e).__name__}"


def _port_free(port: int) -> tuple[bool, str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True, f"{port} 可绑定"
    except OSError as e:
        return False, f"{port} 被占用（{e}）—— 多半是控制台正在收听"
    finally:
        s.close()


def print_probe() -> None:
    print("=" * 66)
    print("小智环境自检")
    print("=" * 66)
    print(f"python           {sys.version.split()[0]}  {sys.executable}")
    for mod in ("vosk", "serial", "sounddevice", "numpy"):
        print(f"{mod:<16} {_pkg(mod)}")

    # --- 语音合成 ------------------------------------------------------
    from . import tts
    p = tts.probe()
    print(f"TTS(SAPI)        {'ok ' + str(p.get('bytes')) + ' B / ' + tts.VOICE_NAME if p['ok'] else p['error']}")

    # --- 语音识别 ------------------------------------------------------
    from . import asr
    a = asr.probe()
    print(f"ASR(Vosk)        {a['model'] if a['ok'] else a['error']}")

    # --- Zynq 板子 -----------------------------------------------------
    from .board import BoardLink
    link = BoardLink()
    free, note = _port_free(PORT_MIC)
    print(f"50505 收包口      {note}")
    if free:
        obs = link.watch(1.0)
        if obs["online"]:
            print(f"Zynq 板子        在线：{obs['packets']} 包 / {obs['window_s']} s "
                  f"≈ {obs['frames_per_s']} 帧每秒，来源 {list(obs['sources'])}")
        else:
            print(f"Zynq 板子        不在线：1 秒内 0 个包（目标 {BOARD_IP}:50505）")
    state = link.console_state()
    print(f"控制台 8770      {'ok ' + str({k: state.get(k) for k in ('link', 'fps', 'packets')}) if state else '没在运行'}")

    # --- ESP32-S3 ------------------------------------------------------
    from . import esp32
    ports = []
    try:
        import serial.tools.list_ports as lp
        ports = [(p.device, p.description) for p in lp.comports()]
    except Exception:  # noqa: BLE001
        pass
    print(f"串口             {ports if ports else '一个都没有'}")
    if ports:
        r = esp32.probe(ports[0][0])
        print(f"ESP32 REPL       {'ok ' + r.get('response', '')[:40] if r['ok'] else r['error'][:90]}")
    else:
        print("ESP32 REPL       跳过（没有串口）")
    print("=" * 66)
