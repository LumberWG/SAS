"""入场护栏（影子层）测试：市场状态滤网 + 日内追高护栏。

不依赖网络 / 外部服务，只读本地 parquet 与 signals 表。
"""
from __future__ import annotations

from app.analysis import entry_guard as eg


def test_thresholds_are_sensible():
    assert eg.GAP_THRESHOLD == 0.03
    assert eg.SPIKE_THRESHOLD == 0.05


def test_market_regime_returns_valid_state():
    r = eg.market_regime()
    assert r["state"] in ("uptrend", "choppy", "declining", "unknown")
    # advice 与 state 一致
    if r["state"] == "declining":
        assert r["new_entry_advice"] == "avoid"
    else:
        assert r["new_entry_advice"] == "ok"


def test_intraday_spike_missing_minute_file_is_safe():
    # 不存在的代码的分钟文件 -> available=False，不应抛异常
    res = eg.intraday_spike("999999", "2026-11-06")
    assert res["available"] is False
    assert res["triggered"] is False
    assert res["note"] == ""


def test_intraday_spike_no_signal_day_is_safe():
    # 用一只真实存在的股票，但给一个肯定不在分钟库里的日期
    res = eg.intraday_spike("600000", "1999-01-01")
    assert res["available"] is False
    assert res["triggered"] is False


def test_evaluate_entry_guards_shape_and_flags():
    out = eg.evaluate_entry_guards(limit=10)
    assert "regime" in out and "guards" in out and "signals" in out
    assert out["guards"]["gap_threshold"] == 0.03
    assert isinstance(out["signals"], list)
    # 每条信号都叠加了护栏字段
    for sig in out["signals"]:
        assert "regime_state" in sig
        assert "intraday" in sig
        assert isinstance(sig["flags"], list)
    # 当前为下降市时，新动量信号应被标注「慎开新仓」
    if out["regime"]["new_entry_advice"] == "avoid":
        assert all("下降市慎开新仓" in s["flags"] for s in out["signals"])
