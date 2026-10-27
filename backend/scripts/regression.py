"""回归测试：同一区间跑多组参数方案，输出对照表（**不改线上配置**）。

数据准备与指标计算只做一次，后续方案复用缓存，因此多跑几组只增加交易模拟时间。

用法（在 backend 目录）:
    .venv\\Scripts\\python.exe scripts\\regression.py --start 2026-06-01 --end 2026-10-23
    .venv\\Scripts\\python.exe scripts\\regression.py --profiles default,th55,hold3

结果：
  - 控制台打印 markdown 表格（可直接粘进 BACKTEST_LOG.md）
  - JSON 落到 data/logs/regression_<end>_<tag>.json（含每组参数、指标、复盘摘要）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.search import resolve_windows  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-06-01")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN")
    parser.add_argument("--capital", type=float, default=None, help="缺省取模拟盘配置")
    parser.add_argument("--max-positions", type=int, default=None, help="缺省取模拟盘配置")
    parser.add_argument("--profiles", default=",".join(PROFILES), help="逗号分隔的方案名")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    parser.add_argument(
        "--windows", default="",
        help="按命名窗口跑多个区间（如 A,B,C,D），给了就不看 --start/--end",
    )
    args = parser.parse_args()

    ranges = (
        [(n, s, e) for n, (s, e) in resolve_windows(args.windows).items()]
        if args.windows
        else [("", args.start, args.end)]
    )

    names = [n.strip() for n in args.profiles.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise SystemExit(f"未知方案: {unknown}，可选: {list(PROFILES)}")

    rows: list[dict] = []
    details: dict[str, dict] = {}
    total_start = time.time()

    # 窗口外层循环：`_prepare_cached` 只缓存最近一组，同窗口的方案连续跑才能命中
    for wname, start, end in ranges:
        base = {
            "start_date": start,
            "end_date": end,
            "universe_size": args.universe,
            "initial_capital": args.capital,
            "max_positions": args.max_positions,
        }
        for name in names:
            params = PipelineParams(**{**base, **PROFILES[name]})
            t0 = time.time()
            try:
                result = _run_sync(params)
            except Exception as exc:  # noqa: BLE001 - 单组失败不影响其它组
                print(f"[{wname or start}/{name}] 失败：{exc}", flush=True)
                continue
            elapsed = round(time.time() - t0, 1)
            m = result["metrics"]
            benchmark = float(m.get("benchmark_return") or 0)
            total = float(m.get("total_return") or 0)
            row = {
                "window": wname,
                "range": f"{start}~{end}",
                "profile": name,
                "total_return": total,
                "benchmark_return": benchmark,
                "excess": round(total - benchmark, 2),
                "max_drawdown": float(m.get("max_drawdown") or 0),
                "trade_count": int(m.get("trade_count") or 0),
                "win_rate": float(m.get("win_rate") or 0),
                "avg_pnl_pct": float(m.get("avg_pnl_pct") or 0),
                "final_equity": float(m.get("final_equity") or 0),
                "elapsed": elapsed,
            }
            rows.append(row)
            details[f"{wname}|{name}" if wname else name] = {
                "overrides": PROFILES[name],
                "params": result["params"],
                "metrics": m,
                "review_summary": result["review"]["summary"],
                "suggestions": result["review"]["suggestions"],
                "elapsed": elapsed,
            }
            print(
                f"[{wname or start}/{name}] {total:+.2f}% | 基准 {benchmark:+.2f}% "
                f"| 超额 {row['excess']:+.2f}% | 回撤 {row['max_drawdown']:.2f}% "
                f"| {row['trade_count']} 笔 | 胜率 {row['win_rate']:.1f}% | {elapsed}s",
                flush=True,
            )

    if not rows:
        raise SystemExit("没有成功的方案，检查数据与参数")

    # ---------- markdown 对照表 ----------
    multi = bool(args.windows)
    head = "| 窗口 | 方案 |" if multi else "| 方案 |"
    print(f"\n{head} 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 平均/笔 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |" if multi
          else "| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        prefix = f"| {r['window']} | {r['profile']} " if multi else f"| {r['profile']} "
        print(
            f"{prefix}| {r['total_return']:+.2f}% | {r['benchmark_return']:+.2f}% "
            f"| {r['excess']:+.2f}% | {r['max_drawdown']:.2f}% | {r['trade_count']} "
            f"| {r['win_rate']:.1f}% | {r['avg_pnl_pct']:+.2f}% |"
        )

    # ---------- 落盘 ----------
    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    name = "windows" if multi else args.end
    out = os.path.join(log_dir, f"regression_{name}{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "windows": [{"name": n, "start": s, "end": e} for n, s, e in ranges],
                "universe": args.universe,
                "rows": rows,
                "details": details,
                "elapsed_total": round(time.time() - total_start, 1),
            },
            fh, ensure_ascii=False, indent=2, default=float,
        )
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
