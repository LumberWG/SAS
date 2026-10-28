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
from app.paper.engine import _is_bj, _name_ok
from app.paper.live import LiveTrader


@pytest.fixture(scope="module")
def trader() -> LiveTrader:
    return LiveTrader()


# ---------------- 股票池口径：北交所 / 名称可信度 ----------------
@pytest.mark.parametrize("code", ["920174", "430047", "830799", "871981", "400001"])
def test_bj_codes_excluded(code):
    assert _is_bj(code)


@pytest.mark.parametrize("code", ["600621", "002945", "301487", "688271", "900901"])
def test_non_bj_codes_pass(code):
    assert not _is_bj(code)


def test_name_ok_accepts_normal_name():
    assert _name_ok("华鑫股份", "600621")


@pytest.mark.parametrize("name", ["", "   ", "600621", "鍗庨懌鑲′唤", "西部超导\ufffd"])
def test_name_rejected(name):
    """空名称 / 用代码当名称 / 乱码（含替换字符）一律拒绝下单。

    名称取不到往往意味着股票池不完整（例如实盘买到了 Top300 之外的票），
    静默用代码继续买等于绕过池子约束。
    """
    assert not _name_ok(name, "600621")


# ---------------- 时点口径：买单只在开盘窗口成交 ----------------
def test_in_buy_window(trader):
    d = datetime(2026, 10, 28)  # 周三
    window = trader._window_minutes()
    assert trader._in_buy_window(d.replace(hour=9, minute=31))
    assert trader._in_buy_window(d.replace(hour=9, minute=29)) is False
    assert trader._in_buy_window(d.replace(hour=13, minute=1)) is False
    assert trader._in_buy_window(d.replace(hour=9, minute=30) + timedelta(minutes=window)) is False


def test_next_buy_window_start(trader):
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


def test_buy_expiry_survives_to_next_open(trader):
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
    assert trader.engine._stop_distance("600621") >= 0.08


def test_stop_distance_respects_cap(trader, monkeypatch):
    import pandas as pd

    from app.data.cache import kline_cache

    # 构造一个波动极大的票：ATR≈8 元 / 收盘 100 元 → 2×ATR = 16%
    df = pd.DataFrame({
        "high": [108.0] * 30, "low": [100.0] * 30, "close": [100.0] * 30,
    })
    monkeypatch.setattr(kline_cache, "get", lambda code: df)
    dist = trader.engine._stop_distance("600621")
    cap = get_settings().paper_atr_stop_max
    assert 0.08 <= dist <= max(cap, 0.08)
    assert dist == pytest.approx(0.16, abs=0.02)


def test_pool_rejects_out_of_top_n(trader, monkeypatch):
    monkeypatch.setattr(trader.engine, "_top_amount_codes", lambda: {"600621"})
    ok, why = trader.engine._buy_allowed("600621", "华鑫股份")
    assert ok, why
    ok, why = trader.engine._buy_allowed("301487", "盟固利")
    assert not ok and "成交额" in why
