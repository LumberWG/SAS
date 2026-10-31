"""回归测试：同一区间/多窗口跑多组参数方案，输出对照表（**不改线上配置**）。

耗时结构（实测 Top300、B 窗口）：读 5000+ parquet 12s + 成交额排名 6s +
指标 14s + 选股信号 23s ≈ 55s，交易模拟每组只要 1~25s。所以提速只有两条路：
  1. 同一窗口内多方案复用准备结果（``_prepare_cached``，已默认）；
  2. 多窗口**并行**跑（``--jobs``），墙钟时间 ≈ 最慢的那个窗口。
默认并行度由 ``scripts.search.auto_jobs`` 给出（min(窗口数, CPU//2, 6)）：
6 个窗口一轮跑完，墙钟 ≈ 最慢窗口 ~2 分钟，又不会把 16 核机器打满
（每进程常驻约 0.5~1GB）。

用法（在 backend 目录）:
    .venv\\Scripts\\python.exe scripts\\regression.py --windows A,B,C,D --profiles default,th55
    .venv\\Scripts\\python.exe scripts\\regression.py --start 2026-06-01 --end 2026-10-23 --jobs 4
    .venv\\Scripts\\python.exe scripts\\regression.py --windows E,F,A,B,C,D --jobs 1   # 串行复现

结果：
  - 控制台打印 markdown 表格（可直接粘进 BACKTEST_LOG.md）
  - JSON 落到 data/logs/regression_<窗口名|end>_<tag>.json（含每组参数、指标、复盘摘要）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.search import auto_jobs, resolve_windows  # noqa: E402


def _run_window(task: tuple) -> tuple[str, list[dict], dict]:
    """单窗口任务（子进程执行）：跑完该窗口下的全部方案。

    Args:
        task: ``(窗口名, 开始, 结束, 方案名列表, 股票池, 初始资金, 最大持仓)``

    Returns:
        ``(窗口名, rows, details)``
    """
    wname, start, end, names, universe, capital, max_positions = task
    rows: list[dict] = []
    details: dict[str, dict] = {}
    for name in names:
        # profile 允许覆盖脚本级默认（否则同一轮里没法对比不同股票池 / 持仓数：
        # 以前 `universe_size=universe` 与 `**PROFILES[name]` 同时出现会直接 TypeError）。
        # 只在该键**没有**被 profile 指定时才填脚本参数，避免把 profile 的值顶掉。
        overrides = dict(PROFILES[name])
        overrides.setdefault("universe_size", universe)
        if capital is not None:
            overrides.setdefault("initial_capital", capital)
        if max_positions is not None:
            overrides.setdefault("max_positions", max_positions)
        params = PipelineParams(start_date=start, end_date=end, **overrides)
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
    return wname, rows, details


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
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="并行窗口数，0=自动（见 search.auto_jobs），1=串行",
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

    jobs = args.jobs if args.jobs > 0 else auto_jobs(len(ranges))
    jobs = max(1, min(jobs, len(ranges)))
    tasks = [
        (wname, start, end, names, args.universe, args.capital, args.max_positions)
        for wname, start, end in ranges
    ]

    rows: list[dict] = []
    details: dict[str, dict] = {}
    total_start = time.time()
    print(
        f"共 {len(ranges)} 个窗口 × {len(names)} 个方案，并行度 {jobs}"
        f"（每个进程负责一个窗口，窗口内方案复用准备结果）",
        flush=True,
    )

    if jobs == 1:
        for task in tasks:
            _, w_rows, w_details = _run_window(task)
            rows.extend(w_rows)
            details.update(w_details)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for wname, w_rows, w_details in pool.map(_run_window, tasks):
                rows.extend(w_rows)
                details.update(w_details)

    if not rows:
        raise SystemExit("没有成功的方案，检查数据与参数")

    # 按传入窗口顺序输出（并行返回顺序不定）
    order = {f"{w}": i for i, (w, _, _) in enumerate(ranges)}
    rows.sort(key=lambda r: (order.get(r["window"], 0), names.index(r["profile"])))

    # ---------- markdown 对照表 ----------
    multi = bool(args.windows)
    print("\n| 窗口 | 方案 | 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 平均/笔 |"
          if multi else "\n| 方案 | 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 平均/笔 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |" if multi
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
                "jobs": jobs,
                "rows": rows,
                "details": details,
                "elapsed_total": round(time.time() - total_start, 1),
            },
            fh, ensure_ascii=False, indent=2, default=float,
        )
    print(f"\n结果已保存：{os.path.abspath(out)}"
          f"（总耗时 {round(time.time() - total_start, 1)}s，并行度 {jobs}）")


if __name__ == "__main__":
    main()
