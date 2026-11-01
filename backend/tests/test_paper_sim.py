"""并行模拟账本：账本隔离、参数覆盖、方案映射。

这里验证的核心是「不会串味」——线上主账本（paper_* 表）与 N 个并行账本
（sim_* 表）必须各自记账、各自受自己的参数约束。串味的后果不是报错，
而是主账本净值被悄悄改掉，看曲线的人根本发现不了。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data.cache import Database, ledger_scope
from app.paper.ledger import OverlaySettings, profile_overrides, sim_specs


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """独立库 + 把各模块里 ``from app.data.cache import db`` 的引用一起换掉。"""
    from app.data import cache as cache_module
    from app.paper import engine as engine_module
    from app.paper import live as live_module

    database = Database(tmp_path / "sim.db")
    monkeypatch.setattr(cache_module, "db", database)
    monkeypatch.setattr(engine_module, "db", database)
    monkeypatch.setattr(live_module, "db", database)
    return database


def test_ledger_scope_keeps_ledgers_isolated(db):
    """同名股票在两个账本各自持仓，互不覆盖。"""
    db.init_paper_account(100000)
    db.add_paper_lot("600004", "白云机场", 1000, 10.0, "2026-10-30", "测试")
    acct = db.ensure_sim_accounts([{
        "tag": "low_exp", "profile": "low_exp", "overrides": {},
        "initial_capital": 50000,
    }])[0]

    with ledger_scope(int(acct["account_id"])):
        db.add_paper_lot("600004", "白云机场", 2000, 10.0, "2026-10-30", "测试")
        assert db.get_paper_account()["initial_capital"] == 50000
        assert db.list_paper_positions()[0]["shares"] == 2000

    assert db.get_paper_account()["initial_capital"] == 100000
    assert db.list_paper_positions()[0]["shares"] == 1000


def test_equity_and_trades_are_per_ledger(db):
    """净值与流水按账本分开写：同一日期两边各一条。"""
    db.init_paper_account(100000)
    db.save_paper_equity("2026-10-30", 101000, 50000, 51000)
    db.insert_paper_trade("600004", "白云机场", "buy", 10.0, 1000, 10000, 5, "测试", "2026-10-30")

    acct = db.ensure_sim_accounts([{
        "tag": "conc5", "profile": "conc5", "overrides": {},
        "initial_capital": 100000,
    }])[0]
    with ledger_scope(int(acct["account_id"])):
        db.save_paper_equity("2026-10-30", 99000, 40000, 59000)
        assert [r["total_asset"] for r in db.list_paper_equity()] == [99000]
        assert db.list_paper_trades() == []

    assert [r["total_asset"] for r in db.list_paper_equity()] == [101000]
    assert len(db.list_paper_trades()) == 1


def test_reset_sim_account_does_not_touch_live(db):
    """重置单个并行账本只清它自己，主账本持仓保留。"""
    db.init_paper_account(100000)
    db.add_paper_lot("600004", "白云机场", 1000, 10.0, "2026-10-30", "测试")
    acct = db.ensure_sim_accounts([{
        "tag": "e30_a15", "profile": "e30_a15",
        "overrides": {"paper_atr_stop_mult": 1.5}, "initial_capital": 100000,
    }])[0]
    with ledger_scope(int(acct["account_id"])):
        db.add_paper_lot("600004", "白云机场", 500, 10.0, "2026-10-30", "测试")

    db.reset_sim_account("e30_a15", 100000)

    with ledger_scope(int(acct["account_id"])):
        assert db.list_paper_positions() == []
        assert db.list_paper_lots() == []
        assert db.get_paper_account()["cash"] == 100000
    assert len(db.list_paper_positions()) == 1
    assert db.list_paper_lots()[0]["shares"] == 1000


def test_overlay_settings_falls_back_to_base():
    base = SimpleNamespace(paper_max_positions=10, paper_stop_loss=0.08)
    overlay = OverlaySettings(base, {"paper_max_positions": 2})
    assert overlay.paper_max_positions == 2
    assert overlay.paper_stop_loss == 0.08
    assert overlay.overrides() == {"paper_max_positions": 2}


def test_profile_mapping_keeps_disposal_params():
    overrides, skipped = profile_overrides("e30_a15")
    assert overrides == {"paper_atr_stop_mult": 1.5}
    assert skipped == []

    overrides, skipped = profile_overrides("conc5")
    assert overrides == {
        "paper_max_positions": 5,
        "paper_max_total_exposure": 0.60,
    }
    assert skipped == []


def test_selection_side_profiles_are_skipped_not_silently_degraded():
    """含选股侧参数的方案必须被跳过并给出原因，不能跑成一条同名曲线。"""
    settings = SimpleNamespace(
        paper_sim_profiles="default,rev_inv,default_ml,e30_a15",
        paper_sim_capital=0.0,
        paper_initial_capital=100000.0,
    )
    specs, skipped = sim_specs(settings)

    assert [s["tag"] for s in specs] == ["default", "e30_a15"]
    assert all(s["initial_capital"] == 100000.0 for s in specs)
    assert len(skipped) == 2
    assert any("rev_inv" in item and "invert_score" in item for item in skipped)
    assert any("default_ml" in item for item in skipped)


def test_ledger_overview_reports_each_ledger(db):
    """概览接口按账本聚合：净值、收益、持仓数各取各自账本的数据。"""
    from app.paper.ledger import ledger_equity, ledger_overview

    db.init_paper_account(100000)
    db.save_paper_equity("2026-10-30", 100500, 100500, 0)
    db.ensure_sim_accounts([
        {"tag": "default", "profile": "default", "overrides": {}, "initial_capital": 100000},
        {"tag": "conc5", "profile": "conc5",
         "overrides": {"paper_max_positions": 5}, "initial_capital": 50000},
    ])
    for tag, asset in (("default", 101000.0), ("conc5", 48000.0)):
        acct = db.get_sim_account(tag)
        with ledger_scope(int(acct["account_id"])):
            db.save_paper_equity("2026-10-30", asset, asset, 0)

    rows = {r["tag"]: r for r in ledger_overview()}
    assert rows["default"]["return_pct"] == 1.0      # 101000 / 100000 - 1
    assert rows["conc5"]["return_pct"] == -4.0       # 48000 / 50000 - 1
    assert rows["conc5"]["overrides"] == {"paper_max_positions": 5}

    series = ledger_equity()
    assert set(series) == {"default", "conc5"}
    assert series["conc5"][0]["total_asset"] == 48000.0


def test_unknown_profile_is_rejected():
    settings = SimpleNamespace(
        paper_sim_profiles="not_a_profile",
        paper_sim_capital=0.0,
        paper_initial_capital=100000.0,
    )
    specs, skipped = sim_specs(settings)
    assert specs == []
    assert "not_a_profile" in skipped[0]


@pytest.mark.asyncio
async def test_sim_ledger_uses_its_own_position_limit(db, monkeypatch):
    """同一批信号，主账本（10 只）成交 2 只，账本（1 只）只成交 1 只。"""
    from app.config import get_settings
    from app.paper import engine as engine_module
    from app.paper.engine import PaperEngine

    settings = get_settings()
    monkeypatch.setattr(settings, "paper_pool_top_n", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_ma", 0, raising=False)
    monkeypatch.setattr(settings, "paper_regime_weak_pause", False, raising=False)
    monkeypatch.setattr(settings, "paper_max_positions", 10, raising=False)
    monkeypatch.setattr(settings, "paper_initial_capital", 100000.0, raising=False)
    monkeypatch.setattr(engine_module, "large_cap_rule", lambda: (False, 0.0))

    async def fake_price(code, quotes=None):
        return 10.0

    def rec(code: str):
        # 名称必须是像个股票名的中文：`_name_ok` 会把纯数字名判成乱码而拒绝下单
        return SimpleNamespace(
            code=code, name="测试股份", action="buy", reason="测试",
            score=60.0, price=10.0,
        )

    db.init_paper_account(100000)
    acct = db.ensure_sim_accounts([{
        "tag": "conc1", "profile": "conc1",
        "overrides": {"paper_max_positions": 1}, "initial_capital": 100000,
    }])[0]
    prices = {"600004": 10.0, "600005": 10.0}

    main = PaperEngine()
    monkeypatch.setattr(main, "_latest_price", fake_price)
    await main.execute_recommendations([rec("600004"), rec("600005")], prices=prices)
    assert len(db.list_paper_positions()) == 2

    with ledger_scope(int(acct["account_id"])):
        sim = PaperEngine(OverlaySettings(settings, {"paper_max_positions": 1}))
        monkeypatch.setattr(sim, "_latest_price", fake_price)
        await sim.execute_recommendations([rec("600004"), rec("600005")], prices=prices)
        assert len(db.list_paper_positions()) == 1

    assert len(db.list_paper_positions()) == 2
