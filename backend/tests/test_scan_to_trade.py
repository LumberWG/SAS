"""选股→交易一体化测试（#选股交易一体）。

验证「扫描命中 → 自动分析 → 进入实时交易队列 → 成交溯源」整条链路把
「选股来源」结构化透传：

  - ``_trade_top_hits`` 把命中策略 + 扫描任务 id 注入推荐对象（``_scan``），
    并把 ``reason`` 前缀标记为「选股命中·策略X」
  - ``live_trader.submit`` 把 ``_scan`` 透传到 ``PendingSignal.scan``
  - ``engine._build_provenance`` 在选股来源下生成 ``source=scan`` + ``scan``
    子对象，``execute_buy`` 据此打上 ``trigger=scan_buy``

全部用 monkeypatch 替换网络/数据依赖，不触达真实行情。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.analysis import recommender
from app.data.cache import Database
from app.paper import engine as engine_mod
from app.paper.live import live_trader
import app.screener.scanner as scanner_mod
from app.screener.scanner import scanner


@pytest.fixture()
def scan_db(tmp_path, monkeypatch):
    """把扫描器内部的全局 db 指向临时库，避免污染真实数据。"""
    db = Database(tmp_path / "scan_trade.db")
    monkeypatch.setattr("app.screener.scanner.db", db)
    return db


@pytest.fixture(autouse=True)
def _clear_state():
    """每个用例隔离扫描任务与实时队列，避免跨用例污染。"""
    scanner._tasks.clear()
    live_trader._pending.clear()
    yield
    scanner._tasks.clear()
    live_trader._pending.clear()


# ----------------------------------------------------------------------
# 成交溯源：_build_provenance 在选股来源下生成 source=scan
# ----------------------------------------------------------------------
def test_build_provenance_scan_source():
    eng = SimpleNamespace(settings=SimpleNamespace())
    prov = json.loads(
        engine_mod.PaperEngine._build_provenance(
            eng,
            trigger="scan_buy",
            reason="r",
            scan={"task_id": "scan-1", "strategy": "均线突破"},
        )
    )
    assert prov["source"] == "scan"
    assert prov["scan"] == {"task_id": "scan-1", "strategy": "均线突破"}

    # 非选股来源 → analysis
    prov2 = json.loads(
        engine_mod.PaperEngine._build_provenance(eng, trigger="fresh_buy", reason="r")
    )
    assert prov2["source"] == "analysis"
    assert prov2["scan"] is None

    # 手动建仓 → manual
    prov3 = json.loads(
        engine_mod.PaperEngine._build_provenance(eng, trigger="manual", reason="r")
    )
    assert prov3["source"] == "manual"


# ----------------------------------------------------------------------
# 队列透传：submit 把推荐的 _scan 标记写入 PendingSignal.scan
# ----------------------------------------------------------------------
def test_submit_passes_scan_meta(monkeypatch):
    live_trader._pending.clear()
    settings = live_trader.settings
    monkeypatch.setattr(settings, "paper_live_enabled", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade_overnight", True)
    monkeypatch.setattr(live_trader, "auto_trade_enabled", lambda: True)

    rec = SimpleNamespace(
        code="600001",
        name="测试股",
        action="buy",
        reason="选股命中·均线突破｜综合评分 +20",
        price=10.0,
        score=60.0,
        _scan={"task_id": "scan-1", "strategy": "均线突破"},
    )
    n = live_trader.submit([rec], source="选股命中")
    assert n == 1
    sig = live_trader._pending["600001"]
    assert sig.scan == {"task_id": "scan-1", "strategy": "均线突破"}


# ----------------------------------------------------------------------
# 端到端接缝：_trade_top_hits 把扫描命中打上选股来源，并送入实时队列
# ----------------------------------------------------------------------
def test_trade_top_hits_tags_scan_source(scan_db, monkeypatch):
    live_trader._pending.clear()
    settings = live_trader.settings
    monkeypatch.setattr(settings, "paper_live_enabled", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade_overnight", True)
    monkeypatch.setattr(live_trader, "auto_trade_enabled", lambda: True)
    monkeypatch.setattr(scanner.settings, "paper_scan_autotrade", True)
    # 盘中：直接成交，不走隔夜顺延（_trade_top_hits 内 from...import，需 mock 源模块）
    monkeypatch.setattr("app.market.clock.is_trading_hours", lambda: True)
    # 避免自动自选 / 任务落盘等旁支依赖
    monkeypatch.setattr(scanner, "_auto_watch_hits", lambda *a, **k: 0)
    monkeypatch.setattr(scanner, "_persist_task", lambda *a, **k: None)
    monkeypatch.setattr("app.analysis.recommender.market_sentiment", lambda: {})

    async def fake_analyze(code, name, price, sentiment):
        return SimpleNamespace(
            code=code,
            name=name,
            action="buy",
            score=60.0,
            reason="综合评分 +20",
            price=10.0,
            stop_loss=None,
            target_price=None,
            sentiment="",
            rank=0,
            detail="[]",
        )

    monkeypatch.setattr(recommender.recommender, "analyze_stock", fake_analyze)

    hits = [
        {
            "code": "600001",
            "name": "测试1",
            "strategy": "均线突破",
            "price": 10.0,
            "extra": {"score": 80.0},
        }
    ]

    asyncio.run(scanner._trade_top_hits(hits, task_id="scan-abc"))

    assert "600001" in live_trader._pending
    sig = live_trader._pending["600001"]
    # 选股来源标记随队列透传
    assert sig.scan == {"task_id": "scan-abc", "strategy": "均线突破"}, sig.scan
    # reason 前缀标明「选股命中」
    assert "选股命中" in sig.reason


# ----------------------------------------------------------------------
# 持久化：scan 标记随撮合队列落库、重启（SASRestart）后回填仍保留
# ----------------------------------------------------------------------
def test_pending_scan_survives_restart(monkeypatch, tmp_path):
    """选股来源（scan）随 pending 落库，重启后从库回填仍保留（#137 续）。"""
    temp_db = Database(tmp_path / "pending_scan.db")
    monkeypatch.setattr("app.paper.live.db", temp_db)

    settings = live_trader.settings
    monkeypatch.setattr(settings, "paper_live_enabled", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade", True)
    monkeypatch.setattr(settings, "paper_scan_autotrade_overnight", True)
    monkeypatch.setattr(live_trader, "auto_trade_enabled", lambda: True)

    rec = SimpleNamespace(
        code="600001",
        name="测试股",
        action="buy",
        reason="选股命中·均线突破｜综合评分 +20",
        price=10.0,
        score=60.0,
        _scan={"task_id": "scan-1", "strategy": "均线突破"},
    )
    assert live_trader.submit([rec], source="选股命中") == 1
    assert live_trader._pending["600001"].scan == {"task_id": "scan-1", "strategy": "均线突破"}

    # 落库后清空内存（模拟重启）
    live_trader._persist_pending()
    live_trader._pending.clear()
    assert "600001" not in live_trader._pending
    # 重启回填
    live_trader._restore_pending()
    assert "600001" in live_trader._pending
    assert live_trader._pending["600001"].scan == {
        "task_id": "scan-1",
        "strategy": "均线突破",
    }
