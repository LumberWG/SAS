"""自定义策略单元测试：参数注入、动态注册、持久化加载。"""
import pandas as pd
import pytest

from app.indicators.technical import add_all_indicators
from app.strategies import builtin, registry  # noqa: F401 - 触发内置注册
from app.strategies.base import ParamSpec


@pytest.fixture(autouse=True)
def clean_custom():
    """每个用例前后清理动态注册的自定义策略，避免相互污染。"""
    yield
    for name in list(registry._custom_meta.keys()):
        registry.unregister(name)


def make_df(n=80, base=10.0) -> pd.DataFrame:
    """平坦基准K线（测试中再覆写最后一日构造突破形态）。"""
    closes = [base] * n
    return add_all_indicators(pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes, "close": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "volume": [1_000_000.0] * n, "amount": [2e8] * n,
    }))


class TestParamInjection:
    def test_default_params(self):
        """未传参数 → 全部取默认值（等价旧版行为）。"""
        strategy = registry.create("海龟突破")
        assert strategy.param("breakout_period") == 20
        assert strategy.param("min_amount_yi") == 1.0

    def test_param_override(self):
        """传参覆盖默认值。"""
        strategy = registry.create("海龟突破", breakout_period=30, require_yang=False)
        assert strategy.param("breakout_period") == 30
        assert strategy.param("require_yang") is False

    def test_param_clamp_out_of_range(self):
        """越界参数被钳制到范围内。"""
        strategy = registry.create("海龟突破", breakout_period=9999)
        assert strategy.param("breakout_period") == 120  # max

    def test_param_invalid_type_fallback(self):
        """非法类型回退默认值。"""
        strategy = registry.create("海龟突破", breakout_period="abc")
        assert strategy.param("breakout_period") == 20

    def test_param_affects_behavior(self):
        """参数真实影响选股行为：高成交额阈值 → 不命中。"""
        df = make_df()
        # 构造突破日
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [11.0, 12.0, 12.2, 10.9]
        df.loc[df.index[-1], "amount"] = 2e8

        strict = registry.create("海龟突破", min_amount_yi=100)  # 100亿阈值
        assert strict.evaluate(df) is None
        loose = registry.create("海龟突破", min_amount_yi=1)
        assert loose.evaluate(df) is not None


class TestDynamicRegistration:
    def test_register_and_create(self):
        registry.register_dynamic("我的突破", "海龟突破", {"breakout_period": 30})
        assert "我的突破" in registry.names()
        strategy = registry.create("我的突破")
        assert strategy.param("breakout_period") == 30
        assert registry.is_custom("我的突破") is True

    def test_custom_ignores_external_params(self):
        """自定义策略实例化时忽略外部参数（固定用保存的参数）。"""
        registry.register_dynamic("固定参数策略", "海龟突破", {"breakout_period": 30})
        strategy = registry.create("固定参数策略", breakout_period=5)
        assert strategy.param("breakout_period") == 30

    def test_duplicate_name_rejected(self):
        registry.register_dynamic("重复名", "海龟突破", {})
        with pytest.raises(ValueError):
            registry.register_dynamic("重复名", "海龟突破", {})

    def test_builtin_name_rejected(self):
        with pytest.raises(ValueError):
            registry.register_dynamic("海龟突破", "海龟突破", {})

    def test_unknown_base_rejected(self):
        with pytest.raises(KeyError):
            registry.register_dynamic("无基础", "不存在的策略", {})

    def test_builtin_not_deletable(self):
        assert registry.unregister("海龟突破") is False

    def test_list_includes_schema(self):
        info = {s["name"]: s for s in registry.list()}["海龟突破"]
        assert info["custom"] is False
        assert "breakout_period" in info["parameters"]
        assert info["parameters"]["breakout_period"]["default"] == 20

    def test_custom_behavior_matches_params(self):
        """自定义策略的选股行为遵循其保存的参数。"""
        df = make_df()
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [11.0, 12.0, 12.2, 10.9]
        df.loc[df.index[-1], "amount"] = 2e8

        registry.register_dynamic(
            "高阈值突破", "海龟突破", {"min_amount_yi": 100}
        )
        assert registry.create("高阈值突破").evaluate(df) is None


class TestPersistence:
    def test_db_roundtrip(self, tmp_path):
        """保存 → 列表 → 删除 全链路。"""
        from app.data.cache import Database

        db = Database(tmp_path / "test.db")
        db.save_custom_strategy(
            "测试策略", "海龟突破", {"breakout_period": 30},
            description="测试", category="自定义",
        )
        items = db.list_custom_strategies()
        assert len(items) == 1
        assert items[0]["name"] == "测试策略"
        assert items[0]["params"]["breakout_period"] == 30
        assert items[0]["base_strategy"] == "海龟突破"

        # 幂等更新
        db.save_custom_strategy("测试策略", "海龟突破", {"breakout_period": 50})
        assert db.list_custom_strategies()[0]["params"]["breakout_period"] == 50

        # 删除
        assert db.delete_custom_strategy("测试策略") is True
        assert db.list_custom_strategies() == []
        assert db.delete_custom_strategy("不存在") is False

    def test_load_custom_from_db(self, tmp_path, monkeypatch):
        """启动加载：DB 中的自定义策略注册进注册表。"""
        from app.data.cache import Database
        from app.strategies.custom import load_custom_strategies

        db = Database(tmp_path / "test.db")
        db.save_custom_strategy("加载测试", "海龟突破", {"breakout_period": 30})
        # load_custom_strategies 内部用全局 db 单例，替换之
        monkeypatch.setattr("app.strategies.custom.db", db)

        count = load_custom_strategies()
        assert count == 1
        assert registry.create("加载测试").param("breakout_period") == 30


class TestParamSpec:
    def test_to_dict(self):
        spec = ParamSpec(type="int", default=20, label="周期", min=5, max=120, unit="日")
        d = spec.to_dict()
        assert d["type"] == "int" and d["default"] == 20
        assert d["min"] == 5 and d["unit"] == "日"
