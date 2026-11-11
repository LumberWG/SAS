# -*- coding: utf-8 -*-
"""模拟盘执行口径规则测试（对齐回测，见 BACKTEST_LOG 记录 #19）。

实盘 2 天 5 笔 −8% 止损、17 笔成交 vs 回测同区间 3 笔，根因是三条口径不一致：
  1) 池子：实盘全市场扫描，回测只买成交额 TopN；
  2) 时点：实盘盘中实时价一把买入，回测按次日开盘价成交；
  3) 止损：固定 8% 对高波动票 ≈ 1×ATR，被噪声打掉。
这里把三条规则的边界锁死。
"""
from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.paper.engine import _name_ok
from app.paper.live import LiveTrader


@pytest.fixture(scope="module")
def trader() -> LiveTrader:
    return LiveTrader()


@pytest.fixture
def window_on(monkeypatch):
    """显式打开 5 分钟开盘窗口：本机 .env 把它设成了 0（全时段可买）。

    这两个用例要锁的是「窗口内的时点规则」，不应依赖本机运行口径；
    不显式设值就会变成「测 .env」，clean checkout 与本机结果不一致。
    """
    monkeypatch.setattr(get_settings(), "paper_buy_open_window_minutes", 5, raising=False)
    return 5


@pytest.fixture(autouse=True)
def _disable_large_cap(monkeypatch):
    """默认关闭大盘股过滤：本文件其它用例用真实代码跑准入闸门。

    过滤规则读本机真实快照的 ``total_mv``，开着的话「盟固利」这类千亿票会先被市值
    拦掉，掩盖被测的池子/名称规则。规则本身在 ``TestLargeCapGate`` 里单独覆盖。
    """
    from app.paper import engine as paper_engine

    monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (False, 0.0))


# ---------------- 股票池口径：名称可信度 ----------------
def test_name_ok_accepts_normal_name():
    assert _name_ok("华鑫股份", "300621")


@pytest.mark.parametrize("name", ["", "   ", "300621", "鍗庨懌鑲′唤", "西部超导\ufffd"])
def test_name_rejected(name):
    """空名称 / 用代码当名称 / 乱码（含替换字符）一律拒绝下单。

    名称取不到往往意味着股票池不完整（例如实盘买到了 Top300 之外的票），
    静默用代码继续买等于绕过池子约束。
    """
    assert not _name_ok(name, "300621")


# ---------------- 时点口径：买单只在开盘窗口成交 ----------------
def test_in_buy_window(trader, window_on):
    d = datetime(2026, 10, 28)  # 周三
    window = trader._window_minutes()
    assert trader._in_buy_window(d.replace(hour=9, minute=31))
    assert trader._in_buy_window(d.replace(hour=9, minute=29)) is False
    assert trader._in_buy_window(d.replace(hour=13, minute=1)) is False
    assert trader._in_buy_window(d.replace(hour=9, minute=30) + timedelta(minutes=window)) is False


def test_next_buy_window_start(trader, window_on):
    assert trader._next_buy_window_start(
        datetime(2026, 10, 28, 13, 1)
    ) == datetime(2026, 10, 29, 9, 30)
    assert trader._next_buy_window_start(
        datetime(2026, 10, 28, 9, 10)
    ) == datetime(2026, 10, 28, 9, 30)
    # 周五收盘后 → 下周一开盘
    assert trader._next_buy_window_start(
        datetime(2026, 10, 30, 15, 30)
    ) == datetime(2026, 11, 2, 9, 30)


def test_buy_expiry_survives_to_next_open(trader, window_on):
    """13:01 产生的买单必须活到次日开盘窗口，否则永远等不到开盘就过期了。"""
    settings = get_settings()
    ttl = timedelta(minutes=settings.paper_signal_ttl_minutes)
    now = datetime(2026, 10, 28, 13, 1)
    expire = trader._expiry(now, "buy")
    window_end = datetime(2026, 10, 29, 9, 30) + timedelta(minutes=trader._window_minutes())
    assert expire == window_end + ttl
    # 卖单不受窗口约束，仍是「盘中 N 分钟」
    assert trader._expiry(now, "sell") == now + ttl


# ---------------- 止损口径：只放宽、不收紧 ----------------
def test_stop_distance_never_tighter_than_base(trader, monkeypatch):
    from app.data.cache import kline_cache

    monkeypatch.setattr(kline_cache, "get", lambda code: None)
    assert trader.engine._stop_distance("300621") >= 0.08


def test_stop_distance_respects_cap(trader, monkeypatch):
    import pandas as pd

    from app.data.cache import kline_cache

    # 构造一个波动极大的票：ATR≈8 元 / 收盘 100 元 → 2×ATR = 16%
    df = pd.DataFrame({
        "high": [108.0] * 30, "low": [100.0] * 30, "close": [100.0] * 30,
    })
    monkeypatch.setattr(kline_cache, "get", lambda code: df)
    dist = trader.engine._stop_distance("300621")
    cap = get_settings().paper_atr_stop_max
    assert 0.08 <= dist <= max(cap, 0.08)
    assert dist == pytest.approx(0.16, abs=0.02)


def test_pool_rejects_out_of_top_n(trader, monkeypatch):
    monkeypatch.setattr(trader.engine, "_top_amount_codes", lambda: {"300621"})
    ok, why = trader.engine._buy_allowed("300621", "华鑫股份")
    assert ok, why
    ok, why = trader.engine._buy_allowed("301487", "盟固利")
    assert not ok and "成交额" in why


# ---------------- 大盘股过滤（「③ 交易队列」页开关） ----------------
class TestLargeCapGate:
    """总市值阈值：默认开（> 阈值不进队列），关掉才放行。

    银行/石油这类巨无霸短线弹性差，且会被评分仓位放大成账户级风险；但用户可能
    想看它们的信号，所以做成开关 + 可调阈值，而不是写死在黑名单里。
    """

    def test_blocks_above_threshold(self, trader, monkeypatch):
        from app.paper import engine as paper_engine

        monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (True, 100 * 1e8))
        monkeypatch.setattr(paper_engine, "_market_cap_map", lambda: {"300621": 120 * 1e8})
        ok, why = trader.engine._buy_allowed("300621", "华鑫股份")
        assert not ok and "大盘股" in why

    def test_allows_below_threshold(self, trader, monkeypatch):
        from app.paper import engine as paper_engine

        monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (True, 100 * 1e8))
        monkeypatch.setattr(paper_engine, "_market_cap_map", lambda: {"300621": 80 * 1e8})
        monkeypatch.setattr(trader.engine, "_top_amount_codes", lambda: None)
        ok, why = trader.engine._buy_allowed("300621", "华鑫股份")
        assert ok, why

    def test_switch_off_allows_large_cap(self, trader, monkeypatch):
        from app.paper import engine as paper_engine

        monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (False, 100 * 1e8))
        monkeypatch.setattr(paper_engine, "_market_cap_map", lambda: {"300621": 2000 * 1e8})
        monkeypatch.setattr(trader.engine, "_top_amount_codes", lambda: None)
        ok, why = trader.engine._buy_allowed("300621", "华鑫股份")
        assert ok, why

    def test_missing_market_cap_does_not_block(self, trader, monkeypatch):
        """市值取不到（快照抖动）时不拦——数据缺失不该让交易整轮停摆。"""
        from app.paper import engine as paper_engine

        monkeypatch.setattr(paper_engine, "large_cap_rule", lambda: (True, 100 * 1e8))
        monkeypatch.setattr(paper_engine, "_market_cap_map", lambda: {})
        monkeypatch.setattr(trader.engine, "_top_amount_codes", lambda: None)
        ok, why = trader.engine._buy_allowed("300621", "华鑫股份")
        assert ok, why
