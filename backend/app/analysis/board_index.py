"""板块分类与板块感知指数参考（Layer 1：同板块才同参考）。

把「选股参考指数必须和标的同板块，不要混为一谈」落地：
- ``classify_board(code)``：按代码前缀判定板块（创业板 / 上证主板 / 深证主板 / 科创板 / 北交所…）。
- ``BOARD_INDEX``：板块 → 参考指数（本地 ``klines/idx_<symbol>.parquet`` 优先）。
- ``board_uptrend_set(kline_dir, board, ma)``：返回该板块参考指数处于上升通道（收盘>MA）的日期集合。
- ``board_regime(board)``：返回该板块当前趋势状态，供个股板块参考。

注：北证50(bj899050) 本地无 parquet，会回退到「不强制板块趋势过滤（available=False）」，
不影响其余板块。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from app.board_rules import BOARD_PREFIXES, board_key_of

# 板块 → 参考指数。index 为 akshare 代码，对应 klines/idx_<index>.parquet。
# #181：前缀统一引用 app.board_rules.BOARD_PREFIXES，与交易/范围白名单共用一套，
# 避免同一只票在「选股板块」和「交易板块」被归到不同板块（689/004/920 曾是差异点）。
BOARD_INDEX: dict[str, dict] = {
    "cyb":     {"index": "sz399006", "name": "创业板指", "prefix": BOARD_PREFIXES["cyb"]},
    "sh_main": {"index": "sh000001", "name": "上证指数", "prefix": BOARD_PREFIXES["sh_main"]},
    "sz_main": {"index": "sz399001", "name": "深证成指", "prefix": BOARD_PREFIXES["sz_main"]},
    "star":    {"index": "sh000688", "name": "科创50",   "prefix": BOARD_PREFIXES["star"]},
    "bse":     {"index": "bj899050", "name": "北证50",   "prefix": BOARD_PREFIXES["bse"]},
}

# 反查：指数代码 → 板块（便于给定指数时回溯板块）
_INDEX_TO_BOARD = {m["index"]: b for b, m in BOARD_INDEX.items()}


def classify_board(code: str) -> str:
    """根据 6 位代码前缀判定板块键。未知返回 ``"other"``。"""
    return board_key_of(str(code).zfill(6))


def board_index_symbol(board: str) -> str | None:
    m = BOARD_INDEX.get(board)
    return m["index"] if m else None


def board_index_name(board: str) -> str:
    m = BOARD_INDEX.get(board)
    return m["name"] if m else board


def _read_index_series(kline_dir: str, symbol: str) -> pd.Series | None:
    f = os.path.join(kline_dir, f"idx_{symbol}.parquet")
    if not os.path.exists(f):
        return None
    try:
        df = pd.read_parquet(f).sort_values("date")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"].astype(float)
        s.index = pd.to_datetime(s.index)
        return s
    except Exception:
        return None


def board_uptrend_set(kline_dir: str, board: str, ma: int = 60) -> set[str] | None:
    """该板块参考指数处于上升通道（收盘 > MA）的日期集合；无数据返回 None。"""
    sym = board_index_symbol(board)
    if not sym:
        return None
    s = _read_index_series(kline_dir, sym)
    if s is None or len(s) < ma + 1:
        return None
    ma_series = s.rolling(ma).mean()
    up = (s > ma_series) & s.notna() & ma_series.notna()
    return set(s.index[up].astype(str))


def board_regime(board: str, kline_dir: str | None = None, ma: int = 60) -> dict:
    """该板块当前趋势状态。无数据返回 available=False。"""
    sym = board_index_symbol(board)
    if not sym:
        return {"board": board, "available": False, "state": "unknown"}
    if kline_dir is None:
        from app.config import get_settings
        kline_dir = str(get_settings().kline_dir)
    s = _read_index_series(kline_dir, sym)
    if s is None or len(s) < ma + 1:
        return {"board": board, "index": sym, "name": board_index_name(board),
                "available": False, "state": "unknown"}
    last = float(s.iloc[-1])
    m = float(s.rolling(ma).mean().iloc[-1])
    above = last > m
    return {
        "board": board, "index": sym, "name": board_index_name(board),
        "available": True, "close": last, "ma": m,
        "dev_pct": round((last / m - 1) * 100, 2),
        "above_ma": bool(above),
        "state": "uptrend" if above else "downtrend",
        "as_of": str(s.index[-1].date()),
    }


__all__ = [
    "BOARD_INDEX", "classify_board", "board_index_symbol", "board_index_name",
    "board_uptrend_set", "board_regime",
]
