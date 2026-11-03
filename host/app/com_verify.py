"""边读串口边发命令：验证"板子到底收到了什么"。

这是本会话里唯一可信的证据源。之前用 JTAG 读 0x08000000 得到的数字是垃圾
（读回来的值明显是代码地址），据此做的判断全部作废。串口日志是固件自己
xil_printf 打出来的，读到什么就是板子做了什么。

用法：
    python com_verify.py            # 自动跑一遍：三档 × 两个延迟档
"""
import json
import sys
import threading
import time
import urllib.request as U

import com_log

API = "http://127.0.0.1:8770"
_op = U.build_opener(U.ProxyHandler({}))


def post(body):
    req = U.Request(API + "/api/cmd", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})
    return json.loads(_op.open(req, timeout=8).read())


class Reader(threading.Thread):
    def __init__(self, handle):
        super().__init__(daemon=True)
        self.h = handle
        self.stop = threading.Event()
        self.buf = []

    def run(self):
        while not self.stop.is_set():
            chunk = com_log.read_for(self.h, 0.3)
            if chunk:
                self.buf.append(chunk)

    def take(self):
        s = "".join(self.buf)
        self.buf = []
        return s


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    h = com_log.open_port(port)
    r = Reader(h)
    r.start()
    time.sleep(0.5)
    r.take()
    print("串口 %s 已连上，开始验证\n" % port)

    ok = []
    for mode, byte in (("bypass", "0"), ("rnnoise", "1"), ("gate", "2")):
        # 先切到这个模式，确认 dsp= 那行回显
        post({"op": "dsp", "v": mode})
        time.sleep(0.9)
        log = r.take()
        got_dsp = [l for l in log.splitlines() if l.startswith("dsp=")]
        print("[%s] 期望字节 %s → 板子日志: %s"
              % (mode, byte, got_dsp or "(无回显)"))
        ok.append(bool(got_dsp) and ("dsp=" + byte) in (got_dsp[0] if got_dsp else ""))

        # 再切两个延迟档
        for frames in (30, 20, 30):
            post({"op": "rntarget", "v": frames})
            time.sleep(0.9)
            log = r.take()
            got_lat = [l for l in log.splitlines() if l.startswith("lat=")]
            print("      延迟 %3d ms → %s" % (frames * 10, got_lat or "(无回显)"))
            ok.append(bool(got_lat) and ("lat=%d" % frames) in (got_lat[0] if got_lat else ""))

    r.stop.set()
    time.sleep(0.4)
    com_log.k32.CloseHandle(h)

    print()
    print("通过 %d / %d 项" % (sum(ok), len(ok)))
    return 0 if all(ok) else 2


if __name__ == "__main__":
    sys.exit(main())
