"""#153 红三兵 + MA20 调头向上 · 随机单月纯净回测（零生产写入）。

复用 ``low_position_probe.py`` 的交易日历、T-1 成交额 Top300 池、撮合与出场
（``_calendar`` / ``_pools`` / ``_simulate``），**只替换选股条件**，
保证与 #150~#152 的撮合 / 费用 / 出场 / seed 口径零差异，可直接比笔数与路径。

四条口径（2026-11-14 用户给定，跑前写死；全部只用 T 日及之前数据，无前视）：

1. ``ll30/hh30 - 1 <= -0.15``，``hh30 = high.rolling(30).max().shift(1)``、
   ``ll30 = low.rolling(30).min().shift(1)`` = T-30~T-1 的 30 日最高 / 最低价，
   **不含当日**。按字面这是「30 日区间内最低点比最高点低 15% 以上」的**形态条件**，
   不含「当前价必须处于低位」约束（若用户本意含此约束，须另开一条）。
2. 近 3 日**三连阳**（``close > open``）且收盘逐日抬高（``close > close.shift(1)``），
   且 ``0 < gain3 <= 0.05``，``gain3 = close/close.shift(3) - 1``（3 日累计涨幅）。
   简化：不要求开盘价落在前一日实体内、不要求收盘接近最高。
3. ``turnover_rate.rolling(3).mean() >= 5.0``，缺值一律剔除。
4. MA20 已调头向上：``ma20 > ma20.shift(1) > ma20.shift(2)``（连续两日上行），
   ``ma20 = close.rolling(20).mean()``。

池 / 成交 / 出场同 #149~#152：T-1 日成交额 Top300、T 日收盘发信号、T+1 开盘买入、
止损 8% / 止盈 20% / 持有满 10 交易日次日开盘卖、同日双触发止损优先；
10 万本金、最多 10 只、等额、100 股整手、固定 seed 随机抽样、
佣金万 2.5（最低 5 元）+ 卖出印花千 1。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\red_three_probe.py --tag 2026-11-red3
    .venv\\Scripts\\python.exe scripts\\red_three_probe.py --tag 2026-11-red3-oos --start 2025-08-21 --end 2025-09-19
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict

import pandas as pd

from low_position_probe import (
    DAILY_COLS, KLINES, LOGS, _calendar, _pools, _simulate,
)

PROTOCOL = {
    "rule1": "low.rolling(30).min()/high.rolling(30).max()-1 <= -0.15 (shift(1), T-30~T-1, 不含当日); 区间形态条件, 不约束当前价",
    "rule2": "近3日三连阳(close>open)且收盘逐日抬高; 0 < close/close.shift(3)-1 <= 0.05 (不要求开盘落前一日实体内)",
    "rule3": "turnover_rate.rolling(3).mean() >= 5.0 (缺值剔除)",
    "rule4": "ma20 > ma20.shift(1) > ma20.shift(2) (MA20 连续两日上行, 视作已调头)",
    "pool": "T-1 日成交额 Top300（避免 T 日 amount 前视）",
    "fill": "T 日收盘发信号、T+1 开盘成交",
    "exit": "止损8%/止盈20%/持有10交易日; 同日双触发止损优先; 盘中止损成交 min(open, 止损价)",
    "benchmark": "Top300 池等权日收益累计（复用 low_position_probe._simulate）",
    "reuse": "_calendar/_pools/_simulate 直接复用 low_position_probe，撮合与出场口径零差异",
    "turnover_caveat": "约半数历史行 turnover_rate 为成交量÷反推流通股本的估计值（见 fix_turnover.py）",
}


def _load(all_days: list[str], window_start: str) -> tuple[dict, dict, dict]:
    """读全市场日线，算新四条件；返回 bars / 按日信号 / 元信息（含条件漏斗）。"""
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
        if len(df) < 34:
            continue

        hh30 = df["high"].rolling(30).max().shift(1)
        ll30 = df["low"].rolling(30).min().shift(1)
        ma20 = df["close"].rolling(20).mean()
        turn3 = df["turnover_rate"].rolling(3).mean()
        yang = df["close"] > df["open"]
        up = df["close"] > df["close"].shift(1)
        red3 = yang & yang.shift(1) & yang.shift(2) & up & up.shift(1) & up.shift(2)
        gain3 = df["close"] / df["close"].shift(3) - 1

        d1 = (ll30 / hh30.replace(0, float("nan")) - 1) <= -0.15
        d2 = red3 & (gain3 > 0) & (gain3 <= 0.05)
        d3 = turn3 >= 5.0
        d4 = (ma20 > ma20.shift(1)) & (ma20.shift(1) > ma20.shift(2))
        valid = (
            hh30.notna() & ll30.notna() & ma20.shift(2).notna()
            & turn3.notna() & gain3.notna()
            & df["close"].notna() & df["open"].notna()
        )
        selected = d1 & d2 & d3 & d4
        ok = selected & valid

        scope = pd.Series(df["date"].to_numpy() >= window_start, index=df.index)
        v = valid & scope
        for key, mask in (
            ("d1_range_dd15", d1), ("d2_red3_gain_le5", d2),
            ("d3_turn3_ge5", d3), ("d4_ma20_up", d4),
            ("d1_and_d2", d1 & d2), ("d2_and_d4", d2 & d4),
            ("selected", selected),
        ):
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-02-25", help="窗口起点（#149~#152 主窗口）")
    ap.add_argument("--end", default="2025-03-26", help="窗口终点")
    ap.add_argument("--universe", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--take", type=float, default=0.20)
    ap.add_argument("--tag", default="2026-11-red3")
    args = ap.parse_args()

    t0 = time.time()
    all_days, window = _calendar(args.start, args.end)
    bars, sig_by_date, meta = _load(all_days, args.start)
    pools = _pools(all_days, meta["amt_by_date"], args.universe)
    result = _simulate(bars, sig_by_date, pools, all_days, window, args)

    out = {
        "protocol": PROTOCOL,
        "conditions": {
            "start": args.start, "end": args.end, "trading_days": len(window),
            "universe": args.universe, "seed": args.seed, "capital": args.capital,
            "max_positions": args.max_positions, "hold": args.hold,
            "stop": args.stop, "take": args.take,
            "compare_with": "BACKTEST_LOG #151/#152 同窗口（撮合/出场/seed 完全一致，仅选股条件全换，非严格配对）",
        },
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
    path = LOGS / f"red_three_probe_{args.tag}.json"
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
