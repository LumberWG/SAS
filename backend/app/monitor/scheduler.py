"""APScheduler 定时任务调度器。

任务清单:
  1. snapshot_refresh  盘中每 60s 刷新实时快照（供概览/异动/自选股使用）
  2. alert_detect      每 5 分钟异动检测（涨跌停/放量突破/急涨急跌），
                       结果存内存环形缓冲供 API 读取，并按去重键推送通知
  3. auto_scan         每交易日 15:30 收盘后自动全市场选股，
                       命中信号持久化到 SQLite 并推送通知

时钟说明: cron 任务使用本地时区（Asia/Shanghai 由 settings 指定）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.data.cache import db
from app.data.snapshot_store import snapshot_store
from app.notify import notifier

logger = logging.getLogger("sas.monitor")

#: 内存中保留的最近异动条数
_MAX_ALERTS = 100

#: 异动规则阈值
_LIMIT_UP_PCT = 9.8       # 涨停（近似：主板 ±10%，忽略 ST/科创创业差异）
_LIMIT_DOWN_PCT = -9.8    # 跌停
_VOL_RATIO_ALERT = 3.0    # 量比阈值（放量异动）
_BIG_MOVE_PCT = 7.0       # 急涨急跌阈值


def is_trading_hours(now: datetime | None = None) -> bool:
    """当前是否处于 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes <= (11 * 60 + 30) or \
        (13 * 60) <= minutes <= (15 * 60)


def is_trading_day(now: datetime | None = None) -> bool:
    """是否为交易日（周一至周五，不处理法定节假日）。"""
    now = now or datetime.now()
    return now.weekday() < 5


class MarketScheduler:
    """市场调度器。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.scheduler: AsyncIOScheduler | None = None
        self._alerts: deque[dict] = deque(maxlen=_MAX_ALERTS)
        self._alert_keys: set[str] = set()  # 去重键（同日同股同类型）
        self._alert_date = ""               # 当前去重日期

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动调度器（开关关闭时静默跳过）。"""
        if not self.settings.scheduler_enabled:
            logger.info("调度器未启用（scheduler_enabled=False）")
            return
        if self.scheduler is not None:
            return

        tz = self.settings.scheduler_timezone
        self.scheduler = AsyncIOScheduler(timezone=tz)

        # 1) 盘中快照刷新（60s）
        self.scheduler.add_job(
            self.refresh_snapshot, IntervalTrigger(minutes=1),
            id="snapshot_refresh", replace_existing=True,
        )
        # 2) 异动检测（5min）
        self.scheduler.add_job(
            self.detect_alerts, IntervalTrigger(minutes=5),
            id="alert_detect", replace_existing=True,
        )
        # 3) 收盘后自动选股（交易日 15:30）
        self.scheduler.add_job(
            self.auto_scan,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=tz),
            id="auto_scan", replace_existing=True,
        )

        self.scheduler.start()
        logger.info("调度器已启动（快照60s / 异动5min / 收盘选股15:30）")

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            logger.info("调度器已关闭")

    # ------------------------------------------------------------------
    # 任务实现
    # ------------------------------------------------------------------
    async def refresh_snapshot(self) -> None:
        """盘中刷新实时快照（非交易时段跳过）。"""
        if not is_trading_hours():
            return
        try:
            await asyncio.to_thread(snapshot_store.refresh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("快照刷新失败: %s", exc)

    async def detect_alerts(self) -> list[dict]:
        """异动检测：涨跌停 / 放量突破 / 急涨急跌。

        规则（按优先级，单股当日只报最高优先级的一类，避免刷屏）:
          1. 涨跌停   |pct| >= 9.8%
          2. 放量突破 量比 >= 3 且 pct > 5%（或 pct < -5%）
          3. 急涨急跌 |pct| >= 7%
        """
        try:
            snapshot = await asyncio.to_thread(snapshot_store.get_snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("异动检测取快照失败: %s", exc)
            return []

        today = datetime.now().strftime("%Y-%m-%d")
        if self._alert_date != today:
            self._alert_keys.clear()
            self._alert_date = today

        new_alerts: list[dict] = []
        for row in snapshot.itertuples():
            pct = row.pct_change
            vol_ratio = getattr(row, "volume_ratio", None)
            if pct is None or (isinstance(pct, float) and pct != pct):
                continue

            alert_type = None
            if pct >= _LIMIT_UP_PCT:
                alert_type = "涨停"
            elif pct <= _LIMIT_DOWN_PCT:
                alert_type = "跌停"
            elif vol_ratio is not None and vol_ratio == vol_ratio and vol_ratio >= _VOL_RATIO_ALERT:
                alert_type = "放量突破" if pct > 5 else ("放量下跌" if pct < -5 else None)
            elif abs(pct) >= _BIG_MOVE_PCT:
                alert_type = "急涨" if pct > 0 else "急跌"
            if alert_type is None:
                continue

            key = f"{today}:{row.code}:{alert_type}"
            if key in self._alert_keys:
                continue
            self._alert_keys.add(key)

            alert = {
                "type": alert_type,
                "code": row.code,
                "name": row.name,
                "price": float(row.price) if row.price == row.price else None,
                "pct_change": float(pct),
                "volume_ratio": float(vol_ratio) if vol_ratio is not None and vol_ratio == vol_ratio else None,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            self._alerts.appendleft(alert)
            new_alerts.append(alert)

        if new_alerts:
            logger.info("检测到 %d 条新异动", len(new_alerts))
            await notifier.notify(
                f"市场异动 {len(new_alerts)} 条",
                "\n".join(
                    f"{a['type']} {a['code']} {a['name']} "
                    f"{a['pct_change']:+.2f}%" + (f" 量比{a['volume_ratio']:.1f}" if a["volume_ratio"] else "")
                    for a in new_alerts[:10]
                ),
            )
        return new_alerts

    async def auto_scan(self) -> dict:
        """收盘后自动选股：全市场扫描 → 信号持久化 → 通知。"""
        from app.screener.scanner import scanner
        from app.strategies import registry

        strategies = self.settings.auto_scan_strategies or registry.names()
        logger.info("收盘自动选股开始，策略: %s", strategies)
        try:
            task = await scanner.start_scan(strategies)
            await notifier.notify(
                f"收盘选股完成：命中 {task.hit_count} 只",
                "\n".join(
                    f"{h.strategy} {h.code} {h.name} 价{h.price:.2f}"
                    for h in task.hits[:10]
                ) or "本轮无命中",
            )
            return {"task_id": task.task_id, "hit_count": task.hit_count}
        except Exception as exc:  # noqa: BLE001
            logger.exception("收盘自动选股失败")
            await notifier.notify("收盘选股失败", str(exc))
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 状态查询（供 API）
    # ------------------------------------------------------------------
    @property
    def alerts(self) -> list[dict]:
        """最近异动列表（新→旧）。"""
        return list(self._alerts)

    def status(self) -> dict:
        """调度器状态。"""
        if self.scheduler is None:
            return {"running": False, "jobs": []}
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            })
        return {"running": True, "jobs": jobs}


# 全局单例
scheduler = MarketScheduler()
