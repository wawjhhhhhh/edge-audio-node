# -*- coding: utf-8 -*-
"""Sample the console /api/state for N seconds and write a CSV log.

Reproducible evidence for the metrics table (frame rate, level, loss counters).
Usage:  python sample_console_state.py [seconds] [outfile]
"""
import json
import sys
import time
import urllib.request
import csv
import datetime

BASE = "http://127.0.0.1:8770"


def post(op, **kw):
    body = json.dumps({"op": op, **kw}).encode()
    req = urllib.request.Request(BASE + "/api/cmd", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=5) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if out is None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = f"../logs/console_sample_{stamp}.csv"

    print("listen ->", post("listen", v=True))
    time.sleep(1.0)

    rows = []
    t0 = time.time()
    while time.time() - t0 < secs:
        s = state()
        link = s.get("link") or {}
        rows.append({
            "t_s": round(time.time() - t0, 2),
            "board_online": s.get("board", {}).get("online"),
            "fps": link.get("fps"),
            "packets": link.get("packets"),
            "lost": link.get("lost"),
            "under": link.get("under"),
            "dbfs": link.get("dbfs"),
            "mode": (s.get("pl") or {}).get("mode"),
            "gain": (s.get("pl") or {}).get("gain"),
        })
        print(rows[-1])
        time.sleep(0.5)

    post("listen", v=False)

    ok = [r for r in rows if isinstance(r["fps"], (int, float))]
    if ok:
        import statistics
        fps = [r["fps"] for r in ok]
        db = [r["dbfs"] for r in ok if isinstance(r["dbfs"], (int, float))]
        print(f"\nsamples={len(ok)} fps mean={statistics.mean(fps):.2f} "
              f"min={min(fps):.2f} max={max(fps):.2f}")
        if db:
            print(f"dbfs mean={statistics.mean(db):.1f} min={min(db):.1f} max={max(db):.1f}")
        first, last = ok[0], ok[-1]
        if first["packets"] is not None and last["packets"] is not None:
            dt = last["t_s"] - first["t_s"]
            dp = last["packets"] - first["packets"]
            print(f"packets delta={dp} over {dt:.2f}s -> {dp/dt:.2f} fps")

    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("written:", out)


if __name__ == "__main__":
    main()
