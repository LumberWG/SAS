"""回测 API：提交回测、查询结果与历史记录。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.backtest.engine import BacktestEngine, BacktestParams, engine
from app.data.cache import db
from app.strategies import registry

logger = logging.getLogger("sas.api.backtest")
router = APIRouter(prefix="/backtest", tags=["回测"])


class BacktestRequest(BaseModel):
    """回测请求体。"""

    strategy: str = Field(..., description="策略名")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    initial_capital: float = Field(1_000_000, gt=0)
    max_positions: int = Field(5, ge=1, le=50)
    commission: float = Field(0.00025, ge=0, le=0.01)
    stamp_tax: float = Field(0.0005, ge=0, le=0.01)
    codes: list[str] | None = Field(None, description="限定股票代码")
    universe_size: int = Field(300, ge=10, le=6000, description="缺省股票池大小")


@router.post("/run")
async def run_backtest(req: BacktestRequest) -> dict:
    """执行回测（同步返回，缺省股票池为成交额前 universe_size 只）。"""
    if req.strategy not in registry.names():
        raise HTTPException(status_code=400, detail=f"未注册的策略: {req.strategy}")
    if req.start_date >= req.end_date:
        raise HTTPException(status_code=400, detail="start_date 必须早于 end_date")

    params = BacktestParams(
        strategy=req.strategy,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        max_positions=req.max_positions,
        commission=req.commission,
        stamp_tax=req.stamp_tax,
        codes=req.codes,
        universe_size=req.universe_size,
    )
    try:
        result = await engine.run(params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("回测执行失败")
        raise HTTPException(status_code=502, detail=f"回测失败: {exc}") from exc

    result["run_id"] = db.save_backtest(result)
    return with_disclaimer(result)


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
