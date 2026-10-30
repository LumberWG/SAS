"""系统设置 API（只读）：返回全部参数的**当前生效值 + 来源 + 生效方式**。

为什么只读：配置里有大量「改了必须重启」的项（路径、并发、数据源），做成可点的开关
只会制造「我点了为什么没变」；真正的动态开关（大盘股过滤、模拟盘模式）已经在各自页面，
且都走 `app_state` 覆盖层。可写化按需再开，见 `app/param_specs.RUNTIME_KEYS`。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.utils import with_disclaimer
from app.param_specs import settings_overview

logger = logging.getLogger("sas.api.settings")
router = APIRouter(prefix="/settings", tags=["系统设置"])


@router.get("")
def get_settings_overview() -> dict:
    """参数总览：78 项 Settings + 硬编码常量，按分组返回。"""
    return with_disclaimer(settings_overview())
