"""选股 API：策略列表、扫描任务、历史信号。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.data.cache import db
from app.screener.scanner import scanner
from app.strategies import registry

router = APIRouter(prefix="/screening", tags=["选股"])


class ScanRequest(BaseModel):
    """发起扫描的请求体。"""

    strategies: list[str] = Field(..., min_length=1, description="策略名列表")
    codes: list[str] | None = Field(None, description="限定股票代码，缺省为全市场")


@router.get("/strategies")
def list_strategies() -> dict:
    """全部已注册策略的元信息。"""
    return with_disclaimer({"strategies": registry.list()})


@router.post("/scan")
async def start_scan(req: ScanRequest) -> dict:
    """启动一轮后台扫描，返回 task_id（前端轮询进度）。

    全市场扫描 5000+ 只股票在冷缓存下需要数分钟，
    设计为异步任务，避免 HTTP 超时。
    """
    unknown = [n for n in req.strategies if n not in registry.names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未注册的策略: {unknown}")
    task_id = await scanner.start_scan(req.strategies, req.codes)
    return {"task_id": task_id}


@router.get("/scan/{task_id}")
def scan_status(task_id: str) -> dict:
    """查询扫描任务进度与命中结果。"""
    task = scanner.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return with_disclaimer({
        "task_id": task.task_id,
        "status": task.status,
        "strategies": task.strategies,
        "total": task.total,
        "scanned": task.scanned,
        "progress": task.progress(),
        "skipped": task.skipped,
        "hit_count": len(task.hits),
        "hits": task.hits if task.status == "done" else [],
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    })


@router.get("/signals")
def signal_history(
    code: str | None = Query(None, description="按股票代码过滤"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """历史命中信号（SQLite 持久化）。"""
    return with_disclaimer({"signals": db.list_signals(code=code, limit=limit)})
