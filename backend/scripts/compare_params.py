#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参数对比回测：用 pipeline 跑多组风控参数，横向比较收益/胜率/回撤。

用法：
    python scripts/compare_params.py
独立运行，依赖 backend 的 app 包。区间/universe 在 main() 顶部可调。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.backtest.pipeline import PipelineParams, run_pipeline


def main() -> None:
    # 区间与股票池（按需调整；长区间更稳但更慢）
    start_date, end_date, universe_size = "2024-01-02", "2024-01-31", 150

    # 各组覆盖的「止血/风控」参数（对照 baseline = 当前默认：关轮动 + 3×ATR）
    groups = [
        ("baseline(关轮动+3xATR)", {}),
        ("加宽止损ATR3", {"stop_atr_mult": 3.0, "stop_atr_min": 0.08, "stop_atr_max": 0.20}),
        ("大盘择时regime", {"regime_ma": 60}),
        ("移动保本", {"breakeven_trigger": 0.10, "trail_pct": 0.10, "trail_arm": 0.0}),
        ("不追高", {"max_open_gain": 0.03, "max_gain_5d": 0.15}),
        ("组合(择时+移动+不追高+宽止损)",
         {"regime_ma": 60, "breakeven_trigger": 0.10, "trail_pct": 0.10,
          "max_open_gain": 0.03, "max_gain_5d": 0.15, "stop_atr_mult": 3.0}),
        ("轮动对照(开)", {"rotation_enabled": True}),
        ("intraday5(需分钟源)", {"intraday_minutes": True}),
    ]

    print(f"{'组':<28}{'收益':>9}{'胜率':>8}{'回撤':>9}{'笔数':>6}{'耗时':>7}")
    print("-" * 68)
    for name, ov in groups:
        params = PipelineParams(
            start_date=start_date, end_date=end_date,
            universe_size=universe_size, **ov,
        )
        t0 = time.time()
        r = asyncio.run(run_pipeline(params))
        dt = time.time() - t0
        m = r.get("metrics", r) if isinstance(r, dict) else {}
        def num(x):
            return x[-1] if isinstance(x, list) else x
        ret = num(m.get("total_return", 0.0))
        win = num(m.get("win_rate", 0.0))
        mdd = num(m.get("max_drawdown", 0.0))
        tr = num(m.get("trade_count", 0))
        print(f"{name:<28} {ret:>8.2f}% {win:>6.1f}% {mdd:>7.2f}% {tr:>5} {dt:>6.0f}s")


if __name__ == "__main__":
    main()
