"""四条件低位选股 · 随机单月纯净回测（BACKTEST_LOG #150 / #151 / #152）。

#152（2026-11-14 用户明确要求）：``--dd-pct 15`` 把条件 1 从「比 30 日最高点低 10%」
拉到「低 15%」，其余条件（含 #151 去掉 c4）逐字不动。默认仍是 10%，
#150 / #151 产物可逐字复现；该参数只改 ``c1`` 的阈值系数。

#151（2026-11-14 用户明确要求）：可用 ``--drop-amp3`` **去掉条件 4**（3 日整体振幅 <3%），
只保留 1/2/3 作纯净配对。默认仍是四条件，#150 产物可逐字复现。
开关只改 ``ok`` 的布尔组合；``valid``、池、成交、出场、seed 一律不动，
保证相对 #150 的唯一改变量就是「去掉 c4」。

零生产写入：只读 ``D:/Backup/SAS/data/klines`` 日线，产物 JSON 落 ``data/logs``。
不改 ``app/``、设置项、``app_state``、``.env``、``sas.db``。

四条口径（2026-11-14 用户确认，全部只用 T 日及之前数据，无前视）：

1. ``close <= hh30 * 0.90``，``hh30 = high.rolling(30).max().shift(1)``
   = T-30~T-1 的 30 日最高价，**不含当日**（盘中 high 未走完）。
2. ``close <= ma20 * 0.95``，``ma20`` = 近 20 日收盘均价（含 T 日），
   作为「全部持仓成本」的近似。项目无筹码/CYQ 数据（#144 已评估不接入），
   MA20 是 ``WORKLOG.md`` 里项目自己定义的「最近 N 天买入者平均成本」。
3. ``turn3 >= 5.0``，``turn3`` = 近 3 日 ``turnover_rate`` 均值（%）；
   缺值一律**剔除**（不沿用主回测 ``pass_filters`` 的「放行」）。
4. ``amp3 < 0.03``，``amp3 = (max(high3) - min(low3)) / min(low3)``
   = 3 日整体高低振幅。

池：T-1 日成交额 Top300（与线上 ``paper_pool_top_n`` 同源，且不用 T 日 amount 造成前视）。
成交：T 日收盘发信号、T+1 开盘买入。出场复刻 ``random_month.py`` / #149：
  止损 8%（当日 low 触发、成交 ``min(open, 止损价)``）、止盈 20%、持有满 10 交易日次日开盘卖；
  同日双触发止损优先。
组合：10 万本金、最多 10 只、等额、100 股整手、当日信号固定 seed 随机抽样、
  佣金万 2.5（最低 5 元）+ 卖出印花千 1。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\low_position_probe.py --tag 2026-11
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DATA_DIR  # noqa: E402

DAILY_COLS = [
    "date", "open", "close", "high", "low", "volume", "amount",
    "pct_change", "turnover_rate",
]
KLINES = DATA_DIR / "klines"
LOGS = DATA_DIR / "logs"

PROTOCOL = {
    "rule1": "close <= hh30*0.90; hh30=high.rolling(30).max().shift(1) (T-30~T-1, 不含当日)",
    "rule2": "close <= ma20*0.95; ma20=close.rolling(20).mean() (代理全部持仓成本; 无 CYQ 数据)",
    "rule3": "turnover_rate.rolling(3).mean() >= 5.0 (缺值剔除)",
    "rule4": "(high.rolling(3).max()-low.rolling(3).min())/low.rolling(3).min() < 0.03",
    "pool": "T-1 日成交额 Top300（避免 T 日 amount 前视）",
    "fill": "T 日收盘发信号、T+1 开盘成交",
    "exit": "止损8%/止盈20%/持有10交易日; 同日双触发止损优先; 盘中止损成交 min(open, 止损价)",
    "benchmark": "Top300 池等权日收益累计（与 #149 pipeline 基准口径可能不同，仅参考）",
    "turnover_caveat": "约半数历史行 turnover_rate 为成交量÷反推流通股本的估计值（见 fix_turnover.py）",
}


def _calendar(start: str, end: str, warmup_days: int = 120) -> tuple[list[str], list[str]]:
    idx = pd.read_parquet(KLINES / "idx_sh000001.parquet")
    days = [str(d)[:10] for d in idx["date"]]
    warm_start = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    all_days = [d for d in days if warm_start <= d <= end]
    window = [d for d in days if start <= d <= end]
    if len(window) < 2:
        raise SystemExit("窗口交易日不足")
    return all_days, window


def _load(
    all_days: list[str], window_start: str,
    drop_amp3: bool = False, dd_pct: float = 10.0,
) -> tuple[dict, dict, dict]:
    """读全市场日线，算条件；返回 bars / 按日信号 / 元信息（含条件漏斗）。

    ``drop_amp3=True`` 时只把 c4 从布尔组合里摘掉（#151）；
    ``dd_pct`` 改条件 1 的 30 日回撤门槛（#152，默认 10%）。其余口径逐字不变。
    """
    lo, hi = all_days[0], all_days[-1]
    bars: dict[str, pd.DataFrame] = {}
    sig_by_date: dict[str, list[str]] = defaultdict(list)
    amt_by_date: dict[str, list[tuple[float, str]]] = defaultdict(list)
    funnel: dict[str, int] = defaultdict(int)
    missing_turnover = 0
    empty = 0

    for path in sorted(KLINES.glob("[0-9]*.parquet")):
        code = path.stem
        try:
            df = pd.read_parquet(path, columns=DAILY_COLS)
        except Exception:
            try:
                df = pd.read_parquet(path)
            except Exception:
                continue
        if "turnover_rate" not in df.columns:
            missing_turnover += 1
            continue
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        df = df[(df["date"] >= lo) & (df["date"] <= hi)]
        if df.empty:
            empty += 1
            continue
        for c in ("open", "close", "high", "low", "amount", "turnover_rate"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < 31:
            continue

        hh30 = df["high"].rolling(30).max().shift(1)
        ma20 = df["close"].rolling(20).mean()
        turn3 = df["turnover_rate"].rolling(3).mean()
        hi3 = df["high"].rolling(3).max()
        lo3 = df["low"].rolling(3).min()
        amp3 = (hi3 - lo3) / lo3.replace(0, float("nan"))
        valid = (
            hh30.notna() & ma20.notna() & turn3.notna() & amp3.notna()
            & df["close"].notna() & df["open"].notna()
        )
        c1 = df["close"] <= hh30 * (1 - dd_pct / 100)
        c2 = df["close"] <= ma20 * 0.95
        c3 = turn3 >= 5.0
        c4 = amp3 < 0.03
        base = c1 & c2 & c3
        selected = base if drop_amp3 else base & c4
        ok = selected & valid

        # 条件漏斗：只统计窗口内、数据完整的 (票,日)，用于定位「0 信号」的瓶颈
        # #150 默认路径的键集合保持不变；#151（drop_amp3）额外记实际入选组合
        # #152 改阈值时把 c1 键名写成实际回撤百分比，避免 dd10 的历史命名误导
        c1_key = "c1_dd10" if dd_pct == 10.0 else f"c1_dd{dd_pct:g}"
        scope = pd.Series(df["date"].to_numpy() >= window_start, index=df.index)
        v = valid & scope
        pairs = [
            (c1_key, c1), ("c2_below_ma20", c2),
            ("c3_turn3_ge5", c3), ("c4_amp3_lt3", c4),
            ("c1_and_c2", c1 & c2), ("c3_and_c4", c3 & c4),
            ("all4", base & c4),
        ]
        if drop_amp3:
            pairs += [("c1_c2_c3", base), ("selected", selected)]
        for key, mask in pairs:
            funnel[key] += int((mask & v).sum())
        funnel["valid_stock_days"] += int(v.sum())

        df = df.set_index("date")
        bars[code] = df[["open", "close", "high", "low", "amount"]]
        for d in df.index[ok.to_numpy()]:
            sig_by_date[d].append(code)
        for d, amt in zip(df.index, df["amount"].to_numpy()):
            if pd.notna(amt) and amt > 0:
                amt_by_date[d].append((float(amt), code))

    return (
        bars,
        sig_by_date,
        {
            "amt_by_date": amt_by_date,
            "missing_turnover": missing_turnover,
            "empty": empty,
            "funnel": dict(funnel),
        },
    )


def _pools(all_days: list[str], amt_by_date: dict, universe: int) -> dict[str, set[str]]:
    """T 日可用池 = T-1 日成交额 TopN。"""
    top = {
        d: {c for _, c in sorted(lst, reverse=True)[:universe]}
        for d, lst in amt_by_date.items()
    }
    pools: dict[str, set[str]] = {}
    for i, d in enumerate(all_days):
        pools[d] = top.get(all_days[i - 1], set()) if i > 0 else set()
    return pools


def _simulate(
    bars: dict, sig_by_date: dict, pools: dict, all_days: list[str],
    window: list[str], args,
) -> dict:
    rng = random.Random(args.seed)
    cash = float(args.capital)
    positions: dict[str, dict] = {}
    pending_buys: list[str] = []
    pending_sells: dict[str, str] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    day_no = {d: i for i, d in enumerate(all_days)}
    slot_value = float(args.capital) / args.max_positions
    exit_counts: dict[str, int] = defaultdict(int)
    signal_candidates = 0
    signal_in_pool = 0
    skipped_no_slot = 0
    skipped_no_cash = 0

    def _bar(code: str, d: str):
        df = bars.get(code)
        if df is None or d not in df.index:
            return None
        return df.loc[d]

    def _sell(code: str, d: str, price: float, reason: str) -> None:
        nonlocal cash
        p = positions.pop(code)
        proceeds = p["shares"] * price
        fee = max(5.0, proceeds * 0.00025) + proceeds * 0.001
        cash += proceeds - fee
        pnl = (price * (1 - 0.00125) / p["buy_price"] - 1) * 100
        trades.append({
            "code": code, "buy_date": p["buy_date"], "sell_date": d,
            "buy_price": round(p["buy_price"], 3), "sell_price": round(price, 3),
            "shares": p["shares"], "pnl_pct": round(pnl, 2), "reason": reason,
        })
        exit_counts[reason] += 1

    for d in window:
        # 1) 昨日委托：今日开盘成交
        for code, reason in list(pending_sells.items()):
            b = _bar(code, d)
            if b is not None and code in positions:
                _sell(code, d, float(b["open"]), reason)
        pending_sells.clear()

        for code in pending_buys:
            if code in positions or len(positions) >= args.max_positions:
                skipped_no_slot += 1
                continue
            b = _bar(code, d)
            if b is None:
                continue
            px = float(b["open"])
            if not px or pd.isna(px):
                continue
            shares = int(slot_value / (px * 100)) * 100
            if shares < 100:
                continue
            cost = shares * px
            fee = max(5.0, cost * 0.00025)
            if cost + fee > cash:
                skipped_no_cash += 1
                continue
            cash -= cost + fee
            positions[code] = {
                "shares": shares, "buy_price": px, "buy_date": d,
                "buy_no": day_no[d],
            }
        pending_buys = []

        # 2) 当日盘中止损 / 止盈（同日双触发止损优先）
        for code, p in list(positions.items()):
            b = _bar(code, d)
            if b is None:
                continue
            stop_px = p["buy_price"] * (1 - args.stop)
            take_px = p["buy_price"] * (1 + args.take)
            if float(b["low"]) <= stop_px:
                _sell(code, d, min(float(b["open"]), stop_px), "止损")
            elif float(b["high"]) >= take_px:
                _sell(code, d, max(float(b["open"]), take_px), "止盈")

        # 3) 收盘：持有满 N 交易日 → 次日开盘时间止损
        for code, p in positions.items():
            if code in pending_sells:
                continue
            if day_no[d] - p["buy_no"] + 1 >= args.hold:
                pending_sells[code] = f"时间止损(持有{args.hold}日)"

        # 4) 收盘：生成次日买入委托
        cands = [c for c in sig_by_date.get(d, []) if c in pools.get(d, set())]
        signal_candidates += len(sig_by_date.get(d, []))
        signal_in_pool += len(cands)
        rng.shuffle(cands)
        pending_buys = cands[:max(0, args.max_positions - len(positions))]

        # 5) 净值
        mv = 0.0
        for code, p in positions.items():
            b = _bar(code, d)
            if b is not None:
                mv += p["shares"] * float(b["close"])
        equity_curve.append({
            "date": d, "equity": round(cash + mv, 2),
            "cash": round(cash, 2), "positions": len(positions),
        })

    # 基准：Top300 池等权日收益累计
    bench = 1.0
    for i in range(1, len(window)):
        d, prev = window[i], window[i - 1]
        rets = []
        for code in pools.get(d, set()):
            df = bars.get(code)
            if df is None or d not in df.index or prev not in df.index:
                continue
            c0, c1 = float(df.loc[prev, "close"]), float(df.loc[d, "close"])
            if c0 > 0 and pd.notna(c1):
                rets.append(c1 / c0 - 1)
        if rets:
            bench *= 1 + sum(rets) / len(rets)

    eqs = [p["equity"] for p in equity_curve]
    peak, mdd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    total_return = eqs[-1] / float(args.capital) - 1
    bench_return = bench - 1
    wins = [t for t in trades if t["pnl_pct"] > 0]

    return {
        "metrics": {
            "initial_capital": float(args.capital),
            "final_equity": round(eqs[-1], 2),
            "total_return": round(total_return * 100, 2),
            "benchmark_return": round(bench_return * 100, 2),
            "excess": round((total_return - bench_return) * 100, 2),
            "max_drawdown": round(mdd * 100, 2),
            "trade_count": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2) if trades else 0.0,
            "open_positions": len(positions),
            "exit_counts": dict(exit_counts),
        },
        "signal_stats": {
            "signal_candidates": signal_candidates,
            "signal_in_pool": signal_in_pool,
            "skipped_no_slot": skipped_no_slot,
            "skipped_no_cash": skipped_no_cash,
        },
        "trades": trades,
        "equity_curve": equity_curve,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-02-25", help="窗口起点（与 #149 同窗口，便于对照）")
    ap.add_argument("--end", default="2025-03-26", help="窗口终点")
    ap.add_argument("--universe", type=int, default=300, help="T-1 日成交额 TopN")
    ap.add_argument("--seed", type=int, default=20261114, help="当日信号随机抽样种子")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--hold", type=int, default=10, help="持有上限（交易日），对齐新默认")
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--take", type=float, default=0.20)
    ap.add_argument("--tag", default="2026-11")
    ap.add_argument(
        "--drop-amp3", action="store_true",
        help="去掉条件 4（3 日整体振幅 <3%），BACKTEST_LOG #151 配对实验",
    )
    ap.add_argument(
        "--dd-pct", type=float, default=10.0,
        help="条件 1：比 30 日最高点低至少 N%%，默认 10；#152 用 15",
    )
    args = ap.parse_args()

    t0 = time.time()
    all_days, window = _calendar(args.start, args.end)
    bars, sig_by_date, meta = _load(all_days, args.start, args.drop_amp3, args.dd_pct)
    pools = _pools(all_days, meta["amt_by_date"], args.universe)
    result = _simulate(bars, sig_by_date, pools, all_days, window, args)

    proto = dict(PROTOCOL)
    cond = {
        "start": args.start, "end": args.end, "trading_days": len(window),
        "universe": args.universe, "seed": args.seed, "capital": args.capital,
        "max_positions": args.max_positions, "hold": args.hold,
        "stop": args.stop, "take": args.take,
        "compare_with": "BACKTEST_LOG #149 同窗口 seed=20261114（新参数臂 21 笔平仓）",
    }
    if args.drop_amp3:
        proto["rule4"] = "【已去掉】3 日整体振幅 <3%（用户 2026-11-14 明确要求；BACKTEST_LOG #151）"
        cond["active_rules"] = ["rule1", "rule2", "rule3"]
        cond["dropped_rule"] = "rule4: amp3 < 0.03"
        cond["compare_with"] = (
            "BACKTEST_LOG #150 同窗口四条件臂（0 信号）；唯一改变量 = 去掉 rule4"
        )
    if args.dd_pct != 10.0:
        ratio = round(1 - args.dd_pct / 100, 4)
        proto["rule1"] = f"close <= hh30*{ratio:.2f}（30 日回撤 >= {args.dd_pct:g}%，#152）"
        cond["dd_pct"] = args.dd_pct
        cond["hh30_ratio"] = ratio
        cond["compare_with"] = (
            "BACKTEST_LOG #151 同窗口三条件臂（回撤>=10%）；唯一改变量 = c1 阈值 10%->15%"
        )
    out = {
        "protocol": proto,
        "conditions": cond,
        "data": {
            "kline_files_with_bars": len(bars),
            "missing_turnover_files": meta["missing_turnover"],
            "empty_in_range_files": meta["empty"],
        },
        "condition_funnel": meta["funnel"],
        **result,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / f"low_position_probe_{args.tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(path),
        "data": out["data"],
        "condition_funnel": out["condition_funnel"],
        "signal_stats": out["signal_stats"],
        "metrics": out["metrics"],
        "elapsed_sec": out["elapsed_sec"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
