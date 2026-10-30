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

_KEYS = ("paper_max_positions", "paper_exclude_large_cap")


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
