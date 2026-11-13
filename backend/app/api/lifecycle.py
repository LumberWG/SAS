"""六阶段生命周期事件查询接口（#191）。

提供只读查询：按股票或全局拉取 lifecycle_events，以及按阶段聚合的事件数，
供前端「生命周期」页展示「初选 → 入自选 → 进队列 → 入场 → 持有 → 离场」完整旅程。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.utils import with_disclaimer
from app.data.cache import db

router = APIRouter(prefix="/lifecycle", tags=["生命周期"])


@router.get("/events")
def list_lifecycle_events(
    code: str | None = Query(None, description="股票代码；省略则返回全局最近事件"),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """查询生命周期事件（按时间倒序）。"""
    events = db.list_lifecycle_events(code=code, limit=limit)
    return with_disclaimer({"code": code, "count": len(events), "events": events})


@router.get("/stages")
def lifecycle_stage_summary() -> dict:
    """按阶段聚合事件数（六阶段概览卡）。"""
    rows = db.count_lifecycle_events_by_stage()
    # 补全缺失阶段（0 计），保证前端六阶段始终有 6 张卡
    present = {int(r["stage"]) for r in rows}
    summary = [
        {"stage": s, "stage_name": n, "cnt": 0}
        for s, n in [(1, "初选"), (2, "入自选"), (3, "进交易队列"),
                     (4, "入场"), (5, "持有"), (6, "离场")]
        if s not in present
    ]
    return with_disclaimer({"stages": rows + summary})
