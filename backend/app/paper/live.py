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
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.config import get_settings
from app.data.cache import db, ledger_scope
from app.market.clock import AM_OPEN, is_trading_day, is_trading_hours, next_open
from app.paper.engine import PaperEngine
from app.paper.ledger import OverlaySettings, sim_specs

logger = logging.getLogger("sas.paper.live")

#: 状态接口里保留的最近成交条数
_MAX_RECENT_TRADES = 20
#: 运行时交易模式的键（auto / manual，持久化在 app_state 表）
_MODE_KEY = "paper_trade_mode"
#: 出现这些 skip 原因时说明信号有效、只是当下没名额/没钱，转入候补队列
_BACKLOG_REASONS = ("持仓已满", "总仓位已达上限", "资金不足一手")
#: 轮动卖出原因里的「换入票分数」，格式由 engine 自己生成：`轮动替换(建仓40分→新票67分)`
_ROT_TARGET_RE = re.compile(r"新票(\d+)分")
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
    #: ⑤ 回踩确认：tick 期间是否曾触及「信号价 ×(1-pullback)」。置位后不再要求
    #: 成交时刻价格仍在回踩线下方（避免反弹一次就永久错过）。
    armed: bool = False

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


@dataclass
class SimRun:
    """一个并行账本的运行时状态：独立引擎（参数被 Overlay 覆盖）+ 独立队列。

    队列必须独立：主账本成交/出队后信号就从 ``_pending`` 消失了，若各账本
    共用一条队列，满仓被拒的那个账本就永远等不到下一次机会（曲线会莫名其妙
    比主账本差），所以入队时按账本复制一份，各自演化到成交或过期。
    """

    account_id: int
    tag: str
    engine: PaperEngine
    pending: dict[str, PendingSignal]


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
        #: 上一批被拒名单的指纹：名单没变就不再打日志（内容相同的 18 条
        #: 每分钟重打一遍，2026-11-03 一天刷了 107 行，新信息被淹掉）
        self._queue_reject_log_key: str = ""
        #: 当日被轮动卖出的代码 → (日期, 允许买回所需最低评分)：
        #: 低于该分不买回（打不过当初换掉它的那只票，买回只是白付摩擦）
        self._rotation_sold: dict[str, tuple[str, float]] = {}
        #: `_rotation_sold` 已经按日期从 `paper_trades` 灌过库的日期标记。
        #: 必须回库读而不是只放内存：重启（每日 SASRestart、部署重启）会把内存清空，
        #: 只靠内存时上午卖掉的票下午就能被买回（2026-11-03 实盘即如此）。
        self._rotation_sold_day: str = ""
        #: 自本次启动累计「因容量未入队」条次：`_queue_rejected` 只留最后一批，
        #: 看不出「一共被挡掉多少只次」，靠这个计数回答（重启归零，不做持久化）
        self._queue_reject_total: int = 0
        #: 最近一批「被判定规则/切入点拒掉」的买入信号（五段式④⑤的证据）
        self._entry_rejected: list[dict[str, Any]] = []
        #: 并行模拟账本：tag -> 独立队列 + 独立引擎（共享信号、各自处置参数）
        self._sim: dict[str, SimRun] = {}
        #: 上次同步账本时的配置指纹（方案串, 初始资金），变了才重建
        self._sim_key: tuple[str, float] | None = None
        #: 被跳过的方案说明（含选股侧参数，tick 级无法复现，需走影子账户）
        self._sim_skipped: list[str] = []
        #: 上一 tick 各账本成交笔数（状态接口展示用）
        self._sim_last: dict[str, int] = {}
        # 重启后从库恢复未过期的撮合队列（避免内存队列丢失）
        self._restore_pending()

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
            for run in self._sim.values():
                run.pending.clear()
            self._persist_pending()
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
            signal = PendingSignal(
                code=code,
                name=str(getattr(rec, "name", "") or code),
                action=action,
                reason=str(getattr(rec, "reason", "") or source),
                created_at=now,
                expire_at=self._expiry(now, action),
                price_at_signal=getattr(rec, "price", None),
                score=_signal_score(rec),
            )
            self._pending[code] = signal
            # 并行账本各存一份：主账本成交后信号就出队了，而账本可能因自己的
            # 名额/仓位被拒，仍要按自己的规则继续等（否则曲线无故比主账本差）
            for run in self._sim.values():
                run.pending[code] = replace(signal)
            added += 1

        buys = []
        for rec in recommendations:
            action = getattr(rec, "action", "")
            code = str(getattr(rec, "code", "") or "")
            if not code:
                continue
            if action == "hold":
                # 最新建议转为观望 → 撤销尚未成交的旧信号（以最新建议为准）
                for run in self._sim.values():
                    run.pending.pop(code, None)
                if self._pending.pop(code, None) is not None:
                    logger.info("建议转观望，撤销待执行信号 %s", code)
                continue
            if action not in ("buy", "sell"):
                continue
            if action == "buy":
                buys.append(rec)
            else:
                _enqueue(rec, "sell")

        # 当日被轮动卖出的票：只有评分**打得过当初换掉它的那只票**才允许买回。
        # 盘中分析每分钟重生信号，不在入口挡一次，tick 里剔掉的下一分钟就回来了
        # （见 tick 里的同一段说明）。
        floors = self._rotation_rebuy_floor(now.strftime("%Y-%m-%d"))
        if floors:
            kept: list = []
            blocked: list[str] = []
            allowed: list[str] = []
            for rec in buys:
                code = str(getattr(rec, "code", "") or "")
                need = floors.get(code)
                if need is None:
                    kept.append(rec)
                    continue
                score = _signal_score(rec)
                if score >= need:
                    kept.append(rec)
                    allowed.append(f"{code}({score:.1f}≥{need:.0f})")
                else:
                    blocked.append(f"{code}({score:.1f}<{need:.0f})")
            buys = kept
            if blocked:
                logger.info(
                    "不给买回（评分不如当初换掉它的票）：%s", ", ".join(blocked))
            if allowed:
                logger.info(
                    "允许买回（评分已超过当初换掉它的票）：%s", ", ".join(allowed))

        # 买入：高分优先占名额；已在队列的同一只票更新建议不重复占位
        #
        # 已持仓的票**不参与容量判定**：引擎下一步必然回「已持仓」把它踢出队列，
        # 让它占名额等于每分钟白扔一个坑（2026-11-03 实盘：301331 恩威医药 83.3 分，
        # 当天 09:30 已建仓，却排在 queue_rejected 第一位）。照常入队是刻意的——
        # 并行账本各有各的持仓，影子账本不能因为主账本持有就丢信号。
        held = {p["code"] for p in db.list_paper_positions()}
        overflow: list[Any] = []
        buys.sort(key=_signal_score, reverse=True)
        for rec in buys:
            code = str(getattr(rec, "code", "") or "")
            if top_m > 0:
                holding = {
                    c for c, s in self._pending.items()
                    if s.action == "buy" and c not in held
                }
                if code not in holding and code not in held and len(holding) >= top_m:
                    rejected.append({
                        "code": code,
                        "name": str(getattr(rec, "name", "") or code),
                        "score": round(_signal_score(rec), 1),
                        "reason": f"交易队列已满（{top_m} 只，按评分择优）",
                    })
                    overflow.append(rec)
                    continue
            _enqueue(rec, "buy")

        # 五段式③收口：因容量被拒的票转存候补，不直接丢掉。
        # 它们此前进不了 _pending，于是也进不了 _backlog、更当不了轮动候选
        # （``rotation_candidates`` 只认候补），高分票就此从视野里彻底消失——
        # 80 分以上的票被 5 个名额挡掉，比「仓位不够」更难发现（WORKLOG 十一节 2026-11-03）。
        parked = sum(1 for rec in overflow if self._backlog_put(rec, now))
        for item in rejected:
            item["deferred"] = item["code"] in self._backlog

        self._queue_rejected = rejected
        self._queue_reject_total += len(rejected)
        # 名单没变就不再打日志：每分钟重复同样的 18 条，真正的变化会被淹掉
        key = ",".join(f"{r['code']}({r['score']})" for r in rejected)
        if rejected and key != self._queue_reject_log_key:
            logger.info(
                "交易队列已满（上限 %d）：%d 条买入未入队，其中 %d 条转入候补 %s",
                top_m, len(rejected), parked, key,
            )
        self._queue_reject_log_key = key
        if added:
            logger.info(
                "实时队列更新 %d 条（%s），待执行 %d 条", added, source, len(self._pending)
            )
        self._persist_pending()
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

    def _mark_armed(self, prices: dict[str, float]) -> None:
        """⑤ 回踩确认：待执行买单期间现价曾触及「信号价×(1-pullback)」即置位。

        没有这一步，``paper_entry_pullback`` 会退化成「成交时刻必须仍在回踩线下方」，
        价格反弹一次就永久错过（回测里对应分钟窗口的「曾触及」口径）。
        """
        pullback = float(getattr(self.settings, "paper_entry_pullback", 0.0) or 0.0)
        if pullback <= 0:
            return
        targets = [*self._pending.values(), *self._backlog.values()]
        for run in self._sim.values():
            targets.extend(run.pending.values())
        for signal in targets:
            if signal.action != "buy" or signal.armed or not signal.price_at_signal:
                continue
            px = prices.get(signal.code)
            if px and px <= signal.price_at_signal * (1 - pullback):
                signal.armed = True
                logger.info(
                    "回踩确认 %s：现价 %.2f ≤ 阈值 %.2f，放开买入",
                    signal.code, px, signal.price_at_signal * (1 - pullback),
                )

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
        self._queue_reject_log_key = ""
        self._queue_reject_total = 0
        self._rotation_sold.clear()
        self._rotation_sold_day = ""
        self._entry_rejected.clear()
        self._sim_last.clear()
        for run in self._sim.values():
            run.pending.clear()
        self._persist_pending()

    def _purge(self, now: datetime) -> None:
        """丢弃过期信号（价格已漂移，执行没有意义）。

        买入信号过期不再直接丢弃，而是降级进候补队列（观察名单持久化）：
        腾出名额 / 下一个买入窗口 / 次日都能优先回补，避免高分候选在轮到成交前
        就被 30 分钟 TTL 静默丢弃（LIVE_REVIEW §P1「待买不过期」）。守「只降不升」——
        观察名单只用于补位，不主动净买入；券源（容量/开关）不足时仍丢弃。
        """
        changed = False
        for code in [c for c, s in self._pending.items() if s.expire_at <= now]:
            signal = self._pending.pop(code)
            changed = True
            if (signal.action == "buy"
                    and self.settings.paper_backlog_enabled
                    and len(self._backlog) < self.settings.paper_backlog_max):
                signal.expire_at = self._backlog_expiry(now)
                if "观察名单" not in (signal.reason or ""):
                    signal.reason = f"{signal.reason or ''}｜过期转入观察名单".strip("｜")
                self._backlog[code] = signal
                logger.info("买入信号过期降级进观察名单 %s %s", code, signal.name)
                continue
            logger.info("信号过期丢弃 %s %s（%s）", code, signal.name, signal.action)
        for code in [c for c, s in self._backlog.items() if s.expire_at <= now]:
            self._backlog.pop(code, None)
            changed = True
        for run in self._sim.values():
            for code in [c for c, s in run.pending.items() if s.expire_at <= now]:
                run.pending.pop(code, None)
                changed = True
        if changed:
            self._persist_pending()

    def _rotation_target_from_reason(self, reason: str) -> float:
        """从轮动卖出原因里取「当初换入那只票」的分数。

        原因格式由 ``engine.execute_recommendations`` 自己生成：
        ``轮动替换(建仓40分→新票67分)``。解析不到（老记录/格式变动）时退回
        ``paper_rotation_min_score``：既不让老记录被意外放行，也不永久封杀。
        """
        m = _ROT_TARGET_RE.search(str(reason or ""))
        if m:
            return float(m.group(1))
        return float(getattr(self.settings, "paper_rotation_min_score", 40.0) or 0.0)

    def _rotation_rebuy_floor(self, day: str) -> dict[str, float]:
        """当日被轮动卖出的代码 → 允许买回所需的最低评分。

        口径（2026-11-03 使用者定义）：**买回来本身没问题，关键是确实值得买回来**。
        所以不是「一律禁止」，而是要求它**打得过当初换掉它的那只票**——
        轮动卖出的理由就是「它不如那只票」，若现在仍不如，买回只是白付一次摩擦。

        今天（2026-11-03）的实例正好被这条判掉：600960 渤海汽车以
        「轮动替换(建仓40分→新票67分)」卖出，13:00:35 时的评分是 41 < 67，
        于是不该买回；若它后来涨到 ≥67，才说明当初卖的判断被推翻，允许买回。

        **以 `paper_trades` 为准，每个交易日回库灌一次**，内存 map 只做当日增量缓存。
        为什么必须回库：重启是常态（每交易日 15:30 的 `SASRestart`、每次部署、盘中手动
        重启）。2026-11-03 就吃过一次亏——12:23 部署重启把纯内存的黑名单清空，13:00:35
        同一只票被买回。回库读还顺带覆盖「功能当天上线、上午的卖出发生在旧代码里」的空档。
        """
        if self._rotation_sold_day != day:
            try:
                rows = db.list_paper_trades(limit=500)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取当日轮动卖出记录失败：%s", exc)
                rows = []
            floors: dict[str, float] = {}
            for r in rows:
                code = str(r.get("code") or "")
                if not code:
                    continue
                if str(r.get("side") or "") != "sell":
                    continue
                if str(r.get("trade_date") or "") != day:
                    continue
                reason = str(r.get("reason") or "")
                if "轮动替换" not in reason:
                    continue
                # 同一只票当日被卖多次时取最高门槛（卖得越多，买回要求越严）
                need = self._rotation_target_from_reason(reason)
                floors[code] = max(floors.get(code, 0.0), need)
            self._rotation_sold = {c: (day, t) for c, t in floors.items()}
            self._rotation_sold_day = day
        return {
            c: t for c, (d, t) in self._rotation_sold.items() if d == day
        }

    def _backlog_put(self, rec, now: datetime) -> bool:
        """把一条「连交易队列都没挤进去」的买入信号放进候补队列。

        与 :meth:`_defer_skips` 分工：那条路处理「已入队、tick 时被判满仓/没钱」，
        这里处理「队列容量满，压根没入队」。两者对下游必须等价——都要能在名额
        释放后补位，也都要能当轮动候选（``rotation_candidates`` 只读候补队列）。

        Args:
            rec: Recommendation 对象
            now: 当前时间（决定候补有效期）

        Returns:
            是否新增。已存在、开关关闭、或候补已满都返回 False。
        """
        if not bool(getattr(self.settings, "paper_backlog_enabled", False)):
            return False
        code = str(getattr(rec, "code", "") or "")
        if not code or code in self._backlog:
            return False
        if len(self._backlog) >= int(getattr(self.settings, "paper_backlog_max", 0) or 0):
            return False
        self._backlog[code] = PendingSignal(
            code=code,
            name=str(getattr(rec, "name", "") or code),
            action="buy",
            reason=str(getattr(rec, "reason", "") or "队列容量已满，转入候补"),
            created_at=now,
            expire_at=self._backlog_expiry(now),
            price_at_signal=getattr(rec, "price", None),
            score=_signal_score(rec),
        )
        return True

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
    # 并行模拟账本（同一份信号，各自处置参数）
    # ------------------------------------------------------------------
    def _ensure_sim(self) -> None:
        """按配置同步并行账本（幂等：配置没变就用现有实例，不重建队列）。"""
        if not bool(getattr(self.settings, "paper_sim_enabled", False)):
            if self._sim:
                self._sim.clear()
                self._sim_last.clear()
            self._sim_key = None
            return
        key = (
            str(getattr(self.settings, "paper_sim_profiles", "") or ""),
            float(getattr(self.settings, "paper_sim_capital", 0) or 0),
        )
        if key == self._sim_key and self._sim:
            return
        specs, skipped = sim_specs(self.settings)
        self._sim_skipped = skipped
        self._sim_key = key
        if not specs:
            self._sim.clear()
            if skipped:
                logger.warning("并行模拟账本没有可用方案：%s", "；".join(skipped))
            return
        by_tag = {s["tag"]: s for s in specs}
        rebuilt: dict[str, SimRun] = {}
        for acct in db.ensure_sim_accounts(specs):
            tag = str(acct.get("tag") or "")
            if tag not in by_tag or not int(acct.get("enabled") or 0):
                continue
            overrides = acct.get("overrides")
            if isinstance(overrides, str):
                try:
                    overrides = json.loads(overrides or "{}")
                except json.JSONDecodeError:
                    overrides = {}
            old = self._sim.get(tag)
            rebuilt[tag] = SimRun(
                account_id=int(acct["account_id"]),
                tag=tag,
                engine=PaperEngine(OverlaySettings(self.settings, dict(overrides or {}))),
                # 首次建立时把主账本当前挂着的信号抄一份，避免当天新建账本错过已入队的票
                pending=old.pending if old else {
                    c: replace(s) for c, s in self._pending.items()
                },
            )
        self._sim = rebuilt
        logger.info(
            "并行模拟账本 %d 个：%s%s",
            len(self._sim), ", ".join(self._sim) or "（无）",
            ("；跳过：" + "；".join(skipped)) if skipped else "",
        )

    async def _tick_sim(self, prices: dict[str, float], now: datetime) -> dict[str, int]:
        """每个账本各自跑一遍：止盈止损 + 队列撮合（共用同一批实时价）。

        与主账本的区别：这里不做候补队列，被「名额/资金不足」拒掉的买单一律留在
        队列里等下一个 tick（队列本身就是候补），因此不会出现「账本比主账本少
        吃一段行情」的偏差。
        """
        if not self._sim:
            return {}
        in_window = self._in_buy_window(now)
        keep_reasons = (*_BACKLOG_REASONS, "取价失败", "等待回踩")
        out: dict[str, int] = {}
        for tag, run in self._sim.items():
            try:
                with ledger_scope(run.account_id):
                    # 开盘保护按各账本自己的参数判定（并行账本可能各有阈值）
                    open_guard = run.engine._open_delayed(now) or run.engine._open_cooldown(now)
                    buy_open = in_window and not open_guard
                    stops = await run.engine.check_stop_loss_target(prices)
                    # 做T引擎：与主账户一致，每个账本按各自参数各自做T
                    # （ledger_scope 已把块内 db.paper_* 调用路由到该账本的 sim_* 表）
                    t_trades = await run.engine.t_tick(prices)
                    ordered = sorted(
                        run.pending.values(),
                        key=lambda s: (s.action != "sell", -s.score),
                    )
                    # 买入只在开盘窗口成交（与主账本、与回测的「次日开盘」同口径）
                    if not buy_open:
                        ordered = [s for s in ordered if s.action != "buy"]
                    recs = [
                        SimpleNamespace(
                            code=s.code, name=s.name, action=s.action, reason=s.reason,
                            score=s.score, price=s.price_at_signal,
                            entry_armed=s.armed,
                        )
                        for s in ordered
                    ]
                    results = (
                        await run.engine.execute_recommendations(recs, prices=prices)
                        if recs else []
                    )
                    for code in list(run.pending):
                        if not buy_open and run.pending[code].action == "buy":
                            continue
                        hit = next((r for r in results if r.code == code), None)
                        if hit is None or hit.side != "skip":
                            run.pending.pop(code, None)
                        elif not any(k in hit.reason for k in keep_reasons):
                            run.pending.pop(code, None)
                out[tag] = len(stops) + len(t_trades) + len([r for r in results if r.side != "skip"])
            except Exception as exc:  # noqa: BLE001 - 单个账本失败不能拖垮其它账本
                logger.warning("并行账本 %s 撮合失败: %s", tag, exc)
                out[tag] = -1
        self._sim_last = out
        return out

    # ------------------------------------------------------------------
    # 撮合
    # ------------------------------------------------------------------
    async def tick(self) -> dict:
        """一次撮合：清过期 → 取实时价 → 止盈止损 → 执行队列。"""
        now = datetime.now()
        self._last_tick = now
        self._ticks += 1
        self._purge(now)
        self._ensure_sim()

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
        # 买入成交时段：默认全时段可成交（paper_buy_open_window_minutes=0），仅受开盘冷静期
        # （paper_open_cooldown_minutes，默认拦开盘前 5 分钟）约束；冷静期内只跑卖出，
        # 买单挂着等冷静期结束（不出队）。按用户 2026-11-04 要求改为「全时段随时可成交」，
        # 配合盘内错峰（paper_buys_per_tick）把积压信号自然摊开，不再「开盘窗口一次性爆发」。
        # 注：此举偏离回测「次日开盘价」口径（记录 #19），换取更真实的盘中成交；
        # 回测/实盘对照需用统一的「成交时点」口径，而非默认对齐到开盘。
        in_window = self._in_buy_window(now)
        # 开盘保护（跳空延迟 / 冷静期）：冷却期内禁止买入，买单留队列等冷却结束（同窗口外逻辑）
        open_guard = self.engine._open_delayed(now) or self.engine._open_cooldown(now)
        buy_open = in_window and not open_guard
        # ⑤ 回踩确认：用 tick 价累积最低价，触及回踩线即放开买入
        self._mark_armed(prices)
        ordered = sorted(
            self._pending.values(),
            key=lambda s: (s.action != "sell", -s.score),
        )
        if not buy_open:
            ordered = [s for s in ordered if s.action != "buy"]
        kept_buys = {
            c for c, s in self._pending.items() if s.action == "buy"
        } if not buy_open else set()
        recs = [
            SimpleNamespace(
                code=s.code, name=s.name, action=s.action, reason=s.reason,
                score=s.score,
                # ⑤ 切入点判定要用信号价（不追高/回踩），丢了就只能裸价成交
                price=s.price_at_signal,
                entry_armed=s.armed,
            )
            for s in ordered
        ]
        # 候补队列：仅在有剩余名额时补位（按评分从高到低），同样受开盘窗口约束
        if self.settings.paper_backlog_enabled and self._backlog and buy_open:
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
                        entry_armed=signal.armed,
                    ))

        # 轮动候选：满仓时 backlog 不会被塞进 recs（只在有剩余名额时补位），
        # 单独传给引擎，轮动才能用上「今天没买到、分还很高」的票。
        # 只在可成交时段内传：冷静期内不传轮动候选（避免「立刻卖 + 立刻买」），
        # 冷静期结束后轮动照常工作。成交时点已不再收敛到开盘，故不强制等下一个开盘。
        rotation_candidates: list[SimpleNamespace] = []
        if buy_open and self.settings.paper_rotation_enabled and self._backlog:
            held_codes = {p["code"] for p in db.list_paper_positions()}
            rotation_candidates = [
                SimpleNamespace(
                    code=s.code, name=s.name, action=s.action, reason=s.reason,
                    score=s.score, price=s.price_at_signal, entry_armed=s.armed,
                )
                for s in self._backlog.values()
                if s.action == "buy" and s.code not in held_codes
            ]

        results = await self.engine.execute_recommendations(
            recs, prices=prices, rotation_candidates=rotation_candidates)

        # 收盘前减仓：80% 是**收盘目标**（盘中允许满仓），把超出部分按最弱优先卖掉。
        # 不并进 results：results 参与「未成交出队 / 候补入队」判定，塞进卖出会误伤同码买单；
        # 成交记录直接走 DB 与 trades 输出（与止盈止损、做T 同一处理方式）。
        trim_trades = await self.engine.eod_trim(prices, now=now)

        # 并行模拟账本：同一批信号、各自处置参数，各自记账（与主账本完全隔离）
        sim = await self._tick_sim(prices, now)

        # 未成交出队策略：只有「取价失败」保留重试，其余（已持仓/已满/资金不足/
        # T+1）本轮确定无法成交，直接出队，避免每个 tick 反复尝试。
        # 「等待回踩」也保留重试：回踩确认是 tick 累积出来的，早晚可能满足
        retry = {
            r.code for r in results
            if r.side == "skip" and (
                r.reason == "取价失败"
                or r.reason.startswith("等待回踩")
                or "买入节奏" in r.reason
            )
        }
        executed = [r for r in results if r.side != "skip"]

        # 实时净值快照：本 tick 只要发生任何成交（止盈止损 / 做T / 队列 / 减仓），
        # 就 upsert 当日净值，使净值曲线在 15:40 收盘结算未跑（服务重启 / 异常）时也有点。
        if stop_trades or t_trades or executed or trim_trades:
            try:
                await self.engine.record_equity_snapshot()
            except Exception:  # noqa: BLE001 - 净值快照失败不影响主流程
                pass

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

        pending_changed = False

        # 轮动卖出的票：当日买回需要「评分 ≥ 当初换掉它的那只票」。
        # 引擎里已有两道闸（选弱时排除「本轮仍建议买入」的票、卖出票不进本轮买入候选），
        # 但盘中分析每分钟都会重生信号，只在单次调用里剔一次不够：不记到日级门槛，
        # 下一分钟的扫描就能把它买回来（2026-11-03：301314 科瑞思 09:30:01 卖 400 买回 300）。
        rot_sold = {
            r.code: self._rotation_target_from_reason(r.reason)
            for r in results
            if r.side == "sell" and "轮动替换" in (r.reason or "")
        }
        if rot_sold:
            day = now.strftime("%Y-%m-%d")
            for code, need in rot_sold.items():
                self._rotation_sold[code] = (day, need)
                self._pending.pop(code, None)
                self._backlog.pop(code, None)
            pending_changed = True
            logger.info(
                "轮动卖出，当日买回门槛：%s",
                ", ".join(f"{c}≥{n:.0f}" for c, n in sorted(rot_sold.items())))

        for code in list(self._pending):
            if code in kept_buys:  # 窗口外的买单留到下个开盘，不算本轮未成交
                continue
            if code not in retry and code not in deferred:
                self._pending.pop(code, None)
                pending_changed = True

        # 已成交/已持仓的候补出队
        held = {p["code"] for p in db.list_paper_positions()}
        traded = {r.code for r in results if r.side != "skip"}
        for code in list(self._backlog):
            if code in held or code in traded:
                self._backlog.pop(code, None)
                pending_changed = True

        # 队列仅在确有变化（出队/撤销）时落库镜像，避免每 tick 无谓写库
        if pending_changed:
            self._persist_pending()

        self._executed += len(executed) + len(stop_trades) + len(t_trades) + len(trim_trades)
        trades = [
            {
                "code": t.code, "name": t.name, "side": t.side,
                "shares": t.shares, "price": round(float(t.price), 2),
                "fee": t.fee, "reason": t.reason,
                "time": now.strftime("%H:%M:%S"),
            }
            for t in [*stop_trades, *t_trades, *executed, *trim_trades]
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
            "sim": sim,
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
    # 撮合队列持久化（重启恢复）
    # ------------------------------------------------------------------
    def _persist_pending(self) -> None:
        """把当前 pending/backlog 快照覆盖式落库，供重启恢复。"""
        items: list[dict] = []
        for s in self._pending.values():
            items.append({
                "code": s.code, "kind": "pending", "name": s.name, "action": s.action,
                "reason": s.reason,
                "created_at": s.created_at.isoformat(timespec="seconds"),
                "expire_at": s.expire_at.isoformat(timespec="seconds"),
                "price_at_signal": s.price_at_signal, "score": s.score,
            })
        for s in self._backlog.values():
            items.append({
                "code": s.code, "kind": "backlog", "name": s.name, "action": s.action,
                "reason": s.reason,
                "created_at": s.created_at.isoformat(timespec="seconds"),
                "expire_at": s.expire_at.isoformat(timespec="seconds"),
                "price_at_signal": s.price_at_signal, "score": s.score,
            })
        try:
            db.save_paper_pending_snapshot(items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("持久化撮合队列失败: %s", exc)

    def _restore_pending(self) -> None:
        """启动时从库恢复未过期的撮合队列（内存队列重启即丢，需回填）。"""
        try:
            rows = db.list_paper_pending()
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复撮合队列失败: %s", exc)
            return
        now = datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        rp = rb = 0
        for r in rows:
            exp = r.get("expire_at")
            if exp and exp <= now_iso:
                continue  # 已过期，丢弃
            try:
                created = datetime.fromisoformat(r["created_at"]) if r.get("created_at") else now
                expire = datetime.fromisoformat(exp) if exp else now
            except ValueError:
                created, expire = now, now
            sig = PendingSignal(
                code=r["code"],
                name=r.get("name") or r["code"],
                action=r.get("action") or "buy",
                reason=r.get("reason") or "",
                created_at=created,
                expire_at=expire,
                price_at_signal=r.get("price_at_signal"),
                score=float(r.get("score") or 0.0),
            )
            if r.get("kind") == "backlog":
                self._backlog[sig.code] = sig
                rb += 1
            else:
                self._pending[sig.code] = sig
                rp += 1
        if rp or rb:
            logger.info("重启恢复撮合队列 pending=%d backlog=%d", rp, rb)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """引擎状态（供 ``GET /api/paper/live``）。"""
        now = datetime.now()
        auto = self.auto_trade_enabled()
        floors = self._rotation_rebuy_floor(now.strftime("%Y-%m-%d"))
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
            # 只保留最后一批看不出「一共被挡掉多少」，故另给累计条次（重启归零）
            "queue_rejected_total": int(self._queue_reject_total),
            # 当日被轮动卖出的代码，以及「买回所需最低评分」= 当初换掉它的那只票的分数
            "rotation_sold_today": sorted(floors),
            "rotation_rebuy_floor": {c: round(v, 1) for c, v in floors.items()},
            # 五段式⑤：切入点参数与本轮被拒原因
            "entry_max_chase": float(getattr(self.settings, "paper_entry_max_chase", 0.0) or 0.0),
            "entry_pullback": float(getattr(self.settings, "paper_entry_pullback", 0.0) or 0.0),
            "entry_timeout_minutes": int(
                getattr(self.settings, "paper_entry_timeout_minutes", 0) or 0),
            "entry_rejected": list(self._entry_rejected),
            # 大盘择时闸门：今天是否弱市、已拦截多少次自动买入（0 次且 weak=True 说明还没 tick）
            "regime": self.engine.regime_state(),
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
            # 持仓直接读库（撮合本就依赖 db），避免重启后内存/库不一致
            "positions": db.list_paper_positions(),
        }


#: 全局单例（调度器 tick 与 API 共享同一队列）
live_trader = LiveTrader()
