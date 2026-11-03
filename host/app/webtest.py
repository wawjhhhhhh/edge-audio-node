# -*- coding: utf-8 -*-
"""逐项验证 Web 控制台的每个功能，不需要真板子也能跑的部分全部跑通。"""
import json, time, urllib.request, sys

BASE = "http://127.0.0.1:8770"
def get(p, raw=False):
    r = urllib.request.urlopen(BASE + p, timeout=10)
    if raw:
        return r.read(), dict(r.headers)
    return json.loads(r.read().decode())
def post(b):
    r = urllib.request.Request(BASE + "/api/cmd", data=json.dumps(b).encode(),
                              headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read().decode())

ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1; print("  [PASS] %-30s %s" % (name, detail))
    else:
        fail += 1; print("  [FAIL] %-30s %s" % (name, detail))

print("1. 静态资源")
html = get("/", raw=True)[0].decode()
check("index.html", len(html) > 40000, "%d B" % len(html.encode()))
for k in ("概览", "监听", "音源", "降噪", "关于", "10 ms", "模拟信号"):
    check("含「%s」" % k, k in html)

print("2. 启动模拟信号")
r = post({"op": "sim", "v": True})
check("sim on", r.get("ack") == "模拟中", str(r))
time.sleep(3.0)
s = get("/api/state")
check("listening", s["listening"] is True)
check("fps≈100", abs((s["fps"] or 0) - 100) < 5, "fps=%.1f" % (s["fps"] or -1))
check("dbfs 有值", s["dbfs"] is not None, "%.1f dBFS" % (s["dbfs"] or 0))
check("wave 96 对", len(s["wave"]) == 192, "%d 点" % len(s["wave"]))
check("spec 64 段", len(s["spec"]) == 64, "%d 段" % len(s["spec"]))
check("spec 有能量", max(s["spec"]) > 0.2, "峰值 %.2f" % max(s["spec"]))
check("depth 有值", s["depth_ms"] is not None, "%s ms" % s["depth_ms"])

print("3. PCM 增量接口")
raw, hdr = get("/api/pcm?from=0", raw=True)
cnt = int(hdr.get("X-Pcm-Count", 0)); start = int(hdr.get("X-Pcm-Start", -1))
check("首包非空", cnt > 0, "count=%d start=%d bytes=%d" % (cnt, start, len(raw)))
check("字节数匹配", len(raw) == cnt * 4, "%d == %d*4" % (len(raw), cnt))
raw2, hdr2 = get("/api/pcm?from=%d" % (start + cnt), raw=True)
check("续取起点连续", int(hdr2.get("X-Pcm-Start", -1)) == start + cnt,
      "next_start=%s" % hdr2.get("X-Pcm-Start"))
raw3, hdr3 = get("/api/pcm?from=999999999", raw=True)
check("超量返回空", int(hdr3.get("X-Pcm-Count", -1)) == 0)

print("4. 控制命令（板子离线，只验不崩、有回执）")
for op, body in [("dsp", {"op": "dsp", "v": "gate"}),
                 ("dsp rnnoise", {"op": "dsp", "v": "rnnoise"}),
                 ("dsp bypass", {"op": "dsp", "v": "bypass"}),
                 ("noise 300", {"op": "noise", "v": 300}),
                 ("noise 0", {"op": "noise", "v": 0}),
                 ("tone", {"op": "tone"}),
                 ("ab", {"op": "ab"}),
                 ("device", {"op": "device", "v": 0})]:
    try:
        rr = post(body)
        check(op, "ack" in rr, str(rr.get("ack")))
    except Exception as e:
        check(op, False, repr(e))

print("5. 停止模拟")
r = post({"op": "sim", "v": False})
check("sim off", r.get("ack") == "已停止模拟", str(r))
time.sleep(0.6)
s = get("/api/state")
check("停止后 listening=False", s["listening"] is False)

print("\n结果：%d 通过 / %d 失败" % (ok, fail))
sys.exit(1 if fail else 0)
