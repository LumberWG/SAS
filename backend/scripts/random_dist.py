"""随机段分布检验：多段不重叠抽样 × 多方案配对比较。

与 ``scripts/random_month.py`` 的区别：
  - 那个脚本是「随便挑一个月看看会怎么打」，重点是逐日明细，只跑一个方案；
  - 这里是**统计验收**：多段（默认 20 段）、**互不重叠**、**同一个种子下多方案配对**，
    只输出分布（中位/分位/正超额占比）与配对差值。

为什么必须不重叠：2023-01 至今约 900 个交易日，若允许重叠，抽 60 段看起来像 60 个样本，
实际独立信息只有十来个（记录 #7 的教训是「均值被一两个暴涨月带跑」）。
不重叠 + 配对能同时压掉「市况噪声」与「路径相关」两个坑。

为什么配对：所有方案跑**同一批段**，比较的是同一段时间内的差值，
普涨月的基准抬升对双方一样，差值才是选股/择时的净效应。

冻结协议（重要）：参数与方案在抽段之前定死，跑完只看分布，**不回头改参数再跑**。
否则随机段就变成新的训练集，和记录 #15/#26 翻车的原因完全一样。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\random_dist.py --draws 20 --seed 20261028
    .venv\\Scripts\\python.exe scripts\\random_dist.py --profiles default,rev_inv --span 21
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402

#: 起点下限：``_load_klines`` 会丢弃「截至 end_date 不足 120 根」的股票，
#: 而 K 线从 2022-09 开始，所以 end_date 至少要到 2023-04；span=21 时起点取 2023-03。
#: （E37 首轮有一段 2023-01-17 起因此整段失败，见 BACKTEST_LOG 记录 #27。）
MIN_START = "2023-03-01"


def _trading_dates() -> list[str]:
    idx = kline_cache.get("idx_sh000001")
    if idx is None or idx.empty:
        raise SystemExit("缺少上证指数 K 线（idx_sh000001），无法确定交易日历")
    return [str(d)[:10] for d in idx["date"]]


def _segments(dates: list[str], span: int, draws: int, seed: int) -> list[tuple[str, str]]:
    """随机抽 ``draws`` 段、**两两不重叠**的区间（起点加 span 个交易日）。

    实现：把可用起点打乱后贪心接受，命中「与已选区间不重叠」才收。
    区间不足时返回能凑出的最大数量（调用方需报告实际段数）。
    """
    starts = [i for i in range(len(dates) - span) if dates[i] >= MIN_START]
    if not starts:
        return []
    rng = random.Random(seed)
    rng.shuffle(starts)
    chosen: list[tuple[int, int]] = []
    for i in starts:
        j = i + span
        if any(not (j <= a or i >= b) for a, b in chosen):
            continue  # 与已选段重叠（含首尾相接）
        chosen.append((i, j))
        if len(chosen) >= draws:
            break
    chosen.sort()
    return [(dates[i], dates[j]) for i, j in chosen]


def _run_segment(task: tuple) -> dict:
    """单段 × 全部方案（子进程执行；同段内方案复用 `_prepare_cached`）。"""
    tag, start, end, names, universe, capital = task
    t0 = time.time()
    out: dict[str, dict] = {}
    for name in names:
        params = PipelineParams(
            start_date=start, end_date=end, universe_size=universe,
            initial_capital=capital, **PROFILES[name],
        )
        try:
            m = _run_sync(params)["metrics"]
        except Exception as exc:  # noqa: BLE001 - 单段失败不影响其它段
            print(f"[{tag}] {name} 失败：{exc}", flush=True)
            continue
        out[name] = {
            "total_return": float(m.get("total_return") or 0.0),
            "benchmark_return": float(m.get("benchmark_return") or 0.0),
            "excess": round(float(m.get("total_return") or 0.0)
                            - float(m.get("benchmark_return") or 0.0), 2),
            "max_drawdown": float(m.get("max_drawdown") or 0.0),
            "trade_count": int(m.get("trade_count") or 0),
            "win_rate": float(m.get("win_rate") or 0.0),
            "avg_pnl_pct": float(m.get("avg_pnl_pct") or 0.0),
        }
    print(
        f"[{tag}] {start}~{end} "
        + " | ".join(f"{n} {v['excess']:+.2f}%" for n, v in out.items())
        + f"  ({time.time() - t0:.0f}s)",
        flush=True,
    )
    return {"tag": tag, "start": start, "end": end, "profiles": out}


def _stats(values: list[float]) -> dict:
    """分布统计：中位/25/75 分位/最差/正比例（样本少时不追求更多指标）。"""
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 2),
        "p25": round(ordered[max(0, int(len(ordered) * 0.25) - 1)], 2),
        "p75": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))], 2),
        "worst": round(ordered[0], 2),
        "best": round(ordered[-1], 2),
        "mean": round(sum(ordered) / len(ordered), 2),
        "positive_rate": round(sum(1 for v in ordered if v > 0) / len(ordered) * 100, 1),
    }


def _print_report(segments: list[dict], names: list[str], baseline: str) -> None:
    print("\n### 单方案分布（每段相对等权基准的超额）\n")
    print("| 方案 | 段数 | 中位超额 | 25 分位 | 75 分位 | 最差段 | 最好段 | 超额>0 比例 | 中位胜率 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in names:
        exc = [s["profiles"][name]["excess"] for s in segments if name in s["profiles"]]
        wr = [s["profiles"][name]["win_rate"] for s in segments if name in s["profiles"]]
        st = _stats(exc)
        if not st:
            print(f"| `{name}` | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
            continue
        print(
            f"| `{name}` | {st['n']} | {st['median']:+.2f}% | {st['p25']:+.2f}% "
            f"| {st['p75']:+.2f}% | {st['worst']:+.2f}% | {st['best']:+.2f}% "
            f"| {st['positive_rate']:.1f}% | {statistics.median(wr):.1f}% |"
        )

    others = [n for n in names if n != baseline]
    if not others:
        return
    print(f"\n### 配对差值（相对 `{baseline}`，同一段内相减）\n")
    print("| 方案 | 段数 | 中位差值 | 25 分位 | 差值>0 段数占比 |")
    print("| --- | --- | --- | --- | --- |")
    for name in others:
        diffs = [
            s["profiles"][name]["excess"] - s["profiles"][baseline]["excess"]
            for s in segments
            if name in s["profiles"] and baseline in s["profiles"]
        ]
        st = _stats(diffs)
        if not st:
            print(f"| `{name}` | 0 | n/a | n/a | n/a |")
            continue
        print(
            f"| `{name}` | {st['n']} | {st['median']:+.2f}% | {st['p25']:+.2f}% "
            f"| {st['positive_rate']:.1f}% |"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draws", type=int, default=20, help="段数（不重叠，实际可能更少）")
    parser.add_argument("--span", type=int, default=21, help="每段交易日数（21≈一个月）")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN")
    parser.add_argument("--capital", type=float, default=None, help="缺省取模拟盘配置")
    parser.add_argument("--profiles", default="default,rev_inv,pct10", help="逗号分隔的方案名")
    parser.add_argument("--baseline", default="default", help="配对比较的基准方案")
    parser.add_argument("--seed", type=int, default=20261028, help="抽样种子（冻结，便于复现）")
    parser.add_argument("--jobs", type=int, default=4, help="并行段数")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    args = parser.parse_args()

    names = [n.strip() for n in args.profiles.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise SystemExit(f"未知方案 {unknown}，可选 {list(PROFILES)}")
    if args.baseline not in names:
        raise SystemExit(f"基准方案 {args.baseline} 必须在 --profiles 里")

    dates = _trading_dates()
    segs = _segments(dates, args.span, args.draws, args.seed)
    if not segs:
        raise SystemExit("可用区间不足，无法抽样（检查 K 线覆盖范围）")
    print(
        f"种子 {args.seed} | 段数 {len(segs)}（要求 {args.draws}，不重叠）"
        f" | 每段 {args.span} 个交易日 | 池 Top{args.universe} | 方案 {names}",
        flush=True,
    )
    for i, (s, e) in enumerate(segs, 1):
        print(f"  第 {i:>2} 段 {s} ~ {e}", flush=True)

    tasks = [
        (i, s, e, names, args.universe, args.capital)
        for i, (s, e) in enumerate(segs, 1)
    ]
    jobs = max(1, min(args.jobs, len(tasks)))
    results: list[dict] = []
    t0 = time.time()
    if jobs == 1:
        for task in tasks:
            results.append(_run_segment(task))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(_run_segment, tasks))
    results.sort(key=lambda r: r["tag"])

    _print_report(results, names, args.baseline)

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"random_dist{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "seed": args.seed,
            "span": args.span,
            "universe": args.universe,
            "profiles": {n: PROFILES[n] for n in names},
            "baseline": args.baseline,
            "segments": results,
            "stats": {
                n: _stats([s["profiles"][n]["excess"] for s in results if n in s["profiles"]])
                for n in names
            },
            "elapsed": round(time.time() - t0, 1),
        }, fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}（耗时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
