"""通知管道：控制台 / Webhook（企业微信/飞书/钉钉）/ 邮件（预留）。

借鉴 Sequoia-X 的推送设计：调度器产生事件后统一走 NotificationManager，
按配置 fan-out 到多个渠道；未配置 Webhook 时仅控制台输出，不影响主流程。
"""
from __future__ import annotations

import abc
import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("sas.notify")


class Notifier(abc.ABC):
    """通知管道接口。"""

    name: str = "base"

    @abc.abstractmethod
    async def send(self, title: str, content: str) -> bool:
        """发送通知，返回是否成功。"""


class ConsoleNotifier(Notifier):
    """控制台通知（默认渠道，始终可用）。"""

    name = "console"

    async def send(self, title: str, content: str) -> bool:
        logger.info("[通知] %s | %s", title, content.replace("\n", " ⏎ "))
        return True


class WebhookNotifier(Notifier):
    """Webhook 通知（企业微信/飞书/钉钉机器人）。

    兼容两种 payload 风格:
      - 企业微信: {"msgtype": "text", "text": {"content": ...}}
      - 飞书/钉钉: {"msgtype": "text", "text": ...}
    发送 {"title, content, text} 合并字段，覆盖多数机器人格式。
    """

    name = "webhook"

    def __init__(self, url: str) -> None:
        self.url = url

    async def send(self, title: str, content: str) -> bool:
        text = f"【{title}】\n{content}"
        payloads = [
            {"msgtype": "text", "text": {"content": text}},   # 企业微信
            {"msgtype": "text", "text": text},                # 飞书/钉钉
            {"title": title, "text": text},                   # 通用
        ]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for payload in payloads:
                    resp = await client.post(self.url, json=payload)
                    # 200/0 视为成功（不同机器人错误码规范不同）
                    if resp.status_code == 200:
                        return True
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webhook 通知失败: %s", exc)
            return False


class EmailNotifier(Notifier):
    """邮件通知（预留，后续可接 SMTP）。"""

    name = "email"

    async def send(self, title: str, content: str) -> bool:
        logger.info("[邮件预留] %s | %s", title, content[:100])
        return False


class NotificationManager:
    """通知管理器：并行发送到全部已配置渠道。"""

    def __init__(self) -> None:
        settings = get_settings()
        self.channels: list[Notifier] = [ConsoleNotifier()]
        if settings.notify_webhook_url:
            self.channels.append(WebhookNotifier(settings.notify_webhook_url))
        if settings.notify_email:
            self.channels.append(EmailNotifier())

    async def notify(self, title: str, content: str) -> None:
        """异步并行发送，单渠道失败不影响其他渠道。"""
        results = await asyncio.gather(
            *(channel.send(title, content) for channel in self.channels),
            return_exceptions=True,
        )
        failed = sum(1 for r in results if r is not True)
        if failed:
            logger.warning("通知发送: %d/%d 渠道失败", failed, len(self.channels))


# 全局单例
notifier = NotificationManager()
