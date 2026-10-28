"""数据缺口审计：量化"回测数字里有多少来自数据本身的偏差"。

背景（BACKTEST_LOG 记录 #34 §4 遗留项、#27~#33 的结论都建立在本地 K 线上）：
本地缓存是「按股一个 Parquet，增量更新」，它天然只覆盖**当前在市**的股票，因此：

  1. 幸存者偏差：已退市股票不在样本里 → 绝对收益系统性偏高（相对比较受影响较小）；
  2. 停牌缺口：个股中间缺失交易日，回测按"当日有 bar 才可交易"处理，实际更差；
  3. 一字板：`high == low` 的日子根本买不进/卖不出，回测用 9.5% 涨停近似，会有偏差；
  4. ST/退市时点：`name` 只有当前快照，历史 ST 状态无法还原。

本脚本只做**量化与定级**，不改结论；输出用于回答"这个回测数字能不能当预期"。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/data_gap_audit.py
    .venv\\Scripts\\python.exe scripts/data_gap_audit.py --tag oct
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402

#: 指数代码（作为交易日历与"最近交易日"的基准）
INDEX_CODE = "idx_sh000001"
#: 最后 bar 落后最近交易日超过这么多个交易日 → 视作「已停更」（退市/长期停牌/代码变更）
STALE_TRADING_DAYS = 10
#: 判断停更股是否真的影响过我们的池子：回看它最后这么多根 bar
IMPACT_LOOKBACK = 20
#: 回测池规模（与 random_dist / regression 默认一致）
POOL_TOP_N = 300


def _codes() -> list[str]:
    return sorted(p.stem for p in kline_cache.dir.glob("*.parquet") if p.stem != INDEX_CODE)


def _delisted_codes() -> set[str]:
    """已知退市股代码：读 ``backfill_delisted.py`` 产出的清单。

    「最后 bar 停在很久以前」有两种完全不同的成因：
      1. **有意的**——已退市股票，本来就不该有今天的 bar（`backfill_delisted.py` 补齐的）；
      2. **缺口**——在市股票停更（长期停牌/代码变更/数据源丢了）。
    只有第 2 种才算缺口，混在一起会把已补齐的工作记成问题。
    """
    path = get_settings().kline_dir.parent / "logs" / "delisted_list.json"
    if not path.exists():
        return set()
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 清单坏了不影响审计主结论
        print(f"（退市清单读取失败，跳过分类：{exc}）", flush=True)
        return set()
    return {str(r["code"]).zfill(6) for r in rows if r.get("code")}


def _calendar() -> list[str]:
    idx = kline_cache.get(INDEX_CODE)
    if idx is None or idx.empty:
        raise SystemExit("缺少指数K线，无法确定交易日历")
    return [str(d)[:10] for d in idx["date"]]


def _scan_one(code: str) -> dict | None:
    """单只股票的基础统计（只读需要的列，缺数据返回 None）。"""
    df = kline_cache.get(code)
    if df is None or df.empty or "date" not in df.columns:
        return None
    dates = [str(d)[:10] for d in df["date"]]
    out = {
        "code": code,
        "first": dates[0],
        "last": dates[-1],
        "bars": len(dates),
        "one_word": 0,       # high == low（一字板）
        "zero_volume": 0,    # 成交量为 0（停牌/无成交）
        "trade_dates": set(dates),
    }
    if {"high", "low"}.issubset(df.columns):
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        out["one_word"] = int(((high == low) & high.notna()).sum())
    if "volume" in df.columns:
        vol = pd.to_numeric(df["volume"], errors="coerce")
        out["zero_volume"] = int((vol.fillna(0) <= 0).sum())
    if "amount" in df.columns:
        # 最后 N 根 bar 的成交额，用于判断"这只退市股当年会不会进入成交额 TopN 池"
        tail = df[["date", "amount"]].tail(IMPACT_LOOKBACK)
        out["recent"] = [
            (str(r.date)[:10], float(r.amount) if pd.notna(r.amount) else 0.0)
            for r in tail.itertuples(index=False)
        ]
    return out


def _pool_impact(stale: list[dict], top_n: int = POOL_TOP_N, workers: int = 8) -> dict:
    """停更股在最后 `IMPACT_LOOKBACK` 根 bar 里有多少次真的进了成交额 TopN 池。

    这是"幸存者偏差对我们到底有没有影响"的关键：若这些票从未进过池，缺失它们不改变
    回测的选股集合；若进过，那些日子的可选集合就比真实市场小（且通常正是放量下跌阶段）。
    """
    target = {d for r in stale for d, _ in r.get("recent", [])}
    if not target:
        return {"checked": 0, "lookback": IMPACT_LOOKBACK, "top_n": top_n, "codes": []}
    amounts: dict[str, dict[str, float]] = {d: {} for d in target}

    def _collect(code: str) -> None:
        df = kline_cache.get(code)
        if df is None or df.empty or "amount" not in df.columns:
            return
        sub = df[df["date"].astype(str).str[:10].isin(target)]
        for row in sub.itertuples(index=False):
            amounts[str(row.date)[:10]][code] = (
                float(row.amount) if pd.notna(row.amount) else 0.0
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_collect, _codes()))

    hits: list[dict] = []
    total_days = 0
    for r in stale:
        days = 0
        for d, _amt in r.get("recent", []):
            row = amounts.get(d) or {}
            if not row:
                continue
            ranked = sorted(row.items(), key=lambda kv: -kv[1])[:top_n]
            if any(code == r["code"] for code, _ in ranked):
                days += 1
        total_days += days
        if days:
            hits.append({"code": r["code"], "last": r["last"], "days_in_pool": days})
    return {
        "checked": len(stale),
        "lookback": IMPACT_LOOKBACK,
        "top_n": top_n,
        "days_in_pool": total_days,
        "codes": sorted(hits, key=lambda x: -x["days_in_pool"]),
    }


def audit(max_workers: int = 8) -> dict:
    codes = _codes()
    cal = _calendar()
    last = cal[-1]
    # 判定「已停更」的日期阈值
    stale_cut = cal[max(0, len(cal) - 1 - STALE_TRADING_DAYS)]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        rows = [r for r in pool.map(_scan_one, codes) if r]

    total = len(rows)
    stale = sorted((r for r in rows if r["last"] < stale_cut), key=lambda r: r["last"])
    known_delisted = _delisted_codes()
    stale_delisted = [r for r in stale if r["code"] in known_delisted]
    stale_unknown = [r for r in stale if r["code"] not in known_delisted]
    late_listed = [r for r in rows if r["first"] > "2023-01-01"]

    # 每个交易日的截面股票数（只统计有 bar 的股）
    per_date: Counter = Counter()
    for r in rows:
        per_date.update(r["trade_dates"])
    month_ends: dict[str, int] = {}
    for d in cal:
        month_ends[d[:7]] = per_date.get(d, 0)

    # 停牌缺口：个股在 [first, last] 区间内缺了多少个交易日
    idx_pos = {d: i for i, d in enumerate(cal)}
    gaps: list[float] = []
    for r in rows:
        i, j = idx_pos.get(r["first"]), idx_pos.get(r["last"])
        if i is None or j is None or j <= i:
            continue
        span = j - i + 1
        gaps.append(1 - r["bars"] / span)

    bars = [r["bars"] for r in rows]
    one_word = sum(r["one_word"] for r in rows)
    total_bars = sum(bars)
    zero_vol = sum(r["zero_volume"] for r in rows)

    st_now = 0
    try:
        from app.data.service import get_stock_pool

        pool = get_stock_pool()
        if pool is not None and not pool.empty and "name" in pool.columns:
            st_now = int(pool["name"].astype(str).str.contains("ST", case=False).sum())
            pool_size = int(len(pool))
        else:
            pool_size = 0
    except Exception:  # noqa: BLE001 - 缺股票池不影响主结论
        pool_size = 0

    return {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kline_dir": str(kline_cache.dir),
        "calendar": {"first": cal[0], "last": last, "days": len(cal)},
        "stocks": total,
        "bars": {"total": total_bars, "median": int(statistics.median(bars)), "min": min(bars)},
        "stale": {
            "count": len(stale),
            "cut": stale_cut,
            "delisted": len(stale_delisted),
            "unexplained": len(stale_unknown),
            "unexplained_stocks": [
                {"code": r["code"], "last": r["last"], "bars": r["bars"]}
                for r in stale_unknown[:10]
            ],
            "samples": [{"code": r["code"], "last": r["last"], "bars": r["bars"]} for r in stale[:10]],
        },
        "late_listed": {"count": len(late_listed), "share": round(len(late_listed) / total * 100, 1)},
        "cross_section": {"by_month": month_ends},
        "one_word": {
            "bars": one_word,
            "share": round(one_word / total_bars * 100, 3) if total_bars else 0.0,
        },
        "zero_volume": {"bars": zero_vol, "share": round(zero_vol / total_bars * 100, 3) if total_bars else 0.0},
        "suspension_gap": {
            "median": round(statistics.median(gaps) * 100, 2) if gaps else 0.0,
            "mean": round(sum(gaps) / len(gaps) * 100, 2) if gaps else 0.0,
            "stocks_over_20pct": sum(1 for g in gaps if g > 0.2),
        },
        "stock_pool": {"size": pool_size, "st_now": st_now},
        "pool_impact": _pool_impact(stale, POOL_TOP_N, max_workers),
    }


def _print_report(d: dict) -> None:
    cal = d["calendar"]
    print(f"\n数据缺口审计（{d['generated_at']}）\n")
    print(f"- 缓存：{d['kline_dir']}")
    print(f"- 交易日历：{cal['first']} ~ {cal['last']}（{cal['days']} 个交易日）")
    print(f"- 股票数：{d['stocks']}；K线条数中位 {d['bars']['median']}，最少 {d['bars']['min']}")
    print(f"- 股票池快照：{d['stock_pool']['size']} 只，其中名称含 ST {d['stock_pool']['st_now']} 只（仅当前状态）")

    print("\n### 1. 幸存者偏差\n")
    st = d["stale"]
    if st["count"] == 0:
        print(f"**未发现任何「已停更」股票**（判定线 {st['cut']}）。"
              "说明本地缓存只含**当前在市**的股票，退市股完全缺失 → 存在幸存者偏差。")
    else:
        print(f"已停更 {st['count']} 只（最后 bar 早于 {st['cut']}）："
              f"其中**已知退市股 {st.get('delisted', 0)} 只**（有意补齐，属正常）、"
              f"原因不明 {st.get('unexplained', 0)} 只（才算缺口）。")
        for s in (st.get("unexplained_stocks") or [])[:10]:
            print(f"  - [缺口] {s['code']} 最后 {s['last']}，{s['bars']} 根")
    imp = d.get("pool_impact") or {}
    if imp.get("checked"):
        print(f"\n**对回测池的影响**：{imp['checked']} 只停更股，在各自最后 {imp['lookback']} 根 bar 里"
              f"共有 {imp['days_in_pool']} 个「进入成交额 Top{imp['top_n']} 池」的交易日。")
        for c in (imp.get("codes") or [])[:5]:
            print(f"  - {c['code']}（最后 {c['last']}）在池内 {c['days_in_pool']} 天")
        if not imp.get("codes"):
            print("  → 没有停更股进过 TopN 池，缺失它们**不改变**回测的选股集合。")

    print(f"\n样本期新上市（首 bar 晚于 2023-01-01）：{d['late_listed']['count']} 只"
          f"（{d['late_listed']['share']}%）——这类股票在早期截面里本就不该出现，回测按当日有 bar 处理，属正确口径。")

    print("\n### 2. 截面股票数（每月，随月末）\n")
    items = list(d["cross_section"]["by_month"].items())
    step = max(1, len(items) // 10)
    print("| 月份 | 有K线的股票数 |")
    print("| --- | --- |")
    for month, n in items[::step]:
        print(f"| {month} | {n} |")

    print("\n### 3. 可交易性缺口\n")
    ow, zv = d["one_word"], d["zero_volume"]
    print(f"- 一字板（high == low）：{ow['bars']} 根，占全部 bar 的 **{ow['share']}%**")
    print(f"- 零成交量 bar：{zv['bars']} 根（{zv['share']}%）")
    gap = d["suspension_gap"]
    print(f"- 个股停牌缺口（区间内缺失交易日占比）：中位 {gap['median']}%，均值 {gap['mean']}%，"
          f"缺口 >20% 的股票 {gap['stocks_over_20pct']} 只")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    parser.add_argument("--workers", type=int, default=8, help="读取并发")
    args = parser.parse_args()

    data = audit(args.workers)
    _print_report(data)

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"data_gap_audit{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
