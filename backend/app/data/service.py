"""行情数据服务：带本地缓存的K线获取（API 与扫描器共用）。"""
from __future__ import annotations

import pandas as pd

from app.data.akshare_source import get_data_source
from app.data.cache import kline_cache


def get_stock_pool() -> pd.DataFrame:
    """全市场股票池（code/name/market）。"""
    return get_data_source().get_stock_pool()


def get_kline_cached(
    code: str,
    adjust: str = "qfq",
    refresh: bool = False,
    days: int | None = None,
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
        df = source.get_daily_kline(code, adjust=adjust)
    else:
        last = kline_cache.last_date(code)
        new_df = source.get_daily_kline(code, adjust=adjust, start_date=last)
        if new_df.empty:
            df = cached
        else:
            df = pd.concat([cached, new_df], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)

    if adjust == "qfq" and not df.empty:
        kline_cache.save(code, df)

    return df if days is None else df.tail(days)
