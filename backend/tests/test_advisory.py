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
            # for_date 必须是下一交易日（生成日+1 跳过周末），不能回显生成日当天
            from app.monitor import is_trading_day
            from datetime import timedelta

            batch_date = datetime.now().strftime("%Y-%m-%d")
            expected = datetime.strptime(batch_date, "%Y-%m-%d")
            for _ in range(7):
                expected = expected + timedelta(days=1)
                if is_trading_day(expected):
                    break
            assert r["for_date"] == expected.strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# #119 明日关注去重：同股多策略命中不能占掉多个 TopN 名额
# ----------------------------------------------------------------------
class TestFocusDedupe:
    @staticmethod
    def hit(code: str, strategy: str, score: float = 3.0, name: str = "测试股") -> dict:
        return {
            "code": code, "name": name, "strategy": strategy,
            "reason": f"{strategy} 触发", "price": 10.0,
            "stop_loss": 9.0, "target_price": 11.5,
            "extra": {"score": score},
        }

    def test_dedupe_merges_strategies(self):
        from app.analysis.recommender import dedupe_focus_hits

        hits = [
            self.hit("002270", "海龟突破"),
            self.hit("002270", "均线放量"),
            self.hit("002270", "平台突破"),
            self.hit("600941", "MACD金叉", score=3.5),
        ]
        out = dedupe_focus_hits(hits)
        assert [h["code"] for h in out] == ["002270", "600941"]
        assert out[0]["_signal_count"] == 3
        assert "3 个策略共振" in out[0]["_focus_reason"]
        assert "海龟突破" in out[0]["_focus_reason"]
        assert "_focus_reason" not in out[1]  # 单策略票不受影响

    def test_dedupe_keeps_highest_score(self):
        from app.analysis.recommender import dedupe_focus_hits

        out = dedupe_focus_hits([
            self.hit("000001", "策略A", score=1.0),
            self.hit("000001", "策略B", score=5.0),
        ])
        assert len(out) == 1
        assert out[0]["extra"]["score"] == 5.0
        assert out[0]["strategy"] == "策略B"

    def test_post_market_focus_topn_are_distinct(self):
        """TopN 必须是 N 只不同的股票（旧逻辑会退化成 1 只票占 3 个名额）。"""
        engine = Recommender()
        hits = [self.hit("002270", s) for s in ("海龟突破", "均线放量", "平台突破")]
        for i, code in enumerate(("600001", "600002", "600003", "600004")):
            hits.append(self.hit(code, "MACD金叉", score=2.0 + i * 0.1))
        focus = asyncio.run(engine.post_market_focus(hits, 5))
        codes = [r.code for r in focus]
        assert len(codes) == 5 and len(set(codes)) == 5
        assert "002270" in codes
        assert any("策略共振" in r.reason for r in focus if r.code == "002270")

    def test_resonance_wins_same_score_tie(self):
        """评分相同时，多策略共振的票排在单策略票前面。"""
        engine = Recommender()
        hits = [self.hit("600001", "单策略")]
        hits += [self.hit("002270", s) for s in ("海龟突破", "均线放量", "平台突破")]
        focus = asyncio.run(engine.post_market_focus(hits, 2))
        assert [r.code for r in focus] == ["002270", "600001"]

    def test_store_dedupe_for_legacy_batch(self, tmp_path):
        """历史批次已存了重复行，读取层按 code 兜底去重（旧行为默认保留）。"""
        db = Database(tmp_path / "test.db")
        db.save_recommendations([
            {"code": "002270", "name": "华明装备", "action": "focus",
             "score": 3.0, "reason": "海龟突破", "price": 19.82},
            {"code": "002270", "name": "华明装备", "action": "focus",
             "score": 3.0, "reason": "均线放量", "price": 19.82},
            {"code": "600941", "name": "中国移动", "action": "focus",
             "score": 3.5, "reason": "MACD金叉", "price": 103.49},
        ], "focus_2026-11-06")
        assert len(db.list_recommendations("focus", "focus")) == 3
        items = db.list_recommendations("focus", "focus", dedupe_by_code=True)
        assert len(items) == 2
        assert [i["code"] for i in items] == ["600941", "002270"]

    def test_api_focus_dedupes_legacy_batch(self, tmp_path, monkeypatch):
        """接口列表与 summary 角标都不再重复。"""
        from fastapi.testclient import TestClient

        from app.main import app

        db = Database(tmp_path / "test.db")
        db.save_recommendations([
            {"code": "002270", "action": "focus", "score": 3.0, "reason": "A"},
            {"code": "002270", "action": "focus", "score": 3.0, "reason": "B"},
            {"code": "600941", "action": "focus", "score": 3.5, "reason": "C"},
        ], f"focus_{datetime.now().strftime('%Y-%m-%d')}")
        monkeypatch.setattr("app.api.advisory.db", db)

        with TestClient(app) as client:
            items = client.get("/api/advisory/focus").json()["items"]
            assert [i["code"] for i in items] == ["600941", "002270"]
            assert client.get("/api/advisory/summary").json()["focus_count"] == 2
