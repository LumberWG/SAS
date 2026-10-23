"""APScheduler 定时任务调度器。

任务清单:
  1. snapshot_refresh  盘中每 60s 刷新实时快照（供概览/异动/自选股使用）
  2. alert_detect      每 5 分钟异动检测（涨跌停/放量突破/急涨急跌），
                       结果存内存环形缓冲供 API 读取，并按去重键推送通知
  3. auto_scan         每交易日 15:30 收盘后自动全市场选股，
                       命中信号持久化到 SQLite 并推送通知
  4. auto_watch        收盘自动选股完成后，评分 Top N 的命中自动加入自选股
  5. pre_market        每交易日 9:20 集合竞价后分析自选股生成本日建议，
                       并读取昨日「明日关注」
  6. intraday_monitor  盘中 9:35-15:00 每 5 分钟分析自选股，
                       生成实时买卖建议（建议买入/建议卖出列表）
  7. post_market       每交易日 15:35 全市场扫描 + 自选股分析，
                       生成次日「明日关注」TopN
  8. paper_settle      每交易日 15:40 模拟盘收盘结算（止盈止损 + 净值快照）

模拟交易联动: 开盘/盘中分析产生的买卖建议自动送入模拟盘执行
（受 PAPER_AUTO_TRADE 开关控制），盘后统一结算。

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
        # 4) 开盘前分析（交易日 9:20 集合竞价后）
        self.scheduler.add_job(
            self.pre_market,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=20, timezone=tz),
            id="pre_market", replace_existing=True,
        )
        # 5) 盘中自选股监控（每 N 分钟，任务内判断交易时段）
        self.scheduler.add_job(
            self.intraday_monitor,
            IntervalTrigger(minutes=self.settings.intraday_analysis_interval),
            id="intraday_monitor", replace_existing=True,
        )
        # 6) 盘后分析（交易日 15:35，生成次日关注）
        self.scheduler.add_job(
            self.post_market,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=tz),
            id="post_market", replace_existing=True,
        )
        # 7) 模拟盘收盘结算（交易日 15:40，止盈止损 + 净值快照）
        self.scheduler.add_job(
            self.paper_settle,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone=tz),
            id="paper_settle", replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            "调度器已启动（快照60s / 异动5min / 开盘9:20 / 盘中%dm / "
            "收盘选股15:30 / 盘后15:35 / 模拟盘结算15:40）",
            self.settings.intraday_analysis_interval,
        )

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
            # 等待扫描完成（轮询任务状态，最长约 6 分钟）
            for _ in range(180):
                await asyncio.sleep(2)
                if task.status in ("done", "error"):
                    break
            if task.status != "done":
                return {"error": "扫描未完成"}
            hit_count = len(task.hits)
            await notifier.notify(
                f"收盘选股完成：命中 {hit_count} 只",
                "\n".join(
                    f"{h['strategy']} {h['code']} {h['name']} 价{h['price']:.2f}"
                    for h in task.hits[:10]
                ) or "本轮无命中",
            )
            # 自动加自选（评分 Top N）
            watch_result = await self.auto_watch_top_hits(task.hits)
            return {
                "task_id": task.task_id, "hit_count": hit_count,
                **watch_result,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("收盘自动选股失败")
            await notifier.notify("收盘选股失败", str(exc))
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 自动加自选（收盘自动选股后：评分 Top N 加入自选股）
    # ------------------------------------------------------------------
    async def auto_watch_top_hits(self, hits: list[dict]) -> dict:
        """将评分 Top N 的扫描命中自动加入自选股（跳过已在自选的）。"""
        if not self.settings.auto_watch_enabled or not hits:
            return {"added": 0}
        top_n = self.settings.auto_watch_top_n
        scored = [
            (h.get("extra", {}).get("score", 0), h) for h in hits
        ]
        scored.sort(key=lambda x: x[0], reverse=True)

        existing = {item["code"] for item in db.list_watchlist()}
        added = []
        for _score, hit in scored[:top_n]:
            code = hit["code"]
            if code in existing:
                continue
            db.add_watch(code, hit.get("name", ""), note="自动加入(收盘选股)")
            existing.add(code)
            added.append(f"{code} {hit.get('name', '')}")

        if added:
            logger.info("自动加自选 %d 只: %s", len(added), ", ".join(added))
            await notifier.notify(
                f"自动加入自选 {len(added)} 只",
                "\n".join(added),
            )
        return {"added": len(added)}

    # ------------------------------------------------------------------
    # 开盘前分析（9:20）：自选股建议 + 读取昨日关注
    # ------------------------------------------------------------------
    async def pre_market(self) -> dict:
        """开盘前分析全部自选股，生成本日建议并驱动模拟盘。"""
        if not is_trading_day():
            return {"skipped": "非交易日"}
        from app.analysis.recommender import recommender
        from app.paper.engine import PaperEngine

        logger.info("开盘前分析开始（9:20）")
        try:
            recommendations = await recommender.analyze_watchlist()
            batch = f"premarket_{datetime.now().strftime('%Y-%m-%d')}"
            saved = await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], batch,
            )
            # 驱动模拟盘（开盘执行昨日分析的结论）
            trades = await PaperEngine().execute_recommendations(recommendations)
            buys = sum(1 for r in recommendations if r.action == "buy")
            sells = sum(1 for r in recommendations if r.action == "sell")

            # 昨日关注列表（今日重点关注）
            focus = db.list_recommendations("focus")
            focus_note = (
                f"昨日关注 {len(focus)} 只: "
                + ", ".join(f"{f['name']}({f['code']})" for f in focus[:5])
            ) if focus else "昨日无关注列表"

            await notifier.notify(
                f"开盘建议：买入 {buys} / 卖出 {sells}",
                focus_note,
            )
            return {"batch": batch, "saved": saved, "buy": buys, "sell": sells,
                    "trades": [t.side for t in trades]}
        except Exception as exc:  # noqa: BLE001
            logger.exception("开盘前分析失败")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 模拟盘收盘结算（15:40）
    # ------------------------------------------------------------------
    async def paper_settle(self) -> dict:
        """模拟盘收盘结算：止盈止损检查 + 记录净值快照。"""
        if not is_trading_day():
            return {"skipped": "非交易日"}
        from app.paper.engine import PaperEngine

        try:
            detail = await PaperEngine().settle()
            return {
                "total_asset": detail["total_asset"],
                "return_pct": detail["return_pct"],
                "positions": detail["position_count"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("模拟盘结算失败")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 盘中监控（每 N 分钟）：自选股实时买卖建议
    # ------------------------------------------------------------------
    async def intraday_monitor(self) -> dict:
        """盘中分析自选股，更新实时建议并驱动模拟盘执行。"""
        if not is_trading_hours():
            return {"skipped": "非交易时段"}
        from app.analysis.recommender import recommender
        from app.paper.engine import PaperEngine

        try:
            recommendations = await recommender.analyze_watchlist()
            if not recommendations:
                return {"skipped": "自选股为空"}
            batch = f"intraday_{datetime.now().strftime('%Y-%m-%d')}"
            saved = await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], batch,
            )
            # 驱动模拟盘（自动执行买卖，受 PAPER_AUTO_TRADE 控制）
            trades = await PaperEngine().execute_recommendations(recommendations)
            return {"batch": batch, "saved": saved,
                    "trades": [t.side for t in trades]}
        except Exception as exc:  # noqa: BLE001
            logger.exception("盘中监控失败")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 盘后分析（15:35）：全市场扫描 → 明日关注 TopN + 自选股建议
    # ------------------------------------------------------------------
    async def post_market(self) -> dict:
        """盘后深度分析：全市场扫描命中 TopN → 明日关注 + 自选股建议。"""
        if not is_trading_day():
            return {"skipped": "非交易日"}
        from app.analysis.recommender import recommender
        from app.screener.scanner import scanner
        from app.strategies import registry

        logger.info("盘后分析开始（15:35）")
        try:
            # 1) 全市场扫描（复用收盘自动选股的策略集）
            strategies = self.settings.auto_scan_strategies or registry.names()
            task = await scanner.start_scan(strategies)
            # 等待扫描完成（轮询任务状态）
            for _ in range(180):  # 最长等待约 6 分钟
                await asyncio.sleep(2)
                if task.status in ("done", "error"):
                    break
            if task.status != "done":
                return {"error": "盘后扫描未完成"}

            # 2) 明日关注（命中评分 TopN，持久化）
            today = datetime.now().strftime("%Y-%m-%d")
            focus = await recommender.post_market_focus(
                task.hits, self.settings.focus_top_n
            )
            focus_batch = f"focus_{today}"
            await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in focus], focus_batch,
            )

            # 3) 自选股建议（同资金歇后分析，批次跟随明日）
            recommendations = await recommender.analyze_watchlist()
            pre_batch = f"premarket_{today}"
            await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], pre_batch,
            )

            await notifier.notify(
                f"盘后分析完成：明日关注 {len(focus)} 只",
                "\n".join(
                    f"{f['name']}({f['code']}) 评分{f['score']:+.1f}"
                    for f in focus[:10]
                ) or "本轮无命中",
            )
            return {
                "focus_batch": focus_batch, "focus": len(focus),
                "watchlist_saved": len(recommendations),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("盘后分析失败")
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
