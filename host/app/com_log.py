"""读板子 PS 串口日志（COMx, 115200 8N1），用来验证控制字节是否真的被固件收到。

固件在每个模式/延迟变化时都会 xil_printf 一行，例如
    dsp=2 轻量门
    lat=30 frames (300 ms) mode=2
所以只要能读到串口，就能拿到"板子收到了什么"的硬证据，不必靠 JTAG 猜。

不依赖 pyserial：用 ctypes 直接调 Win32 的 CreateFile/SetCommState/ReadFile。
"""
import ctypes
import sys
import time
from ctypes import wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value


class DCB(ctypes.Structure):
    _fields_ = [
        ("DCBlength", w.DWORD), ("BaudRate", w.DWORD),
        ("fBinary", w.DWORD, 1), ("fParity", w.DWORD, 1),
        ("fOutxCtsFlow", w.DWORD, 1), ("fOutxDsrFlow", w.DWORD, 1),
        ("fDtrControl", w.DWORD, 2), ("fDsrSensitivity", w.DWORD, 1),
        ("fTXContinueOnXoff", w.DWORD, 1), ("fOutX", w.DWORD, 1),
        ("fInX", w.DWORD, 1), ("fErrorChar", w.DWORD, 1),
        ("fNull", w.DWORD, 1), ("fRtsControl", w.DWORD, 2),
        ("fAbortOnError", w.DWORD, 1), ("fDummy2", w.DWORD, 17),
        ("wReserved", w.WORD), ("XonLim", w.WORD), ("XoffLim", w.WORD),
        ("ByteSize", w.BYTE), ("Parity", w.BYTE), ("StopBits", w.BYTE),
        ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char), ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char), ("wReserved1", w.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [("ReadIntervalTimeout", w.DWORD),
                ("ReadTotalTimeoutMultiplier", w.DWORD),
                ("ReadTotalTimeoutConstant", w.DWORD),
                ("WriteTotalTimeoutMultiplier", w.DWORD),
                ("WriteTotalTimeoutConstant", w.DWORD)]


def open_port(name):
    # GENERIC_READ|GENERIC_WRITE: 只读句柄写不进去，但验证脚本必须能敲命令。
    # 打开后立刻 SetCommState，把 DTR/RTS 都关掉，否则有些 USB 串口一动这两根
    # 线板子就复位，日志会被截断。
    h = k32.CreateFileW("\\\\.\\" + name, GENERIC_READ | GENERIC_WRITE, 0, None,
                        OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE or h is None:
        raise OSError("打不开 %s，err=%d" % (name, ctypes.get_last_error()))
    dcb = DCB()
    dcb.DCBlength = ctypes.sizeof(DCB)
    if not k32.GetCommState(h, ctypes.byref(dcb)):
        raise OSError("GetCommState 失败 err=%d" % ctypes.get_last_error())
    dcb.BaudRate = 115200
    dcb.ByteSize = 8
    dcb.Parity = 0          # NOPARITY
    dcb.StopBits = 0        # ONESTOPBIT
    dcb.fBinary = 1
    dcb.fParity = 0
    dcb.fOutxCtsFlow = 0
    dcb.fOutxDsrFlow = 0
    dcb.fDtrControl = 0     # DTR_CONTROL_DISABLE：不碰板子的 DTR
    dcb.fRtsControl = 0     # RTS_CONTROL_DISABLE
    dcb.fOutX = 0
    dcb.fInX = 0
    dcb.fAbortOnError = 0
    if not k32.SetCommState(h, ctypes.byref(dcb)):
        raise OSError("SetCommState 失败 err=%d" % ctypes.get_last_error())
    to = COMMTIMEOUTS(0xFFFFFFFF, 0, 100, 0, 100)
    k32.SetCommTimeouts(h, ctypes.byref(to))
    k32.PurgeComm(h, 0x0004 | 0x0008)   # PURGE_TXABORT|PURGE_RXABORT
    return h


def write_bytes(h, data):
    """往串口写原始字节。固件按单字符解析，所以一个字节就是一条命令。"""
    n = w.DWORD(0)
    buf = ctypes.create_string_buffer(data, len(data))
    if not k32.WriteFile(h, buf, len(data), ctypes.byref(n), None):
        raise OSError("WriteFile 失败 err=%d" % ctypes.get_last_error())
    return n.value


def read_for(h, seconds, out=sys.stdout):
    buf = ctypes.create_string_buffer(4096)
    n = w.DWORD(0)
    t_end = time.time() + seconds
    acc = bytearray()
    while time.time() < t_end:
        if not k32.ReadFile(h, buf, 4096, ctypes.byref(n), None):
            break
        if n.value:
            acc += buf.raw[:n.value]
    return acc.decode("utf-8", "replace")


def main():
    ports = sys.argv[1:] or ["COM5", "COM6", "COM7", "COM8", "COM3", "COM4"]
    secs = 3.0
    for p in ports:
        try:
            h = open_port(p)
        except OSError:
            continue
        data = read_for(h, secs)
        k32.CloseHandle(h)
        if data.strip():
            print("=== %s 收到 %d 字节 ===" % (p, len(data)))
            print(data[-3000:])
            return 0
        print("(%s 打开成功但 %gs 内无数据)" % (p, secs))
    print("没有找到在打日志的串口")
    return 1


if __name__ == "__main__":
    sys.exit(main())
