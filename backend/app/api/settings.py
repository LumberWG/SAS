"""系统设置 API：查看 / 修改 / 还原运行时配置。

写模型：改动落到 `app_state` 覆盖层（`cfg:<key>`，或大盘股那几项既有的无前缀键），
同时**就地**改 `get_settings()` 单例——所有持有 `self.settings` 的实例下一个 tick 就用新值；
重启时由 `apply_persisted_overrides()` 重放，所以改完不用重启也不会丢。

不可编辑项只有路径 / 列表类（`param_specs.READONLY_KEYS`）：它们在进程启动时就被
静态挂载、CORS、缓存目录捕获，运行中改只会造成「显示改了实际没变」的错觉。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.param_specs import reset_settings, settings_overview, update_settings

logger = logging.getLogger("sas.api.settings")
router = APIRouter(prefix="/settings", tags=["系统设置"])


class UpdateBody(BaseModel):
    """批量修改：``{"changes": {"paper_max_positions": 8, "paper_auto_trade": false}}``。"""

    changes: dict[str, Any] = Field(default_factory=dict)


class ResetBody(BaseModel):
    """还原为 .env/环境变量值；``keys`` 为空表示还原全部可编辑项。"""

    keys: list[str] = Field(default_factory=list)


@router.get("")
def get_settings_overview() -> dict:
    """参数总览：Settings 全部字段 + 硬编码常量，按分组返回。"""
    return with_disclaimer(settings_overview())


@router.post("")
def patch_settings(body: UpdateBody) -> dict:
    """修改配置项（立即生效 + 落库），返回修改后的总览。"""
    if not body.changes:
        raise HTTPException(status_code=400, detail="没有要修改的项")
    try:
        applied = update_settings(body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return with_disclaimer({**settings_overview(), "applied": applied})


@router.post("/reset")
def post_reset_settings(body: ResetBody) -> dict:
    """还原配置：``keys`` 为空 = 还原全部可编辑项。"""
    done = reset_settings(body.keys or None)
    return with_disclaimer({**settings_overview(), "reset": done})
