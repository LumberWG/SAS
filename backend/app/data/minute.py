# -*- coding: utf-8 -*-
"""历史分钟线 provider（用于回测 intraday 精确止损/止盈）。

默认关闭；仅当 ``PipelineParams.intraday_minutes=True`` 时才被 ``backtest.pipeline`` 调用。

数据源：akshare ``stock_zh_a_hist_min``（基于新浪/腾讯历史分钟线），带：
  - 内存缓存：每个 code 只拉取一次（区间内全部分钟，按日切片），跨持仓日/参数组合复用；
  - 节流：请求间隔 ``_MIN_INTERVAL``，避免数据源限频；
  - 失败降级：任何异常 / akshare 缺失 / 网络失败都返回 ``None``，由调用方回落到
    日K 边界（open/low/high/close），**保证回测不崩、结果与日K 边界一致**。
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Optional

import pandas as pd

from app.config import get_settings

# 本地预下载分钟线目录：与 kline_dir 同级（<root>/data/minute5/）
try:
    _settings = get_settings()
    MINUTE5_DIR = _settings.kline_dir.parent / "minute5"
except Exception:  # noqa: BLE001
    MINUTE5_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "minute5"

_MINUTE_CACHE: dict[str, pd.DataFrame] = {}   # code -> 区间内分钟线(df: date, close)
_CACHE_LOCK = threading.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.35   # 秒，节流避免数据源限频
_PERIOD = "5"
# 数据源熔断：首次连续失败/拉不到数据后短期禁用，避免反复网络超时拖慢回测
_SOURCE_DISABLED = False
_SOURCE_DISABLED_AT = 0.0
_DISABLE_TTL = 600.0      # 禁用后 10 分钟内不再尝试
_FETCH_BUDGET = 20.0      # 单只 code 拉取总预算（秒），超时放弃降级


def clear_minute_cache() -> None:
    """清除内存缓存（不同回测区间之间调用，避免串区间）。"""
    with _CACHE_LOCK:
        _MINUTE_CACHE.clear()


def get_minute_prices(
    code: str, date: str, period: str = _PERIOD, *, local_only: bool = False
) -> Optional[list[float]]:
    """返回 ``code`` 在 ``date`` 当日的分钟收盘价序列（按时间升序）。

    失败 / 无数据返回 ``None``（调用方回落日K 边界）。数据源短期熔断时直接返回 ``None``。

    ``local_only=True`` 时**只读本地 ``data/minute5`` 缓存**，不联网。回测必须用它：
    否则同一参数在不同时间点跑，会因「哪些票恰好联网抓到了分钟线」而结果不同
    （实测同参数两次跑出 20.63% / 16.36%，不可复现）。
    """
    global _SOURCE_DISABLED
    if _SOURCE_DISABLED and (time.time() - _SOURCE_DISABLED_AT) < _DISABLE_TTL:
        return None
    # 优先读本地预下载缓存（data/minute5/{code}.parquet，秒级、无需网络）
    local = MINUTE5_DIR / f"{code}.parquet"
    if local.exists():
        try:
            ldf = pd.read_parquet(local)
            if ldf is not None and not ldf.empty and "date" in ldf.columns and "close" in ldf.columns:
                # 缓存里的 date 是带时分的 timestamp（2025-01-02 09:35:00），
                # 直接跟 "YYYY-MM-DD" 比会永远匹配不上 → 统一截到日期再比
                lday = ldf[ldf["date"].astype(str).str.slice(0, 10) == date]
                if not lday.empty:
                    return [float(x) for x in lday["close"].tolist()]
        except Exception:  # noqa: BLE001
            pass
    if local_only:
        return None
    # 降级到实时拉取（带缓存 + 熔断）
    df = _ensure_code(code, period)
    if df is None or df.empty:
        return None
    day = df[df["date"].astype(str).str.slice(0, 10) == date]
    if day.empty:
        return None
    return [float(x) for x in day["close"].tolist()]


def _ensure_code(code: str, period: str) -> Optional[pd.DataFrame]:
    with _CACHE_LOCK:
        if code in _MINUTE_CACHE:
            return _MINUTE_CACHE[code]
    df = _fetch_code_minutes(code, period)  # 锁已释放，可安全拉取
    with _CACHE_LOCK:
        _MINUTE_CACHE[code] = df
    return df


def _code_date_range(code: str):
    """从该 code 的日K缓存推断需拉取的分钟线区间（避免拉全市场全历史）。"""
    try:
        settings = get_settings()
        path = os.path.join(str(settings.kline_dir), f"{code}.parquet")
        if not os.path.isfile(path):
            return None, None
        df = pd.read_parquet(path)
        if df.empty or "date" not in df:
            return None, None
        return str(df["date"].iloc[0]), str(df["date"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None, None


def _fetch_code_minutes(code: str, period: str) -> Optional[pd.DataFrame]:
    global _SOURCE_DISABLED, _SOURCE_DISABLED_AT
    try:
        import akshare as ak
    except Exception:  # noqa: BLE001 - akshare 未安装则降级并熔断
        _SOURCE_DISABLED = True
        _SOURCE_DISABLED_AT = time.time()
        return None
    start, end = _code_date_range(code)
    if start is None:
        return None
    sym = code  # akshare 用 6 位代码
    start_t = time.time()
    try:
        parts: list[pd.DataFrame] = []
        cur = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        # 分段拉取：每段 ≤7 天 5min，避免单次超过数据源条数上限
        while cur <= end_ts:
            if time.time() - start_t > _FETCH_BUDGET:
                break  # 总预算耗尽，放弃后续分段（降级）
            nxt = min(cur + pd.Timedelta(days=6), end_ts)
            _throttle()
            s = cur.strftime("%Y%m%d")
            e = nxt.strftime("%Y%m%d")
            try:
                part = ak.stock_zh_a_hist_min(sym, period=period, start_date=s, end_date=e, adjust="")
            except Exception:  # noqa: BLE001 - 限频/网络异常，跳过该段
                part = None
            if part is not None and not part.empty:
                part = part.copy()
                tcol = "时间" if "时间" in part.columns else (part.columns[0] if len(part.columns) else None)
                ccol = "收盘" if "收盘" in part.columns else None
                if tcol and ccol:
                    part["date"] = part[tcol].astype(str).str.slice(0, 10)
                    part["close"] = pd.to_numeric(part[ccol], errors="coerce")
                    parts.append(part[["date", "close"]].dropna())
            cur = nxt + pd.Timedelta(days=1)
        if not parts:
            _SOURCE_DISABLED = True  # 拉不到任何数据，熔断避免反复网络
            _SOURCE_DISABLED_AT = time.time()
            return pd.DataFrame(columns=["date", "close"])
        return pd.concat(parts, ignore_index=True)
    except Exception:  # noqa: BLE001
        _SOURCE_DISABLED = True
        _SOURCE_DISABLED_AT = time.time()
        return pd.DataFrame(columns=["date", "close"])


def _throttle() -> None:
    """请求节流（全局），避免触发数据源限频。"""
    global _LAST_CALL
    with _CACHE_LOCK:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.time()
