# -*- coding: utf-8 -*-
"""双向做T（正T+反T）单元测试：合成面板注入（无未来函数）+ 真实数据冒烟。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies.cross_sectional import _prepare_df
from app.strategies.twin_t import TwinTParams, simulate_twin_t


def _mk(code: str, closes):
    """构造 96 根（1 个交易日）5 分钟 K 线：open=high=low=close，volume=1，amount=close。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    morning = [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(24)]
    afternoon = [datetime(2025, 1, 2, 13, 5) + timedelta(minutes=5 * i) for i in range(24)]
    times = morning + afternoon
    if n > len(times):
        extra = n - len(times)
        d2m = [datetime(2025, 1, 3, 9, 35) + timedelta(minutes=5 * i) for i in range(min(extra, 24))]
        d2a = [datetime(2025, 1, 3, 13, 5) + timedelta(minutes=5 * i) for i in range(max(0, extra - 24))]
        times = times + d2m + d2a
    times = times[:n]
    closes = np.concatenate([closes, np.full(max(0, n - len(closes)), closes[-1])])[:n]
    df = pd.DataFrame({
        "date": times,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1.0, "amount": closes,
    })
    return df


def _closes96(pattern: str):
    base = np.full(96, 10.0)
    if pattern == "dip":
        base[25:35] = 9.0      # 下午中段急跌 → 相对 VWAP 超卖（当日内回归）
    elif pattern == "spike":
        base[25:35] = 11.0     # 下午中段急涨 → 相对 VWAP 超买（当日内回归）
    return base


def _raw(*patterns):
    raw = {}
    for i, p in enumerate(patterns):
        code = f"T{i:03d}"
        raw[code] = _prepare_df(_mk(code, _closes96(p)), "2025-01-01", "2026-12-31")
    return raw


def test_both_fires_long_and_short_with_profit():
    raw = _raw("dip", "spike", "flat", "flat")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", n_select=1))
    # 超卖股产生盈利多头、超买股产生盈利空头（回归触发）；截面逻辑也可能在回归后
    # 触发额外小笔，故只断言「存在」双向盈利腿，而非精确笔数。
    assert any(t.side == "long" and t.pnl > 0 for t in res["trades"])
    assert any(t.side == "short" and t.pnl > 0 for t in res["trades"])
    assert res["t_pnl_pct"] > 0
    assert res["beats_hold"]


def test_long_only_no_short():
    raw = _raw("dip", "spike", "flat", "flat")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="long", n_select=1))
    assert any(t.side == "long" and t.pnl > 0 for t in res["trades"])
    assert not any(t.side == "short" for t in res["trades"])


def test_short_only_no_long():
    raw = _raw("dip", "spike", "flat", "flat")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="short", n_select=1))
    assert any(t.side == "short" and t.pnl > 0 for t in res["trades"])
    assert not any(t.side == "long" for t in res["trades"])


def test_trades_are_same_day():
    raw = _raw("dip", "spike")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", n_select=1))
    assert res["trades"]
    for t in res["trades"]:
        assert t.bars_held < 96          # 当日 round-trip，隔夜无敞口
        assert t.forced or t.bars_held >= 0


def test_combined_equals_hold_plus_t():
    raw = _raw("dip", "spike", "flat", "flat")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", n_select=1))
    # 合成面板平进平出，持有基线≈0；组合应等于做T贡献
    assert abs(res["combined"] - (res["hold_return"] + res["t_pnl_pct"])) < 1e-12
    assert isinstance(res["hold_return"], float)


def test_beats_hold_flag_consistent():
    raw = _raw("dip", "spike", "flat", "flat")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", n_select=1))
    assert res["beats_hold"] == (res["t_pnl_pct"] > 0)


def test_raw_injection_is_deterministic():
    """同一 panel 反复注入应产出完全一致的结果（回测脚本共享 raw 的前提）。"""
    raw = _raw("dip", "spike", "flat", "flat")
    p = TwinTParams(mode="both", n_select=1)
    a = simulate_twin_t(raw=raw, params=p)
    b = simulate_twin_t(raw=raw, params=p)
    assert a["aggregate"]["rounds"] == b["aggregate"]["rounds"]
    assert abs(a["t_pnl_pct"] - b["t_pnl_pct"]) < 1e-12
    assert len(a["trades"]) == len(b["trades"])


def test_hold_return_is_equal_weight_basket():
    """持有基线 = 篮子内各股 (末收盘-首收盘)/首收盘 的等权平均。"""
    raw = {}
    for i, (first, last) in enumerate([(10.0, 11.0), (10.0, 9.0), (10.0, 10.0)]):
        cl = np.full(96, first)
        cl[-1] = last
        code = f"H{i:03d}"
        raw[code] = _prepare_df(_mk(code, cl), "2025-01-01", "2026-12-31")
    res = simulate_twin_t(raw=raw, params=TwinTParams(mode="long", n_select=1))
    expected = ((11.0 - 10.0) / 10.0 + (9.0 - 10.0) / 10.0 + 0.0) / 3.0
    assert abs(res["hold_return"] - expected) < 1e-9
    assert abs(res["combined"] - (res["hold_return"] + res["t_pnl_pct"])) < 1e-12


@pytest.mark.parametrize("mode", ["both", "long", "short"])
def test_twin_real_smoke(mode):
    codes = ["000001", "600000", "000002", "600519"]
    res = simulate_twin_t(codes=codes, start="2025-01-02", end="2026-11-09",
                          params=TwinTParams(mode=mode, n_select=5))
    # 真实数据缺失则跳过；有数据则链路不崩、贡献为有限值
    if res["scanned"] == 0:
        pytest.skip("样本标的本地数据缺失，跳过")
    assert np.isfinite(res["t_pnl_pct"])
    assert np.isfinite(res["hold_return"])
