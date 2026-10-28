"""回测引擎单元测试（合成K线，monkeypatch 数据源）。"""
import asyncio

import pandas as pd
import pytest

from app.backtest.engine import BacktestEngine, BacktestParams


def build_kline() -> pd.DataFrame:
    """构造一段合成K线: 90日横盘 → 突破日 → 连续上涨至目标价 → 高位横盘。"""
    dates = pd.date_range("2026-01-01", periods=120).strftime("%Y-%m-%d")

    def rows(price, n, amount=2e8):
        return [{"open": price, "close": price, "high": price * 1.01,
                 "low": price * 0.99, "volume": 1e6, "amount": amount}
                for _ in range(n)]

    data = rows(10.0, 90)
    # 突破日: 收盘 12 创 20 日新高、成交额过亿、阳线
    data.append({"open": 11.0, "close": 12.0, "high": 12.2,
                 "low": 10.9, "volume": 3e6, "amount": 2.5e8})
    # 之后连续上涨, 目标价 = 12 × 1.15 = 13.8
    for p in (12.5, 13.0, 13.5, 13.9, 14.0):
        data.extend(rows(p, 3))
    df = pd.DataFrame(data[:120])
    df.insert(0, "date", dates[:len(df)])
    return df


@pytest.fixture()
def patched_engine(monkeypatch):
    """替换数据获取为合成数据，隔离网络。"""
    df = build_kline()
    monkeypatch.setattr(
        "app.backtest.engine.get_kline_cached", lambda code, **kw: df.copy()
    )
    monkeypatch.setattr(
        "app.data.service.get_stock_pool",
        lambda: pd.DataFrame([{"code": "600000", "name": "测试股", "market": "sh"}]),
    )


class TestBacktest:
    def test_buy_and_take_profit(self, patched_engine):
        params = BacktestParams(
            strategy="海龟突破",
            start_date="2026-03-01",
            end_date="2026-04-30",
            initial_capital=100_000,
            max_positions=1,
            codes=["600000"],
        )
        result = asyncio.run(BacktestEngine().run(params))

        assert result["metrics"]["trade_count"] >= 1
        trade = result["trades"][0]
        assert trade["code"] == "600000"
        assert trade["pnl"] > 0                      # 止盈盈利
        assert result["metrics"]["final_equity"] > 100_000
        assert len(result["equity_curve"]) > 0

    def test_stop_loss_trade(self, monkeypatch):
        """突破后次日暴跌触及止损 → 止损卖出且亏损。"""
        dates = pd.date_range("2026-01-01", periods=120).strftime("%Y-%m-%d")

        def rows(price, n, amount=2e8):
            return [{"open": price, "close": price, "high": price * 1.01,
                     "low": price * 0.99, "volume": 1e6, "amount": amount}
                    for _ in range(n)]

        data = rows(10.0, 90)
        data.append({"open": 11.0, "close": 12.0, "high": 12.2,
                     "low": 10.9, "volume": 3e6, "amount": 2.5e8})
        # 次日跌至 9.5（低于止损 ≈ 10.9）
        data.extend(rows(9.5, 10))
        df = pd.DataFrame(data[:120])
        df.insert(0, "date", dates[:len(df)])

        monkeypatch.setattr(
            "app.backtest.engine.get_kline_cached", lambda code, **kw: df.copy()
        )
        monkeypatch.setattr(
            "app.data.service.get_stock_pool",
            lambda: pd.DataFrame([{"code": "600000", "name": "测试股", "market": "sh"}]),
        )

        params = BacktestParams(
            strategy="海龟突破", start_date="2026-03-01", end_date="2026-04-30",
            initial_capital=100_000, max_positions=1, codes=["600000"],
        )
        result = asyncio.run(BacktestEngine().run(params))
        trade = result["trades"][0]
        assert trade["reason"] == "止损"
        assert trade["pnl"] < 0

    def test_metrics_formulas(self, patched_engine):
        """绩效指标计算正确性: 总收益率与净值曲线一致。"""
        params = BacktestParams(
            strategy="海龟突破", start_date="2026-03-01", end_date="2026-04-30",
            initial_capital=100_000, max_positions=1, codes=["600000"],
        )
        result = asyncio.run(BacktestEngine().run(params))
        metrics = result["metrics"]

        final_equity = result["equity_curve"][-1]["equity"]
        expected_return = (final_equity / 100_000 - 1) * 100
        assert metrics["total_return"] == pytest.approx(expected_return, abs=0.01)
        assert metrics["max_drawdown"] <= 0
        assert -10 < metrics["sharpe"] < 10

    def test_invalid_params_rejected(self):
        """回测区间为空时不应崩溃（返回空结果）。"""
        params = BacktestParams(
            strategy="海龟突破", start_date="2026-03-01",
            end_date="2026-03-02", codes=["999999"],
        )
        # 不 patch 时真实取数会失败/无数据, 引擎应容错返回
        result = asyncio.run(BacktestEngine().run(params))
        assert result["metrics"] == {} or "total_return" in result["metrics"]


def test_pipeline_load_klines_skips_index_files(tmp_path):
    """流水线回测：指数缓存（idx_ 前缀）不能被当成个股混进股票池。"""
    from app.backtest.pipeline import _load_klines

    dates = pd.date_range("2025-01-01", periods=130).strftime("%Y-%m-%d")
    df = pd.DataFrame({
        "date": dates, "open": 10.0, "close": 10.0, "high": 10.1,
        "low": 9.9, "volume": 1e6, "amount": 1e8,
    })
    df.to_parquet(tmp_path / "600000.parquet", index=False)
    df.to_parquet(tmp_path / "idx_sh000001.parquet", index=False)

    data = _load_klines(dates[-1], dates[100], str(tmp_path))
    assert list(data) == ["600000"]


def test_top_pct_takes_relative_rank():
    """分位数门槛：取前 X%，至少 1 只，边界外不越界（记录 #24 的下一步）。"""
    from app.backtest.pipeline import _top_pct

    items = [{"score": 50 - i} for i in range(10)]
    assert [x["score"] for x in _top_pct(items, 0.20)] == [50, 49]
    assert [x["score"] for x in _top_pct(items, 0.10)] == [50]     # ceil(1.0) = 1
    assert len(_top_pct(items[:3], 0.10)) == 1                     # 至少 1 只
    assert _top_pct([], 0.20) == []
    assert len(_top_pct(items, 1.5)) == 10                         # 上限截到全量
    assert len(_top_pct(items, -1)) == 1                           # 下限截到 1 只


def test_universe_board_filter_applies_before_top_n():
    """板块过滤必须先剔除再排名，否则池子容量会缩水。"""
    from app.backtest.pipeline import MAIN_BOARD_PREFIXES, _build_universe_by_date

    raw = {
        "600000": pd.DataFrame({"date": ["d1"], "amount": [10.0]}),
        "688001": pd.DataFrame({"date": ["d1"], "amount": [99.0]}),
        "300001": pd.DataFrame({"date": ["d1"], "amount": [5.0]}),
        "920001": pd.DataFrame({"date": ["d1"], "amount": [50.0]}),
    }
    unfiltered = _build_universe_by_date(raw, ["d1"], 2)
    assert unfiltered["d1"] == ["688001", "920001"]
    filtered = _build_universe_by_date(raw, ["d1"], 2, MAIN_BOARD_PREFIXES)
    assert filtered["d1"] == ["600000", "300001"]


def test_accept_verdict_three_lines():
    """验收三条线：中位差 ≥0、差值>0 占比 ≥60%、最差段容差 5pp；缺一不通过。"""
    from scripts.accept import verdict

    assert verdict([0, 0, 0, 0, 0], [1, 1, 1, 1, 1])["ok"] is True

    # 中位为正（4.5）但只有 50% 的段更好 → ② 不过
    v = verdict([0] * 10, [-1] * 5 + [10] * 5)
    assert v["checks"]["median_diff_ge_0"]["pass"] is True
    assert v["checks"]["positive_share_ge_60"]["pass"] is False
    assert v["ok"] is False

    # 中位与占比都过，但最差段 −20 比基准 worst 0 差 20pp → ③ 不过
    v = verdict([0] * 5, [-20, 10, 10, 10, 10])
    assert v["checks"]["median_diff_ge_0"]["pass"] is True
    assert v["checks"]["positive_share_ge_60"]["pass"] is True
    assert v["checks"]["worst_not_much_worse"]["pass"] is False
    assert v["ok"] is False

    # 容差可调：4pp 差在 5pp 容差内通过
    assert verdict([-10], [-14])["checks"]["worst_not_much_worse"]["pass"] is True
    assert verdict([-10], [-16])["checks"]["worst_not_much_worse"]["pass"] is False

    assert verdict([], [])["ok"] is False


def test_slippage_cost_sign_convention():
    """成交偏离的符号约定：cost_bps > 0 一律表示"比开盘价吃亏"。"""
    from scripts.slippage_audit import bucket_of, cost_bps

    assert cost_bps("buy", 10.10, 10.00) == pytest.approx(100.0)      # 买贵了
    assert cost_bps("buy", 9.90, 10.00) == pytest.approx(-100.0)      # 买便宜了
    assert cost_bps("sell", 9.90, 10.00) == pytest.approx(101.0, rel=1e-3)   # 卖便宜了
    assert cost_bps("sell", 10.10, 10.00) == pytest.approx(-99.0, rel=1e-3)  # 卖贵了
    assert cost_bps("buy", 0.0, 10.0) is None
    assert cost_bps("buy", 10.0, 0.0) is None

    assert bucket_of("09:31") == "开盘30分"
    assert bucket_of("09:59") == "开盘30分"
    assert bucket_of("10:00") == "上午盘中"
    assert bucket_of("13:05") == "下午盘中"
    assert bucket_of("14:35") == "尾盘"
    assert bucket_of("") == "其它"


def test_random_dist_segments_are_disjoint():
    """随机段必须两两不重叠：否则「60 段」只是十来个独立样本（记录 #26 的教训）。"""
    from scripts.random_dist import _segments

    dates = list(pd.bdate_range("2023-01-02", periods=200).strftime("%Y-%m-%d"))
    segs = _segments(dates, span=10, draws=8, seed=7)
    assert len(segs) == 8
    pos = {d: i for i, d in enumerate(dates)}
    spans = [(pos[s], pos[e]) for s, e in segs]
    for i, (a, b) in enumerate(spans):
        for j, (c, d) in enumerate(spans):
            if i != j:
                assert b <= c or a >= d, f"第 {i} 段与第 {j} 段重叠：{(a, b)} {(c, d)}"
    # 同种子必须可复现（冻结协议的前提）
    assert _segments(dates, span=10, draws=8, seed=7) == segs


class TestSelectionModel:
    """选股模型实验开关：策略集筛选与 signals 指纹（见 BACKTEST_LOG 记录 #12）。"""

    def _strategies(self):
        from app.strategies import builtin  # noqa: F401 - 触发内置策略注册
        from app.strategies import registry

        return [registry.create(n) for n in registry.names()]

    def test_none_keeps_all_strategies(self):
        from app.backtest.pipeline import _pick_strategies

        strategies = self._strategies()
        assert _pick_strategies(strategies, None) == strategies

    def test_subset_keeps_only_wanted(self):
        from app.backtest.pipeline import _pick_strategies

        picked = _pick_strategies(self._strategies(), ("海龟突破", "平台突破"))
        assert sorted(s.name for s in picked) == ["平台突破", "海龟突破"]

    def test_unknown_strategy_raises(self):
        from app.backtest.pipeline import _pick_strategies

        with pytest.raises(RuntimeError, match="未知策略"):
            _pick_strategies(self._strategies(), ("不存在的策略",))

    def test_signals_key_tracks_selection_model(self):
        from app.backtest.pipeline import PipelineParams, _signals_key

        base = PipelineParams("2026-01-01", "2026-02-01")
        same = PipelineParams("2026-01-01", "2026-02-01")
        weights = PipelineParams("2026-01-01", "2026-02-01", factor_weights={"rsi": 20.0})
        subset = PipelineParams("2026-01-01", "2026-02-01", strategies=("海龟突破",))
        assert _signals_key(base) == _signals_key(same)
        assert _signals_key(base) != _signals_key(weights)
        assert _signals_key(base) != _signals_key(subset)

    def test_ensure_signals_reuses_cache_on_same_key(self):
        from app.backtest.pipeline import PipelineParams, _ensure_signals, _signals_key

        params = PipelineParams("2026-01-01", "2026-02-01")
        prepared = {"signals": {"2026-01-05": []}, "signals_key": _signals_key(params)}
        assert _ensure_signals(prepared, params) is prepared["signals"]
