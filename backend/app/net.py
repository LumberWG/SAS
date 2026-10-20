"""网络设置：确保国内行情数据源直连。

背景：部分用户系统配置了本地代理（如 Clash 127.0.0.1:7890），
requests/urllib 会自动读取该代理（Windows 下读注册表），
但代理可能未运行或拒绝国内行情站点，导致 akshare 全部请求失败。

方案：将数据源域名写入 NO_PROXY 环境变量，requests 的
``should_bypass_proxies`` 会优先于系统代理使其直连（已验证生效）。
LLM 等海外服务不受影响（httpx 独立处理，可继续走代理）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("sas.net")

#: 默认直连白名单（A股数据源均为国内站点）
DEFAULT_DATA_DIRECT_DOMAINS: list[str] = [
    "eastmoney.com",   # 东方财富：行情快照 / K线 / 指数
    "sina.com.cn",     # 新浪财经：实时行情 / 日线
    "gtimg.cn",        # 腾讯财经：实时行情 / K线
    "szse.cn",         # 深交所
    "sse.com.cn",      # 上交所
    "hexun.com",       # 和讯
    "10jqka.com.cn",   # 同花顺
]


def apply_data_network_settings(
    domains: list[str] | None = None, enabled: bool = True
) -> None:
    """将数据源域名合并进 NO_PROXY，迫使其绕过系统代理直连。

    Args:
        domains: 需要直连的域名后缀列表，None 时用默认白名单
        enabled: False 时不做任何修改（数据请求跟随系统代理）
    """
    if not enabled:
        logger.info("数据源代理绕过已禁用，数据请求将跟随系统代理")
        return

    domains = domains or DEFAULT_DATA_DIRECT_DOMAINS
    existing = os.environ.get("NO_PROXY", "")
    merged = [d.strip() for d in existing.split(",") if d.strip()]
    for domain in domains:
        if domain not in merged:
            merged.append(domain)
    value = ",".join(merged)
    # requests 大小写两种都会读取，统一设置
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value
    logger.info("数据源直连白名单已生效（NO_PROXY）: %s", value)
