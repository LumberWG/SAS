"""实时模拟交易引擎：事件驱动的信号队列 + 秒级撮合。

设计分工：
  - ``PaperEngine`` 负责交易规则（费用 / T+1 / 仓位 / 整手）的单笔原子操作；
  - ``LiveTrader`` 负责「什么时候买卖」：信号入队 + 高频 tick 撮合。

链路（全链条打通）::

    选股命中 ──► 自动加自选 ──► 立即分析 ──► submit() ──┐
    盘中分析 ──► submit() ───────────────────────────────┤
                                                        ├─► tick()（默认 5s）
    持仓 ──► 实时止盈止损（成本 ±8% / +20%）─────────────┘        │
                                                                  ▼
                                                        按实时价成交、落库

为什么不用「定时批量成交」：定时任务最短也是 5 分钟一轮，价格会漂移；
这里把执行从分析任务里剥出来，信号一到队列，下个 tick（≤5 秒）就用
逐股实时报价成交，止盈止损也从「收盘检查」变成「盘中逐 tick 检查」。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.data.cache import db
from app.market.clock import AM_OPEN, is_trading_day, is_trading_hours, next_open
from app.paper.engine import PaperEngine

logger = logging.getLogger("sas.paper.live")

#: 状态接口里保留的最近成交条数
_MAX_RECENT_TRADES = 20
#: 运行时交易模式的键（auto / manual，持久化在 app_state 表）
_MODE_KEY = "paper_trade_mode"
#: 出现这些 skip 原因时说明信号有效、只是当下没名额/没钱，转入候补队列
_BACKLOG_REASONS = ("持仓已满", "总仓位已达上限", "资金不足一手")
#: 五段式④⑤：被判定规则/切入点拒掉的买入原因（前端「④ 交易池」展示，白名单避免噪音）
_ENTRY_REJECT_REASONS = (
    "追高", "未回踩", "大盘弱势", "不在当日成交额", "不在沪深主板",
    "北交所", "名称缺失", "持仓已满", "总仓位已达上限",
)


@dataclass
class PendingSignal:
    """队列中的一条待执行信号。"""

    code: str
    name: str
    action: str                      # buy / sell
    reason: str
    created_at: datetime
    expire_at: datetime
    price_at_signal: float | None = None
    score: float = 0.0               # 第 2 次排序总分（用于第 3 次排序与仓位权重）

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "action": self.action,
            "reason": self.reason,
            "created_at": self.created_at.strftime("%H:%M:%S"),
            "expire_at": self.expire_at.strftime("%m-%d %H:%M"),
            "waiting_seconds": int((datetime.now() - self.created_at).total_seconds()),
            "price_at_signal": self.price_at_signal,
            "score": round(self.score, 1),
        }


def _signal_score(rec) -> float:
    """建议携带的排序分（缺失或异常按 0 处理）。"""
    try:
        return float(getattr(rec, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class LiveTrader:
    """实时撮合器（进程内单例，持仓与流水全部落 SQLite）。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.engine = PaperEngine()
        self._pending: dict[str, PendingSignal] = {}
        # 候补队列：因满仓/资金不足没买成的高分票，等有名额时优先买入
        self._backlog: dict[str, PendingSignal] = {}
        self._last_tick: datetime | None = None
        self._last_prices: dict[str, float] = {}
        self._last_trades: list[dict] = []
        self._last_error = ""
        self._ticks = 0
        self._executed = 0
        #: 最近一批「因队列容量未入队」的买入信号（五段式③的判定证据）
        self._queue_rejected: list[dict[str, Any]] = []
        #: 最近一批「被判定规则/切入点拒掉」的买入信号（五段式④⑤的证据）
        self._entry_rejected: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 交易模式（自动 / 手动）
    # ------------------------------------------------------------------
    def auto_trade_enabled(self) -> bool:
        """当前是否为自动交易模式。

        以 DB 里的运行时开关为准（重启后保留），未设置时取
        ``PAPER_AUTO_TRADE`` 配置默认值。
        """
        mode = db.get_state(_MODE_KEY)
        if mode is None:
            return bool(self.settings.paper_auto_trade)
        return mode == "auto"

    def set_mode(self, auto: bool) -> dict:
        """切换自动/手动交易模式，返回最新状态。

        切到手动时清空待执行队列：避免切回自动后旧信号集中补成交。
        """
        db.set_state(_MODE_KEY, "auto" if auto else "manual")
        if not auto:
            self._pending.clear()
            self._backlog.clear()
        logger.info("模拟交易模式切换为 %s", "自动" if auto else "手动")
        return self.status()

    # ------------------------------------------------------------------
    # 信号入队
    # ------------------------------------------------------------------
    def submit(self, recommendations, source: str = "建议") -> int:
        """把买卖建议放进实时队列（同股覆盖，下个 tick 即执行）。

        五段式③的收口：买入信号受队列容量 ``paper_trade_queue_top_m`` 约束
        （同一批按 14 因子总分降序择优），**卖出信号不受限**（释放风险优先）。
        超出容量的买单不入队，原因记在 ``_queue_rejected``（由 /api/paper/live 展示）。

        Args:
            recommendations: Recommendation 对象（需含 code/action/name/reason）
            source: 来源标记，仅用于日志

        Returns:
            入队条数（开关关闭或非交易日时为 0）
        """
        if not self.settings.paper_live_enabled or not self.auto_trade_enabled():
            return 0
        now = datetime.now()
        added = 0
        rejected: list[dict[str, Any]] = []
        top_m = int(getattr(self.settings, "paper_trade_queue_top_m", 0) or 0)

        def _enqueue(rec, action: str) -> None:
            nonlocal added
            code = str(getattr(rec, "code", "") or "")
            self._pending[code] = PendingSignal(
                code=code,
                name=str(getattr(rec, "name", "") or code),
                action=action,
                reason=str(getattr(rec, "reason", "") or source),
                created_at=now,
                expire_at=self._expiry(now, action),
                price_at_signal=getattr(rec, "price", None),
                score=_signal_score(rec),
            )
            added += 1

        buys = []
        for rec in recommendations:
            action = getattr(rec, "action", "")
            code = str(getattr(rec, "code", "") or "")
            if not code:
                continue
            if action == "hold":
                # 最新建议转为观望 → 撤销尚未成交的旧信号（以最新建议为准）
                if self._pending.pop(code, None) is not None:
                    logger.info("建议转观望，撤销待执行信号 %s", code)
                continue
            if action not in ("buy", "sell"):
                continue
            if action == "buy":
                buys.append(rec)
            else:
                _enqueue(rec, "sell")

        # 买入：高分优先占名额；已在队列的同一只票更新建议不重复占位
        buys.sort(key=_signal_score, reverse=True)
        for rec in buys:
            code = str(getattr(rec, "code", "") or "")
            if top_m > 0:
                holding = {c for c, s in self._pending.items() if s.action == "buy"}
                if code not in holding and len(holding) >= top_m:
                    rejected.append({
                        "code": code,
                        "name": str(getattr(rec, "name", "") or code),
                        "score": round(_signal_score(rec), 1),
                        "reason": f"交易队列已满（{top_m} 只，按评分择优）",
                    })
                    continue
            _enqueue(rec, "buy")

        self._queue_rejected = rejected
        if rejected:
            logger.info(
                "交易队列已满（上限 %d）：%d 条买入未入队 %s",
                top_m, len(rejected),
                ", ".join(f"{r['code']}({r['score']})" for r in rejected),
            )
        if added:
            logger.info(
                "实时队列更新 %d 条（%s），待执行 %d 条", added, source, len(self._pending)
            )
        return added

    def _window_minutes(self) -> int:
        return max(0, int(getattr(self.settings, "paper_buy_open_window_minutes", 0) or 0))

    def _buy_window(self, day: datetime) -> datetime:
        """当日开盘撮合窗口的起点（09:30）。"""
        return day.replace(hour=AM_OPEN // 60, minute=AM_OPEN % 60, second=0, microsecond=0)

    def _in_buy_window(self, now: datetime) -> bool:
        """是否处于开盘撮合窗口（09:30 起 N 分钟内）。N<=0 视为不限制。"""
        window = self._window_minutes()
        if window <= 0:
            return True
        start = self._buy_window(now)
        return start <= now < start + timedelta(minutes=window)

    def _next_buy_window_start(self, now: datetime) -> datetime:
        """下一个开盘撮合窗口的起点：买单只可能在这里成交。

        回测按「次日开盘价」成交，所以信号产生的时点无关紧要；
        实盘把成交收敛到开盘那一刻，两边口径才可比（见 BACKTEST_LOG 记录 #19）。
        """
        start = self._buy_window(now)
        if now < start and is_trading_day(now):
            return start
        day = now
        for _ in range(10):
            day = day + timedelta(days=1)
            if is_trading_day(day):
                return self._buy_window(day)
        return now + timedelta(days=1)

    def _expiry(self, now: datetime, action: str = "") -> datetime:
        """信号有效期：盘中 N 分钟；非交易时段顺延到下一个开盘后计时。

        买单在「开盘窗口」模式下只可能在下一个窗口成交，有效期必须相应顺延，
        否则 13:01 产生的买单会在 13:31 过期，永远等不到次日开盘。
        """
        ttl_minutes = int(self.settings.paper_signal_ttl_minutes)
        if action == "buy":
            # 五段式⑤：切入点超时（0 = 沿用统一 TTL）
            timeout = int(getattr(self.settings, "paper_entry_timeout_minutes", 0) or 0)
            if timeout > 0:
                ttl_minutes = min(ttl_minutes, timeout)
        ttl = timedelta(minutes=ttl_minutes)
        window = self._window_minutes()
        if action == "buy" and window > 0 and not self._in_buy_window(now):
            return self._next_buy_window_start(now) + timedelta(minutes=window) + ttl
        return now + ttl if is_trading_hours(now) else next_open(now) + ttl

    def clear(self) -> None:
        """清空待执行队列、候补队列与最近成交（账户重置时调用）。"""
        self._pending.clear()
        self._backlog.clear()
        self._last_trades.clear()
        self._last_prices.clear()
        self._last_error = ""
        self._queue_rejected.clear()
        self._entry_rejected.clear()

    def _purge(self, now: datetime) -> None:
        """丢弃过期信号（价格已漂移，执行没有意义）。"""
        for code in [c for c, s in self._pending.items() if s.expire_at <= now]:
            signal = self._pending.pop(code)
            logger.info("信号过期丢弃 %s %s（%s）", code, signal.name, signal.action)
        for code in [c for c, s in self._backlog.items() if s.expire_at <= now]:
            self._backlog.pop(code, None)

    def _defer_skips(self, results, now: datetime) -> set[str]:
        """把「名额/资金不足」的买入信号转入候补队列，返回被延后的代码集合。

        只延后信号本身有效、只是当下买不进的场景（满仓/总仓位上限/资金不足一手）；
        已持仓、评分不足、取价失败、T+1 等不进入候补。
        """
        deferred: set[str] = set()
        if not self.settings.paper_backlog_enabled:
            return deferred
        for r in results:
            if r.side != "skip" or not any(k in r.reason for k in _BACKLOG_REASONS):
                continue
            signal = self._pending.get(r.code) or self._backlog.get(r.code)
            if signal is None or signal.action != "buy":
                continue
            if (
                len(self._backlog) >= self.settings.paper_backlog_max
                and r.code not in self._backlog
            ):
                continue
            signal.expire_at = self._backlog_expiry(now)
            self._backlog[r.code] = signal
            deferred.add(r.code)
        return deferred

    def _backlog_expiry(self, now: datetime) -> datetime:
        """候补有效到当日收盘；非交易时段则顺延到下一个交易日收盘。

        开盘窗口模式下候补也只可能在下一个开盘窗口成交，故同样顺延。
        """
        window = self._window_minutes()
        if window > 0 and not self._in_buy_window(now):
            return self._next_buy_window_start(now) + timedelta(minutes=window)
        if is_trading_hours(now):
            return now.replace(hour=15, minute=0, second=0, microsecond=0)
        return next_open(now) + timedelta(hours=5, minutes=30)

    # ------------------------------------------------------------------
    # 撮合
    # ------------------------------------------------------------------
    async def tick(self) -> dict:
        """一次撮合：清过期 → 取实时价 → 止盈止损 → 执行队列。"""
        now = datetime.now()
        self._last_tick = now
        self._ticks += 1
        self._purge(now)

        if not self.settings.paper_live_enabled:
            return {"skipped": "实时引擎未启用"}
        if not is_trading_hours(now):
            return {"skipped": "非交易时段", "pending": len(self._pending)}

        positions = {p["code"]: p for p in db.list_paper_positions()}
        watch = set(positions) | set(self._pending) | set(self._backlog)
        if not watch:
            return {"skipped": "无持仓与信号"}

        prices = await self._fetch_prices(watch)
        if not prices:
            self._last_error = "实时报价获取失败"
            return {"skipped": self._last_error, "pending": len(self._pending)}
        self._last_prices = prices
        self._last_error = ""

        # 手动模式：只刷新报价供页面参考，不执行任何买卖（含止盈止损），
        # 由用户在模拟交易页自己下单。
        if not self.auto_trade_enabled():
            return {
                "skipped": "手动交易模式",
                "prices": len(prices),
                "pending": len(self._pending),
            }

        # 1) 实时止盈止损（T+1 由 engine 内部拦截）
        stop_trades = await self.engine.check_stop_loss_target(prices)

        # 1.5) 做T引擎（默认关闭；开启后按 T+1 多批次做T）
        t_trades = await self.engine.t_tick(prices)

        # 2) 队列信号：第 3 次排序——卖出优先，其余按评分从高到低
        # 买入只在开盘窗口成交：窗口外只跑卖出，买单继续挂着等下一个开盘（不出队）。
        # 回测按「次日开盘价」成交，实盘此前在盘中分析时刻（如 13:01）按实时价
        # 一把买入，系统性买高 —— 2 天 17 笔成交 vs 回测同区间 3 笔（记录 #19）。
        in_window = self._in_buy_window(now)
        ordered = sorted(
            self._pending.values(),
            key=lambda s: (s.action != "sell", -s.score),
        )
        if not in_window:
            ordered = [s for s in ordered if s.action != "buy"]
        kept_buys = {
            c for c, s in self._pending.items() if s.action == "buy"
        } if not in_window else set()
        recs = [
            SimpleNamespace(
                code=s.code, name=s.name, action=s.action, reason=s.reason,
                score=s.score,
                # ⑤ 切入点判定要用信号价（不追高/回踩），丢了就只能裸价成交
                price=s.price_at_signal,
            )
            for s in ordered
        ]
        # 候补队列：仅在有剩余名额时补位（按评分从高到低），同样受开盘窗口约束
        if self.settings.paper_backlog_enabled and self._backlog and in_window:
            held = {p["code"] for p in db.list_paper_positions()}
            slots = self.settings.paper_max_positions - len(held)
            if slots > 0:
                queued = {r.code for r in recs}
                for signal in sorted(self._backlog.values(), key=lambda s: -s.score):
                    if signal.code in held or signal.code in queued:
                        continue
                    recs.append(SimpleNamespace(
                        code=signal.code, name=signal.name, action=signal.action,
                        reason=signal.reason, score=signal.score,
                        price=signal.price_at_signal,
                    ))

        results = await self.engine.execute_recommendations(recs, prices=prices)

        # 未成交出队策略：只有「取价失败」保留重试，其余（已持仓/已满/资金不足/
        # T+1）本轮确定无法成交，直接出队，避免每个 tick 反复尝试。
        retry = {r.code for r in results if r.side == "skip" and r.reason == "取价失败"}
        executed = [r for r in results if r.side != "skip"]

        # 五段式④⑤的可观测性：本轮被判定规则/切入点拒掉的买入原因
        self._entry_rejected = [
            {
                "code": r.code,
                "name": r.name,
                "reason": r.reason,
                "chase": self.settings.paper_entry_max_chase,
            }
            for r in results
            if r.side == "skip" and any(k in r.reason for k in _ENTRY_REJECT_REASONS)
        ][:10]

        # 候补：因满仓/总仓位上限/资金不足没买成的高分票，存起来等名额释放
        deferred = self._defer_skips(results, now)

        for code in list(self._pending):
            if code in kept_buys:  # 窗口外的买单留到下个开盘，不算本轮未成交
                continue
            if code not in retry and code not in deferred:
                self._pending.pop(code, None)

        # 已成交/已持仓的候补出队
        held = {p["code"] for p in db.list_paper_positions()}
        traded = {r.code for r in results if r.side != "skip"}
        for code in list(self._backlog):
            if code in held or code in traded:
                self._backlog.pop(code, None)

        self._executed += len(executed) + len(stop_trades) + len(t_trades)
        trades = [
            {
                "code": t.code, "name": t.name, "side": t.side,
                "shares": t.shares, "price": round(float(t.price), 2),
                "fee": t.fee, "reason": t.reason,
                "time": now.strftime("%H:%M:%S"),
            }
            for t in [*stop_trades, *t_trades, *executed]
        ]
        if trades:
            self._last_trades = (trades + self._last_trades)[:_MAX_RECENT_TRADES]
            logger.info(
                "实时撮合成交 %d 笔：%s", len(trades),
                ", ".join(f"{t['side']}{t['code']}@{t['price']}" for t in trades),
            )
        return {
            "trades": len(trades),
            "stop_loss": len(stop_trades),
            "pending": len(self._pending),
        }

    async def _fetch_prices(self, codes: set[str]) -> dict[str, float]:
        """批量取实时价：新浪逐股行情（秒级）优先，缺失的用内存快照兜底。"""
        from app.data.fallback import code_to_symbol, sina_quotes

        prices: dict[str, float] = {}
        try:
            symbols = [code_to_symbol(code) for code in sorted(codes)]
            df = await asyncio.to_thread(sina_quotes, symbols)
            if df is not None and not df.empty and "price" in df.columns:
                for row in df.itertuples(index=False):
                    price = getattr(row, "price", None)
                    if price is not None and price == price and float(price) > 0:
                        prices[str(row.code)] = float(price)
        except Exception as exc:  # noqa: BLE001 - 换快照兜底
            logger.debug("新浪实时报价失败: %s", exc)

        missing = codes - set(prices)
        if missing:
            try:
                from app.data.snapshot_store import quote_map

                quotes = await asyncio.to_thread(quote_map)
                for code in missing:
                    quote = quotes.get(code)
                    if quote:
                        prices[code] = float(quote["price"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("快照兜底取价失败: %s", exc)
        return prices

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """引擎状态（供 ``GET /api/paper/live``）。"""
        now = datetime.now()
        auto = self.auto_trade_enabled()
        return {
            "enabled": bool(auto and self.settings.paper_live_enabled),
            "engine_enabled": bool(self.settings.paper_live_enabled),
            "auto_trade": auto,
            "mode": "auto" if auto else "manual",
            "trading": is_trading_hours(now),
            "tick_interval": self.settings.paper_tick_interval,
            "signal_ttl_minutes": self.settings.paper_signal_ttl_minutes,
            # 五段式③：队列容量与「因容量被拒」的买入候选（0=不限制）
            "queue_top_m": int(getattr(self.settings, "paper_trade_queue_top_m", 0) or 0),
            "queue_rejected": list(self._queue_rejected),
            # 五段式⑤：切入点参数与本轮被拒原因
            "entry_max_chase": float(getattr(self.settings, "paper_entry_max_chase", 0.0) or 0.0),
            "entry_pullback": float(getattr(self.settings, "paper_entry_pullback", 0.0) or 0.0),
            "entry_timeout_minutes": int(
                getattr(self.settings, "paper_entry_timeout_minutes", 0) or 0),
            "entry_rejected": list(self._entry_rejected),
            "last_tick": self._last_tick.strftime("%H:%M:%S") if self._last_tick else None,
            "last_tick_at": (
                self._last_tick.isoformat(timespec="seconds")
                if self._last_tick else None
            ),
            "ticks": self._ticks,
            "executed": self._executed,
            "pending": [s.to_dict() for s in self._pending.values()],
            "backlog": [
                s.to_dict()
                for s in sorted(self._backlog.values(), key=lambda s: -s.score)[:10]
            ],
            "backlog_count": len(self._backlog),
            "recent_trades": self._last_trades,
            "last_error": self._last_error,
            "prices": {code: round(p, 2) for code, p in self._last_prices.items()},
        }


#: 全局单例（调度器 tick 与 API 共享同一队列）
live_trader = LiveTrader()
