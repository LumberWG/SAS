"""智能建议 API：建议买入/卖出列表、明日关注、概况。"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query

from app.api.utils import with_disclaimer
from app.config import get_settings
from app.data.cache import db
from app.monitor import is_trading_hours, is_trading_day

logger = logging.getLogger("sas.api.advisory")
router = APIRouter(prefix="/advisory", tags=["智能建议"])


@router.get("/buy")
def buy_recommendations() -> dict:
    """建议买入列表（今日最新批次：盘中 > 开盘前）。"""
    items = _latest_by_action(("intraday", "premarket"), "buy")
    return with_disclaimer({
        "items": items,
        "batch": _batch_of(items),
        "updated_at": _latest_time(items),
    })


@router.get("/sell")
def sell_recommendations() -> dict:
    """建议卖出列表（今日最新批次）。"""
    items = _latest_by_action(("intraday", "premarket"), "sell")
    return with_disclaimer({
        "items": items,
        "batch": _batch_of(items),
        "updated_at": _latest_time(items),
    })


@router.get("/focus")
def focus_list() -> dict:
    """明日关注（盘后生成，含生成日期）。"""
    items = db.list_recommendations("focus", "focus")
    batch = db.latest_batch("focus")
    return with_disclaimer({
        "items": items,
        "batch": batch,
        # focus_2026-10-21 → 对应 2026-10-22 开盘关注
        "for_date": batch.split("_", 1)[1] if batch else None,
        "updated_at": _latest_time(items),
    })


@router.get("/summary")
def advisory_summary() -> dict:
    """建议概况（角标数字 + 市场状态）。"""
    settings = get_settings()
    buy = db.list_recommendations("intraday", "buy") or \
        db.list_recommendations("premarket", "buy")
    sell = db.list_recommendations("intraday", "sell") or \
        db.list_recommendations("premarket", "sell")
    focus = db.list_recommendations("focus", "focus")
    return with_disclaimer({
        "buy_count": len(buy),
        "sell_count": len(sell),
        "focus_count": len(focus),
        "focus_batch": db.latest_batch("focus"),
        "trading_hours": is_trading_hours(),
        "trading_day": is_trading_day(),
        "auto_watch_enabled": settings.auto_watch_enabled,
        "auto_watch_top_n": settings.auto_watch_top_n,
    })


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _latest_by_action(prefixes: tuple[str, ...], action: str) -> list[dict]:
    """按优先级取今日批次的建议（盘中 > 开盘前；非今日批次视为过期）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    for prefix in prefixes:
        items = db.list_recommendations(prefix, action)
        # 只返回今日批次（过期建议不展示，避免周末看到上周五的盘中建议）
        if items and items[0]["batch"].endswith(today):
            return items
    return []


def _batch_of(items: list[dict]) -> str | None:
    return items[0]["batch"] if items else None


def _latest_time(items: list[dict]) -> str | None:
    return items[0]["created_at"] if items else None
