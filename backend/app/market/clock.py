"""交易日历 / 交易时段判断（A 股）。

独立成模块是为了让「实时交易引擎」和「调度器」共用同一套时间语义，
同时避免 scheduler ↔ paper 之间循环导入。

简化说明：只按周一至周五判断交易日，不含法定节假日（节假日取不到
有效行情，交易引擎会自动跳过，不影响资金安全）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: 交易时段（分钟表示，便于比较）
AM_OPEN = 9 * 60 + 30     # 09:30
AM_CLOSE = 11 * 60 + 30   # 11:30
PM_OPEN = 13 * 60         # 13:00
PM_CLOSE = 15 * 60        # 15:00


def is_trading_day(now: datetime | None = None) -> bool:
    """是否为交易日（周一至周五，不处理法定节假日）。"""
    now = now or datetime.now()
    return now.weekday() < 5


def is_trading_hours(now: datetime | None = None) -> bool:
    """当前是否处于 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return (AM_OPEN <= minutes <= AM_CLOSE) or (PM_OPEN <= minutes <= PM_CLOSE)


def next_open(now: datetime | None = None) -> datetime:
    """下一个开盘时刻（用于给非交易时段产生的信号设置有效期）。

    Examples:
        - 09:20 → 当日 09:30
        - 12:00 → 当日 13:00
        - 15:30（周五）→ 下周一 09:30
    """
    now = now or datetime.now()
    if is_trading_day(now):
        for hour, minute in ((9, 30), (13, 0)):
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                return candidate
    day = now
    for _ in range(10):  # 最多跨一周，足够跳过周末
        day = day + timedelta(days=1)
        if is_trading_day(day):
            return day.replace(hour=9, minute=30, second=0, microsecond=0)
    return now + timedelta(days=1)
