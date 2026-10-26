"""模拟交易 API：账户、持仓、流水、净值曲线、实时撮合状态、手动下单、重置。"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.utils import with_disclaimer
from app.config import get_settings
from app.data.cache import db
from app.paper.engine import PaperEngine
from app.paper.live import live_trader

logger = logging.getLogger("sas.api.paper")
router = APIRouter(prefix="/paper", tags=["模拟交易"])

engine = PaperEngine()


class ModeBody(BaseModel):
    """交易模式切换请求。"""

    auto: bool


class OrderBody(BaseModel):
    """手动下单请求。"""

    code: str
    side: Literal["buy", "sell"]
    shares: int | None = None      # 买入/卖出股数（卖出留空=全部）
    amount: float | None = None    # 买入金额（元），优先于 shares
    name: str = ""


@router.get("/account")
async def paper_account() -> dict:
    """账户详情：现金/市值/总资产/收益率/持仓浮盈。"""
    detail = await engine.account_detail()
    return with_disclaimer(detail)


@router.get("/live")
def paper_live() -> dict:
    """实时交易引擎状态：模式、撮合时间、待执行信号、最近成交、报价。"""
    return with_disclaimer(live_trader.status())


@router.post("/mode")
def paper_mode(body: ModeBody) -> dict:
    """切换自动/手动交易模式（持久化，重启后保留）。

    - auto：系统按建议实时撮合 + 盘中止盈止损，人工不干预；
    - manual：只生成/展示建议，撮合与止盈止损全部暂停，由用户手动下单。
    """
    return with_disclaimer(live_trader.set_mode(body.auto))


@router.post("/order")
async def paper_order(body: OrderBody) -> dict:
    """手动下单：按实时价立即成交（不受自动/手动开关影响）。"""
    code = body.code.strip().zfill(6)
    result = await engine.place_order(
        code, body.side, shares=body.shares, amount=body.amount, name=body.name,
    )
    detail = await engine.account_detail()
    return with_disclaimer({"result": asdict(result), "account": detail})


@router.post("/tick")
async def paper_tick_now() -> dict:
    """立即撮合一次（手动触发，不必等 5s 周期；非交易时段/手动模式返回 skipped）。"""
    return with_disclaimer(await live_trader.tick())


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
    """重置模拟账户（清空持仓/流水/净值/待执行队列，资金恢复初始值）。"""
    settings = get_settings()
    db.reset_paper_account(settings.paper_initial_capital)
    live_trader.clear()
    detail = await engine.account_detail()
    return with_disclaimer({"reset": True, "account": detail})
