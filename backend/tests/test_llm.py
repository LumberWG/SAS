"""LLM 分析模块单元测试（mock 客户端，隔离真实 API 调用）。"""
from types import SimpleNamespace

import pandas as pd
import pytest

from app.indicators.technical import add_all_indicators
from app.llm.analyzer import StockAnalyzer, analyzer

VALID_JSON_RESPONSE = """```json
{
  "conclusion": "买入",
  "summary": "均线多头排列，MACD金叉，短线看多",
  "entry_price": 12.5,
  "stop_loss": 11.5,
  "target_price": 14.5,
  "checklist": [
    {"label": "均线排列", "status": "pass", "detail": "多头排列"},
    {"label": "MACD动量", "status": "pass", "detail": "金叉红柱"},
    {"label": "KDJ位置", "status": "warn", "detail": "中位运行"},
    {"label": "量能", "status": "pass", "detail": "放量1.8倍"},
    {"label": "乖离率", "status": "warn", "detail": "偏离正常"},
    {"label": "布林带位置", "status": "pass", "detail": "轨道内运行"}
  ]
}
```"""


def make_kline(n=80, trend=1.0) -> pd.DataFrame:
    """构造带趋势的合成K线。"""
    closes = [10 + i * 0.1 * trend for i in range(n)]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes, "close": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "volume": [1_000_000.0] * n, "amount": [2e8] * n,
    })


class FakeLLMClient:
    """可控假客户端。"""

    def __init__(self, response: str | None = None, raises: bool = False):
        self._response = response
        self._raises = raises
        self.available = True
        self.settings = SimpleNamespace(llm_model="fake-model")

    async def chat(self, messages, **kwargs):
        if self._raises:
            raise RuntimeError("模拟调用失败")
        return self._response


class TestRuleBasedDashboard:
    def test_uptrend_conclusion(self):
        """上涨趋势 → 规则评分应给买入或持有，且 degraded=True。"""
        df = add_all_indicators(make_kline(80, trend=1.0))
        result = analyzer._rule_based_dashboard("600000", "测试股", df)
        assert result.degraded is True
        assert result.model == "rule-based"
        assert result.conclusion in ("买入", "持有", "卖出")
        assert len(result.checklist) == 6
        assert result.stop_loss is not None and result.target_price is not None

    def test_downtrend_conclusion(self):
        """下跌趋势 → 评分应偏空。"""
        df = add_all_indicators(make_kline(80, trend=-1.0))
        result = analyzer._rule_based_dashboard("600000", "测试股", df)
        assert result.conclusion in ("卖出", "持有")


class TestLLMParsing:
    @pytest.mark.asyncio
    async def test_parse_valid_json_with_markdown(self, monkeypatch):
        """LLM 返回 markdown 包裹的 JSON → 正确解析。"""
        monkeypatch.setattr(
            "app.llm.analyzer.llm_client", FakeLLMClient(VALID_JSON_RESPONSE)
        )
        df = make_kline(80)
        result = await analyzer.analyze("600000", "测试股", df)
        assert result.conclusion == "买入"
        assert result.entry_price == 12.5
        assert result.stop_loss == 11.5
        assert result.target_price == 14.5
        assert len(result.checklist) == 6
        assert result.degraded is False
        assert result.model == "fake-model"

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back_to_rules(self, monkeypatch):
        """LLM 输出非 JSON → 降级规则评分。"""
        monkeypatch.setattr(
            "app.llm.analyzer.llm_client", FakeLLMClient("我无法分析该股票。")
        )
        result = await analyzer.analyze("600000", "测试股", make_kline(80))
        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_invalid_conclusion_falls_back(self, monkeypatch):
        """LLM 输出结论非法 → 降级。"""
        bad = VALID_JSON_RESPONSE.replace('"conclusion": "买入"',
                                          '"conclusion": "加仓"')
        monkeypatch.setattr(
            "app.llm.analyzer.llm_client", FakeLLMClient(bad)
        )
        result = await analyzer.analyze("600000", "测试股", make_kline(80))
        assert result.degraded is True

    @pytest.mark.asyncio
    async def test_llm_error_falls_back(self, monkeypatch):
        """LLM 调用异常 → 降级。"""
        monkeypatch.setattr(
            "app.llm.analyzer.llm_client", FakeLLMClient(raises=True)
        )
        result = await analyzer.analyze("600000", "测试股", make_kline(80))
        assert result.degraded is True
        assert result.conclusion in ("买入", "持有", "卖出")

    @pytest.mark.asyncio
    async def test_insufficient_data_raises(self):
        """K线不足 → ValueError。"""
        with pytest.raises(ValueError):
            await analyzer.analyze("600000", "测试股", make_kline(10))
