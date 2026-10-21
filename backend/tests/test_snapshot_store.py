"""快照存储测试：TTL 缓存与并发刷新去重（防惊群）。"""
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


class TestRefreshDedup:
    def test_concurrent_refresh_single_fetch(self, monkeypatch):
        """并发等待同一缓存过期时，只触发一次全量刷新。"""
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
        # 先填充一次缓存
        store.get_snapshot()
        time.sleep(0.15)  # 令缓存过期
        calls.clear()     # 重置计数，只关注并发刷新阶段

        # 10 个线程并发获取（缓存已过期）
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
