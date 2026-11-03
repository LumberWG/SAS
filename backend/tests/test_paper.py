"""模拟交易引擎测试：费用/整手/仓位/执行/结算。"""
import asyncio
from dataclasses import dataclass

import pandas as pd
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
    from app.paper import engine as paper_engine

    settings = get_settings()
    monkeypatch.setattr(settings, "paper_pool_top_n", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_ma", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_weak_pause", False, raising=False)
    # 同理关闭「大盘股过滤」：它读本机真实快照的 total_mv，用例里的票（000001/600036 等）
    # 在真实市值下都会被判成大盘股而跳过。规则本身由 tests/test_exec_rules.py 覆盖。
    monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (False, 0.0))


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
    # 批量核算（total_asset / account_detail）会先把内存快照价合并进报价再取价，
    # 生产环境等价于 _latest_price 内部那次 get_snapshot。但单测里 snapshot_store
    # 会加载磁盘上的真实快照（同一个进程共享单例），真实价会盖掉上面的假价格，
    # 让「取价失败回退成本价」这类用例失真。这里显式清空以保持既有假设；
    # 合并逻辑本身由 TestAccountValuation::test_with_snapshot_prices_* 覆盖。
    monkeypatch.setattr(
        eng, "_with_snapshot_prices", lambda quotes: dict(quotes or {})
    )
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

    def test_sell_frees_slot_for_same_batch_buy(self, engine):
        """同批卖出要立刻腾出名额，否则「卖了就买不进」要等下一轮 tick。"""
        eng, db, prices = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        db.add_paper_lot("600004", "测试A", 2000, 10.05, "2020-01-01", "突破")
        db.update_paper_cash(80000.0)

        results = asyncio.run(eng.execute_recommendations([
            FakeRec("600004", "测试A", "sell", -4.0, "死叉"),
            FakeRec("600001", "测试B", "buy", 45.0, "突破"),
        ]))

        sell = next(r for r in results if r.code == "600004")
        buy = next(r for r in results if r.code == "600001")
        assert sell.shares == 2000
        # 名额只有 1 个：卖出那只是清仓，本轮就该能买新票
        assert buy.side == "buy", buy.reason
        assert db.get_paper_position("600001") is not None

    def test_partial_sell_still_occupies_slot(self, engine):
        """T+1 只能卖昨日批次时，剩余今仓仍占名额，本轮不再买新票。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        db.add_paper_lot("600004", "测试A", 300, 10.0, "2020-01-01", "突破")
        db.add_paper_lot("600004", "测试A", 200, 10.0, today, "突破", today=today)

        results = asyncio.run(eng.execute_recommendations([
            FakeRec("600004", "测试A", "sell", -4.0, "死叉"),
            FakeRec("600001", "测试B", "buy", 45.0, "突破"),
        ]))

        sell = next(r for r in results if r.code == "600004")
        buy = next(r for r in results if r.code == "600001")
        assert sell.shares == 300                      # 只有昨日批次可卖
        assert db.get_paper_position("600004")["shares"] == 200
        assert buy.side == "skip"
        assert "持仓已满" in buy.reason

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

    def test_intraday_no_exposure_cap(self, engine, monkeypatch):
        """盘中不设 80% 硬上限（80% 是收盘目标，由 eod_trim 压回）：

        6 只等权候选会把现金打到近乎为 0，但单票仍受 20% 限制 → 5 只 × 2 万。
        """
        eng, db, _ = engine

        async def fake_price(code, quotes=None):
            return 10.0

        monkeypatch.setattr(eng, "_latest_price", fake_price)
        eng.settings.paper_max_positions = 10
        eng.ensure_account()
        codes = [f"6000{i:02d}" for i in range(6)]
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec(c, f"股票{c}", "buy", 45.0, "突破") for c in codes]))

        bought = [r for r in results if r.side == "buy"]
        invested = sum(r.amount for r in bought)
        assert len(bought) == 6                      # 不再被 80% 拦住，钱能打满
        assert 98000 <= invested <= 100000
        assert all(r.amount <= 20000 + 1e-6 for r in bought)   # 单票 20% 仍生效
        assert db.get_paper_account()["cash"] < 2000

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
        # 可用现金（盘中上限=现金）在三只间均分 ≈33333，但受单票 20% 上限 → 3×20000
        assert invested == pytest.approx(60000, abs=1)

    def test_higher_score_gets_bigger_position(self, engine, monkeypatch):
        """评分加权：45 分的仓位明显大于 30 分（半额档）。

        单票上限放到 50%，否则 20% 的上限会把两组都顶到同一个金额，看不出加权。
        """
        eng, db, _ = engine

        async def fake_price(code, quotes=None):
            return 10.0

        monkeypatch.setattr(eng, "_latest_price", fake_price)
        eng.settings.paper_max_positions = 10
        monkeypatch.setattr(eng.settings, "paper_single_position_max", 0.5, raising=False)
        eng.ensure_account()
        recs = [
            FakeRec("600010", "高分", "buy", 45.0, "突破"),
            *[FakeRec(f"60001{i}", f"低分{i}", "buy", 30.0, "半额") for i in range(1, 5)],
        ]
        results = asyncio.run(eng.execute_recommendations(recs))
        amounts = {r.code: r.amount for r in results if r.side == "buy"}
        assert amounts["600010"] > max(v for k, v in amounts.items() if k != "600010")


class TestAccountValuation:
    def test_total_asset_falls_back_to_cost_price(self, engine, monkeypatch):
        """取价失败按成本价计市值：不能让持仓凭空消失，否则回撤熔断误触发。"""
        eng, db, _ = engine
        eng.ensure_account()
        db.upsert_paper_position("600004", "测试A", 2000, 10.05, "2020-01-01", "t")
        db.update_paper_cash(80000.0)

        async def no_price(code, quotes=None):
            return None

        monkeypatch.setattr(eng, "_latest_price", no_price)
        total = asyncio.run(eng.total_asset())
        assert total == pytest.approx(80000 + 2000 * 10.05, abs=0.01)

    def test_with_snapshot_prices_merges_and_prefers_given(self, engine, monkeypatch):
        """快照价批量并入报价：调用方给定报价优先，缺的由快照补齐。"""
        from app.data.snapshot_store import snapshot_store

        eng, _, _ = engine
        snap = pd.DataFrame([
            {"code": "600004", "price": 8.05},
            {"code": "600088", "price": 12.5},
            {"code": "600099", "price": 0.0},   # 停牌/未开盘，应被丢弃
        ])
        monkeypatch.setattr(snapshot_store, "_snapshot", snap, raising=False)
        monkeypatch.setattr(snapshot_store, "_quotes", None, raising=False)

        # fixture 已把实例方法替换成清空版，这里直接测类方法本身
        merged = PaperEngine._with_snapshot_prices({"600004": 9.9})
        assert merged["600004"] == 9.9          # 给定报价优先
        assert merged["600088"] == 12.5         # 快照补齐
        assert "600099" not in merged           # 无效价不入表


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

    def test_settle_records_regime_snapshot(self, engine):
        """结算顺带落一行择时快照：弱市但没信号的日子也要留痕（#70 §8）。"""
        import json
        from datetime import datetime

        eng, db, _ = engine
        eng.ensure_account()
        asyncio.run(eng.settle())

        today = datetime.now().strftime("%Y-%m-%d")
        raw = db.get_state(f"paper_regime_state:{today}")
        assert raw, "收盘结算必须无条件写一条择时快照"
        payload = json.loads(raw)
        assert payload["date"] == today
        # autouse fixture 关了择时闸门 → 不判弱势，但要注明原因，不能悄悄留个 False
        assert payload["weak"] is False
        assert payload["note"] == "择时未启用"
        assert payload["exposure"] == pytest.approx(0.0)
        # 档位可能来自 app_state 运行时覆盖（线上就是 0.3），断言与引擎同源而非写死
        assert payload["weak_exposure"] == pytest.approx(
            float(eng.settings.paper_weak_exposure)
        )

    def test_regime_detail_reads_prev_close_and_ma(self, engine, monkeypatch, tmp_path):
        """择时明细给出前一交易日收盘与均线，供事后复核判定依据。"""
        import pandas as pd

        eng, _, _ = engine
        # 前 60 天 100、后 20 天 80 → 前一交易日收盘 80 明确跌破 MA60
        closes = [100.0] * 60 + [80.0] * 20
        dates = pd.date_range("2026-01-01", periods=80, freq="D").strftime("%Y-%m-%d")
        pd.DataFrame({"date": dates, "close": closes}).to_parquet(
            tmp_path / "idx_sh000001.parquet", index=False
        )
        monkeypatch.setattr(eng.settings, "kline_dir", tmp_path, raising=False)

        detail = asyncio.run(eng._index_regime_detail("2026-03-21", 60))
        assert detail["weak"] is True
        assert detail["prev_close"] == pytest.approx(80.0)
        assert detail["prev_ma"] > detail["prev_close"]
        assert detail["reason"] == ""

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


class TestQueueTopM:
    """五段式③：交易队列容量——买入按评分择优取 topM，卖出不受限。"""

    def _trader(self, monkeypatch, tmp_path, top_m=2):
        from app.data.cache import Database

        from app.paper import live as live_module

        db = Database(tmp_path / "queue.db")
        monkeypatch.setattr(live_module, "db", db)
        monkeypatch.setattr("app.data.cache.db", db, raising=False)
        trader = live_module.LiveTrader()
        monkeypatch.setattr(trader.settings, "paper_trade_queue_top_m", top_m, raising=False)
        monkeypatch.setattr(trader.settings, "paper_live_enabled", True, raising=False)
        monkeypatch.setattr(trader, "auto_trade_enabled", lambda: True)
        return trader, live_module

    @staticmethod
    def _rec(live_module, code, score, action="buy"):
        return live_module.SimpleNamespace(
            code=code, name=code, action=action, reason="突破", price=10.0, score=score
        )

    def test_buy_queue_capped_by_score(self, monkeypatch, tmp_path):
        """3 条买入、上限 2：只留评分最高的两条，第三条记入拒绝列表。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=2)
        added = trader.submit([
            self._rec(live_module, "600001", 30.0),
            self._rec(live_module, "600002", 50.0),
            self._rec(live_module, "600003", 40.0),
        ])

        assert added == 2
        assert set(trader._pending) == {"600002", "600003"}   # 高分优先
        assert [r["code"] for r in trader._queue_rejected] == ["600001"]
        assert "队列已满" in trader._queue_rejected[0]["reason"]

    def test_sell_not_capped(self, monkeypatch, tmp_path):
        """卖出是释放风险，不受队列容量约束。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        added = trader.submit([
            self._rec(live_module, "600001", 10.0, action="sell"),
            self._rec(live_module, "600002", 10.0, action="sell"),
            self._rec(live_module, "600003", 60.0, action="buy"),
        ])

        assert added == 3
        assert trader._queue_rejected == []

    def test_existing_buy_keeps_slot(self, monkeypatch, tmp_path):
        """已在队列的买单占名额；同股更新不重复占位。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        trader.submit([self._rec(live_module, "600001", 45.0)])
        # 队列已满，新票进不来
        trader.submit([self._rec(live_module, "600002", 90.0)])
        assert set(trader._pending) == {"600001"}
        # 同一只票更新建议：仍只占一个名额
        added = trader.submit([self._rec(live_module, "600001", 70.0)])
        assert added == 1
        assert set(trader._pending) == {"600001"}
        assert trader._pending["600001"].score == 70.0

    def test_zero_means_unlimited(self, monkeypatch, tmp_path):
        """0 = 不限制（回到旧行为）。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=0)
        added = trader.submit([
            self._rec(live_module, f"60000{i}", 10.0 * i) for i in range(1, 6)
        ])

        assert added == 5
        assert len(trader._pending) == 5
        assert trader._queue_rejected == []

    def test_held_code_does_not_take_slot(self, monkeypatch, tmp_path):
        """已持仓的票不占名额。

        引擎对已持仓的买入建议下一步必然回「已持仓」并把它踢出队列，占名额
        等于白扔一个坑。2026-11-03 实盘就是：301331 恩威医药当日 09:30 已建仓、
        评分 83.3，却排在 queue_rejected 第一位，把 600887(82.6) 挤出队列。
        入队本身保留（并行账本各有各的持仓，不能替它们决定）。
        """
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        live_module.db.upsert_paper_position(
            "600001", "测试A", 100, 10.0, "2026-11-03", "测试")

        added = trader.submit([
            self._rec(live_module, "600001", 90.0),
            self._rec(live_module, "600002", 60.0),
        ])

        assert added == 2
        assert set(trader._pending) == {"600001", "600002"}
        assert trader._queue_rejected == []

    def test_overflow_goes_to_backlog(self, monkeypatch, tmp_path):
        """容量被拒的票转存候补，而不是从视野里消失。

        没进过 ``_pending`` 的票进不了 ``_backlog``，也就当不了轮动候选
        （``rotation_candidates`` 只读候补），高分票会被 5 个名额静默挡掉。
        """
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        added = trader.submit([
            self._rec(live_module, "600001", 30.0),
            self._rec(live_module, "600002", 80.0),
            self._rec(live_module, "600003", 50.0),
        ])

        assert added == 1
        # 队列本体仍是 top_m 条，候补不是第二条队列，只在名额释放时补位
        assert set(trader._pending) == {"600002"}
        assert set(trader._backlog) == {"600003", "600001"}
        assert [r["code"] for r in trader._queue_rejected] == ["600003", "600001"]
        assert all(r["deferred"] for r in trader._queue_rejected)
        assert trader._backlog["600003"].score == 50.0

    def test_overflow_backlog_respects_max(self, monkeypatch, tmp_path):
        """候补有上限，满了就不再收，被拒名单照旧留痕。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        monkeypatch.setattr(trader.settings, "paper_backlog_max", 1)

        trader.submit([
            self._rec(live_module, "600001", 30.0),
            self._rec(live_module, "600002", 80.0),
            self._rec(live_module, "600003", 50.0),
        ])

        assert set(trader._backlog) == {"600003"}       # 高分先占候补
        flags = {r["code"]: r["deferred"] for r in trader._queue_rejected}
        assert flags == {"600003": True, "600001": False}

    def test_reject_log_deduped_but_counted(self, monkeypatch, tmp_path, caplog):
        """同一份被拒名单重复提交只留一条日志，累计条次仍要涨。"""
        trader, live_module = self._trader(monkeypatch, tmp_path, top_m=1)
        recs = [
            self._rec(live_module, "600001", 10.0),
            self._rec(live_module, "600002", 90.0),
        ]

        with caplog.at_level("INFO", logger="sas.paper.live"):
            trader.submit(recs)
            once = [r for r in caplog.records if "交易队列已满" in r.getMessage()]
            trader.submit(recs)
            twice = [r for r in caplog.records if "交易队列已满" in r.getMessage()]

        assert len(once) == 1
        assert len(twice) == 1          # 名单没变 → 不再刷屏
        assert trader._queue_reject_total == 2


class TestEntryPoint:
    """五段式⑤：切入点——不追高 / 回踩 / 信号价缺失不限制。"""

    @staticmethod
    def _rec(code="600004", price=None, score=45.0):
        from types import SimpleNamespace

        return SimpleNamespace(
            code=code, name="测试A", action="buy", score=score, reason="突破", price=price
        )

    def test_chase_rejected(self, engine, monkeypatch):
        """现价高于信号价 1.5% 以上 → 追高放弃。"""
        eng, _db, _prices = engine
        monkeypatch.setattr(eng.settings, "paper_entry_max_chase", 0.015, raising=False)
        monkeypatch.setattr(eng.settings, "paper_entry_pullback", 0.0, raising=False)

        ok, why = eng.entry_price_ok(self._rec(price=10.0), 10.2, 10.0)
        assert ok is False and "追高" in why
        assert eng.entry_price_ok(self._rec(price=10.0), 10.1, 10.0)[0] is True

    def test_pullback_required(self, engine, monkeypatch):
        """要求回踩 2%：不低开则放弃，低开够深才买。"""
        eng, _db, _prices = engine
        monkeypatch.setattr(eng.settings, "paper_entry_max_chase", 0.05, raising=False)
        monkeypatch.setattr(eng.settings, "paper_entry_pullback", 0.02, raising=False)

        ok, why = eng.entry_price_ok(self._rec(), 10.0, 10.0)
        assert ok is False and "回踩" in why
        assert eng.entry_price_ok(self._rec(), 9.7, 10.0)[0] is True

    def test_armed_allows_rebound(self, engine, monkeypatch):
        """曾触及回踩线后（armed），价格反弹也能买，只受不追高约束。"""
        eng, _db, _prices = engine
        monkeypatch.setattr(eng.settings, "paper_entry_max_chase", 0.015, raising=False)
        monkeypatch.setattr(eng.settings, "paper_entry_pullback", 0.02, raising=False)

        # 未 armed：10.05 未回踩到 9.8，等回踩
        assert eng.entry_price_ok(self._rec(), 10.05, 10.0)[0] is False
        # armed 后反弹到 10.05（未超过追高线 10.15）→ 允许
        assert eng.entry_price_ok(self._rec(), 10.05, 10.0, armed=True)[0] is True
        # armed 但已追高到 10.2 → 仍然拒绝
        ok, why = eng.entry_price_ok(self._rec(), 10.2, 10.0, armed=True)
        assert ok is False and "追高" in why

    def test_missing_signal_price_allows(self, engine, monkeypatch):
        """老信号没有信号价 → 只校验价格有效性，不做切入点限制。"""
        eng, _db, _prices = engine
        monkeypatch.setattr(eng.settings, "paper_entry_max_chase", 0.015, raising=False)

        assert eng.entry_price_ok(self._rec(), 10.9, None)[0] is True

    def test_execute_skips_chasing(self, engine, monkeypatch):
        """端到端：信号价 10、现价 10.3（+3%）→ 不成交，原因写进结果。"""
        eng, db, _prices = engine
        monkeypatch.setattr(eng.settings, "paper_entry_max_chase", 0.015, raising=False)
        eng.ensure_account()

        results = asyncio.run(eng.execute_recommendations(
            [self._rec(code="600004", price=10.0)], prices={"600004": 10.3}))

        assert results[0].side == "skip"
        assert "追高" in results[0].reason
        assert db.get_paper_position("600004") is None


class TestRotation:
    """满仓后的轮动替换：用显著更高分的新票换掉最弱持仓。"""

    @pytest.fixture(autouse=True)
    def _enable_rotation(self, monkeypatch):
        """轮动默认已开（2026-11-02），这里仍显式置 True：本类用例断言的是「开着时的规则」，
        不随默认值漂移。"""
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(), "paper_rotation_enabled", True, raising=False)

    def _seed(self, db, code="600004", score=30.0, buy_date="2026-10-20", shares=1000):
        db.upsert_paper_position(code, code, shares, 10.0, buy_date, "test", score=score)
        db.update_paper_cash(90000.0)

    def test_rotation_default_on(self):
        """默认开（2026-11-02 使用者决策：模拟盘默认开轮动）。

        断言的是 **Settings 字段默认值**，不是运行时 `eng.settings`（后者会被
        `app_state` 覆盖层与 `.env` 改掉，拿它断言会随运行时配置漂移）。
        """
        from app.config import Settings

        assert Settings.model_fields["paper_rotation_enabled"].default is True

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

    def test_rotation_daily_limit(self, engine, monkeypatch):
        from datetime import datetime

        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        monkeypatch.setattr(eng.settings, "paper_rotation_max_per_day", 1, raising=False)
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

    def test_no_rotation_when_target_rejected_by_gate(self, engine):
        """换入票被交易池闸门拒掉时不许卖：否则只卖不买、凭空降仓。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db)

        # 北交所票：评分再高也买不进，不值得为它卖掉现有持仓
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("830799", "北交票", "buy", 90.0, "突破")]))

        assert all(r.side == "skip" for r in results)
        assert db.get_paper_position("600004") is not None

    def test_no_rotation_when_target_below_half_score(self, engine, monkeypatch):
        """低于半额分的换入票买入阶段本来就会丢，轮动时同样不认。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        monkeypatch.setattr(eng.settings, "paper_rotation_min_score", 20.0, raising=False)
        self._seed(db, score=0.0)

        # 22 分：过了 min_score(20) 与 gap(22>=20)，但 < half_score(25)
        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "温票", "buy", 22.0, "突破")]))

        assert all(r.side == "skip" for r in results)
        assert db.get_paper_position("600004") is not None

    def test_no_rotation_when_proceeds_below_one_lot(self, engine):
        """卖出所得不够买新票一手时不卖（只卖不买等于凭空降仓）。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        # 100 股 × 10 元 = 1000 元，换 600001（50 元/股，一手要 5000）
        self._seed(db, shares=100)

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert all(r.side == "skip" for r in results)
        assert db.get_paper_position("600004") is not None

    def test_rotation_uses_backlog_candidates(self, engine):
        """满仓时候补队列的票不会进 recommendations，必须靠 rotation_candidates 才换得动。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 1
        eng.ensure_account()
        self._seed(db)

        results = asyncio.run(eng.execute_recommendations(
            [], rotation_candidates=[FakeRec("600001", "候补强票", "buy", 60.0, "突破")]))

        assert [r.side for r in results] == ["sell", "buy"]
        assert results[0].code == "600004"
        assert db.get_paper_position("600001") is not None

    def test_weakest_skips_today_buy(self, engine):
        """选弱要跳过当日买入的持仓，而不是被它把整次轮动挡掉。"""
        from datetime import datetime

        eng, db, prices = engine
        eng.settings.paper_max_positions = 2
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        db.upsert_paper_position("600004", "今仓", 1000, 10.0, today, "test", score=0.0)
        db.upsert_paper_position("600001", "底仓", 1000, 50.0, "2026-10-20", "test", score=30.0)
        db.update_paper_cash(40000.0)
        prices["600002"] = 100.0

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600002", "新强", "buy", 60.0, "突破")]))

        assert [r.side for r in results] == ["sell", "buy"]
        assert results[0].code == "600001"      # 换掉底仓，今仓不动
        assert db.get_paper_position("600004") is not None
        assert db.get_paper_position("600001") is None

    def test_rotation_without_full_positions(self, engine):
        """`max_positions` 是上限不是门槛：只有 1 只持仓也能换（2026-11-02 口径）。"""
        eng, db, _ = engine
        eng.settings.paper_max_positions = 10
        eng.ensure_account()
        self._seed(db)                       # 1 只、30 分

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "新强", "buy", 60.0, "突破")]))

        assert [r.side for r in results] == ["sell", "buy"]
        assert db.get_paper_position("600004") is None
        assert db.get_paper_position("600001") is not None

    def test_rotation_swaps_multiple(self, engine):
        """一次调用里可以连换多只（不必等下一个 tick，也不要求满仓）。"""
        eng, db, prices = engine
        eng.settings.paper_max_positions = 10
        eng.ensure_account()
        db.upsert_paper_position("600004", "弱A", 1000, 10.0, "2026-10-20", "test", score=10.0)
        db.upsert_paper_position("600001", "弱B", 1000, 50.0, "2026-10-20", "test", score=20.0)
        db.update_paper_cash(40000.0)
        prices["600002"] = 100.0
        prices["600003"] = 30.0

        results = asyncio.run(eng.execute_recommendations([
            FakeRec("600002", "强票甲", "buy", 70.0, "突破"),
            FakeRec("600003", "强票乙", "buy", 55.0, "突破"),
        ]))

        assert [r.code for r in results if r.side == "sell"] == ["600004", "600001"]
        assert db.get_paper_position("600004") is None
        assert db.get_paper_position("600001") is None


class TestLotAttrs:
    """做T加仓/买回走同一个 `add_paper_lot`，但**不得覆盖底仓的建仓属性**（2026-11-02）。"""

    def test_add_lot_keeps_open_attrs(self, tmp_path, monkeypatch):
        from app.data.cache import Database

        from app.paper import engine as engine_module

        d = Database(tmp_path / "lot.db")
        monkeypatch.setattr(engine_module, "db", d)
        monkeypatch.setattr("app.data.cache.db", d, raising=False)
        d.add_paper_lot(
            "600004", "底仓", 1000, 10.0, "2026-10-20", "综合评分 +88",
            score=88.0, stop_price=9.2, peak_price=12.0, today="2026-10-20",
        )
        # 做T加仓：score 默认 0、stop_price/peak_price 为 None
        d.add_paper_lot(
            "600004", "做T加仓", 100, 10.5, "2026-11-02", "做T加仓",
            today="2026-11-02", tag="t_add",
        )

        pos = d.get_paper_position("600004")
        assert pos["shares"] == 1100                       # 股数照常摊薄
        assert pos["score"] == 88.0                        # 建仓评分不能被写成 0
        assert pos["stop_price"] == 9.2                    # 建仓时定死的止损线
        assert pos["peak_price"] == 12.0                   # 移动止损的高水位不能重置


class TestEodTrim:
    """80% 是**收盘目标**：收盘前 N 分钟把超出部分压回，今仓卖不掉不强求。"""

    def test_trims_inside_window(self, engine):
        from datetime import datetime

        eng, db, _ = engine
        eng.ensure_account()
        # 市值 20000 + 现金 0 → 仓位 100%；目标 80% → 需减 4000（400 股）
        db.upsert_paper_position("600004", "全仓", 2000, 10.0, "2026-10-20", "test", score=30.0)
        db.update_paper_cash(0.0)
        now = datetime.now().replace(hour=14, minute=55, second=0, microsecond=0)

        trades = asyncio.run(eng.eod_trim(now=now))

        assert len(trades) == 1
        assert trades[0].shares == 400
        assert "收盘减仓" in trades[0].reason
        assert db.get_paper_position("600004")["shares"] == 1600

    def test_no_trim_outside_window(self, engine):
        from datetime import datetime

        eng, db, _ = engine
        eng.ensure_account()
        db.upsert_paper_position("600004", "全仓", 2000, 10.0, "2026-10-20", "test", score=30.0)
        db.update_paper_cash(0.0)
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

        assert asyncio.run(eng.eod_trim(now=now)) == []
        assert db.get_paper_position("600004")["shares"] == 2000

    def test_skip_today_buy(self, engine):
        """今仓 T+1 不可卖：全部是今仓时减不动，也不报错（不强求）。"""
        from datetime import datetime

        eng, db, _ = engine
        eng.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")
        db.upsert_paper_position("600004", "今仓", 2000, 10.0, today, "test", score=30.0)
        db.update_paper_cash(0.0)
        now = datetime.now().replace(hour=14, minute=55, second=0, microsecond=0)

        assert asyncio.run(eng.eod_trim(now=now)) == []
        assert db.get_paper_position("600004")["shares"] == 2000


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


class TestRegimeGateState:
    """``/api/paper/live`` 的 regime 字段：把「今天为什么没买」变成可读数字。

    2026-10-30 全天零成交，闸门每 5 秒打一条日志、共 1999 条，排查只能翻日志；
    现在把判定结果与拦截计数直接挂到状态接口上。
    """

    def test_reports_pause_count_for_today(self, monkeypatch):
        from datetime import datetime

        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "paper_regime_ma", 60, raising=False)
        monkeypatch.setattr(settings, "paper_regime_weak_pause", True, raising=False)

        eng = PaperEngine()
        today = datetime.now().strftime("%Y-%m-%d")
        eng._regime_day, eng._regime_paused = today, 12
        eng._weak_cache = (today, True)

        state = eng.regime_state()
        assert state["enabled"] is True
        assert state["ma"] == 60
        assert state["weak"] is True
        assert state["paused_count"] == 12

        # 换日后计数归零：不能让昨天的拦截次数一直挂在页面上
        eng._regime_day = "2020-01-01"
        assert eng.regime_state()["paused_count"] == 0

    def test_index_is_weak_cached_per_day(self, monkeypatch):
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "paper_regime_ma", 60, raising=False)

        eng = PaperEngine()
        calls: list[str] = []

        async def fake_uncached(today: str, n: int) -> bool:
            calls.append(today)
            return True

        monkeypatch.setattr(eng, "_index_is_weak_uncached", fake_uncached)
        eng._weak_cache = ("", False)

        assert asyncio.run(eng._index_is_weak("2026-10-30")) is True
        assert asyncio.run(eng._index_is_weak("2026-10-30")) is True
        # tick 每 5 秒一次，判定只依赖前一日收盘 → 一天只读一次指数 parquet
        assert calls == ["2026-10-30"]

    @staticmethod
    def _gate_on(monkeypatch, eng, *, weak_exposure: float):
        """把弱市闸门打开并固定判为弱市，价格固定 10 元。"""
        monkeypatch.setattr(eng.settings, "paper_regime_ma", 60, raising=False)
        monkeypatch.setattr(eng.settings, "paper_regime_weak_pause", True, raising=False)
        monkeypatch.setattr(eng.settings, "paper_weak_exposure", weak_exposure, raising=False)

        async def _weak(today: str) -> bool:
            return True

        async def _price(code: str, prices):
            return 10.0

        monkeypatch.setattr(eng, "_index_is_weak", _weak)
        monkeypatch.setattr(eng, "_latest_price", _price)

    def test_weak_exposure_zero_pauses_buying(self, engine, monkeypatch):
        """weak_exposure=0（默认）→ 弱市仍按原样拒掉全部买入。"""
        eng, db, _ = engine
        self._gate_on(monkeypatch, eng, weak_exposure=0.0)
        eng.ensure_account()

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "暂停票", "buy", 60.0, "突破")]))

        assert results[0].side == "skip"
        assert "大盘弱势" in results[0].reason

    def test_weak_exposure_keeps_buying(self, engine, monkeypatch):
        """weak_exposure>0 → 弱市不拒买入，只把总仓位上限压到该比例。"""
        eng, db, _ = engine
        self._gate_on(monkeypatch, eng, weak_exposure=0.30)
        monkeypatch.setattr(eng.settings, "paper_max_total_exposure", 0.80, raising=False)
        eng.ensure_account()

        results = asyncio.run(eng.execute_recommendations(
            [FakeRec("600001", "降仓票", "buy", 60.0, "突破")]))

        assert results[0].side == "buy"
        # 30% 上限下，10 万本金最多建 3 万仓位
        pos = db.get_paper_position("600001")
        assert pos is not None
        assert pos["shares"] * 10.0 <= 30000 + 1e-6
