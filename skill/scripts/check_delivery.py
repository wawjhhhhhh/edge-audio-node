#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付包结构自检：路径 ASCII、必需文件、metrics 表头、密钥是否泄漏。

用法：
    python skill/scripts/check_delivery.py [包根目录]
不给参数时，默认取本脚本所在目录向上两级（即包根）。

只读，不改任何文件。全部通过时退出码 0，有 FAIL 时退出码 1。
"""

from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path

REQUIRED = [
    "README.md",
    "docs/architecture.md",
    "docs/interface.md",
    "docs/hardware_setup.md",
    "docs/known_issues.md",
    "metrics/metrics.csv",
    "metrics/README.md",
    "skill/README.md",
    "fpga/build/build_eth_audio.tcl",
    "fpga/build/eth_audio_pins.xdc",
    "fpga/src/rtl/audio_axis_core.v",
    "fpga/src/rtl/afifo.v",
    "host/firmware/app_ea_main.c",
]

METRICS_HEADER = [
    "指标名称", "类别", "数值", "单位", "测量条件", "测试次数·时长", "证据文件",
]

# 不该出现在交付包里的东西
FORBIDDEN_NAMES = {"key.txt", "id_rsa", ".env"}
# 这些目录在任何层级都不该有
FORBIDDEN_ANYWHERE = {"__pycache__", ".git", ".webprofile", ".webprofile2"}
# 这些只可能在包根出现（fpga/build 是合法目录，不要误判）
FORBIDDEN_AT_ROOT = {"dist"}

fails: list[str] = []
warns: list[str] = []


def ok(msg: str) -> None:
    print(f"PASS  {msg}")


def bad(msg: str) -> None:
    fails.append(msg)
    print(f"FAIL  {msg}")


def warn(msg: str) -> None:
    warns.append(msg)
    print(f"WARN  {msg}")


def is_ascii_name(name: str) -> bool:
    try:
        name.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def check_names(root: Path) -> None:
    total = 0
    bad_names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            total += 1
            if not is_ascii_name(d):
                bad_names.append(os.path.join(dirpath, d))
        for f in filenames:
            total += 1
            if not is_ascii_name(f):
                bad_names.append(os.path.join(dirpath, f))
    if bad_names:
        for p in bad_names[:20]:
            bad(f"非 ASCII 路径：{os.path.relpath(p, root)}")
        if len(bad_names) > 20:
            bad(f"...还有 {len(bad_names) - 20} 个非 ASCII 路径")
    else:
        ok(f"路径全部为 ASCII（{total} 个条目）")


def check_required(root: Path) -> None:
    missing = [r for r in REQUIRED if not (root / r).exists()]
    if missing:
        for m in missing:
            bad(f"缺少必需文件：{m}")
    else:
        ok(f"必需文件齐全（{len(REQUIRED)} 项）")


def check_metrics(root: Path) -> None:
    p = root / "metrics" / "metrics.csv"
    if not p.exists():
        return
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        bad("metrics.csv 是空的")
        return
    header = [h.strip() for h in rows[0]]
    if header == METRICS_HEADER:
        ok("metrics.csv 表头与赛题要求一致")
    else:
        bad(f"metrics.csv 表头不符：{header}")
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    untested = [r for r in body if len(r) > 1 and r[1].strip() == "未测"]
    ok(f"metrics.csv 共 {len(body)} 行指标，其中「未测」{len(untested)} 行")
    if untested:
        for r in untested:
            print(f"      未测：{r[0]}（{r[5] if len(r) > 5 else ''}）")
    # 每行都得有证据或写明为什么没有
    for i, r in enumerate(body, start=2):
        if len(r) < 7:
            bad(f"metrics.csv 第 {i} 行字段不足（{len(r)}/7）")
        elif r[1].strip() != "未测" and not r[6].strip():
            bad(f"metrics.csv 第 {i} 行「{r[0]}」没有证据文件")


def check_forbidden(root: Path) -> None:
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        rel = os.path.relpath(dirpath, root)
        for d in dirnames:
            full = os.path.join(rel, d) + "/"
            if d in FORBIDDEN_ANYWHERE or d.startswith(".webprofile"):
                hits.append(full)
            elif rel == "." and d in FORBIDDEN_AT_ROOT:
                hits.append(full)
        for f in filenames:
            if f.lower() in FORBIDDEN_NAMES:
                hits.append(os.path.join(rel, f))
    if hits:
        for h in hits:
            bad(f"不该打包的东西：{h}")
    else:
        ok("未发现密钥/缓存/构建产物")


def check_bits(root: Path) -> None:
    bits = list(root.glob("fpga/build/output/*.bit"))
    if not bits:
        bad("没有找到 .bit")
        return
    for b in bits:
        size = b.stat().st_size
        if size < 1_000_000:
            bad(f"{b.name} 只有 {size} B，不像是完整的比特流")
        else:
            ok(f"{b.name} {size:,} B")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(f"检查目录：{root}\n")
    if not root.exists():
        print(f"FAIL  目录不存在：{root}")
        return 1
    check_names(root)
    check_required(root)
    check_metrics(root)
    check_forbidden(root)
    check_bits(root)
    n_files = sum(len(fs) for _, _, fs in os.walk(root))
    n_bytes = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    print(f"\n共 {n_files} 个文件，{n_bytes / 1048576:.2f} MB")
    print(f"结果：FAIL {len(fails)} / WARN {len(warns)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
