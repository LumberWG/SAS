"""行情 API：大盘概览、指数走势、个股快照与K线。"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.data.akshare_source import get_data_source
from app.data.snapshot_store import snapshot_store
from app.market.sentiment import classify_sentiment

logger = logging.getLogger("sas.api.market")
router = APIRouter(prefix="/market", tags=["行情"])


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _limit_pct(code: str, name: str = "") -> float:
    """返回该股票的涨跌停幅度（%）。

    A股各板块涨跌幅限制不同：
    - 沪深主板   : ±10%（ST 股为 ±5%，此处按名称含 ST 判断）
    - 创业板(30x)/ 科创板(68x) : ±20%
    - 北交所(43/83/87/88/92x)  : ±30%
    """
    if "ST" in name.upper():
        return 5.0
    if code.startswith(("30", "68")):
        return 20.0
    if code.startswith(("43", "83", "87", "88", "92")):
        return 30.0
    return 10.0


def _is_trading_time(now: datetime | None = None) -> bool:
    """判断当前是否处于 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (11 * 60 + 30) or (13 * 60) <= minutes <= (15 * 60)


def _market_temperature(up: int, down: int, flat: int, limit_up: int, total: int) -> float:
    """市场温度计（0-100）。

    公式说明：
      基础分 = 上涨家数占比 × 60      （市场广度的基础贡献）
      强度分 = min(涨停家数占比, 5%) / 5% × 40  （极端强势的加成，封顶40）
    温度越高代表市场整体越强，>70 偏热，<30 偏冷。
    """
    if total <= 0:
        return 50.0
    up_ratio = up / total
    limit_ratio = min(limit_up / total, 0.05) / 0.05
    return round(up_ratio * 60 + limit_ratio * 40, 1)


def _with_disclaimer(data: dict) -> dict:
    """统一注入免责声明字段。"""
    data["disclaimer"] = get_settings().disclaimer
    return data


# ----------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------
@router.get("/overview")
def market_overview() -> dict:
    """大盘概览：主要指数 + 涨跌家数 + 成交额 + 市场温度。"""
    try:
        indices_df = snapshot_store.get_indices()
        snapshot = snapshot_store.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取大盘概览失败")
        raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc

    # ---- 涨跌家数统计 ----
    pct = snapshot["pct_change"].fillna(0.0)
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    total = len(snapshot)

    # 涨停/跌停家数：按各板块涨跌停幅度判断（留 0.1% 误差容忍）
    limit_up = 0
    limit_down = 0
    for row in snapshot.itertuples():
        limit = _limit_pct(row.code, getattr(row, "name", ""))
        if row.pct_change is not None and not pd.isna(row.pct_change):
            if row.pct_change >= limit - 0.1:
                limit_up += 1
            elif row.pct_change <= -limit + 0.1:
                limit_down += 1

    total_amount = float(snapshot["amount"].fillna(0).sum())
    temperature = _market_temperature(up, down, flat, limit_up, total)
    sentiment = classify_sentiment(temperature)

    indices = []
    for row in indices_df.itertuples():
        indices.append({
            "symbol": row.symbol,
            "name": row.name,
            "price": None if pd.isna(row.price) else float(row.price),
            "pct_change": None if pd.isna(row.pct_change) else float(row.pct_change),
            "change": None if pd.isna(row.change) else float(row.change),
            "amount": None if pd.isna(row.amount) else float(row.amount),
        })

    return _with_disclaimer({
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trading": _is_trading_time(),
        "indices": indices,
        "breadth": {
            "up": up, "down": down, "flat": flat, "total": total,
            "limit_up": limit_up, "limit_down": limit_down,
        },
        "total_amount": round(total_amount, 2),  # 单位：元
        "temperature": temperature,
        "sentiment": sentiment,  # 5 档情绪等级（含权重与操作建议）
    })


@router.get("/indices/{symbol}/kline")
def index_kline(
    symbol: str,
    days: int = Query(60, ge=5, le=500),
    refresh: bool = Query(False, description="强制刷新缓存"),
) -> dict:
    """指数迷你走势（最近 N 个交易日收盘价，带 Parquet 缓存）。

    指数数据量小且变更慢（每日一根），与个股K线共用 Parquet 缓存
    （缓存 key 加 idx_ 前缀避免与个股代码冲突），
    避免前端轮询时反复触发完整数据源降级链。
    """
    from app.data.cache import kline_cache

    cache_key = f"idx_{symbol}"
    try:
        df = None if refresh else kline_cache.get(cache_key)
        if df is None:
            df = get_data_source().get_index_daily(symbol)
            if not df.empty:
                kline_cache.save(cache_key, df)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取指数K线失败 symbol=%s", symbol)
        raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"指数 {symbol} 无数据")
    df = df.tail(days)
    return _with_disclaimer({
        "symbol": symbol,
        "dates": df["date"].tolist(),
        "closes": [None if pd.isna(v) else float(v) for v in df["close"]],
    })


@router.get("/stocks/{code}")
def stock_quote(code: str) -> dict:
    """个股实时快照（来自全市场快照）。"""
    try:
        snapshot = snapshot_store.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc
    row = snapshot[snapshot["code"] == code]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"未找到股票 {code}")
    r = row.iloc[0]
    return _with_disclaimer({
        "code": code,
        "name": r.get("name", ""),
        "price": None if pd.isna(r.get("price")) else float(r["price"]),
        "pct_change": None if pd.isna(r.get("pct_change")) else float(r["pct_change"]),
        "change": None if pd.isna(r.get("change")) else float(r["change"]),
        "open": None if pd.isna(r.get("open")) else float(r["open"]),
        "high": None if pd.isna(r.get("high")) else float(r["high"]),
        "low": None if pd.isna(r.get("low")) else float(r["low"]),
        "prev_close": None if pd.isna(r.get("prev_close")) else float(r["prev_close"]),
        "volume": None if pd.isna(r.get("volume")) else float(r["volume"]),
        "amount": None if pd.isna(r.get("amount")) else float(r["amount"]),
        "turnover_rate": None if pd.isna(r.get("turnover_rate")) else float(r["turnover_rate"]),
        "volume_ratio": None if pd.isna(r.get("volume_ratio")) else float(r["volume_ratio"]),
        "total_mv": None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
        "float_mv": None if pd.isna(r.get("float_mv")) else float(r["float_mv"]),
    })


@router.get("/stocks/{code}/kline")
def stock_kline(
    code: str,
    days: int = Query(120, ge=20, le=1000),
    adjust: str = Query("qfq", pattern="^(qfq|hfq|)$"),
    refresh: bool = Query(False, description="强制刷新本地缓存"),
) -> dict:
    """个股日K线（本地 Parquet 缓存优先，增量更新）。"""
    from app.data.service import get_kline_cached

    try:
        df = get_kline_cached(code, adjust=adjust, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取K线失败 code=%s", code)
        raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc

    if df.empty:
        raise HTTPException(status_code=404, detail=f"股票 {code} 无K线数据")

    df = df.tail(days)
    # 降级数据源（腾讯）不提供涨跌幅，由收盘价推导：pct = (close_t/close_{t-1} - 1) * 100
    if "pct_change" not in df.columns:
        df = df.copy()
        df["pct_change"] = df["close"].pct_change() * 100

    return _with_disclaimer({
        "code": code,
        "dates": df["date"].tolist(),
        "kline": [
            [float(r.open), float(r.close), float(r.low), float(r.high)]
            for r in df.itertuples()
        ],
        "volumes": [float(v) for v in df["volume"].fillna(0)],
        "amounts": [None if pd.isna(v) else float(v) for v in df["amount"]],
        "pct_changes": [None if pd.isna(v) else float(v) for v in df["pct_change"]],
    })
