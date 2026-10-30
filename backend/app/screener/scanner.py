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
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.config import get_settings
from app.data.cache import db, kline_cache
from app.data.scope import is_tradable
from app.data.service import (
    INDEX_SYMBOL,
    get_index_cached,
    get_kline_cached,
    get_stock_pool,
)
from app.indicators import add_all_indicators
from app.strategies.base import Signal, Strategy

logger = logging.getLogger("sas.screener")


def _num(value) -> float | None:
    """数值转 float，NaN/None 返回 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def rank_hits(hits: list[dict]) -> list[dict]:
    """第 1 次排序：命中信号按技术面评分降序排名（原地排序并写入 rank）。

    - 主键：``extra.score``（缺省按 0），同分看触发价；
    - 名次按**股票**编号：同一只股票命中多个策略时并列同一 rank，
      保证前端聚合后列表的名次连续（1,2,3...）。
    """
    hits.sort(
        key=lambda h: (
            (h.get("extra") or {}).get("score") or 0,
            h.get("price") or 0,
        ),
        reverse=True,
    )
    rank = 0
    ranked_codes: set[str] = set()
    for hit in hits:
        code = str(hit.get("code") or "")
        if code not in ranked_codes:
            ranked_codes.add(code)
            rank += 1
        hit["rank"] = rank
    return hits


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
    codes: list[str] | None = None   # 指定代码（None=按 scope）；并发复用判断用
    sentiment: dict = field(default_factory=dict)    # 扫描开始时市场情绪快照
    error: str = ""                  # 失败原因（status=error 时供前端展示）
    excluded_by_scope: int = 0      # 被范围过滤剔除的股票数（科创板/北交所/ST/巨无霸）

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
        """最近一轮扫描（供定时任务/前端展示）。

        重启后内存任务为空，此时从 data/last_scan.json 恢复上一轮结果，
        避免"改完代码重启，选股中心列表变空"。
        """
        if not self._tasks:
            return self._restore_task()
        return max(self._tasks.values(), key=lambda t: t.started_at or "")

    # ------------------------------------------------------------------
    # 最近一轮扫描落盘（重启后恢复）
    # ------------------------------------------------------------------
    def _state_path(self):
        return self.settings.db_path.parent / "last_scan.json"

    def _persist_task(self, task: ScanTask) -> None:
        """把已完成的扫描结果写入磁盘（原子替换，避免半截文件）。"""
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "task_id": task.task_id,
                "strategies": task.strategies,
                "scope": task.scope,
                "codes": task.codes,
                "total": task.total,
                "scanned": task.scanned,
                "skipped": task.skipped,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "sentiment": task.sentiment,
                "hits": task.hits,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
            logger.info("最近一轮扫描已落盘: %s (%d 条命中)", path, len(task.hits))
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响扫描结果
            logger.warning("扫描结果落盘失败: %s", exc)

    def _restore_task(self) -> ScanTask | None:
        """从磁盘恢复上一轮扫描；无文件或损坏时返回 None。"""
        try:
            path = self._state_path()
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            hits = data.get("hits")
            if not isinstance(hits, list):
                return None
            task = ScanTask(
                task_id=str(data.get("task_id") or "restored"),
                strategies=list(data.get("strategies") or []),
                status="done",
                total=int(data.get("total") or 0),
                scanned=int(data.get("scanned") or 0),
                hits=hits,
                skipped=int(data.get("skipped") or 0),
                started_at=str(data.get("started_at") or ""),
                finished_at=str(data.get("finished_at") or ""),
                scope=str(data.get("scope") or "all"),
                codes=list(data["codes"]) if data.get("codes") else None,
                sentiment=dict(data.get("sentiment") or {}),
            )
            self._tasks[task.task_id] = task
            logger.info(
                "已恢复上一轮扫描结果: %s (%d 条命中, 完成于 %s)",
                task.task_id, len(task.hits), task.finished_at,
            )
            return task
        except Exception as exc:  # noqa: BLE001 - 恢复失败按无历史处理
            logger.warning("扫描结果恢复失败: %s", exc)
            return None

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
        # 并发保护：同一策略集 + 同一扫描目标正在跑时直接复用，
        # 避免「连点两次开始扫描」或「定时任务撞车」并行跑两遍全市场。
        running = self._find_reusable(strategy_names, codes, scope)
        if running is not None:
            logger.info(
                "复用进行中的扫描任务 %s（策略 %d 个，%s），不再重复启动",
                running.task_id, len(running.strategies),
                ("代码 %d 只" % len(running.codes)) if running.codes else running.scope,
            )
            return running.task_id

        task_id = uuid.uuid4().hex[:12]
        task = ScanTask(
            task_id=task_id,
            strategies=list(strategy_names),
            scope=scope,
            codes=list(codes) if codes else None,
        )
        self._tasks[task_id] = task
        asyncio.create_task(self._run(task, codes, scope))
        return task_id

    def _find_reusable(
        self,
        strategy_names: list[str],
        codes: list[str] | None,
        scope: str,
    ) -> ScanTask | None:
        """查找可复用的进行中任务（策略集与扫描目标都一致）。"""
        wanted_strategies = sorted(strategy_names)
        wanted_codes = sorted(str(c) for c in codes) if codes else None
        for task in self._tasks.values():
            if task.status not in ("pending", "running"):
                continue
            if sorted(task.strategies) != wanted_strategies:
                continue
            if wanted_codes is not None or task.codes:
                # 指定代码的扫描只与相同的代码集复用
                if sorted(task.codes or []) != (wanted_codes or []):
                    continue
            elif task.scope != scope:
                continue
            return task
        return None

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
            # 范围过滤（项目原则：只分析沪深主板+创业板，剔除 ST 与巨无霸）。
            # 统一作用于所有分支（显式 codes / 成交额 TopN / scope），
            # 保证选股中心候选池不出科创板/北交所/ST/巨无霸。
            before = len(pool)
            if "name" not in pool.columns:
                pool["name"] = pool["code"]
            # 巨无霸黑名单与「交易队列」页的大盘股开关同一口径：开关关闭时黑名单也放开，
            # 否则页面上关掉开关，候选池里仍然永远看不到银行/石油，开关等于没用
            from app.paper.engine import large_cap_rule  # 延迟导入避免循环依赖

            large_on, large_limit = large_cap_rule()
            keep = [
                is_tradable(str(c), str(n), exclude_giant=large_on)[0]
                for c, n in zip(pool["code"].astype(str), pool["name"].astype(str))
            ]
            pool = pool[pd.Series(keep, index=pool.index)].reset_index(drop=True)
            # 总市值阈值（默认 100 亿）同样作用于候选池，避免大市值票占掉二选名额
            if large_on and large_limit > 0 and "total_mv" in pool.columns:
                mv = pd.to_numeric(pool["total_mv"], errors="coerce")
                pool = pool[mv.isna() | (mv <= large_limit)].reset_index(drop=True)
            task.excluded_by_scope = before - len(pool)
            if task.excluded_by_scope:
                logger.info(
                    "范围过滤：剔除 %d 只范围外股票（科创板/北交所/ST/巨无霸），剩余 %d 只",
                    task.excluded_by_scope, len(pool),
                )
            task.total = len(pool)

            strategies = self._resolve_strategies(task.strategies)
            # 本轮实时快照（盘中用于修正日K滞后的最新价/涨跌幅，见 _scan_stock）
            quotes = await loop.run_in_executor(self._io, self._realtime_quotes)
            semaphore = asyncio.Semaphore(self.settings.scan_concurrency)

            async def scan_one(row) -> None:
                async with semaphore:
                    try:
                        signals = await self._scan_stock(
                            row.code, row.name, strategies, quotes.get(str(row.code))
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

            # 第 1 次排序：按技术面评分降序，同股并列同一名次
            rank_hits(task.hits)
            task.status = "done"
            task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 信号持久化
            scan_date = datetime.now().strftime("%Y-%m-%d")
            db.save_signals(task.hits, scan_date=scan_date)
            # 整轮结果落盘：后端重启后 latest_scan 仍能恢复列表
            await loop.run_in_executor(self._io, self._persist_task, task)
            # 命中自动进入「程序自选」（按评分取前 N，配置为 0 时关闭）
            auto_added = await loop.run_in_executor(
                self._io, self._auto_watch_hits, task.hits
            )
            logger.info(
                "扫描完成: %d 只股票, 命中 %d 个信号, 跳过 %d 只, 程序自选新增 %d",
                task.total, len(task.hits), task.skipped, auto_added,
            )
            # 选股 → 建议 → 实时交易：盘中命中立即分析并送入实时队列
            await self._trade_top_hits(task.hits)
        except Exception as exc:  # noqa: BLE001 - 整轮失败记录状态
            task.status = "error"
            task.error = str(exc)
            task.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.exception("扫描任务失败: %s", exc)

    async def _trade_top_hits(self, hits: list[dict]) -> None:
        """把命中 TopN 立即送入「建议 → 实时交易」队列（打通选股到成交）。

        盘中：分析完立刻入队，下个 tick（≤5s）按实时价成交。
        盘后：同样入队，由队列的「非交易时段顺延」规则在次日 9:30 开盘后
        按开盘实时价成交（仍是实时撮合，不是用收盘价直接成交）。
        盘后联动可用 ``PAPER_SCAN_AUTOTRADE_OVERNIGHT=false`` 关闭。

        注意：本方法在 ``task.status = "done"`` 之后调用，必须自行兜底异常，
        否则会把已完成的任务改写成 error。
        """
        if not hits or not self.settings.paper_scan_autotrade:
            return
        from app.market.clock import is_trading_hours

        overnight = not is_trading_hours()
        if overnight and not self.settings.paper_scan_autotrade_overnight:
            return
        from app.analysis.recommender import market_sentiment, recommender
        from app.paper.live import live_trader

        ranked = sorted(
            hits, key=lambda h: h.get("extra", {}).get("score", 0), reverse=True
        )
        picked: list[dict] = []
        seen: set[str] = set()
        for hit in ranked:
            code = str(hit.get("code", "") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            picked.append(hit)
            if len(picked) >= self.settings.paper_scan_autotrade_top_n:
                break

        sentiment = await asyncio.to_thread(market_sentiment)
        semaphore = asyncio.Semaphore(5)

        async def analyze(hit: dict):
            async with semaphore:
                try:
                    return await recommender.analyze_stock(
                        str(hit["code"]), str(hit.get("name") or ""),
                        hit.get("price"), sentiment,
                    )
                except Exception as exc:  # noqa: BLE001 - 单只失败不影响其他
                    logger.debug("选股命中分析失败 %s: %s", hit.get("code"), exc)
                    return None

        try:
            results = await asyncio.gather(*(analyze(h) for h in picked))
            recommendations = [r for r in results if r is not None]
            queued = live_trader.submit(
                recommendations,
                source="选股命中(盘后顺延开盘)" if overnight else "选股命中",
            )
            buys = sum(1 for r in recommendations if r.action == "buy")
            logger.info(
                "选股联动%s：分析 %d 只，买入建议 %d 条，实时入队 %d 条",
                "（盘后，次日开盘成交）" if overnight else "", len(picked), buys, queued,
            )
        except Exception:  # noqa: BLE001
            logger.exception("选股联动实时交易失败")

    def _auto_watch_hits(self, hits: list[dict]) -> int:
        """本轮命中按技术面评分 Top N 写入「程序自选」，返回新增条数。

        程序自选只是推荐关注池，已存在于手动自选的股票不会被降级。
        """
        limit = self.settings.scan_auto_watch_limit
        if limit <= 0 or not hits:
            return 0
        ranked = sorted(
            hits, key=lambda h: h.get("extra", {}).get("score", 0), reverse=True
        )
        # 一只股票命中多个策略时只取一次，避免 TopN 名额被同一只股票吃掉
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        skipped = 0
        for hit in ranked:
            code = str(hit.get("code") or "")
            name = str(hit.get("name") or code)
            if not code or code in seen:
                continue
            seen.add(code)
            # 兜底再判一次范围：候选池正常已过滤，但显式 codes / 盘后选股等路径
            # 可能把科创板(688)/北交所/ST 带进来，写进自选后会长期占着二选名额
            if not is_tradable(code, name)[0]:
                skipped += 1
                continue
            items.append((code, name))
            if len(items) >= limit:
                break
        if skipped:
            logger.info("程序自选跳过 %d 只范围外股票（科创板/北交所/ST/巨无霸）", skipped)
        added = db.add_watch_many(items, source="auto", note="自动加入(扫描命中)")
        if added:
            logger.info("程序自选新增 %d 只（本轮命中 Top %d）", added, limit)
        return added

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
    def _realtime_quotes() -> dict[str, dict]:
        """全市场实时快照映射（只读内存缓存，见 ``snapshot_store.quote_map``）。"""
        from app.data.snapshot_store import quote_map

        try:
            return quote_map()
        except Exception as exc:  # noqa: BLE001 - 快照不可用则沿用K线口径
            logger.debug("实时快照不可用，扫描价格沿用日K口径: %s", exc)
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
        self,
        code: str,
        name: str,
        strategies: list[Strategy],
        quote: dict | None = None,
    ) -> list[Signal]:
        """扫描单只股票：取K线 → 算指标 → 逐策略评估。

        Args:
            quote: 该股实时快照（price/pct_change/volume_ratio/amount），
                用于修正日K缓存滞后的展示口径；缺失时退化为K线收盘价推导。
        """
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

        # ---- 展示口径：最新价 / 涨跌幅 / 量比 / 成交额 ----
        # 日K缓存最后一根在盘中仍是上一交易日（盘中接口不返回当日K），直接用它的
        # 收盘价会把「昨天的价格与涨幅」当成今天的：实测 920207 众诚科技显示
        # +31.39%（10-23 的涨幅），当日实际只有 +6.95%。因此以实时快照为准；
        # 只有K线已含当日收盘时，才用收盘价推导涨跌幅。
        if signals:
            today = datetime.now().strftime("%Y-%m-%d")
            last_date = str(df["date"].iloc[-1]) if "date" in df.columns else ""
            snap_price = _num(quote.get("price")) if quote else None
            if quote and last_date != today and snap_price and snap_price > 0:
                pct = _num(quote.get("pct_change"))
                vol_ratio = _num(quote.get("volume_ratio"))
                amount = _num(quote.get("amount"))
                for s in signals:
                    s.price = float(snap_price)
                    if pct is not None:
                        s.extra["pct_change"] = round(pct, 2)
                    if vol_ratio is not None:
                        s.extra["vol_ratio"] = round(vol_ratio, 2)
                    if amount is not None:
                        s.extra["amount_yi"] = round(amount / 1e8, 2)
            elif len(df) > 1 and last_date == today:
                pct = (df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
                for s in signals:
                    s.extra["pct_change"] = round(float(pct), 2)
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
    with_index: bool = True,
) -> dict:
    """后台批量预热K线：把股票日K增量缓存到本地Parquet，加速扫描。

    已有缓存的股票只增量拉取最近几天（很快）；缺失的首次全量拉取
    （较慢但在后台进行，不阻塞服务）。预热完成后扫描直接读本地缓存。

    指数日线（``idx_sh000001``）也在这里刷新：它是大盘择时
    （``regime_ma`` / ``regime_tiers``）的唯一输入，若只有个股日更，
    个股已经走到当日、指数还停在前两天，那两天的择时就会静默退化成
    默认档（不报错，只是回测/影子收益失真）。放在个股循环**之后**，
    这样个股预热因扫描让路提前 break 时，指数仍会被刷新。

    Args:
        codes: 指定代码列表；None 表示全市场
        concurrency: 预热并发（默认取配置 kline_warmup_concurrency）
        with_index: 是否顺带刷新指数日线（默认刷新）
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

    index_info = await _refresh_index() if with_index else {}
    result = {"ok": ok, "fail": fail, "total": len(pool)}
    if with_index:
        result["index"] = index_info
    _last_warmup.update(
        {"result": result, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    )
    logger.info(
        "K线预热完成：成功 %d，失败 %d，共 %d；指数最后 %s",
        ok, fail, len(pool), index_info.get("last") or "未刷新",
    )
    return result


#: 指数日线刷新超时（秒）：单个请求，卡住就跳过，不拖住个股预热的结果返回
_INDEX_TIMEOUT = 20.0


async def _refresh_index() -> dict:
    """刷新指数日线缓存，返回 ``{ok, last, bars, error?}``（失败不影响个股结果）。"""
    try:
        df = await asyncio.wait_for(
            run_in_io(get_index_cached, INDEX_SYMBOL),
            timeout=_INDEX_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("指数K线刷新超时（%.0fs），择时可能滞后", _INDEX_TIMEOUT)
        return {"ok": False, "last": None, "bars": 0, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001 - 指数失败不影响预热主结果
        logger.warning("指数K线刷新失败: %s", exc)
        return {"ok": False, "last": None, "bars": 0, "error": str(exc)}
    if df is None or df.empty:
        return {"ok": False, "last": None, "bars": 0, "error": "empty"}
    return {
        "ok": True,
        "last": str(df["date"].iloc[-1])[:10],
        "bars": int(len(df)),
    }


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
