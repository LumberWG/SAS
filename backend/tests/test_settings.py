"""系统设置读写测试：就地生效 / 落库重放 / 类型校验 / 只读拦截 / 还原。"""
import pytest

from app.config import Settings, get_settings
from app.data.cache import db
from app.param_specs import (
    OVERRIDE_PREFIX,
    READONLY_KEYS,
    apply_persisted_overrides,
    reset_settings,
    settings_overview,
    update_settings,
)

_KEYS = (
    "paper_max_positions",
    "paper_exclude_large_cap",
    "paper_large_cap_yi",
    "paper_stop_loss",
    "factor_buy_threshold",
    "factor_sell_threshold",
    "trade_fee_commission_rate",
    "trade_fee_commission_min",
    "trade_fee_stamp_rate",
    "trade_fee_transfer_rate",
)


@pytest.fixture(autouse=True)
def _isolate():
    """测试会改全局 Settings 单例与 app_state，前后都复原，别污染开发库。"""
    s = get_settings()
    before = {k: getattr(s, k) for k in _KEYS}
    reset_settings(list(_KEYS))          # 先清掉可能存在的旧覆盖
    yield
    reset_settings(list(_KEYS))
    for k, v in before.items():
        setattr(s, k, v)


def test_update_takes_effect_immediately():
    """改完立刻对持有 `self.settings` 的实例生效（不重启）。"""
    update_settings({"paper_max_positions": 7})
    assert get_settings().paper_max_positions == 7


def test_override_is_persisted_and_replayed():
    """落库后模拟重启（先把内存改回默认），启动重放应恢复覆盖值。"""
    update_settings({"paper_max_positions": 9})
    assert db.get_state(OVERRIDE_PREFIX + "paper_max_positions") == "9"
    s = get_settings()
    s.paper_max_positions = 10
    assert apply_persisted_overrides() >= 1
    assert s.paper_max_positions == 9


def test_runtime_key_uses_existing_state_key():
    """大盘股开关走无前缀的既有键，engine 的 large_cap_rule 读的是同一个。"""
    update_settings({"paper_exclude_large_cap": False})
    assert db.get_state("paper_exclude_large_cap") == "0"
    from app.paper.engine import large_cap_rule

    enabled, _limit = large_cap_rule()
    assert enabled is False


def test_overview_marks_source():
    update_settings({"paper_max_positions": 6})
    items = [i for g in settings_overview()["groups"] for i in g["items"]]
    item = next(i for i in items if i["key"] == "paper_max_positions")
    assert item["source"] == "app_state"
    assert item["value"] == 6
    assert item["writable"] is True


def test_reset_restores_env_value():
    expected = Settings().paper_max_positions      # .env / 默认口径
    update_settings({"paper_max_positions": 5})
    assert reset_settings(["paper_max_positions"]) == ["paper_max_positions"]
    assert get_settings().paper_max_positions == expected


def test_overview_labels_are_short_with_desc_for_tooltip():
    """卡片里只放短名，整句注释走 `desc`（tooltip）。

    以前标签直接取 `config.py` 注释首行，界面上一堆
    「待执行买单最多同时挂 M 条（卖出不受限，释放风险优先）。」这种半句话，
    卡片里必然截断成省略号。现在标签 ≤ 10 字，说明单独给。
    """
    overview = settings_overview()
    items = [i for g in overview["groups"] for i in g["items"]]
    assert items
    for it in items:
        assert "。" not in it["label"] and "；" not in it["label"], it["key"]
        assert len(it["label"]) <= 10, it
        assert it.get("desc"), f"{it['key']} 缺少 tooltip 说明"


def test_thresholds_and_fees_are_configurable():
    """评分阈值与费率不再硬编码：改一次，选股判级、实盘费用、回测费用同时跟上。"""
    from app.analysis.factors import FactorScore, thresholds
    from app.backtest.pipeline import calc_fee as bt_fee
    from app.paper.engine import PaperEngine

    update_settings({"factor_buy_threshold": 30.0, "trade_fee_commission_rate": 0.00012})

    assert thresholds() == (30.0, -15.0)
    assert FactorScore(total=20.0).action == "hold"      # 20 分不再算 buy
    assert FactorScore(total=31.0).action == "buy"
    # 实盘与回测共用同一份费率：10 万元买入 = 佣金 12 + 过户费 1
    assert PaperEngine.calc_fee("buy", 100000) == pytest.approx(13.0)
    assert bt_fee("buy", 100000) == pytest.approx(13.0)


def test_threshold_cross_validation():
    """买入阈值必须大于卖出阈值，否则同一只票同时满足买卖。"""
    with pytest.raises(ValueError):
        update_settings({"factor_buy_threshold": 10.0, "factor_sell_threshold": 12.0})
    assert get_settings().factor_buy_threshold == Settings().factor_buy_threshold


def test_batch_validation_is_atomic():
    """批里有一项非法就整批不写，避免"前几项已落库"的半成品状态。"""
    with pytest.raises(ValueError):
        update_settings({"paper_max_positions": 6, "paper_stop_loss": 0})
    assert get_settings().paper_max_positions == Settings().paper_max_positions


def test_negative_fee_rejected():
    with pytest.raises(ValueError):
        update_settings({"trade_fee_stamp_rate": -0.001})


def test_overview_only_lists_editable_params():
    """只读项与硬编码常量**完全不出现在清单里**：改不了就不展示。

    路径/列表类（`db_path`、`kline_dir`、`cors_origins` ...）进程启动时就被静态挂载、
    CORS、缓存目录捕获，硬编码常量更要改代码 —— 放在「能改就生效」的页面里只会误导。
    """
    overview = settings_overview()
    editable = {i["key"] for g in overview["groups"] for i in g["items"]}
    assert not (READONLY_KEYS & editable)
    assert overview["total"] == len(editable)
    assert all(i["writable"] for g in overview["groups"] for i in g["items"])
    # 不再返回只读参考区
    assert "readonly" not in overview and "constants" not in overview


@pytest.mark.parametrize("key", sorted(READONLY_KEYS))
def test_readonly_keys_rejected(key):
    """路径/列表类不可页面修改：改了也不会真正生效，直接拒绝更诚实。"""
    with pytest.raises(ValueError):
        update_settings({key: "x"})


def test_type_validation():
    with pytest.raises(ValueError):
        update_settings({"paper_max_positions": "abc"})
    with pytest.raises(ValueError):
        update_settings({"not_a_real_key": 1})


def test_min_value_guard():
    """数值下限集中在校验层，各入口不再各写一份。"""
    with pytest.raises(ValueError):
        update_settings({"paper_large_cap_yi": 0})


def test_queue_filter_shares_same_write_layer():
    """③ 交易队列页的开关与设置页写同一份状态，且共用同一套校验。"""
    from fastapi import HTTPException

    from app.api.paper import QueueFilterBody, set_queue_filter

    set_queue_filter(QueueFilterBody(exclude_large_cap=True, large_cap_yi=150))
    s = get_settings()
    assert s.paper_large_cap_yi == 150
    assert s.paper_exclude_large_cap is True
    with pytest.raises(HTTPException):
        set_queue_filter(QueueFilterBody(exclude_large_cap=True, large_cap_yi=0))
