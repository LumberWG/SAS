"""内存快照存储（带 TTL 与刷新去重）。

实时快照数据量大（5000+ 行）且更新频繁，不落 SQLite，
只在进程内缓存，多接口/定时任务共享，避免重复请求数据源。

刷新去重（防惊群）:
  全量刷新一次约需 90 秒（59 批顺序请求）。若不加保护，
  TTL 过期瞬间的多个并发请求会各自触发一次刷新，互相拖慢。
  这里用 Condition 实现「单刷新者 + 多等待者」:
    - 第一个线程负责刷新，其余线程等待其完成（带超时）；
    - 等待者被唤醒后重新检查缓存新鲜度，避免事件竞态；
    - 等待者超时后抛 TimeoutError，由上层降级（如自选股列表
      退化为无行情静态列表），避免接口被拖死。
"""
from __future__ import annotations

import threading
import time

import pandas as pd

from app.config import get_settings
from app.data.akshare_source import get_data_source


class SnapshotStore:
    """全市场快照与指数快照的内存缓存。"""

    def __init__(self, ttl: float = 30.0) -> None:
        self._ttl = ttl
        self._cond = threading.Condition()
        self._snapshot: pd.DataFrame | None = None
        self._snapshot_at: float = 0.0
        self._indices: pd.DataFrame | None = None
        self._indices_at: float = 0.0
        self._refreshing = False

    # ------------------------------------------------------------------
    # 通用刷新逻辑（单刷新者 + 多等待者，Condition 防竞态）
    # ------------------------------------------------------------------
    def _get_with_refresh(
        self,
        get_cached,
        get_cached_at,
        fetcher,
        force: bool,
        wait_timeout: float,
        store,
    ) -> pd.DataFrame:
        """通用刷新逻辑。

        Args:
            get_cached: 读取当前缓存（lambda，刷新后重读防竞态）
            get_cached_at: 读取缓存时间戳
            store: 刷新成功后写回缓存的回调（区分快照/指数字段）
        """
        deadline = time.monotonic() + wait_timeout
        with self._cond:
            while True:
                cached = get_cached()
                cached_at = get_cached_at()
                fresh = (
                    cached is not None
                    and (time.monotonic() - cached_at) < self._ttl
                )
                if fresh and not force:
                    return cached  # type: ignore[return-value]
                if not self._refreshing:
                    self._refreshing = True
                    refresher = True
                    break
                # 已有线程在刷新：等待其完成（被唤醒后重新检查缓存）
                refresher = False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("快照刷新等待超时")
                self._cond.wait(remaining)

        if refresher:
            try:
                df = fetcher()
                with self._cond:
                    store(df)
                    self._cond.notify_all()
                return df
            finally:
                with self._cond:
                    self._refreshing = False
        # 非刷新者走到这里说明缓存已被其他线程刷新（loop 内已 return）
        raise TimeoutError("快照刷新等待超时")

    # ------------------------------------------------------------------
    def get_snapshot(
        self, force: bool = False, wait_timeout: float = 15.0
    ) -> pd.DataFrame:
        """全市场实时快照（TTL 内返回缓存；刷新中去重等待）。"""

        def store(df: pd.DataFrame) -> None:
            self._snapshot = df
            self._snapshot_at = time.monotonic()

        return self._get_with_refresh(
            lambda: self._snapshot,
            lambda: self._snapshot_at,
            get_data_source().get_realtime_snapshot,
            force, wait_timeout, store,
        )

    def get_indices(
        self, force: bool = False, wait_timeout: float = 10.0
    ) -> pd.DataFrame:
        """主要指数实时快照（TTL 内返回缓存）。"""

        def store(df: pd.DataFrame) -> None:
            self._indices = df
            self._indices_at = time.monotonic()

        return self._get_with_refresh(
            lambda: self._indices,
            lambda: self._indices_at,
            get_data_source().get_index_snapshot,
            force, wait_timeout, store,
        )


# 全局单例（TTL 用独立的 snapshot_ttl 配置，与请求超时解耦）
snapshot_store = SnapshotStore(ttl=get_settings().snapshot_ttl)
