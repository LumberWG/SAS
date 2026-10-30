"""系统设置 API：查看 / 修改 / 还原运行时配置。

写模型：改动落到 `app_state` 覆盖层（`cfg:<key>`，或大盘股那几项既有的无前缀键），
同时**就地**改 `get_settings()` 单例——所有持有 `self.settings` 的实例下一个 tick 就用新值；
重启时由 `apply_persisted_overrides()` 重放，所以改完不用重启也不会丢。

不可编辑项只有路径 / 列表类（`param_specs.READONLY_KEYS`）：它们在进程启动时就被
静态挂载、CORS、缓存目录捕获，运行中改只会造成「显示改了实际没变」的错觉。

每次写操作都会追加一条审计记录（`app.audit`）——多环境部署后，
「谁在什么时候还原了哪几项」只能靠日志复盘。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.audit import log_settings_change
from app.param_specs import (
    SECRET_KEYS,
    mask_secret,
    reset_settings,
    settings_overview,
    update_settings,
)

logger = logging.getLogger("sas.api.settings")
router = APIRouter(prefix="/settings", tags=["系统设置"])


class UpdateBody(BaseModel):
    """批量修改：``{"changes": {"paper_max_positions": 8, "paper_auto_trade": false}}``。"""

    changes: dict[str, Any] = Field(default_factory=dict)


class ResetBody(BaseModel):
    """还原为 .env/环境变量值；``keys`` 为空表示还原全部可编辑项。"""

    keys: list[str] = Field(default_factory=list)


def _client(request: Request) -> tuple[str, str]:
    """取来源 IP 与 UA，写审计用（反向代理下取 X-Forwarded-For 首个地址）。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else ""
    )
    return ip, request.headers.get("user-agent", "")


def _audit_values(values: dict[str, Any]) -> dict[str, Any]:
    """审计里的值：密钥类只留掩码，其余原样（便于回看改了哪些数）。"""
    return {
        key: mask_secret(value) if key in SECRET_KEYS else value
        for key, value in values.items()
    }


@router.get("")
def get_settings_overview() -> dict:
    """参数总览：Settings 全部字段 + 硬编码常量，按分组返回。"""
    return with_disclaimer(settings_overview())


@router.post("")
def patch_settings(body: UpdateBody, request: Request) -> dict:
    """修改配置项（立即生效 + 落库），返回修改后的总览。"""
    if not body.changes:
        raise HTTPException(status_code=400, detail="没有要修改的项")
    try:
        applied = update_settings(body.changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ip, ua = _client(request)
    log_settings_change(
        action="update",
        keys=list(applied),
        values=_audit_values(applied),
        ip=ip,
        user_agent=ua,
    )
    return with_disclaimer({**settings_overview(), "applied": applied})


@router.post("/reset")
def post_reset_settings(body: ResetBody, request: Request) -> dict:
    """还原配置：``keys`` 为空 = 还原全部可编辑项。

    目标值是「**当前磁盘上的** `.env` / 环境变量」，没有 `.env` 的机器上等于
    `config.py` 默认值 —— 于是「还原」在不同机器上的结果并不相同，
    这是按发布基线配置 `.env` 的依据（见 README「部署」章节）。
    """
    done = reset_settings(body.keys or None)
    ip, ua = _client(request)
    log_settings_change(
        action="reset",
        keys=done,
        values={},
        ip=ip,
        user_agent=ua,
    )
    return with_disclaimer({**settings_overview(), "reset": done})
