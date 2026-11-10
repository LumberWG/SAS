# -*- coding: utf-8 -*-
"""twin_t_daily_signal 纯函数单测（不依赖真实数据，秒级）。"""
from __future__ import annotations

import numpy as np

from scripts.twin_t_daily_signal import (
    cost_break_even,
    holding_percentiles,
)


def test_cost_break_even_small_notional_min_commission_dominant():
    # 小名义额：最低佣金 5 元/腿 主导 -> 平衡幅度高
    be = cost_break_even(
        notional=1000.0, commission_rate=0.00025, min_commission=5.0,
        stamp_rate=0.001, slippage_bps=0.0005,
    )
    # 成本 = 2*max(0.25,5)=10 + 1 + 1 = 12 -> 1.2%
    assert abs(be - 1.2) < 1e-6


def test_cost_break_even_large_notional_commission_rate_applies():
    # 大名义额：佣金率生效（低于最低），平衡幅度低
    be = cost_break_even(
        notional=100_000.0, commission_rate=0.00025, min_commission=5.0,
        stamp_rate=0.001, slippage_bps=0.0005,
    )
    # 成本 = 2*(25) + 100 + 100 = 250 -> 0.25%
    assert abs(be - 0.25) < 1e-6


def test_cost_break_even_zero_notional_safe():
    be = cost_break_even(0.0, 0.00025, 5.0, 0.001, 0.0005)
    assert be == float("inf")


def test_holding_percentiles_extremes():
    # 3 只票、3 个截面槽；持仓 dev 在某槽为全市场最小/最大
    universe = np.array([
        [-0.02, -0.01, 0.00],
        [0.00, 0.00, 0.01],
        [0.02, 0.01, 0.02],
    ], dtype=float)  # (3, 3)
    hold = np.array([-0.02, -0.01, 0.00], dtype=float)  # 每只槽都是最小
    pct = holding_percentiles(hold, universe)
    # 每只槽持仓都是最小 -> 分位 0%
    assert np.allclose(pct, 0.0, atol=1e-9)

    hold_max = np.array([0.02, 0.01, 0.02], dtype=float)  # 每只槽都是最大
    pct2 = holding_percentiles(hold_max, universe)
    # 严格 < 语义：最大 dev 的分位 = (N-1)/N = 2/3 ≈ 66.67%（已处于顶部尾部，正确）
    assert np.allclose(pct2, (2.0 / 3.0) * 100.0, atol=1e-9)


def test_holding_percentiles_middle():
    universe = np.array([
        [-0.05, 0.00],
        [0.00, 0.00],
        [0.05, 0.00],
    ], dtype=float)
    # 持仓在某槽=0.00，三个值 [-0.05,0,0.05] -> (col<0).mean() = 1/3 -> 33.3%
    hold = np.array([0.00, 0.00], dtype=float)
    pct = holding_percentiles(hold, universe)
    assert abs(pct[0] - (1.0 / 3.0) * 100.0) < 1e-6


def test_holding_percentiles_nan_ignored():
    universe = np.full((3, 2), np.nan)
    universe[0, 0] = -0.01
    universe[1, 0] = 0.00
    universe[2, 0] = 0.01
    hold = np.array([0.00, np.nan], dtype=float)
    pct = holding_percentiles(hold, universe)
    # 槽0：col=[-0.01,0,0.01]，持仓0.00 -> (col<0).mean()=1/3 -> 33.3%；槽1全NaN -> NaN
    assert abs(pct[0] - (1.0 / 3.0) * 100.0) < 1e-6
    assert np.isnan(pct[1])
