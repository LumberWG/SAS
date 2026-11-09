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


def test_should_block_buy_off_by_default():
    # 默认开关关闭：即使处于下降市也不拦截
    s = eg.get_settings()
    prev = s.entry_guard_block_declining_market
    s.entry_guard_block_declining_market = False
    try:
        blocked, regime = eg.should_block_buy()
        assert blocked is False
        # regime 仍返回（供展示），但不触发拦截
        assert isinstance(regime, dict)
    finally:
        s.entry_guard_block_declining_market = prev


def test_should_block_buy_active_in_declining_market(monkeypatch):
    # 开关打开 + 下降市 -> 拦截并附 regime
    s = eg.get_settings()
    prev = s.entry_guard_block_declining_market
    s.entry_guard_block_declining_market = True
    try:
        monkeypatch.setattr(
            eg, "market_regime",
            lambda: {"state": "declining", "close": 3600.0, "ma20": 3680.0,
                     "ma60": 3830.0, "as_of": "2026-11-09",
                     "new_entry_advice": "avoid"},
        )
        blocked, regime = eg.should_block_buy()
        assert blocked is True
        assert regime["state"] == "declining"
    finally:
        s.entry_guard_block_declining_market = prev


def test_should_block_buy_armed_but_not_declining(monkeypatch):
    # 开关打开但非下降市（如上升市）-> 不拦截
    s = eg.get_settings()
    prev = s.entry_guard_block_declining_market
    s.entry_guard_block_declining_market = True
    try:
        monkeypatch.setattr(
            eg, "market_regime",
            lambda: {"state": "uptrend", "close": 3700.0, "ma20": 3680.0,
                     "ma60": 3830.0, "as_of": "2026-11-09",
                     "new_entry_advice": "ok"},
        )
        blocked, regime = eg.should_block_buy()
        assert blocked is False
        assert regime["state"] == "uptrend"
    finally:
        s.entry_guard_block_declining_market = prev


def test_ops_block_state_reflects_setting(monkeypatch):
    # _entry_guard_block_state 应如实反映「未启用 / 已布防(非下降市) / 已拦截(下降市)」
    from app.api import ops

    s = eg.get_settings()
    prev = s.entry_guard_block_declining_market
    try:
        # 1) 未启用
        s.entry_guard_block_declining_market = False
        st = ops._entry_guard_block_state()
        assert st["enabled"] is False and st["state"] == "off"

        # 2) 已启用 + 下降市 -> 拦截
        s.entry_guard_block_declining_market = True
        monkeypatch.setattr(
            eg, "market_regime",
            lambda: {"state": "declining", "close": 3600.0, "ma20": 3680.0,
                     "ma60": 3830.0, "as_of": "2026-11-09",
                     "new_entry_advice": "avoid"},
        )
        st = ops._entry_guard_block_state()
        assert st["enabled"] is True and st["active"] is True and st["state"] == "declining"

        # 3) 已启用 + 上升市 -> 布防但未触发
        monkeypatch.setattr(
            eg, "market_regime",
            lambda: {"state": "uptrend", "close": 3700.0, "ma20": 3680.0,
                     "ma60": 3830.0, "as_of": "2026-11-09",
                     "new_entry_advice": "ok"},
        )
        st = ops._entry_guard_block_state()
        assert st["enabled"] is True and st["active"] is False and st["state"] == "armed"
    finally:
        s.entry_guard_block_declining_market = prev


