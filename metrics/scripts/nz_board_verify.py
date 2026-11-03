# -*- coding: utf-8 -*-
"""底噪注入迁移到 PL 之后的上板四组合验收（方案 A）。

测量源：PC 控制台 HTTP（http://127.0.0.1:8770），因为那是用户端实际看到的数字。
  /api/cmd      POST {"op":"dsp","v":"bypass|rnnoise|gate|pl"} / {"op":"noise","v":0|300|800|1500}
  /api/state    取 fps（板子自报的帧率）
  /api/spectrum 取 rms_dbfs / floor_db

四组合：
  A  底噪 0    + 旁路     -> 链路基线
  B  底噪 1500 + 旁路     -> PL 注入的噪声原样通过（证明注入点在链路上游）
  C  底噪 1500 + PL 谱减  -> 噪声经过滤波器，floor 应显著低于 B
  D  底噪 0    + PL 谱减  -> 对照：谱减本身不该把 floor 抬起来

判定：B-A > 0（注入有效）、B-C >= 8 dB（噪声确实在滤波器之前、被压掉）、fps 仍 ~100。

用法：
  python work/eth_audio/pl/denoise/tools/nz_board_verify.py --dwell 4 --json D:/vitis_tmp/nz_verify.json
"""
import argparse
import json
import statistics
import time
import urllib.request

BASE = "http://127.0.0.1:8770"


def get(path, timeout=8.0):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read())


def post(body, timeout=10.0):
    req = urllib.request.Request(
        BASE + "/api/cmd",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def sample(secs, period=0.25):
    """在 secs 秒里周期性取 /api/spectrum + /api/state，返回各字段的中位数。"""
    rms, floor, fps, n = [], [], [], 0
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            sp = get("/api/spectrum")
            st = get("/api/state")
        except Exception as exc:                       # noqa: BLE001
            print("  取数失败: %r" % (exc,))
            time.sleep(period)
            continue
        if sp.get("rms_dbfs") is not None:
            rms.append(float(sp["rms_dbfs"]))
            floor.append(float(sp["floor_db"]))
        if st.get("fps"):
            fps.append(float(st["fps"]))
        n += 1
        time.sleep(period)
    med = lambda a: round(statistics.median(a), 2) if a else None   # noqa: E731
    return {"rms_dbfs": med(rms), "floor_db": med(floor), "fps": med(fps),
            "samples": n, "n_rms": len(rms), "n_fps": len(fps)}


def set_state(mode, noise, settle):
    post({"op": "dsp", "v": mode})
    post({"op": "noise", "v": noise})
    time.sleep(settle)          # 等延迟线/滤波状态收敛


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dwell", type=float, default=4.0, help="每个组合采样多少秒")
    ap.add_argument("--settle", type=float, default=3.0, help="切换后静置多少秒")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    st0 = get("/api/state")
    if st0.get("sim"):
        print("!! 控制台处于 sim 模式（本地模拟信号），数字不可用，先关掉 sim")
    if not st0.get("listening"):
        post({"op": "listen", "v": True})
        time.sleep(2.0)
        st0 = get("/api/state")
    print("控制台: listening=%s sim=%s link=%s 板子=%s"
          % (st0.get("listening"), st0.get("sim"), st0.get("link"),
             (st0.get("board") or {}).get("raw")))

    combos = [("A 底噪0 + 旁路", "bypass", 0),
              ("B 底噪1500 + 旁路", "bypass", 1500),
              ("C 底噪1500 + PL谱减", "pl", 1500),
              ("D 底噪0 + PL谱减", "pl", 0)]

    res = {}
    for name, mode, noise in combos:
        print("== %s ..." % name)
        set_state(mode, noise, args.settle)
        r = sample(args.dwell)
        r["mode"] = mode
        r["noise"] = noise
        r["board"] = (get("/api/state").get("board") or {})
        res[name] = r
        print("   rms=%s dBFS  floor=%s dB  fps=%s  (n=%d/%d/%d)"
              % (r["rms_dbfs"], r["floor_db"], r["fps"], r["samples"], r["n_rms"], r["n_fps"]))

    # 收尾：恢复底噪 0 + 旁路
    set_state("bypass", 0, 0.5)

    a, b, c, d = (res["A 底噪0 + 旁路"], res["B 底噪1500 + 旁路"],
                  res["C 底噪1500 + PL谱减"], res["D 底噪0 + PL谱减"])

    def diff(x, y):
        if x["floor_db"] is None or y["floor_db"] is None:
            return None
        return round(y["floor_db"] - x["floor_db"], 2)

    summary = {
        "B-A_floor_lift_db": diff(a, b),     # >0 说明注入有效
        "B-C_floor_drop_db": diff(b, c),     # >=8 说明噪声在滤波器之前被压掉
        "C-D_floor_delta_db": diff(d, c),    # 对照
        "fps_A": a["fps"], "fps_C": c["fps"],
    }

    print("\n%-22s %10s %10s %8s" % ("组合", "rms_dbfs", "floor_db", "fps"))
    print("-" * 56)
    for name, _, _ in combos:
        r = res[name]
        print("%-22s %10s %10s %8s" % (name, r["rms_dbfs"], r["floor_db"], r["fps"]))
    print("-" * 56)
    print("B-A floor 抬升 : %s dB   (期望 > 0)" % summary["B-A_floor_lift_db"])
    print("B-C floor 下降 : %s dB   (期望 >= 8)" % summary["B-C_floor_drop_db"])
    print("C-D 对照差     : %s dB" % summary["C-D_floor_delta_db"])
    print("fps: A=%s C=%s  (期望 ~100)" % (a["fps"], c["fps"]))

    out = {"combos": res, "summary": summary, "dwell": args.dwell}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("JSON -> %s" % args.json)


if __name__ == "__main__":
    main()
