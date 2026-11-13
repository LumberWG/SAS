"""#191 同步验证：回测 pipeline 已对齐实盘入场护栏（板块趋势闸门 / 危机硬拦 / 四档 cap）。

这些用例只测试护栏的**决策逻辑**与接线，不跑完整回测（完整 run_pipeline 需行情数据，
在离线沙箱不可行）。完整回测对齐需在正常环境用真实数据复核。
"""
import app.backtest.pipeline as pl
from app.backtest.pipeline import PipelineParams


def test_imports_resolve():
    # 新增的实盘护栏函数已接入回测模块命名空间（导入不报错 = 接线成功）
    assert hasattr(pl, "should_block_buy")
    assert hasattr(pl, "board_regime_for_code")
    assert hasattr(pl, "get_unified_market_risk")


def test_pipeline_params_defaults_none():
    # 两个新开关默认 None = 沿用线上配置（默认关），零行为变更
    p = PipelineParams("2026-01-01", "2026-02-01")
    assert p.board_trend_gate is None
    assert p.unified_risk_cap_enabled is None


def test_buy_blocked_crisis(monkeypatch):
    # 危机市：should_block_buy 返 True → 无论板块闸门如何都拒买
    monkeypatch.setattr(pl, "should_block_buy", lambda: (True, {"tier": "crisis"}))
    blocked, reason = pl._buy_blocked("600000", board_trend_gate=True)
    assert blocked and "危机" in reason


def test_buy_blocked_board_gate_on_downtrend(monkeypatch):
    # 板块闸门开启 + 板块下降通道 → 拒买
    monkeypatch.setattr(pl, "should_block_buy", lambda: (False, {}))
    monkeypatch.setattr(
        pl, "board_regime_for_code",
        lambda code, ma=None: {"available": True, "regime": "downtrend"},
    )
    blocked, reason = pl._buy_blocked("600000", board_trend_gate=True)
    assert blocked and "板块" in reason


def test_buy_blocked_board_gate_off_downtrend(monkeypatch):
    # 板块闸门关闭 → 即使板块下降通道也放行（默认零风险）
    monkeypatch.setattr(pl, "should_block_buy", lambda: (False, {}))
    monkeypatch.setattr(
        pl, "board_regime_for_code",
        lambda code, ma=None: {"available": True, "regime": "downtrend"},
    )
    blocked, _ = pl._buy_blocked("600000", board_trend_gate=False)
    assert not blocked


def test_buy_blocked_board_uptrend(monkeypatch):
    # 板块上升通道 → 放行
    monkeypatch.setattr(pl, "should_block_buy", lambda: (False, {}))
    monkeypatch.setattr(
        pl, "board_regime_for_code",
        lambda code, ma=None: {"available": True, "regime": "uptrend"},
    )
    blocked, _ = pl._buy_blocked("600000", board_trend_gate=True)
    assert not blocked


def test_buy_blocked_board_unavailable(monkeypatch):
    # 板块数据缺失（available=False）→ 与线上同口径放行，不因缺数据误杀
    monkeypatch.setattr(pl, "should_block_buy", lambda: (False, {}))
    monkeypatch.setattr(
        pl, "board_regime_for_code",
        lambda code, ma=None: {"available": False, "regime": "downtrend"},
    )
    blocked, _ = pl._buy_blocked("600000", board_trend_gate=True)
    assert not blocked
