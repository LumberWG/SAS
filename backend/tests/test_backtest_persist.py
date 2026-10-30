"""回测规则落盘：映射白名单 / 跳过原因 / 就地生效 + 持久化 / engine 读新值。

设计要点：回测中心默认只影响回测；「将规则落盘保存到数据库」才写实盘配置。
两套参数并非一一对应，落盘只写有实盘落点的子集，其余必须**明确列出原因**，
否则又会回到「页面显示改了、实盘没变」的老问题。
"""
import pytest

from app.api.backtest import (
    BacktestRequest,
    backtest_defaults,
    backtest_persist_map,
    persist_backtest_rules,
)
from app.backtest.apply import BACKTEST_TO_SETTING, split_changes
from app.config import get_settings
from app.data.cache import db
from app.param_specs import OVERRIDE_PREFIX, reset_settings

_KEYS = (
    "paper_stop_loss",
    "paper_take_profit",
    "paper_single_position_max",
    "paper_max_positions",
    "paper_pool_top_n",
)


@pytest.fixture(autouse=True)
def _isolate():
    """落盘会写全局单例与 app_state，用例前后都还原，别污染开发库。"""
    s = get_settings()
    before = {k: getattr(s, k) for k in _KEYS}
    reset_settings(list(_KEYS))
    yield
    reset_settings(list(_KEYS))
    for k, v in before.items():
        setattr(s, k, v)


def _req(**kwargs) -> BacktestRequest:
    return BacktestRequest(start_date="2025-01-01", end_date="2025-06-30", **kwargs)


def test_split_keeps_only_mapped_and_explains_skips():
    applied, skipped = split_changes(
        {"stop_loss": 0.06, "board_prefixes": ["300"], "buy_top_pct": 0.3}
    )
    assert applied == {"paper_stop_loss": 0.06}
    reasons = {item["key"]: item["reason"] for item in skipped}
    assert set(reasons) == {"board_prefixes", "buy_top_pct"}
    assert all(reasons.values())
    # 落盘映射表里每一项都得是真字段，别指向不存在的配置
    from app.config import Settings

    assert all(target in Settings.model_fields for target in BACKTEST_TO_SETTING.values())
    # 买入阈值已从「硬编码、只能改代码」挪进 Settings，回测可落盘
    assert BACKTEST_TO_SETTING["buy_threshold"] == "factor_buy_threshold"


def test_persist_writes_live_config_and_reports_skips():
    res = persist_backtest_rules(_req(stop_loss=0.05, board_prefixes=["300"]))

    assert get_settings().paper_stop_loss == pytest.approx(0.05)
    assert db.get_state(OVERRIDE_PREFIX + "paper_stop_loss") == "0.05"
    assert res["applied"]["paper_stop_loss"] == pytest.approx(0.05)
    assert any(c["setting"] == "paper_stop_loss" for c in res["changed"])
    assert any(item["key"] == "board_prefixes" for item in res["skipped"])


def test_persist_only_touches_explicit_fields():
    """未提交的字段沿用实盘现值，不因 Pydantic 默认值被动改掉配置。"""
    before_pool = get_settings().paper_pool_top_n
    persist_backtest_rules(_req(max_positions=6))
    assert get_settings().paper_max_positions == 6
    assert get_settings().paper_pool_top_n == before_pool


def test_defaults_follow_persisted_values():
    """落盘后默认值接口同步变化，前端换基线后不会再显示成「已改」。"""
    persist_backtest_rules(_req(stop_loss=0.11, take_profit=0.44, single_position_ratio=0.15))
    d = backtest_defaults()["defaults"]
    assert d["stop_loss"] == pytest.approx(0.11)
    assert d["take_profit"] == pytest.approx(0.44)
    assert d["single_position_ratio"] == pytest.approx(0.15)


def test_persist_map_exposes_mapping_and_current():
    payload = backtest_persist_map()
    assert payload["mapping"]["stop_loss"] == "paper_stop_loss"
    assert payload["current"]["paper_stop_loss"] == pytest.approx(get_settings().paper_stop_loss)
    assert payload["skipped"]["invert_score"]


def test_engine_reads_persisted_risk_ratios():
    """实盘风控不再硬编码：落盘后 engine 立即用新值。"""
    from app.paper.engine import PaperEngine

    persist_backtest_rules(_req(stop_loss=0.12, take_profit=0.35))
    eng = PaperEngine()
    assert eng._base_stop_ratio() == pytest.approx(0.12)
    assert eng._take_profit_ratio() == pytest.approx(0.35)
