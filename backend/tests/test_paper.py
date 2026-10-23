"""模拟交易引擎测试：费用/整手/仓位/执行/结算。"""
import asyncio
from dataclasses import dataclass

import pytest

from app.paper.engine import (
    FEE_COMMISSION_MIN,
    LOT_SIZE,
    SINGLE_POSITION_MAX_RATIO,
    PaperEngine,
    TradeResult,
)


class TestKlineWarmup:
    def test_warmup_uses_cache(self, monkeypatch, tmp_path):
        """K线预热应调用增量缓存（get_kline_cached），并统计成功/失败。"""
        from app.screener import scanner as scanner_module

        calls = []

        def fake_get_kline(code):
            calls.append(code)
            return None

        monkeypatch.setattr(
            scanner_module, "get_kline_cached", fake_get_kline
        )
        monkeypatch.setattr(
            scanner_module, "get_stock_pool",
            lambda: __import__("pandas").DataFrame({"code": ["600000", "600001", "600002"]}),
        )

        result = asyncio.run(scanner_module.warmup_klines())
        assert result["total"] == 3
        assert result["ok"] == 3
        assert result["fail"] == 0
        assert calls == ["600000", "600001", "600002"]

    def test_warmup_counts_failures(self, monkeypatch):
        """单股取数异常应计入 fail 而不中断整轮。"""
        from app.screener import scanner as scanner_module

        def fake_get_kline(code):
            if code == "600001":
                raise RuntimeError("网络错误")
            return None

        monkeypatch.setattr(scanner_module, "get_kline_cached", fake_get_kline)
        monkeypatch.setattr(
            scanner_module, "get_stock_pool",
            lambda: __import__("pandas").DataFrame({"code": ["600000", "600001"]}),
        )
        result = asyncio.run(scanner_module.warmup_klines())
        assert result["ok"] == 1
        assert result["fail"] == 1


@dataclass
class FakeRec:
    """模拟 Recommendation（只含引擎用到的字段）。"""

    code: str
    name: str
    action: str
    score: float
    reason: str


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """隔离引擎的 DB 与价格依赖。"""
    from app.data.cache import Database

    from app.paper import engine as engine_module

    db = Database(tmp_path / "paper.db")
    monkeypatch.setattr(engine_module, "db", db)
    monkeypatch.setattr(
        "app.data.cache.db", db, raising=False
    )
    eng = PaperEngine()
    eng.settings.paper_initial_capital = 100000.0
    eng.settings.paper_max_positions = 5
    eng.settings.paper_auto_trade = True
    # 价格：600000→10元，600001→50元
    prices = {"600000": 10.0, "600001": 50.0}

    async def fake_price(code):
        return prices.get(code)

    monkeypatch.setattr(eng, "_latest_price", fake_price)
    return eng, db, prices


class TestFee:
    def test_buy_fee_min_commission(self, engine):
        """小额买入触发最低佣金 5 元 + 过户费。"""
        eng, _, _ = engine
        fee = eng.calc_fee("buy", 1000.0)  # 佣金=1000*2.5/万=0.25 → 兜底5元
        assert fee == FEE_COMMISSION_MIN + 1000.0 * 0.1 / 10000

    def test_sell_fee_includes_stamp(self, engine):
        """卖出含印花税千1。"""
        eng, _, _ = engine
        fee = eng.calc_fee("sell", 100000.0)
        # 佣金25 + 印花税100 + 过户费1 = 126
        assert fee == 126.0


class TestExecute:
    def test_buy_lot_rounding(self, engine):
        """买入数量向下取整到 100 股。"""
        eng, db, prices = engine
        eng.ensure_account()
        recs = [FakeRec("600000", "测试A", "buy", 4.0, "金叉突破")]
        results = asyncio.run(eng.execute_recommendations(recs))

        assert len(results) == 1
        r = results[0]
        assert r.side == "buy"
        assert r.shares % LOT_SIZE == 0
        # 预算 = 100000/5 = 20000 → 2000股 × 10元 = 20000
        assert r.shares == 2000
        assert r.amount == 20000.0
        # 现金扣减 = 金额 + 费用
        account = db.get_paper_account()
        assert account["cash"] == pytest.approx(100000 - 20000 - r.fee, abs=0.01)

    def test_buy_single_position_cap(self, engine):
        """单票不超过总资产 20%（大资金场景）。"""
        eng, db, prices = engine
        # 缩小名额使预算超过单票上限：5 名额 × 20% = 100% > 20%
        eng.ensure_account()
        recs = [FakeRec("600000", "测试A", "buy", 4.0, "突破")]
        asyncio.run(eng.execute_recommendations(recs))
        pos = db.get_paper_position("600000")
        # 上限 20000 → 2000股
        assert pos["shares"] == 2000
        assert pos["shares"] * 10.0 <= 100000 * SINGLE_POSITION_MAX_RATIO + 1

    def test_sell_releases_cash(self, engine):
        """卖出后现金增加（扣卖出费用）。"""
        eng, db, prices = engine
        eng.ensure_account()
        asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "buy", 4.0, "突破")]))
        cash_after_buy = db.get_paper_account()["cash"]

        # 次日卖出（绕过 T+1：改买入日期）
        pos = db.get_paper_position("600000")
        db.upsert_paper_position(
            pos["code"], pos["name"], pos["shares"], pos["cost_price"],
            "2020-01-01", pos["strategy"],
        )
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "sell", -4.0, "死叉")]))

        assert results[0].side == "sell"
        assert db.get_paper_position("600000") is None
        cash_after_sell = db.get_paper_account()["cash"]
        assert cash_after_sell > cash_after_buy  # 收回资金

    def test_t_plus_1_blocks_same_day_sell(self, engine):
        """当日买入不可卖出（T+1）。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        db.upsert_paper_position(
            "600000", "测试A", 1000, 10.05, today, "突破")
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "sell", -4.0, "死叉")]))
        assert results[0].side == "skip"
        assert "T+1" in results[0].reason

    def test_max_positions_limit(self, engine):
        """持仓已满时新的买入被跳过。"""
        eng, db, prices = engine
        eng.ensure_account()
        for i in range(5):  # max_positions=5
            db.upsert_paper_position(
                f"60001{i}", f"票{i}", 100, 10.0, "2020-01-01", "test")
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "buy", 4.0, "突破")]))
        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason

    def test_skip_when_already_holding(self, engine):
        """已持仓的买入建议跳过。"""
        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600000", "测试A", 100, 10.0, "2020-01-01", "t")
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "buy", 4.0, "突破")]))
        assert results[0].side == "skip"
        assert "已持仓" in results[0].reason

    def test_auto_trade_disabled(self, engine):
        """开关关闭时不执行任何交易。"""
        eng, db, prices = engine
        eng.settings.paper_auto_trade = False
        eng.ensure_account()
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "buy", 4.0, "突破")]))
        assert results == []
        assert db.get_paper_account()["cash"] == 100000.0


class TestStopLossTarget:
    def test_stop_loss_triggers(self, engine):
        """亏损超过 8% 触发止损卖出。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        # 成本 10 元，现价 9 元（-10% < -8%）→ 止损
        db.upsert_paper_position("600000", "测试A", 1000, 10.0, "2020-01-01", "t")
        prices["600000"] = 9.0
        results = asyncio.run(eng.check_stop_loss_target())
        assert len(results) == 1
        assert results[0].side == "sell"
        assert "止损" in results[0].reason
        assert db.get_paper_position("600000") is None

    def test_take_profit_triggers(self, engine):
        """盈利超过 20% 触发止盈卖出。"""
        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600001", "测试B", 100, 50.0, "2020-01-01", "t")
        prices["600001"] = 61.0  # +22% > +20%
        results = asyncio.run(eng.check_stop_loss_target())
        assert len(results) == 1
        assert "止盈" in results[0].reason

    def test_within_band_no_trigger(self, engine):
        """盈亏在 ±阈值内不触发。"""
        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600000", "测试A", 1000, 10.0, "2020-01-01", "t")
        prices["600000"] = 10.5  # +5%，不触发
        results = asyncio.run(eng.check_stop_loss_target())
        assert results == []


class TestSettle:
    def test_settle_records_equity(self, engine):
        """结算记录当日净值快照。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600000", "测试A", 1000, 10.0, "2020-01-01", "t")
        detail = asyncio.run(eng.settle())

        today = datetime.now().strftime("%Y-%m-%d")
        equity = db.list_paper_equity()
        assert len(equity) == 1
        assert equity[0]["date"] == today
        # 总资产 = 现金100000 + 市值1000×10 = 110000
        assert detail["total_asset"] == pytest.approx(110000, abs=0.01)
        assert detail["return_pct"] == pytest.approx(10.0, abs=0.01)

    def test_account_detail_structure(self, engine):
        """账户详情字段完整。"""
        eng, db, prices = engine
        detail = asyncio.run(eng.account_detail())
        for key in ("initial_capital", "cash", "market_value", "total_asset",
                    "total_pnl", "return_pct", "position_count", "positions"):
            assert key in detail
        assert detail["initial_capital"] == 100000.0

    def test_reset(self, engine):
        """重置恢复初始资金并清空数据。"""
        eng, db, prices = engine
        eng.ensure_account()
        asyncio.run(eng.execute_recommendations(
            [FakeRec("600000", "测试A", "buy", 4.0, "突破")]))
        assert db.get_paper_account()["cash"] < 100000.0

        db.reset_paper_account(100000.0)
        assert db.get_paper_account()["cash"] == 100000.0
        assert db.list_paper_positions() == []
        assert db.list_paper_trades() == []
