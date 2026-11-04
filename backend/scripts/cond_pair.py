"""条件配对：只在特定 regime 段（弱市/强市）上做同段配对。

复用 ``scripts/random_dist.py`` 的 ``_run_segment`` / ``_stats`` / ``_print_report``，
但段抽样改为「只接受 regime 段」：段起点即处于目标 regime，且段内该 regime 日占比 ≥ --min-share。

目的：验证「集中持仓是否在弱市更优、强市更差」（记录 #104 §6 的开放假设）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\cond_pair.py --regime bear --universe 300 --tag bear
    .venv\\Scripts\\python.exe scripts\\cond_pair.py --regime bull --universe 300 --tag bull
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics  # noqa: F401  (random_dist 内部用到)
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
import random_dist as RD  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402


def _trading_dates() -> list[str]:
    return RD._trading_dates()


def _regime_mask(dates: list[str], kind: str) -> dict[str, bool]:
    """上证 close 相对 MA60：bear = 下方，bull = 上方。"""
    idx = kline_cache.get("idx_sh000001")
    if idx is None or idx.empty:
        raise SystemExit("缺少上证指数 K 线")
    s = pd.Series(idx["close"].values, index=[str(d)[:10] for d in idx["date"]])
    ma60 = s.rolling(60).mean()
    if kind == "bear":
        flag = s < ma60
    elif kind == "bull":
        flag = s > ma60
    else:
        raise SystemExit(f"未知 regime {kind}")
    return {d: bool(flag.get(d, False)) for d in dates}


def _segments(dates, span, draws, seed, mask, min_share) -> list[tuple[str, str]]:
    starts = list(range(len(dates) - span))
    rng = random.Random(seed)
    rng.shuffle(starts)
    chosen: list[tuple[int, int]] = []
    for i in starts:
        seg = dates[i : i + span]
        if not mask.get(seg[0], False):
            continue
        if sum(1 for d in seg if mask.get(d, False)) / span < min_share:
            continue
        j = i + span
        if any(not (j <= a or i >= b) for a, b in chosen):
            continue
        chosen.append((i, j))
        if len(chosen) >= draws:
            break
    chosen.sort()
    return [(dates[i], dates[j]) for i, j in chosen]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", default="bear", choices=["bear", "bull"])
    ap.add_argument("--draws", type=int, default=20)
    ap.add_argument("--span", type=int, default=21)
    ap.add_argument("--universe", type=int, default=300)
    ap.add_argument("--profiles", default="cur10,conc4")
    ap.add_argument("--baseline", default="cur10")
    ap.add_argument("--seed", type=int, default=20261028)
    ap.add_argument("--min-share", type=float, default=0.5,
                    help="段内目标 regime 日占比下限")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    names = [n.strip() for n in args.profiles.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise SystemExit(f"未知方案 {unknown}")
    if args.baseline not in names:
        raise SystemExit("baseline 必须在 profiles 里")

    dates = _trading_dates()
    mask = _regime_mask(dates, args.regime)
    segs = _segments(dates, args.span, args.draws, args.seed, mask, args.min_share)
    if not segs:
        raise SystemExit("抽不到足够的 regime 段（放宽 --min-share 或 --draws）")
    print(
        f"regime={args.regime} | 段数 {len(segs)}（要求 {args.draws}）"
        f" | 每段 {args.span} 日 | 池 Top{args.universe} | 方案 {names}",
        flush=True,
    )
    for i, (s, e) in enumerate(segs, 1):
        print(f"  段{i:>2} {s} ~ {e}", flush=True)

    tasks = [(i, s, e, names, args.universe, None) for i, (s, e) in enumerate(segs, 1)]
    jobs = max(1, min(args.jobs, len(tasks)))
    t0 = time.time()
    if jobs == 1:
        results = [RD._run_segment(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(RD._run_segment, tasks))
    results.sort(key=lambda r: r["tag"])
    RD._print_report(results, names, args.baseline)

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else f"_{args.regime}"
    out = os.path.join(log_dir, f"cond_pair{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "regime": args.regime,
            "seed": args.seed,
            "span": args.span,
            "universe": args.universe,
            "profiles": {n: PROFILES[n] for n in names},
            "baseline": args.baseline,
            "segments": results,
            "stats": {
                n: RD._stats([s["profiles"][n]["excess"] for s in results if n in s["profiles"]])
                for n in names
            },
            "elapsed": round(time.time() - t0, 1),
        }, fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}（耗时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
