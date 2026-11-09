# -*- coding: utf-8 -*-
"""#124 出场规则实验：锁住跟踪止盈的因果性与「可交易口径」的统计语义。

最要紧的三条：
- 跟踪峰值只累加**入场之后**的收盘，入场前的历史高点不许抬高止盈锚；
- 回撤宽度用入场时可见的 ATR 冻结，不许用出场时的波动率反推（那是用未来调止损）；
- ``swing_trail`` 只认已确认的前高，未确认的结构位当时看不见。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.analysis.exit import (
    EXIT_PRESETS,
    ExitResult,
    ExitSpec,
    exit_config_from_settings,
    simulate_exit,
)
from app.analysis.swing import SwingPoint

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("exit_lab_under_test", _SCRIPTS / "exit_lab.py")
lab = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(lab)

# 入场 10 → 冲高 10.8 → 回落 10.0；固定持有 3 根正好卖在最高点附近
_CLOSE = [10.0, 10.2, 10.5, 10.8, 10.6, 10.3, 10.0, 9.8]
_ATR = [0.2] * len(_CLOSE)


def test_fixed_exit_sells_after_hold_bars():
    spec = ExitSpec("f3", "fixed", hold_bars=3, cost_pct=0.3)
    res = simulate_exit(_CLOSE, 0, spec)
    assert res is not None
    assert res.exit_bar == 3
    assert res.exit_pct == pytest.approx(8.0, abs=1e-9)
    assert res.net_pct == pytest.approx(7.7, abs=1e-9)
    assert res.holding_bars == 3
    assert res.reason == "time_cap"
    # MFE/MAE 覆盖入场到出场整段（含入场点）
    assert res.mfe_pct == pytest.approx(8.0, abs=1e-9)
    assert res.mae_pct == pytest.approx(0.0, abs=1e-9)


def test_fixed_exit_clamps_at_last_bar():
    spec = ExitSpec("f10", "fixed", hold_bars=10)
    res = simulate_exit(_CLOSE, 5, spec)
    assert res.exit_bar == len(_CLOSE) - 1
    assert res.reason == "no_exit"


def test_atr_trail_exits_on_drawdown_from_peak():
    # width = max(1.0%, 1.5 * 0.2 / 10 = 3.0%) → 峰值 10.8 回撤 3% 即 10.476
    spec = ExitSpec("t", "atr_trail", hold_bars=20, atr_k=1.5, floor_pct=1.0, cost_pct=0.3)
    res = simulate_exit(_CLOSE, 0, spec, atr=_ATR)
    assert res is not None
    assert res.exit_bar == 5          # 10.3 相对 10.8 回撤 4.63% > 3%
    assert res.exit_pct == pytest.approx(3.0, abs=1e-9)
    assert res.reason == "trail"
    assert res.mfe_pct == pytest.approx(8.0, abs=1e-9)


def test_trail_width_is_frozen_at_entry_atr():
    """入场后 ATR 爆炸也不许放宽宽度：否则等于用未来波动率调止损。"""
    wide_atr = [0.2, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    spec = ExitSpec("t", "atr_trail", hold_bars=20, atr_k=1.5, floor_pct=1.0)
    res = simulate_exit(_CLOSE, 0, spec, atr=wide_atr)
    assert res.exit_bar == 5          # 与 ATR 恒为 0.2 时同一个位置
    res_flat = simulate_exit(_CLOSE, 0, spec, atr=_ATR)
    assert res.exit_bar == res_flat.exit_bar


def test_trail_ignores_pre_entry_history():
    """入场前的高点不能抬高跟踪锚：峰值从入场价起算。"""
    close = [100.0, 100.0, 10.0, 10.1, 10.05, 10.0]
    spec = ExitSpec("t", "atr_trail", hold_bars=20, atr_k=1.5, floor_pct=1.0)
    res = simulate_exit(close, 2, spec, atr=[0.2] * len(close))
    assert res is not None
    assert res.mfe_pct == pytest.approx(1.0, abs=1e-9)   # 只算 10.0 → 10.1
    assert res.exit_bar == 5


def test_take_profit_caps_upside():
    close = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5]
    spec = ExitSpec("tp", "atr_trail", hold_bars=20, atr_k=1.0, floor_pct=1.0,
                    take_profit_pct=10.0)
    res = simulate_exit(close, 0, spec, atr=[0.2] * len(close))
    assert res is not None
    assert res.exit_bar == 2          # 11.0 首次触及 10.0 × 1.10
    assert res.exit_pct == pytest.approx(10.0, abs=1e-9)
    assert res.reason == "take_profit"


def test_hold_bars_caps_trailing_exit():
    close = [10.0 + 0.01 * i for i in range(30)]
    spec = ExitSpec("cap", "atr_trail", hold_bars=5, atr_k=3.0, floor_pct=5.0)
    res = simulate_exit(close, 0, spec, atr=[0.2] * len(close))
    assert res is not None
    assert res.exit_bar == 5
    assert res.reason == "time_cap"


def test_swing_trail_only_sees_confirmed_highs():
    """confirmed_bar=4 的前高在第 3 根还看不见，不能提前触发。"""
    highs = [SwingPoint("high", 2, 4, 10.9, 0.05)]
    spec = ExitSpec("sw", "swing_trail", hold_bars=20, floor_pct=0.0)
    res = simulate_exit(_CLOSE, 0, spec, price_highs=highs)
    assert res is not None
    assert res.exit_bar == 4          # 第 3 根收盘 10.8 < 10.9 但当时不可见
    assert res.exit_pct == pytest.approx(6.0, abs=1e-9)


def test_swing_trail_holds_above_prior_high():
    highs = [SwingPoint("high", 2, 3, 10.4, 0.05)]
    spec = ExitSpec("sw", "swing_trail", hold_bars=20, floor_pct=0.0)
    res = simulate_exit(_CLOSE, 0, spec, price_highs=highs)
    assert res is not None
    assert res.exit_bar == 5          # 10.3 首次跌破 10.4
    assert res.reason == "trail"


def test_excess_subtracts_benchmark_over_same_window():
    bench = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    spec = ExitSpec("f3", "fixed", hold_bars=3)
    res = simulate_exit(_CLOSE, 0, spec, bench_close=bench)
    assert res is not None
    assert res.excess_pct == pytest.approx(8.0 - 3.0, abs=1e-9)


def test_invalid_entry_returns_none():
    assert simulate_exit([0.0, 1.0, 2.0], 0, ExitSpec("f", "fixed")) is None
    assert simulate_exit([1.0, 2.0], 5, ExitSpec("f", "fixed")) is None


def test_spec_validation():
    with pytest.raises(ValueError):
        ExitSpec("x", "nope").validate()
    with pytest.raises(ValueError):
        ExitSpec("x", "fixed", hold_bars=0).validate()
    with pytest.raises(ValueError):
        ExitSpec("x", "atr_trail", atr_k=0.0).validate()
    with pytest.raises(ValueError):
        ExitSpec("x", "fixed", take_profit_pct=-1.0).validate()


def test_presets_are_valid_and_include_baseline():
    names = [s.name for s in EXIT_PRESETS]
    assert "fixed3" in names and "swing_trail" in names
    for spec in EXIT_PRESETS:
        spec.validate()
    # 基线必须与 #120~#123 的「3 根」口径完全一致
    base = next(s for s in EXIT_PRESETS if s.name == "fixed3")
    assert base.mode == "fixed" and base.hold_bars == 3


def test_exit_config_from_settings_defaults_to_fixed():
    spec = exit_config_from_settings()
    assert spec.mode == "fixed"
    assert spec.hold_bars >= 1


def _res(exit_pct: float, excess: float) -> ExitResult:
    return ExitResult(
        exit_bar=1, exit_pct=exit_pct, excess_pct=excess, net_pct=exit_pct - 0.3,
        holding_bars=1, mfe_pct=exit_pct, mae_pct=0.0, reason="time_cap",
    )


def test_stats_and_verdict_semantics():
    # _verdict 对 n<30 一律判样本不足，这里用 15 正 15 负凑够门槛
    st = lab._stats([_res(5.0, 4.0)] * 15 + [_res(-2.0, -1.0)] * 15, 0.3)
    assert st["n"] == 30
    assert st["mean_net_pct"] == pytest.approx((4.7 - 2.3) / 2, abs=1e-9)
    assert st["win_rate"] == 0.5
    assert lab._verdict(st, 0.3) == "positive_expectancy"

    # 绝对赚、超额亏 → 只有 beta，不能算这套方法的价值
    beta = lab._stats([_res(5.0, -1.0)] * 15 + [_res(3.0, -0.5)] * 15, 0.3)
    assert lab._verdict(beta, 0.3) == "beta_only"

    assert lab._verdict(lab._stats([], 0.3), 0.3) == "insufficient_samples"
    assert lab._verdict(lab._stats([_res(-1.0, -1.0)] * 30, 0.3), 0.3) == "negative_expectancy"


def test_entry_bar_never_uses_signal_day():
    scan = SimpleNamespace(last_valid_bar=50)
    q = SimpleNamespace(bar=7, entry_bar=9)
    assert lab._entry_bar(q, "signal_next", scan, 100) == 8
    assert lab._entry_bar(q, "confirmed_low", scan, 100) == 9
    assert lab._entry_bar(SimpleNamespace(bar=7, entry_bar=-1), "confirmed_low", scan, 100) is None
    assert lab._entry_bar(q, "signal_next", scan, 7) is None   # 次日越过数据末尾


def test_entry_bar_rejects_post_delisting_fill():
    """退市后价格被 ffill 成常数，那段必须判为不可交易，不能算成 0 收益。"""
    scan = SimpleNamespace(last_valid_bar=10)
    q = SimpleNamespace(bar=10, entry_bar=12)
    assert lab._entry_bar(q, "signal_next", scan, 100) is None
    assert lab._entry_bar(SimpleNamespace(bar=9, entry_bar=11), "signal_next", scan, 100) == 10


def test_split_preserves_all_queries():
    qs = [SimpleNamespace(bar=i, code="000001") for i in range(100)]
    cut, dev, oos = lab._split(qs, 0.6)
    assert len(dev) + len(oos) == len(qs)
    assert max(q.bar for q in dev) <= min(q.bar for q in oos)
    assert cut == min(q.bar for q in oos)


def test_folds_cover_every_query():
    qs = [SimpleNamespace(bar=i, code="000001") for i in range(53)]
    parts = lab._folds(qs, 5)
    assert sum(len(p) for p in parts) == len(qs)
    assert all(parts)
