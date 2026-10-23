"""模拟交易 API：账户、持仓、流水、净值曲线、重置。"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.utils import with_disclaimer
from app.config import get_settings
from app.data.cache import db
from app.paper.engine import PaperEngine

logger = logging.getLogger("sas.api.paper")
router = APIRouter(prefix="/paper", tags=["模拟交易"])

engine = PaperEngine()


@router.get("/account")
async def paper_account() -> dict:
    """账户详情：现金/市值/总资产/收益率/持仓浮盈。"""
    detail = await engine.account_detail()
    return with_disclaimer(detail)


@router.get("/trades")
def paper_trades(limit: int = 100, code: str | None = None) -> dict:
    """交易流水（按时间倒序，可按股票过滤）。"""
    trades = db.list_paper_trades(limit=limit, code=code)
    return with_disclaimer({"trades": trades})


@router.get("/equity")
def paper_equity(limit: int = 250) -> dict:
    """净值曲线（每日总资产快照，按日期升序）。"""
    equity = db.list_paper_equity(limit=limit)
    return with_disclaimer({"equity": equity})


@router.post("/reset")
async def paper_reset() -> dict:
    """重置模拟账户（清空持仓/流水/净值，资金恢复初始值）。"""
    settings = get_settings()
    db.reset_paper_account(settings.paper_initial_capital)
    detail = await engine.account_detail()
    return with_disclaimer({"reset": True, "account": detail})
