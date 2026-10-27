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
