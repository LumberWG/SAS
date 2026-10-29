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


@pytest.fixture(autouse=True)
def _disable_pool_gate(monkeypatch):
    """本文件的用例只验证仓位/轮动/候补规则，与「成交额 TopN 池」无关。

    池子约束（北交所排除 / 名称守卫 / TopN）由 ``tests/test_exec_rules.py``
    单独覆盖；否则这里读到真实快照时，测试用的假代码会被判成
    「不在当日成交额 Top300 池」而全部跳过。

    同理关闭「大盘择时买入闸门」：它依赖本机真实的上证指数缓存，
    弱市时会把全部买单改成 skip，让仓位/轮动断言失去意义；
    闸门本身由 ``tests/test_exec_rules.py`` 与回测 regime 用例覆盖。
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "paper_pool_top_n", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_ma", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_weak_pause", False, raising=False)


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
            lambda: __import__("pandas").DataFrame({"code": ["600004", "600001", "600002"]}),
        )
        monkeypatch.setattr(
            scanner_module, "get_index_cached",
            lambda symbol: __import__("pandas").DataFrame({"date": ["2026-10-28"]}),
        )

        result = asyncio.run(scanner_module.warmup_klines())
        assert result["total"] == 3
        assert result["ok"] == 3
        assert result["fail"] == 0
        assert calls == ["600004", "600001", "600002"]
        # 指数日线随个股一起日更（择时依赖它，滞后会静默退化默认档）
        assert result["index"]["ok"] is True
        assert result["index"]["last"] == "2026-10-28"

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
            lambda: __import__("pandas").DataFrame({"code": ["600004", "600001"]}),
        )
        monkeypatch.setattr(
            scanner_module, "get_index_cached",
            lambda symbol: __import__("pandas").DataFrame({"date": ["2026-10-28"]}),
        )
        result = asyncio.run(scanner_module.warmup_klines())
        assert result["ok"] == 1
        assert result["fail"] == 1

    def test_warmup_index_failure_keeps_stock_result(self, monkeypatch):
        """指数刷新失败只记在 index 字段里，不影响个股预热统计。"""
        from app.screener import scanner as scanner_module

        def boom(symbol):
            raise RuntimeError("指数数据源不可用")

        monkeypatch.setattr(scanner_module, "get_kline_cached", lambda code: None)
        monkeypatch.setattr(
            scanner_module, "get_stock_pool",
            lambda: __import__("pandas").DataFrame({"code": ["600004"]}),
        )
        monkeypatch.setattr(scanner_module, "get_index_cached", boom)

        result = asyncio.run(scanner_module.warmup_klines())
        assert result["ok"] == 1
        assert result["index"]["ok"] is False
        assert "不可用" in result["index"]["error"]

    def test_kline_cache_stats_counts_existing(self, monkeypatch, tmp_path):
        """缓存统计应按 Parquet 文件存在计数（含最近预热结果）。"""
        import app.screener.scanner as scanner_module

        monkeypatch.setattr(
            scanner_module, "get_stock_pool",
            lambda: __import__("pandas").DataFrame({"code": ["600004", "600001", "600002"]}),
        )
        # 仅 600004 / 600001 有缓存文件
        def fake_exists(code):
            return code in ("600004", "600001")

        monkeypatch.setattr(scanner_module.kline_cache, "_path", lambda code: type(
            "P", (), {"exists": lambda self: fake_exists(code)}
        )())
        stats = scanner_module.kline_cache_stats()
        assert stats["total"] == 3
        assert stats["cached"] == 2
        assert stats["ratio"] == pytest.approx(66.7, abs=0.1)


class TestIndexCached:
    """指数日线缓存：成功写盘合并，失败沿用旧缓存（择时不能因为一次失败断掉）。"""

    @staticmethod
    def _df(dates: list[str]):
        import pandas as pd

        return pd.DataFrame({
            "date": dates,
            "open": 3000.0, "close": 3010.0, "high": 3020.0,
            "low": 2990.0, "volume": 1e8, "amount": 1e11,
        })

    def test_saves_and_merges(self, monkeypatch, tmp_path):
        from app.data import service as service_module
        from app.data.cache import KlineCache

        class FakeSource:
            @staticmethod
            def get_index_daily(symbol, timeout=None, retries=None):
                assert symbol == service_module.INDEX_SYMBOL
                return TestIndexCached._df(["2026-10-27", "2026-10-28"])

        cache = KlineCache(tmp_path)
        cache.save("idx_sh000001", self._df(["2026-10-24", "2026-10-27"]))
        monkeypatch.setattr(service_module, "kline_cache", cache)
        monkeypatch.setattr(service_module, "get_data_source", lambda: FakeSource())

        df = service_module.get_index_cached()
        # 合并去重：旧的两根 + 新的一天，同一天以新拉取为准
        assert list(df["date"]) == ["2026-10-24", "2026-10-27", "2026-10-28"]
        assert cache.last_date("idx_sh000001") == "2026-10-28"

    def test_falls_back_to_cache_on_failure(self, monkeypatch, tmp_path):
        from app.data import service as service_module
        from app.data.cache import KlineCache

        class BrokenSource:
            @staticmethod
            def get_index_daily(symbol, timeout=None, retries=None):
                raise RuntimeError("数据源不可用")

        cache = KlineCache(tmp_path)
        cache.save("idx_sh000001", self._df(["2026-10-27"]))
        monkeypatch.setattr(service_module, "kline_cache", cache)
        monkeypatch.setattr(service_module, "get_data_source", lambda: BrokenSource())

        df = service_module.get_index_cached()
        assert list(df["date"]) == ["2026-10-27"]


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
    # 价格：600004→10元，600001→50元
    prices = {"600004": 10.0, "600001": 50.0}

    async def fake_price(code, quotes=None):
        # 引擎新增了 prices 参数（实时撮合传入逐股报价），这里兼容两种调用
        if quotes and quotes.get(code):
            return quotes[code]
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
        recs = [FakeRec("600004", "测试A", "buy", 45.0, "金叉突破")]
        results = asyncio.run(eng.execute_recommendations(recs))

        assert len(results) == 1
        r = results[0]
        assert r.side == "buy"
        assert r.shares % LOT_SIZE == 0
        # 单票上限 = 100000×20% = 20000 → 2000股 × 10元
        assert r.shares == 2000
        assert r.amount == 20000.0
        # 现金扣减 = 金额 + 费用
        account = db.get_paper_account()
        assert account["cash"] == pytest.approx(100000 - 20000 - r.fee, abs=0.01)

    def test_buy_single_position_cap(self, engine):
        """单票不超过总资产 20%（大资金场景）。"""
        eng, db, prices = engine
        # 单一候选时预算会被单票上限 20% 截住，不会全仓
        eng.ensure_account()
        recs = [FakeRec("600004", "测试A", "buy", 45.0, "突破")]
        asyncio.run(eng.execute_recommendations(recs))
        pos = db.get_paper_position("600004")
        # 上限 20000 → 2000股
        assert pos["shares"] == 2000
        assert pos["shares"] * 10.0 <= 100000 * SINGLE_POSITION_MAX_RATIO + 1

    def test_sell_releases_cash(self, engine):
        """卖出后现金增加（扣卖出费用）。"""
        eng, db, prices = engine
        eng.ensure_account()
        # 批次模型下 T+1 由 lots.buy_date 判定，直接灌一个「昨日买入」的批次，
        # 避免走 upsert_paper_position 改日期（只改汇总表不会改批次，仍判当日买入）
        db.add_paper_lot("600004", "测试A", 2000, 10.05, "2020-01-01", "突破")
        db.update_paper_cash(20000.0)
        cash_before_sell = db.get_paper_account()["cash"]

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600004", "测试A", "sell", -4.0, "死叉")]))

        assert results[0].side == "sell"
        assert db.get_paper_position("600004") is None
        assert db.get_paper_account()["cash"] > cash_before_sell  # 收回资金

    def test_t_plus_1_blocks_same_day_sell(self, engine):
        """当日买入不可卖出（T+1）。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        db.upsert_paper_position(
            "600004", "测试A", 1000, 10.05, today, "突破")
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600004", "测试A", "sell", -4.0, "死叉")]))
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
            [FakeRec("600004", "测试A", "buy", 45.0, "突破")]))
        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason

    def test_skip_when_already_holding(self, engine):
        """已持仓的买入建议跳过。"""
        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600004", "测试A", 100, 10.0, "2020-01-01", "t")
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600004", "测试A", "buy", 45.0, "突破")]))
        assert results[0].side == "skip"
        assert "已持仓" in results[0].reason

    def test_auto_trade_disabled(self, engine):
        """开关关闭时不执行任何交易。"""
        eng, db, prices = engine
        eng.settings.paper_auto_trade = False
        eng.ensure_account()
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600004", "测试A", "buy", 45.0, "突破")]))
        assert results == []
        assert db.get_paper_account()["cash"] == 100000.0

    def test_low_score_only_recorded(self, engine):
        """评分低于半额线（25）只记录建议，不下单。"""
        eng, db, prices = engine
        eng.ensure_account()
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600004", "测试A", "buy", 20.0, "刚过买入线")]))
        assert results[0].side == "skip"
        assert "仅记录" in results[0].reason
        assert db.get_paper_account()["cash"] == 100000.0

    def test_total_exposure_cap(self, engine, monkeypatch):
        """总仓位不超过 80%：6 只等权候选只能花掉 8 万。"""
        eng, db, _ = engine

        async def fake_price(code, quotes=None):
            return 10.0

        monkeypatch.setattr(eng, "_latest_price", fake_price)
        eng.settings.paper_max_positions = 10
        eng.ensure_account()
        codes = [f"6000{i:02d}" for i in range(6)]
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec(c, f"股票{c}", "buy", 45.0, "突破") for c in codes]))

        invested = sum(r.amount for r in results if r.side == "buy")
        assert 75000 <= invested <= 80000 + 1e-6
        assert db.get_paper_account()["cash"] >= 19000

    def test_pool_split_only_among_available_slots(self, engine, monkeypatch):
        """候选多于剩余名额时，预算只在高分候选中分配（不被买不到的摊薄）。"""
        eng, db, _ = engine

        async def fake_price(code, quotes=None):
            return 10.0

        monkeypatch.setattr(eng, "_latest_price", fake_price)
        eng.settings.paper_max_positions = 3
        eng.ensure_account()
        codes = [f"6000{i:02d}" for i in range(5)]
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec(c, f"股票{c}", "buy", 45.0, "突破") for c in codes]))

        bought = [r for r in results if r.side == "buy"]
        assert len(bought) == 3
        invested = sum(r.amount for r in bought)
        # 80% 预算在三只间均分 → 每只 ≈26666 但受单票 20% 上限 → 3×20000
        assert invested == pytest.approx(60000, abs=1)

    def test_higher_score_gets_bigger_position(self, engine, monkeypatch):
        """评分加权：45 分的仓位明显大于 30 分（半额档）。"""
        eng, db, _ = engine

        async def fake_price(code, quotes=None):
            return 10.0

        monkeypatch.setattr(eng, "_latest_price", fake_price)
        eng.settings.paper_max_positions = 10
        eng.ensure_account()
        recs = [
            FakeRec("600010", "高分", "buy", 45.0, "突破"),
            *[FakeRec(f"60001{i}", f"低分{i}", "buy", 30.0, "半额") for i in range(1, 5)],
        ]
        results = asyncio.run(eng.execute_recommendations(recs))
        amounts = {r.code: r.amount for r in results if r.side == "buy"}
        assert amounts["600010"] > max(v for k, v in amounts.items() if k != "600010")


class TestStopLossTarget:
    def test_stop_loss_triggers(self, engine):
        """亏损超过 8% 触发止损卖出。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        # 成本 10 元，现价 9 元（-10% < -8%）→ 止损
        db.upsert_paper_position("600004", "测试A", 1000, 10.0, "2020-01-01", "t")
        prices["600004"] = 9.0
        results = asyncio.run(eng.check_stop_loss_target())
        assert len(results) == 1
        assert results[0].side == "sell"
        assert "止损" in results[0].reason
        assert db.get_paper_position("600004") is None

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
        db.upsert_paper_position("600004", "测试A", 1000, 10.0, "2020-01-01", "t")
        prices["600004"] = 10.5  # +5%，不触发
        results = asyncio.run(eng.check_stop_loss_target())
        assert results == []


class TestSettle:
    def test_settle_records_equity(self, engine):
        """结算记录当日净值快照。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.ensure_account()
        db.upsert_paper_position("600004", "测试A", 1000, 10.0, "2020-01-01", "t")
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
            [FakeRec("600004", "测试A", "buy", 45.0, "突破")]))
        assert db.get_paper_account()["cash"] < 100000.0

        db.reset_paper_account(100000.0)
        assert db.get_paper_account()["cash"] == 100000.0
        assert db.list_paper_positions() == []
        assert db.list_paper_trades() == []


class TestRotation:
    """满仓后的轮动替换：用显著更高分的新票换掉最弱持仓。"""

    @pytest.fixture(autouse=True)
    def _enable_rotation(self, monkeypatch):
        """线上默认已关闭轮动（Top300 池下几乎不触发），本类用例显式打开。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "paper_rotation_enabled", True, raising=False)

    def _seed(self, db, code="600004", score=30.0, buy_date="2026-10-20", shares=1000):
        db.upsert_paper_position(code, code, shares, 10.0, buy_date, "test", score=score)
        db.update_paper_cash(90000.0)

    def test_rotation_swaps_weakest(self, engine):
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db)

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert [r.side for r in results] == ["sell", "buy"]
        assert results[0].code == "600004"
        assert "轮动替换" in results[0].reason
        assert db.get_paper_position("600004") is None
        assert db.get_paper_position("600001") is not None

    def test_no_rotation_when_gap_small(self, engine):
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db)

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新票", "buy", 45.0, "突破")]))  # 差距 15 < 20

        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason
        assert db.get_paper_position("600004") is not None

    def test_no_rotation_on_t1(self, engine):
        from datetime import datetime

        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db, buy_date=datetime.now().strftime("%Y-%m-%d"))

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason

    def test_rotation_daily_limit(self, engine):
        from datetime import datetime

        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db)
        today = datetime.now().strftime("%Y-%m-%d")
        db.insert_paper_trade(
            "600002", "已换过", "sell", 10.0, 1000, 10000.0, 5.0,
            "轮动替换(建仓30分→新票60分)", today,
        )

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason

    def test_rotation_disabled(self, engine):
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.settings.paper_rotation_enabled = False
        eng.ensure_account()
        self._seed(db)

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason


class TestBacklog:
    """候补队列：名额/资金不足时先存着，有名额再买。"""

    def _trader(self, monkeypatch, tmp_path):
        from app.data.cache import Database

        from app.paper import live as live_module

        db = Database(tmp_path / "live.db")
        monkeypatch.setattr(live_module, "db", db)
        monkeypatch.setattr("app.data.cache.db", db, raising=False)
        return live_module.LiveTrader(), live_module

    def _signal(self, live_module, code="600004", score=45.0):
        from datetime import datetime, timedelta

        now = datetime.now()
        return live_module.PendingSignal(
            code=code, name=code, action="buy", reason="突破",
            created_at=now, expire_at=now + timedelta(minutes=5), score=score,
        )

    def test_full_slot_goes_to_backlog(self, monkeypatch, tmp_path):
        from datetime import datetime

        trader, live_module = self._trader(monkeypatch, tmp_path)
        trader._pending["600004"] = self._signal(live_module)

        deferred = trader._defer_skips(
            [TradeResult("600004", "600004", "skip", 0, 10.0, 0.0, 0.0,
                         "持仓已满(10只)")],
            datetime.now(),
        )

        assert deferred == {"600004"}
        assert "600004" in trader._backlog
        # 候补有效期长于普通信号 TTL
        assert trader._backlog["600004"].expire_at > datetime.now()

    def test_other_skips_not_deferred(self, monkeypatch, tmp_path):
        from datetime import datetime

        trader, live_module = self._trader(monkeypatch, tmp_path)
        trader._pending["600004"] = self._signal(live_module)

        deferred = trader._defer_skips(
            [TradeResult("600004", "600004", "skip", 0, 0.0, 0.0, 0.0, "已持仓"),
             TradeResult("600001", "600001", "skip", 0, 0.0, 0.0, 0.0,
                         "评分20<25，仅记录不下单")],
            datetime.now(),
        )

        assert deferred == set()
        assert trader._backlog == {}

    def test_backlog_capacity(self, monkeypatch, tmp_path):
        from datetime import datetime

        trader, live_module = self._trader(monkeypatch, tmp_path)
        monkeypatch.setattr(trader.settings, "paper_backlog_max", 1)
        trader._pending["600004"] = self._signal(live_module, "600004")
        trader._pending["600001"] = self._signal(live_module, "600001")

        deferred = trader._defer_skips(
            [TradeResult("600004", "a", "skip", 0, 10.0, 0.0, 0.0, "持仓已满(10只)"),
             TradeResult("600001", "b", "skip", 0, 10.0, 0.0, 0.0, "持仓已满(10只)")],
            datetime.now(),
        )

        assert len(deferred) == 1
        assert len(trader._backlog) == 1

    def test_manual_position_not_rotated(self, engine):
        """手动建仓的持仓不被自动轮动卖出。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        db.upsert_paper_position(
            "600004", "手动票", 1000, 10.0, "2026-10-20", "手动下单", score=0.0)
        db.update_paper_cash(90000.0)

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert results[0].side == "skip"
        assert "持仓已满" in results[0].reason
        assert db.get_paper_position("600004") is not None
