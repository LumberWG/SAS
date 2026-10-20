"""API 公共工具。"""
from __future__ import annotations

from app.config import get_settings


def with_disclaimer(data: dict) -> dict:
    """统一注入免责声明字段（合规要求）。"""
    data["disclaimer"] = get_settings().disclaimer
    return data
