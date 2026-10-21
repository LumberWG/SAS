"""策略管理 API：参数 schema 查询、自定义策略 CRUD。

自定义策略 = 内置策略 + 用户调整的参数，保存后可像内置策略一样
用于选股扫描与回测（注册表动态注册，下游无感）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.data.cache import db
from app.strategies import registry

logger = logging.getLogger("sas.api.strategies")
router = APIRouter(prefix="/strategies", tags=["策略管理"])


class CustomStrategyRequest(BaseModel):
    """保存自定义策略的请求体。"""

    name: str = Field(..., min_length=1, max_length=30, description="策略名（唯一）")
    base_strategy: str = Field(..., description="基础内置策略名")
    params: dict = Field(default_factory=dict, description="策略参数")
    description: str = Field("", max_length=100, description="策略说明")


@router.get("")
def list_strategies() -> dict:
    """全部策略（内置 + 自定义），含参数 schema 与默认值。"""
    return with_disclaimer({"strategies": registry.list()})


@router.get("/{name}")
def strategy_detail(name: str) -> dict:
    """单个策略详情：参数 schema + 默认值 + 是否自定义。"""
    cls = registry.get_class(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"策略不存在: {name}")
    custom = registry.is_custom(name)
    return with_disclaimer({
        "name": cls.name,
        "description": cls.description,
        "category": cls.category,
        "min_bars": cls.min_bars,
        "custom": custom,
        "custom_meta": registry.custom_meta(name) if custom else None,
        "parameters": {key: spec.to_dict() for key, spec in cls.parameters.items()},
    })


@router.post("/custom")
def create_custom_strategy(req: CustomStrategyRequest) -> dict:
    """保存自定义策略（立即注册生效，重启后自动加载）。"""
    # 名校验：不能与内置策略重名，不能与已有自定义重名
    if not registry.is_custom(req.name) and req.name in registry.names():
        raise HTTPException(status_code=409, detail=f"策略名已被内置策略占用: {req.name}")
    if registry.get_class(req.base_strategy) is None:
        raise HTTPException(status_code=400, detail=f"基础策略不存在: {req.base_strategy}")

    try:
        registry.register_dynamic(
            name=req.name,
            base_name=req.base_strategy,
            params=req.params,
            description=req.description,
        )
        db.save_custom_strategy(
            name=req.name,
            base_strategy=req.base_strategy,
            params=req.params,
            description=req.description,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return with_disclaimer({"created": True, "name": req.name})


@router.delete("/custom/{name}")
def delete_custom_strategy(name: str) -> dict:
    """删除自定义策略（内置策略不可删除）。"""
    if not registry.is_custom(name):
        raise HTTPException(status_code=400, detail=f"{name} 不是自定义策略，不能删除")
    registry.unregister(name)
    db.delete_custom_strategy(name)
    return with_disclaimer({"deleted": True, "name": name})
