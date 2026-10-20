"""LLM 智能分析 API：个股决策仪表盘。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.api.utils import with_disclaimer
from app.config import get_settings
from app.data.service import get_kline_cached, get_stock_pool
from app.data.snapshot_store import snapshot_store
from app.llm.analyzer import analyzer
from app.llm.client import llm_client

logger = logging.getLogger("sas.api.analysis")
router = APIRouter(prefix="/analysis", tags=["智能分析"])


async def _lookup_name_snapshot(code: str) -> tuple[str, dict | None]:
    """查询股票名称与实时快照字段（失败返回空，不影响主流程）。"""
    name = ""
    snapshot = None
    try:
        snap_df = await asyncio.to_thread(snapshot_store.get_snapshot)
        row = snap_df[snap_df["code"] == code]
        if not row.empty:
            name = str(row.iloc[0].get("name", ""))
            snapshot = row.iloc[0].to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("快照查询失败 code=%s: %s", code, exc)
    if not name:
        try:
            pool = await asyncio.to_thread(get_stock_pool)
            prow = pool[pool["code"] == code]
            if not prow.empty:
                name = str(prow.iloc[0]["name"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("股票池查询失败 code=%s: %s", code, exc)
    return name, snapshot


@router.get("/status")
def analysis_status() -> dict:
    """LLM 模块状态（未配置时前端提示将使用规则降级结论）。"""
    settings = get_settings()
    return with_disclaimer({
        "llm_available": llm_client.available,
        "model": settings.llm_model if llm_client.available else "rule-based",
        "note": "未配置 LLM 时自动使用技术面规则评分，功能不受影响",
    })


@router.post("/{code}")
async def analyze_stock(
    code: str,
    refresh: bool = Query(False, description="强制刷新K线缓存"),
) -> dict:
    """生成个股决策仪表盘（LLM 优先，失败自动降级规则评分）。"""
    try:
        df = await asyncio.to_thread(get_kline_cached, code, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取K线失败 code=%s", code)
        raise HTTPException(status_code=502, detail=f"数据源异常: {exc}") from exc

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"股票 {code} 无K线数据")

    name, snapshot = await _lookup_name_snapshot(code)

    try:
        dashboard = await analyzer.analyze(code, name, df, snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("分析失败 code=%s", code)
        raise HTTPException(status_code=502, detail=f"分析失败: {exc}") from exc

    return with_disclaimer(dashboard.to_dict())
