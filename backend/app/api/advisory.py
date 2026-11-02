"""智能建议 API：建议买入/卖出列表、明日关注、概况。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from app.api.utils import with_disclaimer, with_realtime
from app.config import get_settings
from app.data.cache import db
from app.monitor import is_trading_hours, is_trading_day

logger = logging.getLogger("sas.api.advisory")
router = APIRouter(prefix="/advisory", tags=["智能建议"])


@router.get("/buy")
def buy_recommendations() -> dict:
    """建议买入列表（今日最新批次：盘中 > 开盘前）。

    ``items[].price`` 是建议生成时的价格；``items[].realtime`` 是当前行情，
    前端优先展示 realtime，避免看到几分钟（或昨天）的价格。
    """
    items = _latest_by_action(("intraday", "premarket"), "buy")
    return with_disclaimer({
        "items": with_realtime(_with_detail(items)),
        "batch": _batch_of(items),
        "updated_at": _latest_time(items),
    })


@router.get("/sell")
def sell_recommendations() -> dict:
    """建议卖出列表（仅含实际持仓的票；没持仓的卖出信号不展示）。"""
    items = _sell_items()
    return with_disclaimer({
        "items": with_realtime(_with_detail(items)),
        "batch": _batch_of(items),
        "updated_at": _latest_time(items),
    })


@router.get("/focus")
def focus_list() -> dict:
    """明日关注（盘后生成，含生成日期；附带当前行情）。"""
    items = db.list_recommendations("focus", "focus")
    batch = db.latest_batch("focus")
    return with_disclaimer({
        "items": with_realtime(_with_detail(items)),
        "batch": batch,
        # focus_<生成日> 对应下一交易日开盘关注，for_date = 生成日之后的首个交易日
        "for_date": _focus_for_date(batch),
        "updated_at": _latest_time(items),
    })


@router.get("/summary")
def advisory_summary() -> dict:
    """建议概况（角标数字 + 市场状态）。"""
    settings = get_settings()
    # 与 /buy、/sell 列表同一套「今日批次」逻辑，避免角标数字与列表内容日期错配
    # （当天首笔盘中/开盘分析跑出前，未加今日守卫会显示昨天的买入数而列表已空）。
    buy = _latest_by_action(("intraday", "premarket"), "buy")
    sell = _sell_items()
    # 明日关注取最新批次即可（盘后生成、对应下一开盘日，本就不该按今日过滤）
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


def _with_detail(items: list[dict]) -> list[dict]:
    """把 detail（JSON 文本）解析为因子分项数组，rank 统一为整数。"""
    out: list[dict] = []
    for item in items:
        row = dict(item)
        raw = row.get("detail")
        try:
            row["detail"] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            row["detail"] = []
        try:
            row["rank"] = int(row.get("rank") or 0)
        except (TypeError, ValueError):
            row["rank"] = 0
        out.append(row)
    return out


def _batch_of(items: list[dict]) -> str | None:
    return items[0]["batch"] if items else None


def _latest_time(items: list[dict]) -> str | None:
    return items[0]["created_at"] if items else None


def _held_codes() -> set[str]:
    """当前持仓代码集合（建议卖出只展示持仓股）。"""
    try:
        return {str(p["code"]) for p in db.list_paper_positions()}
    except Exception:
        return set()


def _sell_items() -> list[dict]:
    """今日建议卖出中、实际持仓的票（没持仓的卖出信号不展示）。

    自选股里没持仓的票若因子转空也会产出卖出建议，但那只是偏空警示、
    没法真卖；展示层只保留持仓股，避免「建议卖出」出现不能卖的票。
    （非持仓的弱势票已由 scheduler._route_sell_signals 自动移出自选。）
    """
    items = _latest_by_action(("intraday", "premarket"), "sell")
    held = _held_codes()
    return [it for it in items if str(it.get("code")) in held]


def _focus_for_date(batch: str | None) -> str | None:
    """focus_<生成日> 对应下一交易日的开盘关注，返回生成日之后的第一个交易日。

    盘后(15:35)生成，本意是「次日开盘重点关注」；直接取批次日期会显示成生成日
    当天（如 11-02 生成却显示 11-02 开盘），这里 +1 天并跳过周末得到真正的下一开盘日。
    """
    if not batch:
        return None
    try:
        d = datetime.strptime(batch.split("_", 1)[1], "%Y-%m-%d")
    except (ValueError, IndexError):
        return None
    for _ in range(7):
        d = d + timedelta(days=1)
        if is_trading_day(d):
            return d.strftime("%Y-%m-%d")
    return None
