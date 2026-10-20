"""策略单元测试（合成K线，验证各策略触发条件）。"""
import pandas as pd
import pytest

from app.indicators.technical import add_all_indicators
from app.strategies import builtin  # noqa: F401 - 导入注册策略
from app.strategies.base import Signal
from app.strategies.registry import registry


def build_df(n=40, base=10.0) -> pd.DataFrame:
    """构造一根基准横盘K线（随后由用例覆写最后几日）。"""
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": [base] * n, "close": [base] * n,
        "high": [base * 1.01] * n, "low": [base * 0.99] * n,
        "volume": [1_000_000.0] * n, "amount": [2e8] * n,
    })


def with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    return add_all_indicators(df)


class TestRegistry:
    def test_builtin_registered(self):
        names = registry.names()
        assert "海龟突破" in names
        assert "MACD金叉" in names
        assert len(names) >= 6

    def test_create_unknown_raises(self):
        with pytest.raises(KeyError):
            registry.create("不存在的策略")


class TestTurtleBreakout:
    def test_hit_on_20day_high_with_volume(self):
        df = build_df(30)
        # 最后一日：放量阳线突破 20 日新高
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [11.0, 12.0, 12.2, 10.9]
        df.loc[df.index[-1], "amount"] = 2e8
        sig = registry.create("海龟突破").evaluate(with_indicators(df))
        assert isinstance(sig, Signal)
        assert sig.price == 12.0
        assert sig.stop_loss is not None and sig.target_price is not None

    def test_no_hit_when_small_amount(self):
        df = build_df(30)
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [11.0, 12.0, 12.2, 10.9]
        df.loc[df.index[-1], "amount"] = 1e7  # 不足 1 亿
        assert registry.create("海龟突破").evaluate(with_indicators(df)) is None

    def test_no_hit_when_bearish_candle(self):
        df = build_df(30)
        # 假突破：长上影阴线（收盘低于开盘）
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [12.0, 11.0, 12.5, 10.9]
        assert registry.create("海龟突破").evaluate(with_indicators(df)) is None


class TestMaVolumeBreakout:
    def test_hit_on_bullish_alignment(self):
        # 构造 30 日单边上涨：天然满足 MA5>MA10>MA20 且站上5日线
        closes = [10 + i * 0.3 for i in range(30)]
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=30).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
            "volume": [1_000_000.0] * 29 + [5_000_000.0],  # 最后一日放量
            "amount": [2e8] * 30,
        })
        sig = registry.create("均线放量").evaluate(with_indicators(df))
        assert isinstance(sig, Signal)

    def test_no_hit_when_bearish_alignment(self):
        closes = [20 - i * 0.3 for i in range(30)]  # 单边下跌
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=30).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
            "volume": [1_000_000.0] * 30, "amount": [2e8] * 30,
        })
        assert registry.create("均线放量").evaluate(with_indicators(df)) is None


class TestMacdGoldenCross:
    def test_hit_on_cross_up(self):
        df = build_df(30)
        df = with_indicators(df)
        # 手工设置 DIF/DEA：前一日 DIF<=DEA，当日 DIF>DEA 且柱>0
        df.loc[df.index[-2], ["dif", "dea", "macd_hist"]] = [-0.1, 0.0, -0.2]
        df.loc[df.index[-1], ["dif", "dea", "macd_hist"]] = [0.1, 0.05, 0.1]
        sig = registry.create("MACD金叉").evaluate(df)
        assert isinstance(sig, Signal)

    def test_no_hit_when_already_above(self):
        df = build_df(30)
        df = with_indicators(df)
        # 持续在 DEA 上方，无新金叉
        df.loc[df.index[-2], ["dif", "dea", "macd_hist"]] = [0.2, 0.0, 0.4]
        df.loc[df.index[-1], ["dif", "dea", "macd_hist"]] = [0.3, 0.05, 0.5]
        assert registry.create("MACD金叉").evaluate(df) is None


class TestKdjOversoldCross:
    def test_hit_when_oversold_and_cross(self):
        df = build_df(30)
        df = with_indicators(df)
        df.loc[df.index[-2], ["kdj_k", "kdj_d", "kdj_j"]] = [15.0, 20.0, 5.0]
        df.loc[df.index[-1], ["kdj_k", "kdj_d", "kdj_j"]] = [22.0, 19.0, 18.0]
        sig = registry.create("KDJ超卖金叉").evaluate(df)
        assert isinstance(sig, Signal)

    def test_no_hit_when_not_oversold(self):
        df = build_df(30)
        df = with_indicators(df)
        df.loc[df.index[-2], ["kdj_k", "kdj_d", "kdj_j"]] = [55.0, 50.0, 65.0]
        df.loc[df.index[-1], ["kdj_k", "kdj_d", "kdj_j"]] = [58.0, 52.0, 70.0]
        assert registry.create("KDJ超卖金叉").evaluate(df) is None


class TestPlatformBreakout:
    def test_hit_on_platform_breakout(self):
        # 20 日窄幅横盘 + 最后一日放量突破
        n = 30
        closes = [10.0] * n
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [10.5] * n, "low": [9.5] * n,   # 平台振幅约 10.5%
            "volume": [1_000_000.0] * n,
            "amount": [2e8] * n,
        })
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [10.2, 11.0, 11.2, 10.1]
        df.loc[df.index[-1], "volume"] = 5_000_000.0
        sig = registry.create("平台突破").evaluate(with_indicators(df))
        assert isinstance(sig, Signal)

    def test_no_hit_when_platform_too_wide(self):
        n = 30
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": [10.0] * n, "close": [10.0] * n,
            "high": [13.0] * n, "low": [8.0] * n,   # 振幅 62%，非平台
            "volume": [1_000_000.0] * n, "amount": [2e8] * n,
        })
        df.loc[df.index[-1], ["open", "close", "high", "low"]] = [10.2, 11.0, 11.2, 10.1]
        df.loc[df.index[-1], "volume"] = 5_000_000.0
        assert registry.create("平台突破").evaluate(with_indicators(df)) is None


class TestMomentumBreakout:
    def test_hit_on_strong_momentum(self):
        # 60 日累计涨幅 > 30% 且贴近新高
        n = 70
        closes = [10 * (1.006 ** i) for i in range(n)]  # 约 +51%
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [c * 1.005 for c in closes], "low": [c * 0.995 for c in closes],
            "volume": [1_000_000.0] * n, "amount": [2e8] * n,
        })
        sig = registry.create("动量突破").evaluate(with_indicators(df))
        assert isinstance(sig, Signal)

    def test_no_hit_when_weak_momentum(self):
        n = 70
        closes = [10.0] * n  # 零涨幅
        df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [10.1] * n, "low": [9.9] * n,
            "volume": [1_000_000.0] * n, "amount": [2e8] * n,
        })
        assert registry.create("动量突破").evaluate(with_indicators(df)) is None
