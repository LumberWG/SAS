"""通知管道抽象：把异动/信号推送到外部渠道。"""

from app.notify.channels import NotificationManager, notifier

__all__ = ["NotificationManager", "notifier"]
