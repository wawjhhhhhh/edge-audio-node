#!/usr/bin/env python
"""A/B 量测：把板子在不同 dsp 模式之间来回切，每个模式停几秒，
从控制台 /api/state + /api/spectrum 取真值，打印成一张表。

用法：
    python .tmp\\ab_pl.py                # 默认 0 / 3 来回两轮
    python .tmp\\ab_pl.py 0 3 1 2        # 指定模式顺序
    python .tmp\\ab_pl.py --dwell 6 0 3  # 每档停 6 秒

判据（PL 谱减 vs 直通）：
    fps 不下降（都应 ≈100，这是 PL 干活不占 ARM 的证据）
    噪声底 floor_db 下降 3–12 dB，rms 下降若干 dB
    under_rate 不异常升高

说明：只读控制台，不直接开 50505/50508 端口（避免和正在跑的控制台抢端口）。
"""
import argparse
import json
import socket
import statistics
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8770"
BOARD_IP = "169.254.191.50"
BCAST = "169.254.255.255"
PORT_CTL = 50507

NAMES = {"0": "bypass 直通", "1": "RNNoise(ARM)", "2": "噪声门", "3": "PL 谱减"}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))


def send_mode(m):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for dst in (BOARD_IP, BCAST):
        try:
            s.sendto(m.encode(), (dst, PORT_CTL))
        except OSError:
            pass
    s.close()


def sample(dwell):
    """在 dwell 秒内持续采样，返回中位数（避开切换瞬间）。"""
    st, sp = [], []
    t0 = time.time()
    while time.time() - t0 < dwell:
        try:
            st.append(get("/api/state"))
        except Exception:
            pass
        try:
            sp.append(get("/api/spectrum"))
        except Exception:
            pass
        time.sleep(0.25)
    st = st[len(st) // 4:] or st
    sp = sp[len(sp) // 4:] or sp

    def med(rows, key, default=None):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return statistics.median(vals) if vals else default

    return {
        "fps": med(st, "fps"),
        "packets": med(st, "packets"),
        "under_rate": med(st, "under_rate"),
        "dsp": st[-1].get("dsp") if st else None,
        "dsp_src": st[-1].get("dsp_source") if st else None,
        "board": (st[-1].get("board") or {}).get("mode") if st else None,
        "rms": med(sp, "rms_dbfs"),
        "floor": med(sp, "floor_db"),
        "snr": med(sp, "snr_db"),
        "peak_hz": med(sp, "peak_hz"),
    }


def fmt(v, digits=1, unit=""):
    if v is None:
        return "  --  "
    if isinstance(v, str):
        return v
    return f"{v:.{digits}f}{unit}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("modes", nargs="*", default=["0", "3", "0", "3"])
    ap.add_argument("--dwell", type=float, default=5.0)
    args = ap.parse_args()

    try:
        st = get("/api/state")
    except Exception as e:
        print(f"控制台没响应（{e}）——先在 work\\pc_app 里起 run_web.py")
        return 1
    if not st.get("listening"):
        print("控制台没在收听（listening=false）——先点「开始收听」，否则没有音频可分析")
        return 1
    if not (st.get("board") or {}).get("online"):
        print("板子状态包没在到达（board.online=false）——检查网线/上电")
        return 1

    print(f"控制台 {BASE} 已连板；每档 {args.dwell:.0f} s\n")
    rows = []
    for m in args.modes:
        send_mode(m)
        time.sleep(1.2)
        s = sample(args.dwell)
        rows.append((m, s))
        print(f"  [{m}] {NAMES.get(m, '?'):12s} fps={fmt(s['fps'])} rms={fmt(s['rms'])}"
              f" floor={fmt(s['floor'])} snr={fmt(s['snr'])} under={fmt(s['under_rate'])}"
              f" board={s['board']}")

    base = next((s for m, s in rows if m == "0"), None)
    print("\n模式 | 上行fps | rms dBFS | 噪声底 dBFS | 信噪比 | 相对直通(rms/底)")
    print("-----|--------|----------|-------------|--------|----------------")
    for m, s in rows:
        if base and base["rms"] and s["rms"]:
            d_rms = s["rms"] - base["rms"]
            d_floor = (s["floor"] - base["floor"]) if (s["floor"] and base["floor"]) else None
            rel = f"{d_rms:+.2f} / {d_floor:+.2f}" if d_floor is not None else f"{d_rms:+.2f} / --"
        else:
            rel = "--"
        print(f"  {m}  | {fmt(s['fps']):6s} | {fmt(s['rms'],2):8s} | {fmt(s['floor'],2):11s} | "
              f"{fmt(s['snr'],1):6s} | {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
