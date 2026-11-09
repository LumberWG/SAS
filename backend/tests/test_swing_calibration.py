# -*- coding: utf-8 -*-
"""在线概率校准与滚动 walk-forward（BACKTEST_LOG #123）的纯函数测试。

最要紧的两条：
1. **校准器只能吃标签窗口已走完的样本** —— 否则就是「用未来标签修当前概率」，
   比不做校准更危险（会让样本外看起来比实际准）。
2. **Platt 只改水平不改排序** —— a > 0 时必须单调，不能悄悄改变选股顺序。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.analysis.baseline import relative_strength
from app.analysis.estimate import (
    EstimateConfig,
    PlattCalibrator,
    SymbolScan,
    TurnEstimator,
    TurnSample,
    adjust_multiple_tests,
    build_dataset,
    evaluate_dataset,
    walk_forward_dataset,
    _fold_stats,
)
from app.analysis.swing import SwingConfig, detect_swings

_CFG = EstimateConfig(
    horizon=2, prior_strength=10.0, symbol_cap=5, entry_lift=2.0,
    cost_pct=0.3, min_exp_net=0.0, z_window=20, confirm_bars=2,
    min_global_samples=1, calibration="platt",
    calib_min_samples=20, calib_refit_every=5,
)


def _series(seed: int, n: int = 400) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    bench = pd.Series(3000 * np.cumprod(1 + rng.normal(0.0004, 0.009, n)))
    stock = bench * np.cumprod(1 + rng.normal(0.0002, 0.012, n))
    return stock, bench


def _scan(seed: int) -> SymbolScan:
    stock, bench = _series(seed)
    rs = relative_strength(stock, bench)
    pts = detect_swings(
        rs, config=SwingConfig(threshold_pct=0.02, atr_mult=0.0, confirm_bars=2, min_gap=4)
    )
    return SymbolScan(code=f"{seed:06d}", rs=rs, points=pts, first_bar=65)


def _sample(avail: int, y: int, key=(1, 2, 3, 0, 1)) -> TurnSample:
    return TurnSample(
        code="000001", available_bar=avail, key=key, y_turn=y,
        downside_pct=-2.0, upside_pct=6.0,
    )


# ---------------------------------------------------------------- 校准因果性

def test_calibrator_ingests_only_matured_labels():
    est = TurnEstimator(_CFG, [_sample(10, 1), _sample(12, 0)])
    est.advance_to(30)
    est.observe(available_bar=100, p_raw=0.2, y=1)
    est.advance_to(99)
    assert est.calib.n == 0          # 标签窗口没走完，一条都不能进
    est.advance_to(100)
    assert est.calib.n == 1


def test_future_labels_do_not_change_current_prediction():
    """把 t 之后的标签全部翻转，t 时点的 p_turn 必须一模一样。"""
    base = [_sample(i, 1 if i % 3 else 0) for i in range(1, 120)]
    flipped = [
        TurnSample(
            code=s.code, available_bar=s.available_bar, key=s.key,
            y_turn=(1 - s.y_turn) if s.available_bar > 60 else s.y_turn,
            downside_pct=s.downside_pct, upside_pct=s.upside_pct,
        )
        for s in base
    ]
    a = TurnEstimator(_CFG, base)
    b = TurnEstimator(_CFG, flipped)
    for est in (a, b):
        est.advance_to(60)
    # 用同一个 key 查询；两者在 60 之前看到的样本完全相同（翻转只发生在 >60）
    pa = a.estimate("000001", (1, 2, 3, 0, 1))
    pb = b.estimate("000001", (1, 2, 3, 0, 1))
    assert pa is not None and pb is not None
    assert pa.p_turn == pytest.approx(pb.p_turn)


def test_calibration_is_inactive_before_min_samples():
    cal = PlattCalibrator(_CFG)
    for i in range(19):
        cal.add(0.1 * i, 1)
    assert not cal.ready
    assert cal.apply(0.17) == 0.17       # 未就绪时严格恒等，不能偷偷改数
    cal.add(0.2, 0)
    assert cal.ready


# ---------------------------------------------------------------- Platt 口径

def test_platt_recovers_slope_on_underestimated_probability():
    """真实频率是 sigmoid(1.7·x) 而模型给的是 sigmoid(x)：校准应把 a 拉回 1.7 附近。"""
    rng = np.random.default_rng(7)
    cfg = EstimateConfig(**{**_CFG.__dict__, "calib_l2": 0.5})
    cal = PlattCalibrator(cfg)
    xs = rng.normal(0.0, 1.0, 6000)
    true_p = 1.0 / (1.0 + np.exp(-1.7 * xs))
    ys = (rng.random(xs.size) < true_p).astype(int)
    for x, y in zip(xs, ys):
        cal.add(float(x), int(y))
    cal.refit()
    assert cal.ready
    assert 1.55 < cal.a < 1.85
    assert abs(cal.b) < 0.15


def test_platt_preserves_ranking():
    cal = PlattCalibrator(_CFG)
    for i in range(200):
        cal.add(-2.0 + i * 0.02, 1 if i % 4 else 0)
    cal.refit()
    ps = [0.02, 0.05, 0.08, 0.12, 0.2, 0.35, 0.5]
    out = [cal.apply(p) for p in ps]
    assert out == sorted(out)            # 单调，不改变选股顺序
    assert all(0.0 < v < 1.0 for v in out)


def test_calibration_keeps_raw_probability_for_comparison():
    """同一份数据要同时留「校准前」与「校准后」两套指标，否则无从判断校准值多少。"""
    scans = [_scan(s) for s in (41, 42, 43)]
    _, bench = _series(41)
    cfg = EstimateConfig(**{**_CFG.__dict__, "calibration": "platt"})
    ds = build_dataset(scans, bench, cfg)
    off = evaluate_dataset(ds, cfg)
    on = evaluate_dataset(ds, EstimateConfig(**{**cfg.__dict__, "calibration": "none"}))
    # 关闭校准后 calibration_before 应与最终指标完全一致（没有第二次变换）
    assert off["calibrator"]["kind"] == "platt"
    assert on["calibrator"] == {"kind": "none"}
    assert on["brier_score"] == on["brier_score_raw"]
    assert on["calibration_bias"] == on["calibration_before"]["calibration_bias"]
    # 开校准后原始概率仍被完整保留，便于对照
    assert on["brier_score_raw"] is not None
    assert on["calibrated_share"] == 0.0


def test_window_evaluation_does_not_cold_start_calibrator():
    """只评估后段窗口时，校准器仍必须带着窗口之前学到的映射，而不是从零开始。

    否则样本外评估会把「已经训练好的状态」抹掉，Platt 会被误判成有害（#123 初版）。
    """
    scans = [_scan(s) for s in (71, 72, 73)]
    _, bench = _series(71)
    cfg = EstimateConfig(**{**_CFG.__dict__, "calibration": "platt"})
    ds = build_dataset(scans, bench, cfg)
    bars = [q.bar for q in ds.queries]
    mid = bars[len(bars) // 2]
    late = evaluate_dataset(ds, cfg, bar_min=mid)
    assert late["calibrator"]["samples"] > 0        # 窗口前样本已进校准器
    assert late["calibrated_share"] and late["calibrated_share"] > 0


# ---------------------------------------------------------------- walk-forward

def test_fold_stats_flags_inconsistency():
    good = _fold_stats([0.03, 0.02, 0.04, 0.01, 0.03])
    assert good["positive_folds"] == 5 and good["consistency"] == 1.0
    assert good["t_stat"] > 0 and good["p_value_one_sided"] < 0.05
    mixed = _fold_stats([0.06, -0.05, 0.05, -0.04, 0.01])
    assert mixed["positive_folds"] == 3 and mixed["consistency"] == 0.6
    assert mixed["t_stat"] < good["t_stat"]


def test_walk_forward_folds_are_disjoint_and_causal():
    scans = [_scan(s) for s in (51, 52, 53)]
    _, bench = _series(51)
    cfg = EstimateConfig(**{**_CFG.__dict__, "calibration": "none"})
    ds = build_dataset(scans, bench, cfg)
    out = walk_forward_dataset(ds, cfg, folds=4)
    folds = out["folds"]
    assert 2 <= len(folds) <= 4
    for prev, nxt in zip(folds, folds[1:]):
        assert prev["bar_max"] < nxt["bar_min"]        # 不重叠、不回头
    assert out["pooled"]["estimated_queries"] >= sum(f["estimated_queries"] for f in folds) - len(folds)
    assert out["p_value_method"] == "normal_approx"


def test_walk_forward_requires_enough_queries():
    scans = [_scan(61)]
    _, bench = _series(61)
    cfg = EstimateConfig(**{**_CFG.__dict__, "calibration": "none"})
    ds = build_dataset(scans, bench, cfg)
    out = walk_forward_dataset(ds, cfg, folds=10, bar_min=300, bar_max=320)
    assert out["folds"] == [] and out["fold_count"] == 0
    assert "不足以分折" in out["note"]


# ---------------------------------------------------------------- 多重检验

def test_bonferroni_rejects_best_of_many():
    weak = adjust_multiple_tests(0.01, 81, consistency=1.0)
    assert weak["significant"] is False          # 1% 在 81 组试验里不算显著
    assert weak["alpha_bonferroni"] == pytest.approx(0.05 / 81, abs=1e-6)
    strong = adjust_multiple_tests(1e-5, 81, consistency=0.8)
    assert strong["significant"] is True
    unstable = adjust_multiple_tests(1e-5, 81, consistency=0.4)
    assert unstable["significant"] is False      # 方向不一致也不认
    assert adjust_multiple_tests(None, 81)["significant"] is False


@pytest.mark.parametrize("bad", [
    EstimateConfig(calibration="isotonic"),
    EstimateConfig(calib_min_samples=1),
    EstimateConfig(calib_refit_every=0),
    EstimateConfig(calib_l2=0.0),
])
def test_invalid_calibration_config_rejected(bad):
    with pytest.raises(ValueError):
        bad.validate()


def test_platt_handles_degenerate_input():
    """全部正例 / 全部负例时 L2 必须保证参数有界，不能发散成 ±inf。"""
    cal = PlattCalibrator(_CFG)
    for _ in range(50):
        cal.add(-1.5, 1)
    cal.refit()
    assert math.isfinite(cal.a) and math.isfinite(cal.b)
    assert 0.0 < cal.apply(0.1) < 1.0
