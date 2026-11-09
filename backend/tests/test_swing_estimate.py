# -*- coding: utf-8 -*-
"""事前预估（BACKTEST_LOG #121）纯函数测试：只锁因果口径，不锁收益数字。

最要紧的是「标签窗口没走完的样本绝不入库」：预估一旦允许用当时的未来数据，
校准度和命中率都会虚高，这正是上一轮拒绝「图上标好极值再对齐买卖」的同一条红线。
"""
import numpy as np
import pandas as pd
import pytest

from app.analysis.baseline import relative_strength
from app.analysis.estimate import (
    EstimateConfig,
    SymbolScan,
    TurnEstimator,
    TurnSample,
    build_feature_buckets,
    build_samples_and_queries,
    estimate_config_from_settings,
    evaluate,
)
from app.analysis.swing import SwingConfig, detect_swings

_CFG = EstimateConfig(
    horizon=2, prior_strength=10.0, symbol_cap=5, entry_lift=2.0,
    cost_pct=0.3, min_exp_net=0.0, z_window=20, confirm_bars=2,
    min_global_samples=1,
)


def _series(seed: int, n: int = 200) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    bench = pd.Series(3000 * np.cumprod(1 + rng.normal(0.0004, 0.009, n)))
    stock = bench * np.cumprod(1 + rng.normal(0.0002, 0.012, n))
    return stock, bench


def _scan(seed: int, cfg: EstimateConfig = _CFG) -> SymbolScan:
    stock, bench = _series(seed)
    rs = relative_strength(stock, bench)
    pts = detect_swings(
        rs, config=SwingConfig(threshold_pct=0.02, atr_mult=0.0, confirm_bars=2, min_gap=4)
    )
    return SymbolScan(code=f"{seed:06d}", rs=rs, points=pts, first_bar=65)


def test_feature_buckets_are_causal():
    scan = _scan(3)
    _, bench = _series(3)
    buckets = build_feature_buckets(scan.rs, scan.points, bench, _CFG)
    changed = scan.rs.copy()
    changed.iloc[120:] = changed.iloc[120:] * 1.8
    # 摆动点沿用原序列：距最近已确认点这一列也必须只按 confirmed_bar 推进
    after = build_feature_buckets(changed, scan.points, bench, _CFG)
    np.testing.assert_array_equal(buckets[:120], after[:120])
    assert buckets.shape == (len(scan.rs), 5)


def test_samples_never_available_before_label_window_closes():
    scan = _scan(5)
    _, bench = _series(5)
    buckets = build_feature_buckets(scan.rs, scan.points, bench, _CFG)
    samples, queries = build_samples_and_queries(scan, buckets, _CFG)
    assert samples and len(samples) == len(queries)
    highs = sorted((p.confirmed_bar for p in scan.points if p.kind == "high"))
    for s, q in zip(samples, queries):
        assert s.available_bar > q.bar, "样本不得在特征 bar 当天就可见"
        assert s.y_turn == q.y_truth
        if q.y_truth == 1 and q.forward_pct > 0:
            # 用到了配对高点的 pivot 值，必须等该高点被确认
            assert s.available_bar in highs or s.available_bar >= q.bar + _CFG.horizon + 1


def test_estimator_only_ingests_available_samples():
    key = (1, 1, 1, 0, 1)
    now = TurnSample("A", 10, key, 1, 0.0, 8.0)
    future = TurnSample("A", 500, key, 0, -30.0, 0.0)
    est = TurnEstimator(_CFG, [future, now])
    est.advance_to(10)
    first = est.estimate("A", key)
    assert first is not None and first.global_samples == 1
    assert first.p_turn == pytest.approx(2 / 3)
    est.advance_to(500)
    later = est.estimate("A", key)
    assert later.global_samples == 2 and later.p_turn < first.p_turn


def test_base_rate_is_online_not_full_sample():
    """发信号门槛用的基础率只能来自当时已入库样本，未来正样本不许提前拉低门槛。"""
    key = (1, 1, 1, 0, 1)
    samples = [TurnSample("A", 100, key, 1, 0.0, 8.0) for _ in range(100)]
    est = TurnEstimator(_CFG, samples)
    est.advance_to(10)
    assert est.base_rate() == pytest.approx(0.5)  # 冷启动：100 个未来正样本一个都没入库
    est.advance_to(100)
    assert est.base_rate() > 0.9


def test_symbol_shrinkage_is_bounded_and_zero_without_history():
    key = (2, 0, 1, 0, 0)
    samples = [TurnSample("A", 1, key, 1, 0.0, 10.0) for _ in range(20)]
    est = TurnEstimator(_CFG, samples)
    est.advance_to(1)
    cold = est.estimate("B", key)
    assert cold is not None
    assert cold.symbol_samples == 0 and cold.symbol_weight == 0.0
    warm = est.estimate("A", key)
    assert warm.symbol_weight == pytest.approx(5 / 15)
    assert warm.p_turn <= cold.p_turn + 1e-9
    assert warm.exp_net_pct == pytest.approx(
        warm.p_turn * warm.exp_upside_pct
        - (1 - warm.p_turn) * warm.exp_downside_pct - _CFG.cost_pct
    )


def test_min_global_samples_blocks_early_estimates():
    key = (0, 0, 0, 0, 0)
    est = TurnEstimator(
        EstimateConfig(**{**_CFG.__dict__, "min_global_samples": 5}),
        [TurnSample("A", 1, key, 1, 0.0, 8.0)],
    )
    est.advance_to(1)
    assert est.estimate("A", key) is None


def test_evaluate_returns_calibration_and_verdict():
    scans = [_scan(s) for s in (7, 8, 9)]
    _, bench = _series(7)
    cfg = EstimateConfig(**{**_CFG.__dict__, "entry_lift": 1.1})
    out = evaluate(scans, bench, cfg)
    assert out["samples"] > 0 and out["queries"] > 0
    assert 0.0 <= out["base_rate"] <= 1.0
    assert 0.0 < out["median_entry_threshold"] <= cfg.entry_lift
    assert out["estimated_queries"] <= out["queries"]
    assert 0.0 <= out["brier_score"] <= 1.0
    assert 0.0 <= out["brier_baseline"] <= 1.0
    assert out["brier_skill"] is not None
    assert {"mean_realized_net_pct", "median_hit_forward_pct", "base_rate"} <= set(out)
    assert out["verdict"] in {"insufficient_samples", "below_breakeven", "above_breakeven"}
    assert sum(b["count"] for b in out["calibration_bins"]) == out["estimated_queries"]
    assert {"hit_rate", "breakeven_hit_pct", "median_signal_forward_pct"} <= set(out)
    assert isinstance(out["recent_signals"], list)


def test_evaluate_hit_rate_matches_signal_hits():
    scans = [_scan(s) for s in (11, 12)]
    _, bench = _series(11)
    cfg = EstimateConfig(**{**_CFG.__dict__, "entry_lift": 1.01, "min_exp_net": -1e9})
    out = evaluate(scans, bench, cfg)
    if out["predicted_signals"]:
        assert 0.0 <= out["hit_rate"] <= 1.0
        assert out["hit_rate"] == round(out["hit_rate"], 4)
    if out["breakeven_hit_pct"] is not None:
        assert 0.0 < out["breakeven_hit_pct"] < 100.0


def test_config_from_settings_is_valid():
    cfg = estimate_config_from_settings()
    cfg.validate()
    assert isinstance(cfg, EstimateConfig)


@pytest.mark.parametrize("bad", [
    EstimateConfig(horizon=0),
    EstimateConfig(prior_strength=0),
    EstimateConfig(entry_lift=1.0),
    EstimateConfig(z_window=2),
])
def test_invalid_estimate_config_rejected(bad):
    with pytest.raises(ValueError):
        bad.validate()
