"""监控 API：异动提醒列表与调度器状态。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.api.utils import with_disclaimer
from app.monitor.scheduler import is_trading_day, is_trading_hours, scheduler

logger = logging.getLogger("sas.api.monitor")
router = APIRouter(prefix="/monitor", tags=["监控"])


@router.get("/alerts")
def list_alerts(limit: int = Query(30, ge=1, le=100)) -> dict:
    """最近市场异动（涨停/跌停/放量突破/急涨急跌，新→旧）。"""
    return with_disclaimer({
        "alerts": scheduler.alerts[:limit],
        "trading_hours": is_trading_hours(),
        "trading_day": is_trading_day(),
    })


@router.get("/status")
def monitor_status() -> dict:
    """调度器状态（任务清单与下次执行时间）。"""
    return with_disclaimer(scheduler.status())


@router.post("/scan/auto")
async def trigger_auto_scan() -> dict:
    """手动触发收盘自动选股（与定时任务同一入口）。"""
    result = await scheduler.auto_scan()
    return with_disclaimer(result)
