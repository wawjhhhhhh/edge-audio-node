"""一键 A/B 录音 + 客观对比（"证明包"里最硬的那一件）。

它做什么
    对每一档降噪模式：切过去 → 等它稳（默认 1.2 s）→ 从控制台抓 N 秒**它正在处理的
    那一路**（`GET /api/pcm`，也就是用户耳朵听到的样点）→ 存成 48 kHz/16 bit 立体声
    WAV → 用 BS.1770-4 算整体响度、用谱分析算噪声底。

为什么不用屏幕截图/听感描述
    答辩和验收时最容易被问的一句是"你怎么证明它真的降噪了"。一段 WAV + 一张
    响度/噪声底对照表，比"我听上去更干净"有效得多；而且 WAV 可以当场放给评审听。

用法
    # 板子在线、控制台在 8770 跑着、已经点过"开始收听"
    python ab_record.py --modes bypass pl --secs 5 --out ..\\..\\材料库\\04-实测记录\\AB

    # 没有板子时也能跑：控制台里有"模拟信号"，先开再跑
    python ab_record.py --sim --modes bypass pl --secs 4

判据
    * pl 相对 bypass：噪声底应降 ≥10 dB（板子实测约 22 dB），整体响度变化 <1.5 LU
      —— 降噪不该把声音整体变小，只该把"底下的沙沙声"拿走。
    * 如果噪声底几乎不变，先看串口状态行的 `bp=` 字段：`dsp=3` 时必须是 `bp=0`。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loudness import LoudnessMeter          # noqa: E402
from spectrum import Analyzer               # noqa: E402

BASE = "http://127.0.0.1:8770"
RATE = 48000


# ------------------------------------------------------------------ HTTP
def get_state(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/state", timeout=3) as r:
        return json.loads(r.read() or b"{}")


def post_cmd(base: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + "/api/cmd", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"{}")


def pull_pcm(base: str, cursor: int, secs: float) -> tuple[np.ndarray, int]:
    """从 cursor 开始抓 secs 秒，返回 (samples×2 int16, 新 cursor)。"""
    need = int(secs * RATE)
    out: list[np.ndarray] = []
    got = 0
    t_end = time.time() + secs + 3.0
    while got < need and time.time() < t_end:
        with urllib.request.urlopen(f"{base}/api/pcm?from={cursor}", timeout=3) as r:
            start = int(r.headers.get("X-Pcm-Start") or 0)
            total = int(r.headers.get("X-Pcm-Total") or 0)
            raw = r.read()
        if not raw:
            time.sleep(0.05)
            if total == cursor:
                continue
            continue
        b = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
        out.append(b)
        got += b.shape[0]
        cursor = start + b.shape[0]
        # 落后太多就跳到最新（录音里宁可少一段，也不要无限拖）
        if start + b.shape[0] < total - RATE:
            cursor = total
    if not out:
        return np.zeros((0, 2), dtype=np.int16), cursor
    data = np.concatenate(out, axis=0)
    return data[:need], cursor


def write_wav(path: Path, data: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.ascontiguousarray(data, dtype="<i2").tobytes())


# ------------------------------------------------------------------ 分析
def analyse(data: np.ndarray) -> dict:
    if data.size == 0:
        return {"lufs": None, "peak_db": None, "floor_db": None, "rms_db": None, "tone_hz": None}
    m = LoudnessMeter()
    for i in range(0, len(data), 480):
        m.feed(data[i:i + 480])
    an = Analyzer()
    for i in range(0, len(data), 480):
        an.feed(data[i:i + 480])
    s = an.snapshot()
    mono = data.astype(np.float64).mean(axis=1) / 32768.0
    rms = float(np.sqrt((mono ** 2).mean())) if mono.size else 0.0
    return {
        "lufs": m.integrated_db(),
        "peak_db": round(m.peak_db(), 1),
        "floor_db": s.get("floor_db"),
        "snr_db": s.get("snr_db"),
        "tone_hz": s.get("tone_hz"),
        "rms_db": round(20.0 * math.log10(max(1e-9, rms)), 1) if rms > 0 else None,
        "clips": s.get("clip"),
    }


def fmt(v, unit="", nd=1):
    if v is None:
        return "—"
    return f"{v:.{nd}f}{unit}"


def main() -> int:
    ap = argparse.ArgumentParser(description="一键 A/B 录音与客观对比")
    ap.add_argument("--modes", nargs="+", default=["bypass", "pl"],
                    help="要对比的模式（bypass/rnnoise/gate/pl）")
    ap.add_argument("--secs", type=float, default=5.0, help="每档录多久")
    ap.add_argument("--settle", type=float, default=1.2, help="切档后等多久再录")
    ap.add_argument("--out", default="", help="WAV 输出目录（默认 ./ab_out）")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--sim", action="store_true", help="没有板子：先让控制台开模拟信号")
    args = ap.parse_args()

    st = get_state(args.base)
    if args.sim:
        post_cmd(args.base, {"op": "sim", "v": True})
        time.sleep(0.6)
        st = get_state(args.base)
    if not st.get("listening"):
        print("控制台没在收听：先在界面上点「开始收听」（或带 --sim 用模拟信号）。")
        return 2
    if not st.get("link") == "ok" and not args.sim:
        print(f"链路状态 {st.get('link')!r}：没有音频包到达，先检查板子/网线。")
        return 2

    outdir = Path(args.out) if args.out else Path("ab_out")
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    cursor = int(get_state(args.base).get("pcm_total") or 0)
    for mode in args.modes:
        ack = post_cmd(args.base, {"op": "dsp", "v": mode})
        time.sleep(args.settle)
        st = get_state(args.base)
        cursor = int(st.get("pcm_total") or cursor)
        data, cursor = pull_pcm(args.base, cursor, args.secs)
        if data.size == 0:
            print(f"[{mode}] 没抓到音频（listening={st.get('listening')} link={st.get('link')}）")
            return 2
        path = outdir / f"ab_{mode}_{time.strftime('%H%M%S')}.wav"
        write_wav(path, data)
        a = analyse(data)
        rows.append((mode, a, path))
        print(f"[{mode}] {data.shape[0] / RATE:.1f} s -> {path.name}  "
              f"LUFS {fmt(a['lufs'])}  噪声底 {fmt(a['floor_db'],' dBFS')}  "
              f"峰值 {fmt(a['peak_db'],' dBFS')}  RMS {fmt(a['rms_db'],' dBFS')}")

    print()
    print(f"{'模式':10s} {'LUFS':>8s} {'噪声底':>10s} {'RMS':>9s} {'峰值':>9s} {'SNR':>7s} {'削波':>5s}")
    for mode, a, _ in rows:
        print(f"{mode:10s} {fmt(a['lufs']):>8s} {fmt(a['floor_db']):>10s} "
              f"{fmt(a['rms_db']):>9s} {fmt(a['peak_db']):>9s} {fmt(a['snr_db']):>7s} {a['clips']!s:>5s}")

    if len(rows) >= 2:
        base = rows[0][1]
        print()
        for mode, a, _ in rows[1:]:
            dl = (a["lufs"] - base["lufs"]) if (a["lufs"] is not None and base["lufs"] is not None) else None
            df = (a["floor_db"] - base["floor_db"]) if (a["floor_db"] is not None and base["floor_db"] is not None) else None
            verdict = ""
            if df is not None:
                verdict = "噪声底下降 %.1f dB" % (-df) if df < 0 else "噪声底升高 %.1f dB" % (-df)
            print(f"{mode} 相对 {rows[0][0]}：响度 {fmt(dl,' LU',2) if dl is not None else '—'}，"
                  f"噪声底 {fmt(df,' dB',2) if df is not None else '—'}  → {verdict}")
        # 判据只在真降噪模式上给，避免拿 bypass 和自己比
        print("\n判据：pl 相对 bypass 噪声底应降 ≥10 dB、响度差 <1.5 LU；"
              "若几乎不变，先看串口状态行 dsp=3 时 bp= 是不是 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
