"""扫描器引擎单元测试：自动自选去重/上限、任务落盘与恢复、选股→实时交易路由、
并发复用保护、整轮编排。

不触达真实数据源：通过 monkeypatch 把 ``_scan_stock`` / 行情快照 / 实时行情 / 自选库 /
可交易判定替换成可控替身，覆盖核心编排逻辑（#136 补测：选股核心链路此前无直接单测）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.data.cache import Database
from app.screener.scanner import ScanTask, Signal, scanner


@pytest.fixture()
def scan_db(tmp_path, monkeypatch):
    """把扫描器内部的全局 db 指向临时库，避免污染真实数据。"""
    db = Database(tmp_path / "scan_test.db")
    monkeypatch.setattr("app.screener.scanner.db", db)
    return db


@pytest.fixture()
def tradable_all(monkeypatch):
    """所有股票都判定为可交易（选股范围过滤放行）。"""
    monkeypatch.setattr(
        "app.screener.scanner.is_tradable",
        lambda code, name, **kw: (True, ""),
    )


@pytest.fixture(autouse=True)
def _clear_tasks():
    """每个用例隔离扫描器内存任务，避免跨用例复用/污染。"""
    scanner._tasks.clear()
    yield
    scanner._tasks.clear()


# ----------------------------------------------------------------------
# 自动自选：去重 / 上限 / 范围过滤
# ----------------------------------------------------------------------
class TestAutoWatchHits:
    def _hit(self, code: str, score: float, name: str | None = None) -> dict:
        return {
            "code": code, "name": name or code, "strategy": "S",
            "price": 10.0, "extra": {"score": score},
        }

    def test_top_n_by_score(self, scan_db, tradable_all, monkeypatch):
        s = get_settings()
        s.scan_auto_watch_limit = 3
        s.watch_auto_max = 9
        added = scanner._auto_watch_hits([
            self._hit("600001", 5.0), self._hit("600002", 4.0),
            self._hit("600003", 3.0), self._hit("600004", 2.0),
            self._hit("600005", 1.0),
        ])
        rows = {r["code"] for r in scan_db.list_watchlist("auto")}
        assert rows == {"600001", "600002", "600003"}
        assert added == 3

    def test_dedup_same_code(self, scan_db, tradable_all, monkeypatch):
        s = get_settings()
        s.scan_auto_watch_limit = 5
        s.watch_auto_max = 10
        added = scanner._auto_watch_hits([
            self._hit("600001", 9.0, "甲"), self._hit("600001", 8.0, "甲"),
        ])
        rows = scan_db.list_watchlist("auto")
        assert len(rows) == 1
        assert rows[0]["code"] == "600001"
        assert added == 1

    def test_out_of_scope_skipped(self, scan_db, monkeypatch):
        s = get_settings()
        s.scan_auto_watch_limit = 5
        s.watch_auto_max = 10
        # 只放行 600001；600688（科创板）判定不可交易
        def fake_tradable(code, name, **kw):
            return (str(code) == "600001", "")
        monkeypatch.setattr("app.screener.scanner.is_tradable", fake_tradable)
        added = scanner._auto_watch_hits([
            self._hit("600001", 9.0), self._hit("600688", 8.0),
        ])
        rows = {r["code"] for r in scan_db.list_watchlist("auto")}
        assert rows == {"600001"}
        assert added == 1

    def test_limit_zero_disabled(self, scan_db, tradable_all, monkeypatch):
        s = get_settings()
        s.scan_auto_watch_limit = 0
        added = scanner._auto_watch_hits([self._hit("600001", 9.0)])
        assert added == 0
        assert scan_db.list_watchlist("auto") == []


# ----------------------------------------------------------------------
# 整轮编排：并发扫描 → 落库 → 自动自选 → 落盘
# ----------------------------------------------------------------------
class TestScanOrchestration:
    @pytest.fixture(autouse=True)
    def _setup(self, scan_db, tradable_all, tmp_path, monkeypatch):
        s = get_settings()
        s.scan_auto_watch_limit = 10
        s.watch_auto_max = 30
        s.paper_scan_autotrade = False  # 编排测试不触发真实下单
        monkeypatch.setattr(scanner, "_state_path", lambda: tmp_path / "last_scan.json")

        async def fake_scan_stock(code, name, strategies, quote):
            # 每只代码返回 1 个命中信号；策略不参与评估（已被替换）
            return [Signal(code=str(code), name=str(name), strategy="X",
                          price=10.0, extra={"score": 5.0})]

        monkeypatch.setattr(scanner, "_scan_stock", fake_scan_stock)
        # 不真正实例化策略（_scan_stock 已被替换，策略不参与评估）
        monkeypatch.setattr(scanner, "_resolve_strategies", lambda names: [])
        # 避免触发真实快照/行情网络拉取
        monkeypatch.setattr(scanner, "_realtime_quotes", lambda: {})
        monkeypatch.setattr(scanner, "_current_sentiment", lambda: {})
        yield

    async def _run_and_wait(self, codes):
        tid = await scanner.start_scan(["X"], codes=codes)
        for _ in range(150):
            t = scanner.get_task(tid)
            if t and t.status in ("done", "error"):
                break
            await asyncio.sleep(0.02)
        return tid

    def test_full_scan_persists_and_autowatches(self, scan_db, tmp_path):
        codes = ["600001", "600002", "600003"]
        tid = asyncio.run(self._run_and_wait(codes))
        task = scanner.get_task(tid)
        assert task is not None
        assert task.status == "done"
        assert task.total == 3
        assert len(task.hits) == 3
        # 信号落库
        assert len(scan_db.list_signals()) == 3
        # 自动自选生效（limit=10 > 3）
        auto = scan_db.list_watchlist("auto")
        assert {r["code"] for r in auto} == set(codes)
        # 任务落盘（重启可恢复）
        assert (tmp_path / "last_scan.json").is_file()
        data = json.loads((tmp_path / "last_scan.json").read_text(encoding="utf-8"))
        assert data["total"] == 3
        assert data["hits"]


# ----------------------------------------------------------------------
# 任务落盘 / 恢复（重启后选股中心列表不丢）
# ----------------------------------------------------------------------
class TestTaskPersist:
    def test_roundtrip(self, tmp_path, monkeypatch):
        state = tmp_path / "last_scan.json"
        monkeypatch.setattr(scanner, "_state_path", lambda: state)
        hits = [{"code": "600001", "name": "甲", "strategy": "S1",
                 "price": 10.0, "extra": {"score": 5}}]
        task = ScanTask(
            task_id="abc123", strategies=["S1"], scope="all",
            total=3, scanned=3, skipped=0,
            started_at="2026-11-09 09:30:00", finished_at="2026-11-09 09:31:00",
            hits=hits,
        )
        scanner._persist_task(task)
        assert state.is_file()
        restored = scanner._restore_task()
        assert restored is not None
        assert restored.task_id == "abc123"
        assert restored.total == 3
        assert restored.hits == hits
        assert restored.scope == "all"

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scanner, "_state_path", lambda: tmp_path / "nope.json")
        assert scanner._restore_task() is None

    def test_corrupted_file_returns_none(self, tmp_path, monkeypatch):
        state = tmp_path / "bad.json"
        state.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(scanner, "_state_path", lambda: state)
        assert scanner._restore_task() is None


# ----------------------------------------------------------------------
# 选股→实时交易 路由（默认开启的强联动，需回归保护）
# ----------------------------------------------------------------------
class TestTradeTopHits:
    @pytest.fixture()
    def fake_submit(self, monkeypatch):
        calls: list = []

        def submit(recos, source=""):
            calls.append((recos, source))
            return len(recos)

        monkeypatch.setattr("app.paper.live.live_trader.submit", submit)
        return calls

    @pytest.fixture()
    def fake_analyze(self, monkeypatch):
        # 注意：analyze_stock 是 async，替身也必须是 async（否则 await 会抛 TypeError 被吞掉）
        async def analyze(code, name, price, sentiment):
            return SimpleNamespace(action="buy", code=code, name=name)

        monkeypatch.setattr(
            "app.analysis.recommender.recommender.analyze_stock", analyze
        )
        monkeypatch.setattr(
            "app.analysis.recommender.market_sentiment", lambda: {}
        )
        return analyze

    def _hit(self, code: str, score: float) -> dict:
        return {"code": code, "name": code, "strategy": "S",
                "price": 10.0, "extra": {"score": score}}

    def test_selects_top_n_distinct(self, fake_submit, fake_analyze, monkeypatch):
        s = get_settings()
        s.paper_scan_autotrade = True
        s.paper_scan_autotrade_overnight = True
        s.paper_scan_autotrade_top_n = 2
        monkeypatch.setattr("app.market.clock.is_trading_hours", lambda: False)
        hits = [self._hit("600001", 9.0), self._hit("600002", 7.0),
                self._hit("600003", 5.0), self._hit("600001", 8.0)]  # 600001 重复
        asyncio.run(scanner._trade_top_hits(hits))
        assert len(fake_submit) == 1
        recos = fake_submit[0][0]
        assert len(recos) == 2
        assert {r.code for r in recos} == {"600001", "600002"}

    def test_disabled_when_setting_off(self, fake_submit, fake_analyze, monkeypatch):
        s = get_settings()
        s.paper_scan_autotrade = False
        asyncio.run(scanner._trade_top_hits([self._hit("600001", 9.0)]))
        assert fake_submit == []

    def test_overnight_gate(self, fake_submit, fake_analyze, monkeypatch):
        s = get_settings()
        s.paper_scan_autotrade = True
        s.paper_scan_autotrade_overnight = False
        monkeypatch.setattr("app.market.clock.is_trading_hours", lambda: False)
        asyncio.run(scanner._trade_top_hits([self._hit("600001", 9.0)]))
        assert fake_submit == []


# ----------------------------------------------------------------------
# 并发复用保护：相同策略集+相同目标只跑一轮
# ----------------------------------------------------------------------
class TestFindReusable:
    def test_reuses_same_target(self, monkeypatch):
        pending = ScanTask(task_id="p1", strategies=["A"], status="running", scope="all")
        scanner._tasks["p1"] = pending
        assert scanner._find_reusable(["A"], None, "all") is pending

    def test_no_reuse_different_scope(self, monkeypatch):
        pending = ScanTask(task_id="p1", strategies=["A"], status="running", scope="all")
        scanner._tasks["p1"] = pending
        assert scanner._find_reusable(["A"], None, "sh") is None

    def test_no_reuse_done_task(self, monkeypatch):
        done = ScanTask(task_id="d1", strategies=["A"], status="done", scope="all")
        scanner._tasks["d1"] = done
        assert scanner._find_reusable(["A"], None, "all") is None
