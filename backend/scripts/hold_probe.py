# -*- coding: utf-8 -*-
"""配对回测：缩短持有期能否放大「5 日反转」（纯研究，不改线上参数）。

依据
----
- #26 排序直测：20 日反转样本外翻车，但**5 日尺度反转是唯一两段同向的证据**
  （Top3 +0.02/+0.15 → +0.42/+0.72，Top10 +0.11/−0.09 → +0.76/+0.86，≈+0.7%/5 日）。
- #139：剔除 20 日最热 20% 在样本外失效，`max_hold_days` 这条线是唯一没被否掉的。
- 线上结构本来就是「短线 + 硬止损」（`max_hold_days=0` = 不设时间止损，靠止损/止盈走），
  如果 5 日反转真实存在，把持有期压到 3~5 日应该放大它；否则换手成本会直接吃掉。

方案
----
| 名称 | 覆盖 | 意图 |
| --- | --- | --- |
| `default` | — | 线上参数，对照基准 |
| `hold3` / `hold5` | `max_hold_days=3/5` | 只压缩持有期，不动打分 |
| `hold3_rev` / `hold5_rev` | 再加 `invert_score=True` | #26 建议的组合：反转打分 + 短持有 |
| `hold10` | `max_hold_days=10` | 反向对照：拉长是否更差（验证不是单纯换手效应） |

口径：A~J 全部 10 窗口、成交额 Top300、`entry_open_delay_minutes=5`（对齐线上/#108）。
同一窗口内多方案复用 `_prepare_cached`；`invert_score` 在 `_signals_key` 里，会多算一次 signals。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\hold_probe.py --windows A,B,C,D
    .venv\\Scripts\\python.exe scripts\\hold_probe.py --windows A,B,C,D,E,F,G,H,I,J --jobs 6 --tag all10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.search import auto_jobs, resolve_windows  # noqa: E402

PLANS: dict[str, dict] = {
    "default": {},
    "hold3": {"max_hold_days": 3},
    "hold5": {"max_hold_days": 5},
    "hold10": {"max_hold_days": 10},
    "hold3_rev": {"max_hold_days": 3, "invert_score": True},
    "hold5_rev": {"max_hold_days": 5, "invert_score": True},
}


def _run_window(task: tuple) -> tuple[str, list[dict], dict]:
    wname, start, end, names, universe, delay = task
    rows: list[dict] = []
    details: dict[str, dict] = {}
    for name in names:
        kwargs = dict(PLANS[name])
        kwargs.setdefault("universe_size", universe)
        kwargs.setdefault("entry_open_delay_minutes", delay)
        params = PipelineParams(start_date=start, end_date=end, **kwargs)
        t0 = time.time()
        try:
            result = _run_sync(params)
        except Exception as exc:  # noqa: BLE001 - 单组失败不影响其它组
            print(f"[{wname}/{name}] 失败：{exc}", flush=True)
            continue
        m = result["metrics"]
        bench = float(m.get("benchmark_return") or 0)
        total = float(m.get("total_return") or 0)
        row = {
            "window": wname, "range": f"{start}~{end}", "profile": name,
            "total_return": total, "benchmark_return": bench,
            "excess": round(total - bench, 2),
            "max_drawdown": float(m.get("max_drawdown") or 0),
            "trade_count": int(m.get("trade_count") or 0),
            "win_rate": float(m.get("win_rate") or 0),
            "avg_pnl_pct": float(m.get("avg_pnl_pct") or 0),
            "final_equity": float(m.get("final_equity") or 0),
            "elapsed": round(time.time() - t0, 1),
        }
        rows.append(row)
        details[f"{wname}|{name}"] = {"params": result["params"], "metrics": m,
                                      "review_summary": result["review"]["summary"]}
        print(f"[{wname}/{name}] {total:+.2f}% | 基准 {bench:+.2f}% "
              f"| 超额 {total - bench:+.2f}% | 回撤 {row['max_drawdown']:.2f}% "
              f"| {row['trade_count']} 笔 | 胜率 {row['win_rate']:.1f}% "
              f"| 每笔 {row['avg_pnl_pct']:+.2f}% | {row['elapsed']}s", flush=True)
    return wname, rows, details


def main() -> None:
    ap = argparse.ArgumentParser(description="持有期 × 反转 配对回测")
    ap.add_argument("--windows", default="A,B,C,D")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--universe", type=int, default=300, help="每日成交额 TopN（线上口径 300）")
    ap.add_argument("--profiles", default=",".join(PLANS), help="逗号分隔，见 PLANS")
    ap.add_argument("--delay", type=int, default=5)
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    ranges = ([(n, s, e) for n, (s, e) in resolve_windows(args.windows).items()]
              if args.windows else [("", args.start, args.end)])
    names = [n.strip() for n in args.profiles.split(",") if n.strip()]
    unknown = [n for n in names if n not in PLANS]
    if unknown:
        raise SystemExit(f"未知方案 {unknown}，可选 {list(PLANS)}")
    jobs = args.jobs if args.jobs > 0 else auto_jobs(len(ranges))
    jobs = max(1, min(jobs, len(ranges)))
    tasks = [(n, s, e, names, args.universe, args.delay) for n, s, e in ranges]

    print(f"共 {len(ranges)} 窗口 × {len(names)} 方案，并行 {jobs}；"
          f"池 Top{args.universe}，延迟 {args.delay} 分钟", flush=True)

    rows: list[dict] = []
    details: dict[str, dict] = {}
    t0 = time.time()
    if jobs == 1:
        for t in tasks:
            _, r, d = _run_window(t)
            rows.extend(r)
            details.update(d)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for _, r, d in pool.map(_run_window, tasks):
                rows.extend(r)
                details.update(d)
    if not rows:
        raise SystemExit("没有成功的方案")

    order = {n: i for i, (n, _, _) in enumerate(ranges)}
    rows.sort(key=lambda r: (order.get(r["window"], 0), names.index(r["profile"])))

    print("\n| 窗口 | 方案 | 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 每笔 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['window']} | {r['profile']} | {r['total_return']:+.2f}% "
              f"| {r['benchmark_return']:+.2f}% | {r['excess']:+.2f}% "
              f"| {r['max_drawdown']:.2f}% | {r['trade_count']} "
              f"| {r['win_rate']:.1f}% | {r['avg_pnl_pct']:+.2f}% |")

    # 配对差值 vs default
    others = [n for n in names if n != "default"]
    by_win: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_win.setdefault(r["window"], {})[r["profile"]] = r
    print("\n### 配对差值（方案 − default，同窗口同池，正=更优）\n")
    if others:
        print("| 窗口 | " + " | ".join(f"{n} 收益差" for n in others) + " |")
        print("| --- | " + " | ".join("---" for _ in others) + " |")
        stat: dict[str, list[float]] = {n: [] for n in others}
        for w in order:
            d = by_win.get(w, {}).get("default")
            cells = []
            for n in others:
                x = by_win.get(w, {}).get(n)
                if not d or not x:
                    cells.append("—")
                    continue
                dr = x["total_return"] - d["total_return"]
                stat[n].append(dr)
                cells.append(f"{dr:+.2f}pp")
            print(f"| {w} | " + " | ".join(cells) + " |")
        print()
        for n in others:
            v = np.asarray(stat[n], dtype=float)
            if not v.size:
                continue
            sd = float(v.std(ddof=1)) if v.size > 1 else 0.0
            t = float(v.mean() / (sd / np.sqrt(v.size))) if sd > 0 else float("nan")
            print(f"- `{n}`：中位 {np.median(v):+.2f}pp、均值 {v.mean():+.2f}pp、"
                  f"t={t:+.2f}、胜出 {int((v > 0).sum())}/{v.size}")

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"hold_probe{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"windows": [{"name": n, "start": s, "end": e} for n, s, e in ranges],
                   "universe": args.universe, "delay": args.delay,
                   "plans": {n: PLANS[n] for n in names},
                   "rows": rows, "details": details},
                  fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}（总耗时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
