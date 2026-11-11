"""访客访问记录：区分真实用户与开发者调试流量。

为什么需要：站点经 cpolar 公网可达后，需要知道「都有谁在真实使用」，
而不是被本机调试（localhost）和开发者自测（同一公网 IP）的访问淹没。

落盘格式：`<数据目录>/logs/access_YYYYMMDD.jsonl`，一行一个 JSON，按天切分；
字段含真实 IP、设备（UA 解析出的浏览器/系统）、路径、状态码、是否开发者。
写失败只告警，绝不阻断请求本身。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import Request

from app.config import get_settings

logger = logging.getLogger("sas.access")

# (UA 特征串, 展示名)；顺序敏感，靠前的优先匹配
_UA_BROWSER = [
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
    ("MSIE ", "IE"),
]
_UA_OS = [
    (r"Windows NT", "Windows"),
    (r"Mac OS X", "macOS"),
    (r"Android", "Android"),
    (r"iPhone", "iOS"),
    (r"iPad", "iPadOS"),
    (r"Linux", "Linux"),
]


def real_ip(request: Request) -> str:
    """反向代理（cpolar 等）下取 X-Forwarded-For 首个地址，否则取直连地址。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _is_private_ip(ip: str) -> bool:
    """本机/局域网/保留地址：这些一律视为调试流量。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved


def parse_device(ua: str) -> dict[str, str]:
    """从 User-Agent 粗略解析浏览器与操作系统（不引入额外依赖）。"""
    ua = ua or ""
    browser = "Other"
    for token, name in _UA_BROWSER:
        if token in ua:
            browser = name
            break
    os_name = "Other"
    for pat, name in _UA_OS:
        if re.search(pat, ua):
            os_name = name
            break
    # 移动端细化：iPhone/iPad 走 iOS 系
    if "iPhone" in ua:
        os_name = "iOS"
    elif "iPad" in ua:
        os_name = "iPadOS"
    return {"browser": browser, "os": os_name}


def is_dev_request(request: Request, ip: str) -> bool:
    """判定是否为开发者/调试流量：本机 localhost、局域网等私有/回环/链路本地地址

    自动标记为调试流量（无需配置）；公网访问一律视为真实访客。
    """
    return _is_private_ip(ip)


def log_access(*, request: Request, status_code: int, is_api: bool) -> None:
    """追加一条访客访问记录。任何异常都吞掉，绝不影响业务响应。"""
    try:
        settings = get_settings()
        ip = real_ip(request)
        ua = request.headers.get("user-agent", "")
        entry: dict[str, Any] = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "ip": ip,
            "method": request.method,
            "path": request.url.path,
            "status": status_code,
            "referer": request.headers.get("referer", "")[:200],
            "ua": ua[:200],
            "device": parse_device(ua),
            "is_dev": is_dev_request(request, ip),
            "is_api": is_api,
        }
        path = settings.db_path.parent / "logs" / f"access_{datetime.now():%Y%m%d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - 访问记录失败绝不能拖垮请求
        logger.warning("访问记录写入失败（不影响本次请求）: %s", exc)
