"""回测 API：提交「全流水线回测」（异步任务 + 轮询）、查询进度与历史记录。

回测固定复刻线上模拟盘的完整链路（全部策略扫描 → 加自选 → 14因子排序
→ 评分加权下单），不再单独选策略，只由用户指定时间段。

回测要逐只股票逐日滚动评估策略，几百只 × 一年量级往往需要 1~3 分钟，
同步接口必然撞上前端/代理超时，因此改为「提交任务 → 轮询状态」。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.backtest.pipeline import PipelineParams, run_pipeline
from app.data.cache import db

logger = logging.getLogger("sas.api.backtest")
router = APIRouter(prefix="/backtest", tags=["回测"])

#: 内存任务表：job_id -> {status, created, elapsed, result, error}
#: 结果同时落库（db.save_backtest），这里只负责「进行中」的进度查询
_JOBS: dict[str, dict] = {}
#: 任务结果在内存里保留的秒数（超过后只能按 run_id 查历史）
_JOB_TTL = 3600.0


class BacktestRequest(BaseModel):
    """回测请求体：只有时间段是必填，其余走线上默认值。"""

    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    initial_capital: float | None = Field(
        None, gt=0, description="初始资金，缺省取模拟盘配置"
    )
    max_positions: int | None = Field(
        None, ge=1, le=50, description="最大持仓数，缺省取模拟盘配置"
    )
    universe_size: int = Field(
        500, ge=10, le=6000, description="每日成交额 TopN 股票池"
    )


def _prune() -> None:
    """清理过期任务，避免内存表无限增长。"""
    now = time.time()
    for job_id in [k for k, v in _JOBS.items() if now - v["created"] > _JOB_TTL]:
        _JOBS.pop(job_id, None)


async def _execute(job_id: str, params: PipelineParams) -> None:
    """后台执行回测，完成后写入任务表与数据库。"""
    job = _JOBS[job_id]
    try:
        result = await run_pipeline(params)
        result["run_id"] = await asyncio.to_thread(db.save_backtest, result)
        job.update(
            status="done",
            result=result,
            elapsed=round(time.time() - job["created"], 1),
        )
        logger.info(
            "流水线回测完成 %s（%s~%s）：%d 笔交易，耗时 %.1fs",
            job_id, params.start_date, params.end_date,
            len(result.get("trades", [])), job["elapsed"],
        )
    except Exception as exc:  # noqa: BLE001 - 失败原因回传给前端
        logger.exception("流水线回测执行失败 %s", job_id)
        job.update(
            status="error",
            error=str(exc),
            elapsed=round(time.time() - job["created"], 1),
        )


@router.post("/run")
async def run_backtest(req: BacktestRequest) -> dict:
    """提交全流水线回测任务，立即返回 job_id（不阻塞 HTTP 连接）。"""
    if req.start_date >= req.end_date:
        raise HTTPException(status_code=400, detail="start_date 必须早于 end_date")

    _prune()
    # 同一时刻只跑一个回测（避免重复点击/多标签页把 CPU 打满）；
    # 已有任务在跑时直接复用，前端继续轮询同一个 job。
    for job_id, job in _JOBS.items():
        if job["status"] == "running":
            return {"job_id": job_id, "status": "running", "reused": True}

    params = PipelineParams(
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        max_positions=req.max_positions,
        universe_size=req.universe_size,
    )
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "status": "running",
        "created": time.time(),
        "elapsed": 0.0,
        "result": None,
        "error": "",
    }
    asyncio.create_task(_execute(job_id, params))
    return {"job_id": job_id, "status": "running"}


@router.get("/jobs/{job_id}")
def get_backtest_job(job_id: str) -> dict:
    """查询回测任务状态；done 时带上完整结果。"""
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="回测任务不存在或已过期")
    payload: dict = {
        "job_id": job_id,
        "status": job["status"],
        "elapsed": job["elapsed"],
        "error": job["error"],
    }
    if job["status"] == "done" and job["result"] is not None:
        payload["result"] = with_disclaimer(job["result"])
    return payload


@router.get("/{run_id}")
def get_backtest(run_id: int) -> dict:
    """查询历史回测结果。"""
    result = db.get_backtest(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return with_disclaimer(result)


@router.get("")
def list_backtests(limit: int = Query(50, ge=1, le=200)) -> dict:
    """历史回测记录列表。"""
    return with_disclaimer({"runs": db.list_backtests(limit=limit)})
