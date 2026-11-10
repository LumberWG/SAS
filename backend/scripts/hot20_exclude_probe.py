# -*- coding: utf-8 -*-
"""配对回测：原打分不动，只在买入池里**剔除「20 日涨幅截面最高 pct」**（纯研究）。

依据：BACKTEST_LOG #138 —— 20 日周期 ret20 的秩 IC 为负（-0.05 ~ -0.08），
且分档表显示 Q1/Q2/Q3 无差别、只有 Q5（最涨 20%）塌陷（fwd20 均值 -0.00%、
中位 -2.35%）。所以本实验**不改打分方向**（#26 已证明「翻方向」样本外翻车），
只做尾部截断：保留 `default` 的排序与阈值，仅把当日最热的 pct 从候选里去掉。

口径
----
- 基线 `default`：线上参数（`PROFILES["default"]` = 无覆盖）。
- 实验 `nohotXX`：同一套参数 + 剔除「当日 **成交额 TopN 池内** `ret20 = close/close[-21]-1`
  的截面分位 > 1-pct」的股票。`ret20` 只用 T 日收盘，与选股同一时点，无未来函数。
- 两版**同窗口配对**，差值即实验的净影响；绝对收益与历史记录不可直接比
  （本脚本统一 `entry_open_delay_minutes=5`，与线上 `paper_open_cooldown_minutes` / #108 对齐）。

实现
----
不改 `app/backtest/pipeline.py`：先用 `_ensure_signals` 拿到原 signals，就地把
`prepared["signals"]` 换成过滤后的版本（`signals_key` 不变，`_simulate` 会直接用），
模拟完立刻换回原引用，避免污染 `_PREPARED` 模块级缓存。

用法（backend 目录）
----
    .venv\\Scripts\\python.exe scripts\\hot20_exclude_probe.py --windows A,B,C,D
    .venv\\Scripts\\python.exe scripts\\hot20_exclude_probe.py --windows E,F,G,H,I,J --jobs 6
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

from app.backtest.pipeline import (  # noqa: E402
    PipelineParams,
    _ensure_signals,
    _prepare_cached,
    _simulate,
)
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.search import auto_jobs, resolve_windows  # noqa: E402


def _ret20_filter(prepared: dict, params: PipelineParams, pct: float,
                  scope: str = "universe") -> dict[str, list[dict]]:
    """返回过滤后的 signals（剔除当日 ret20 截面最高的 pct）。"""
    signals = _ensure_signals(prepared, params)
    universe: dict[str, list[str]] = prepared["universe"]
    klines = prepared["klines"]
    pos_index: dict[str, dict[str, int]] = prepared["pos_index"]
    cache: dict[tuple[str, str], float] = {}

    def r20(code: str, date: str) -> float:
        key = (code, date)
        hit = cache.get(key)
        if hit is not None:
            return hit
        val = np.nan
        idx = pos_index.get(code, {}).get(date)
        if idx is not None and idx >= 20:
            df = klines[code]
            cur = float(df.iloc[idx]["close"])
            base = float(df.iloc[idx - 20]["close"])
            if base > 0:
                val = cur / base - 1.0
        cache[key] = val
        return val

    out: dict[str, list[dict]] = {}
    for date, scored in signals.items():
        if scope == "candidates":
            pool = [s["code"] for s in scored]
        else:
            pool = universe.get(date, [])
        vals = [v for v in (r20(c, date) for c in pool) if np.isfinite(v)]
        if len(vals) < 30:          # 池子太小，分位不可靠 → 本日不过滤
            out[date] = scored
            continue
        th = float(np.quantile(np.asarray(vals), 1.0 - pct))
        out[date] = [s for s in scored if not (r20(s["code"], date) > th)]
    return out


def _run_window(task: tuple) -> tuple[str, list[dict], dict]:
    wname, start, end, pcts, universe, delay = task
    base = PipelineParams(start_date=start, end_date=end,
                          universe_size=universe, entry_open_delay_minutes=delay)
    t0 = time.time()
    try:
        prepared = _prepare_cached(base)
    except Exception as exc:  # noqa: BLE001
        print(f"[{wname}] 准备失败：{exc}", flush=True)
        return wname, [], {}

    rows: list[dict] = []
    details: dict[str, dict] = {}

    def run(tag: str, pct: float | None) -> None:
        keep = prepared.get("signals")
        keep_key = prepared.get("signals_key")
        if pct is None:
            result = _simulate(prepared, base)
        else:
            prepared["signals"] = _ret20_filter(prepared, base, pct)
            try:
                result = _simulate(prepared, base)
            finally:
                prepared["signals"] = keep
                prepared["signals_key"] = keep_key
        m = result["metrics"]
        bench = float(m.get("benchmark_return") or 0)
        total = float(m.get("total_return") or 0)
        rows.append({
            "window": wname, "range": f"{start}~{end}", "profile": tag,
            "pct": pct,
            "total_return": total, "benchmark_return": bench,
            "excess": round(total - bench, 2),
            "max_drawdown": float(m.get("max_drawdown") or 0),
            "trade_count": int(m.get("trade_count") or 0),
            "win_rate": float(m.get("win_rate") or 0),
            "avg_pnl_pct": float(m.get("avg_pnl_pct") or 0),
            "final_equity": float(m.get("final_equity") or 0),
        })
        details[f"{wname}|{tag}"] = {"params": result["params"], "metrics": m,
                                     "review_summary": result["review"]["summary"]}
        print(f"[{wname}/{tag}] {total:+.2f}% | 基准 {bench:+.2f}% "
              f"| 超额 {total - bench:+.2f}% | 回撤 {rows[-1]['max_drawdown']:.2f}% "
              f"| {rows[-1]['trade_count']} 笔 | 胜率 {rows[-1]['win_rate']:.1f}% "
              f"| {time.time() - t0:.0f}s", flush=True)

    run("default", None)
    for pct in pcts:
        run(f"nohot{int(round(pct * 100)):02d}", pct)
    return wname, rows, details


def main() -> None:
    ap = argparse.ArgumentParser(description="剔除 20 日涨幅最高 pct 的配对回测")
    ap.add_argument("--windows", default="A,B,C,D", help="A~J，空=默认近端窗口")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--universe", type=int, default=300, help="每日成交额 TopN（线上口径 300）")
    ap.add_argument("--pct", default="0.2", help="剔除比例，逗号分隔，如 0.2,0.1")
    ap.add_argument("--scope", default="universe", choices=["universe", "candidates"],
                    help="分位按当日 TopN 池算(universe) 还是自选池内算(candidates)")
    ap.add_argument("--delay", type=int, default=5, help="entry_open_delay_minutes")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    ranges = ([(n, s, e) for n, (s, e) in resolve_windows(args.windows).items()]
              if args.windows else [("", args.start, args.end)])
    pcts = [float(x) for x in str(args.pct).split(",") if x.strip()]
    jobs = args.jobs if args.jobs > 0 else auto_jobs(len(ranges))
    jobs = max(1, min(jobs, len(ranges)))
    tasks = [(n, s, e, pcts, args.universe, args.delay) for n, s, e in ranges]

    print(f"共 {len(ranges)} 窗口 × {1 + len(pcts)} 方案（default + 剔除），并行 {jobs}；"
          f"池 Top{args.universe}，分位口径 {args.scope}，延迟 {args.delay} 分钟", flush=True)

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
    names = [f"default"] + [f"nohot{int(round(p * 100)):02d}" for p in pcts]
    rows.sort(key=lambda r: (order.get(r["window"], 0), names.index(r["profile"])))

    print("\n| 窗口 | 方案 | 收益 | 基准 | 超额 | 回撤 | 笔数 | 胜率 | 平均/笔 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['window']} | {r['profile']} | {r['total_return']:+.2f}% "
              f"| {r['benchmark_return']:+.2f}% | {r['excess']:+.2f}% "
              f"| {r['max_drawdown']:.2f}% | {r['trade_count']} "
              f"| {r['win_rate']:.1f}% | {r['avg_pnl_pct']:+.2f}% |")

    # 配对差值：每个窗口 nohot - default
    print("\n### 配对差值（nohot - default，同窗口同参数，正=剔除更优）\n")
    print("| 窗口 | " + " | ".join(f"{n} 超额差 | {n} 收益差" for n in names[1:]) + " |")
    print("| --- | " + " | ".join("--- | ---" for _ in names[1:]) + " |")
    by_win: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_win.setdefault(r["window"], {})[r["profile"]] = r
    stat: dict[str, list[float]] = {n: [] for n in names[1:]}
    for w in order:
        cells = []
        d = by_win.get(w, {}).get("default")
        for n in names[1:]:
            x = by_win.get(w, {}).get(n)
            if not d or not x:
                cells.extend(["—", "—"])
                continue
            de = x["excess"] - d["excess"]
            dr = x["total_return"] - d["total_return"]
            stat[n].append(dr)
            cells.append(f"{de:+.2f}pp | {dr:+.2f}pp")
        print(f"| {w} | " + " | ".join(cells) + " |")
    for n in names[1:]:
        v = np.asarray(stat[n], dtype=float)
        if v.size:
            print(f"\n- `{n}`：收益差中位 {np.median(v):+.2f}pp、均值 {v.mean():+.2f}pp、"
                  f"胜出 {int((v > 0).sum())}/{v.size} 个窗口")

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"hot20_exclude{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"windows": [{"name": n, "start": s, "end": e} for n, s, e in ranges],
                   "universe": args.universe, "pcts": pcts, "scope": args.scope,
                   "delay": args.delay, "rows": rows, "details": details},
                  fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}（总耗时 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
