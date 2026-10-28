"""可交易范围（沪深主板+创业板，剔 ST/巨无霸）规模接口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.data.scope import get_universe_stats

router = APIRouter(tags=["scope"])


@router.get("/scope/stats")
def scope_stats() -> dict:
    """当前可交易范围股票总数、板块拆分与排除分布（供前端展示）。

    返回字段：source_total（全 A 池）/ in_scope（范围内总数）/
    mainboard（主板）/ cyb（创业板）/ excluded（各类排除原因计数）。
    """
    return get_universe_stats()
