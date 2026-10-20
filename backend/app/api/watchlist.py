"""自选股 API：CRUD + 实时行情合并展示。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.api.utils import with_disclaimer
from app.data.cache import db
from app.data.service import get_stock_pool
from app.data.snapshot_store import snapshot_store

logger = logging.getLogger("sas.api.watchlist")
router = APIRouter(prefix="/watchlist", tags=["自选股"])


@router.get("")
async def list_watchlist() -> dict:
    """自选股列表（合并实时行情）。"""
    items = await asyncio.to_thread(db.list_watchlist)

    # 合并实时快照（失败时仅返回静态列表）
    snapshot_map: dict[str, dict] = {}
    try:
        snapshot = await asyncio.to_thread(snapshot_store.get_snapshot)
        for row in snapshot.itertuples():
            snapshot_map[row.code] = {
                "price": None if pd_isna(row.price) else float(row.price),
                "pct_change": None if pd_isna(row.pct_change) else float(row.pct_change),
                "amount": None if pd_isna(row.amount) else float(row.amount),
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("自选股行情合并失败: %s", exc)

    for item in items:
        quote = snapshot_map.get(item["code"], {})
        item["price"] = quote.get("price")
        item["pct_change"] = quote.get("pct_change")

    return with_disclaimer({"items": items})


@router.post("/{code}")
async def add_watch(code: str) -> dict:
    """添加自选股（名称自动从股票池查询）。"""
    code = code.zfill(6)
    if any(item["code"] == code for item in db.list_watchlist()):
        raise HTTPException(status_code=409, detail="已在自选股中")

    name = ""
    try:
        pool = await asyncio.to_thread(get_stock_pool)
        row = pool[pool["code"] == code]
        if row.empty:
            raise HTTPException(status_code=404, detail=f"未找到股票 {code}")
        name = str(row.iloc[0]["name"])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"股票池查询失败: {exc}") from exc

    await asyncio.to_thread(db.add_watch, code, name)
    return with_disclaimer({"code": code, "name": name, "added": True})


@router.delete("/{code}")
async def remove_watch(code: str) -> dict:
    """移除自选股。"""
    await asyncio.to_thread(db.remove_watch, code.zfill(6))
    return with_disclaimer({"code": code, "removed": True})


@router.get("/{code}/signals")
def watch_signals(
    code: str,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """某只自选股的历史信号。"""
    return with_disclaimer({
        "signals": db.list_signals(code=code.zfill(6), limit=limit)
    })


def pd_isna(value) -> bool:
    """pandas NaN 判断（局部导入避免顶层依赖）。"""
    import pandas as pd

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return value is None
