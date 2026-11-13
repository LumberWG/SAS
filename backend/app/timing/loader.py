"""读取本地指数/ETF 日 K 线（D: 盘 klines/ 下的 idx_*.parquet / *.parquet）。

离线、无网络依赖。指数命名约定与 board_index.py 一致：idx_<symbol>.parquet。
"""
from __future__ import annotations

import os

import pandas as pd


def load_index(symbol: str, kline_dir: str | None = None) -> pd.DataFrame:
    """读取指数/ETF 日线，返回按 date 升序、含 date/open/high/low/close/volume/amount 的 DataFrame。

    symbol 如 'sz399006'（创业板指）、'sh000300'（沪深300）、'510300'（沪深300ETF）。
    优先读 idx_<symbol>.parquet，回退到 <symbol>.parquet。
    """
    if kline_dir is None:
        from app.config import get_settings
        kline_dir = str(get_settings().kline_dir)
    p_idx = os.path.join(kline_dir, f"idx_{symbol}.parquet")
    p_plain = os.path.join(kline_dir, f"{symbol}.parquet")
    path = p_idx if os.path.exists(p_idx) else p_plain
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到指数/ETF 数据: {path} (symbol={symbol})")
    df = pd.read_parquet(path)
    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    df = df.sort_values("date").reset_index(drop=True)
    return df
