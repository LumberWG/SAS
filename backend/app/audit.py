"""系统设置审计日志。

为什么要留痕：设置页能改交易开关、费率、止损与 LLM 密钥，而「还原」会一次性把
全部覆盖清掉。多环境部署（本机 / 公网隧道 / 交付出去的机器）出问题时，
「谁在什么时候改了或还原了哪几项」是唯一能复盘的东西。

格式：`<数据目录>/logs/settings_audit_YYYYMMDD.jsonl`，一行一个 JSON，
按天切分避免无限增长；密钥类只记掩码，**不落明文**。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger("sas.audit")


def audit_dir() -> Path:
    """审计日志目录：跟数据库同级的 `logs/`（随数据目录走，便于整机迁移）。"""
    return get_settings().db_path.parent / "logs"


def audit_path() -> Path:
    """当天审计文件（按日期切分）。"""
    return audit_dir() / f"settings_audit_{datetime.now():%Y%m%d}.jsonl"


def log_settings_change(
    *,
    action: str,
    keys: list[str],
    values: dict[str, Any] | None = None,
    ip: str = "",
    user_agent: str = "",
) -> Path | None:
    """追加一条审计记录，返回写入的文件路径；写失败只告警，不影响业务。

    ``action``：``update``（保存） / ``reset``（还原）。
    审计失败绝不能阻断配置修改本身 —— 所以整段包在 try 里。
    """
    entry: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "keys": sorted(keys),
        "values": values or {},
        "ip": ip,
        "ua": (user_agent or "")[:160],
    }
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path
    except OSError as exc:
        logger.warning("审计日志写入失败（不影响本次修改）: %s", exc)
        return None
