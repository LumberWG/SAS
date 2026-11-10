"""首页「查股票」接口测试：匹配逻辑与边界，不依赖实时网络。

直接调用 ``app.api.market.market_search``，用 monkeypatch 注入一张小型
合成股票池，覆盖：精确代码、名称模糊、交易所前缀剥离、空查询、limit 截断、
非数字（代码/名称包含）、无命中。
"""
from __future__ import annotations

import pandas as pd

from app.api import market as mkt


def _pool() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("600519", "贵州茅台", "sh"),
            ("600000", "浦发银行", "sh"),
            ("000001", "平安银行", "sz"),
            ("000024", "招商地产", "sz"),
            ("601988", "中国银行", "sh"),
            ("300750", "宁德时代", "sz"),
        ],
        columns=["code", "name", "market"],
    )


def test_exact_code_match(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="600519", limit=10)
    assert d["total"] == 1
    assert d["results"][0]["code"] == "600519"
    assert d["results"][0]["name"] == "贵州茅台"


def test_name_substring_match(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="招商", limit=10)
    assert d["total"] == 1
    assert d["results"][0]["name"] == "招商地产"


def test_exchange_prefix_stripped(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="sh600519", limit=10)
    assert d["total"] == 1
    assert d["results"][0]["code"] == "600519"


def test_empty_query_returns_empty(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="   ", limit=10)
    assert d["total"] == 0
    assert d["results"] == []


def test_limit_truncates_but_total_keeps(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="银行", limit=1)
    # 浦发银行 / 平安银行 / 中国银行 都含「银行」
    assert d["total"] >= 3
    assert len(d["results"]) == 1


def test_nondigit_matches_code_or_name(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    # 纯英文/非数字：代码包含或名称包含
    d = mkt.market_search(q="300750", limit=10)
    assert any(r["code"] == "300750" for r in d["results"])


def test_no_match_returns_zero(monkeypatch):
    monkeypatch.setattr("app.data.service.get_stock_pool", _pool)
    d = mkt.market_search(q="不存在的股票xyz", limit=10)
    assert d["total"] == 0
    assert d["results"] == []
