r"""ESP32-S3 通道（串口）。

一件事：把一段 MicroPython 代码送到板子上执行，把它打印的东西拿回来。

**这块板子现在的实际状态**（2026-10-30 22:3x 实测，别再猜）：
  * 串口 **COM7**（FTDI FT232，VID_0403:PID_6001，SN=A5069RR4A），不是 CH340 的 COM3；
  * ESP32-S3 (QFN56) rev v0.2 / **16 MB flash** / **8 MB PSRAM (AP_3v3)** / MAC f8:5b:1b:8f:ff:e8；
  * 已烧 **MicroPython v1.29.0（Generic ESP32S3 · Octal-SPIRAM）**，原厂固件备份在
    `D:\esp32_backup\esp32s3_factory_backup_2mb.bin`（只备了前 2 MB，够回滚启动区）；
  * `boot.py` 上电自动连手机热点 **8848**（10.55.67.228），`main.py` 起 TCP 9200 → I2S 播放。

坑（都踩过）：
  * DTR/RTS 接在 EN/IO0 上，开串口必须 `dsrdtr=False, rtscts=False, dtr=False, rts=False`，
    否则每开一次口就复位一次；
  * `exec("<很长的代码>")` 在 115200 下会掉字符 → 写文件用 `write_file()`（base64 分块）；
  * `mpremote` 进不去 raw REPL（could not enter raw repl），一律走本模块。
"""

from __future__ import annotations

import time

# 实测这块板子是 COM7；留 None 表示"自己找"，找到的第一个能进 REPL 的口就用它
DEFAULT_PORT: str | None = None
KNOWN_PORT = "COM7"
DEFAULT_BAUD = 115200


class EspError(RuntimeError):
    pass


def list_ports() -> list[tuple[str, str]]:
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return []
    return [(p.device, p.description or "") for p in lp.comports()]


def _open(port: str, baud: int):
    try:
        import serial  # pyserial
    except ImportError as e:  # pragma: no cover
        raise EspError(f"没装 pyserial：{e}") from e
    try:
        # 这块板子的 DTR/RTS 接在 EN/IO0 上（厂商自动下载电路）：
        # 打开口时若把 DTR 拉高就会复位板子——会看到一串重复的
        # MicroPython banner，REPL 也永远停在刚启动那一瞬。
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.timeout = 0.2
        ser.write_timeout = 2.0
        ser.dsrdtr = False
        ser.rtscts = False
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.setDTR(False)
        ser.setRTS(False)
        return ser
    except Exception as e:
        raise EspError(f"打不开 {port}：{e}") from e


def _sync(p, tries: int = 3) -> str:
    """打断当前程序，把 REPL 逼到提示符。返回这期间收到的原始文本。"""
    buf = b""
    for _ in range(tries):
        p.write(b"\x03")
        time.sleep(0.12)
        buf += p.read(4096)
    p.write(b"\r\n")
    time.sleep(0.25)
    buf += p.read(4096)
    return buf.decode("utf-8", "replace")


def _looks_like_repl(text: str) -> bool:
    return ">>>" in text or "MicroPython" in text


def find_port(baud: int = DEFAULT_BAUD) -> str:
    """在现有串口里找那个能进 MicroPython REPL 的。找不到就报清楚原因。"""
    ports = list_ports()
    if not ports:
        raise EspError("系统里一个串口都没有：USB 线没插，或者只是充电线。")
    tried: list[str] = []
    for dev, desc in ports:
        try:
            p = _open(dev, baud)
        except EspError as e:
            tried.append(f"{dev}({desc}) 打不开：{e}")
            continue
        try:
            text = _sync(p)
        finally:
            p.close()
        if _looks_like_repl(text):
            return dev
        head = text.strip().split("\n")[0][:60] if text.strip() else "无回显"
        tried.append(f"{dev}({desc}) 有回显但不是 REPL：{head!r}")
    raise EspError("没有串口在跑 MicroPython → " + "；".join(tried))


def _read_until_prompt(p, timeout: float = 8.0) -> str:
    """一直读到 `>>>` 为止（超时也返回已收到的内容）。

    比"静默 1.5 秒就收工"可靠：写文件时每块都要确认板子真的回到了提示符，
    否则下一块的 payload 会和上一块挤在一起，被 REPL 当垃圾吃掉。
    """
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        chunk = p.read(4096)
        if chunk:
            buf += chunk
            if buf.rstrip().endswith(b">>>"):
                break
    return buf.decode("utf-8", "replace")


def _exec_once(p, code: str, settle_s: float = 1.2, idle_s: float = 1.5) -> str:
    """在已打开的串口上执行一段代码，返回它打印的内容。

    先等提示符 → 再写 payload → 再等提示符，三步都对上时序，才不会串行。
    `settle_s` 是整体超时，`idle_s` 是"没有提示符时，静默多久算结束"的兜底。
    """
    _read_until_prompt(p, timeout=2.0)          # 确认起点在提示符
    payload = "exec(" + repr(code) + ")\r\n"
    p.write(payload.encode("utf-8"))
    deadline = time.time() + settle_s
    buf = b""
    while time.time() < deadline:
        chunk = p.read(4096)
        if chunk:
            buf += chunk
            if buf.rstrip().endswith(b">>>"):
                break
            deadline = min(max(deadline, time.time() + idle_s), time.time() + settle_s)
    text = buf.decode("utf-8", "replace")
    echo = payload.strip()
    lines: list[str] = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        ln = ln.strip("\x00 ")
        if not ln or (ln.endswith(">>>") and not ln[:-3].strip()):
            continue
        if ln == echo:
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def write_file(path: str, text: str, port: str | None = None,
               baud: int = DEFAULT_BAUD, chunk: int = 512) -> dict:
    """把 `text` 写进板子上的 `path`，写完用 `os.stat` 核对字节数。

    直接 `exec("open(...,'w').write(<长字符串>)")` 在 115200 下会掉字符，
    所以 base64 分块追加，每块都等 `>>>` 再发下一块。核对不上就抛 EspError，
    绝不返回"看起来成功"的结果。
    """
    import base64

    dev = port or find_port(baud)
    raw = text.encode("utf-8")
    data = base64.b64encode(raw).decode("ascii")
    p = _open(dev, baud)
    try:
        _sync(p)
        _exec_once(p, "open(%r, 'wb').close()" % path, settle_s=5.0)
        for i in range(0, len(data), chunk):
            piece = data[i:i + chunk]
            code = ("import binascii\n"
                    "f = open(%r, 'ab')\n"
                    "n = f.write(binascii.a2b_base64(%r))\n"
                    "f.close()\n"
                    "print('CHUNK', n)" % (path, piece))
            out = _exec_once(p, code, settle_s=8.0, idle_s=1.0)
            if "CHUNK" not in out:
                raise EspError(f"{path} 第 {i // chunk + 1} 块写失败：{out[:200]!r}")
        size = _exec_once(p, "import os\nprint('SIZE', os.stat(%r)[6])" % path,
                          settle_s=5.0)
    finally:
        p.close()

    got = None
    for ln in size.split("\n"):
        if ln.startswith("SIZE"):
            got = ln.split()[1] if len(ln.split()) > 1 else None
    if got is None:
        raise EspError(f"{path} 写完后读不到大小：{size[:200]!r}")
    if int(got) != len(raw):
        raise EspError(f"{path} 大小不对：板子上 {got} 字节，期望 {len(raw)} 字节")
    return {"path": path, "bytes": len(raw), "chunks": (len(data) + chunk - 1) // chunk,
            "b64_len": len(data), "verify": f"SIZE {got}"}


def run(code: str, port: str | None = None, baud: int = DEFAULT_BAUD,
        settle_s: float = 1.2, idle_s: float = 1.5) -> str:
    """在 REPL 里执行 `code`，返回板子打印的文本（已去掉回显与提示符）。

    settle_s: 写完命令后最多等多久（秒）
    idle_s:   连续静默多久就认为跑完了（秒）。板子里有 sleep 时把它调大，
              否则会在板子还没打印下一行时就收工，只拿到半截输出。

    注意：`code` 太长（>1 KB）在 115200 下会掉字符，写文件请用 `write_file()`。
    """
    dev = port or find_port(baud)
    p = _open(dev, baud)
    try:
        banner = _sync(p)
        if not _looks_like_repl(banner) and not banner.strip():
            raise EspError(f"{dev} 上一个字节都没有：TX 方向或供电没通。")
        # 用 exec 包一层：多行代码也能跑，异常会打印出来而不是被吞掉
        out = _exec_once(p, code, settle_s=settle_s, idle_s=idle_s)
    finally:
        p.close()

    if not out:
        raise EspError(f"{dev} 上执行了但没有输出（REPL 可能在跑死循环）。")
    return out


def info(port: str | None = None, baud: int = DEFAULT_BAUD) -> dict:
    """读板子的实现、flash、PSRAM、MAC —— 用来确认是不是 N16R8。"""
    code = (
        "import sys, gc\n"
        "import esp, machine, os\n"
        "print('IMPL', sys.implementation)\n"
        "print('PLATFORM', sys.platform)\n"
        "print('FLASH', getattr(esp, 'flash_size', lambda: -1)())\n"
        "print('FREQ', machine.freq())\n"
        "print('MEM_FREE', gc.mem_free())\n"
        "print('UNIQUE', machine.unique_id().hex())\n"
    )
    text = run(code, port=port, baud=baud)
    out: dict[str, str] = {"raw": text}
    for ln in text.split("\n"):
        if " " in ln:
            k, _, v = ln.partition(" ")
            out[k.strip()] = v.strip()
    return out


def probe(port: str | None = None, baud: int = DEFAULT_BAUD) -> dict:
    """只做"有没有 REPL 应答"的探测，不抛异常，方便上层展示状态。"""
    try:
        dev = port or find_port(baud)
        text = run("print('PONG')", port=dev, baud=baud, settle_s=1.0)
    except EspError as e:
        return {"ok": False, "port": port, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - 串口被占用之类
        return {"ok": False, "port": port, "error": f"{type(e).__name__}: {e}"}
    return {"ok": "PONG" in text, "port": dev, "response": text[:400]}


def boot_log(port: str = KNOWN_PORT, baud: int = DEFAULT_BAUD, seconds: float = 2.0) -> str:
    """不打断板子，单纯收一段串口输出（看它在跑什么固件）。"""
    p = _open(port, baud)
    buf = b""
    try:
        t0 = time.time()
        while time.time() - t0 < seconds:
            chunk = p.read(8192)
            if chunk:
                buf += chunk
    finally:
        p.close()
    return buf.decode("utf-8", "replace")
