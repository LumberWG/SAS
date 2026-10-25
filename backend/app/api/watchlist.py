"""自选股 API：CRUD + 程序/手动来源区分 + 批量转入 + 实时行情合并展示。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.data.cache import db
from app.data.service import get_stock_pool
from app.data.snapshot_store import snapshot_store

logger = logging.getLogger("sas.api.watchlist")
router = APIRouter(prefix="/watchlist", tags=["自选股"])

#: 允许的自选股来源
VALID_SOURCES = ("auto", "manual")


class WatchBatchRequest(BaseModel):
    """批量加入自选股（扫描命中 → 程序自选）。"""

    codes: list[str] = Field(default_factory=list)
    source: str = Field(default="auto", description="auto=程序自选 / manual=手动自选")
    note: str = ""


class WatchPromoteRequest(BaseModel):
    """把程序自选转入手动自选。"""

    codes: list[str] = Field(default_factory=list)


def _normalize(codes: list[str]) -> list[str]:
    return [str(c).strip().zfill(6) for c in codes if str(c).strip()]


def _names_from_pool(codes: list[str]) -> dict[str, str]:
    """从股票池补齐名称（池子不可用时退化为空名称）。"""
    try:
        pool = get_stock_pool()
        wanted = set(codes)
        rows = pool[pool["code"].isin(wanted)]
        return {str(r["code"]): str(r["name"]) for _, r in rows.iterrows()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("股票池查询失败，自选股名称留空: %s", exc)
        return {}


def _attach_quotes(items: list[dict]) -> None:
    """合并内存快照的实时价（不触发冷启动刷新）。"""
    snapshot_map: dict[str, dict] = {}
    try:
        snapshot = snapshot_store.peek_snapshot()
        if (
            snapshot is not None and not snapshot.empty
            and "price" in snapshot.columns and "pct_change" in snapshot.columns
        ):
            for row in snapshot.itertuples():
                snapshot_map[row.code] = {
                    "price": None if pd_isna(getattr(row, "price", None)) else float(row.price),
                    "pct_change": None if pd_isna(getattr(row, "pct_change", None)) else float(row.pct_change),
                    "amount": None if pd_isna(getattr(row, "amount", None)) else float(row.amount),
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("自选股行情合并跳过（%s），返回静态列表", exc)

    for item in items:
        quote = snapshot_map.get(item["code"], {})
        item["price"] = quote.get("price")
        item["pct_change"] = quote.get("pct_change")


@router.get("")
async def list_watchlist(
    source: str | None = Query(None, description="auto / manual，缺省返回全部"),
) -> dict:
    """自选股列表（区分程序自选 / 手动自选，合并实时行情）。"""
    if source is not None and source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"source 只能是 {VALID_SOURCES}")

    items = await asyncio.to_thread(db.list_watchlist, source)
    _attach_quotes(items)

    all_items = items if source is None else await asyncio.to_thread(db.list_watchlist)
    counts = {
        "auto": sum(1 for i in all_items if i.get("source") == "auto"),
        "manual": sum(1 for i in all_items if i.get("source") == "manual"),
        "total": len(all_items),
    }
    return with_disclaimer({"items": items, "counts": counts})


@router.post("/batch")
async def add_watch_batch(payload: WatchBatchRequest) -> dict:
    """批量加入自选股（选股中心扫描命中 → 程序自选）。"""
    if payload.source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"source 只能是 {VALID_SOURCES}")
    codes = _normalize(payload.codes)
    if not codes:
        return with_disclaimer({"added": 0, "codes": []})

    names = await asyncio.to_thread(_names_from_pool, codes)
    items = [(code, names.get(code, code)) for code in dict.fromkeys(codes)]
    added = await asyncio.to_thread(db.add_watch_many, items, payload.source, payload.note)
    return with_disclaimer({
        "added": added,
        "codes": [code for code, _ in items],
        "source": payload.source,
    })


@router.post("/promote")
async def promote_watch(payload: WatchPromoteRequest) -> dict:
    """把程序自选转入手动自选。"""
    codes = _normalize(payload.codes)
    if not codes:
        return with_disclaimer({"promoted": 0})
    promoted = await asyncio.to_thread(db.set_watch_source, codes, "manual")
    return with_disclaimer({"promoted": promoted, "codes": codes})


@router.post("/{code}")
async def add_watch(
    code: str,
    source: str = Query("manual", description="auto / manual，默认手动自选"),
) -> dict:
    """添加自选股（名称自动从股票池查询）。"""
    if source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"source 只能是 {VALID_SOURCES}")
    code = code.zfill(6)

    existing = {item["code"]: item for item in db.list_watchlist()}
    if code in existing:
        if source == "manual" and existing[code].get("source") != "manual":
            # 已在程序自选中：转为手动自选，避免返回 409 让用户无路可走
            await asyncio.to_thread(db.set_watch_source, [code], "manual")
            return with_disclaimer({"code": code, "name": existing[code]["name"],
                                    "added": True, "source": "manual"})
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

    await asyncio.to_thread(db.add_watch, code, name, "", source)
    return with_disclaimer({"code": code, "name": name, "added": True, "source": source})


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
