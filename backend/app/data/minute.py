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

import numpy as np
import pandas as pd

from app.config import get_settings

# 本地预下载分钟线目录：与 kline_dir 同级（<root>/data/）
# 5 分钟线优先用 free-stockdb 导出的全市场数据 minute5_fsdb（5559 只，
# 2025-01-02 起），没有该目录时才回退旧的 baostock 缓存 minute5（803 只）。
# 可用环境变量 SAS_MINUTE5_DIR 强制指定。
def _data_dir() -> Path:
    try:
        return get_settings().kline_dir.parent
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent.parent.parent / "data"


_DATA_DIR = _data_dir()
_MINUTE5_NEW = _DATA_DIR / "minute5_fsdb"
_MINUTE5_OLD = _DATA_DIR / "minute5"
_env5 = os.getenv("SAS_MINUTE5_DIR")
_env1 = os.getenv("SAS_MINUTE1_DIR")
MINUTE5_DIR = Path(_env5) if _env5 else (_MINUTE5_NEW if _MINUTE5_NEW.is_dir() else _MINUTE5_OLD)
# 1 分钟线（本地全市场，约 10 GB），供分时接口直接读盘，不必联网
MINUTE1_DIR = Path(_env1) if _env1 else (_DATA_DIR / "minute1_fsdb")

_MINUTE_CACHE: dict[str, pd.DataFrame] = {}   # code -> 区间内分钟线(df: date, close)
#: 本地 5 分钟线按「code → ({日期: (起, 止)}, 列数组)」缓存。
#: 原实现每次调用都 ``pd.read_parquet`` 整个文件（约 2.1 万行、2.5ms），
#: 回测里每个持仓日都会调一次，重复解析纯属浪费。
#: 只保存日期→切片下标的映射和 4 个 float 列（约 680KB/只），不复制 DataFrame；
#: 切分用 numpy（~3ms/只）而非 groupby（~13ms/只），首次读入后取值近乎零成本。
#: 列：``open``（日内强势判定要当日开盘价）、``close``、``volume``、``amount``
#: （后三列用于累计成交均价 = 分时均价）。
_COLS = ("open", "close", "volume", "amount")
_LOCAL5_CACHE: dict[str, tuple[dict[str, tuple[int, int]], dict[str, "np.ndarray"]]] = {}
_LOCAL5_CAP = 256          # 单次回测用到的标的远小于此，超限整体清空防内存膨胀
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
    """清除内存缓存（不同回测区间之间调用，避免串区间）。

    一并清掉本地 5 分钟线的按日缓存：那份数据本身与区间无关，但回测结束后
    没有理由继续占内存（每只标的约 440 天 × 48 根 close）。
    """
    with _CACHE_LOCK:
        _MINUTE_CACHE.clear()
        _LOCAL5_CACHE.clear()


def _local5_slice(
    code: str, path: Path
) -> Optional[tuple[dict[str, tuple[int, int]], dict[str, "np.ndarray"]]]:
    """读本地 5 分钟 parquet，返回 ``({日期: (起, 止)}, {列名: 数组})``。

    文件按时间升序，所以 ``np.unique(..., return_index=True)`` 给出的首个下标
    正好是每个交易日的起点，相邻日期起点即前一日的终点，无需 groupby。
    首次读某只标的约 5~35ms（热盘 5ms），之后所有持仓日直接命中缓存。
    读取失败返回 ``None``，由调用方决定降级还是当作无数据。
    """
    with _CACHE_LOCK:
        cached = _LOCAL5_CACHE.get(code)
    if cached is not None:
        return cached
    try:
        ldf = pd.read_parquet(path, columns=["date", *_COLS])
    except Exception:  # noqa: BLE001 - 坏文件按无数据处理
        return None
    if ldf is None or ldf.empty or "date" not in ldf.columns:
        return None
    days = ldf["date"].astype(str).str.slice(0, 10).to_numpy()
    if days.size == 0:
        return None
    cols: dict[str, np.ndarray] = {}
    for name in _COLS:
        if name not in ldf.columns:
            return None
        arr = pd.to_numeric(ldf[name], errors="coerce").to_numpy(dtype=float)
        if arr.size != days.size:
            return None
        cols[name] = arr
    uniq, first = np.unique(days, return_index=True)
    ends = np.append(first[1:], days.size)
    spans = {str(u): (int(a), int(b)) for u, a, b in zip(uniq, first, ends)}
    entry = (spans, cols)
    with _CACHE_LOCK:
        if len(_LOCAL5_CACHE) >= _LOCAL5_CAP:
            _LOCAL5_CACHE.clear()
        _LOCAL5_CACHE[code] = entry
    return entry


def get_minute_prices(
    code: str, date: str, period: str = _PERIOD, *, local_only: bool = False
) -> Optional[list[float]]:
    """返回 ``code`` 在 ``date`` 当日的分钟收盘价序列（按时间升序）。

    失败 / 无数据返回 ``None``（调用方回落日K 边界）。数据源短期熔断时直接返回 ``None``。

    ``local_only=True`` 时**只读本地分钟缓存**（``data/minute5_fsdb``，缺失时回退
    ``data/minute5``），不联网。回测必须用它：
    否则同一参数在不同时间点跑，会因「哪些票恰好联网抓到了分钟线」而结果不同
    （实测同参数两次跑出 20.63% / 16.36%，不可复现）。
    """
    global _SOURCE_DISABLED
    if _SOURCE_DISABLED and (time.time() - _SOURCE_DISABLED_AT) < _DISABLE_TTL:
        return None
    # 优先读本地预下载缓存（data/minute5_fsdb/{code}.parquet，秒级、无需网络）
    local = MINUTE5_DIR / f"{code}.parquet"
    if local.exists():
        # 缓存里的 date 是带时分的 timestamp（2025-01-02 09:35:00），
        # 直接跟 "YYYY-MM-DD" 比会永远匹配不上 → 统一截到日期再比
        entry = _local5_slice(code, local)
        if entry is not None:
            span = entry[0].get(date)
            if span is not None:
                seq = [float(x) for x in entry[1]["close"][span[0]:span[1]] if x == x]
                if seq:
                    return seq
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


def get_minute_day_bars(code: str, date: str) -> Optional[dict[str, list[float]]]:
    """返回 ``code`` 在 ``date`` 当日的 5 分钟 bar（纯本地，缺失返回 ``None``）。

    键：``open`` / ``close`` / ``volume`` / ``amount``，均为按时间升序的列表。
    供「分钟级强势确认」买入使用：需要当日开盘价（``open[0]``）与累计成交均价
    （``cumsum(amount)/cumsum(volume)``）。
    """
    path = MINUTE5_DIR / f"{code}.parquet"
    if not path.exists():
        return None
    entry = _local5_slice(code, path)
    if entry is None:
        return None
    span = entry[0].get(date)
    if span is None:
        return None
    start, end = span
    out: dict[str, list[float]] = {}
    for name in _COLS:
        out[name] = [float(x) for x in entry[1][name][start:end]]
    return out if out["close"] else None


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


# ---------------------------------------------------------------------------
# 本地 1 分钟数据 → 当日分时（免联网，毫秒级）
# ---------------------------------------------------------------------------
_INTRADAY_COLUMNS = ["date", "time", "price", "avg_price", "volume", "amount"]


def get_intraday_local(code: str) -> Optional[pd.DataFrame]:
    """从本地 ``data/minute1_fsdb/{code}.parquet`` 生成最近一个交易日的分时序列。

    返回列与联网版完全一致：``date, time, price, avg_price, volume, amount``，
    其中 ``volume`` 单位为**手**（本地库是股，已除以 100 对齐），``avg_price`` 为
    当日累计成交均价。本地无该代码 / 读取异常时返回 ``None``，由调用方降级联网。
    """
    path = MINUTE1_DIR / f"{code}.parquet"
    if not path.is_file():
        return None
    try:
        # 文件按时间升序，最后一行所在日期即最近交易日
        days = pd.read_parquet(path, columns=["date"])["date"]
        if days.empty:
            return None
        day = str(days.iloc[-1])[:10]
        df = pd.read_parquet(
            path,
            filters=[("date", ">=", f"{day} 00:00:00"), ("date", "<=", f"{day} 23:59:59")],
        )
    except Exception:  # noqa: BLE001 - 任何读取问题都交给联网降级
        return None
    if df is None or df.empty:
        return None

    price = pd.to_numeric(df["close"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)   # 股
    amt = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)   # 元
    cum_v = vol.cumsum()
    cum_a = amt.cumsum()

    out = pd.DataFrame(
        {
            "date": day,
            "time": df["date"].astype(str).str.slice(11, 19),
            "price": price.round(3),
            "avg_price": (cum_a / cum_v.where(cum_v > 0)).round(3),
            "volume": (vol / 100.0).round(2),   # 股 → 手，与联网口径一致
            "amount": amt.round(2),
        }
    )
    out = out.dropna(subset=["price"]).reset_index(drop=True)
    return out[_INTRADAY_COLUMNS] if not out.empty else None
