#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把纸面账户从「首次模拟日」到今天重新回测一遍，对比改进前后的策略表现。

用法：
    python scripts/replay_paper.py [start_date] [end_date]
默认起点取纸面账户创建日 2026-10-23，终点取今天。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.backtest.pipeline import PipelineParams, run_pipeline

START = sys.argv[1] if len(sys.argv) > 1 else "2026-10-23"
END = sys.argv[2] if len(sys.argv) > 2 else date.today().strftime("%Y-%m-%d")
UNI = 150

# 旧策略 = 当初实盘在跑的：轮动开 + 固定 8% 止损 + 无大盘择时 + 无保本移动
# 新策略 = 现已落地的：关轮动 + 2×ATR + 保本/移动止损 + 大盘择时闸门
groups = {
    "旧策略(轮动开+固定8%止损,无择时无保本)": {
        "rotation_enabled": True, "stop_atr_mult": None,
        "regime_ma": 0, "breakeven_trigger": 0.0, "trail_pct": 0.0,
    },
    "新策略(关轮动+2xATR+保本移动+大盘择时)": {
        "rotation_enabled": False, "stop_atr_mult": 2.0,
        "regime_ma": 60, "breakeven_trigger": 0.10, "trail_pct": 0.10, "trail_arm": 0.0,
    },
}


def num(x):
    return x[-1] if isinstance(x, list) else x


print(f"回测区间 {START} ~ {END}  universe={UNI}")
print(f"{'组':<42}{'收益':>9}{'胜率':>8}{'回撤':>9}{'笔数':>6}")
print("-" * 74)
for name, ov in groups.items():
    p = PipelineParams(start_date=START, end_date=END, universe_size=UNI, **ov)
    r = asyncio.run(run_pipeline(p))
    m = r.get("metrics", r)
    ret = num(m.get("total_return", 0.0))
    win = num(m.get("win_rate", 0.0))
    mdd = num(m.get("max_drawdown", 0.0))
    tr = num(m.get("trade_count", 0))
    print(f"{name:<42} {ret:>8.2f}% {win:>6.1f}% {mdd:>7.2f}% {tr:>5}")
