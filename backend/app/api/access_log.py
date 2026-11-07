"""访客访问记录查询接口。

默认只返回**真实用户**的访问（已排除开发者调试流量），并支持按 IP 去重统计独立访客。
记录落盘见 `app.accesslog`（`<数据目录>/logs/access_YYYYMMDD.jsonl`）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from app.api.utils import with_disclaimer
from app.config import get_settings

logger = logging.getLogger("sas.api.access_log")
router = APIRouter(prefix="/access-log", tags=["访问记录"])


def _read_recent(days: int) -> list[dict]:
    """读取最近 days 天的访问记录（新→旧）。"""
    root = get_settings().db_path.parent / "logs"
    records: list[dict] = []
    for offset in range(days):
        day = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        path = root / f"access_{day}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


@router.get("")
def get_access_log(
    limit: int = Query(100, ge=1, le=2000, description="返回条数上限"),
    days: int = Query(1, ge=1, le=30, description="回溯天数"),
    exclude_dev: bool = Query(True, description="排除开发者/调试流量，只看真实访客"),
    unique: bool = Query(False, description="按 IP 去重，统计独立访客"),
):
    """最近访客访问记录。

    - ``exclude_dev=true``（默认）：剔除 localhost / 局域网 / `dev_ips` / `X-Dev-Key` 命中项；
    - ``unique=true``：同一 IP 只保留最新一条，便于看「都有哪些人」。
    """
    records = _read_recent(days)
    if exclude_dev:
        records = [r for r in records if not r.get("is_dev")]
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)

    unique_visitors = None
    if unique:
        seen: dict[str, dict] = {}
        for r in records:
            ip = r.get("ip", "")
            if ip not in seen:
                seen[ip] = r
        records = list(seen.values())
        unique_visitors = len(seen)

    total = len(records)
    return with_disclaimer({
        "total": total,
        "unique_visitors": unique_visitors,
        "records": records[:limit],
    })
