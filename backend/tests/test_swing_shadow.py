# -*- coding: utf-8 -*-
"""摆动影子（BACKTEST_LOG #120）纯函数测试：只锁口径，不锁收益数字。

重点是前视偏差：极值天然是事后概念，任何「先标好全区间高低点再对齐买卖」的写法
都会让回测虚高。这里用「前缀重算 == 全量重算后按 confirmed_bar 过滤」来锁死它。
"""
import numpy as np
import pandas as pd
import pytest

from app.analysis.baseline import (
    BaselineConfig,
    anchor_windows,
    classify_status,
    configs_from_settings,
    deviation_class,
    find_anchor,
    relative_strength,
    rolling_zscore,
    scan_symbol,
    summarize,
    wilder_atr,
)
from app.analysis.swing import (
    SwingConfig,
    detect_swings,
    pair_swings,
    remaining_amplitude,
    visible_swings,
)

_CFG = SwingConfig(threshold_pct=0.05, atr_mult=0.0, confirm_bars=2, min_gap=2)


def _v_shape() -> pd.Series:
    return pd.Series(
        [100, 96, 92, 88, 84, 80, 84, 88, 92, 96, 100, 104,
         108, 112, 108, 104, 100, 96, 92, 88, 84, 80, 84, 88],
        dtype=float,
    )


def test_pivot_is_confirmed_later_than_it_occurs():
    pts = detect_swings(_v_shape(), config=_CFG)
    assert [p.kind for p in pts] == ["low", "high", "low"]
    first = pts[0]
    assert first.pivot_bar == 5 and first.value == 80.0
    assert first.confirmed_bar == 7 and first.lag_bars == 2
    assert visible_swings(pts, 6) == []
    assert [p.kind for p in visible_swings(pts, 7)] == ["low"]


def test_no_lookahead_prefix_equivalence():
    """逐日重放的结果必须等于全量结果按可见时点过滤，否则就是未来函数。"""
    rng = np.random.default_rng(7)
    vals = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.02, 120)))
    cfg = SwingConfig(threshold_pct=0.02, atr_mult=0.0, confirm_bars=3, min_gap=4)
    full = detect_swings(vals, config=cfg)
    assert full, "构造序列应至少产生一个摆动点"
    for t in range(20, len(vals)):
        prefix = detect_swings(vals.iloc[:t + 1], config=cfg)
        expected = visible_swings(full, t)
        assert [p.as_dict() for p in prefix] == [p.as_dict() for p in expected], f"t={t}"


def test_no_lookahead_with_atr_scale():
    frame = pd.DataFrame({
        "high": np.linspace(101, 130, 120) + np.sin(np.arange(120)),
        "low": np.linspace(99, 128, 120) - np.cos(np.arange(120)),
        "close": np.linspace(100, 129, 120),
    })
    atr = wilder_atr(frame, 14)
    vals = frame["close"]
    cfg = SwingConfig(threshold_pct=0.01, atr_mult=1.0, confirm_bars=3, min_gap=4)
    full = detect_swings(vals, atr=atr, config=cfg)
    for t in (60, 80, 100, 119):
        prefix = detect_swings(vals.iloc[:t + 1], atr=atr.iloc[:t + 1], config=cfg)
        assert [p.as_dict() for p in prefix] == [p.as_dict() for p in visible_swings(full, t)]


def test_min_gap_keeps_only_first_point():
    cfg = SwingConfig(threshold_pct=0.05, atr_mult=0.0, confirm_bars=2, min_gap=100)
    pts = detect_swings(_v_shape(), config=cfg)
    assert len(pts) == 1 and pts[0].kind == "low"


def test_threshold_filters_small_noise():
    noise = pd.Series([100 + (0.2 if i % 2 else -0.2) for i in range(60)], dtype=float)
    assert detect_swings(noise, config=SwingConfig(
        threshold_pct=0.05, atr_mult=0.0, confirm_bars=2, min_gap=2,
    )) == []


def test_remaining_amplitude_shows_cost_of_confirmation():
    vals = _v_shape()
    low, high = pair_swings(detect_swings(vals, config=_CFG))[0]
    item = remaining_amplitude(vals, low, high)
    assert item["lag_bars"] == 2
    assert item["gross_pct"] > item["net_pct"] > 0
    assert item["eaten_pct"] > 0


def test_rs_removes_common_factor():
    bench = pd.Series([100, 102, 104, 108, 106, 110, 114], dtype=float)
    stock = bench * 2.0
    rs = relative_strength(stock, bench)
    assert rs.dropna().nunique() == 1
    assert detect_swings(rs, config=_CFG) == []


def test_rs_rise_does_not_mean_price_rise():
    """大盘 -2% / 个股 -1%：RS 上升但账户是亏的，必须叠加 regime 闸门。"""
    bench = pd.Series([100.0, 98.0])
    stock = pd.Series([100.0, 99.0])
    rs = relative_strength(stock, bench)
    assert stock.iloc[1] < stock.iloc[0]
    assert rs.iloc[1] > rs.iloc[0]


def test_rolling_zscore_and_atr_are_causal():
    s = pd.Series(np.arange(1, 101, dtype=float))
    a = rolling_zscore(s, 20)
    changed = s.copy()
    changed.iloc[80:] = 999.0
    b = rolling_zscore(changed, 20)
    pd.testing.assert_series_equal(a.iloc[:80], b.iloc[:80])

    frame = pd.DataFrame({
        "high": s + 2, "low": s - 2, "close": s,
    })
    atr1 = wilder_atr(frame, 14)
    frame2 = frame.copy()
    frame2.loc[80:, "high"] = 999.0
    pd.testing.assert_series_equal(atr1.iloc[:80], wilder_atr(frame2, 14).iloc[:80])


def test_deviation_class_boundaries():
    cfg = BaselineConfig(band=1.0, extreme_z=2.0)
    assert deviation_class(0.5, cfg) == "resonance"
    assert deviation_class(1.5, cfg) == "mild_positive"
    assert deviation_class(-1.5, cfg) == "weak"
    assert deviation_class(2.5, cfg) == "extreme_positive"
    assert deviation_class(-2.5, cfg) == "extreme_negative"
    assert deviation_class(float("nan"), cfg) == "unknown"


def test_classify_status_priority():
    cfg = BaselineConfig(band=1.0, extreme_z=2.0, min_corr=0.2)
    anchor = {"confirmed_bar": 10, "start": 10, "end": 13}
    assert classify_status(0.0, 0.8, anchor, cfg)[1] == "resonance_buy"
    assert classify_status(0.0, 0.8, None, cfg)[1] == "no_anchor"
    assert classify_status(0.0, 0.1, anchor, cfg)[1] == "excluded_low_corr"
    assert classify_status(3.0, 0.1, anchor, cfg)[1] == "excluded_extreme"
    assert classify_status(float("nan"), 0.8, anchor, cfg)[1] == "excluded_insufficient"


def test_anchor_windows_are_time_filters_not_orders():
    from app.analysis.swing import SwingPoint

    lows = [SwingPoint("low", 8, 10, 100.0, 0.05), SwingPoint("low", 30, 32, 101.0, 0.05)]
    wins = anchor_windows(lows, BaselineConfig(anchor_bars=3))
    assert [w["end"] for w in wins] == [13, 35]
    assert find_anchor(12, wins)["confirmed_bar"] == 10
    assert find_anchor(20, wins) is None


def test_scan_symbol_shape_and_summarize():
    rng = np.random.default_rng(11)
    n = 160
    bench = pd.Series(3000 * np.cumprod(1 + rng.normal(0.0005, 0.008, n)))
    stock = bench * np.cumprod(1 + rng.normal(0.0002, 0.006, n))
    swing = SwingConfig(threshold_pct=0.02, atr_mult=0.0, confirm_bars=3, min_gap=5)
    base = BaselineConfig(z_window=30, corr_window=30, beta_window=30, anchor_bars=3)
    bench_lows = [p for p in detect_swings(bench, config=swing) if p.kind == "low"]
    wins = anchor_windows(bench_lows, base)
    signals, amps = scan_symbol(
        "000001", stock, bench, swing_cfg=swing, base_cfg=base, windows=wins,
    )
    allowed = {
        "resonance_buy", "no_anchor", "excluded_extreme",
        "excluded_low_corr", "excluded_insufficient",
    }
    assert signals, "构造序列应至少产生一条观测信号"
    for row in signals:
        assert row["status"] in allowed
        assert {"code", "bar", "lag_bars", "deviation", "rs_z", "corr"} <= set(row)
    stats = summarize(signals, amps)
    assert stats["signals"] == len(signals)
    assert sum(stats["by_status"].values()) == len(signals)


def test_configs_from_settings_are_valid():
    swing, base = configs_from_settings()
    swing.validate()
    base.validate()
    assert isinstance(swing, SwingConfig) and isinstance(base, BaselineConfig)


@pytest.mark.parametrize("bad", [
    SwingConfig(confirm_bars=0),
    SwingConfig(min_gap=0),
    SwingConfig(threshold_pct=-0.1),
])
def test_invalid_swing_config_rejected(bad):
    with pytest.raises(ValueError):
        detect_swings(_v_shape(), config=bad)
