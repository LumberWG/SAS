"""内存快照存储（SWR + 刷新去重）。

实时快照数据量大（5000+ 行）且更新频繁，不落 SQLite，
只在进程内缓存，多接口/定时任务共享，避免重复请求数据源。

刷新策略（stale-while-revalidate + 防惊群）:
  全量刷新一次约 90 秒（59 批顺序请求），远大于 TTL，若让页面请求
  同步等待刷新，前端 30 秒超时等不起。采用 SWR 模式:
    1. TTL 内有缓存   → 直接返回新鲜数据；
    2. TTL 外有旧缓存 → 立即返回旧数据（页面永不等待），
                        同时后台单线程刷新（去重）；
    3. 完全无缓存     → 当前线程刷新（首次冷启动），
                        并发请求单刷新者 + 多等待者（带超时，
                        超时抛 TimeoutError 由上层降级）。
  调度器每分钟 force 刷新一次，保证盘中数据持续更新。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.data.akshare_source import DataSourceError, get_data_source

logger = logging.getLogger("sas.snapshot")

# 持久化默认目录（与 kline 缓存分离）：重启/数据源故障后保留最近一次成功快照
_CACHE_DIR = Path(__file__).resolve().parent / "cache"

#: 防残缺快照覆盖：旧快照至少这么多行时才做数量对比校验
_SNAPSHOT_MIN_ROWS = 1000
#: 新快照行数低于旧快照的这个比例即判为残缺（分批抓取部分失败）
_SNAPSHOT_MIN_KEEP_RATIO = 0.6


class SnapshotStore:
    """全市场快照与指数快照的内存缓存。"""

    def __init__(
        self,
        ttl: float = 30.0,
        fail_cooldown: float = 60.0,
        cache_dir: str | None = None,
    ) -> None:
        self._ttl = ttl
        #: 刷新失败后的冷却时长（秒）：防止数据源挂掉时每个请求
        #: 都重复走一遍降级链（慢且刷日志）
        self._fail_cooldown = fail_cooldown
        self._cond = threading.Condition()
        self._snapshot: pd.DataFrame | None = None
        self._snapshot_at: float = 0.0
        self._indices: pd.DataFrame | None = None
        self._indices_at: float = 0.0
        self._refreshing_keys: set[str] = set()  # 各 key 独立刷新标志，避免 indices/snapshot 互相阻塞
        self._failed_at: dict[str, float] = {}  # key → 最近失败时间
        self._cache_dir = Path(cache_dir) if cache_dir is not None else _CACHE_DIR
        self._snapshot_file = self._cache_dir / "snapshot_latest.parquet"
        self._indices_file = self._cache_dir / "indices_latest.parquet"
        self._load_persisted()

    # ------------------------------------------------------------------
    # 持久化：重启/数据源故障后仍有最近一次成功数据，避免首页空白
    # ------------------------------------------------------------------
    def _load_persisted(self) -> None:
        """启动时加载上次成功的快照/指数（若有）。

        加载后视为「已过期」，触发 SWR 后台刷新（立即返回旧数据，后台补新）。
        """
        for path, attr, at_attr in (
            (self._indices_file, "_indices", "_indices_at"),
            (self._snapshot_file, "_snapshot", "_snapshot_at"),
        ):
            try:
                if path.exists():
                    df = pd.read_parquet(path)
                    if df is not None and not df.empty:
                        setattr(self, attr, df)
                        # 视为已过期 → 首次请求触发 SWR 后台刷新（立即返回旧）
                        setattr(self, at_attr, time.monotonic() - self._ttl - 1)
                        logger.info("已加载持久化 %s（%d 行）", attr, len(df))
            except Exception:  # noqa: BLE001
                logger.exception("加载持久化 %s 失败", attr)

    def _persist(self, path: Path, df: pd.DataFrame) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path)
        except Exception:  # noqa: BLE001
            logger.exception("持久化 %s 失败", path)

    # ------------------------------------------------------------------
    # 通用刷新逻辑（SWR + 单刷新者 + 多等待者 + 失败冷却）
    # ------------------------------------------------------------------
    def _get_with_refresh(
        self,
        key: str,
        get_cached,
        get_cached_at,
        fetcher,
        force: bool,
        wait_timeout: float,
        store,
    ) -> pd.DataFrame:
        """SWR 刷新逻辑。

        Returns:
            新鲜缓存（TTL 内）或旧缓存（后台刷新中）；
            完全无缓存时同步刷新（可能等待其他刷新者完成）。

        Raises:
            DataSourceError: 无缓存且最近刷新失败（冷却期内），
                避免重复触发降级链，由上层降级处理。
        """
        deadline = time.monotonic() + wait_timeout
        with self._cond:
            cached = get_cached()
            cached_at = get_cached_at()
            fresh = (
                cached is not None
                and (time.monotonic() - cached_at) < self._ttl
            )
            if fresh and not force:
                return cached  # type: ignore[return-value]

            # 失败冷却：无缓存且最近刷新失败过 → 直接报错（不重复触发）
            failed_at = self._failed_at.get(key, 0.0)
            if (
                cached is None
                and not force
                and (time.monotonic() - failed_at) < self._fail_cooldown
            ):
                raise DataSourceError(f"数据源不可用（{key} 刷新失败冷却中）")

            if key not in self._refreshing_keys:
                # 成为刷新者：有旧缓存 → 后台线程刷新（SWR，页面不等待）；
                # 无旧缓存 → 当前线程同步刷新（首次冷启动）。
                self._refreshing_keys.add(key)
                refresher = True
                background = cached is not None
            else:
                # 已有线程在刷新：锁内等待其完成（被唤醒后重读缓存）
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("快照刷新等待超时")
                    self._cond.wait(remaining)
                    cached = get_cached()
                    if cached is not None:
                        return cached  # type: ignore[return-value]

        if refresher and background:
            # SWR：立即返回旧数据，后台刷新
            self._spawn_background_refresh(key, fetcher, store)
            return cached  # type: ignore[return-value]

        # 冷启动同步刷新（无旧缓存，必须等待数据可用）
        try:
            df = fetcher()
            with self._cond:
                store(df)
                self._failed_at.pop(key, None)  # 成功则清除失败冷却
                self._cond.notify_all()
            return df
        except Exception:
            with self._cond:
                self._failed_at[key] = time.monotonic()
            raise
        finally:
            with self._cond:
                self._refreshing_keys.discard(key)

    def _spawn_background_refresh(self, key: str, fetcher, store) -> None:
        """后台线程执行刷新（刷新标志由请求线程持有，线程结束后释放）。"""

        def run() -> None:
            try:
                df = fetcher()
                with self._cond:
                    store(df)
                    self._failed_at.pop(key, None)
                    self._cond.notify_all()
            except Exception:  # noqa: BLE001 - 后台刷新失败静默（旧缓存仍可用）
                logger.exception("后台快照刷新失败 %s", key)
                with self._cond:
                    self._failed_at[key] = time.monotonic()
            finally:
                with self._cond:
                    self._refreshing_keys.discard(key)

        threading.Thread(target=run, daemon=True, name="snapshot-refresh").start()

    # ------------------------------------------------------------------
    def peek_snapshot(self) -> pd.DataFrame | None:
        """只读内存快照（不触发刷新、不等待）。

        用于合并类场景（如自选股行情）：有缓存就用、没有就跳过，
        绝不为它触发全市场冷启动刷新（避免列表接口被拖慢）。
        返回缓存引用（可能为 None），调用方需自行容错缺失列。
        """
        with self._cond:
            return self._snapshot

    def peek_indices(self) -> pd.DataFrame | None:
        """只读内存指数快照（不触发刷新、不等待）。"""
        with self._cond:
            return self._indices

    # ------------------------------------------------------------------
    def get_snapshot(
        self, force: bool = False, wait_timeout: float = 15.0
    ) -> pd.DataFrame:
        """全市场实时快照（SWR：旧数据立即可用，后台刷新）。"""

        def store(df: pd.DataFrame) -> None:
            # 降级源（新浪批量）是分批抓取的，部分批次失败会返回大幅缩水的
            # 结果。这里拒绝用这种残缺数据覆盖已有完整快照，否则
            # 「成交额前 N」等依赖快照的功能会突然只覆盖很少的股票。
            prev = self._snapshot
            if (
                prev is not None
                and len(prev) >= _SNAPSHOT_MIN_ROWS
                and len(df) < len(prev) * _SNAPSHOT_MIN_KEEP_RATIO
            ):
                logger.warning(
                    "快照数据量异常（%d → %d 条），保留旧快照不覆盖",
                    len(prev), len(df),
                )
                self._snapshot_at = time.monotonic()  # 避免每个请求都重试刷新
                return
            self._snapshot = df
            self._snapshot_at = time.monotonic()
            self._persist(self._snapshot_file, df)

        return self._get_with_refresh(
            "snapshot",
            lambda: self._snapshot,
            lambda: self._snapshot_at,
            get_data_source().get_realtime_snapshot,
            force, wait_timeout, store,
        )

    def get_indices(
        self, force: bool = False, wait_timeout: float = 10.0
    ) -> pd.DataFrame:
        """主要指数实时快照（SWR：旧数据立即可用，后台刷新）。"""

        def store(df: pd.DataFrame) -> None:
            self._indices = df
            self._indices_at = time.monotonic()
            self._persist(self._indices_file, df)

        return self._get_with_refresh(
            "indices",
            lambda: self._indices,
            lambda: self._indices_at,
            get_data_source().get_index_snapshot,
            force, wait_timeout, store,
        )


# 全局单例（TTL 用独立的 snapshot_ttl 配置，与请求超时解耦）
snapshot_store = SnapshotStore(ttl=get_settings().snapshot_ttl)
