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

from app.config import get_settings
from app.data.cache import db
from app.market.clock import is_trading_hours, next_open
from app.paper.engine import PaperEngine

logger = logging.getLogger("sas.paper.live")

#: 状态接口里保留的最近成交条数
_MAX_RECENT_TRADES = 20
#: 运行时交易模式的键（auto / manual，持久化在 app_state 表）
_MODE_KEY = "paper_trade_mode"


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
        self._last_tick: datetime | None = None
        self._last_prices: dict[str, float] = {}
        self._last_trades: list[dict] = []
        self._last_error = ""
        self._ticks = 0
        self._executed = 0

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
        logger.info("模拟交易模式切换为 %s", "自动" if auto else "手动")
        return self.status()

    # ------------------------------------------------------------------
    # 信号入队
    # ------------------------------------------------------------------
    def submit(self, recommendations, source: str = "建议") -> int:
        """把买卖建议放进实时队列（同股覆盖，下个 tick 即执行）。

        Args:
            recommendations: Recommendation 对象（需含 code/action/name/reason）
            source: 来源标记，仅用于日志

        Returns:
            入队条数（开关关闭或非交易日时为 0）
        """
        if not self.settings.paper_live_enabled or not self.auto_trade_enabled():
            return 0
        now = datetime.now()
        expire = self._expiry(now)
        added = 0
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
            self._pending[code] = PendingSignal(
                code=code,
                name=str(getattr(rec, "name", "") or code),
                action=action,
                reason=str(getattr(rec, "reason", "") or source),
                created_at=now,
                expire_at=expire,
                price_at_signal=getattr(rec, "price", None),
                score=_signal_score(rec),
            )
            added += 1
        if added:
            logger.info(
                "实时队列更新 %d 条（%s），待执行 %d 条", added, source, len(self._pending)
            )
        return added

    def _expiry(self, now: datetime) -> datetime:
        """信号有效期：盘中 N 分钟；非交易时段顺延到下一个开盘后计时。"""
        ttl = timedelta(minutes=self.settings.paper_signal_ttl_minutes)
        return now + ttl if is_trading_hours(now) else next_open(now) + ttl

    def clear(self) -> None:
        """清空待执行队列与最近成交（账户重置时调用）。"""
        self._pending.clear()
        self._last_trades.clear()
        self._last_prices.clear()
        self._last_error = ""

    def _purge(self, now: datetime) -> None:
        """丢弃过期信号（价格已漂移，执行没有意义）。"""
        for code in [c for c, s in self._pending.items() if s.expire_at <= now]:
            signal = self._pending.pop(code)
            logger.info("信号过期丢弃 %s %s（%s）", code, signal.name, signal.action)

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
        watch = set(positions) | set(self._pending)
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

        # 2) 队列信号：第 3 次排序——卖出优先，其余按评分从高到低
        ordered = sorted(
            self._pending.values(),
            key=lambda s: (s.action != "sell", -s.score),
        )
        recs = [
            SimpleNamespace(
                code=s.code, name=s.name, action=s.action, reason=s.reason,
                score=s.score,
            )
            for s in ordered
        ]
        results = await self.engine.execute_recommendations(recs, prices=prices)

        # 未成交出队策略：只有「取价失败」保留重试，其余（已持仓/已满/资金不足/
        # T+1）本轮确定无法成交，直接出队，避免每个 tick 反复尝试。
        retry = {r.code for r in results if r.side == "skip" and r.reason == "取价失败"}
        executed = [r for r in results if r.side != "skip"]
        for code in list(self._pending):
            if code not in retry:
                self._pending.pop(code, None)

        self._executed += len(executed) + len(stop_trades)
        trades = [
            {
                "code": t.code, "name": t.name, "side": t.side,
                "shares": t.shares, "price": round(float(t.price), 2),
                "fee": t.fee, "reason": t.reason,
                "time": now.strftime("%H:%M:%S"),
            }
            for t in [*stop_trades, *executed]
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
            "last_tick": self._last_tick.strftime("%H:%M:%S") if self._last_tick else None,
            "last_tick_at": (
                self._last_tick.isoformat(timespec="seconds")
                if self._last_tick else None
            ),
            "ticks": self._ticks,
            "executed": self._executed,
            "pending": [s.to_dict() for s in self._pending.values()],
            "recent_trades": self._last_trades,
            "last_error": self._last_error,
            "prices": {code: round(p, 2) for code, p in self._last_prices.items()},
        }


#: 全局单例（调度器 tick 与 API 共享同一队列）
live_trader = LiveTrader()
