#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参数对比回测：用 pipeline 跑多组风控参数，横向比较收益/胜率/回撤。

用法：
    python scripts/compare_params.py
独立运行，依赖 backend 的 app 包。区间/universe 在 main() 顶部可调。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.backtest.pipeline import PipelineParams, run_pipeline


def _sideways_segments(start_date, end_date, kline_dir, ma_window=60, trend_th=0.08,
                       min_len=60, merge_gap=20):
    """用上证指数相对 MA60 的偏离度标出「震荡市」连续段（无趋势≈震荡，避免未来函数）。

    判定：``|close - MA60| / MA60 < trend_th`` 即视为震荡（横盘 / 无明显趋势）；
    该指标只用到截至当日的历史，无未来函数。返回 [(seg_start, seg_end), ...] 连续段。
    """
    path = os.path.join(str(kline_dir), "idx_sh000001.parquet")
    if not os.path.isfile(path):
        return []
    df = pd.read_parquet(path)
    if df is None or df.empty or "close" not in df or "date" not in df:
        return []
    df = df.sort_values("date")
    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].reset_index(drop=True)
    if len(df) < ma_window + 2:
        return []
    ma = df["close"].rolling(ma_window).mean()
    trend = (df["close"] - ma).abs() / ma
    df["side"] = trend < trend_th
    dates = df["date"].tolist()
    sides = df["side"].tolist()
    n = len(dates)
    raw = []
    i = 0
    while i < n:
        if sides[i]:
            j = i
            while j < n and sides[j]:
                j += 1
            if j - i >= min_len:
                raw.append((dates[i], dates[j - 1]))
            i = j
        else:
            i += 1
    merged = []
    for s in raw:
        if merged and (pd.Timestamp(s[0]) - pd.Timestamp(merged[-1][1])).days <= merge_gap + 5:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(list(s))
    return [(str(a), str(b)) for a, b in merged]


def _run_group(ov, start_date, end_date, universe_size):
    params = PipelineParams(
        start_date=start_date, end_date=end_date,
        universe_size=universe_size, **ov,
    )
    r = asyncio.run(run_pipeline(params))
    m = r.get("metrics", {}) if isinstance(r, dict) else {}
    def num(x):
        return x[-1] if isinstance(x, list) else x
    ret = num(m.get("total_return", 0.0))
    win = num(m.get("win_rate", 0.0))
    mdd = num(m.get("max_drawdown", 0.0))
    tr = num(m.get("trade_count", 0))
    # 近似换手率：平仓双侧成交额 / 平均权益（未含仍持仓的建仓买入，方向性足够）
    ec = r.get("equity_curve") or []
    if ec and isinstance(ec[0], dict):
        vals = [float(d.get("equity", d.get("value", 0)) or 0) for d in ec]
    else:
        vals = [float(x) for x in ec]
    avg_eq = sum(vals) / len(vals) if vals else 1.0
    totturn = 0.0
    for t in (r.get("trades") or []):
        bp = getattr(t, "buy_price", None)
        sp = getattr(t, "sell_price", None)
        sh = getattr(t, "shares", None)
        if (bp is None or sp is None or sh is None) and isinstance(t, dict):
            bp = t.get("buy_price"); sp = t.get("sell_price"); sh = t.get("shares")
        if bp is not None and sp is not None and sh is not None:
            totturn += (float(bp) + float(sp)) * float(sh)
    turnover = totturn / avg_eq if avg_eq else 0.0
    # 新股进入节奏：建仓事件 = 平仓记录中唯一 (buy_date, code) 数（未含期末仍持仓，
    # 各组对称）；轮动换入 ≈ reason 含「轮动替换」的平仓数；自然建仓 = 建仓事件 − 轮动换入。
    trades = r.get("trades") or []
    entries = set()
    rot = 0
    for t in trades:
        bd = t.get("buy_date"); cd = t.get("code")
        if bd and cd:
            entries.add((str(bd), str(cd)))
        rs = t.get("reason") or ""
        if "轮动替换" in rs:
            rot += 1
    return {"ret": ret, "win": win, "mdd": mdd, "tr": tr, "to": turnover,
            "entries": len(entries), "rot": rot, "nat": max(0, len(entries) - rot)}


def main() -> None:
    # #99 第三轮：分离「持仓数 vs 轮动」两变量 + 震荡市分段印证（不改 pipeline 轮动逻辑，尊重 #97）
    #   D=10只+关轮动 / B=4只+关轮动 → 纯持仓数效应；B vs C、D vs A → 纯轮动效应
    start_date, end_date, universe_size = "2022-09-01", "2025-12-31", 120
    from app.config import get_settings
    kline_dir = get_settings().kline_dir
    groups = [
        ("D 10只+关轮动",  {"max_positions": 10, "rotation_enabled": False}),
        ("B 4只+关轮动",   {"max_positions": 4,  "rotation_enabled": False}),
        ("A 10只+轮动开",  {}),
        ("C 4只+轮动开",   {"max_positions": 4,  "rotation_enabled": True}),
    ]

    # ---- 全样本对照（兼付数据冷启动） ----
    print("=== 全样本对照 ===")
    print(f"{'组':<22}{'收益':>9}{'胜率':>8}{'回撤':>9}{'笔数':>6}{'换手(近似)':>12}")
    print("-" * 68)
    results = []
    for name, ov in groups:
        res = _run_group(ov, start_date, end_date, universe_size)
        results.append((name, res))
        print(f"{name:<22} {res['ret']:>8.2f}% {res['win']:>6.1f}% {res['mdd']:>7.2f}% {res['tr']:>5} {res['to']:>11.2f}x")

    # ---- 新股进入节奏：验证「关轮动后新股是否仍进来」 ----
    print("\n=== 新股进入节奏（建仓事件来自平仓 buy_date；未含期末仍持仓，各组对称） ===")
    print(f"{'组':<22}{'建仓事件':>9}{'轮动换入':>9}{'自然建仓':>9}")
    print("-" * 52)
    for name, res in results:
        print(f"{name:<22} {res['entries']:>9} {res['rot']:>9} {res['nat']:>9}")

    # ---- 震荡市分段汇总 ----
    segs = _sideways_segments(start_date, end_date, kline_dir, trend_th=0.10)
    for s, e in segs:
        print("  段", s, e)
    print(f"\n=== 震荡市分段（{len(segs)} 段）===")
    print(f"{'组':<22}{'段数':>5}{'均收益':>9}{'均回撤':>9}{'总笔数':>7}{'均换手':>10}")
    print("-" * 62)
    for name, ov in groups:
        rets, mdds, trs, tos = [], [], [], []
        for s, e in segs:
            res = _run_group(ov, s, e, universe_size)
            rets.append(res["ret"]); mdds.append(res["mdd"])
            trs.append(res["tr"]); tos.append(res["to"])
        n = len(rets)
        print(f"{name:<22} {n:>5} {sum(rets)/n:>8.2f}% {sum(mdds)/n:>7.2f}% {sum(trs):>7} {sum(tos)/n:>9.2f}x")


if __name__ == "__main__":
    main()
