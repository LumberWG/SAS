# -*- coding: utf-8 -*-
"""观察池/持仓池分离模块单测：合成面板注入（无未来函数）+ 真实数据冒烟。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies.cross_sectional import _prepare_df
from app.strategies.t0_macd import _settle as _settle_flat
from app.strategies.twin_t import TwinTParams, simulate_twin_t
from app.strategies.twin_t_decoupled import (
    DecoupledParams,
    _observe_dev_matrix,
    _quantile_thresholds,
    _settle_realistic,
    build_sector_observe_codes,
    simulate_decoupled,
)


def _mk(code: str, closes):
    """构造 48 根（1 个完整交易日）5 分钟 K 线，open=high=low=close。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    morning = [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(24)]
    afternoon = [datetime(2025, 1, 2, 13, 5) + timedelta(minutes=5 * i) for i in range(24)]
    times = morning + afternoon
    if n > len(times):
        extra = n - len(times)
        d2m = [datetime(2025, 1, 3, 9, 35) + timedelta(minutes=5 * i)
               for i in range(min(extra, 24))]
        d2a = [datetime(2025, 1, 3, 13, 5) + timedelta(minutes=5 * i)
               for i in range(max(0, extra - 24))]
        times = times + d2m + d2a
    times = times[:n]
    df = pd.DataFrame({
        "date": times,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1.0, "amount": closes,
    })
    return df


def _raw_dev(specs, n=48, trigger=25):
    """specs: [(code, dev_at_trigger)]。dev 其余为 0；直接覆盖 dev 列以精确控制信号，
    close 恒为 10（成交价与信号解耦，便于纯逻辑单测）。"""
    raw = {}
    for code, dv in specs:
        df = _prepare_df(_mk(code, np.full(n, 10.0)), "2025-01-01", "2026-12-31")
        dev = np.zeros(n)
        dev[trigger] = dv
        df["dev"] = dev
        raw[code] = df
    return raw


def _raw_pattern(*patterns, n=48):
    """真实 VWAP 路径：dip/spike 放在下午中段（同一交易日内回归）。"""
    raw = {}
    for i, p in enumerate(patterns):
        cl = np.full(n, 10.0)
        if p == "dip":
            cl[25:35] = 9.0
        elif p == "spike":
            cl[25:35] = 11.0
        elif p == "rally":
            cl[-1] = 20.0
        code = f"T{i:03d}"
        raw[code] = _prepare_df(_mk(code, cl), "2025-01-01", "2026-12-31")
    return raw


# ---------------------------------------------------------------------------
# 分位阈值单元
# ---------------------------------------------------------------------------
def test_observe_dev_matrix_shape_and_values():
    obs = _raw_dev([("O0", -0.10), ("O1", 0.0), ("O2", 0.05)])
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    devmat, codes = _observe_dev_matrix(obs, days, 48)
    assert devmat.shape == (3, 1, 48)
    assert codes == ["O0", "O1", "O2"]
    assert abs(float(devmat[0, 0, 25]) - (-0.10)) < 1e-6
    assert abs(float(devmat[2, 0, 25]) - 0.05) < 1e-6
    assert abs(float(devmat[1, 0, 0])) < 1e-6


def test_quantile_thresholds_direction_and_monotonicity():
    devmat = np.array([[[-0.10]], [[-0.001]], [[0.001]], [[0.10]]], dtype=np.float32)
    lo01, hi01 = _quantile_thresholds(devmat, 0.01)
    lo25, hi25 = _quantile_thresholds(devmat, 0.25)
    assert lo01[0, 0] < lo25[0, 0] < hi25[0, 0] < hi01[0, 0]
    assert lo25[0, 0] < 0 < hi25[0, 0]


# ---------------------------------------------------------------------------
# 核心分离性：观察池分布决定持仓股是否够「极端」
# ---------------------------------------------------------------------------
def test_quantile_gate_blocks_mild_holding_when_observe_pool_is_extreme():
    # 观察池下 25% 分位 ≈ -0.0258；持仓股 dev=-0.02 不够极端 → quantile 不触发，
    # 但 hold_only（只看自己）会触发。这正是「观察池信息改变选股」的证据。
    obs = _raw_dev([("O0", -0.10), ("O1", -0.001), ("O2", 0.001), ("O3", 0.10)])
    hold = _raw_dev([("H0", -0.02)])
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})

    q = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                           params=DecoupledParams(select_mode="quantile",
                                                  quantile=0.25, n_select=1))
    b = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                           params=DecoupledParams(select_mode="hold_only",
                                                  n_select=1))
    assert len(q["trades"]) == 0
    assert len(b["trades"]) > 0
    assert q["observe_size"] == 4
    # 直接核验触发截面的门槛（平均门槛会被 47 个全平截面稀释，故不用于精确断言）
    devmat, _ = _observe_dev_matrix(obs, days, 48)
    lo, _ = _quantile_thresholds(devmat, 0.25)
    assert lo[0, 25] < -0.02             # 门槛比持仓股 -0.02 更负
    assert q["mean_thr_low"] < 0


def test_quantile_gate_fires_when_holding_is_extreme_enough():
    obs = _raw_dev([("O0", -0.10), ("O1", -0.001), ("O2", 0.001), ("O3", 0.10)])
    hold = _raw_dev([("H0", -0.08)])       # 越过 -0.0258 门槛
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    r = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                           params=DecoupledParams(select_mode="quantile",
                                                  quantile=0.25, n_select=1))
    assert len(r["trades"]) > 0
    assert r["trades"][0].side == "long"


def test_intersect_requires_being_absolute_topn_of_observe_pool():
    obs = _raw_dev([("O0", -0.10), ("O1", -0.001), ("O2", 0.001), ("O3", 0.10)])
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    # 持仓股不在观察池 Top5 → 无交集
    h_out = _raw_dev([("H0", -0.08)])
    r0 = simulate_decoupled(raw_hold=h_out, raw_observe=obs, days=days,
                            params=DecoupledParams(select_mode="intersect",
                                                   n_select=5))
    assert len(r0["trades"]) == 0
    # 持仓股恰是观察池里最极端的 O0 → 触发
    h_in = {c: obs[c] for c in ["O0"]}
    r1 = simulate_decoupled(raw_hold=h_in, raw_observe=obs, days=days,
                            params=DecoupledParams(select_mode="intersect",
                                                   n_select=5))
    assert len(r1["trades"]) > 0
    assert r1["trades"][0].code == "O0"


# ---------------------------------------------------------------------------
# 与既有模块的一致性 / 量纲 / 纪律
# ---------------------------------------------------------------------------
def test_hold_only_equivalent_to_twin_t():
    raw = _raw_pattern("dip", "spike", "flat", "flat")
    days = sorted({d for df in raw.values() for d in df["date"].dt.normalize().unique()})
    a = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", n_select=3,
                                                    cost_per_round_pct=0.0015))
    b = simulate_decoupled(raw_hold=raw, raw_observe=raw, days=days,
                           params=DecoupledParams(mode="both", select_mode="hold_only",
                                                  n_select=3,
                                                  cost_per_round_pct=0.0015))
    assert a["aggregate"]["rounds"] == b["aggregate"]["rounds"]
    assert abs(a["t_pnl_pct"] - b["t_pnl_pct"]) < 1e-12
    assert abs(a["hold_return"] - b["hold_return"]) < 1e-12


def test_hold_return_ignores_observe_only_names():
    # 观察池里有一只翻倍股（rally），但它不在持仓池 → 持有基线不应被抬高
    obs = _raw_pattern("flat", "flat", "flat", "rally")
    hold = {c: obs[c] for c in list(obs)[:2]}
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    r = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                           params=DecoupledParams(select_mode="quantile", n_select=1))
    assert abs(r["hold_return"]) < 1e-9
    assert r["scanned"] == 2
    assert r["observe_size"] == 4
    assert abs(r["combined"] - (r["hold_return"] + r["t_pnl_pct"])) < 1e-12


def test_trades_are_intraday_only():
    obs = _raw_pattern("dip", "spike", "flat", "flat", "flat", "rally")
    hold = {c: obs[c] for c in list(obs)[:4]}
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    for sm in ("hold_only", "quantile", "intersect"):
        r = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                               params=DecoupledParams(select_mode=sm, n_select=2))
        for t in r["trades"]:
            assert t.bars_held < 48          # 单日 round-trip，隔夜无敞口
            assert t.day == "2025-01-02"
        assert np.isfinite(r["t_pnl_pct"])
        assert r["beats_hold"] == (r["t_pnl_pct"] > 0)


def test_dev_cleaning_removes_dirty_bars_from_percentiles():
    """脏 dev（+2850%）会拉歪上分位；清洗后门槛回落，且报告清洗比例。"""
    obs = _raw_dev([("O0", -0.10), ("O1", -0.001), ("O2", 0.001), ("O3", 0.10)])
    dv = obs["O3"]["dev"].to_numpy().copy()
    dv[25] = 28.50                    # 模拟 VWAP=1 导致的 +2850% 脏值
    obs["O3"]["dev"] = dv
    hold = _raw_dev([("H0", -0.02)])
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    clean = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                               params=DecoupledParams(select_mode="quantile",
                                                      quantile=0.25, n_select=1,
                                                      clean_dev=True))
    dirty = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                               params=DecoupledParams(select_mode="quantile",
                                                      quantile=0.25, n_select=1,
                                                      clean_dev=False))
    assert clean["invalid_dev_frac"] > 0
    assert dirty["invalid_dev_frac"] == 0.0
    assert dirty["mean_thr_high"] > clean["mean_thr_high"] * 10
    assert clean["mean_thr_high"] < 0.01     # 清洗后上分位回到正常量级


def test_raw_injection_is_deterministic():
    obs = _raw_pattern("dip", "spike", "flat", "flat")
    hold = {c: obs[c] for c in list(obs)[:3]}
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    p = DecoupledParams(select_mode="quantile", n_select=1)
    a = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days, params=p)
    b = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days, params=p)
    assert a["aggregate"]["rounds"] == b["aggregate"]["rounds"]
    assert abs(a["t_pnl_pct"] - b["t_pnl_pct"]) < 1e-12


@pytest.mark.parametrize("sm", ["hold_only", "quantile", "intersect"])
def test_decoupled_real_smoke(sm):
    codes = ["000001", "600000", "000002", "600519", "000063", "600036"]
    r = simulate_decoupled(hold_codes=codes[:4], observe_codes=codes,
                           start="2025-01-02", end="2026-11-09",
                           params=DecoupledParams(select_mode=sm, n_select=2))
    if r["scanned"] == 0:
        pytest.skip("样本标的本地数据缺失，跳过")
    assert np.isfinite(r["t_pnl_pct"])
    assert np.isfinite(r["hold_return"])
    assert r["observe_size"] >= r["scanned"]


# ---------------------------------------------------------------------------
# #154 同行业观察池 + #155 真实成本
# ---------------------------------------------------------------------------
def test_build_sector_observe_codes_covers_holdings_and_peers():
    ind_map = {"A": "银行", "B": "银行", "C": "银行", "D": "医药", "E": "医药", "F": "科技"}
    all_codes = ["A", "B", "C", "D", "E", "F", "G"]
    obs = build_sector_observe_codes(["A", "B"], ind_map, all_codes, cap=10)
    # 持仓自身 + 同行业 C 都在；D/E 属另一行业不应被纳入
    assert "A" in obs and "B" in obs and "C" in obs
    assert "D" not in obs and "E" not in obs and "G" not in obs


def test_build_sector_observe_codes_respects_cap():
    ind_map = {f"C{i:03d}": "X" for i in range(200)}
    all_codes = [f"C{i:03d}" for i in range(200)] + [f"O{i:03d}" for i in range(50)]
    obs = build_sector_observe_codes(["C000"], ind_map, all_codes, cap=50)
    assert len(obs) <= 50
    assert "C000" in obs


def test_realistic_cost_exceeds_flat_when_min_commission_binds():
    p = DecoupledParams(realistic_cost=True, total_capital=30_000.0, t_ratio=1.0)
    # 小额名义（每笔 3000 元）：最低佣金 5 元/腿 = 10 元 ≈ 0.33%，远高于 flat 0.15%
    qty = 300.0  # notional = 300*10 = 3000
    _, flat = _settle_flat(1, 10.0, 10.2, qty,
                           DecoupledParams(cost_per_round_pct=0.0015))
    _, real = _settle_realistic(1, 10.0, 10.2, qty, p)
    # real 的净百分比应明显低于 flat（成本更高）
    assert real < flat - 0.1


def test_realistic_cost_settlement_matches_notional():
    p = DecoupledParams(realistic_cost=True, min_commission=0.0, commission_rate=0.0,
                        stamp_rate=0.0, slippage_bps=0.0)
    # 零附加成本时，realistic 应等于纯价差百分比（正T）
    qty = 100.0  # notional = 1000
    pnl, pct = _settle_realistic(1, 10.0, 11.0, qty, p)
    assert abs(pnl - 100.0) < 1e-6          # (11-10)*100
    assert abs(pct - 10.0) < 1e-6           # 10% 价差


def test_simulate_decoupled_realistic_cost_runs():
    obs = _raw_pattern("dip", "spike", "flat", "flat")
    hold = {c: obs[c] for c in list(obs)[:3]}
    days = sorted({d for df in obs.values() for d in df["date"].dt.normalize().unique()})
    r = simulate_decoupled(raw_hold=hold, raw_observe=obs, days=days,
                           params=DecoupledParams(select_mode="quantile", n_select=1,
                                                  realistic_cost=True))
    assert np.isfinite(r["t_pnl_pct"])
    assert len(r["trades"]) >= 0
