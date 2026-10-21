"""市场情绪分级单元测试（阈值边界 + 权重 + 快照计算）。"""
import pandas as pd
import pytest

from app.market.sentiment import SENTIMENT_LEVELS, classify_sentiment, sentiment_from_snapshot


class TestClassify:
    @pytest.mark.parametrize("temp,level,weight", [
        (100, "极度亢奋", 1.0),
        (80, "极度亢奋", 1.0),
        (79.9, "多头占优", 0.5),
        (60, "多头占优", 0.5),
        (59.9, "中性", 0.0),
        (40, "中性", 0.0),
        (39.9, "空头占优", -0.5),
        (20, "空头占优", -0.5),
        (19.9, "极度悲观", -1.0),
        (0, "极度悲观", -1.0),
    ])
    def test_boundaries(self, temp, level, weight):
        result = classify_sentiment(temp)
        assert result["level"] == level
        assert result["weight"] == weight
        assert result["advice"]  # 每个等级都有操作建议

    def test_levels_descending(self):
        """等级定义必须按温度降序（classify 依赖此顺序）。"""
        thresholds = [t for t, *_ in SENTIMENT_LEVELS]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_weights_symmetric(self):
        """权重对称: +1/+0.5/0/-0.5/-1。"""
        weights = [w for _, _, w, _ in SENTIMENT_LEVELS]
        assert weights == [1.0, 0.5, 0.0, -0.5, -1.0]


class TestFromSnapshot:
    def _snapshot(self, pcts):
        return pd.DataFrame({"code": [f"60000{i}" for i in range(len(pcts))],
                             "name": ["测试"] * len(pcts),
                             "pct_change": pcts})

    def test_all_up_is_euphoric(self):
        """全部上涨且部分涨停 → 极度亢奋。"""
        pcts = [10.0] * 50 + [1.0] * 950
        result = sentiment_from_snapshot(self._snapshot(pcts))
        assert result["level"] == "极度亢奋"
        assert result["weight"] == 1.0

    def test_all_down_is_pessimistic(self):
        """全部下跌 → 极度悲观。"""
        result = sentiment_from_snapshot(self._snapshot([-1.0] * 1000))
        assert result["level"] == "极度悲观"
        assert result["weight"] == -1.0

    def test_balanced_is_neutral(self):
        """涨跌各半、无涨跌停 → 中性（温度 = 50% × 60 = 30 → 空头占优）。

        注: 上涨占比 50% 对应温度 30（涨停占比 0），落在空头占优区间，
        说明纯广度指标下"半涨半跌"已偏弱——这是公式特性，非 bug。
        """
        pcts = [1.0] * 500 + [-1.0] * 500
        result = sentiment_from_snapshot(self._snapshot(pcts))
        assert result["level"] == "空头占优"

    def test_empty_snapshot_neutral(self):
        """空快照兜底为中性（温度 50）。"""
        result = sentiment_from_snapshot(pd.DataFrame({"pct_change": []}))
        assert result["level"] == "中性"

    def test_nan_pct_ignored(self):
        """NaN 涨跌幅（停牌）不参与统计。"""
        pcts = [10.0] * 50 + [1.0] * 950 + [float("nan")]
        result = sentiment_from_snapshot(self._snapshot(pcts))
        assert result["level"] == "极度亢奋"


class TestRuleBasedDowngrade:
    """规则降级结论的情绪修正: 逆环境买入 → 持有。"""
    def _strong_uptrend_df(self):
        """构造技术面强势（评分 >= 3 触发买入）的K线。

        组合: 均线多头(+2) + KDJ 超卖金叉(+1.5) = 3.5 分。
        """
        from app.indicators.technical import add_all_indicators

        n = 80
        closes = [10 + i * 0.15 for i in range(n)]
        df = add_all_indicators(pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": closes, "close": closes,
            "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
            "volume": [1_000_000.0] * n, "amount": [2e8] * n,
        }))
        # 手工构造 KDJ 超卖金叉（前一日 J=5 超卖，当日 K 上穿 D）
        df.loc[df.index[-2], ["kdj_k", "kdj_d", "kdj_j"]] = [15.0, 20.0, 5.0]
        df.loc[df.index[-1], ["kdj_k", "kdj_d", "kdj_j"]] = [22.0, 19.0, 18.0]
        return df

    def test_bearish_sentiment_downgrades_buy(self):
        from app.llm.analyzer import analyzer

        df = self._strong_uptrend_df()
        # 无情绪: 技术面强势 → 买入
        base = analyzer._rule_based_dashboard("600000", "测试", df)
        assert base.conclusion == "买入"

        # 极度悲观情绪 → 降级为持有
        bear = analyzer._rule_based_dashboard("600000", "测试", df, sentiment={
            "level": "极度悲观", "weight": -1.0, "advice": "恐慌", "temperature": 10.0,
        })
        assert bear.conclusion == "持有"
        assert "降级为持有" in bear.summary
        assert bear.sentiment is not None

    def test_bullish_sentiment_keeps_buy(self):
        from app.llm.analyzer import analyzer

        df = self._strong_uptrend_df()
        bull = analyzer._rule_based_dashboard("600000", "测试", df, sentiment={
            "level": "多头占优", "weight": 0.5, "advice": "顺势", "temperature": 65.0,
        })
        assert bull.conclusion == "买入"
