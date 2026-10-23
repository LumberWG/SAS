"""选股 API：策略列表、扫描任务、历史信号、命中统计。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.data.cache import db
from app.screener.scanner import SCAN_SCOPES, scanner, warmup_klines, kline_cache_stats
from app.strategies import registry

router = APIRouter(prefix="/screening", tags=["选股"])


class ScanRequest(BaseModel):
    """发起扫描的请求体。"""

    strategies: list[str] = Field(..., min_length=1, description="策略名列表")
    codes: list[str] | None = Field(None, description="限定股票代码，缺省按 scope")
    #: 扫描范围: all / sh / sz / cyber / star / bjs / top_amount:N
    scope: str = Field("all", description="扫描范围")


@router.get("/strategies")
def list_strategies() -> dict:
    """全部已注册策略的元信息 + 今日命中数 + 可用扫描范围。"""
    today = datetime.now().strftime("%Y-%m-%d")
    stats = db.signal_stats_by_date(today)
    return with_disclaimer({
        "strategies": registry.list(),
        "today_hits": stats,
        "scopes": [
            {"key": key, "label": info["label"]}
            for key, info in SCAN_SCOPES.items()
        ],
    })


@router.post("/scan")
async def start_scan(req: ScanRequest) -> dict:
    """启动一轮后台扫描，返回 task_id（前端轮询进度）。

    全市场扫描 5000+ 只股票在冷缓存下需要数分钟，
    设计为异步任务，避免 HTTP 超时。
    """
    unknown = [n for n in req.strategies if n not in registry.names()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未注册的策略: {unknown}")
    task_id = await scanner.start_scan(req.strategies, req.codes, req.scope)
    return {"task_id": task_id}


@router.get("/kline-status")
async def kline_status() -> dict:
    """K线缓存覆盖状态：已缓存只数 / 股票池总数 / 最近一次预热结果。"""
    return with_disclaimer(await asyncio.to_thread(kline_cache_stats))


@router.post("/warmup")
async def trigger_warmup() -> dict:
    """手动触发全市场K线预热（后台执行，不阻塞请求）。

    预热把股票日K增量缓存到本地 Parquet，完成后扫描直接读本地缓存，
    消除实时网络拉取与数据源限流带来的等待。
    """
    asyncio.create_task(warmup_klines())
    return with_disclaimer(
        {"started": True, "message": "K线预热已在后台开始，完成后扫描将使用本地缓存"}
    )


@router.get("/scan/latest")
def latest_scan() -> dict:
    """最近一轮扫描任务（供前端刷新页面后恢复结果）。"""
    task = scanner.latest_task()
    if task is None:
        return with_disclaimer({"task": None})
    return with_disclaimer({"task": _task_payload(task)})


@router.get("/scan/{task_id}")
def scan_status(task_id: str) -> dict:
    """查询扫描任务进度与命中结果。"""
    task = scanner.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return with_disclaimer(_task_payload(task))


def _task_payload(task) -> dict:
    """任务状态序列化（含耗时统计）。"""
    duration = None
    if task.started_at and task.finished_at:
        try:
            start = datetime.strptime(task.started_at, "%Y-%m-%d %H:%M:%S")
            finish = datetime.strptime(task.finished_at, "%Y-%m-%d %H:%M:%S")
            duration = int((finish - start).total_seconds())
        except ValueError:
            pass
    return {
        "task_id": task.task_id,
        "status": task.status,
        "strategies": task.strategies,
        "scope": task.scope,
        "total": task.total,
        "scanned": task.scanned,
        "progress": task.progress(),
        "skipped": task.skipped,
        "hit_count": len(task.hits),
        "hits": task.hits if task.status == "done" else [],
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "duration": duration,
        "sentiment": task.sentiment,  # 扫描开始时市场情绪快照
    }


@router.get("/signals")
def signal_history(
    code: str | None = Query(None, description="按股票代码过滤"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    """历史命中信号（SQLite 持久化）。"""
    return with_disclaimer({"signals": db.list_signals(code=code, limit=limit)})
