"""智能建议测试：评分规则、建议引擎分级、批次持久化、API。"""
import asyncio
from datetime import datetime

import pandas as pd
import pytest

from app.analysis.recommender import Recommender
from app.analysis.scoring import score_technical
from app.data.cache import Database
from app.indicators.technical import add_all_indicators


# ----------------------------------------------------------------------
#  fixtures / helpers
# ----------------------------------------------------------------------
def make_df(n: int = 80, base: float = 10.0, trend: float = 0.0) -> pd.DataFrame:
    """构造K线；trend>0 上涨、<0 下跌、=0 平坦。"""
    closes = [base + i * trend for i in range(n)]
    return add_all_indicators(pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes, "close": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "volume": [1_000_000.0] * n, "amount": [2e8] * n,
    }))


def bullish_df() -> pd.DataFrame:
    """技术面强势（均线多头 + KDJ 超卖金叉 → 评分 >= 3）。"""
    df = make_df(80, trend=0.15)
    df.loc[df.index[-2], ["kdj_k", "kdj_d", "kdj_j"]] = [15.0, 20.0, 5.0]
    df.loc[df.index[-1], ["kdj_k", "kdj_d", "kdj_j"]] = [22.0, 19.0, 18.0]
    return df


def bearish_df() -> pd.DataFrame:
    """技术面弱势（均线空头 + KDJ 超买 + MACD 死叉 → 评分 <= -3）。"""
    df = make_df(80, base=30, trend=-0.15)
    # KDJ 超买（当日 J=85）
    df.loc[df.index[-2], ["kdj_k", "kdj_d", "kdj_j"]] = [80.0, 75.0, 95.0]
    df.loc[df.index[-1], ["kdj_k", "kdj_d", "kdj_j"]] = [76.0, 74.0, 85.0]
    # MACD 死叉（前一日 DIF>=DEA，当日 DIF<DEA 且柱<0）
    df.loc[df.index[-2], ["dif", "dea", "macd_hist"]] = [0.1, 0.05, 0.1]
    df.loc[df.index[-1], ["dif", "dea", "macd_hist"]] = [-0.1, 0.0, -0.2]
    return df


@pytest.fixture()
def patched_engine(monkeypatch):
    """隔离 recommender 的数据依赖。

    add_all_indicators 替换为 identity：analyze_stock 内部会重算指标，
    测试需要保留手工构造的指标值（指标计算本身由 TestScoring 覆盖）。
    """
    engine = Recommender()
    monkeypatch.setattr(
        "app.analysis.recommender._safe_snapshot",
        lambda: pd.DataFrame({"pct_change": [1.0] * 100}),  # 情绪多头占优
    )
    monkeypatch.setattr(
        "app.analysis.recommender.add_all_indicators", lambda df: df
    )
    return engine


# ----------------------------------------------------------------------
class TestScoring:
    def test_bullish_scores_positive(self):
        result = score_technical(bullish_df())
        assert result.score >= 3
        assert any(status == "pass" for _, status, _ in result.checklist)

    def test_bearish_scores_negative(self):
        result = score_technical(bearish_df())
        assert result.score <= -3

    def test_checklist_six_dimensions(self):
        result = score_technical(make_df())
        labels = [label for label, _, _ in result.checklist]
        assert labels == ["均线排列", "MACD动量", "KDJ位置", "量能", "乖离率"]


class TestRecommender:
    def test_bullish_stock_is_buy(self, patched_engine, monkeypatch):
        monkeypatch.setattr(
            "app.analysis.recommender.get_kline_cached",
            lambda code, **kw: bullish_df(),
        )
        rec = asyncio.run(patched_engine.analyze_stock("600000", "测试股"))
        assert rec is not None
        assert rec.action == "buy"
        assert rec.stop_loss is not None and rec.target_price is not None

    def test_bearish_stock_is_sell(self, patched_engine, monkeypatch):
        monkeypatch.setattr(
            "app.analysis.recommender.get_kline_cached",
            lambda code, **kw: bearish_df(),
        )
        rec = asyncio.run(patched_engine.analyze_stock("600000", "测试股"))
        assert rec is not None
        assert rec.action == "sell"

    def test_bearish_sentiment_keeps_buy(self, patched_engine, monkeypatch):
        """情绪悲观不再一票否决：弱市仍按因子总分给买入，情绪只写入理由。

        设计依据：别人恐惧时贪婪——大落才有机，是否买入交给因子模型判断，
        而不是宏观情绪直接降级。
        """
        monkeypatch.setattr(
            "app.analysis.recommender.get_kline_cached",
            lambda code, **kw: bullish_df(),
        )
        monkeypatch.setattr(
            "app.analysis.recommender._safe_snapshot",
            lambda: pd.DataFrame({"pct_change": [-5.0] * 1000}),  # 极度悲观
        )
        rec = asyncio.run(patched_engine.analyze_stock("600000", "测试股"))
        assert rec.action == "buy"
        assert "市场情绪" in rec.reason

    def test_insufficient_data_returns_none(self, patched_engine, monkeypatch):
        monkeypatch.setattr(
            "app.analysis.recommender.get_kline_cached",
            lambda code, **kw: make_df(10),
        )
        rec = asyncio.run(patched_engine.analyze_stock("600000", "测试股"))
        assert rec is None


class TestRecommendationStore:
    def test_batch_roundtrip(self, tmp_path):
        db = Database(tmp_path / "test.db")
        items = [
            {"code": "600001", "name": "甲", "action": "buy", "score": 5.0,
             "reason": "测试", "price": 10.0, "stop_loss": 9.0,
             "target_price": 11.5, "sentiment": "多头占优"},
            {"code": "600002", "name": "乙", "action": "sell", "score": -4.0,
             "reason": "测试", "price": 20.0},
        ]
        saved = db.save_recommendations(items, "intraday_2026-10-22")
        assert saved == 2

        buys = db.list_recommendations("intraday", "buy")
        assert len(buys) == 1 and buys[0]["code"] == "600001"
        sells = db.list_recommendations("intraday", "sell")
        assert len(sells) == 1 and sells[0]["code"] == "600002"
        assert db.latest_batch("intraday") == "intraday_2026-10-22"

    def test_same_batch_replaced(self, tmp_path):
        """同批次重复保存只保留最新（盘中每 5 分钟覆盖）。"""
        db = Database(tmp_path / "test.db")
        db.save_recommendations(
            [{"code": "600001", "action": "buy", "score": 1.0}],
            "intraday_2026-10-22",
        )
        db.save_recommendations(
            [{"code": "600002", "action": "buy", "score": 5.0}],
            "intraday_2026-10-22",
        )
        items = db.list_recommendations("intraday", "buy")
        assert len(items) == 1 and items[0]["code"] == "600002"

    def test_latest_batch_wins(self, tmp_path):
        """同前缀多日期批次取最新。"""
        db = Database(tmp_path / "test.db")
        db.save_recommendations(
            [{"code": "600001", "action": "focus", "score": 3.0}],
            "focus_2026-10-21",
        )
        db.save_recommendations(
            [{"code": "600002", "action": "focus", "score": 5.0}],
            "focus_2026-10-22",
        )
        items = db.list_recommendations("focus")
        assert len(items) == 1 and items[0]["code"] == "600002"


class TestAdvisoryApi:
    def test_endpoints_empty(self):
        """无数据时接口正常返回空列表。"""
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            for path in ("/api/advisory/buy", "/api/advisory/sell",
                         "/api/advisory/focus", "/api/advisory/summary"):
                r = client.get(path)
                assert r.status_code == 200, path
                assert "disclaimer" in r.json()

    def test_focus_with_data(self, tmp_path, monkeypatch):
        """造一批 focus 数据后接口可查。"""
        from fastapi.testclient import TestClient

        from app.data.cache import Database
        from app.main import app

        db = Database(tmp_path / "test.db")
        db.save_recommendations(
            [{"code": "600519", "name": "贵州茅台", "action": "focus",
              "score": 6.5, "reason": "海龟突破", "price": 1500.0,
              "stop_loss": 1400.0, "target_price": 1725.0}],
            f"focus_{datetime.now().strftime('%Y-%m-%d')}",
        )
        monkeypatch.setattr("app.api.advisory.db", db)

        with TestClient(app) as client:
            r = client.get("/api/advisory/focus").json()
            assert len(r["items"]) == 1
            assert r["items"][0]["code"] == "600519"
            assert r["for_date"]  # 对应关注日期
