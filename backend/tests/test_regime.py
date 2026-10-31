from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.regime import detect_regime


def _series(values) -> pd.Series:
    return pd.Series(values, dtype="float64")


def test_calm_no_drawdown_low_vol():
    rng = np.random.default_rng(0)
    base = np.linspace(100, 110, 200)
    noise = rng.normal(0, 0.001, 200)
    out = detect_regime(_series(base + noise), cap_calm=0.9)
    assert out["state"] == "calm"
    assert out["exposure_cap"] == pytest.approx(0.9)
    assert out["pause_all"] is False
    assert out["reason"] == ""


def test_crisis_big_drawdown_triggers_pause():
    up = np.linspace(100, 130, 80)
    down = np.linspace(130, 95, 80)  # 从 130 跌到 95，回撤约 27%
    out = detect_regime(_series(np.concatenate([up, down])))
    assert out["state"] == "crisis"
    assert out["pause_all"] is True
    assert out["exposure_cap"] == pytest.approx(0.15)


def test_weak_trend_low_vol_small_dd():
    rng = np.random.default_rng(1)
    base = np.linspace(100, 98, 150)
    noise = rng.normal(0, 0.002, 150)
    out = detect_regime(_series(base + noise))
    assert out["state"] == "weak"
    assert out["exposure_cap"] == pytest.approx(0.30)
    assert out["pause_all"] is False


def test_shock_high_vol():
    rng = np.random.default_rng(2)
    n = 130
    low = rng.normal(0, 0.004, n - 20)
    high = rng.normal(0, 0.02, 20)
    rets = np.concatenate([low, high])
    price = 100 * np.cumprod(1 + rets)
    out = detect_regime(_series(price))
    assert out["state"] in ("shock", "crisis")
    assert out["exposure_cap"] <= 0.40
    assert out["pause_all"] == (out["state"] == "crisis")


def test_insufficient_data_is_safe():
    out = detect_regime(_series([100, 101, 99]), cap_calm=0.9)
    assert out["state"] == "calm"
    assert out["exposure_cap"] == pytest.approx(0.9)
    assert out["reason"]  # 退化分支，而非崩溃


def test_failure_path_returns_safe_dict():
    out = detect_regime(pd.Series([], dtype="float64"), cap_calm=0.9)
    assert out["state"] == "calm"
    assert out["exposure_cap"] == pytest.approx(0.9)
