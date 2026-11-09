# -*- coding: utf-8 -*-
"""#122 特征敏感性网格：锁因果性、等频校准、时间切分等价与网格防过拟合口径。

最要紧的两条：
- 新增的基准/领先型特征不许偷看未来（含 np.roll 绕回头部这个经典坑）；
- dev / oos 两段评估相加必须等于整段，否则「样本外只验证一次」就失去意义。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.analysis.baseline import relative_strength
from app.analysis.estimate import (
    BASE_FEATURES,
    FEATURE_NAMES,
    EstimateConfig,
    EstimateDataset,
    SymbolScan,
    build_dataset,
    build_feature_buckets,
    evaluate_dataset,
    parse_features,
)
from app.analysis.swing import SwingConfig, detect_swings

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("estimate_grid_under_test", _SCRIPTS / "estimate_grid.py")
grid = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(grid)

_FULL = "z,streak,since,vol,regime,bench_mom,lead,bench_z"
_CFG = EstimateConfig(
    horizon=2, prior_strength=10.0, symbol_cap=5, entry_lift=1.5,
    cost_pct=0.3, min_exp_net=-1e9, z_window=20, confirm_bars=2,
    min_global_samples=1,
)


def _series(seed: int, n: int = 220) -> tuple[pd.Series, pd.Series]:
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


def test_parse_features_rules():
    assert parse_features(BASE_FEATURES) == FEATURE_NAMES[:5]
    assert parse_features(_FULL) == FEATURE_NAMES
    assert parse_features("lead,z") == ("z", "lead")  # z 强制排首位，掩码语义不丢
    for bad in ("", "streak,vol", "z,unknown", "z,z", "z,z,lead"):
        with pytest.raises(ValueError):
            parse_features(bad)
    with pytest.raises(ValueError):
        EstimateConfig(features="streak").validate()


def test_default_feature_matrix_unchanged_and_full_has_eight_cols():
    scan = _scan(3)
    _, bench = _series(3)
    base = build_feature_buckets(scan.rs, scan.points, bench, _CFG)
    full = build_feature_buckets(scan.rs, scan.points, bench, _CFG, features=_FULL)
    assert base.shape == (len(scan.rs), 5)
    assert full.shape == (len(scan.rs), 8)
    np.testing.assert_array_equal(base, full[:, :5])
    assert base[:, 0].min() == -1  # z 窗口掩码仍在第一列


def test_new_features_are_causal_in_both_rs_and_benchmark():
    """改掉 rs / bench 的后半段，前半段 8 列必须逐列不变。"""
    scan = _scan(5)
    _, bench = _series(5)
    before = build_feature_buckets(scan.rs, scan.points, bench, _CFG, features=_FULL)
    rs2 = scan.rs.copy()
    rs2.iloc[140:] = rs2.iloc[140:] * 1.7
    bench2 = bench.copy()
    bench2.iloc[140:] = bench2.iloc[140:] * 0.8
    after = build_feature_buckets(rs2, scan.points, bench2, _CFG, features=_FULL)
    np.testing.assert_array_equal(before[:140], after[:140])
    assert not np.array_equal(before[140:], after[140:])  # 后半段确实受影响，测试不是空转


def test_equal_frequency_reliability_covers_all_pairs():
    scans = [_scan(s) for s in (7, 8, 9, 10)]
    _, bench = _series(7)
    ds = build_dataset(scans, bench, _CFG)
    out = evaluate_dataset(ds, _CFG)
    bins = out["calibration_bins"]
    assert 0 < len(bins) <= 10
    assert sum(b["count"] for b in bins) == out["estimated_queries"]
    assert out["calibration_kind"] == "equal_frequency_deciles"
    for b in bins:
        assert {"p_low", "p_high", "predicted", "actual"} <= set(b)
        assert b["p_low"] <= b["p_high"]
        assert 0 <= b["predicted"] <= 1 and 0 <= b["actual"] <= 1
    assert 0 <= out["calibration_ece"] <= 1
    assert -1 <= out["calibration_bias"] <= 1
    ps = [b["predicted"] for b in bins]
    assert ps == sorted(ps)  # 等频分箱按预测概率排序，必须单调


def test_time_split_is_exactly_complementary():
    """dev + oos 的查询数与逐样本 Brier 加权平均必须还原整段（无泄漏、无丢失）。"""
    scans = [_scan(s) for s in (11, 12, 13)]
    _, bench = _series(11)
    ds = build_dataset(scans, bench, _CFG)
    split = 150
    whole = evaluate_dataset(ds, _CFG)
    dev = evaluate_dataset(ds, _CFG, bar_max=split)
    oos = evaluate_dataset(ds, _CFG, bar_min=split)
    assert dev["queries"] + oos["queries"] == whole["queries"]
    assert dev["estimated_queries"] + oos["estimated_queries"] == whole["estimated_queries"]
    assert dev["samples"] == oos["samples"] == whole["samples"]  # 样本表始终全量，靠 available_bar 推进
    nd, no = dev["estimated_queries"], oos["estimated_queries"]
    if nd and no:
        blended = (dev["brier_score"] * nd + oos["brier_score"] * no) / (nd + no)
        assert blended == pytest.approx(whole["brier_score"], abs=0.001)
    assert dev["bar_max"] == split and oos["bar_min"] == split


def test_dataset_reuse_only_changes_replay_statistics():
    scans = [_scan(s) for s in (21, 22)]
    _, bench = _series(21)
    ds = build_dataset(scans, bench, _CFG)
    assert isinstance(ds, EstimateDataset)
    a = evaluate_dataset(ds, _CFG)
    b = evaluate_dataset(ds, _CFG, bar_min=0)
    assert a["samples"] == b["samples"] == len(ds.samples)
    cfg2 = EstimateConfig(**{**_CFG.__dict__, "prior_strength": 80.0, "entry_lift": 3.0})
    c = evaluate_dataset(ds, cfg2)
    assert c["samples"] == a["samples"]
    assert c["median_symbol_weight"] != a["median_symbol_weight"] or c["predicted_signals"] != a["predicted_signals"]


def test_all_grid_feature_sets_are_valid_and_rank_uses_skill():
    for name, spec in grid.FEATURE_SETS.items():
        assert parse_features(spec), name
    assert grid.FEATURE_SETS["base"] == BASE_FEATURES
    rows = [
        {"brier_skill": 0.01, "calibration_ece": 0.02},
        {"brier_skill": 0.30, "calibration_ece": 0.05},
        {"brier_skill": None, "calibration_ece": 0.001},
    ]
    assert sorted(rows, key=grid._rank)[0]["brier_skill"] == 0.30
    assert sorted(rows, key=grid._rank)[-1]["brier_skill"] is None
    tied = [
        {"brier_skill": 0.1, "calibration_ece": 0.01, "estimated_queries": 100},
        {"brier_skill": 0.1, "calibration_ece": 0.01, "estimated_queries": 900},
    ]
    assert sorted(tied, key=grid._rank)[0]["estimated_queries"] == 900  # 覆盖率优先
    slim = grid._slim({"brier_skill": 0.1, "secret": 1})
    assert "secret" not in slim and set(slim) <= set(grid._SLIM_KEYS)
    assert slim["brier_skill"] == 0.1


def test_dedupe_configs_keeps_distinct_and_neutral_lift():
    """同一配置的三个门槛只留一个代表，且代表取最接近默认门槛的那行。"""
    cfg0 = EstimateConfig(entry_lift=2.0)
    rows = [
        {"features": "z,lead", "horizon": 3, "prior_strength": 40.0,
         "entry_lift": 3.0, "brier_skill": 0.09, "hit_rate": 0.9},
        {"features": "z,lead", "horizon": 3, "prior_strength": 40.0,
         "entry_lift": 2.0, "brier_skill": 0.09, "hit_rate": 0.3},
        {"features": "z,lead", "horizon": 3, "prior_strength": 40.0,
         "entry_lift": 1.5, "brier_skill": 0.09, "hit_rate": 0.2},
        {"features": "z,vol", "horizon": 5, "prior_strength": 80.0,
         "entry_lift": 1.5, "brier_skill": 0.05, "hit_rate": 0.4},
    ]
    reps = grid._dedupe_configs(rows, cfg0, 3)
    assert len(reps) == 2  # 只有两种 (features, horizon, prior)
    assert reps[0]["features"] == "z,lead"
    assert reps[0]["entry_lift"] == 2.0  # 不挑命中率最高的 3.0，避免偷偷调参


def test_grid_stage1_only_on_synthetic_scans(monkeypatch):
    """不碰真实 K 线也能验证主流程：切分、只评估一次的样本外、结论字段。"""
    scans = [_scan(s) for s in (31, 32, 33)]
    _, bench = _series(31)
    n = len(bench)
    prep = {
        "dates": [f"d{i:04d}" for i in range(n)],
        "bench_close": bench, "scans": scans,
        "est_cfg": EstimateConfig(**{**_CFG.__dict__, "features": BASE_FEATURES}),
        "n": n, "start_bar": 65, "symbols_scanned": len(scans),
    }
    monkeypatch.setattr(grid, "prepare_scans", lambda **kwargs: prep)
    out = grid.run_grid(tag="unit", dev_frac=0.6, stage1_only=True)
    assert out["grid_size"] == len(grid.FEATURE_SETS)
    assert out["ranking_metric"] == "brier_skill"
    assert len(out["stage1_feature_sets"]) == len(grid.FEATURE_SETS)
    assert out["stage2_param_grid"] == []
    assert len(out["threshold_sensitivity"]) == len(grid.ENTRY_LIFTS)
    assert len({r["features"] for r in out["top_dev"]}) == len(out["top_dev"])  # 门槛去重生效
    assert 0 < len(out["oos_rows"]) <= grid.TOP_K + 1
    assert any(r["label"] == "default" for r in out["oos_rows"])
    assert out["split"]["split_bar"] > out["split"]["start_bar"]
    for r in out["oos_rows"]:
        assert r["oos"]["brier_skill"] is not None or r["oos"]["estimated_queries"] == 0
        assert "dev_to_oos_skill_delta" in r
    assert out["conclusion"] in {
        "no_information_gain_out_of_sample", "no_tradeable_edge",
        "no_incremental_value_over_default", "needs_walk_forward_validation",
    }
    assert "只验证一次" in out["note"]
