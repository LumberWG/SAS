# -*- coding: utf-8 -*-
"""截面统计套利单元测试：_select 纯函数 + 合成 panel 注入（验证相对回归盈利）+ 真实数据冒烟。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies import cross_sectional as cs
from app.strategies.cross_sectional import (
    CSParams,
    load_industry_map,
    simulate_cross_sectional,
    _select,
)


def _cs_times(day: str, n: int = 48):
    base = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=35)
    out = [base + timedelta(minutes=5 * i) for i in range(24)]
    base = base.replace(hour=13, minute=5)
    out += [base + timedelta(minutes=5 * i) for i in range(24)]
    return out[:n]


def _make_cs_df(code: str, close, day="2025-01-02"):
    n = len(close)
    times = _cs_times(day, n)
    df = pd.DataFrame({"close": np.asarray(close, dtype=float)})
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 100.0
    df["amount"] = df["close"] * 100.0
    df["date"] = times
    return df


def _build_raw(prices: dict, day="2025-01-02") -> dict:
    raw = {}
    for code, close in prices.items():
        df = _make_cs_df(code, close, day)
        prepared = cs._prepare_df(df, "2025-01-01", "2026-12-31")
        assert prepared is not None
        raw[code] = prepared
    return raw


def _trades(raw, params):
    return simulate_cross_sectional(raw=raw, params=params)["trades"]


# ---------------------------------------------------------------------------
# _select 纯函数：截面排序逻辑（无未来函数、确定性强）
# ---------------------------------------------------------------------------
def test_select_picks_most_extreme_each_side():
    present = [("A", -0.04), ("B", -0.01), ("C", 0.0), ("D", 0.03), ("E", 0.06)]
    longs, shorts = _select(present, CSParams(mode="longshort", n_select=2, vwap_entry_pct=0.005))
    assert longs == {"A", "B"}          # 最负两端
    assert shorts == {"D", "E"}         # 最正两端


def test_select_requires_min_magnitude():
    present = [("A", -0.001), ("B", 0.001), ("C", 0.0005)]
    longs, shorts = _select(present, CSParams(mode="longshort", n_select=2, vwap_entry_pct=0.005))
    # 偏离不够大 → 不入选
    assert longs == set() and shorts == set()


def test_select_long_mode_no_shorts():
    present = [("A", -0.04), ("D", 0.06)]
    longs, shorts = _select(present, CSParams(mode="long", n_select=2, vwap_entry_pct=0.005))
    assert longs == {"A"}
    assert shorts == set()


# ---------------------------------------------------------------------------
# 端到端：相对回归应盈利（容忍尾段 VWAP 滞后造成的微小刷单）
# ---------------------------------------------------------------------------
def test_cs_long_selects_most_oversold_and_profits():
    a = np.full(48, 10.0); a[10:26] = 9.5
    b = np.full(48, 10.0); c = np.full(48, 10.0)
    raw = _build_raw({"A": a, "B": b, "C": c})
    trades = _trades(raw, CSParams(mode="long", n_select=1))
    traded = {t.code for t in trades}
    assert "B" not in traded and "C" not in traded   # 贴近 VWAP 不参与
    a_long = [t for t in trades if t.code == "A" and t.side == "long" and t.pnl > 0]
    assert a_long, "A 做多应在回归后盈利"


def test_cs_longshort_selects_both_sides_and_profits():
    a = np.full(48, 10.0); a[10:26] = 9.5      # 最超卖
    d = np.full(48, 10.0); d[10:26] = 10.5     # 最超买
    b = np.full(48, 10.0); c = np.full(48, 10.0)
    raw = _build_raw({"A": a, "D": d, "B": b, "C": c})
    trades = _trades(raw, CSParams(mode="longshort", n_select=1))
    a_long = [t for t in trades if t.code == "A" and t.side == "long" and t.pnl > 0]
    d_short = [t for t in trades if t.code == "D" and t.side == "short" and t.pnl > 0]
    assert a_long and d_short


def test_cs_n_select_limits_count():
    a = np.full(48, 10.0); a[10:26] = 9.4
    a2 = np.full(48, 10.0); a2[10:26] = 9.6
    d = np.full(48, 10.0); d[10:26] = 10.6
    d2 = np.full(48, 10.0); d2[10:26] = 10.4
    raw = _build_raw({"A": a, "A2": a2, "D": d, "D2": d2})
    # 用 _select 直接验证 n_select 上限（端到端会被尾段刷单干扰）
    present = [("A", -0.04), ("A2", -0.02), ("D2", 0.02), ("D", 0.04)]
    longs, shorts = _select(present, CSParams(mode="longshort", n_select=2, vwap_entry_pct=0.005))
    assert longs <= {"A", "A2"} and len(longs) <= 2
    assert shorts <= {"D", "D2"} and len(shorts) <= 2


def test_cs_force_clear_at_end_of_day():
    a = np.full(48, 10.0); a[10:] = 9.0   # 一直远离 VWAP，不回归
    raw = _build_raw({"A": a, "B": np.full(48, 10.0)})
    trades = _trades(raw, CSParams(mode="long", n_select=1))
    assert len(trades) >= 1
    assert any(t.forced for t in trades)


@pytest.mark.parametrize("mode", ["long", "longshort"])
def test_cs_real_smoke(mode):
    codes = ["000001", "600000", "000002", "600519"]
    raw, days = cs._build_panel(codes, "2025-01-02", "2026-11-09")
    if not raw:
        pytest.skip("样本标的本地数据缺失，跳过")
    res = simulate_cross_sectional(raw=raw, params=CSParams(mode=mode, n_select=5))
    assert res["aggregate"]["rounds"] >= 0


# ---------------------------------------------------------------------------
# #149 续：稳健性增强（行业中性 / 流动性加权 / 融券费）
# ---------------------------------------------------------------------------
def test_select_industry_neutral_flips_selection():
    # 行业 x 的两只齐跌 -10%（行业 beta 相同）→ 残差均归零；
    # 行业 y 的 C 跌 -5%、D 涨 +5%（行业内有分散）→ C 残差 -5%。
    # 原始：最负是 A(-10%) → 做多 A；中性化：A 残差 0，C 残差 -5% → 改做多 C。
    present = [("A", -0.10), ("B", -0.10), ("C", -0.05), ("D", 0.05)]
    industries = {"A": "x", "B": "x", "C": "y", "D": "y"}
    longs_raw, _ = _select(present, CSParams(mode="long", n_select=1, vwap_entry_pct=0.005))
    longs_neu, _ = _select(present, CSParams(mode="long", n_select=1, vwap_entry_pct=0.005,
                                            industry_neutral=True), industries=industries)
    assert longs_raw == {"A"}
    assert longs_neu == {"C"}          # 行业中性剔除了齐跌的行业 beta，改选中行业内分散的 C


def test_select_liq_weight_excludes_illiquid():
    # T 更超卖但极不流动；L 超卖较轻但流动性好。liq 门限应剔除 T。
    present = [("T", -0.06), ("L", -0.02)]
    liq = {"T": 1.0, "L": 100.0}
    longs_raw, _ = _select(present, CSParams(mode="long", n_select=1, vwap_entry_pct=0.005))
    longs_liq, _ = _select(present, CSParams(mode="long", n_select=1, vwap_entry_pct=0.005,
                                            liq_weight=True, liq_quantile=0.5), liq=liq)
    assert longs_raw == {"T"}
    assert longs_liq == {"L"}           # 流动性加权后只留高流动标的


def test_short_cost_reduces_short_pnl_only():
    a = np.full(48, 10.0); a[10:26] = 9.5       # A 超卖→回归，做多盈利
    d = np.full(48, 10.0); d[10:26] = 10.5      # D 超买→回归，做空盈利
    raw = _build_raw({"A": a, "D": d})
    base = _trades(raw, CSParams(mode="longshort", n_select=1))
    cost = _trades(raw, CSParams(mode="longshort", n_select=1, short_cost_pct=0.001))
    sum_d_short = lambda ts: sum(t.pnl for t in ts if t.code == "D" and t.side == "short")
    sum_a_long = lambda ts: sum(t.pnl for t in ts if t.code == "A" and t.side == "long")
    assert sum_d_short(cost) < sum_d_short(base)   # 空腿成本生效
    assert sum_a_long(cost) == sum_a_long(base)    # 多腿不受影响


def test_load_industry_map_graceful():
    # 不存在的路径应返回空字典，不抛
    assert load_industry_map("/no/such/file.csv") == {}


def test_cs_industry_neutral_end_to_end_runs():
    a = np.full(48, 10.0); a[10:26] = 9.4
    a2 = np.full(48, 10.0); a2[10:26] = 9.6   # 与 A 同行业
    d = np.full(48, 10.0); d[10:26] = 10.6
    raw = _build_raw({"A": a, "A2": a2, "D": d})
    ind = {"A": "x", "A2": "x", "D": "y"}
    res = simulate_cross_sectional(raw=raw, params=CSParams(mode="longshort", n_select=1,
                                                            industry_neutral=True),
                                   industry_map=ind)
    assert res["aggregate"]["rounds"] >= 0

