# -*- coding: utf-8 -*-
"""板子在线时的逐控件验收：命令 → 板子回读 → 判定。

判据一律用「板子自己上报的真值」（50508 状态包），不看本机意图。
DAC 模式自状态包 v2 起由板子自报（board.dac_mode），串口 dm= 只作兜底。
"""
import json
import time
import urllib.request

import serial
import serial.tools.list_ports as _lp


def _port():
    """板子的 CH340 会随插拔在 COM3/COM4 之间跳，按 VID 找，别写死。"""
    for p in _lp.comports():
        if "CH340" in (p.description or "").upper() or "1A86" in (p.hwid or ""):
            return p.device
    return "COM3"

B = "http://127.0.0.1:8770"
ROWS = []


def post(b):
    r = urllib.request.Request(B + "/api/cmd", data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())


def st():
    return json.loads(urllib.request.urlopen(B + "/api/state", timeout=8).read())


def board():
    return (st().get("board") or {})


def check(name, body, field, want, wait=1.0, read=None):
    a = post(body)
    time.sleep(wait)
    b = board()
    got = (read or (lambda x: x.get(field)))(b)
    ok = (got == want)
    ROWS.append((name, str(a.get("ack"))[:26], "%s" % got, "%s" % want, "PASS" if ok else "FAIL"))
    return ok


def serial_probe(cmd_byte, field="dm"):
    """发一个控制字节，同时读调试串口状态行里的 dm= （兜底证据）。"""
    try:
        sp = serial.Serial(_port(), 115200, timeout=0.3)
    except Exception as exc:                       # noqa: BLE001
        return "COM3 打不开: %r" % (exc,)
    try:
        sp.reset_input_buffer()
        post({"op": "dac", "m": {"m": 0, "o": 1, "p": 2}[cmd_byte]})
        t0 = time.time()
        last = ""
        while time.time() - t0 < 4.0:
            line = sp.readline().decode("ascii", "ignore").strip()
            if line:
                last = line
            if "dm=" in line:
                return line
        return last or "(没读到状态行)"
    finally:
        sp.close()


print("=== 板子在线确认 ===")
b = board()
print("online=%s mode=%s ts=%s gain=%s dly=%s lat_off=%s age=%s"
      % (b.get("online"), b.get("mode"), b.get("ts_rate"), b.get("gain"),
         b.get("dly_frames"), b.get("lat_off"), b.get("age")))

print("\n=== 1. 收听 ===")
post({"op": "listen", "v": True}); time.sleep(2.0)
s = st()
ROWS.append(("开始收听", "收听", "listening=%s link=%s fps=%s"
             % (s["listening"], s["link"], round(s["fps"] or 0, 1)),
             "listening=True link=ok", "PASS" if (s["listening"] and s["link"] == "ok") else "FAIL"))

print("=== 2. 降噪四档（板子回读 mode）===")
for k, v in (("旁路", "bypass"), ("RNNoise", "rnnoise"), ("噪声门", "gate"), ("PL 谱减", "pl")):
    check("降噪 → " + k, {"op": "dsp", "v": v}, "mode", v, 1.0)

print("=== 3. 时长缩放 ===")
for v in (75, 50, 100):
    check("时长 %d%%" % v, {"op": "ts", "v": v}, "ts_rate", v, 1.0)

print("=== 4. 增益四档（Q15 回读）===")
for db, q in ((0, 32768), (-3, 24576), (-6, 16384), (-12, 8192)):
    check("增益 %+d dB" % db, {"op": "gain", "db": db}, "gain", q, 1.0)

print("=== 5. 延迟 ===")
check("延迟 30 帧", {"op": "lat", "frames": 30, "on": True}, "dly_frames", 30, 2.0)
check("延迟 20 帧", {"op": "lat", "frames": 20, "on": True}, "dly_frames", 20, 2.0)
check("延迟 关", {"op": "lat", "on": False}, "lat_off", True, 2.0)

print("=== 6. 底噪注入（状态包 v2 起带 noise，按板子回读判）===")
for v in (0, 300, 800, 1500):
    a = post({"op": "noise", "v": v})
    time.sleep(1.2)                     # 状态包 4 Hz，留够两包
    got = (st().get("board") or {}).get("noise")
    ROWS.append(("底噪 %d" % v, str(a.get("ack"))[:26], "board.noise=%s" % got, "board.noise=%d" % v,
                 "PASS" if got == v else "FAIL"))

print("=== 7. 板载测试音 ===")
check("测试音 开", {"op": "tone", "v": True}, "tone", 1, 1.2)
check("测试音 关", {"op": "tone", "v": False}, "tone", 0, 1.2)

print("=== 8. 双 DAC（状态包 v2 的 dac_mode 为主，串口 dm= 兜底）===")
for cb in ("m", "o", "p", "m"):
    want = {"m": 0, "o": 1, "p": 2}[cb]
    line = serial_probe(cb)             # 顺带把串口那行留作证据
    time.sleep(1.2)
    got = (st().get("board") or {}).get("dac_mode")
    ok = (got == want) or ("dm=%d" % want) in (line or "")
    ROWS.append(("双 DAC '%s'" % cb, "已发",
                 "board.dac_mode=%s | %s" % (got, (line or "")[:44]),
                 "board.dac_mode=%d" % want, "PASS" if ok else "FAIL"))

print("=== 9. 推流 / 环回 ===")
a = post({"op": "push", "v": 2, "src": "tone"}); time.sleep(0.6)
pushing = st().get("pushing")
ROWS.append(("推 2 s 测试音", str(a.get("ack"))[:26], "pushing=%s" % pushing, "pushing=True",
             "PASS" if pushing else "FAIL"))
time.sleep(2.5)
ROWS.append(("推流自动结束", "-", "pushing=%s" % st().get("pushing"), "pushing=False",
             "PASS" if not st().get("pushing") else "FAIL"))
a = post({"op": "loop", "on": True}); time.sleep(2.0)
lo = (st().get("loop") or {})
ROWS.append(("同步电脑声音", str(a.get("ack"))[:26], "blocks=%s" % lo.get("blocks"), ">0",
             "PASS" if (lo.get("blocks") or 0) > 0 else "FAIL"))
post({"op": "loop", "on": False}); time.sleep(0.6)

print("=== 10. 收尾恢复（含底噪归 0、测试音关）===")
for body in ({"op": "noise", "v": 0}, {"op": "tone", "v": False},
             {"op": "dsp", "v": "bypass"}, {"op": "ts", "v": 100}, {"op": "gain", "db": 0},
             {"op": "lat", "frames": 20, "on": True}, {"op": "dac", "m": 0}):
    post(body); time.sleep(0.3)
time.sleep(1.2)
_b = st().get("board") or {}
print("  收尾后 board: mode=%s noise=%s tone=%s dac_mode=%s"
      % (_b.get("mode"), _b.get("noise"), _b.get("tone"), _b.get("dac_mode")))

print("\n%-16s %-28s %-30s %-14s %s" % ("控件", "回执", "板子回读", "期望", "判定"))
print("-" * 108)
for r in ROWS:
    print("%-16s %-28s %-30s %-14s %s" % r)
print("-" * 108)
print("PASS %d / FAIL %d" % (sum(1 for r in ROWS if r[4] == "PASS"),
                            sum(1 for r in ROWS if r[4] == "FAIL")))
