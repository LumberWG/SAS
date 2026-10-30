# -*- coding: utf-8 -*-
"""补齐失败项：从日志里捞出失败代码，重试一次（已带 SDK 聚合补丁）。

背景：原版 SDK 遇到停牌分钟（high/low 为 None）会抛 TypeError，导致整只股票
取不到数据。补丁在 fsdb_safe_merge.py 里，本脚本通过 import
fetch_minutes_fsdb 自动加载补丁后重试。

用法：
    python scripts/repair_failed.py                # 补 5m 和 1m 的失败项
    python scripts/repair_failed.py --freq 5m      # 只补 5m
    python scripts/repair_failed.py --codes 002762,002719 --freq 5m
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fetch_minutes_fsdb as F  # noqa: E402  导入即加载聚合补丁

LOG = F.DATA_ROOT / "logs" / "minutes_all.log"
PAT = re.compile(r"\]\s+X\s+(\d{6})\b")


def codes_from_log(freq: str) -> list[str]:
    """日志里所有失败代码（去重保序）。无标签，两个周期共用，后面按目录现状过滤。"""
    if not LOG.exists():
        return []
    seen: list[str] = []
    s: set[str] = set()
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = PAT.search(line)
        if m:
            c = m.group(1)
            if c not in s:
                s.add(c)
                seen.append(c)
    return seen


def out_dir_for(freq: str) -> Path:
    return F.DATA_ROOT / ("minute1_fsdb" if freq == "1m" else "minute5_fsdb")


def repair(freq: str, codes: list[str], workers: int) -> int:
    d = out_dir_for(freq)
    d.mkdir(parents=True, exist_ok=True)
    done = F.load_done(d, freq)
    todo = [c for c in codes if c not in done and F.existing_rows(d / f"{c}.parquet") < F.MIN_ROWS]
    if not todo:
        F.log(f"[{freq}] 无待补代码")
        return 0

    F.log(f"[{freq}] 待补 {len(todo)} 只，并发 {workers}")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        fut = {ex.submit(F.process_one, c, d / f"{c}.parquet", freq, d, False): c for c in todo}
        for f in as_completed(fut):
            code = fut[f]
            try:
                status, detail = f.result()
            except Exception as e:  # noqa: BLE001
                status, detail = "fail", f"{type(e).__name__}: {str(e)[:70]}"
            if status in ("ok", "skip"):
                ok += 1
                if status == "ok":
                    F.log(f"[{freq}] 补齐 {code} {detail[0]} 行")
            else:
                fail += 1
                F.log(f"[{freq}] 仍失败 {code} {detail}")
    F.log(f"[{freq}] 补齐结束 ok={ok} fail={fail} 用时 {(time.time() - t0) / 60:.1f} 分钟")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐失败的分钟线")
    ap.add_argument("--freq", default="both", choices=["5m", "1m", "both"])
    ap.add_argument("--codes", default="", help="逗号分隔；留空则从日志提取")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = codes_from_log(args.freq)
    if not codes:
        F.log("没有需要补的代码")
        return 0

    freqs = ["5m", "1m"] if args.freq == "both" else [args.freq]
    for fq in freqs:
        repair(fq, codes, args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
