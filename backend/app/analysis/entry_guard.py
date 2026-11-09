"""入场护栏（仅影子层 / 提示，不改动选股、评分、下单逻辑）。

两个相互独立、都不预测转折点的护栏：

1. **市场状态滤网（market-regime guard）**
   用上证指数相对 MA20 / MA60 的位置判断当前是上升市 / 震荡 / 下降市。
   回测（4 年、4 万+ 次动量信号）显示：指数站上 MA20 时信号 20 日超额 +0.74%，
   跌破 MA20 时转为 −1.22%。因此下降市状态下，对新动量 / ML 信号标注「慎开新仓」。
   它只是个状态开关，不预测拐点，与「大盘给基准线」的原始直觉一致。

2. **日内追高护栏（intraday-spike guard）**
   当某信号触发日个股高开（开盘价 vs 前收盘 > 阈值）或盘中冲高（最高价 vs 开盘价
   > 阈值）明显时，提示用限价单在尾盘或回踩至均线附近买入，避免买在当根尖峰。
   纯阈值规则，无预测成分。

所有函数只读数据，遇缺失文件返回 ``available=false``，不抛异常、不 500。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.config import DATA_DIR, get_settings

#: 高开阈值：开盘价较前一交易日收盘涨幅超过该值即触发追高提示
GAP_THRESHOLD = 0.03
#: 盘中冲高阈值：当日最高价较开盘价涨幅超过该值即触发追高提示
SPIKE_THRESHOLD = 0.05

_IDX_CODE = "idx_sh000001"


def _read_parquet(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "date" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    return df


def market_regime() -> dict:
    """判定当前市场状态（基于上证指数日线）。

    返回状态：``uptrend``（上升市，close>MA20 且 close>MA60）、
    ``choppy``（震荡，close>MA20 但 <MA60）、``declining``（下降市，close<MA20）、
    ``unknown``（数据不可用）。``new_entry_advice`` 在下降市为 ``avoid``，其余 ``ok``。
    """
    s = get_settings()
    df = _read_parquet(s.kline_dir / f"{_IDX_CODE}.parquet")
    if df is None or len(df) < 60:
        return {
            "state": "unknown", "close": None, "ma20": None, "ma60": None,
            "above_ma20": None, "above_ma60": None, "as_of": None,
            "new_entry_advice": "unknown",
        }
    df = df.sort_values("date")
    close = float(df["close"].iloc[-1])
    ma20 = float(df["close"].rolling(20).mean().iloc[-1])
    ma60 = float(df["close"].rolling(60).mean().iloc[-1])
    above20 = close > ma20
    above60 = close > ma60
    if above20 and above60:
        state = "uptrend"
    elif not above20:
        state = "declining"
    else:
        state = "choppy"
    return {
        "state": state,
        "close": close,
        "ma20": ma20,
        "ma60": ma60,
        "above_ma20": bool(above20),
        "above_ma60": bool(above60),
        "as_of": str(df["date"].iloc[-1].date()),
        "new_entry_advice": "avoid" if state == "declining" else "ok",
    }


def intraday_spike(code: str, scan_date: str) -> dict:
    """判定某信号触发日是否出现明显的日内追高。

    读取 ``minute5_fsdb/{code}.parquet`` 当日 5 分钟 bar：
    - ``gap_up_pct`` = 开盘价 / 前一日收盘 − 1（前收盘取自日线）；
    - ``intraday_high_pct`` = 当日最高 / 开盘价 − 1。
    任一超过阈值即 ``triggered``，并给出「勿追当根尖峰」的提示文案。
    数据缺失（无分钟文件 / 无日线前收盘）返回 ``available=false``。
    """
    s = get_settings()
    code6 = str(code).zfill(6)
    f = DATA_DIR / "minute5_fsdb" / f"{code6}.parquet"
    df = _read_parquet(f)
    if df is None:
        return {"available": False, "triggered": False,
                "gap_up_pct": None, "intraday_high_pct": None,
                "open": None, "high": None, "note": ""}
    day = pd.to_datetime(scan_date).date()
    sub = df[df["date"].dt.date == day]
    if sub.empty:
        return {"available": False, "triggered": False,
                "gap_up_pct": None, "intraday_high_pct": None,
                "open": None, "high": None, "note": ""}
    open0 = float(sub["open"].iloc[0])
    high = float(sub["high"].max())

    prev_close = None
    kl = s.kline_dir / f"{code6}.parquet"
    k = _read_parquet(kl)
    if k is not None:
        k = k[k["date"] < pd.to_datetime(scan_date)]
        if not k.empty:
            prev_close = float(k["close"].iloc[-1])

    gap = (open0 / prev_close - 1.0) if prev_close else float("nan")
    spike = (high / open0 - 1.0) if open0 else float("nan")
    triggered = (
        (not np.isnan(gap) and gap > GAP_THRESHOLD)
        or (not np.isnan(spike) and spike > SPIKE_THRESHOLD)
    )
    note = (
        "信号日高开/盘中冲高明显，建议用限价单在尾盘或回踩至均线附近买入，勿追当根尖峰"
        if triggered else ""
    )
    return {
        "available": True,
        "triggered": bool(triggered),
        "gap_up_pct": None if np.isnan(gap) else float(gap),
        "intraday_high_pct": None if np.isnan(spike) else float(spike),
        "open": open0,
        "high": high,
        "note": note,
    }


def evaluate_entry_guards(limit: int = 20, signal_table: str = "signals") -> dict:
    """对最近一批信号叠加两个护栏，返回可直接展示的结构。

    不修改任何信号 / 选股逻辑，只读 ``signals`` 表与行情数据。
    """
    regime = market_regime()
    s = get_settings()
    import sqlite3

    con = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT code, name, strategy, price, scan_date, reason, score, rank "
            f"FROM {signal_table} ORDER BY scan_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()

    items = []
    for r in rows:
        rec = {k: r[k] for k in r.keys()}
        sp = intraday_spike(rec["code"], rec["scan_date"])
        rec["regime_state"] = regime["state"]
        rec["regime_advice"] = regime["new_entry_advice"]
        rec["intraday"] = sp
        # 综合提示：下降市 或 日内追高触发
        flags = []
        if regime["new_entry_advice"] == "avoid":
            flags.append("下降市慎开新仓")
        if sp.get("triggered"):
            flags.append("日内追高")
        rec["flags"] = flags
        items.append(rec)

    return {
        "regime": regime,
        "guards": {
            "gap_threshold": GAP_THRESHOLD,
            "spike_threshold": SPIKE_THRESHOLD,
        },
        "signals": items,
    }
