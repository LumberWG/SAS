"""全市场并发扫描器。

流程:
  1. 获取股票池（或指定代码子集）；
  2. asyncio 并发（信号量限流）逐股获取K线（本地缓存优先）并计算指标；
  3. 逐策略 evaluate，收集命中信号；
  4. 扫描完成后自动将信号写入 SQLite。

容错: 单股数据获取/计算失败仅记录日志并跳过，不影响整轮扫描。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.config import get_settings
from app.data.cache import db
from app.data.service import get_kline_cached, get_stock_pool
from app.indicators import add_all_indicators
from app.strategies.base import Signal, Strategy

logger = logging.getLogger("sas.screener")


@dataclass
class ScanTask:
    """一轮扫描任务的状态（内存中管理，前端轮询）。"""

    task_id: str
    strategies: list[str]
    status: str = "pending"          # pending / running / done / error
    total: int = 0                   # 待扫描股票数
    scanned: int = 0                 # 已扫描股票数
    hits: list[dict] = field(default_factory=list)   # 命中信号
    skipped: int = 0                 # 数据不足/失败跳过的股票数
    started_at: str = ""
    finished_at: str = ""

    def progress(self) -> float:
        return round(self.scanned / self.total * 100, 1) if self.total else 0.0


class Scanner:
    """全市场扫描器（进程内单例，同时只跑一轮扫描）。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._tasks: dict[str, ScanTask] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------
    def get_task(self, task_id: str) -> ScanTask | None:
        return self._tasks.get(task_id)

    def latest_task(self) -> ScanTask | None:
        """最近一轮扫描（供定时任务/前端展示）。"""
        if not self._tasks:
            return None
        return max(self._tasks.values(), key=lambda t: t.started_at or "")

    async def start_scan(
        self, strategy_names: list[str], codes: list[str] | None = None
    ) -> str:
        """启动一轮后台扫描，返回 task_id。

        Args:
            strategy_names: 策略名列表（已注册）
            codes: 限定扫描的股票代码；None 表示全市场
        """
        task_id = uuid.uuid4().hex[:12]
        task = ScanTask(task_id=task_id, strategies=strategy_names)
        self._tasks[task_id] = task
        asyncio.create_task(self._run(task, codes))
        return task_id

    # ------------------------------------------------------------------
    # 扫描主流程
    # ------------------------------------------------------------------
    async def _run(self, task: ScanTask, codes: list[str] | None) -> None:
        task.status = "running"
        task.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            pool = get_stock_pool()
            if codes:
                wanted = set(codes)
                pool = pool[pool["code"].isin(wanted)]
            task.total = len(pool)

            strategies = self._resolve_strategies(task.strategies)
            semaphore = asyncio.Semaphore(self.settings.scan_concurrency)

            async def scan_one(row) -> None:
                async with semaphore:
                    try:
                        signals = await self._scan_stock(
                            row.code, row.name, strategies
                        )
                        task.hits.extend(s.to_dict() for s in signals)
                    except Exception as exc:  # noqa: BLE001 - 单股失败跳过
                        task.skipped += 1
                        logger.debug("扫描 %s 失败: %s", row.code, exc)
                    finally:
                        task.scanned += 1
                        if task.scanned % 200 == 0:
                            logger.info(
                                "扫描进度 %d/%d (%.0f%%)",
                                task.scanned, task.total, task.progress(),
                            )

            await asyncio.gather(*(scan_one(r) for r in pool.itertuples()))

            # 命中的股票补充名称（信号生成时还不知道名称的场景已由 scan_stock 填充）
            task.hits.sort(key=lambda h: h.get("price", 0), reverse=True)
            task.status = "done"
            task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 信号持久化
            scan_date = datetime.now().strftime("%Y-%m-%d")
            db.save_signals(task.hits, scan_date=scan_date)
            logger.info(
                "扫描完成: %d 只股票, 命中 %d 个信号, 跳过 %d 只",
                task.total, len(task.hits), task.skipped,
            )
        except Exception as exc:  # noqa: BLE001 - 整轮失败记录状态
            task.status = "error"
            task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.exception("扫描任务失败: %s", exc)

    def _resolve_strategies(self, names: list[str]) -> list[Strategy]:
        from app.strategies import registry

        return [registry.create(n) for n in names]

    async def _scan_stock(
        self, code: str, name: str, strategies: list[Strategy]
    ) -> list[Signal]:
        """扫描单只股票：取K线 → 算指标 → 逐策略评估。"""
        # 网络与计算都是阻塞操作，放到线程池避免卡住事件循环
        df = await asyncio.to_thread(get_kline_cached, code)
        if df is None or df.empty or len(df) < 30:
            return []
        df = await asyncio.to_thread(add_all_indicators, df)

        signals: list[Signal] = []
        for strategy in strategies:
            if len(df) < strategy.min_bars:
                continue
            signal = strategy.evaluate(df)
            if signal is not None:
                signal.code = code
                signal.name = name
                signals.append(signal)
        return signals


# 全局单例
scanner = Scanner()
