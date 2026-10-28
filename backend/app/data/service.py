"""行情数据服务：带本地缓存的K线获取（API 与扫描器共用）。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from app.data.akshare_source import DataSourceError, _market_of, get_data_source
from app.data.cache import kline_cache
from app.data.scope import clean_name

logger = logging.getLogger("sas.data.service")

#: 大盘择时用的指数（上证综指，新浪代码格式）。回测/影子读的缓存文件名是
#: ``idx_sh000001.parquet``，与个股共用同一个 Parquet 目录。
INDEX_SYMBOL = "sh000001"

#: 股票池进程内缓存（秒）。缓存可避免每次扫描/状态查询都走一遍分钟级的
#: 数据源降级链（东财不可达时会拖慢整个后端）。
_POOL_TTL = 3600.0        # 主源结果：股票池按日变化，缓存 1 小时
_POOL_FALLBACK_TTL = 120.0  # 降级快照结果：覆盖可能不全，短缓存以便尽快重试主源
_pool_cache: tuple[float, pd.DataFrame, float] | None = None  # (时刻, 池, TTL)

#: 股票池磁盘缓存：重启后首个请求不必等待 8-15 秒的翻页拉取
_POOL_FILE = Path(__file__).resolve().parent / "cache" / "pool_latest.parquet"
_POOL_FILE_MAX_AGE = 12 * 3600.0
#: 磁盘缓存只在进程内首次取池时用作快速引导；之后 TTL 到期一律回源刷新，
#: 避免进程长期运行时一直吃磁盘里的旧股票池
_pool_bootstrapped = False


def get_stock_pool() -> pd.DataFrame:
    """全市场股票池（code/name/market），带内存缓存、磁盘缓存与多级降级。

    优先级：
      1. 进程内缓存（TTL 内）；
      2. 磁盘缓存（12 小时内），重启后秒开，避免首请求顶到前端 30s 超时；
      3. 东财 push2 列表 → 新浪列表（并行翻页）→ akshare → 快照代码表。
    成功构建后同时写回磁盘。
    """
    global _pool_cache, _pool_bootstrapped
    now = time.monotonic()
    if _pool_cache is not None and now - _pool_cache[0] < _pool_cache[2]:
        return _pool_cache[1]

    if not _pool_bootstrapped:
        _pool_bootstrapped = True
        cached = _load_pool_from_disk()
        if cached is not None:
            _pool_cache = (now, cached, _POOL_TTL)
            return cached

    pool = _pool_from_http()
    ttl = _POOL_TTL
    if pool is None:
        try:
            pool = get_data_source().get_stock_pool()
        except DataSourceError:
            logger.warning("股票池主源不可用，降级为持久化快照代码表")
            pool = _pool_from_snapshot()
            ttl = _POOL_FALLBACK_TTL
            if pool is None:
                raise
    # 清洗名称：消除个别票（多为 688 科创板）的 GBK 误读乱码，避免写进缓存与前端
    if pool is not None and "name" in pool.columns:
        pool = pool.copy()
        pool["name"] = [clean_name(c, n) for c, n in zip(pool["code"], pool["name"])]
    _pool_cache = (now, pool, ttl)
    _save_pool_to_disk(pool)
    return pool


def _load_pool_from_disk() -> pd.DataFrame | None:
    """读取 12 小时内的股票池磁盘缓存；无缓存/过期/损坏返回 None。"""
    try:
        if not _POOL_FILE.exists():
            return None
        if time.time() - _POOL_FILE.stat().st_mtime > _POOL_FILE_MAX_AGE:
            return None
        df = pd.read_parquet(_POOL_FILE)
        if df is None or df.empty or "code" not in df.columns:
            return None
        return _finalize_pool(df)
    except Exception as exc:  # noqa: BLE001 - 缓存损坏不影响主流程
        logger.debug("股票池磁盘缓存读取失败: %s", exc)
        return None


def _save_pool_to_disk(pool: pd.DataFrame) -> None:
    """写回股票池磁盘缓存（失败仅记录日志）。"""
    try:
        _POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
        pool.to_parquet(_POOL_FILE, index=False)
    except Exception as exc:  # noqa: BLE001 - 缓存写入失败不影响主流程
        logger.debug("股票池磁盘缓存写入失败: %s", exc)


def _pool_from_http() -> pd.DataFrame | None:
    """HTTP 直连全市场列表（东财 push2 → 新浪分页）；全部不可用返回 None。

    取到列表后用上交所官方数据校验沪市覆盖，缺失的沪市代码自动补齐。
    """
    from app.data.fallback import em_stock_list, sina_stock_list

    for fetch, label in ((em_stock_list, "东财列表"), (sina_stock_list, "新浪列表")):
        try:
            raw = fetch()
        except Exception as exc:  # noqa: BLE001 - 换下一个来源
            logger.warning("%s接口不可用: %s", label, exc)
            continue
        if raw is not None and not raw.empty:
            return _merge_with_sse(_finalize_pool(raw))
    return None


def _merge_with_sse(pool: pd.DataFrame) -> pd.DataFrame:
    """用上交所官方列表校验沪市覆盖，缺失代码补齐（交易所官方口径）。"""
    from app.data.fallback import sse_stock_list

    try:
        official = sse_stock_list()
    except Exception as exc:  # noqa: BLE001 - 校验源失败不影响主池
        logger.warning("上交所官方列表不可用，跳过沪市校验: %s", exc)
        return pool
    if official is None or official.empty:
        return pool

    official = _finalize_pool(official)
    official = official[official["market"] == "sh"]
    missing = official[~official["code"].isin(set(pool["code"]))]
    if missing.empty:
        logger.info("股票池校验通过：上交所 %d 只沪市股票已全部覆盖", len(official))
        return pool
    logger.info(
        "股票池用上交所官方数据补齐 %d 只沪市股票（官方共 %d 只）",
        len(missing), len(official),
    )
    return pd.concat([pool, missing], ignore_index=True).drop_duplicates("code")


def _pool_from_snapshot() -> pd.DataFrame | None:
    """从持久化快照构建股票池（code/name/market）；无快照时返回 None。"""
    from app.data.snapshot_store import snapshot_store

    snap = snapshot_store.peek_snapshot()
    if snap is None or snap.empty or "code" not in snap.columns:
        return None
    pool = snap[["code", "name"]].dropna(subset=["code"]).drop_duplicates("code")
    return _finalize_pool(pool)


def _finalize_pool(pool: pd.DataFrame) -> pd.DataFrame:
    """统一股票池字段：补零代码 + market 列。"""
    pool = pool.copy()
    pool["code"] = pool["code"].astype(str).str.zfill(6)
    if "name" not in pool.columns:
        pool["name"] = pool["code"]
    pool["market"] = pool["code"].map(_market_of)
    return pool[["code", "name", "market"]].reset_index(drop=True)


def get_kline_cached(
    code: str,
    adjust: str = "qfq",
    refresh: bool = False,
    days: int | None = None,
    timeout: float | None = None,
    retries: int | None = None,
) -> pd.DataFrame:
    """获取日K线：本地 Parquet 缓存优先，增量更新。

    流程:
      1. 有缓存 → 以缓存最后日期为起点增量拉取，合并去重；
      2. 无缓存 → 全量拉取（默认近3年）；
      3. 前复权数据写回缓存，供后续请求/扫描复用。
    """
    source = get_data_source()
    cached = None if refresh else kline_cache.get(code)

    if cached is None:
        df = source.get_daily_kline(code, adjust=adjust, timeout=timeout, retries=retries)
    else:
        last = kline_cache.last_date(code)
        new_df = source.get_daily_kline(code, adjust=adjust, start_date=last,
                                        timeout=timeout, retries=retries)
        if new_df.empty:
            df = cached
        else:
            df = pd.concat([cached, new_df], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)

    if adjust == "qfq" and not df.empty:
        kline_cache.save(code, df)

    return df if days is None else df.tail(days)


def get_index_cached(
    symbol: str = INDEX_SYMBOL,
    refresh: bool = False,
    timeout: float | None = None,
    retries: int | None = None,
) -> pd.DataFrame:
    """获取指数日线（全历史），写回同一个 Parquet 缓存目录。

    与个股 ``get_kline_cached`` 的两点区别：
      1. 不做 ``last_date`` 增量——指数一天只有一根 bar，全量也就几千行，
         而且回源整段能顺带修正历史修订，比增量更省心；
      2. 回源失败时**不退化成抛错**，有缓存就沿用缓存：择时（regime_ma /
         regime_tiers）只依赖这一条序列，宁可滞后一天，也不要让整轮预热或
         回测因为一个指数请求失败而中断。

    Args:
        symbol: 指数代码（新浪格式，如 ``sh000001``）
        refresh: 忽略缓存强制回源
        timeout / retries: 透传给数据源

    Returns:
        按日期升序的 DataFrame；无缓存且回源失败时返回空表。
    """
    key = f"idx_{symbol}"
    cached = None if refresh else kline_cache.get(key)
    try:
        df = get_data_source().get_index_daily(
            symbol, timeout=timeout, retries=retries
        )
    except Exception as exc:  # noqa: BLE001 - 单指数失败不拖垮调用方
        if cached is not None and not cached.empty:
            logger.warning("指数日线刷新失败，沿用缓存 %s（最后 %s）: %s",
                           key, str(cached["date"].iloc[-1]), exc)
            return cached
        logger.warning("指数日线获取失败且无缓存 %s: %s", key, exc)
        return pd.DataFrame()

    if df is None or df.empty:
        if cached is not None and not cached.empty:
            logger.warning("指数日线回源为空，沿用缓存 %s", key)
            return cached
        return pd.DataFrame()

    kline_cache.save(key, df)
    merged = kline_cache.get(key)
    return merged if merged is not None else df
