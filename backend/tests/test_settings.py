"""系统设置读写测试：就地生效 / 落库重放 / 类型校验 / 只读拦截 / 还原。"""
import pytest

from app.config import Settings, get_settings
from app.data.cache import db
from app.param_specs import (
    MASK_MARK,
    OVERRIDE_PREFIX,
    READONLY_KEYS,
    apply_persisted_overrides,
    mask_secret,
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


@pytest.fixture
def _llm_key_isolate():
    """`llm_api_key` 不在自动隔离的 _KEYS 里，单独兜住，别把开发库的密钥写坏。"""
    s = get_settings()
    before = s.llm_api_key
    reset_settings(["llm_api_key"])
    yield s
    reset_settings(["llm_api_key"])
    s.llm_api_key = before


def test_secret_values_are_masked_in_overview(_llm_key_isolate):
    """密钥 / 机器人地址在接口里只回显掩码 —— 站点公网可达时明文等于公开。"""
    s = _llm_key_isolate
    s.llm_api_key = "sk-abcdefghijklmnop"
    items = {i["key"]: i for g in settings_overview()["groups"] for i in g["items"]}
    item = items["llm_api_key"]
    assert item["secret"] is True
    assert item["value"].endswith("mnop")
    assert "defghij" not in item["value"]
    assert MASK_MARK in item["value"]


def test_masked_value_is_ignored_on_update(_llm_key_isolate):
    """前端把掩码原样回传时视为「未修改」，不能把密钥写成 `sk-****mnop`。"""
    s = _llm_key_isolate
    s.llm_api_key = "sk-real-key-abcdef"
    update_settings({"llm_api_key": mask_secret(s.llm_api_key)})
    assert s.llm_api_key == "sk-real-key-abcdef"


def test_secret_can_be_cleared_explicitly(_llm_key_isolate):
    """显式传空串 = 清空（掩码逻辑只拦带 `****` 的回传，不拦真正的空值）。"""
    s = _llm_key_isolate
    s.llm_api_key = "sk-real-key-abcdef"
    update_settings({"llm_api_key": ""})
    assert s.llm_api_key == ""


def test_depends_on_forms_valid_tree():
    """条件可见的父键必须存在、本身可编辑，且不能成环。

    前端只认 `depends_on` 一条关系（父为开关看真假、父为文本看非空），
    所以这里守住「父键合法 + 单链无环」，避免某个键永远显示不出来。
    """
    items = {i["key"]: i for g in settings_overview()["groups"] for i in g["items"]}
    parents = {k: it["depends_on"] for k, it in items.items() if it.get("depends_on")}
    assert parents, "至少应有做T / LLM 这类条件项"
    for child, parent in parents.items():
        assert parent in items, f"{child} 的父键 {parent} 不在可编辑清单里"
        seen = {child}
        cursor = parent
        while cursor in parents:
            assert cursor not in seen, f"条件可见成环: {child}"
            seen.add(cursor)
            cursor = parents[cursor]

    # 做T：总开关关着时 8 个子项都不该显示
    t_children = [k for k, p in parents.items() if p == "paper_t_active"]
    assert "paper_t_size" in t_children and len(t_children) == 8


def test_reset_is_audited(tmp_path, monkeypatch):
    """还原要留痕：谁、什么时候、还原了哪几项。"""
    from fastapi.testclient import TestClient

    from app.main import app

    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr("app.audit.audit_path", lambda: audit_file)

    with TestClient(app) as client:
        r = client.post("/api/settings/reset", json={"keys": ["paper_max_positions"]})
        assert r.status_code == 200

    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert lines, "还原没有写审计日志"
    import json

    entry = json.loads(lines[-1])
    assert entry["action"] == "reset"
    assert "paper_max_positions" in entry["keys"]


def test_api_token_guard(monkeypatch):
    """配了 API_TOKEN 后所有业务接口都要令牌，健康检查放行（部署脚本探活用）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(get_settings(), "api_token", "tkn-123")

    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        ok = client.get("/api/settings", headers={"X-API-Token": "tkn-123"})
        assert ok.status_code == 200
        bearer = client.get("/api/settings", headers={"Authorization": "Bearer tkn-123"})
        assert bearer.status_code == 200
        assert client.get("/api/health").status_code == 200


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
