"""多因子评分测试：权重、分组、阈值分级、明细与脏数据兜底。"""
import pandas as pd

from app.analysis.factors import (
    BUY_THRESHOLD,
    FACTOR_LABELS,
    FACTOR_WEIGHTS,
    SELL_THRESHOLD,
    compute_factors,
)
from app.indicators.technical import add_all_indicators


def make_df(n: int = 80, base: float = 10.0, trend: float = 0.0) -> pd.DataFrame:
    """构造K线；trend>0 上涨、<0 下跌、=0 平坦。"""
    closes = [base + i * trend for i in range(n)]
    return add_all_indicators(pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes, "close": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "volume": [1_000_000.0] * n, "amount": [2e8] * n,
    }))


class TestWeights:
    def test_weights_sum_to_100(self):
        assert sum(FACTOR_WEIGHTS.values()) == 100.0

    def test_every_weight_has_label(self):
        assert set(FACTOR_WEIGHTS) == set(FACTOR_LABELS)

    def test_thresholds_symmetric(self):
        assert BUY_THRESHOLD == -SELL_THRESHOLD == 15.0


class TestGrading:
    def test_bullish_trend_scores_buy(self):
        result = compute_factors(make_df(80, trend=0.15))
        assert result.total >= BUY_THRESHOLD
        assert result.action == "buy"

    def test_bearish_trend_scores_sell(self):
        result = compute_factors(make_df(80, base=30, trend=-0.15))
        assert result.total <= SELL_THRESHOLD
        assert result.action == "sell"

    def test_flat_market_is_hold(self):
        """震荡市不应给出买入/卖出方向。"""
        result = compute_factors(make_df(80, trend=0.0))
        assert result.action in ("buy", "sell", "hold")
        # 横盘时趋势类因子为 0，总分不应达到买卖阈值
        assert abs(result.total) < BUY_THRESHOLD


class TestBreakdown:
    def test_breakdown_covers_all_factors(self):
        result = compute_factors(make_df(80, trend=0.1))
        rows = result.breakdown()
        assert len(rows) == len(FACTOR_WEIGHTS)
        assert {r["key"] for r in rows} == set(FACTOR_WEIGHTS)

    def test_breakdown_sorted_by_contribution(self):
        rows = compute_factors(make_df(80, trend=0.1)).breakdown()
        contribs = [abs(r["contrib"]) for r in rows]
        assert contribs == sorted(contribs, reverse=True)

    def test_contrib_sums_to_total(self):
        result = compute_factors(make_df(80, trend=0.1))
        total = round(sum(i.contrib for i in result.items), 2)
        assert result.total == total

    def test_top_reasons_picks_dominant_side(self):
        result = compute_factors(make_df(80, base=30, trend=-0.15))
        assert result.top_reasons(positive=False)
        assert len(result.top_reasons(positive=False)) <= 2


class TestDirtyData:
    def test_short_df_does_not_raise(self):
        """K线很短时各因子退化为 0，不抛异常。"""
        result = compute_factors(make_df(3, trend=0.1))
        assert isinstance(result.total, float)

    def test_missing_indicator_columns(self):
        """缺少指标列时绝大多数因子按 0 分处理，不给买卖方向。"""
        df = pd.DataFrame({
            "close": [10.0, 10.1, 10.2], "high": [10.1, 10.2, 10.3],
            "low": [9.9, 10.0, 10.1], "volume": [1e6, 1e6, 1e6],
        })
        result = compute_factors(df)
        assert result.action == "hold"
        # 只有「突破强度」可算，其余因子（均线/MACD/RSI/ATR 等）均为 0
        zero_keys = {i.key for i in result.items if i.sub == 0}
        assert {"ma_trend", "macd", "rsi", "atr", "boll"} <= zero_keys
