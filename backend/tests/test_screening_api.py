"""选股 API 测试：策略列表、启动扫描、状态轮询、范围过滤、历史信号。

启动扫描用替身替换 ``scanner.start_scan``，避免真实全市场扫描（耗时长、依赖数据源）；
重点验证 HTTP 层的路由、请求校验与响应结构（#136 补测：选股 API 此前无直接单测）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.screener.scanner import ScanTask, scanner
from app.strategies import registry


@pytest.fixture()
def client(monkeypatch):
    """替身：start_scan 直接返回一个预置的已完成任务，不真正扫描。

    同时禁用 lifespan 里的调度器启动与股票池预热网络拉取，避免测试触发真实扫描。
    """
    import pandas as pd

    async def fake_start_scan(strategy_names, codes=None, scope="all"):
        tid = "apitest-task-1"
        scanner._tasks[tid] = ScanTask(
            task_id=tid, strategies=list(strategy_names), status="done",
            scope=scope, total=1, scanned=1,
            hits=[{"code": "600001", "name": "测试", "strategy": "X",
                   "price": 10.0, "rank": 1, "extra": {"score": 5.0}}],
        )
        return tid

    monkeypatch.setattr(
        "app.screener.scanner.scanner.start_scan", fake_start_scan
    )
    # 关掉 lifespan 的调度器与预热网络
    monkeypatch.setattr("app.monitor.scheduler.scheduler.start", lambda: None)
    monkeypatch.setattr("app.monitor.scheduler.scheduler.shutdown", lambda: None)
    monkeypatch.setattr(
        "app.data.service.get_stock_pool",
        lambda: pd.DataFrame({"code": [], "name": []}),
    )
    scanner._tasks.clear()
    from app.main import app

    with TestClient(app) as c:
        yield c
    scanner._tasks.clear()


class TestScreeningAPI:
    def test_list_strategies(self, client):
        r = client.get("/api/screening/strategies")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["strategies"], list) and body["strategies"]
        assert isinstance(body["scopes"], list) and body["scopes"]
        assert "today_hits" in body

    def test_start_scan_success(self, client):
        name = registry.names()[0]
        r = client.post(
            "/api/screening/scan",
            json={"strategies": [name], "codes": ["600001"]},
        )
        assert r.status_code == 200
        tid = r.json()["task_id"]
        assert tid == "apitest-task-1"
        # 轮询该任务状态
        r2 = client.get(f"/api/screening/scan/{tid}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "done"
        assert r2.json()["hit_count"] == 1

    def test_start_scan_unknown_strategy(self, client):
        r = client.post(
            "/api/screening/scan",
            json={"strategies": ["不存在的策略"]},
        )
        assert r.status_code == 400

    def test_start_scan_empty_strategies(self, client):
        r = client.post("/api/screening/scan", json={"strategies": []})
        assert r.status_code == 422

    def test_scan_status_404(self, client):
        r = client.get("/api/screening/scan/does-not-exist")
        assert r.status_code == 404

    def test_latest_scan_structure(self, client):
        r = client.get("/api/screening/scan/latest")
        assert r.status_code == 200
        assert "task" in r.json()

    def test_signals_endpoint(self, client):
        r = client.get("/api/screening/signals", params={"limit": 10})
        assert r.status_code == 200
        assert "signals" in r.json()
