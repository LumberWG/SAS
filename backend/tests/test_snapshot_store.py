"""快照存储测试：TTL 缓存、SWR 旧数据立即返回、并发刷新去重。"""
import threading
import time

import pandas as pd
import pytest

from app.data.snapshot_store import SnapshotStore


@pytest.fixture()
def store():
    return SnapshotStore(ttl=0.3)  # 短 TTL 便于测试过期逻辑


class TestTTL:
    def test_cache_hit(self, store, monkeypatch):
        calls = []

        def fetcher():
            calls.append(1)
            return pd.DataFrame({"code": ["600519"]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )
        store.get_snapshot()
        store.get_snapshot()
        store.get_snapshot()
        assert len(calls) == 1  # TTL 内只拉一次

    def test_cache_expiry_refetch(self, store, monkeypatch):
        calls = []

        def fetcher():
            calls.append(1)
            return pd.DataFrame({"code": ["600519"]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )
        store.get_snapshot()
        time.sleep(0.4)  # 等 TTL 过期
        store.get_snapshot()
        assert len(calls) == 2

    def test_force_refetch(self, store, monkeypatch):
        calls = []

        def fetcher():
            calls.append(1)
            return pd.DataFrame({"code": ["600519"]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )
        store.get_snapshot(force=True)
        store.get_snapshot(force=True)
        assert len(calls) == 2


class TestSWR:
    def test_stale_returned_immediately_with_background_refresh(self, monkeypatch):
        """TTL 外有旧缓存：立即返回旧数据，后台刷新后缓存更新。"""
        state = {"v": 1}

        def fetcher():
            time.sleep(0.3)  # 模拟慢刷新
            state["v"] += 1
            return pd.DataFrame({"v": [state["v"]]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1)
        first = store.get_snapshot()          # 冷启动同步刷新
        assert first["v"].iloc[0] == 2
        time.sleep(0.15)                       # 缓存过期

        t0 = time.monotonic()
        stale = store.get_snapshot()           # SWR：应立即返回旧数据
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1                    # 关键：不等待刷新
        assert stale["v"].iloc[0] == 2          # 返回的是旧数据

        time.sleep(0.5)                        # 等后台刷新完成
        fresh = store.get_snapshot()
        assert fresh["v"].iloc[0] == 3          # 缓存已被后台刷新更新

    def test_background_refresh_dedup(self, monkeypatch):
        """SWR 期间多个请求只触发一次后台刷新。"""
        calls = []

        def fetcher():
            calls.append(1)
            time.sleep(0.3)
            return pd.DataFrame({"v": [1]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1)
        store.get_snapshot()                   # 冷启动
        calls.clear()
        time.sleep(0.15)                       # 过期

        # 连续 5 次请求（都走 SWR 旧数据路径）
        for _ in range(5):
            store.get_snapshot()
        time.sleep(0.5)                        # 等后台刷新完成
        assert len(calls) == 1                  # 只刷新一次


class TestFailCooldown:
    def test_failed_refresh_not_retriggered_within_cooldown(self, monkeypatch):
        """无缓存时刷新失败：冷却期内直接报错，不重复触发降级链。"""
        calls = []

        def failing_fetcher():
            calls.append(1)
            raise ConnectionError("模拟数据源不可用")

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(failing_fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1, fail_cooldown=60.0)
        for _ in range(3):
            with pytest.raises(Exception):
                store.get_snapshot()
        assert len(calls) == 1  # 冷却期内只真正尝试一次

    def test_stale_cache_returned_during_cooldown(self, monkeypatch):
        """有旧缓存时刷新失败：仍立即返回旧缓存（SWR 不受冷却影响）。"""
        state = {"fail": False}

        def fetcher():
            if state["fail"]:
                raise ConnectionError("模拟失败")
            return pd.DataFrame({"v": [1]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1, fail_cooldown=60.0)
        first = store.get_snapshot()          # 成功，有缓存
        assert first["v"].iloc[0] == 1
        state["fail"] = True
        time.sleep(0.15)                       # 过期
        stale = store.get_snapshot()           # 后台刷新失败，但旧缓存仍可用
        assert stale["v"].iloc[0] == 1


class TestRefreshDedup:
    def test_cold_start_concurrent_single_fetch(self, monkeypatch):
        """冷启动（无缓存）并发请求只触发一次全量刷新。"""
        calls = []

        def slow_fetcher():
            calls.append(1)
            time.sleep(0.5)  # 模拟 90 秒全量刷新的压缩版
            return pd.DataFrame({"code": ["600519"]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(slow_fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1)

        # 10 个线程并发获取（冷启动，无缓存）
        results: list[pd.DataFrame] = []
        errors: list[Exception] = []

        def worker():
            try:
                results.append(store.get_snapshot(wait_timeout=5.0))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(calls) == 1          # 关键：只刷新一次
        assert len(results) == 10       # 所有线程都拿到数据

    def test_wait_timeout_raises(self, monkeypatch):
        """等待超过超时时间抛 TimeoutError（上层据此降级）。"""
        def very_slow_fetcher():
            time.sleep(2.0)
            return pd.DataFrame({"code": ["600519"]})

        monkeypatch.setattr(
            "app.data.snapshot_store.get_data_source",
            lambda: type("S", (), {"get_realtime_snapshot": staticmethod(very_slow_fetcher)})(),
        )

        store = SnapshotStore(ttl=0.1)
        # 后台线程先启动刷新
        threading.Thread(target=lambda: store.get_snapshot()).start()
        time.sleep(0.05)  # 确保刷新者已进入 fetcher
        with pytest.raises(TimeoutError):
            store.get_snapshot(wait_timeout=0.3)
