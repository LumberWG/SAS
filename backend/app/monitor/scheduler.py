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
  6. intraday_monitor  盘中每隔 intraday_analysis_interval（默认 1 分钟）
                       分析自选股，生成实时买卖建议（买入/卖出列表）
  7. post_market       每交易日 15:35 全市场扫描 + 自选股分析，
                       生成次日「明日关注」TopN
  8. paper_settle      每交易日 15:40 模拟盘收盘结算（净值快照 + 兜底止盈止损）
  9. paper_tick        实时撮合（默认 5s）：信号队列成交 + 盘中止盈止损
 10. shadow_eval       每交易日 16:45 影子账户：冻结参数重跑「起点 ~ 今天」，
                       只记录快照（data/logs/shadow_<tag>.json），不交易；
                       需配置 shadow_start 才启用

模拟交易联动（全链条）: 选股命中/开盘分析/盘中分析产生的买卖建议都进入
实时队列（LiveTrader），由 paper_tick 在 ≤5 秒内按实时价成交；
9:20 开盘前的信号顺延到 9:30 开盘执行。

时钟说明: cron 任务使用本地时区（Asia/Shanghai 由 settings 指定）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.data.cache import db
from app.data.service import get_stock_pool
from app.data.snapshot_store import snapshot_store
from app.market.clock import is_trading_day, is_trading_hours
from app.notify import notifier
from app.screener.scanner import warmup_klines as screener_warmup

logger = logging.getLogger("sas.monitor")

#: 内存中保留的最近异动条数
_MAX_ALERTS = 100

#: 异动规则阈值
_LIMIT_UP_PCT = 9.8       # 涨停（近似：主板 ±10%，忽略 ST/科创创业差异）
_LIMIT_DOWN_PCT = -9.8    # 跌停
_VOL_RATIO_ALERT = 3.0    # 量比阈值（放量异动）
_BIG_MOVE_PCT = 7.0       # 急涨急跌阈值


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
        # 5) 盘中自选股监控（默认 1 分钟，任务内判断交易时段；
        #    单轮超时则跳过下一轮，不会堆积）
        self.scheduler.add_job(
            self.intraday_monitor,
            IntervalTrigger(minutes=max(1, self.settings.intraday_analysis_interval)),
            id="intraday_monitor", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        # 6) 盘后分析（交易日 15:35，生成次日关注）
        self.scheduler.add_job(
            self.post_market,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=tz),
            id="post_market", replace_existing=True,
        )
        # 7) 模拟盘收盘结算（交易日 15:40，净值快照 + 兜底止盈止损）
        self.scheduler.add_job(
            self.paper_settle,
            CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone=tz),
            id="paper_settle", replace_existing=True,
        )
        # 7b) 实时撮合（默认 5s，任务内判断交易时段；信号入队后下个 tick 成交）
        self.scheduler.add_job(
            self.paper_tick,
            IntervalTrigger(seconds=max(1, self.settings.paper_tick_interval)),
            id="paper_tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        # 8) K线预热（交易日 16:00，全市场日K增量刷新本地缓存，加速后续扫描）
        self.scheduler.add_job(
            self.warmup_klines,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=tz),
            id="kline_warmup", replace_existing=True,
        )

        # 10) 影子账户（交易日 16:45，冻结参数逐日重跑；未配置起点则不注册）
        if self.settings.shadow_start:
            self.scheduler.add_job(
                self.shadow_eval,
                CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=tz),
                id="shadow_eval", replace_existing=True,
                max_instances=1, coalesce=True,
            )

        self.scheduler.start()
        # 开机后台预热K线（低并发，不阻塞服务就绪；其余由16:00调度补全）
        asyncio.create_task(self.warmup_klines())
        logger.info(
            "调度器已启动（快照60s / 异动5min / 开盘9:20 / 盘中%dm / "
            "实时撮合%ds / 收盘选股15:30 / 盘后15:35 / 结算15:40 / K线预热16:00%s）",
            self.settings.intraday_analysis_interval,
            self.settings.paper_tick_interval,
            " / 影子账户16:45" if self.settings.shadow_start else "",
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

    async def _wait_scan(self, task_id: str, timeout_s: int | None = None):
        """轮询等待扫描任务结束，返回 ScanTask（丢失返回 None，超时返回当前 task）。

        注意：scanner.start_scan 返回的是 task_id 字符串，不是任务对象，
        必须经 get_task 取回任务再判断状态。
        """
        from app.screener.scanner import scanner

        limit = timeout_s or self.settings.scan_wait_timeout
        waited = 0
        while waited < limit:
            await asyncio.sleep(2)
            waited += 2
            task = scanner.get_task(task_id)
            if task is None:
                logger.warning("扫描任务 %s 已不存在", task_id)
                return None
            if task.status in ("done", "error"):
                return task
        logger.warning("等待扫描任务 %s 超时（%ds）", task_id, limit)
        return scanner.get_task(task_id)

    async def _scan_for_post_market(self, strategies: list[str]):
        """盘后取数：优先复用当日扫描结果，避免 5 分钟内重复全市场扫描。

        三种情况：
          1) 当日已有完成的扫描结果 → 直接复用；
          2) 当日有扫描正在跑（15:30 那轮还没结束）→ 等它跑完；
          3) 当天没扫过 → 自己发起一轮。
        """
        from app.screener.scanner import scanner

        today = datetime.now().strftime("%Y-%m-%d")
        latest = scanner.latest_task()
        if latest is not None and latest.status in ("pending", "running"):
            logger.info("当日扫描 %s 仍在进行，盘后等待其完成", latest.task_id)
            return await self._wait_scan(latest.task_id)
        if (
            latest is not None
            and latest.status == "done"
            and latest.hits
            and latest.finished_at.startswith(today)
        ):
            logger.info(
                "盘后复用当日扫描结果 %s（完成于 %s，命中 %d 条）",
                latest.task_id, latest.finished_at, len(latest.hits),
            )
            return latest
        logger.info("当日无可用扫描结果，盘后自行发起一轮扫描")
        task_id = await scanner.start_scan(
            strategies, scope=self.settings.auto_scan_scope
        )
        return await self._wait_scan(task_id)

    async def auto_scan(self) -> dict:
        """收盘后自动选股：全市场扫描 → 信号持久化 → 通知。"""
        from app.screener.scanner import scanner
        from app.strategies import registry

        strategies = self.settings.auto_scan_strategies or registry.names()
        logger.info("收盘自动选股开始，策略: %s", strategies)
        try:
            task_id = await scanner.start_scan(
                strategies, scope=self.settings.auto_scan_scope
            )
            task = await self._wait_scan(task_id)
            if task is None or task.status != "done":
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
            db.add_watch(
                code, hit.get("name", ""), note="自动加入(收盘选股)", source="auto"
            )
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
        from app.paper.live import live_trader

        logger.info("开盘前分析开始（9:20）")
        try:
            # 持仓股先纳入自选，确保卖出建议能覆盖到持仓
            await asyncio.to_thread(self._sync_positions_to_watchlist)
            recommendations = await recommender.analyze_watchlist()
            batch = f"premarket_{datetime.now().strftime('%Y-%m-%d')}"
            saved = await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], batch,
            )
            # 卖出建议分流：有持仓→入队卖出；无持仓→移出自选
            actionable, pruned = await asyncio.to_thread(
                self._route_sell_signals, recommendations
            )
            # 送入实时队列：9:20 尚未开盘，队列顺延到 9:30 开盘后按实时价成交
            queued = live_trader.submit(actionable, source="开盘分析")
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
                    "queued": queued, "pruned": len(pruned)}
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
    # 自选股动态维护（持仓纳入 / 无持仓卖出剔除）
    # ------------------------------------------------------------------
    @staticmethod
    def _sync_positions_to_watchlist() -> int:
        """持仓股自动纳入自选：保证卖出建议能覆盖到持仓股。"""
        positions = db.list_paper_positions()
        if not positions:
            return 0
        added = db.add_watch_many(
            [(p["code"], p.get("name") or p["code"]) for p in positions],
            source="auto", note="持仓自动加入",
        )
        if added:
            logger.info("持仓自动加入自选 %d 只", added)
        return added

    @staticmethod
    def _route_sell_signals(recommendations: list) -> tuple[list, list[str]]:
        """卖出建议分流：有持仓→交给模拟盘；无持仓→移出自选。

        卖出建议只对「仍在跟踪/持有」的股票有意义：没有持仓时继续留在自选
        里，只会每个周期重复产出无效卖出建议，因此直接移出自选
        （自选因此成为动态关注池：持仓自动进来，清仓后转弱自动出去）。
        """
        holdings = {p["code"] for p in db.list_paper_positions()}
        kept: list = []
        removed: list[str] = []
        for rec in recommendations:
            if rec.action != "sell" or rec.code in holdings:
                kept.append(rec)
                continue
            db.remove_watch(rec.code)
            removed.append(f"{rec.code} {rec.name}")
        if removed:
            logger.info(
                "卖出建议无持仓，移出自选 %d 只: %s",
                len(removed), ", ".join(removed[:10]),
            )
        return kept, removed

    # ------------------------------------------------------------------
    # 实时撮合（默认每 5s）
    # ------------------------------------------------------------------
    async def paper_tick(self) -> dict:
        """实时撮合一次：信号队列 + 实时止盈止损（非交易时段直接跳过）。"""
        from app.paper.live import live_trader

        try:
            return await live_trader.tick()
        except Exception as exc:  # noqa: BLE001
            logger.exception("实时撮合失败")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # K线预热（后台，加速扫描）
    # ------------------------------------------------------------------
    async def warmup_klines(self) -> dict:
        """全市场K线预热：后台增量刷新本地 Parquet 缓存，消除扫描实时网络拉取。"""
        try:
            if self.settings.kline_warmup_top_n > 0:
                pool = await asyncio.to_thread(get_stock_pool)
                codes = pool["code"].head(self.settings.kline_warmup_top_n).tolist()
                return await screener_warmup(codes)
            return await screener_warmup()
        except Exception as exc:  # noqa: BLE001
            logger.exception("K线预热失败")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # 影子账户（16:45）：冻结参数逐日重跑，只记录不下单
    # ------------------------------------------------------------------
    async def shadow_eval(self) -> dict:
        """跑一次影子快照：``shadow_start ~ 今天``，四方案并行记录。

        与模拟盘完全隔离：不读写 ``paper_*`` 表、不改配置、不影响其它任务。
        放在 16:45 是因为 16:00 的 K 线预热要先补齐当日 bar，否则最后一根还是昨天，
        快照会原地覆盖而不向前推进。同日重跑幂等（按 as_of 覆盖）。
        """
        if not is_trading_day():
            return {"skipped": "非交易日"}
        start = self.settings.shadow_start
        if not start:
            return {"skipped": "未配置 shadow_start"}
        # 起点还没到（提前配置的情况）就别跑：start > today 会得到空区间并报错
        if datetime.now().strftime("%Y-%m-%d") < start:
            return {"skipped": f"未到影子起点 {start}"}

        def _run() -> dict:
            # scripts/ 不在 app 包内：把 backend 目录加进 sys.path 再导入
            backend_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)
            from scripts.shadow import DEFAULT_PROFILES, run_shadow

            return run_shadow(
                start=start,
                universe=self.settings.shadow_universe,
                profiles=self.settings.shadow_profiles or DEFAULT_PROFILES,
                tag=self.settings.shadow_tag,
                log=lambda msg: logger.info("%s", msg),
            )

        try:
            data = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.exception("影子账户运行失败")
            await notifier.notify("影子账户失败", str(exc))
            return {"error": str(exc)}

        snaps = {
            name: (series["snapshots"][-1] if series.get("snapshots") else None)
            for name, series in (data.get("profiles") or {}).items()
        }
        base = snaps.get("default")
        diffs = [
            f"{name} {s['excess'] - base['excess']:+.2f}"
            for name, s in snaps.items()
            if base and s and name != "default"
        ]
        await notifier.notify(
            f"影子账户 {data.get('tag')} 已更新",
            (f"{start} 起 | default 超额 {base['excess']:+.2f}% "
             f"回撤 {base['max_drawdown']:.2f}%" if base else f"{start} 起（无 default 快照）")
            + ("\n相对 default 超额差: " + ", ".join(diffs) if diffs else ""),
        )
        return {
            "tag": data.get("tag"),
            "as_of": base["as_of"] if base else None,
            "profiles": [n for n, s in snaps.items() if s],
        }

    # ------------------------------------------------------------------
    # 盘中监控（每 N 分钟）：自选股实时买卖建议
    # ------------------------------------------------------------------
    async def intraday_monitor(self) -> dict:
        """盘中分析自选股，更新实时建议并驱动模拟盘执行。"""
        if not is_trading_hours():
            return {"skipped": "非交易时段"}
        from app.analysis.recommender import recommender
        from app.paper.live import live_trader

        try:
            # 持仓股先纳入自选，确保卖出建议能覆盖到持仓
            await asyncio.to_thread(self._sync_positions_to_watchlist)
            recommendations = await recommender.analyze_watchlist()
            if not recommendations:
                return {"skipped": "自选股为空"}
            batch = f"intraday_{datetime.now().strftime('%Y-%m-%d')}"
            saved = await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], batch,
            )
            # 卖出建议分流：有持仓→入队卖出；无持仓→移出自选
            actionable, pruned = await asyncio.to_thread(
                self._route_sell_signals, recommendations
            )
            # 入队后由 paper_tick（默认 5s）按实时价成交，不再等下一轮分析
            queued = live_trader.submit(actionable, source="盘中分析")
            return {"batch": batch, "saved": saved, "queued": queued,
                    "pruned": len(pruned)}
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
        from app.strategies import registry

        logger.info("盘后分析开始（15:35）")
        try:
            # 1) 取当日扫描结果：优先复用 15:30 收盘选股那一轮，缺失才自己扫
            strategies = self.settings.auto_scan_strategies or registry.names()
            task = await self._scan_for_post_market(strategies)
            if task is None or task.status != "done":
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
            await asyncio.to_thread(self._sync_positions_to_watchlist)
            recommendations = await recommender.analyze_watchlist()
            pre_batch = f"premarket_{today}"
            await asyncio.to_thread(
                db.save_recommendations,
                [r.__dict__ for r in recommendations], pre_batch,
            )
            # 盘后同样执行卖出分流：无持仓的弱势股移出自选，次日不再重复分析
            _, pruned = await asyncio.to_thread(
                self._route_sell_signals, recommendations
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
