"""做T引擎单元测试：涨停卖半 / 倒T / 下跌加仓 / 反弹减仓 + 严格 T+1 + 大盘护栏。

全程使用临时 SQLite（monkeypatch app.paper.engine.db）与 mock 实时快照，
不触碰真实模拟盘数据。
"""
import asyncio
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.paper import engine as engine_module
from app.paper.engine import PaperEngine
from app.data.cache import Database


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _snap(open_, high, low, prev_close, price):
    return pd.DataFrame([{
        "code": "300001", "open": open_, "high": high, "low": low,
        "prev_close": prev_close, "price": price,
    }])


@pytest.fixture
def eng(monkeypatch, tmp_path):
    new_db = Database(tmp_path / "paper_t_test.db")
    # engine 通过 `from app.data.cache import db` 持有单例引用，必须 patch 模块属性
    monkeypatch.setattr(engine_module, "db", new_db)
    from app.data.snapshot_store import snapshot_store

    monkeypatch.setattr(
        snapshot_store, "peek_snapshot",
        lambda: _snap(10.0, 11.0, 9.5, 10.0, 10.5),
    )
    monkeypatch.setattr(
        snapshot_store, "peek_indices",
        lambda: pd.DataFrame([{"code": "000001", "pct_change": 0.0}]),
    )
    e = PaperEngine()
    # 布尔开关开，数值参数给真实阈值（不能都设 True，否则阈值计算失效）
    monkeypatch.setattr(e.settings, "paper_t_active", True, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_limit_sell_half", True, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_size", 0.25, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_up_sell", 0.05, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_up_buyback", 0.03, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_down_add", 0.05, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_down_full", 0.07, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_rebound_sell", 0.02, raising=False)
    monkeypatch.setattr(e.settings, "paper_t_crash_guard", 0.03, raising=False)
    e.ensure_account()
    return e


def _seed(db, shares=1000, cost=10.0, buy_date=None, tag="base"):
    buy_date = buy_date or _yesterday()
    db.add_paper_lot("300001", "测试股", shares, cost, buy_date, "seed",
                     today=buy_date, tag=tag)


def test_disabled_by_default(monkeypatch, tmp_path):
    """开关默认关：t_tick 直接返回空，不动盘。

    断言的是 **Settings 的字段默认值**，不是运行时的 ``e.settings``——
    后者会被 ``app_state`` 覆盖层与 ``.env`` 改掉（线上 ``paper_t_active``
    曾被运行时打开），拿它断言会让「默认值」这个用例随运行时配置漂移。
    """
    from app.config import Settings

    assert Settings.model_fields["paper_t_active"].default is False

    new_db = Database(tmp_path / "t_off.db")
    monkeypatch.setattr(engine_module, "db", new_db)
    from app.data.snapshot_store import snapshot_store
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10, 11, 9, 10, 11))
    e = PaperEngine()
    monkeypatch.setattr(e.settings, "paper_t_active", False, raising=False)
    assert asyncio.run(e.t_tick({"300001": 11.0})) == []


def test_limit_sell_half(monkeypatch, eng):
    _seed(engine_module.db, 1000)
    from app.data.snapshot_store import snapshot_store
    # 涨停：high=11 ≥ prev_close 10*1.095=10.95
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.0, 11.0, 9.8, 10.0, 11.0))
    trades = asyncio.run(eng.t_tick({"300001": 11.0}))
    assert any(t.reason == "涨停卖半" for t in trades)
    pos = engine_module.db.list_paper_positions()[0]
    assert int(pos["shares"]) == 500          # 卖 1/2 可卖
    assert int(pos["t_t_sold"]) == 500        # 待买回


def test_reverse_t(monkeypatch, eng):
    _seed(engine_module.db, 1000)
    from app.data.snapshot_store import snapshot_store
    # 开盘涨(10.1>10) 且盘中 high 触 open*1.05=10.605
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.1, 10.7, 9.9, 10.0, 10.5))
    trades = asyncio.run(eng.t_tick({"300001": 10.5}))
    assert any(t.reason == "做T卖出" for t in trades)
    pos = engine_module.db.list_paper_positions()[0]
    assert int(pos["t_t_sold"]) == 200         # 1000*0.25 向下取整到整手


def test_down_add(monkeypatch, eng):
    _seed(engine_module.db, 1000)
    from app.data.snapshot_store import snapshot_store
    # 开盘跌(9.9<10) 且盘中 low 触 open*(1-0.05)=9.405
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(9.9, 9.95, 9.4, 10.0, 9.5))
    cash_before = engine_module.db.get_paper_account()["cash"]
    trades = asyncio.run(eng.t_tick({"300001": 9.5}))
    assert any(t.reason == "做T加仓" for t in trades)
    pos = engine_module.db.list_paper_positions()[0]
    assert int(pos["shares"]) > 1000           # 加仓（今仓）
    assert int(pos["t_down_armed"]) == 1
    assert engine_module.db.get_paper_account()["cash"] < cash_before


def test_rebound_sell(monkeypatch, eng):
    _seed(engine_module.db, 1000, tag="base")
    # 模拟已加仓且隔夜（t_add 批次 buy_date=昨天，可卖）
    engine_module.db.add_paper_lot("300001", "测试股", 500, 9.5, _yesterday(),
                                  "做T加仓", today=_yesterday(), tag="t_add")
    engine_module.db.update_paper_t_state("300001", t_down_armed=1)
    from app.data.snapshot_store import snapshot_store
    # 反弹：price 9.9 ≥ prev_close*(1-0.02)=9.8
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(9.6, 9.7, 9.5, 10.0, 9.9))
    trades = asyncio.run(eng.t_tick({"300001": 9.9}))
    assert any(t.reason == "反弹减仓" for t in trades)
    lots = engine_module.db.list_paper_lots("300001")
    assert all(l["tag"] != "t_add" for l in lots)   # 只减加仓批次
    assert any(l["tag"] == "base" for l in lots)     # 底仓不动


def test_t1_only_sellable(monkeypatch, tmp_path):
    """严格 T+1：今仓不可卖，昨仓可卖。"""
    new_db = Database(tmp_path / "t_t1.db")
    new_db.init_paper_account(100000)
    t = _today()
    new_db.add_paper_lot("300001", "X", 1000, 10.0, t, "seed", today=t, tag="base")
    assert new_db.reduce_paper_sellable("300001", 500, t) == 0   # 今仓不可卖
    new_db.add_paper_lot("300001", "X", 1000, 10.0, _yesterday(), "seed",
                         today=_yesterday(), tag="base")
    assert new_db.reduce_paper_sellable("300001", 500, t) == 500  # 昨仓可卖


def test_crash_guard_pauses(monkeypatch, eng):
    _seed(engine_module.db, 1000)
    from app.data.snapshot_store import snapshot_store
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.0, 11.0, 9.8, 10.0, 11.0))   # 涨停
    monkeypatch.setattr(snapshot_store, "peek_indices",
                        lambda: pd.DataFrame([{"code": "000001", "pct_change": -5.0}]))
    trades = asyncio.run(eng.t_tick({"300001": 11.0}))
    assert not any(t.reason == "涨停卖半" for t in trades)  # 大盘暴跌护栏暂停


def test_crash_defers_buyback_until_next_open(monkeypatch, eng):
    """当日 crash 暂停买回：待买回跨日保留，次日开盘恢复底仓（与回测口径一致）。"""
    db = engine_module.db
    from app.data.snapshot_store import snapshot_store
    _seed(db, 1000)

    # ① 非 crash：涨停卖半 → 底仓减半，标记「今日待买回」
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.0, 11.0, 9.8, 10.0, 11.0))
    asyncio.run(eng.t_tick({"300001": 11.0}))
    pos = db.list_paper_positions()[0]
    assert int(pos["shares"]) == 500
    assert int(pos["t_t_sold"]) == 500
    assert str(pos["t_sold_date"]) == _today()

    # ② 同日 crash：价格已跌回买回线（10.2 ≤ max(10,10)×1.03），护栏不接刀
    monkeypatch.setattr(snapshot_store, "peek_indices",
                        lambda: pd.DataFrame([{"code": "000001", "pct_change": -5.0}]))
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.0, 11.0, 9.8, 10.0, 10.2))
    trades = asyncio.run(eng.t_tick({"300001": 10.2}))
    assert not any(t.reason == "做T买回" for t in trades)
    pos = db.list_paper_positions()[0]
    assert int(pos["shares"]) == 500          # 底仓仍缺 500，但状态保留（不再丢）
    assert int(pos["t_t_sold"]) == 500

    # ③ 次日（无 crash、价格远高于买回线）：往日待买回 → 直接恢复底仓
    monkeypatch.setattr(snapshot_store, "peek_indices",
                        lambda: pd.DataFrame([{"code": "000001", "pct_change": 0.0}]))
    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(12.0, 12.5, 11.8, 11.0, 12.2))
    db.update_paper_t_state("300001", t_sold_date=_yesterday())
    eng._t_day = ""                           # 模拟跨交易日（触发每日重置分支）
    trades = asyncio.run(eng.t_tick({"300001": 12.2}))
    assert any(t.reason == "做T买回" for t in trades)
    pos = db.list_paper_positions()[0]
    assert int(pos["shares"]) == 1000         # 底仓恢复
    assert int(pos["t_t_sold"]) == 0
    assert not pos["t_sold_date"]             # 状态已清空
    lots = db.list_paper_lots("300001")
    assert any(l["tag"] == "t_buyback" and str(l["buy_date"]) == _today() for l in lots)


def test_no_buyback_when_price_not_pulled_back(monkeypatch, eng):
    """当日未回落（高于买回线）不买回，但次日开盘同样恢复底仓（不做永久减仓）。"""
    db = engine_module.db
    from app.data.snapshot_store import snapshot_store
    _seed(db, 1000)

    monkeypatch.setattr(snapshot_store, "peek_snapshot",
                        lambda: _snap(10.0, 11.0, 9.8, 10.0, 11.0))
    asyncio.run(eng.t_tick({"300001": 11.0}))          # 涨停卖半 500
    trades = asyncio.run(eng.t_tick({"300001": 11.0}))  # 价格未回落
    assert not any(t.reason == "做T买回" for t in trades)
    assert int(db.list_paper_positions()[0]["t_t_sold"]) == 500

    db.update_paper_t_state("300001", t_sold_date=_yesterday())
    eng._t_day = ""
    trades = asyncio.run(eng.t_tick({"300001": 11.5}))
    assert any(t.reason == "做T买回" for t in trades)
    assert int(db.list_paper_positions()[0]["shares"]) == 1000
