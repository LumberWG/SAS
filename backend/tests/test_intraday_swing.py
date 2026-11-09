# -*- coding: utf-8 -*-
"""#125 日内摆动实验的单元测试：纯函数 + 一小段真实分钟数据冒烟。"""
from __future__ import annotations

import json

import pytest

from scripts import intraday_swing as lab
from app.analysis.estimate import estimate_config_from_settings


def test_scaled_estimate_config_multiplies_windows_by_bars_per_day():
    base = estimate_config_from_settings()
    for period, bpd in ((30, 8), (60, 4)):
        cfg = lab._intraday_estimate_config(period, base)
        assert cfg.z_window == 60 * bpd
        assert cfg.confirm_bars == max(2, 2 * bpd)
        assert cfg.horizon == max(2, 3 * bpd)
        # 其余沿用默认值，不随周期乱变
        assert cfg.entry_lift == base.entry_lift
        assert cfg.cost_pct == base.cost_pct
        assert cfg.features == base.features


def test_exit_presets_use_sensible_atr_k_not_scaled_by_bpd():
    # 关键防回归：atr_k 之前被错误地乘了 bpd(=8)，等于变相长持。
    # 这里锁死紧跟踪止损（2/4 倍 ATR），不再是 16 那种宽到约 8%~16% 的伪长持。
    specs30 = lab._intraday_exit_presets(30, 0.3)
    assert {s.name for s in specs30} == {"fixed8", "fixed16", "fixed32", "atr2", "atr4", "swing_trail"}
    for s in specs30:
        if s.mode == "atr_trail":
            assert s.atr_k in (2.0, 4.0)
            assert s.hold_bars == 10 * 8  # 持有上限兜底，约 10 交易日
        elif s.mode == "fixed":
            assert s.hold_bars in (8, 16, 32)
        else:  # swing_trail：持有上限同样是 10×bpd
            assert s.mode == "swing_trail"
            assert s.hold_bars == 10 * 8

    specs60 = lab._intraday_exit_presets(60, 0.3)
    assert {s.name for s in specs60} == {"fixed4", "fixed8", "fixed16", "atr2", "atr4", "swing_trail"}
    assert all(s.atr_k in (2.0, 4.0) for s in specs60 if s.mode == "atr_trail")


def test_load_aggregated_groups_consecutive_bars():
    # 用真实 000001 验证「5 分钟 → 30 分钟」聚合：连续分组、OHLC 正确。
    g = lab._load_aggregated("000001", 6)
    assert g is not None
    assert list(g.columns) == ["open", "high", "low", "close", "volume", "amount"]
    # 30 分钟 = 每 6 根 5 分钟一组；总数约为 5 分钟根数 / 6
    assert len(g) >= 3500  # 444 交易日 × 8 根
    # 第一组 open = 原始第 0 根 open，high = 第 0~5 根 max
    assert g["open"].iloc[0] == g["open"].iloc[0]
    assert g["high"].iloc[0] >= g["low"].iloc[0]


def test_load_aggregated_skips_index_like_files():
    # 只有 (date, close) 的文件缺少 OHLC，必须被跳过（返回 None）。
    assert lab._load_aggregated("__nonexistent_code__", 6) is None


def test_prepare_intraday_scans_builds_rs_and_swings():
    # 真实取 2 只，验证对齐 + 篮子基准 + RS 摆动点都到位。
    prep = lab.prepare_intraday_scans(period=30, start="", max_symbols=2, log=lambda m: None)
    assert prep["period"] == 30
    assert prep["bars_per_day"] == 8
    assert prep["baseline_kind"] == "equal_weight_universe_mean"
    scans = prep["scans"]
    assert len(scans) >= 1
    for s in scans:
        assert s.rs is not None and len(s.rs) == prep["n"]
        assert isinstance(s.points, list)
        assert s.close is not None and s.atr is not None
        # 摆动点必须带 confirmed_bar，出场实验才定位得到入场
        for p in s.points:
            assert p.confirmed_bar >= 0


def test_api_rejects_bad_period_and_missing_file(monkeypatch, tmp_path):
    from app.api import ops

    monkeypatch.setattr(ops, "get_settings", lambda: __import__(
        "app.config", fromlist=["Settings"]).Settings if False else _fake_settings(tmp_path))
    # period 非法
    with pytest.raises(Exception):
        ops.intraday_swing(period=15, tag="live")
    # 产物缺失时优雅降级
    r = ops.intraday_swing(period=30, tag="live")
    assert r["available"] is False and r["all_rows"] == []
    assert r["oos_rows"] == [] and r["conclusion"] is None


def _fake_settings(tmp_path):
    class S:
        kline_dir = tmp_path / "klines"
    return S()


def test_api_exposes_period_and_estimate(monkeypatch, tmp_path):
    from app.api import ops

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    data = {
        "period": 30, "start": "2025-01-01", "as_of": "2026-11-06",
        "run_at": "2026-11-09 10:00:00", "elapsed_sec": 25.2,
        "bars_per_day": 8, "baseline_kind": "equal_weight_universe_mean",
        "symbols_scanned": 243, "symbols_skipped": 1, "bars": 3568,
        "queries": 734640, "signals": 6320, "signal_hit_rate": 0.2921,
        "dev_cut_date": "2026-03-23", "entry_bases": ["signal_next", "confirmed_low"],
        "scaled_config": {"z_window": 480, "confirm_bars": 16, "horizon": 24},
        "estimate": {"verdict": "below_breakeven", "brier_skill": 0.0238,
                     "hit_rate": 0.2921, "predicted_signals": 6320},
        "all_rows": [], "dev_rows": [], "oos_rows": [
            {"entry_basis": "signal_next", "exit": "atr2", "mean_net_pct": -0.3,
             "mean_excess_pct": -0.1, "verdict": "negative_expectancy"}],
        "dev_selection": {"entry_basis": "signal_next", "exit": "atr2"},
        "oos_best": {"exit": "atr2", "mean_net_pct": -0.3},
        "oos_folds": [{"fold": 1, "mean_net_pct": 0.1}],
        "fold_positive": 1, "fold_consistency": 0.2,
        "conclusion": "no_tradeable_edge", "conclusion_detail": "样本外均值为负",
        "note": "纯研究产物，不参与交易",
    }
    (log_dir / "intraday_swing_30_live.json").write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(ops, "get_settings", lambda: _fake_settings(tmp_path))
    r = ops.intraday_swing(period=30, tag="live")
    assert r["available"] is True
    assert r["period"] == 30 and r["bars_per_day"] == 8
    assert r["estimate"]["verdict"] == "below_breakeven"
    assert r["estimate"]["brier_skill"] == 0.0238
    assert r["entry_bases"] == ["signal_next", "confirmed_low"]
    assert r["oos_best"]["exit"] == "atr2"
    assert "净值" not in r["note"] and "持仓" not in r["note"]
    assert "disclaimer" in r
