#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时序报告健康检查：no_clock / TIMING-17 / AFIFO 被拆成触发器 / WNS。

用法：
    python skill/scripts/check_timing_health.py [report 目录]
不给参数时，默认取 <包根>/fpga/report。

只读。发现真问题退出码 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_REL = Path("fpga") / "report"
UNCONSTRAINED_LIMIT = 100  # no_clock 引脚数超过这个就认为有问题

fails: list[str] = []
notes: list[str] = []


def ok(m: str) -> None:
    print(f"PASS  {m}")


def bad(m: str) -> None:
    fails.append(m)
    print(f"FAIL  {m}")


def info(m: str) -> None:
    notes.append(m)
    print(f"INFO  {m}")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def check_no_clock(rep: Path) -> None:
    rpt = rep / "timing_summary_routed.rpt"
    if not rpt.exists():
        bad(f"找不到 {rpt.name}")
        return
    text = read(rpt)
    m = re.search(r"no_clock\s*\((\d+)\)", text)
    if not m:
        info("报告里没有 no_clock 条目（可能该设计不需要检查）")
        return
    n = int(m.group(1))
    if n > UNCONSTRAINED_LIMIT:
        bad(f"no_clock = {n} 个引脚：有触发器域没有时钟约束（阈值 {UNCONSTRAINED_LIMIT}）")
        info("  这是本工程已知的问题，见 docs/known_issues.md 问题 1（修复方案 4 条）")
        # 找出根时钟引脚
        for line in text.splitlines():
            if "no_clock" in line and "pins under root clock pin" in line:
                info("  " + line.strip()[:160])
    else:
        ok(f"no_clock = {n}（阈值 {UNCONSTRAINED_LIMIT} 以内）")

    m17 = re.search(r"Non-clocked sequential cell\s*(\d+)?", text)
    if m17 and m17.group(1):
        info(f"TIMING-17 Non-clocked sequential cell 计数：{m17.group(1)}（报告可能截顶）")


def check_wns(rep: Path) -> None:
    rpt = rep / "timing_summary_routed.rpt"
    if not rpt.exists():
        return
    text = read(rpt)
    m = re.search(r"^\s*(-?\d+\.\d+)\s+.*?WHS", text, re.M)
    # 更稳的做法：找表头行之后的第一个数据行
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "WNS(ns)" in line and "WHS(ns)" in line:
            for nxt in lines[i + 1: i + 6]:
                parts = nxt.split()
                if len(parts) >= 2:
                    try:
                        wns = float(parts[0])
                        whs = float(parts[4]) if len(parts) > 4 else None
                    except ValueError:
                        continue
                    if wns < 0:
                        bad(f"WNS = {wns} ns（时序没收敛）")
                    else:
                        ok(f"WNS = {wns} ns")
                    if whs is not None and whs < 0:
                        bad(f"WHS = {whs} ns（保持时间违例）")
                    elif whs is not None:
                        ok(f"WHS = {whs} ns")
                    return
    info("没能从报告里解析出 WNS 数据行（格式可能变化）")


def check_fifo_inference(rep: Path) -> None:
    logs = list(rep.glob("*synth*.log")) + list(rep.glob("*ooc*.log"))
    # 综合日志也可能放在 metrics/logs/ 下
    extra = rep.parent.parent / "metrics" / "logs"
    if extra.exists():
        logs += list(extra.glob("*synth*.log")) + list(extra.glob("*ooc*.log"))
    logs = sorted(set(logs))
    pat_hit = False
    for lg in logs:
        text = read(lg)
        for pat in ("8-4767", "8-13159"):
            for line in text.splitlines():
                if pat in line:
                    bad(f"[{lg.name}] {line.strip()[:150]}")
                    pat_hit = True
    if not pat_hit:
        if logs:
            ok(f"{len(logs)} 份综合日志里没有 RAM 被拆成触发器（8-4767 / 8-13159）")
        else:
            info("没有找到综合日志，跳过 FIFO 推断检查")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) > 1:
        rep = Path(sys.argv[1]).resolve()
    else:
        rep = Path(__file__).resolve().parents[2] / DEFAULT_REL
    print(f"检查目录：{rep}\n")
    if not rep.exists():
        print(f"FAIL  目录不存在：{rep}")
        return 1
    check_no_clock(rep)
    check_wns(rep)
    check_fifo_inference(rep)
    print(f"\n结果：FAIL {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
