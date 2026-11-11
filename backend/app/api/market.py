"""行情 API：大盘概览、指数走势、个股快照与K线。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.board_rules import limit_pct as board_limit_pct
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

    #181 起统一代理到 ``app.board_rules.limit_pct``，与撮合/回测共用同一套口径，
    避免行情统计与交易引擎对同一只票给出不同的涨跌停判定。
    - 沪深主板 ±10%、创业板/科创板 ±20%、北交所 ±30%、ST ±5%。
    """
    return board_limit_pct(code, name)


def _is_trading_time(now: datetime | None = None) -> bool:
    """判断当前是否处于 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (11 * 60 + 30) or (13 * 60) <= minutes <= (15 * 60)


#: 分时源分界时刻：工作日 9:15（集合竞价）。之后当日曲线一律取实时。
INTRADAY_LIVE_FROM_MINUTE = 9 * 60 + 15


def _intraday_source(now: datetime | None = None) -> str:
    """决定分时图取哪个源：``realtime``（当日）还是 ``local``（D 盘历史）。

    规则只有一条 —— **工作日 9:15 之后一律走实时源，其余时间读本地**：

    - 9:15 之后（含午休、收盘后直到次日凌晨）：实时源返回当日的完整曲线，
      不依赖 16:40 批量导出是否跑完，也不会拿到上一交易日的过期数据；
    - 9:15 之前、周六周日：读 ``minute1_fsdb``，即 D 盘上一交易日的分时
      （9:15 前当日还没有任何成交，本地就是最新的可用数据）；
    - 节假日即使是工作日也走实时，此时实时源返回上一交易日，``date`` 字段会写明。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return "local"
    return "realtime" if now.hour * 60 + now.minute >= INTRADAY_LIVE_FROM_MINUTE else "local"


async def _intraday_local_fallback(code: str) -> pd.DataFrame | None:
    """实时源不可用时，退回本地最近交易日的分时（不校验日期）。

    宁可展示上一交易日的收盘走势，也好过直接报错；返回的 ``date`` 字段会告诉
    前端这是哪一天的数据。
    """
    try:
        from app.data.minute import get_intraday_local

        return await asyncio.to_thread(get_intraday_local, code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地分时兜底失败 code=%s: %s", code, exc)
        return None


def _clip_trading_minutes(df: pd.DataFrame) -> pd.DataFrame:
    """只保留连续竞价时段（09:30-11:30 / 13:00-15:00）的分钟。

    实时源收盘后会多出 15:06-15:30 一批"盘后定价"记录：价格恒等于收盘价、
    成交量近乎为 0。直接画出来会把 x 轴拉长半小时、右侧拖一条平线，也与本地
    导出的 242 根对不上。统一裁掉，两个源的时间轴就一致了。
    """
    if df is None or df.empty or "time" not in df.columns:
        return df
    hhmm = df["time"].astype(str).str.slice(0, 5)
    keep = (hhmm.ge("09:30") & hhmm.le("11:30")) | (hhmm.ge("13:00") & hhmm.le("15:00"))
    trimmed = df[keep]
    # 时间字段异常导致全被裁掉时，宁可原样返回也不给空图
    return trimmed.reset_index(drop=True) if not trimmed.empty else df


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
def _minute_export_status() -> dict:
    """本地 1m 库导出进度（只读清单文件尾，不扫几千个 parquet）。

    清单每行 ``code|YYYYMMDD``；只看末尾 64KB 里的最大日期，足以回答
    「分时图是不是落后了一天」。读不到返回 available=False，绝不影响主接口。
    """
    try:
        from app.data.minute import MINUTE1_DIR

        path = MINUTE1_DIR / "_done_1m.txt"
        if not path.is_file():
            return {"available": False}
        stat = path.stat()
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            chunk = fh.read().decode("utf-8", "ignore")
        dates = re.findall(r"\|(\d{8})\s*$", chunk, flags=re.MULTILINE)
        return {
            "available": True,
            "latest_date": max(dates) if dates else None,
            "exported_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:  # noqa: BLE001 - 状态查询失败不能拖垮页面
        logger.debug("分钟库导出状态读取失败: %s", exc)
        return {"available": False}


@router.get("/data-status")
def market_data_status() -> dict:
    """全局数据新鲜度（顶栏状态条用）：快照/指数有多旧、是否在降级、分钟库导到哪天。

    与 ``/overview`` 不同，这里**不触发任何刷新、不联网**，因此 30s 轮询很便宜。
    ``degraded=true`` 的 200 响应也会由前端拦截器上报到这里的状态条，
    避免「接口成功但给的是旧数据」这种静默降级没人提示。
    """
    s = snapshot_store.status()
    trading = _is_trading_time()
    no_data = s["snapshot"]["rows"] == 0 or s["indices"]["rows"] == 0
    degraded = no_data or bool(s["recent_failures"])
    stale = bool(trading and (s["snapshot"]["stale"] or s["indices"]["stale"]))
    level = "error" if no_data else "warn" if (degraded or stale) else "ok"
    return _with_disclaimer({
        "level": level,
        "degraded": degraded,
        "stale": stale,
        "trading": trading,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": (
            "行情快照不可用，页面可能显示旧数据或缺失" if no_data else
            "行情刷新失败或数据已过期，页面可能显示旧数据" if (degraded or stale) else
            "行情数据正常"
        ),
        **s,
        "minute_export": _minute_export_status(),
    })


@router.get("/overview")
async def market_overview() -> dict:
    """大盘概览：主要指数 + 涨跌家数 + 成交额 + 市场温度。

    数据源故障（东财/新浪不可达）或冷启动无缓存时，快照拉取可能长时间挂起。
    这里对每个数据源分别限时等待（25s），失败则降级（返回 200 + degraded 结构），
    避免首页空白；若磁盘有持久化旧缓存则立即返回旧数据（SWR）。
    """
    indices_df: pd.DataFrame | None = None
    snapshot: pd.DataFrame | None = None

    async def _indices() -> pd.DataFrame | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(snapshot_store.get_indices, 20.0), timeout=25
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数快照不可用: %s", exc)
            return None

    async def _snapshot() -> pd.DataFrame | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(snapshot_store.get_snapshot, 20.0), timeout=25
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("全市场快照不可用: %s", exc)
            return None

    # 两路数据源互不依赖：并行等待，最坏 25s 而不是串行的 50s
    indices_df, snapshot = await asyncio.gather(_indices(), _snapshot())

    if indices_df is None and snapshot is None:
        # 完全无数据：返回降级结构（200），前端展示提示而非空白
        return _with_disclaimer({
            "degraded": True,
            "message": "行情数据源暂不可用，已为您保留最近缓存（若有）",
            "indices": [],
            "total_amount": 0,
            "breadth": {
                "up": 0, "down": 0, "flat": 0, "total": 0,
                "limit_up": 0, "limit_down": 0,
            },
            "temperature": 50.0,
            "sentiment": {
                "level": "未知", "weight": 0,
                "advice": "数据源暂不可用，情绪指标无法计算",
            },
            "updated_at": None,
        })

    # ---- 涨跌家数统计（快照列不完整时降级，避免 KeyError 致 500）----
    # 注意：新浪降级路径有时会返回残缺快照（仅 code 列），持久化后
    # 旧缓存也可能缺列。这里对关键列做存在性检查，缺失则视为不可用。
    snapshot_usable = (
        snapshot is not None and not snapshot.empty
        and "pct_change" in snapshot.columns and "amount" in snapshot.columns
    )
    if snapshot_usable:
        # 未开盘/停牌的行没有有效成交价（pct_change 为 NaN），
        # 它们不能算进涨跌家数，否则会显示成「全部跌停」。
        pct_all = snapshot["pct_change"]
        valid = pct_all.notna()
        pct = pct_all[valid]
        up = int((pct > 0).sum())
        down = int((pct < 0).sum())
        flat = int((pct == 0).sum())
        traded = int(valid.sum())
        total = len(snapshot)
        untraded = total - traded

        # 涨停/跌停家数：按各板块涨跌停幅度判断（留 0.1% 误差容忍）
        limit_up = 0
        limit_down = 0
        for row in snapshot.itertuples():
            limit = _limit_pct(row.code, getattr(row, "name", ""))
            v = row.pct_change
            if v is not None and not pd.isna(v):
                if v >= limit - 0.1:
                    limit_up += 1
                elif v <= -limit + 0.1:
                    limit_down += 1

        total_amount = float(snapshot["amount"].fillna(0).sum())
    else:
        up = down = flat = untraded = total = limit_up = limit_down = 0
        traded = 0
        total_amount = 0.0

    # 温度按「有成交的股票」计算；全部未成交（未开盘）时回落到中性 50
    temperature = _market_temperature(up, down, flat, limit_up, traded)
    sentiment = classify_sentiment(temperature)

    def _fnum(row, col: str) -> float | None:
        v = getattr(row, col, float("nan"))
        return None if pd.isna(v) else float(v)

    indices = []
    if indices_df is not None and not indices_df.empty:
        for row in indices_df.itertuples():
            price = _fnum(row, "price")
            pct = _fnum(row, "pct_change")
            change = _fnum(row, "change")
            # 未开盘时指数最新价为 0，涨跌幅会算成 -100%，这里统一判为无效
            if price is not None and price <= 0:
                price = pct = change = None
            indices.append({
                "symbol": getattr(row, "symbol", None),
                "name": getattr(row, "name", None),
                "price": price,
                "pct_change": pct,
                "change": change,
                "amount": _fnum(row, "amount"),
            })

    # 指数或快照任一部分不可用 → 标记部分降级（前端顶部提示）
    degraded = (not snapshot_usable) or (indices_df is None or indices_df.empty)
    return _with_disclaimer({
        "degraded": degraded,
        # SWR 会把旧快照立即返回给页面；写 now() 会让「数据更新于」变成请求时刻，
        # 把几分钟前（甚至上一交易日）的行情伪装成实时。改用快照真实落库时间。
        "updated_at": (
            snapshot_store.status()["snapshot"]["updated_at"]
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ),
        "trading": _is_trading_time(),
        "indices": indices,
        "breadth": {
            "up": up, "down": down, "flat": flat, "total": total,
            "untraded": untraded, "traded": traded,
            "limit_up": limit_up, "limit_down": limit_down,
        },
        "total_amount": round(total_amount, 2),  # 单位：元
        "temperature": temperature,
        "sentiment": sentiment,  # 5 档情绪等级（含权重与操作建议）
    })


@router.get("/indices/{symbol}/kline")
async def index_kline(
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
            # 限时拉取：数据源故障时最多 12s 返回 503，避免前端长时间挂起
            source = get_data_source()
            df = await asyncio.wait_for(
                asyncio.to_thread(source.get_index_daily, symbol, 8, 0),
                timeout=12,
            )
            if not df.empty:
                kline_cache.save(cache_key, df)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="数据源暂时不可用（超时）") from None
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
    """个股实时快照（来自全市场快照）。

    先读内存里的已就绪快照（``peek_snapshot``，不等待），没有再回源。
    原来无条件调 ``get_snapshot``：冷启动时会在请求线程里同步跑完 59 批抓取
    （约 90s），而它是**同步**端点，占住线程池会连带拖慢其它所有同步接口。
    """
    snapshot = None
    try:
        snapshot = snapshot_store.peek_snapshot()
    except Exception:  # noqa: BLE001
        snapshot = None
    if snapshot is None or snapshot.empty:
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


@router.get("/stocks/{code}/intraday")
async def stock_intraday(code: str) -> dict:
    """个股分时（逐分钟价格 / 均价 / 成交量），供详情页分时图使用。

    数据来源按日期分工，见 :func:`_intraday_source`：

    - **当日**（工作日 9:15 之后）：实时源（腾讯分钟线，约 0.1s），保证盘中实时；
    - **上一交易日及更早**（开盘前 / 周末）：读 D 盘 ``minute1_fsdb``，零联网。

    实时源失败时退回本地最近交易日；本地也没有（新股未导出）时再由实时源兜底。
    """
    code = code.zfill(6)

    now = datetime.now()
    source_used = _intraday_source(now)

    df = None
    if source_used == "local":
        try:
            from app.data.minute import get_intraday_local

            df = await asyncio.to_thread(get_intraday_local, code)
        except Exception as exc:  # noqa: BLE001 - 本地读失败不影响联网降级
            logger.warning("本地分时读取失败 code=%s: %s", code, exc)
        if df is None or df.empty:
            # 本地没有这只股票（新股 / 尚未导出）——退回实时源当兜底
            logger.info("本地无分时，转实时源 code=%s", code)
            source_used = "realtime"

    if df is None or df.empty:
        source = get_data_source()
        try:
            # 限时拉取：数据源故障时最多 15s 返回 503，避免前端长时间挂起
            df = await asyncio.wait_for(
                asyncio.to_thread(source.get_intraday, code), timeout=15
            )
        except asyncio.TimeoutError:
            df = await _intraday_local_fallback(code)
            if df is None or df.empty:
                raise HTTPException(status_code=503, detail="分时数据源暂时不可用（超时）") from None
            source_used = "local"
            logger.warning("实时分时超时，已退回本地最近交易日 code=%s", code)
        except Exception as exc:  # noqa: BLE001
            df = await _intraday_local_fallback(code)
            if df is None or df.empty:
                logger.exception("获取分时失败 code=%s", code)
                raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc
            source_used = "local"
            logger.warning("实时分时异常，已退回本地最近交易日 code=%s: %s", code, exc)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"股票 {code} 暂无分时数据")

    df = _clip_trading_minutes(df)
    # 裁剪后可能为空（例如 9:15-9:30 集合竞价期间实时源还没有分钟数据）：
    # 此时退回本地最近交易日，好过给前端一张空图或 404。
    if df is None or df.empty:
        df = await _intraday_local_fallback(code)
        if df is not None and not df.empty:
            source_used = "local"
            logger.info("实时源无分钟数据，退回本地最近交易日 code=%s", code)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"股票 {code} 暂无分时数据")

    # 昨收（分时图基准线）：只读内存快照，不触发冷启动刷新
    prev_close: float | None = None
    try:
        snapshot = snapshot_store.peek_snapshot()
        if snapshot is not None and not snapshot.empty and "code" in snapshot.columns:
            row = snapshot[snapshot["code"] == code]
            if not row.empty and "prev_close" in snapshot.columns:
                raw = row.iloc[0]["prev_close"]
                if raw is not None and not pd.isna(raw) and float(raw) > 0:
                    prev_close = float(raw)
    except Exception as exc:  # noqa: BLE001 - 昨收缺失不影响分时曲线
        logger.warning("分时昨收获取失败 code=%s: %s", code, exc)

    def _num(value, digits: int = 3) -> float | None:
        return None if value is None or pd.isna(value) else round(float(value), digits)

    def _clock(value) -> str:
        """时间统一成 HH:MM:SS —— 腾讯源给 HH:MM，本地 parquet 给 HH:MM:SS，
        两种源交替使用，不统一的话前端 x 轴刻度会跳变。"""
        text = str(value).strip()
        parts = text.split(":")
        if len(parts) == 2:
            return f"{text}:00"
        return text

    date = None
    if "date" in df.columns:
        dates = df["date"].dropna()
        if not dates.empty:
            date = str(dates.iloc[-1])
    # 实时源不带 date 列：9:15 后走实时源=当日行情，补一个今天日期，
    # 便于前端把分时曲线与当日成交标记(trade_date)按日期对齐；
    # 周末/节假日实时源返回上一交易日时会带 date，此处不覆盖。
    if not date:
        date = now.strftime("%Y-%m-%d")
    else:
        date = date.split(" ")[0]  # 兼容 "2026-11-02 00:00:00" 这类带时间的日期

    return _with_disclaimer({
        "code": code,
        "date": date,
        "source": source_used,   # realtime=当日实时 / local=D 盘历史
        "prev_close": prev_close,
        "times": [_clock(t) for t in df["time"]],
        "prices": [_num(v) for v in df["price"]],
        "avg_prices": [_num(v) for v in df["avg_price"]],
        "volumes": [0.0 if pd.isna(v) else float(v) for v in df["volume"]],
    })


@router.get("/stocks/{code}/kline")
async def stock_kline(
    code: str,
    days: int = Query(120, ge=20, le=1000),
    adjust: str = Query("qfq", pattern="^(qfq|hfq|)$"),
    refresh: bool = Query(False, description="强制刷新本地缓存"),
) -> dict:
    """个股日K线（本地 Parquet 缓存优先，增量更新）。"""
    from app.data.service import get_kline_cached

    try:
        # 限时拉取：数据源故障时最多 12s 返回 503，避免前端长时间挂起
        # （默认 _call 为 20s×3 次重试≈60s；k线接口不重试、短超时）
        df = await asyncio.wait_for(
            asyncio.to_thread(get_kline_cached, code, adjust=adjust,
                              refresh=refresh, timeout=8, retries=0),
            timeout=12,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="数据源暂时不可用（超时）") from None
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


@router.get("/search")
def market_search(
    q: str = Query("", max_length=20, description="查询词：股票代码（可带 sh/sz/bj 前缀）或名称片段"),
    limit: int = Query(20, ge=1, le=50),
) -> dict:
    """股票模糊查询（代码 / 名称）—— 首页「查股票」输入框用。

    数据来自全市场股票池 ``get_stock_pool``（带内存 + 磁盘缓存，首调用后极快）。
    匹配规则：

    - 纯数字：优先按代码前缀（``600`` → ``600xxx``），其次代码包含、名称包含；
    - 含字母 / 中文：代码包含或名称包含（会去掉 ``sh/sz/bj`` 交易所前缀后再比代码）；
    - 返回 ``total`` 为命中总数（不受 ``limit`` 截断影响），前端可显示「共 N 条」。

    股票池不可用时返回空结果 + ``error`` 字段，不拖垮查询框。
    """
    q = (q or "").strip()
    if not q:
        return _with_disclaimer({"query": q, "results": [], "total": 0})

    # 去掉交易所前缀（sh/sz/bj），便于「sh600000」「600000」都能查到
    q_norm = re.sub(r"^(sh|sz|bj)", "", q.lower())
    try:
        from app.data.service import get_stock_pool  # 延迟导入，避免循环依赖

        pool = get_stock_pool()
    except Exception as exc:  # noqa: BLE001 - 股票池不可用就返回空，不拖垮查询框
        logger.warning("股票池不可用，搜索返回空: %s", exc)
        return _with_disclaimer({"query": q, "results": [], "total": 0, "error": "股票池暂不可用"})

    if pool is None or pool.empty:
        return _with_disclaimer({"query": q, "results": [], "total": 0})

    code = pool["code"].astype(str)
    name = pool["name"].astype(str)

    if q_norm.isdigit():
        # 纯数字：代码前缀优先，其次代码包含；都为 0 才用名称兜底
        mask = code.str.startswith(q_norm) | code.str.contains(q_norm, regex=False)
        if int(mask.sum()) == 0:
            mask = name.str.contains(q_norm, regex=False)
    else:
        mask = code.str.contains(q_norm, regex=False) | name.str.contains(q, regex=False)

    total = int(mask.sum())
    matched = pool[mask].head(limit)
    results = [
        {
            "code": str(r["code"]),
            "name": str(r["name"]),
            "market": str(r["market"]) if "market" in pool.columns and pd.notna(r["market"]) else "",
        }
        for r in matched.to_dict("records")
    ]
    return _with_disclaimer({"query": q, "results": results, "total": total})
