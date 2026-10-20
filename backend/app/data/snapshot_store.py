"""内存快照存储（带 TTL）。

实时快照数据量大（5000+ 行）且更新频繁，不落 SQLite，
只在进程内缓存，多接口/定时任务共享，避免重复请求数据源。
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from app.config import get_settings
from app.data.akshare_source import get_data_source


class SnapshotStore:
    """全市场快照与指数快照的内存缓存。"""

    def __init__(self, ttl: float = 10.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._snapshot: pd.DataFrame | None = None
        self._snapshot_at: float = 0.0
        self._indices: pd.DataFrame | None = None
        self._indices_at: float = 0.0

    # ------------------------------------------------------------------
    def get_snapshot(self, force: bool = False) -> pd.DataFrame:
        """全市场实时快照（TTL 内返回缓存）。"""
        with self._lock:
            fresh = (
                self._snapshot is not None
                and (time.monotonic() - self._snapshot_at) < self._ttl
            )
            if fresh and not force:
                return self._snapshot  # type: ignore[return-value]
        # 锁外执行网络请求，避免阻塞其他线程
        df = get_data_source().get_realtime_snapshot()
        with self._lock:
            self._snapshot = df
            self._snapshot_at = time.monotonic()
        return df

    def get_indices(self, force: bool = False) -> pd.DataFrame:
        """主要指数实时快照（TTL 内返回缓存）。"""
        with self._lock:
            fresh = (
                self._indices is not None
                and (time.monotonic() - self._indices_at) < self._ttl
            )
            if fresh and not force:
                return self._indices  # type: ignore[return-value]
        df = get_data_source().get_index_snapshot()
        with self._lock:
            self._indices = df
            self._indices_at = time.monotonic()
        return df


# 全局单例
snapshot_store = SnapshotStore(ttl=get_settings().request_timeout)
