# -*- coding: utf-8 -*-
"""严格零前视 ML 回归：标签 embargo、CV purge、seed、双端 parity、缓存键与门控诊断。"""
from __future__ import annotations

from dataclasses import replace

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest

from app.backtest import ml_scorer as ms
from app.backtest.ic_gate import ICGate
from app.backtest.ml_scorer import build_live_ml_predictions, build_ml_predictions
from app.backtest.pipeline import PipelineParams, _ml_effective, _prepared_key, _signals_key


def _synth_df(n_days: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days).strftime("%Y-%m-%d").tolist()
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n_days)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.004, n_days)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.004, n_days)))
    volume = rng.integers(1_000_000, 5_000_000, n_days).astype(float)
    return pd.DataFrame({
        "date": dates,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close,
        "turnover_rate": rng.uniform(1.0, 5.0, n_days),
    })


def _synth_klines(n_codes: int = 8, n_days: int = 180, seed: int = 7) -> dict[str, pd.DataFrame]:
    return {f"{600000 + c:06d}": _synth_df(n_days, seed + c) for c in range(n_codes)}


def test_prepare_samples_honors_label_before_and_need_label_false() -> None:
    df = _synth_df(220, 11)
    dates = df["date"].tolist()
    pos = {d: i for i, d in enumerate(dates)}
    horizon = 2
    boundary = dates[190]
    loose_x, loose_y, loose_g, _ = ms._prepare_samples({"A": df}, set(dates), horizon)
    strict_x, strict_y, strict_g, _ = ms._prepare_samples(
        {"A": df}, set(dates), horizon, label_before=boundary,
    )
    assert len(loose_x) > len(strict_x) > 0
    assert np.isfinite(strict_y).all()
    assert all(pos[str(g)] + horizon < 190 for g in strict_g)
    assert any(pos[str(g)] + horizon >= 190 for g in loose_g)

    last = {dates[-1]}
    no_label_x, _, _, _ = ms._prepare_samples({"A": df}, last, horizon, need_label=False)
    with_label_x, _, _, _ = ms._prepare_samples({"A": df}, last, horizon)
    assert len(no_label_x) == 1
    assert len(with_label_x) == 0


def test_purged_group_cv_uses_horizon_purge_gap(monkeypatch) -> None:
    x = np.zeros((1000, 3), dtype=float)
    y = np.linspace(-0.01, 0.01, 1000)
    groups = np.array([f"d{i // 10:03d}" for i in range(1000)])
    captured: list[int] = []

    def fake_ridge_fit(X, _y, _alpha):
        captured.append(int(X.shape[0]))
        return np.zeros(X.shape[1])

    monkeypatch.setattr(ms, "_ridge_fit", fake_ridge_fit)
    ms.purged_group_cv(
        x, y, groups, alphas=(1.0,), model="ridge", n_splits=5,
        horizon=15, strict_lookahead=True,
    )
    assert captured[0] == 50

    captured.clear()
    ms.purged_group_cv(
        x, y, groups, alphas=(1.0,), model="ridge", n_splits=5,
        horizon=15, strict_lookahead=False,
    )
    assert captured[0] == 200


def test_lgb_seed_is_configurable(monkeypatch) -> None:
    captured: dict = {}

    class _FakeModel:
        def predict(self, X):
            return np.zeros(len(X))

    def fake_train(params, dtrain, *, num_boost_round=0):
        captured.update(params)
        captured["num_boost_round"] = num_boost_round
        return _FakeModel()

    monkeypatch.setattr(lgb, "train", fake_train)
    ms._lgb_fit(np.zeros((10, 3)), np.linspace(-1, 1, 10), seed=777)
    assert captured["seed"] == 777
    assert captured["feature_fraction_seed"] == 777
    assert captured["bagging_seed"] == 777
    assert captured["data_random_seed"] == 777


def test_live_and_backtest_ml_parity_on_synthetic_data() -> None:
    klines = _synth_klines()
    all_dates = sorted({d for df in klines.values() for d in df["date"]})
    as_of = max(all_dates)
    kw = dict(
        horizon=5,
        alphas=(0.1, 1.0),
        n_splits=3,
        require_ic=-1.0,
        model="ridge",
        industry_neutral=False,
        seed=42,
        strict_lookahead=True,
    )
    back_pred, back_ic = build_ml_predictions(klines, all_dates, as_of, **kw)
    live_pred, live_ic = build_live_ml_predictions(klines, as_of=as_of, **kw)
    assert back_pred and live_pred
    assert back_ic == pytest.approx(live_ic, abs=1e-6)
    for code in klines:
        assert back_pred[(code, as_of)] == pytest.approx(live_pred[(code, as_of)], abs=1e-6)


def test_pipeline_cache_keys_include_seed_and_gate() -> None:
    base = PipelineParams(start_date="2026-01-01", end_date="2026-01-31")
    seed2 = replace(base, ml_seed=777)
    gate = replace(base, ml_ic_gate=True, ml_ic_gate_thresh=0.2)
    strict_off = replace(base, ml_strict_lookahead=False)
    assert _prepared_key(base) != _prepared_key(seed2)
    assert _prepared_key(base) != _prepared_key(gate)
    assert _prepared_key(base) != _prepared_key(strict_off)
    assert _signals_key(base) != _signals_key(seed2)
    assert _signals_key(base) != _signals_key(gate)


def test_ml_effective_reflects_gate_fallback() -> None:
    base = PipelineParams(start_date="2026-01-01", end_date="2026-01-31")
    prepared = {"ml_pred": {("600000", "2026-01-05"): 80.0}}
    rolling = replace(base, scoring_mode="factors", ml_ic_gate=True)
    ICGate.reset_telemetry()
    assert not _ml_effective(rolling, prepared)
    ICGate.TELEMETRY["use_days"] = 1
    assert _ml_effective(rolling, prepared)

    segment = replace(base, scoring_mode="factors", ml_ic_gate_segment=True, ml_ic_gate_thresh=0.12)
    assert not _ml_effective(segment, {"ml_pred": prepared["ml_pred"], "ml_oos_ic": 0.05})
    assert _ml_effective(segment, {"ml_pred": prepared["ml_pred"], "ml_oos_ic": 0.20})
