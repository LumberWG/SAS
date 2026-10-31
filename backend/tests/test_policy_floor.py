"""policy_floor 的纯函数单测：托底强度检测 + 熔断滞回判定。

不依赖数据库/引擎，纯 numpy/pandas 构造序列，保证回归稳定。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.policy_floor import detect_policy_floor, circuit_breaker_state


def _falling(n: int = 130, seed: int = 1) -> pd.Series:
    """持续下行、不断新低、波动恒定 → 托底证据最弱。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(-0.0015, 0.020, n)
    return pd.Series(100.0 * np.cumprod(1.0 + ret))


def _bottoming(n_total: int = 150, seed: int = 2) -> pd.Series:
    """前段下行（高波动），后段企稳（低波动、低点抬升、偏多）→ 托底证据强。"""
    rng = np.random.default_rng(seed)
    ret1 = rng.normal(-0.0020, 0.025, 120)
    ret2 = rng.normal(0.0005, 0.008, n_total - 120)
    return pd.Series(100.0 * np.cumprod(1.0 + np.concatenate([ret1, ret2])))


def test_none_when_no_drawdown():
    # 近单调上行（几乎无波动）→ 最大回撤<阈值 → 直接 none，score=0
    up = pd.Series(100.0 + np.arange(150) * 0.1)
    out = detect_policy_floor(up)
    assert out["score"] == 0.0
    assert out["level"] == "none"
    assert "未处于下行段" in out["reason"]


def test_bounds_and_components():
    out = detect_policy_floor(_bottoming())
    assert 0.0 <= out["score"] <= 1.0
    assert out["level"] in ("none", "weak", "strong")
    assert set(out["components"].keys()) >= {
        "vol_compression", "stabilization", "reversal_days",
    }
    # 没有传入直接信号 → available=False，诚实标注
    assert out["available"] is False


def test_bottoming_scores_higher_than_falling():
    s_fall = detect_policy_floor(_falling())
    s_bot = detect_policy_floor(_bottoming())
    assert s_bot["score"] > s_fall["score"]


def test_etf_direct_signal_sets_available_and_lifts_score():
    base = _bottoming()
    out_no = detect_policy_floor(base)
    # 构造 ETF 放量：前段平稳、末 10 根大幅放量（倍数足够大，避免被基线窗口稀释）
    rng = np.random.default_rng(3)
    etf = rng.normal(100, 5, len(base))
    etf[-10:] = etf[-10:] * 10.0
    out_yes = detect_policy_floor(base, etf_volume=etf)
    assert out_yes["available"] is True
    assert "etf_volume_spike" in out_yes["components"]
    assert out_yes["score"] >= out_no["score"] - 1e-9


def test_short_series_safe():
    out = detect_policy_floor(pd.Series([100.0, 99.0, 98.0]))
    assert out["score"] == 0.0
    assert out["level"] == "none"
    assert out["reason"]  # 有原因，不是空


def test_circuit_breaker_hysteresis():
    # 未触发 → 需 ≥trigger 才触发
    assert circuit_breaker_state(0.05, False, trigger=0.08, release=0.04) is False
    assert circuit_breaker_state(0.08, False, trigger=0.08, release=0.04) is True
    # 已触发 → 回撤须 ≤release 才解除（滞回）
    assert circuit_breaker_state(0.10, True, trigger=0.08, release=0.04) is True
    assert circuit_breaker_state(0.05, True, trigger=0.08, release=0.04) is True
    assert circuit_breaker_state(0.04, True, trigger=0.08, release=0.04) is False
    assert circuit_breaker_state(0.03, True, trigger=0.08, release=0.04) is False
