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
import functools
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.config import get_settings
from app.data.cache import db, kline_cache
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
    scope: str = "all"               # 扫描范围
    sentiment: dict = field(default_factory=dict)    # 扫描开始时市场情绪快照
    error: str = ""                  # 失败原因（status=error 时供前端展示）

    def progress(self) -> float:
        return round(self.scanned / self.total * 100, 1) if self.total else 0.0


class Scanner:
    """全市场扫描器（进程内单例，同时只跑一轮扫描）。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._tasks: dict[str, ScanTask] = {}
        self._lock = asyncio.Lock()
        # 扫描 IO 专用线程池：asyncio 默认执行器只有 cpu+4 个 worker，
        # 冷缓存全市场扫描（每只要走降级链、单只可达十几秒）会严重供不应求；
        # 这里按扫描并发上限单独扩容，信号量仍然限制同时在飞的股票数。
        self._io = ThreadPoolExecutor(
            max_workers=max(self.settings.scan_concurrency, 8),
            thread_name_prefix="scan-io",
        )

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

    def has_active_scan(self) -> bool:
        """是否有扫描正在运行（后台预热据此让路，优先保证扫描出结果）。"""
        return any(t.status in ("pending", "running") for t in self._tasks.values())

    async def start_scan(
        self,
        strategy_names: list[str],
        codes: list[str] | None = None,
        scope: str = "all",
    ) -> str:
        """启动一轮后台扫描，返回 task_id。

        Args:
            strategy_names: 策略名列表（已注册）
            codes: 限定扫描的股票代码；None 表示全市场
            scope: 扫描范围（见 SCAN_SCOPES；codes 优先于 scope）
        """
        task_id = uuid.uuid4().hex[:12]
        task = ScanTask(task_id=task_id, strategies=strategy_names, scope=scope)
        self._tasks[task_id] = task
        asyncio.create_task(self._run(task, codes, scope))
        return task_id

    # ------------------------------------------------------------------
    # 扫描主流程
    # ------------------------------------------------------------------
    async def _run(
        self, task: ScanTask, codes: list[str] | None, scope: str = "all"
    ) -> None:
        task.status = "running"
        task.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            loop = asyncio.get_running_loop()
            # 记录扫描开始时的市场情绪（供结果展示与 LLM 分析上下文）
            # 取快照可能触发冷启动同步刷新（分钟级），必须放到线程池，
            # 否则会阻塞事件循环导致整个 API 无响应
            task.sentiment = await loop.run_in_executor(
                self._io, self._current_sentiment
            )

            pool = await loop.run_in_executor(self._io, get_stock_pool)
            if codes:
                # 显式指定代码时直接以其为准：股票池可能来自降级快照
                # （覆盖不全），不能再用 isin 过滤，否则会漏扫
                wanted = [str(c).zfill(6) for c in codes]
                names = dict(zip(pool["code"], pool["name"])) if "name" in pool else {}
                pool = pd.DataFrame(
                    {"code": wanted, "name": [names.get(c, c) for c in wanted]}
                )
            elif scope.startswith("top_amount:"):
                # 成交额前 N（流动性优先，冷缓存下最快出结果的范围）
                n = int(scope.split(":", 1)[1] or 500)
                pool = await loop.run_in_executor(self._io, self._top_amount_pool, pool, n)
            else:
                pool = filter_pool_by_scope(pool, scope)
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
                        if task.scanned % 50 == 0:
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
            task.error = str(exc)
            task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.exception("扫描任务失败: %s", exc)

    def _resolve_strategies(self, names: list[str]) -> list[Strategy]:
        from app.strategies import registry

        return [registry.create(n) for n in names]

    @staticmethod
    def _current_sentiment() -> dict:
        """当前市场情绪快照（失败时返回空 dict，不影响扫描）。"""
        try:
            from app.data.snapshot_store import snapshot_store
            from app.market.sentiment import sentiment_from_snapshot

            return sentiment_from_snapshot(snapshot_store.get_snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.debug("情绪快照获取失败: %s", exc)
            return {}

    @staticmethod
    def _top_amount_pool(pool: pd.DataFrame, n: int) -> pd.DataFrame:
        """按实时成交额取前 N 只股票（快照失败时退化为全量前 N 行）。"""
        from app.data.snapshot_store import snapshot_store

        try:
            snapshot = snapshot_store.get_snapshot()
            top = snapshot.nlargest(n, "amount")
            codes = set(top["code"])
            return pool[pool["code"].isin(codes)]
        except Exception as exc:  # noqa: BLE001 - 快照不可用时降级
            logger.warning("成交额股票池获取失败，退化为股票池前 %d 只: %s", n, exc)
            return pool.head(n)

    async def _scan_stock(
        self, code: str, name: str, strategies: list[Strategy]
    ) -> list[Signal]:
        """扫描单只股票：取K线 → 算指标 → 逐策略评估。"""
        # 网络与计算都是阻塞操作，放到线程池避免卡住事件循环
        loop = asyncio.get_running_loop()
        df = await loop.run_in_executor(self._io, get_kline_cached, code)
        if df is None or df.empty or len(df) < 30:
            return []
        df = await loop.run_in_executor(self._io, add_all_indicators, df)

        signals: list[Signal] = []
        for strategy in strategies:
            if len(df) < strategy.min_bars:
                continue
            signal = strategy.evaluate(df)
            if signal is not None:
                signal.code = code
                signal.name = name
                signals.append(signal)

        # 补充当日涨跌幅到 extra（命中表格展示用；K线缓存无该列时由收盘价推导）
        if signals and len(df) > 1:
            pct = (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
            for s in signals:
                s.extra.setdefault("pct_change", round(float(pct), 2))
        # 补充技术面评分（盘后 TopN 排序与自动加自选用）
        if signals:
            from app.analysis.scoring import score_technical

            technical = score_technical(df)
            for s in signals:
                s.extra.setdefault("score", technical.score)
        return signals


# 全局单例
scanner = Scanner()

#: 最近一次K线预热结果（供状态接口读取）
_last_warmup: dict = {}

# ----------------------------------------------------------------------
# 扫描范围定义（选股中心「扫描范围」下拉）
# ----------------------------------------------------------------------
SCAN_SCOPES: dict[str, dict] = {
    "top_amount:500": {"label": "成交额前 500（推荐）", "filter": None},
    "top_amount:1000": {"label": "成交额前 1000", "filter": None},
    "sh": {"label": "沪市主板", "filter": lambda p: p["code"].str.startswith("60")},
    "sz": {"label": "深市主板", "filter": lambda p: p["code"].str.startswith(("00", "001"))},
    "cyber": {"label": "创业板", "filter": lambda p: p["code"].str.startswith(("30", "301"))},
    "star": {"label": "科创板", "filter": lambda p: p["code"].str.startswith("68")},
    "bjs": {"label": "北交所", "filter": lambda p: p["code"].str.startswith(("43", "83", "87", "88", "92"))},
}


async def run_in_io(fn, /, *args, **kwargs):
    """在扫描专用 IO 线程池里执行阻塞调用。

    asyncio 默认执行器只有 cpu+4 个 worker，被预热/扫描占满时其他接口
    （同样用 asyncio.to_thread）会排队到超时，因此对外统一用这个池。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        scanner._io, functools.partial(fn, *args, **kwargs)
    )


async def warmup_klines(
    codes: list[str] | None = None,
    concurrency: int | None = None,
) -> dict:
    """后台批量预热K线：把股票日K增量缓存到本地Parquet，加速扫描。

    已有缓存的股票只增量拉取最近几天（很快）；缺失的首次全量拉取
    （较慢但在后台进行，不阻塞服务）。预热完成后扫描直接读本地缓存。

    Args:
        codes: 指定代码列表；None 表示全市场
        concurrency: 预热并发（默认取配置 kline_warmup_concurrency）
    """
    settings = get_settings()
    if concurrency is None:
        concurrency = settings.kline_warmup_concurrency
    loop = asyncio.get_running_loop()
    pool = await run_in_io(get_stock_pool)
    if codes is not None:
        wanted = set(codes)
        pool = pool[pool["code"].isin(wanted)]
    sem = asyncio.Semaphore(concurrency)
    ok = 0
    fail = 0

    async def one(code: str) -> None:
        nonlocal ok, fail
        async with sem:
            try:
                # 走扫描专用线程池：默认执行器（cpu+4）会被预热占满，
                # 导致其他接口的 asyncio.to_thread 排队（实测 kline-status 13s）
                await loop.run_in_executor(scanner._io, get_kline_cached, code)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单股失败跳过
                fail += 1
                logger.debug("K线预热失败 %s: %s", code, exc)

    # 分批预热：每批之间检查是否有用户扫描在跑，有则立即让路。
    # 预热与扫描抢的是同一批数据源连接与节流额度，若同时进行，
    # 刚点下的扫描会几十秒看不到任何进度（实测冷启动差 1 分钟以上）。
    codes_all = list(pool["code"])
    chunk = max(concurrency * 5, 50)
    for start in range(0, len(codes_all), chunk):
        if scanner.has_active_scan():
            logger.info(
                "扫描任务进行中，K线预热让路：已完成 %d/%d 只", ok, len(codes_all)
            )
            break
        await asyncio.gather(*(one(c) for c in codes_all[start : start + chunk]))

    result = {"ok": ok, "fail": fail, "total": len(pool)}
    _last_warmup.update(
        {"result": result, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    )
    logger.info("K线预热完成：成功 %d，失败 %d，共 %d", ok, fail, len(pool))
    return result


def kline_cache_stats() -> dict:
    """统计本地K线缓存覆盖情况（Parquet 文件存在即计为已缓存）。

    供前端展示预热进度：已缓存多少只 / 股票池总数 / 最近一次预热结果。
    """
    pool = get_stock_pool()
    total = len(pool)
    cached = 0
    for code in pool["code"]:
        if kline_cache._path(code).exists():
            cached += 1
    return {
        "total": total,
        "cached": cached,
        "ratio": round(cached / total * 100, 1) if total else 0.0,
        "last_warmup": dict(_last_warmup),
    }


def filter_pool_by_scope(pool: pd.DataFrame, scope: str) -> pd.DataFrame:
    """按扫描范围过滤股票池。

    top_amount 系列需要实时快照（按成交额排序取前 N），
    由 scanner 在 _run 中特殊处理；此处只处理静态板块范围。
    """
    if scope == "all" or scope not in SCAN_SCOPES:
        return pool
    return pool[SCAN_SCOPES[scope]["filter"](pool)]
