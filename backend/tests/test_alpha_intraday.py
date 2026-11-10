# -*- coding: utf-8 -*-
"""独立 alpha 模块单元测试：四类信号生成器（注入合成数据，无未来函数）+ 真实数据冒烟。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies import alpha_intraday as al
from app.strategies.alpha_intraday import (
    AlphaParams,
    ComboParams,
    aggregate_trades,
    simulate_series,
    simulate_series_combo,
)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def _md_times(date_str: str, n: int = 48) -> list[datetime]:
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, minute=35)
    out = [base + timedelta(minutes=5 * i) for i in range(24)]
    base = base.replace(hour=13, minute=5)
    out += [base + timedelta(minutes=5 * i) for i in range(24)]
    return out[:n]


def _make_bars(close, times, open_=None, high=None, low=None, vol=None, amt=None):
    df = pd.DataFrame({"close": np.asarray(close, dtype=float)})
    df["open"] = df["close"] if open_ is None else np.asarray(open_, dtype=float)
    df["high"] = df["close"] if high is None else np.asarray(high, dtype=float)
    df["low"] = df["close"] if low is None else np.asarray(low, dtype=float)
    df["volume"] = 1.0 if vol is None else np.asarray(vol, dtype=float)
    if amt is None:
        df["amount"] = df["close"] * df["volume"]
    else:
        df["amount"] = np.asarray(amt, dtype=float)
    df["date"] = times
    return df


# ---------------------------------------------------------------------------
# VWAP 偏离均值回归
# ---------------------------------------------------------------------------
def test_vwap_enters_on_deviation_and_reverts():
    # 20 根 10.0 预热（dev≈0 无信号）→ 5 根 9.9（低于 VWAP 触发做多）→ 5 根 10.1（回归触发平仓）
    times = _md_times("2025-01-02")
    close = np.concatenate([
        np.full(20, 10.0),
        np.full(5, 9.9),
        np.full(5, 10.1),
        np.full(len(times) - 30, 10.0),
    ])[: len(times)]
    bars = _make_bars(close, times)
    trades = simulate_series(bars, AlphaParams(axis="vwap"))
    assert len(trades) >= 1
    assert trades[0].side == "long"
    assert trades[0].pnl > 0  # 9.9 买、10.1 卖（对称信号可能产生多笔，首笔即做多盈利）


def test_vwap_short_on_upside_deviation():
    times = _md_times("2025-01-02")
    close = np.concatenate([
        np.full(20, 10.0),
        np.full(5, 10.1),
        np.full(5, 9.9),
        np.full(len(times) - 30, 10.0),
    ])[: len(times)]
    bars = _make_bars(close, times)
    p = AlphaParams(axis="vwap", allow_reverse=True, require_uptrend=False)
    trades = simulate_series(bars, p)
    assert len(trades) >= 1
    assert trades[0].side == "short"
    assert trades[0].pnl > 0


# ---------------------------------------------------------------------------
# OBV 量价背离
# ---------------------------------------------------------------------------
def test_obv_bullish_divergence_signal():
    # 价格升至 10.3 后回落到 9.9（低位），但 OBV 仍在窗口高位 → 看多背离
    close = np.array([10.0, 10.1, 10.2, 10.3, 9.9, 10.4, 10.5, 10.6, 10.7, 10.8])
    times = _md_times("2025-01-02", len(close))
    bars = _make_bars(close, times)
    el, xl, es, xs = al._obv_signals(bars, AlphaParams(axis="obv"))
    # 第 4 根价格 9.9 处应有看多背离
    assert el[4]


def test_obv_end_to_end_long_on_divergence():
    # 先上行积累 OBV，再下探制造背离，再回升触发平仓
    close = np.concatenate([
        np.linspace(10.0, 10.5, 15),   # 上行，OBV 累积
        np.linspace(10.5, 9.9, 10),    # 下探到 9.9（背离区）
        np.linspace(9.9, 10.4, 15),    # 回升（回归 / 触发平仓）
        np.full(8, 10.2),
    ])
    times = _md_times("2025-01-02", len(close))
    bars = _make_bars(close, times)
    p = AlphaParams(axis="obv", allow_reverse=True, require_uptrend=False, max_hold_bars=200)
    trades = simulate_series(bars, p)
    assert len(trades) >= 1
    assert trades[0].side == "long"


# ---------------------------------------------------------------------------
# 成交量异常冲击
# ---------------------------------------------------------------------------
def test_volshock_signals_and_trade():
    times = _md_times("2025-01-02")
    n = len(times)
    close = np.full(n, 10.0)
    op = close.copy()
    vol = np.ones(n)
    # 第 30 根放量大涨（向上冲击 → 做多），第 31 根放量大跌（反向冲击 → 平仓）
    close[30] = 10.1
    op[30] = 10.0
    vol[30] = 300.0
    close[31] = 10.0
    op[31] = 10.1
    vol[31] = 300.0
    bars = _make_bars(close, times, open_=op, vol=vol)
    p = AlphaParams(axis="volshock", vs_min_amount=1.0)  # 单元测试用低门槛
    el, xl, es, xs = al._volshock_signals(bars, p)
    assert el[30] and es[31]
    p2 = AlphaParams(axis="volshock", allow_reverse=True, require_uptrend=False,
                    max_hold_bars=200, vs_min_amount=1.0)
    trades = simulate_series(bars, p2)
    assert len(trades) >= 1
    assert trades[0].side == "long"


# ---------------------------------------------------------------------------
# 开盘区间突破（ORB）
# ---------------------------------------------------------------------------
def test_orb_breakout_long_and_revert():
    times = _md_times("2025-01-02")
    n = len(times)
    # 前 6 根区间 9.8~10.2；第 6 根刚突破(10.3)入场，之后拉到 10.8，尾盘回撤到 10.6(仍>区间)
    # 全程不回到区间内 → 持仓至 14:50 强平，出场价 10.6 > 入场 10.3 → 盈利
    close = np.full(n, 10.0)
    high = np.full(n, 10.2)
    low = np.full(n, 9.8)
    close[6] = 10.3
    high[6] = 10.4
    low[6] = 10.2
    close[7:30] = 10.8
    high[7:30] = 10.9
    low[7:30] = 10.7
    close[30:] = 10.6
    high[30:] = 10.7
    low[30:] = 10.5
    bars = _make_bars(close, times, high=high, low=low)
    p = AlphaParams(axis="orb", allow_reverse=True, require_uptrend=False, max_hold_bars=200)
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    assert trades[0].side == "long"
    assert trades[0].pnl > 0


# ---------------------------------------------------------------------------
# 纪律：14:50 强制清零、硬止损
# ---------------------------------------------------------------------------
def test_force_clear_at_1450():
    times = _md_times("2025-01-02")
    n = len(times)
    close = np.full(n, 10.0)
    # 第 5 根即低于 VWAP 触发做多，一直不回归 → 14:50 强平
    close[5:] = 9.8
    bars = _make_bars(close, times)
    p = AlphaParams(axis="vwap", max_hold_bars=10 ** 9, stop_pct=10.0)
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    assert trades[0].forced is True


def test_hard_stop_limits_loss():
    times = _md_times("2025-01-02")
    n = len(times)
    # 20 根 10.0 → 5 根 9.9（做多入场）→ 9.8 → 9.74（击穿 −1.5% 硬止损）→ 之后 9.5 低位
    close = np.concatenate([
        np.full(20, 10.0),
        np.full(5, 9.9),
        [9.8, 9.74],
        np.full(n - 27, 9.5),
    ])[:n]
    bars = _make_bars(close, times)
    trades = simulate_series(bars, AlphaParams(axis="vwap"))
    assert len(trades) >= 1
    # 首笔做多应在硬止损位附近平仓，亏损被 −1.5% 量级封顶（非无限扩大）
    assert trades[0].side == "long"
    assert -2.5 < trades[0].pnl_pct < 0


# ---------------------------------------------------------------------------
# 真实数据冒烟（无本地数据则跳过）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("axis", ["vwap", "obv", "volshock", "orb"])
def test_alpha_real_smoke(axis):
    bars = al._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    for gated in (False, True):
        p = AlphaParams(axis=axis)
        if gated:
            p.trend_filter = True
            p.min_daily_amp_pct = 2.5
            p.min_daily_amount = 1e8
        trades = simulate_series(bars, p)
        agg = aggregate_trades(trades)
        assert agg["rounds"] >= 0


# ---------------------------------------------------------------------------
# ATR 风险层：用 ATR×mult 替代固定百分比硬止损，应更早截断小赢大亏尾
# ---------------------------------------------------------------------------
def test_atr_stop_tighter_than_fixed():
    # 20 根 10.0 预热 → 之后每根 −0.1% 持续阴跌（VWAP 触发做多）
    # 固定 1.5% 止损要跌到 −1.5% 才触发；ATR≈每根波动（~0.1%）在首根阴跌即 −0.1% 截断
    times = _md_times("2025-01-02")
    n = len(times)
    close = np.full(n, 10.0)
    close[20:] = 9.9 * np.power(0.999, np.arange(n - 20))  # 入场后每根约 −0.1% 持续阴跌
    bars = _make_bars(close, times)
    fixed = simulate_series(bars, AlphaParams(axis="vwap", stop_pct=0.015))
    atr = simulate_series(bars, AlphaParams(axis="vwap", atr_stop_mult=1.0, atr_period=5))
    assert len(fixed) >= 1 and len(atr) >= 1
    # ATR 止损应明显更浅（亏损更接近 0）than 固定 1.5%
    assert atr[0].pnl_pct > fixed[0].pnl_pct
    assert atr[0].pnl_pct > -0.5  # 被 ATR 量级（~0.1%）封顶，远小于 −1.5%


# ---------------------------------------------------------------------------
# 组合层 #148
# ---------------------------------------------------------------------------
def test_combo_single_axis_ensemble_equals_single():
    times = _md_times("2025-01-02")
    close = np.concatenate([
        np.full(20, 10.0),
        np.full(5, 9.9),
        np.full(5, 10.1),
        np.full(len(times) - 30, 10.0),
    ])[: len(times)]
    bars = _make_bars(close, times)
    single = simulate_series(bars, AlphaParams(axis="vwap"))
    combo = simulate_series_combo(bars, ComboParams(axes=("vwap",), combo_mode="ensemble"))
    assert len(combo) == len(single)
    for a, b in zip(combo, single):
        assert a.side == b.side and a.entry_idx == b.entry_idx and a.exit_idx == b.exit_idx


def test_combo_portfolio_sums_sleeves():
    times = _md_times("2025-01-02")
    close = np.concatenate([
        np.full(20, 10.0),
        np.full(5, 9.9),
        np.full(5, 10.1),
        np.full(len(times) - 30, 10.0),
    ])[: len(times)]
    bars = _make_bars(close, times)
    # portfolio(VWAP,ORB) 至少应 ≥ 单独 VWAP 的笔数（多袖叠加）
    p_vwap = simulate_series(bars, AlphaParams(axis="vwap"))
    combo = simulate_series_combo(bars, ComboParams(axes=("vwap", "orb"), combo_mode="portfolio"))
    assert len(combo) >= len(p_vwap)


def test_combo_ensemble_conflict_blocks_entry():
    # 构造：同一根同时 VWAP 做多(价跌破 VWAP) 且 ORB 做空(跌破开盘区间)，共识应冲突不开仓
    times = _md_times("2025-01-02")
    n = len(times)
    close = np.full(n, 10.0)
    high = np.full(n, 10.3)
    low = np.full(n, 9.7)
    # 前 6 根在 10.0 附近定义 ORB 区间 9.7~10.3
    # 第 7 根直接跌到 9.6（既跌破 VWAP 触发 VWAP 做多，又跌破 ORB 区间触发 ORB 做空 → 冲突）
    close[7:] = 9.6
    low[7:] = 9.5
    high[7:] = 9.65
    bars = _make_bars(close, times, high=high, low=low)
    combo = simulate_series_combo(
        bars, ComboParams(axes=("vwap", "orb"), combo_mode="ensemble",
                          max_hold_bars=200, stop_pct=10.0, orb_trade_until="15:00"))
    # 冲突持续全天（ORB 全时段激活）：VWAP 做多 与 ORB 做空 同根成立 → 共识冲突不开仓 → 0 笔
    assert len(combo) == 0


@pytest.mark.parametrize("mode", ["ensemble", "portfolio"])
@pytest.mark.parametrize("axes", [("vwap", "orb"), ("vwap", "orb", "obv")])
def test_combo_real_smoke(mode, axes):
    bars = al._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    cp = ComboParams(axes=axes, combo_mode=mode)
    trades = simulate_series_combo(bars, cp)
    agg = aggregate_trades(trades)
    assert agg["rounds"] >= 0
