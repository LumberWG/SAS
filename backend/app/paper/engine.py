"""模拟交易引擎：自动执行买卖信号、管理仓位、记录净值。

设计目标：验证选股思路的准确性——系统分析产生的买卖建议
（或策略信号）由本引擎自动执行，形成可复盘的资金曲线。

交易规则（对齐 A 股实盘，简化但透明）:
  1. 初始资金默认 10 万，账户为单账户（个人使用）；
  2. 仓位管理：按建议评分加权（第 3 次排序）——评分高者先买、分到更多资金，
     预算 = min(可投预算 × 加权占比, 总资产×20%)，最多持有 max_positions（默认10）只；
     评分分档：≥40 全额、25~40 权重打 5 折、<25 只记录不下单；
     总仓位上限 paper_max_total_exposure（默认 80%，即常留 20% 现金）；
  2.1 轮动替换：满仓时若新票评分 >= paper_rotation_min_score 且比最弱持仓的建仓
     评分高出 paper_rotation_gap，则卖出最弱持仓换入（T+1、每日限次由配置控制）。
     换入票必须**真能买进**：与买入阶段同一套交易池/切入点闸门，且卖出所得够买一手，
     否则不卖（避免「只卖不买」把仓位凭空降下来）；手动建仓与当日买入的持仓不参与选弱。
  3. 整手交易：买入数量向下取整到 100 股；
  4. T+1：当日买入次日才能卖出；
  5. 费用：佣金万2.5（最低5元）+ 卖出印花税千1 + 双边过户费万0.1；
  6. 成交价：执行时刻的最新价（实时快照），快照不可用时
     退化用最近一根K线收盘价；
  7. 止盈止损：默认止损=成本-8%、止盈=成本+20%（可由调用方覆盖），
     盘中由实时撮合逐 tick 检查（收盘结算时再兜底检查一次）。

调度联动（scheduler）:
  - 建议产生后进入 LiveTrader 队列，由 paper_tick（默认 5s）实时成交；
  - 每日收盘调用 settle() 记录净值快照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from app.analysis.regime import detect_regime
from app.analysis.policy_floor import detect_policy_floor, circuit_breaker_state
from app.config import get_settings
from app.data.cache import db
from app.data.service import get_kline_cached

logger = logging.getLogger("sas.paper")

# ---- 大盘股过滤（「③ 交易队列」页开关，运行时存 app_state）----
_MV_CACHE: tuple[float, dict[str, float]] | None = None
_MV_TTL = 60.0      # 秒：市值随价格变动，60s 刷一次足够，不必每个 tick 重建


def _market_cap_map() -> dict[str, float]:
    """``code → 总市值(元)``，取自实时快照的 ``total_mv``；取不到返回空字典。

    空字典时下游按「不拦」处理——数据源抖动不应该让交易整轮停摆。
    """
    global _MV_CACHE
    now = time.time()
    if _MV_CACHE is not None and now - _MV_CACHE[0] < _MV_TTL:
        return _MV_CACHE[1]
    out: dict[str, float] = {}
    try:
        from app.data.snapshot_store import snapshot_store

        snap = snapshot_store.peek_snapshot()
        if snap is not None and not snap.empty and "total_mv" in snap.columns:
            mv = pd.to_numeric(snap["total_mv"], errors="coerce")
            for code, val in zip(snap["code"], mv):
                if val == val and val > 0:      # 排除 NaN / 0
                    out[str(code).zfill(6)] = float(val)
    except Exception as exc:  # noqa: BLE001
        logger.warning("市值表构建失败（本轮不限制大市值）: %s", exc)
        return {}
    _MV_CACHE = (now, out)
    return out


def large_cap_rule() -> tuple[bool, float]:
    """运行时的大盘股规则：``(是否启用, 阈值元)``。

    默认取配置（``paper_exclude_large_cap=True`` / ``paper_large_cap_yi=100``），
    页面上改过之后以 ``app_state`` 为准（重启保留）。读取失败退回配置，绝不抛错。
    """
    s = get_settings()
    enabled = bool(getattr(s, "paper_exclude_large_cap", True))
    limit_yi = float(getattr(s, "paper_large_cap_yi", 100.0) or 0.0)
    try:
        raw_on = db.get_state("paper_exclude_large_cap")
        if raw_on is not None:
            enabled = str(raw_on).strip().lower() not in ("0", "false", "")
        raw_limit = db.get_state("paper_large_cap_yi")
        if raw_limit is not None:
            limit_yi = float(raw_limit)
    except Exception as exc:  # noqa: BLE001
        logger.debug("读大盘股开关失败，用配置值: %s", exc)
    return enabled, limit_yi * 1e8

# ---- 费用参数（A 股标准费率，个人可调） ----
FEE_COMMISSION_RATE = 2.5 / 10000   # 佣金万2.5，双向
FEE_COMMISSION_MIN = 5.0            # 单笔最低佣金 5 元
FEE_STAMP_RATE = 1 / 1000           # 印花税千1，仅卖出
FEE_TRANSFER_RATE = 0.1 / 10000     # 过户费万0.1，双向

# ---- 风控参数 ----
SINGLE_POSITION_MAX_RATIO = 0.20    # 单票市值不超过总资产 20%
STOP_LOSS_RATIO = 0.08              # 默认止损 -8%
TAKE_PROFIT_RATIO = 0.20            # 默认止盈 +20%
LOT_SIZE = 100                      # A股整手


def _rec_score(rec) -> float:
    """建议携带的排序分（缺失或异常按 0 处理）。"""
    try:
        return float(getattr(rec, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rec_price(rec) -> float | None:
    """建议携带的「信号价」（缺失/非法返回 None → 不做切入点限制）。"""
    try:
        value = float(getattr(rec, "price", None) or 0.0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _is_bj(code: str) -> bool:
    """北交所代码：920xxx / 43xxxx / 83xxxx / 87xxxx（4、8 开头的旧三类同属北交所）。"""
    return str(code).startswith(("920", "43", "83", "87", "4", "8"))


def _name_ok(name: str, code: str) -> bool:
    """名称是否可信：空、等于代码、含替换字符或疑似乱码一律拒绝下单。

    「名称取不到」往往是股票池不完整的信号（池子里没有这只票），
    静默用代码当名称继续买，等于绕过了池子约束（见 BACKTEST_LOG 记录 #19）。
    """
    text = str(name or "").strip()
    if not text or text == str(code):
        return False
    if "\ufffd" in text:
        return False
    try:
        # 能 round-trip 还原且与原串不同 → UTF-8 被当 GBK 解的乱码
        if text.encode("gbk").decode("utf-8") != text:
            return False
    except (UnicodeEncodeError, UnicodeDecodeError):
        return True
    return True


#: 当日「成交额 TopN」池缓存：(日期, N, 代码集合)
_pool_cache: tuple[str, int, set[str]] | None = None


@dataclass
class TradeResult:
    """单笔执行结果。"""

    code: str
    name: str
    side: str            # buy / sell / skip
    shares: int
    price: float
    amount: float
    fee: float
    reason: str


class PaperEngine:
    """模拟交易引擎（进程内单例，状态全部落 SQLite）。"""

    def __init__(self, settings: object | None = None) -> None:
        # settings 可注入：并行模拟账本用 ``OverlaySettings`` 覆盖 paper_* 参数，
        # 引擎内所有 ``self.settings.xxx`` 一行不改就能跑出各自的处置口径。
        self.settings = settings if settings is not None else get_settings()
        # 大盘择时闸门的当日状态：让 /api/paper/live 能直接回答「今天为什么没买」，
        # 不用再翻 data/logs 里的「大盘弱势」行（2026-10-30 全天 1999 条，就是从那翻出来的）。
        self._regime_day: str = ""
        self._regime_paused: int = 0
        #: (日期, 是否弱市)：判定只依赖**前一交易日**收盘，同一天内结果不变，
        #: 缓存后 tick 不必每 5 秒读一次指数 parquet。
        self._weak_cache: tuple[str, bool] = ("", False)
        #: (日期, 动态机制层结果)：与 _weak_cache 同口径，按自然日缓存，避免重复读 parquet。
        self._regime_dyn_cache: tuple[str, dict] = ("", {})
        #: 硬性回撤熔断状态：峰值净值与是否触发，跨 tick 保留；落库后也跨重启保留。
        self._cb_peak: float | None = None
        self._cb_tripped: bool = False
        self._cb_prev_tripped: bool = False  # 仅用于触发/解除时打一条日志，避免每 tick 刷屏
        #: 上次落库的 (峰值, 触发态)：只在变化时写库，避免每 tick 一次无意义写入
        self._cb_persisted: tuple[float, bool] | None = None

    # ------------------------------------------------------------------
    # 账户与行情
    # ------------------------------------------------------------------
    def ensure_account(self) -> dict:
        """确保账户存在（首次调用时按配置初始化）。"""
        account = db.get_paper_account()
        if account is None:
            account = db.init_paper_account(self.settings.paper_initial_capital)
            logger.info("模拟账户初始化：%.0f 元", self.settings.paper_initial_capital)
        # 批次模型兼容：把尚无批次的现有持仓补建为一个批次（幂等）
        db.ensure_lots_migrated(datetime.now().strftime("%Y-%m-%d"))
        return account

    @staticmethod
    def _with_snapshot_prices(
        prices: dict[str, float] | None,
    ) -> dict[str, float]:
        """把内存快照的最新价合并进批量报价（给定报价优先，同代码覆盖）。

        批量核算（:meth:`total_asset` / :meth:`account_detail`）会对每只持仓
        各调一次 :meth:`_latest_price`；旧实现里调用方不传 ``prices`` 时，
        每只持仓都要 ``asyncio.to_thread(get_snapshot)`` 重取一次全市场快照
        （20 只持仓 = 20 次线程切换 + 20 次整表过滤）。这里一次性合成映射，
        之后全部命中 ``_latest_price`` 的第一条分支。

        快照缺失（未开盘/数据源故障）时返回原 ``prices``，
        逐只取价的老路径与降级口径完全不变。
        """
        try:
            from app.data.snapshot_store import quote_map

            merged = {code: q["price"] for code, q in quote_map().items()}
        except Exception:  # noqa: BLE001 - 快照不可用则退回逐只取价
            return dict(prices or {})
        if prices:
            merged.update(prices)
        return merged

    async def _latest_price(
        self, code: str, prices: dict[str, float] | None = None
    ) -> float | None:
        """最新价：调用方给定报价 > 实时快照 > K线收盘价。

        ``prices`` 供实时撮合使用：一次批量请求拿到的逐股最新价，
        避免每只股票都去切全市场快照（慢且滞后）。
        """
        if prices and code in prices:
            return float(prices[code])
        try:
            from app.data.snapshot_store import snapshot_store

            snapshot = await asyncio.to_thread(snapshot_store.get_snapshot)
            row = snapshot[snapshot["code"] == code]
            if not row.empty:
                price = row["price"].iloc[0]
                if price == price and price > 0:  # 排除 NaN
                    return float(price)
        except Exception as exc:  # noqa: BLE001
            logger.debug("快照取价失败 %s: %s", code, exc)

        try:
            df = await asyncio.to_thread(get_kline_cached, code)
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            logger.debug("K线取价失败 %s: %s", code, exc)
        return None

    def _top_amount_codes(self) -> set[str] | None:
        """当日成交额 TopN 代码集合（与回测的股票池口径对齐）。

        回测的股票池是「每日成交额 TopN」，实盘此前是全市场扫描，
        于是回测里选不到的小票（尤其北交所）被真实买入 —— 两边的策略
        根本不是同一个，任何回测结论都无法解释实盘（BACKTEST_LOG 记录 #19）。

        取不到快照时返回 ``None``（本轮不限制），不让数据源抖动直接停掉交易。
        """
        top_n = int(getattr(self.settings, "paper_pool_top_n", 0) or 0)
        if top_n <= 0:
            return None
        global _pool_cache
        today = datetime.now().strftime("%Y-%m-%d")
        if _pool_cache is not None and _pool_cache[0] == today and _pool_cache[1] == top_n:
            return _pool_cache[2]
        try:
            from app.data.snapshot_store import snapshot_store

            snap = snapshot_store.peek_snapshot()
            if snap is None or snap.empty or "amount" not in snap.columns:
                return None
            ranked = snap.dropna(subset=["amount"]).sort_values("amount", ascending=False)
            codes = {str(c).zfill(6) for c in ranked["code"].head(top_n)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("成交额 TopN 池构建失败（本轮不限制股票池）: %s", exc)
            return None
        _pool_cache = (today, top_n, codes)
        return codes

    def _buy_allowed(self, code: str, name: str) -> tuple[bool, str]:
        """下单前的准入检查：板块范围 / 北交所 / 大市值 / 名称可信度 / 成交额 TopN 池。

        板块范围与选股 scanner 统一口径（app.data.scope.is_tradable）：只做
        沪深主板+创业板，排除科创板(688)/北交所(92,43,83,87,88)/B股/ST。

        大市值两套口径合并到「③ 交易队列」的一个开关上：
        - 开关**开**（默认）：`is_tradable` 的巨无霸黑名单生效，且 ``总市值 > 阈值``
          （默认 100 亿）的票一律不进买入队列；
        - 开关**关**：黑名单与市值阈值同时失效（此时才允许买银行/石油这类票）。
        """
        code = str(code or "").zfill(6)
        if getattr(self.settings, "paper_exclude_bj", False) and _is_bj(code):
            return False, "北交所股票已排除（PAPER_EXCLUDE_BJ）"
        from app.data.scope import is_tradable
        large_on, large_limit = large_cap_rule()
        if not is_tradable(code, name, exclude_giant=large_on)[0]:
            return False, "不在沪深主板+创业板范围（scope 过滤）"
        if large_on and large_limit > 0:
            cap = _market_cap_map().get(code)
            if cap is not None and cap > large_limit:
                return False, (
                    f"总市值 {cap / 1e8:.0f}亿 > {large_limit / 1e8:.0f}亿"
                    "（大盘股已排除，可在交易队列页关闭）"
                )
        if not _name_ok(name, code):
            return False, "名称缺失或乱码，拒绝下单（股票池可能不完整）"
        pool = self._top_amount_codes()
        if pool is not None and code not in pool:
            top_n = int(getattr(self.settings, "paper_pool_top_n", 0) or 0)
            return False, f"不在当日成交额Top{top_n}池（与回测口径对齐）"
        return True, ""

    def entry_gate(
        self, rec, *, positions: dict, timing_pause: bool, ignore_slots: bool = False,
    ) -> tuple[bool, str]:
        """五段式④ 交易池判定（前置闸，不取价），判定顺序即语义：

        1. 板块范围 / 北交所 / 大市值 / 名称 / 成交额池（``_buy_allowed``，**永久性**拒绝）；
        2. 择时：仅 ``timing_pause=True``（**「全暂停」档**）时拦下；降仓档放行，
           由 ``exposure_cap`` 控总量，不在这里拒；
        3. 已持仓；
        4. 持仓名额 ``paper_max_positions``。

        第 1 步必须排在第 2 步之前：否则弱势期间所有票都显示「大盘弱势」，
        交易池面板看不出 688 这类票其实是被「不在可交易范围」拒掉的
        （真实原因被临时闸门盖住）。

        ``ignore_slots=True`` 只供轮动替换使用：那一刻**必然是满仓**，名额检查会
        把所有候选一律拒成「持仓已满」，于是「换入票是否真的买得进」永远无法预判。
        跳过名额这一步，其余（永久性拒绝 / 已持仓）照常生效。

        与 :meth:`entry_price_ok` 合起来构成 ``entry_check``；拆成两段是为了
        保持原实现的取价时机——被前置闸拒掉的票不必再去拉一次实时价。
        """
        code = str(getattr(rec, "code", "") or "")
        name = str(getattr(rec, "name", "") or code)
        # 永久性拒绝优先于临时性择时（见 docstring 第 1/2 步的顺序理由）
        allowed, why = self._buy_allowed(code, name)
        if not allowed:
            return False, why
        if timing_pause:
            return False, "大盘弱势，暂停买入"
        if code in positions:
            return False, "已持仓"
        max_positions = self.settings.paper_max_positions
        if not ignore_slots and len(positions) >= max_positions:
            return False, f"持仓已满({max_positions}只)"
        return True, ""

    def entry_price_ok(
        self, rec, price: float | None, signal_price: float | None,
        armed: bool | None = None,
    ) -> tuple[bool, str]:
        """五段式⑤ 切入点判定（取价后）：价格有效 + 不追高 + 回踩要求。

        - 不追高：``price > 信号价 × (1 + paper_entry_max_chase)`` 放弃；
        - 回踩：``paper_entry_pullback > 0`` 时，要求价格**曾**触及 ``信号价×(1-pullback)``
          （``armed=True`` 由 tick 累积最低价置位）。未触及时返回「等待回踩」而不是直接放弃，
          否则价格反弹一次这个机会就永久消失；
        - 信号价缺失（老信号 / 手动信号）时只做价格有效性检查，不做切入点限制。
        """
        if price is None or price <= 0:
            return False, "取价失败"
        chase = float(getattr(self.settings, "paper_entry_max_chase", 0.0) or 0.0)
        pullback = float(getattr(self.settings, "paper_entry_pullback", 0.0) or 0.0)
        if armed is None:
            armed = bool(getattr(rec, "entry_armed", False))
        if signal_price and signal_price > 0:
            if pullback > 0 and not armed:
                if price > signal_price * (1 - pullback):
                    return False, f"等待回踩（需低于信号价 {pullback:.1%}）"
            if chase > 0 and price > signal_price * (1 + chase):
                return False, f"追高（高于信号价 {chase:.1%}）"
        return True, ""

    def entry_check(
        self,
        rec,
        price: float | None = None,
        signal_price: float | None = None,
        *,
        positions: dict | None = None,
        timing_pause: bool = False,
    ) -> tuple[bool, str]:
        """五段式④⑤ 统一判定入口：前置闸 + 切入点（供 API/测试/前端解释原因）。

        ``timing_pause`` 传的是「全暂停」档（大盘弱势且已达暂停阈值），不是
        「大盘弱势」本身；降仓档不应传 True，否则会和实盘路径口径不一致。
        """
        if positions is None:
            positions = {p["code"]: p for p in db.list_paper_positions()}
        ok, why = self.entry_gate(rec, positions=positions, timing_pause=timing_pause)
        if not ok or price is None:
            return ok, why
        return self.entry_price_ok(rec, price, signal_price)

    def _base_stop_ratio(self) -> float:
        """固定止损比例：优先用 ``paper_stop_loss``（回测落盘可改），缺省回调常量。"""
        return float(getattr(self.settings, "paper_stop_loss", STOP_LOSS_RATIO) or STOP_LOSS_RATIO)

    def _take_profit_ratio(self) -> float:
        """固定止盈比例：优先用 ``paper_take_profit``（回测落盘可改），缺省回调常量。"""
        return float(getattr(self.settings, "paper_take_profit", TAKE_PROFIT_RATIO) or TAKE_PROFIT_RATIO)

    def _stop_distance(self, code: str) -> float:
        """止损距离：固定止损（``paper_stop_loss``），开启 ATR 止损时取 max(固定, N×ATR%)，上限可配。

        高波动票的日振幅常达 5~10%，固定 8% ≈ 1×ATR，止损必然被正常波动打掉
        （实盘 2 天 5 笔 −8% 止损全是「刚碰线就砍」）。ATR 版本只放宽不收紧，
        与回测里 ``e30_a15`` 的机制一致。
        """
        base = self._base_stop_ratio()
        mult = float(getattr(self.settings, "paper_atr_stop_mult", 0.0) or 0.0)
        if mult <= 0:
            return base
        try:
            from app.data.cache import kline_cache
            from app.indicators.technical import atr as atr_indicator

            # 只用本地缓存：交易路径里不能因为某只票没缓存就去联网拉 K 线
            # （会把 tick 拖成秒级甚至超时）；没缓存就退回固定止损。
            df = kline_cache.get(code)
            if df is None or df.empty or len(df) < 20:
                return base
            # 注意：atr() 返回的是**整张表**（新增 atr 列），不是 Series
            frame = atr_indicator(df)
            if "atr" not in getattr(frame, "columns", []):
                return base
            atr_val = float(frame["atr"].iloc[-1])
            close = float(frame["close"].iloc[-1])
            if not (atr_val == atr_val) or close <= 0:
                return base
            dist = mult * atr_val / close
        except Exception as exc:  # noqa: BLE001
            logger.debug("ATR 止损计算失败 %s: %s", code, exc)
            return base
        return max(base, min(dist, float(getattr(self.settings, "paper_atr_stop_max", 0.2))))

    async def _index_is_weak(self, today: str) -> bool:
        """大盘择时：上证指数前一交易日收盘 < MA(N) 即判弱势（与回测 regime 同口径，无未来函数）。

        判定只用到**前一交易日**收盘与截至前日的均线，同一交易日内结果恒定，
        因此按自然日缓存：tick 每 5 秒一次，没必要每次都读 8MB 的指数 parquet。

        指数文件 ``idx_sh000001.parquet`` 缺失时退化为「不暂停」（避免数据源抖动直接停交易）。
        """
        n = int(getattr(self.settings, "paper_regime_ma", 0) or 0)
        if n <= 0:
            return False
        if self._weak_cache[0] == today:
            return self._weak_cache[1]
        weak = await self._index_is_weak_uncached(today, n)
        self._weak_cache = (today, weak)
        return weak

    async def _index_is_weak_uncached(self, today: str, n: int) -> bool:
        """``_index_is_weak`` 的实际计算（带 IO，按日缓存后一天只跑一次）。"""
        return bool((await self._index_regime_detail(today, n))["weak"])

    async def _index_regime_detail(self, today: str, n: int) -> dict:
        """大盘择时的数值明细：前一交易日收盘、MA(N)、是否弱势。

        判定口径与原先完全一致（都用**前一交易日**收盘与截至前日的均线，无未来函数）。
        单独拆出来是因为收盘对账需要的是「为什么弱」——只有 ``is_weak=True``
        无法复核判定是否正确，而带上指数点位与均线就能一眼看出距离。

        返回的 ``reason`` 非空表示本次判定退化（数据缺失/均线不足），此时 ``weak=False``，
        与 :meth:`_index_is_weak` 的既有行为保持一致：数据源抖动不停交易。
        """
        out: dict = {"ma": n, "prev_close": None, "prev_ma": None,
                     "weak": False, "reason": ""}
        try:
            idx_path = self.settings.kline_dir / "idx_sh000001.parquet"
            if not idx_path.exists():
                out["reason"] = "指数文件缺失"
                return out
            df = await asyncio.to_thread(pd.read_parquet, idx_path)
            if df is None or df.empty or "close" not in df.columns or len(df) < n + 1:
                out["reason"] = "指数数据不足"
                return out
            close = df["close"].astype(float)
            ma = close.rolling(n).mean().shift(1)  # 前一交易日，避免未来函数
            last = df[df["date"] <= today] if "date" in df.columns else df
            i = (len(last) - 1) if not last.empty else (len(df) - 1)
            if i <= 0:
                out["reason"] = "缺少前一交易日"
                return out
            prev_close = float(close.iloc[i - 1])
            prev_ma = float(ma.iloc[i - 1])
            if prev_ma != prev_ma or prev_ma <= 0:
                out["reason"] = "均线无效"
                return out
            out.update({
                "prev_close": round(prev_close, 2),
                "prev_ma": round(prev_ma, 2),
                "weak": prev_close < prev_ma,
            })
            return out
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"判定失败: {exc}"
            logger.debug("大盘择时判断失败: %s", exc)
            return out

    async def _dynamic_regime(self, today: str) -> dict:
        """动态机制层：读指数 parquet → detect_regime → 按日缓存。

        失败时退化为 normal / 不降仓（与关闭开关等价，零风险）。
        数据口径与 :meth:`_index_regime_detail` 一致：只用截至前一交易日的数据，无未来函数。
        """
        if self._regime_dyn_cache[0] == today:
            return self._regime_dyn_cache[1]
        default_cap = float(getattr(self.settings, "paper_max_total_exposure", 0.8))
        out: dict = {"state": "normal", "exposure_cap": default_cap,
                     "pause_all": False, "reason": "未启用", "metrics": {}}
        try:
            idx_path = self.settings.kline_dir / "idx_sh000001.parquet"
            if not idx_path.exists():
                out["reason"] = "指数文件缺失"
                self._regime_dyn_cache = (today, out)
                return out
            df = await asyncio.to_thread(pd.read_parquet, idx_path)
            if df is None or df.empty or "close" not in df.columns:
                out["reason"] = "指数数据不足"
                self._regime_dyn_cache = (today, out)
                return out
            close = df["close"].astype(float)
            if "date" in df.columns:
                last = df[df["date"] <= today]
                i = (len(last) - 1) if not last.empty else (len(df) - 1)
            else:
                i = len(df) - 1
            if i <= 1:
                out["reason"] = "缺少前一交易日"
                self._regime_dyn_cache = (today, out)
                return out
            # 截至前一交易日（与弱市闸门同口径，排除最新一根可能的当日不完整量价）
            series = close.iloc[:i]
            s = getattr(self.settings, "paper_regime_ma", 60) or 60
            calm_cap = float(getattr(self.settings, "paper_regime_cap_calm", 0.0) or default_cap)
            cfg = dict(
                ma_n=int(s),
                vol_win=20, vol_base=60, dd_win=120,
                crisis_vol=2.0, crisis_dd=0.12, shock_vol=1.4, shock_dd=0.06,
                cap_calm=calm_cap,
                cap_weak=float(getattr(self.settings, "paper_regime_cap_weak", 0.30)),
                cap_shock=float(getattr(self.settings, "paper_regime_cap_shock", 0.40)),
                cap_crisis=float(getattr(self.settings, "paper_regime_cap_crisis", 0.15)),
            )
            res = detect_regime(series, **cfg)
            # 政策托底强度叠加（paper_policy_floor_enabled 时）：把"看不见的底"变可见，
            # 让 weak/shock 态分治——托底型阴跌可缩仓收割，自由落体型升级危机防御。
            # 缺失直接信号（ETF/期指/新闻）时，detect_policy_floor 自动降级为指数底部代理。
            if bool(getattr(self.settings, "paper_policy_floor_enabled", False)):
                floor = detect_policy_floor(series)
                res["floor"] = floor
                base = res["state"]
                if base in ("weak", "shock"):
                    score = float(floor.get("score", 0.0))
                    if score >= float(getattr(self.settings, "paper_policy_floor_min_score", 0.5)):
                        harvest_cap = float(getattr(self.settings, "paper_policy_floor_harvest_cap", 0.30))
                        res.update(state="managed_downtrend", exposure_cap=harvest_cap,
                                   pause_all=False, harvest_mode=True,
                                   reason=f"托底型阴跌(强度{score:.2f})，缩仓收割反转")
                    elif score <= float(getattr(self.settings, "paper_policy_floor_low_escalate", 0.2)):
                        # 自由落体型：托底证据弱 + 高波动 → 升级为危机级防御（必须撤退+熔断）
                        if base == "shock" or float(floor.get("metrics", {}).get("max_drawdown", 0.0)) >= 0.12:
                            res.update(state="crisis",
                                       exposure_cap=float(getattr(self.settings, "paper_regime_cap_crisis", 0.15)),
                                       pause_all=True, reason="自由落体(无托底证据)，升级危机防御")
            self._regime_dyn_cache = (today, res)
            return res
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"机制检测失败: {exc}"
            logger.debug("动态机制检测失败: %s", exc)
            self._regime_dyn_cache = (today, out)
            return out

    async def _circuit_breaker(self, today: str, total_asset: float) -> dict:
        """硬性回撤熔断：组合回撤触及阈值即锁仓/压仓，凌驾于任何信号与机制层之上。

        设计（BACKTEST_LOG #85）：
        - 最后一道防线，优先级高于动态机制层与所有买入信号；
        - 滞回状态机（:func:`circuit_breaker_state`）：回撤 ≥ trigger 触发，回落到 ≤ release
          才解除，避免临界值附近反复抖；
        - 峰值与触发状态落库（``paper_cb_state``），**跨重启保留**——重启不会因"忘了刚才跌"
          就立刻满仓接飞刀；
        - 关闭开关或任何异常 → 返回未触发（零风险）。
        """
        out = {"enabled": False, "tripped": False, "drawdown": 0.0,
               "peak": total_asset, "target_exposure": 0.0, "reason": "未启用"}
        if not bool(getattr(self.settings, "paper_circuit_breaker_enabled", False)):
            return out
        try:
            trigger = float(getattr(self.settings, "paper_circuit_breaker_dd", 0.08))
            release = float(getattr(self.settings, "paper_circuit_breaker_release", 0.04))
            target = float(getattr(self.settings, "paper_circuit_breaker_target", 0.0))
            # 滞回要求 release < trigger。设置页是逐字段校验，跨字段填反了（例如
            # trigger=0.04 / release=0.08）会让状态机在触发线附近每个 tick 反复翻转——
            # 买入被反复暂停又放开。这里静默收敛到 trigger*0.9，而不是信任用户输入。
            if release >= trigger:
                release = round(max(0.0, trigger * 0.9), 4)
                logger.debug(
                    "熔断 release(%.3f) 不小于 trigger(%.3f)，已按 trigger*0.9 收敛为 %.3f",
                    float(getattr(self.settings, "paper_circuit_breaker_release", 0.04)),
                    trigger, release,
                )
            # 峰值：优先内存，首次/重启后从库恢复（json 已在文件顶部导入）
            if self._cb_peak is None or self._cb_peak <= 0:
                try:
                    raw = db.get_state("paper_cb_state")
                    if raw:
                        st = json.loads(raw)
                        self._cb_peak = float(st.get("peak", total_asset) or total_asset)
                        self._cb_tripped = bool(st.get("tripped", False))
                except Exception:  # noqa: BLE001
                    self._cb_peak = total_asset
            if self._cb_peak is None or self._cb_peak <= 0:
                self._cb_peak = total_asset
            self._cb_peak = max(self._cb_peak, total_asset)
            dd = (1.0 - total_asset / self._cb_peak) if self._cb_peak > 0 else 0.0
            # 滞回状态机
            self._cb_tripped = circuit_breaker_state(
                dd, self._cb_tripped, trigger=trigger, release=release)
            # 落库（重启后仍能记得"刚才跌过"）。**只在签名变化时写**：tick 每 5 秒一次，
            # 峰值不变时每次写的内容完全一样，白让 sas.db 的 WAL 涨。
            sig = (round(self._cb_peak, 2), self._cb_tripped)
            if sig != self._cb_persisted:
                self._cb_persisted = sig
                try:
                    db.set_state("paper_cb_state", json.dumps(
                        {"peak": sig[0], "tripped": sig[1]}, ensure_ascii=False))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("熔断状态落库失败: %s", exc)
            out.update(enabled=True, tripped=self._cb_tripped, drawdown=round(dd, 4),
                       peak=round(self._cb_peak, 2), target_exposure=target,
                       reason=("已触发熔断(锁仓/压仓)" if self._cb_tripped
                               else f"回撤{dd:.1%}未触发"))
            return out
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"熔断判定失败: {exc}"
            return out

    def regime_state(self) -> dict:
        """大盘择时闸门的当日状态（供 ``GET /api/paper/live`` 展示）。

        ``weak`` 为 ``None`` 表示今天还没跑过判定（非交易时段/尚未 tick）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        n = int(getattr(self.settings, "paper_regime_ma", 0) or 0)
        cached = self._weak_cache[0] == today
        dyn_on = bool(getattr(self.settings, "paper_dynamic_exposure", False))
        return {
            "enabled": bool(getattr(self.settings, "paper_regime_weak_pause", False)) and n > 0,
            "ma": n,
            # 动态机制层开启时静态 MA 闸门**完全不参与交易决策**（execute_recommendations 里是
            # if/else），此时 `weak` 没有意义。返回 None 而不是残留值，免得前端同时显示
            # 「弱市压到 weak_exposure(0.3)」和「机制层压到 regime_cap(0.15)」两个互相矛盾的数字。
            "weak": (None if dyn_on else (bool(self._weak_cache[1]) if cached else None)),
            "paused_count": (self._regime_paused if self._regime_day == today else 0),
            # 弱市保留仓位比例：0=全暂停买入；>0=只压总仓位上限（页面按此分流文案）
            "weak_exposure": float(getattr(self.settings, "paper_weak_exposure", 0.0) or 0.0),
            "day": today,
            # 动态机制层（paper_dynamic_exposure）：暴露当日状态/上限/指标，便于前端展示
            "dynamic": bool(getattr(self.settings, "paper_dynamic_exposure", False)),
            "regime_state": (self._regime_dyn_cache[1].get("state")
                             if self._regime_dyn_cache[0] == today else None),
            "regime_cap": (self._regime_dyn_cache[1].get("exposure_cap")
                           if self._regime_dyn_cache[0] == today else None),
            "regime_metrics": (self._regime_dyn_cache[1].get("metrics")
                               if self._regime_dyn_cache[0] == today else None),
            # 硬性回撤熔断 + 政策托底强度：供 /api/paper/live 实时可见
            "circuit_breaker": {
                "enabled": bool(getattr(self.settings, "paper_circuit_breaker_enabled", False)),
                "tripped": bool(self._cb_tripped),
                "peak": (round(self._cb_peak, 2) if self._cb_peak else None),
            },
            "floor": (self._regime_dyn_cache[1].get("floor")
                      if (self._regime_dyn_cache[0] == today
                          and bool(getattr(self.settings, "paper_policy_floor_enabled", False)))
                      else None),
        }

    # ------------------------------------------------------------------
    # 费用计算
    # ------------------------------------------------------------------
    @staticmethod
    def calc_fee(side: str, amount: float) -> float:
        """按 A 股标准费率计算单笔费用。

        费率取 ``Settings.trade_fee_*``（与回测 ``pipeline.calc_fee`` 同一份）：
        佣金万2.5（最低5元，双边）+ 印花税千1（仅卖出）+ 过户费万0.1（双边）。
        改费率只改设置页，两边同时生效，回测 PnL 才和实盘可比。
        """
        s = get_settings()   # staticmethod：不依赖实例，取单例保证与回测同一份费率
        commission = max(amount * s.trade_fee_commission_rate, s.trade_fee_commission_min)
        stamp = amount * s.trade_fee_stamp_rate if side == "sell" else 0.0
        transfer = amount * s.trade_fee_transfer_rate
        return round(commission + stamp + transfer, 2)

    # ------------------------------------------------------------------
    # 轮动替换（选弱 + 选强）
    # ------------------------------------------------------------------
    def _weakest_rotatable(
        self, positions: dict, today: str, keep: set[str] | None = None
    ) -> tuple[str | None, dict | None, float]:
        """选出可以被自动轮动卖出的最弱持仓（按建仓评分）。

        排除三类持仓：

        - **手动建仓**（``strategy`` 含「手动」）：不打分、代表用户意图，程序不该卖掉；
        - **当日买入**（``buy_date >= today``）：T+1 卖不掉；
        - **本轮仍被建议买入的**（``keep``：买入候选 ∪ 候补候选）：同一只票在两条口径下
          同时是「最弱」和「最强」是自相矛盾。2026-11-03 实盘：301314 科瑞思**建仓分 0**
          （0 = 未知，不是真的差）、**当前分 100**，于是被判为最弱卖掉 400 股，同一 tick 又
          按 100 分买回 300 股 —— 白付 24.12+5.14 元费用，持仓还少了一手。
          结论：**建仓分缺失不能当建仓分最差用**；只要本轮还在建议买入，就不许当卖出对象。

        T+1 的判断放在「选弱」而不是「成交前」：原来的实现先选最弱的、再检查它是否
        当日买入，于是今天恰好买了最弱那只时整个轮动被挡掉，哪怕另一只持仓明明可换。
        """
        weak_code: str | None = None
        weak_pos: dict | None = None
        weak_score = 0.0
        skip = keep or set()
        for code, pos in positions.items():
            if "手动" in str(pos.get("strategy") or ""):
                continue
            if str(pos.get("buy_date") or "") >= today:
                continue
            if code in skip:
                continue
            score = float(pos.get("score") or 0.0)
            if weak_pos is None or score < weak_score:
                weak_code, weak_pos, weak_score = code, pos, score
        return weak_code, weak_pos, weak_score

    async def _pick_rotation_target(
        self,
        candidates: list,
        *,
        positions: dict,
        prices: dict[str, float] | None,
        budget: float,
        weak_score: float,
        exclude: set[str] | None = None,
    ) -> tuple[object, float, float] | None:
        """挑出「真能买进」的替换标的，挑不到就返回 ``None``（此时不卖）。

        门槛按买入阶段同一套口径（否则会出现「卖了却买不进」的空转卖出）：

        1. 未持仓、代码去重；
        2. 评分 >= ``paper_rotation_min_score`` 且 >= ``paper_half_score``
           （低于半额分的票买入阶段本来就会被丢掉）；
        3. 建仓分差 >= ``paper_rotation_gap``；
        4. 交易池闸门（板块/北交所/大市值/名称/成交额池），``ignore_slots=True``
           ——此刻必然满仓，名额检查只会一律拒掉；
        5. 切入点闸门（不追高 / 待回踩），用实时价；
        6. 卖出所得 ``budget`` 够买一手。
        """
        min_score = float(self.settings.paper_rotation_min_score)
        gap = float(self.settings.paper_rotation_gap)
        half_score = float(self.settings.paper_half_score)
        pool: list[tuple[float, object]] = []
        seen: set[str] = set()
        skip = set(exclude or ())
        for rec in candidates:
            code = str(getattr(rec, "code", "") or "")
            # exclude：本次已轮动卖出的票不能再被选回来（否则「换出去又买回来」白付两次摩擦）
            if not code or code in positions or code in seen or code in skip:
                continue
            seen.add(code)
            score = _rec_score(rec)
            if score < min_score or score < half_score or score - weak_score < gap:
                continue
            ok, _why = self.entry_gate(
                rec, positions=positions, timing_pause=False, ignore_slots=True)
            if not ok:
                continue
            pool.append((score, rec))
        for score, rec in sorted(pool, key=lambda item: -item[0]):
            price = await self._latest_price(rec.code, prices)
            ok, _why = self.entry_price_ok(rec, price, _rec_price(rec))
            if not ok:
                continue
            if budget < float(price) * LOT_SIZE:
                continue
            return rec, score, float(price)
        return None

    # ------------------------------------------------------------------
    # 信号执行（核心）
    # ------------------------------------------------------------------
    async def execute_recommendations(
        self,
        recommendations: list,
        prices: dict[str, float] | None = None,
        rotation_candidates: list | None = None,
    ) -> list[TradeResult]:
        """执行一批买卖建议（智能建议引擎的输出）。

        Args:
            recommendations: Recommendation 对象列表（action/price 等字段）
            prices: 可选的逐股最新价（实时撮合传入），缺省时内部取最新价
            rotation_candidates: 额外的**轮动候选**（只在轮动分支里使用）。
                满仓时 ``live.tick`` 不会把候补队列塞进 ``recommendations``
                （只在有剩余名额时补位），于是被满仓挡下的高分票永远进不了轮动视野。
                由调用方把它们单独传进来，轮动才能用上「今天没买到、分还很高」的票。

        Returns:
            执行结果列表（含 skip 的原因）
        """
        self.ensure_account()
        if not self.settings.paper_auto_trade:
            logger.info("模拟交易开关关闭（PAPER_AUTO_TRADE=false），跳过执行")
            return []

        account = db.get_paper_account()
        positions = {p["code"]: p for p in db.list_paper_positions()}
        cash = float(account["cash"])
        total_asset = await self.total_asset(prices)
        today = datetime.now().strftime("%Y-%m-%d")

        results: list[TradeResult] = []
        buy_candidates = [r for r in recommendations if r.action == "buy"]
        sell_candidates = [r for r in recommendations if r.action == "sell"]

        # ---- 卖出优先（释放资金与仓位名额） ----
        for rec in sell_candidates:
            pos = positions.get(rec.code)
            if pos is None:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, "无持仓可卖"))
                continue
            price = await self._latest_price(rec.code, prices)
            if price is None:
                results.append(TradeResult(
                    rec.code, pos["name"], "skip", 0,
                    0.0, 0.0, 0.0, "取价失败"))
                continue
            # 严格 T+1：只卖可卖批次（buy_date<today），今仓留到次日
            sold = db.reduce_paper_sellable(rec.code, int(pos["shares"]), today)
            if sold <= 0:
                results.append(TradeResult(
                    rec.code, pos["name"], "skip", 0,
                    0.0, 0.0, 0.0, "T+1 当日买入不可卖"))
                continue
            amount = price * sold
            fee = self.calc_fee("sell", amount)
            cash += amount - fee
            db.insert_paper_trade(
                rec.code, pos["name"], "sell", price, sold, amount, fee,
                getattr(rec, "reason", "卖出信号"), today,
            )
            results.append(TradeResult(
                rec.code, pos["name"], "sell", sold, price, amount, fee,
                getattr(rec, "reason", "卖出信号")))
            # 同步内存里的持仓表：`positions` 决定本轮名额（`len(positions) >= max_positions`）
            # 与轮动触发，卖了不减会让「卖了就买不进新票」——本轮全部报「持仓已满」，
            # 要等下一轮 tick 才恢复。轮动分支早就 pop 了，这里之前漏了。
            remaining = int(pos["shares"]) - sold
            if remaining <= 0:
                positions.pop(rec.code, None)
            else:
                positions[rec.code] = {**pos, "shares": remaining}

        # ---- 大盘择时买入闸门：弱市暂停自动买入与轮动（仅保留卖出） ----
        # 动态机制层（paper_dynamic_exposure）开启时，由 vol/回撤/趋势统一驱动降仓与暂停，
        # 替代下面的静态 MA 闸门；关闭时完全沿用既有逻辑（零风险）。见 BACKTEST_LOG #84。
        dyn_enabled = bool(getattr(self.settings, "paper_dynamic_exposure", False))
        dyn: dict | None = None
        if dyn_enabled:
            dyn = await self._dynamic_regime(today)
            market_weak = dyn["state"] in ("weak", "shock", "crisis")
            weak_exposure = float(dyn["exposure_cap"])
            weak_pause_all = bool(dyn["pause_all"])
        else:
            market_weak = (
                bool(self.settings.paper_regime_weak_pause)
                and int(self.settings.paper_regime_ma) > 0
                and await self._index_is_weak(today)
            )
            #: 弱市保留仓位比例（0=全暂停；>0=不暂停买入，只把总仓位上限压到该比例）。
            #: 回测 #59/#60：0.3~0.5 在急跌月小亏、换震荡/反弹时的收益，代价是涨市初期踏空。
            weak_exposure = float(getattr(self.settings, "paper_weak_exposure", 0.0) or 0.0)
            weak_pause_all = market_weak and weak_exposure <= 0
        # ---- 硬性回撤熔断：最高优先级，凌驾于机制层与任何买入信号 ----
        # 开启时计算当前组合回撤；一旦触发，强制全暂停买入 + 总仓位压到 target。
        cb_tripped = False
        cb_target = 0.0
        cb_dd = 0.0
        if bool(getattr(self.settings, "paper_circuit_breaker_enabled", False)):
            cb = await self._circuit_breaker(today, total_asset)
            cb_tripped = bool(cb["tripped"])
            cb_target = float(cb["target_exposure"])
            cb_dd = float(cb.get("drawdown", 0.0))
            if cb_tripped:
                market_weak = True
                weak_pause_all = True
                weak_exposure = cb_target
            # 触发/解除切换时打一条日志（避免每 tick 刷屏，与弱市计数同思路）
            if cb_tripped != self._cb_prev_tripped:
                self._cb_prev_tripped = cb_tripped
                if cb_tripped:
                    logger.warning(
                        "硬性回撤熔断已触发：组合回撤 %.1f%%，总仓位压到 %.0f%%，暂停自动买入",
                        cb_dd * 100, cb_target * 100,
                    )
                else:
                    logger.info("硬性回撤熔断解除：组合回撤回落至 %.1f%%，恢复常规风控", cb_dd * 100)
        if market_weak:
            if self._regime_day != today:
                self._regime_day = today
                self._regime_paused = 0
            self._regime_paused += 1
            # 每 5 秒一条会把日志刷满（2026-10-30 全天 1999 条），只在首次与每 60 次留痕；
            # 该计数同时经 /api/paper/live 的 regime 字段对外可见。
            if self._regime_paused == 1 or self._regime_paused % 60 == 0:
                # 每 60 次（约 5 分钟）把当日计数落库：内存计数重启即丢，而
                # 「本周为什么没交易」必须能跨重启回答（scripts/weekly_review.py 读这个键）。
                try:
                    db.set_state(f"paper_regime_paused:{today}", str(self._regime_paused))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("弱市计数落库失败: %s", exc)
                if weak_pause_all:
                    logger.info(
                        "大盘弱势（指数在 MA 下），暂停自动买入/轮动，仅执行卖出（今日第 %d 次拦截）",
                        self._regime_paused,
                    )
                else:
                    logger.info(
                        "大盘弱势（指数在 MA 下），总仓位压到 %.0f%%（今日第 %d 次触发）",
                        weak_exposure * 100, self._regime_paused,
                    )

        # ---- 开盘保护：跳空护栏 + 开盘延迟（仅拦买入，卖出照常） ----
        # 跳空护栏复用做T 的 crash guard 阈值（paper_t_crash_guard）：大盘当日开盘跌幅超阈值，
        # 暂停自动买入与轮动换入（开盘猛跌/外围利空时不接刀）。
        # 开盘延迟：仅在大盘向下跳空时，开盘后 N 分钟内禁止自动买入，让跳空消化；
        # 利好日（大盘上涨）不延迟，避免踏空误伤（保护只针对下行风险）。
        idx_open_drop = self._idx_open_drop()
        crash_guard_th = float(getattr(self.settings, "paper_t_crash_guard", 0) or 0)
        crash_pause_buy = (
            bool(crash_guard_th)
            and idx_open_drop is not None
            and idx_open_drop >= crash_guard_th
        )
        open_delayed = self._open_delayed(datetime.now())
        open_cooldown = self._open_cooldown(datetime.now())
        buy_paused = weak_pause_all or crash_pause_buy or open_delayed or open_cooldown
        if crash_pause_buy and not weak_pause_all:
            logger.warning(
                "开盘跳空护栏：大盘开盘跌幅 %.1f%% ≥ %.1f%%，暂停自动买入/轮动（仅卖出）",
                idx_open_drop * 100, crash_guard_th * 100,
            )
        if open_delayed:
            logger.info(
                "开盘延迟：开盘后 %d 分钟内暂停自动买入/轮动（仅卖出）",
                int(getattr(self.settings, "paper_buy_open_delay_minutes", 0) or 0),
            )
        if open_cooldown:
            logger.info(
                "开盘冷静期：开盘后 %d 分钟内暂停自动买入/轮动（仅卖出）",
                int(getattr(self.settings, "paper_open_cooldown_minutes", 0) or 0),
            )

        # ---- 总仓位上限（轮动与买入共用，故提前算） ----
        # 盘中不设「收盘目标」硬上限：`paper_max_total_exposure`(80%) 是**收盘目标**，
        # 由 eod_trim 在收盘前压回；盘中允许满仓，只受现金（等价 100%）与单票上限约束。
        # 熔断 / 动态机制 / 弱市三档仍按各自上限**硬压**（那些是风控，不是目标）。
        # 轮动必须在卖出**之前**知道这个上限：弱市压到 30% 时，卖掉一只大票的钱根本投不
        # 回去，「换股」会退化成「降仓」，却挂着「轮动替换」的名义（2026-11-03 实例：
        # 卖 3 只共 94,687 元，只投回 28,182 元，仓位 96% → 28.6%）。
        effective_exposure = 1.0
        if cb_tripped:
            # 熔断最高优先级：总仓位压到 target（只降不升），凌驾于机制层与弱市档
            effective_exposure = min(effective_exposure, cb_target)
        elif dyn_enabled and dyn is not None:
            # 动态机制降暴露：把总仓位上限压到机制层给出的上限（只降不升）
            effective_exposure = min(effective_exposure, float(dyn["exposure_cap"]))
        elif market_weak and weak_exposure > 0:
            # 弱市降仓：总仓位上限压到 paper_weak_exposure，与回测 day_exposure 同口径
            effective_exposure = min(effective_exposure, weak_exposure)
        exposure_cap = total_asset * effective_exposure

        # ---- 轮动替换：不用满仓，有更明显的强票就换；一次可换多只 ----
        # 语义（2026-11-02 使用者口径）：`paper_max_positions` 是**上限**不是门槛，
        #   「明显更好的票」就该把差的换掉，不必等止损/止盈；持仓可以少于上限。
        # 触发只需：开关开 + 非全暂停 + 有未持仓候选（评分 >= rotation_min_score 且
        #   >= half_score、比「当前最弱可卖持仓」的建仓分高出 rotation_gap、过交易池与
        #   切入点闸门）+ 该持仓卖得掉（非手动、非今仓 T+1）+ 卖出所得够买该票一手。
        #   每换一只消耗一次配额，上限 paper_rotation_max_per_day 只（默认 10，等于不限）。
        # 为什么要求「真能买进」：早期版本只看评分就卖，换入票随后被交易池（大市值/
        #   北交所/不在成交额池）、切入点（追高/待回踩）或评分分档拒掉 → 白付一次卖出
        #   摩擦、名额空着等下一轮，等于凭空降仓。见 BACKTEST_LOG 记录 #91。
        # 为什么循环：一次 API 调用里可以连换多只（含全换），不必等下一个 tick；
        #   每轮重新取「当前最弱」，被换掉的票即退出候选池（sold_codes 防回转买入）。
        sold_codes: set[str] = set()
        if self.settings.paper_rotation_enabled and not buy_paused:
            used = db.count_paper_trades("轮动替换", today, side="sell")
            max_rot = int(self.settings.paper_rotation_max_per_day)
            rot_pool = [*buy_candidates, *(rotation_candidates or [])]
            # 本轮仍被建议买入的代码：不许当轮动卖出对象（见 _weakest_rotatable 第 3 条）
            keep = {str(getattr(r, "code", "") or "") for r in rot_pool}
            #: 已被本轮换入占用的目标：一个换入标的只能承接**一次**卖出。
            #: 少了这个，`while` 会反复挑同一只「未持仓」的票当目标（它要到买入阶段才建仓），
            #: 于是「卖出 3 只、换入同一只」——2026-11-03 日志：三笔轮动的换入全是 301331。
            consumed: set[str] = set()
            single_max = float(
                getattr(self.settings, "paper_single_position_max", SINGLE_POSITION_MAX_RATIO)
                or SINGLE_POSITION_MAX_RATIO
            )
            while used < max_rot:
                weak_code, weak_pos, weak_score = self._weakest_rotatable(
                    positions, today, keep=keep)
                if weak_code is None or weak_pos is None:
                    break
                weak_price = await self._latest_price(weak_code, prices)
                if not weak_price or weak_price <= 0:
                    break
                # 换入预算 = 卖出所得扣费后的金额。不够买一手就不换，
                # 否则「卖得掉、买不进」，仓位被凭空降下来（与弱市降仓混在一起无法归因）。
                weak_mv = weak_price * int(weak_pos["shares"])
                proceeds = weak_mv - self.calc_fee("sell", weak_mv)
                target = await self._pick_rotation_target(
                    rot_pool,
                    positions=positions,
                    prices=prices,
                    budget=proceeds,
                    weak_score=weak_score,
                    exclude=sold_codes | consumed,
                )
                if target is None:
                    break
                target_rec, target_score, target_price = target
                # 「换股」必须真能把钱投回去：换入票能吸收的金额受三重约束——
                # 卖出所得、单票上限（总资产×20%）、卖出后的总仓位剩余额度。
                # 弱市压到 30% 时，卖一只大票的钱大部分投不回去，那不是换股而是降仓；
                # 降仓是风控的职责（exposure 上限在买入端自然实现），不该借轮动之名完成，
                # 否则归因完全错位（2026-11-03：卖 3 只 94,687 元只投回 28,182 元）。
                available_after = max(
                    0.0, exposure_cap - max(0.0, total_asset - (cash + proceeds)))
                deployable = min(proceeds, total_asset * single_max, available_after)
                if deployable < proceeds or deployable < target_price * LOT_SIZE:
                    logger.info(
                        "不轮动 %s（市值 %.0f 元）：换入 %s 最多只投回 %.0f 元"
                        "（单票上限 %.0f / 仓位剩余 %.0f），卖了也买不回来",
                        weak_code, weak_mv, target_rec.code, deployable,
                        total_asset * single_max, available_after,
                    )
                    break
                # 严格 T+1：只卖可卖批次，今仓留到次日
                sold = db.reduce_paper_sellable(weak_code, int(weak_pos["shares"]), today)
                if sold <= 0:
                    break
                amount = weak_price * sold
                fee = self.calc_fee("sell", amount)
                cash += amount - fee
                reason = f"轮动替换(建仓{weak_score:.0f}分→新票{target_score:.0f}分)"
                db.insert_paper_trade(
                    weak_code, weak_pos.get("name", ""), "sell", weak_price,
                    sold, amount, fee, reason, today,
                )
                positions.pop(weak_code, None)
                sold_codes.add(weak_code)
                consumed.add(target_rec.code)   # 该目标已用掉，下一轮必须换别的票
                used += 1
                results.append(TradeResult(
                    weak_code, weak_pos.get("name", ""), "sell", sold,
                    weak_price, amount, fee, reason,
                ))
                # 换入标的必须在同一次调用里真的买到：候补队列里的票不在本轮
                # buy_candidates 里，不补进去就只剩「卖出」这一半。
                if all(r.code != target_rec.code for r in buy_candidates):
                    buy_candidates.append(target_rec)
                logger.info(
                    "轮动替换：卖出 %s（建仓 %.0f 分），换入 %s（%.0f 分），今日第 %d 只",
                    weak_code, weak_score, target_rec.code, target_score, used,
                )

        # 轮动刚卖掉的票不进本轮买入候选：卖出与买入是两条独立分支，队列里留着它的旧
        # 买单就会「卖完立刻买回」。2026-11-03 实盘：301314 科瑞思 卖 400 又买回 300。
        # 卖出侧已有 keep 规则挡住主因，这里是同一 tick 内的兜底。
        if sold_codes:
            before = len(buy_candidates)
            buy_candidates = [
                r for r in buy_candidates
                if str(getattr(r, "code", "") or "") not in sold_codes
            ]
            if len(buy_candidates) != before:
                logger.info(
                    "轮动已卖出，本轮不再买回：%s", ", ".join(sorted(sold_codes)))

        # ---- 买入（第 3 次排序：评分加权仓位 + 评分分档 + 总仓位上限） ----
        # 规则：
        #   1) 评分 < half_score(25) 只记录不下单；half~full(25~40) 权重打 5 折；
        #   2) 高分先买，可投预算按「评分×档位系数」比例分配；
        #   3) 新建仓市值不超过总资产 × paper_max_total_exposure(80%)；
        #   4) 单票不超过总资产 20%，整手取整，费用从现金扣除。
        max_positions = self.settings.paper_max_positions
        full_score = self.settings.paper_full_score
        half_score = self.settings.paper_half_score
        # `exposure_cap` 在轮动之前就算好了（见上），这里按**卖出后的现金**重算剩余额度
        available_budget = max(0.0, exposure_cap - max(0.0, total_asset - cash))
        candidates: list[tuple[float, object, float]] = []
        for rec in buy_candidates:
            name = getattr(rec, "name", "") or rec.code
            # 开盘保护（跳空护栏/开盘延迟/弱市全暂停）：先按准确原因跳过买入，避免误归因为「弱势」
            if buy_paused:
                why = (
                    "开盘跳空护栏暂停买入" if crash_pause_buy
                    else "开盘延迟暂停买入" if open_delayed
                    else "开盘冷静期暂停买入" if open_cooldown
                    else "大盘弱势暂停买入"
                )
                results.append(TradeResult(
                    rec.code, name, "skip", 0, 0.0, 0.0, 0.0, why))
                continue
            # ④ 交易池前置闸：范围/大市值/名称/池子 → 择时 → 已持仓 → 名额
            #    只有「全暂停」档（buy_paused）才在这里拒掉买入；降仓档放行，
            #    由上面算出的 exposure_cap 控总量
            ok, why = self.entry_gate(rec, positions=positions, timing_pause=buy_paused)
            if not ok:
                results.append(TradeResult(
                    rec.code, name, "skip", 0, 0.0, 0.0, 0.0, why))
                continue
            price = await self._latest_price(rec.code, prices)
            # ⑤ 切入点：价格有效 / 不追高 / 回踩要求
            ok, why = self.entry_price_ok(rec, price, _rec_price(rec))
            if not ok:
                results.append(TradeResult(
                    rec.code, name, "skip", 0, 0.0, 0.0, 0.0, why))
                continue
            candidates.append((_rec_score(rec), rec, price))

        candidates.sort(key=lambda c: c[0], reverse=True)

        # 评分分档：低于半额分只记录；其余按「评分 × 档位系数」加权
        eligible: list[tuple[float, object, float, float]] = []
        for score, rec, price in candidates:
            if score < half_score:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0, price, 0.0, 0.0,
                    f"评分{score:.0f}<{half_score:.0f}，仅记录不下单"))
                continue
            factor = 1.0 if score >= full_score else 0.5
            eligible.append((score, rec, price, max(score, 1.0) * factor))

        # 候选多于剩余名额时，只让名额内的高分股参与分钱，
        # 否则预算会被买不到的候选摊薄（导致只建了两三成仓）
        slots = max(0, max_positions - len(positions))
        if len(eligible) > slots:
            eligible = eligible[:slots]

        weights = {rec.code: weight for _, rec, _, weight in eligible}
        # 单票上限优先用 paper_single_position_max（回测落盘可改），缺省回调常量
        single_max = float(
            getattr(self.settings, "paper_single_position_max", SINGLE_POSITION_MAX_RATIO)
            or SINGLE_POSITION_MAX_RATIO
        )

        for _score, rec, price, weight in eligible:
            weights.pop(rec.code, None)
            weight_total = weight + sum(weights.values())
            if len(positions) >= max_positions:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, f"持仓已满({max_positions}只)"))
                continue
            if available_budget < price * LOT_SIZE:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0, price, 0.0, 0.0,
                    f"可用资金不足一手({available_budget:.0f}元)"))
                continue
            # 目标金额 = 本轮可投预算 × 加权占比，再受单票上限与剩余现金约束
            share_of_pool = weight / weight_total if weight_total > 0 else 1.0
            budget = min(
                available_budget * share_of_pool,
                total_asset * single_max,
                cash,
                available_budget,
            )
            shares = int(budget / price / LOT_SIZE) * LOT_SIZE
            # 费用可能让「金额+费用」超出可用现金，逐手回退
            while shares >= LOT_SIZE and price * shares + self.calc_fee(
                "buy", price * shares
            ) > cash:
                shares -= LOT_SIZE
            if shares < LOT_SIZE:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    price, 0.0, 0.0, "资金不足一手"))
                continue
            amount = price * shares
            fee = self.calc_fee("buy", amount)
            cost_price = (amount + fee) / shares  # 含费成本价
            cash -= amount + fee
            available_budget = max(0.0, available_budget - amount)
            name = getattr(rec, "name", "") or rec.code
            # 止损线在建仓时一次定死（固定 8% 或 max(8%, 2×ATR)），
            # 避免高波动票被固定 8% 的噪声打掉、也便于复盘「当时定的止损是多少」
            stop_price = round(cost_price * (1 - self._stop_distance(rec.code)), 3)
            db.add_paper_lot(
                rec.code, name, shares, cost_price, today,
                getattr(rec, "reason", "买入信号"), score=_rec_score(rec),
                stop_price=stop_price, peak_price=cost_price, today=today,
            )
            db.insert_paper_trade(
                rec.code, name, "buy", price, shares, amount, fee,
                getattr(rec, "reason", "买入信号"), today,
            )
            positions[rec.code] = {"code": rec.code, "name": name}
            results.append(TradeResult(
                rec.code, name, "buy", shares, price, amount, fee,
                getattr(rec, "reason", "买入信号")))

        db.update_paper_cash(round(cash, 2))
        executed = sum(1 for r in results if r.side != "skip")
        logger.info("模拟交易执行完成：%d 笔成交，%d 笔跳过", executed,
                    len(results) - executed)
        return results

    # ------------------------------------------------------------------
    # 手动下单（模拟盘）
    # ------------------------------------------------------------------
    async def place_order(
        self,
        code: str,
        side: str,
        shares: int | None = None,
        amount: float | None = None,
        name: str = "",
        price: float | None = None,
        reason: str = "手动下单",
    ) -> TradeResult:
        """手动下单：按实时价立即成交（不受自动交易开关影响）。

        Args:
            side: ``buy`` / ``sell``
            shares: 指定股数（买入向下取整到手；卖出留空=全部）
            amount: 买入金额（元），优先于 shares
            price: 指定成交价，缺省取最新价
        """
        self.ensure_account()
        today = datetime.now().strftime("%Y-%m-%d")

        if not name:
            name = next(
                (w["name"] for w in db.list_watchlist() if w["code"] == code), code
            )

        if price is None or price <= 0:
            price = await self._latest_price(code)
        if price is None or price <= 0:
            return TradeResult(code, name, "skip", 0, 0.0, 0.0, 0.0, "取价失败")

        account = db.get_paper_account()
        cash = float(account["cash"])

        # ---- 卖出 ----（严格 T+1：只卖可卖批次，今仓留次日）
        if side == "sell":
            pos = db.get_paper_position(code)
            if pos is None:
                return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "无持仓可卖")
            held = int(pos["shares"])
            qty = held if (not shares or int(shares) >= held) else int(shares) // LOT_SIZE * LOT_SIZE
            if qty <= 0:
                return TradeResult(code, pos["name"], "skip", 0, price, 0.0, 0.0, "股数不足一手")
            sold = db.reduce_paper_sellable(code, qty, today)
            if sold <= 0:
                return TradeResult(
                    code, pos["name"], "skip", 0, price, 0.0, 0.0, "T+1 当日买入不可卖"
                )
            amt = price * sold
            fee = self.calc_fee("sell", amt)
            cash += amt - fee
            db.insert_paper_trade(
                code, pos["name"], "sell", price, sold, amt, fee, reason, today
            )
            db.update_paper_cash(round(cash, 2))
            logger.info("手动卖出 %s %d 股（可卖）@ %.2f", code, sold, price)
            return TradeResult(code, pos["name"], "sell", sold, price, amt, fee, reason)

        # ---- 买入 ----
        if db.get_paper_position(code) is not None:
            return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "已持仓，暂不支持加仓")
        if amount and amount > 0:
            qty = int(amount / price / LOT_SIZE) * LOT_SIZE
        elif shares:
            qty = int(shares) // LOT_SIZE * LOT_SIZE
        else:
            return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "请指定金额或股数")
        if qty < LOT_SIZE:
            return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "不足一手")
        amt = price * qty
        fee = self.calc_fee("buy", amt)
        if amt + fee > cash:
            return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "可用资金不足")
        cash -= amt + fee
        db.add_paper_lot(code, name, qty, (amt + fee) / qty, today, reason, today=today)
        db.insert_paper_trade(code, name, "buy", price, qty, amt, fee, reason, today)
        db.update_paper_cash(round(cash, 2))
        # 持仓纳入自选：保证后续卖出建议能覆盖到这只股票
        db.add_watch(code, name, note="持仓自动加入", source="auto")
        logger.info("手动买入 %s %d 股 @ %.2f", code, qty, price)
        return TradeResult(code, name, "buy", qty, price, amt, fee, reason)

    # ------------------------------------------------------------------
    # 止盈止损检查（每日收盘）
    # ------------------------------------------------------------------
    async def check_stop_loss_target(
        self, prices: dict[str, float] | None = None
    ) -> list[TradeResult]:
        """检查全部持仓的止盈止损，触发则按最新价市价卖出。

        ``prices`` 供实时撮合传入（逐 tick 检查，而非只在收盘后检查）。
        """
        self.ensure_account()  # 确保批次模型已迁移
        positions = db.list_paper_positions()
        if not positions:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        account = db.get_paper_account()
        cash = float(account["cash"])
        results: list[TradeResult] = []

        for pos in positions:
            if int(pos.get("sellable_shares") or 0) <= 0:
                continue  # T+1 无可卖批次（今仓留次日）
            price = await self._latest_price(pos["code"], prices)
            if price is None:
                continue
            cost = float(pos["cost_price"])
            change = price / cost - 1
            # 持仓期最高价（移动/保本止损跟踪），先更新再落库
            peak = float(pos.get("peak_price") or cost)
            if price > peak:
                peak = price
                db.update_paper_peak(pos["code"], peak)
            # 建仓时定死的止损线（含 ATR 放宽）；老持仓没有该字段时按当前固定止损现算
            stop_price = float(pos.get("stop_price") or 0) or cost * (1 - self._base_stop_ratio())
            # 盈利保护：浮盈达 breakeven 上移止损到成本；达 trail_arm 后跟踪高点回撤
            if self.settings.paper_breakeven_pct > 0 and peak >= cost * (1 + self.settings.paper_breakeven_pct):
                stop_price = max(stop_price, cost)
            if self.settings.paper_trail_pct > 0 and peak >= cost * (1 + self.settings.paper_trail_arm):
                stop_price = max(stop_price, peak * (1 - self.settings.paper_trail_pct))
            reason = None
            if price <= stop_price:
                reason = (
                    f"止损触发（成本{cost:.2f}，现价{price:.2f}，{change:+.1%}，"
                    f"止损线{stop_price:.2f}）"
                )
            elif change >= self._take_profit_ratio():
                reason = f"止盈触发（成本{cost:.2f}，现价{price:.2f}，{change:+.1%}）"
            if reason is None:
                continue
            # 严格 T+1：只卖可卖批次，今仓留到次日（由批次模型保证）
            sold = db.reduce_paper_sellable(pos["code"], int(pos["shares"]), today)
            if sold <= 0:
                continue
            amount = price * sold
            fee = self.calc_fee("sell", amount)
            cash += amount - fee
            db.insert_paper_trade(
                pos["code"], pos["name"], "sell", price, sold, amount, fee,
                reason, today,
            )
            results.append(TradeResult(
                pos["code"], pos["name"], "sell", sold, price, amount, fee,
                reason))

        if results:
            db.update_paper_cash(round(cash, 2))
            logger.info("止盈止损触发 %d 笔", len(results))
        return results

    def _idx_open_drop(self) -> float | None:
        """大盘当日开盘跌幅（护栏用）：取最弱主要指数当日涨跌幅的负值（即最大跌幅）。

        与做T crash guard、买入端跳空护栏共用，避免重复取数；``None`` 表示无法取指数。
        """
        try:
            from app.data.snapshot_store import snapshot_store
            indices = snapshot_store.peek_indices()
            if indices is not None and not indices.empty and "pct_change" in indices.columns:
                vals = [
                    float(getattr(r, "pct_change", 0) or 0)
                    for r in indices.itertuples(index=False)
                ]
                if vals:
                    return -min(vals)
        except Exception:  # noqa: BLE001
            return None
        return None

    def _open_delayed(self, now: datetime | None = None) -> bool:
        """是否仍处于「开盘延迟」期：各交易时段（09:30 / 13:00）开始后
        ``paper_buy_open_delay_minutes`` 分钟内，禁止自动买入。仅拦买入、不拦卖出。

        关键：只在**大盘向下跳空**时才延迟——外围也可能出**利好**（大盘上涨），
        无条件延迟会让利好日开盘踏空误伤；保护只针对下行风险，不针对上行收益。
        方向由大盘当日最弱指数涨跌幅判定（与跳空护栏同口径）。
        """
        delay = int(getattr(self.settings, "paper_buy_open_delay_minutes", 0) or 0)
        if delay <= 0:
            return False
        idx_open_drop = self._idx_open_drop()
        if idx_open_drop is None or idx_open_drop <= 0:
            return False
        if now is None:
            now = datetime.now()
        cur = now.hour * 60 + now.minute
        for start in (9 * 60 + 30, 13 * 60):
            if start <= cur < start + delay:
                return True
        return False

    def _open_cooldown(self, now: datetime | None = None) -> bool:
        """是否仍处于「开盘冷静期」：各交易时段（09:30 / 13:00）开始后
        ``paper_open_cooldown_minutes`` 分钟内，**无条件**禁止自动买入
        （含轮动换入、做T买回/加仓）。仅拦买入、不拦卖出。
        区别于 ``_open_delayed``（仅大盘向下跳空时才延迟）。用途：避开开盘瞬间
        波动/跳空，按使用者要求"开盘后 N 分钟才交易"（本轮）。"""
        delay = int(getattr(self.settings, "paper_open_cooldown_minutes", 0) or 0)
        if delay <= 0:
            return False
        if now is None:
            now = datetime.now()
        cur = now.hour * 60 + now.minute
        for start in (9 * 60 + 30, 13 * 60):
            if start <= cur < start + delay:
                return True
        return False

    # ------------------------------------------------------------------
    # 做T引擎（真实 tick，严格 T+1）
    # ------------------------------------------------------------------
    async def t_tick(self, prices: dict[str, float]) -> list:
        """做T引擎（真实 tick，严格 T+1）：涨停卖半 / 倒T / 下跌分级加仓 / 反弹减仓 / 大盘护栏。

        设计要点：
        - 卖出只动「可卖批次」（底仓），今仓留到次日才可卖；
        - 加仓/买回建「今仓」批次（tag=t_add/t_buyback），次日才可卖；
        - 每日只重置「下跌加仓已触发」（t_down_armed），使每票每日最多加一次；
          待买回股数（t_t_sold）**跨日保留**：当日 crash 护栏或价格未回落没买回的底仓，
          次日开盘恢复（回测侧 ``t_buyback_shares`` 同样是次日开盘买回，口径一致）；
        - 默认 paper_t_active=False，不影响现有模拟盘。
        """
        settings = self.settings
        if not settings.paper_t_active:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now()
        # 开盘延迟/冷静期：开盘后 N 分钟内禁止做T买入（买回/加仓），卖出照常
        open_delayed = self._open_delayed(now) or self._open_cooldown(now)
        self.ensure_account()  # 确保 lots migrate + 刷新可卖股数（T+1 跨日生效）

        # 每日重置：只清「下跌加仓已触发」，使每票每日最多加一次。
        # 待买回股数（t_t_sold）**不清零** —— 当日 crash 或价格未回落没买回的底仓要留到
        # 次日开盘恢复，否则做T会悄悄变成永久减仓（与回测口径也不一致）。
        if getattr(self, "_t_day", "") != today:
            self._t_day = today
            for p in db.list_paper_positions():
                db.update_paper_t_state(p["code"], t_down_armed=0)

        positions = db.list_paper_positions()
        if not positions:
            return []

        # 实时快照（内存，不刷新）：含 open/high/low/prev_close/price
        from app.data.snapshot_store import snapshot_store
        snap = snapshot_store.peek_snapshot()
        snap_map = {}
        if snap is not None and not snap.empty and "code" in snap.columns:
            for row in snap.itertuples(index=False):
                snap_map[str(getattr(row, "code", ""))] = row

        # 大盘当日开盘跌幅（护栏）：复用统一口径（与买入端跳空护栏同方法）
        idx_drop = self._idx_open_drop()

        account = db.get_paper_account()
        cash = float(account["cash"])
        trades = []

        # 做T加仓的总仓位/单票上限：与买入、轮动同口径，避免 ``cash * 0.95`` 式近乎满仓加仓
        # （单票打满、组合超配）。盘中常规上限=100%（80% 是收盘目标，由 eod_trim 压回），
        # 弱市/熔断/动态机制按下限硬压。
        total_asset = await self.total_asset(prices)
        single_ratio = float(
            getattr(self.settings, "paper_single_position_max", SINGLE_POSITION_MAX_RATIO)
            or SINGLE_POSITION_MAX_RATIO
        )
        eff_exposure = 1.0
        if bool(getattr(self.settings, "paper_circuit_breaker_enabled", False)):
            cb = await self._circuit_breaker(today, total_asset)
            if bool(cb["tripped"]):
                eff_exposure = min(eff_exposure, float(cb["target_exposure"]))
        elif bool(getattr(self.settings, "paper_dynamic_exposure", False)):
            dyn = await self._dynamic_regime(today)
            if dyn is not None:
                eff_exposure = min(eff_exposure, float(dyn["exposure_cap"]))
        elif (
            bool(getattr(self.settings, "paper_regime_weak_pause", False))
            and int(getattr(self.settings, "paper_regime_ma", 0) or 0) > 0
            and await self._index_is_weak(today)
        ):
            we = float(getattr(self.settings, "paper_weak_exposure", 0.0) or 0.0)
            if we > 0:
                eff_exposure = min(eff_exposure, we)
        exposure_cap = total_asset * eff_exposure

        def _num(v):
            try:
                f = float(v)
                return f if f == f else None
            except (TypeError, ValueError):
                return None

        for pos in positions:
            code = pos["code"]
            row = snap_map.get(code)
            if row is None:
                continue
            price = prices.get(code)
            if price is None:
                continue
            open_px = _num(getattr(row, "open", None))
            high = _num(getattr(row, "high", None))
            low = _num(getattr(row, "low", None))
            prev_close = _num(getattr(row, "prev_close", None))
            if None in (open_px, high, low, prev_close) or prev_close <= 0 or open_px <= 0:
                continue

            total = int(pos["shares"])
            sellable = int(pos.get("sellable_shares") or 0)
            t_t_sold = int(pos.get("t_t_sold") or 0)
            t_down_armed = int(pos.get("t_down_armed") or 0)
            name = pos.get("name", "")
            size = float(settings.paper_t_size or 0.25)

            # 大盘极速下跌护栏：暂停所有做T动作（仅允许已卖出待买回的平仓）
            crash = (
                bool(settings.paper_t_crash_guard)
                and idx_drop is not None
                and idx_drop >= float(settings.paper_t_crash_guard)
            )

            # 1) 买回（倒T/涨停卖半待买回）
            #    - 当日卖出（t_sold_date == today）：回落到 max(开盘,昨收)×(1+t_up_buyback)
            #      以内才买回；遇 crash 护栏当日不接刀，顺延次日；
            #    - 往日卖出（t_sold_date < today）：开盘直接恢复底仓，不看 crash 与买回线
            #      （回测侧 t_buyback_shares 也是次日开盘无条件买回，两边口径一致）。
            if t_t_sold > 0:
                stale = str(pos.get("t_sold_date") or today) < today
                if crash and not stale:
                    continue
                if open_delayed and not stale:
                    continue
                buyback_line = max(open_px, prev_close) * (1 + float(settings.paper_t_up_buyback))
                if stale or price <= buyback_line:
                    want = (t_t_sold // LOT_SIZE) * LOT_SIZE
                    if want >= LOT_SIZE:
                        amount = price * want
                        fee = self.calc_fee("buy", amount)
                        if amount + fee <= cash:
                            cash -= amount + fee
                            db.add_paper_lot(code, name, want, price, today, "做T买回", today=today, tag="t_buyback")
                            db.update_paper_t_state(code, t_t_sold=0, t_sold_date="")
                            db.insert_paper_trade(code, name, "buy", price, want, amount, fee, "做T买回", today)
                            trades.append(TradeResult(code, name, "buy", want, price, amount, fee, "做T买回"))
                        else:
                            logger.warning(
                                "做T买回资金不足 %s：待买回 %d 股，现金 %.2f（保留待买回状态）",
                                code, want, cash,
                            )
                continue

            if crash:
                continue

            # 2) 涨停卖半（封板弱）：盘中高点触涨停且仍有可卖底仓 → 卖 1/2
            if settings.paper_t_limit_sell_half and high >= prev_close * 1.095 and sellable >= LOT_SIZE:
                want = ((sellable // 2) // LOT_SIZE) * LOT_SIZE
                if want >= LOT_SIZE:
                    sold = db.reduce_paper_sellable(code, want, today)
                    if sold > 0:
                        amount = price * sold
                        fee = self.calc_fee("sell", amount)
                        cash += amount - fee
                        db.insert_paper_trade(code, name, "sell", price, sold, amount, fee, "涨停卖半", today)
                        db.update_paper_t_state(code, t_t_sold=sold, t_sold_date=today)
                        trades.append(TradeResult(code, name, "sell", sold, price, amount, fee, "涨停卖半"))
                continue

            open_chg = open_px / prev_close - 1

            # 3) 倒T：开盘涨且盘中触 +t_up_sell → 卖 t_size 可卖底仓，待买回
            if (
                settings.paper_t_up_sell
                and open_chg > 0
                and high >= open_px * (1 + float(settings.paper_t_up_sell))
                and sellable >= LOT_SIZE
            ):
                want = int(total * size / LOT_SIZE) * LOT_SIZE
                want = min(want, sellable)
                want = (want // LOT_SIZE) * LOT_SIZE
                if want >= LOT_SIZE:
                    sold = db.reduce_paper_sellable(code, want, today)
                    if sold > 0:
                        amount = price * sold
                        fee = self.calc_fee("sell", amount)
                        cash += amount - fee
                        db.insert_paper_trade(code, name, "sell", price, sold, amount, fee, "做T卖出", today)
                        db.update_paper_t_state(code, t_t_sold=sold, t_sold_date=today)
                        trades.append(TradeResult(code, name, "sell", sold, price, amount, fee, "做T卖出"))
                continue

            # 4) 下跌分级加仓（今仓）/ 反弹减仓（次日卖加仓批次，T+1）
            if open_chg < 0:
                # 反弹减仓：已有「可卖」的做T加仓批次（今仓次日才可卖）→ 只减加仓批次
                if settings.paper_t_rebound_sell and price >= prev_close * (1 - float(settings.paper_t_rebound_sell)):
                    sold = db.reduce_paper_lots_by_tag(code, "t_add", 10**9, today)
                    if sold > 0:
                        amount = price * sold
                        fee = self.calc_fee("sell", amount)
                        cash += amount - fee
                        db.insert_paper_trade(code, name, "sell", price, sold, amount, fee, "反弹减仓", today)
                        trades.append(TradeResult(code, name, "sell", sold, price, amount, fee, "反弹减仓"))
                        continue  # 本轮已减仓，不再加仓
                # 分级加仓（今仓，T+1 次日可卖）：每日每票最多一次（t_down_armed 当日内防重复）
                if t_down_armed == 0:
                    if open_delayed:
                        # 开盘延迟期内不触发下跌加仓（避免开盘跳空接刀），标记避免反复尝试
                        db.update_paper_t_state(code, t_down_armed=1)
                        continue
                    budget = 0.0
                    if settings.paper_t_down_full and low <= open_px * (1 - float(settings.paper_t_down_full)):
                        budget = cash * 0.95
                    elif settings.paper_t_down_add and low <= open_px * (1 - float(settings.paper_t_down_add)):
                        budget = cash * float(settings.paper_t_down_add)
                    if budget > 0:
                        # 套上单票上限与总仓位上限：单票市值不超过总资产×single_ratio，
                        # 加仓后总持仓不超过 exposure_cap（防止单票打满/组合超配）。
                        pos_mv = total * price
                        single_room = max(0.0, total_asset * single_ratio - pos_mv)
                        total_room = max(0.0, exposure_cap - (total_asset - cash))
                        budget = min(budget, single_room, total_room)
                        est = int(budget / price / LOT_SIZE) * LOT_SIZE
                        if est >= LOT_SIZE:
                            amount = price * est
                            fee = self.calc_fee("buy", amount)
                            while est >= LOT_SIZE and amount + fee > cash:
                                est -= LOT_SIZE
                                amount = price * est
                                fee = self.calc_fee("buy", amount)
                            if est >= LOT_SIZE:
                                cash -= amount + fee
                                db.add_paper_lot(code, name, est, price, today, "做T加仓", today=today, tag="t_add")
                                db.update_paper_t_state(code, t_down_armed=1)
                                db.insert_paper_trade(code, name, "buy", price, est, amount, fee, "做T加仓", today)
                                trades.append(TradeResult(code, name, "buy", est, price, amount, fee, "做T加仓"))
                        else:
                            db.update_paper_t_state(code, t_down_armed=1)  # 现金不足也标记，避免反复尝试

        db.update_paper_cash(round(cash, 2))
        return trades

    # ------------------------------------------------------------------
    # 收盘减仓（80% 是收盘目标，不是盘中硬上限）
    # ------------------------------------------------------------------
    def _in_eod_window(self, now: datetime) -> bool:
        """是否处于收盘前的减仓窗口（15:00 前 N 分钟）。N<=0 视为关闭。"""
        minutes = int(getattr(self.settings, "paper_eod_trim_minutes", 0) or 0)
        if minutes <= 0:
            return False
        close = now.replace(hour=15, minute=0, second=0, microsecond=0)
        return close - timedelta(minutes=minutes) <= now < close

    async def eod_trim(
        self, prices: dict[str, float] | None = None, *, now: datetime | None = None
    ) -> list[TradeResult]:
        """收盘前把总仓位压回 ``paper_max_total_exposure``（默认 80%），卖不掉不强求。

        口径（2026-11-02 使用者）：80% 是**收盘目标**而不是盘中硬上限——盘中允许满仓，
        留 20% 现金是为了次日做T 有子弹。因此只在收盘前 ``paper_eod_trim_minutes`` 分钟内
        动作：按「建仓评分最低优先」卖出超出目标的那部分金额（整手），
        当日买入的今仓（T+1 不可卖）直接跳过；全都卖不动就只记日志，不强求。

        为什么不做部分减仓的精确优化：目标是「收盘前尽量」，多卖一手的误差远小于
        为了精确到分而反复下单带来的摩擦与日志噪声。
        """
        if not bool(getattr(self.settings, "paper_eod_trim_enabled", False)):
            return []
        now = now or datetime.now()
        if not self._in_eod_window(now):
            return []
        positions = db.list_paper_positions()
        if not positions:
            return []
        total = await self.total_asset(prices)
        if total <= 0:
            return []
        acct = db.get_paper_account() or {}
        cash = float(acct.get("cash") or 0.0)
        target_ratio = min(1.0, float(self.settings.paper_max_total_exposure))
        need = max(0.0, total - cash) - total * target_ratio
        if need <= 0:
            return []
        today = now.strftime("%Y-%m-%d")
        queue = sorted(
            (
                p for p in positions
                if "手动" not in str(p.get("strategy") or "")
                and str(p.get("buy_date") or "") < today
            ),
            key=lambda p: float(p.get("score") or 0.0),
        )
        if not queue:
            logger.info(
                "收盘减仓：仓位超目标 %.0f 元，但没有可卖持仓（今仓 T+1 全部不可卖），不强求",
                need,
            )
            return []
        results: list[TradeResult] = []
        for pos in queue:
            if need <= 0:
                break
            code = pos["code"]
            price = await self._latest_price(code, prices)
            if not price or price <= 0:
                continue
            shares = int(pos["shares"])
            # 差一点也按一手减：否则「超一点点但一手就超」会永远减不动
            qty = min(shares, max(LOT_SIZE, int(need / price / LOT_SIZE) * LOT_SIZE))
            sold = db.reduce_paper_sellable(code, qty, today)
            if sold <= 0:
                continue
            amount = price * sold
            fee = self.calc_fee("sell", amount)
            cash += amount - fee
            need -= amount
            reason = f"收盘减仓(仓位→{target_ratio:.0%})"
            db.insert_paper_trade(
                code, pos.get("name", ""), "sell", price, sold, amount, fee, reason, today,
            )
            results.append(TradeResult(
                code, pos.get("name", ""), "sell", sold, price, amount, fee, reason))
            logger.info("收盘减仓：卖出 %s %d 股（建仓 %.0f 分）@ %.2f",
                        code, sold, float(pos.get("score") or 0.0), price)
        if results:
            db.update_paper_cash(round(cash, 2))
            logger.info("收盘减仓完成：%d 笔，剩余超出 %.0f 元", len(results), max(0.0, need))
        return results

    # ------------------------------------------------------------------
    # 账户查询与结算
    # ------------------------------------------------------------------
    async def total_asset(self, prices: dict[str, float] | None = None) -> float:
        """总资产 = 现金 + 持仓市值（按最新价）。

        **取价失败时按成本价计入**，与 :meth:`account_detail` 保持同一口径。这里原来
        是「取不到价就跳过该持仓」（市值按 0 算）：一旦某只票的实时快照与 K 线**同时**
        取不到（停牌、数据源熔断——2026-10-31 的日志里就有成片「降级为腾讯K线 / 熔断
        300 秒」），一个 20% 仓位会瞬间从总资产里消失，8% 的组合回撤熔断当场误触发，
        而且熔断状态会落库、跨重启保留。

        回退成本价不是"美化净值"：它对浮亏是**中性**的（不涨不跌），比按 0 计市值
        这种把未实现亏损一次性放大的做法更接近真实。取价失败本身会打 warning 留痕。
        """
        account = self.ensure_account()
        cash = float(account["cash"])
        market_value = 0.0
        prices = self._with_snapshot_prices(prices)
        for pos in db.list_paper_positions():
            shares = int(pos["shares"])
            price = await self._latest_price(pos["code"], prices)
            if price is None:
                price = float(pos["cost_price"])
                logger.warning(
                    "总资产核算：%s 取价失败，按成本价 %.2f 计入市值（避免误触发熔断）",
                    pos["code"], price,
                )
            market_value += price * shares
        return cash + market_value

    async def account_detail(self) -> dict:
        """账户详情（含持仓浮盈与收益率）。"""
        account = self.ensure_account()
        cash = float(account["cash"])
        initial = float(account["initial_capital"])

        positions = []
        market_value = 0.0
        cost_total = 0.0
        quotes = self._with_snapshot_prices(None)
        for pos in db.list_paper_positions():
            price = await self._latest_price(pos["code"], quotes) or float(pos["cost_price"])
            shares = int(pos["shares"])
            mv = price * shares
            cost = float(pos["cost_price"]) * shares
            market_value += mv
            cost_total += cost
            positions.append({
                "code": pos["code"], "name": pos["name"], "shares": shares,
                "cost_price": round(float(pos["cost_price"]), 2),
                "price": round(price, 2),
                "market_value": round(mv, 2),
                "pnl": round(mv - cost, 2),
                "pnl_pct": round((price / float(pos["cost_price"]) - 1) * 100, 2),
                "buy_date": pos["buy_date"], "strategy": pos.get("strategy", ""),
            })

        total = cash + market_value
        return {
            "initial_capital": initial,
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "total_asset": round(total, 2),
            "total_pnl": round(total - initial, 2),
            "return_pct": round((total / initial - 1) * 100, 2),
            "position_count": len(positions),
            "positions": positions,
        }

    async def settle(self, *, record_regime: bool = True) -> dict:
        """每日收盘结算：止盈止损检查 + 记录净值快照 + 记录当日择时状态。

        ``record_regime=False`` 用于并行模拟账本：择时状态是全局单例（每日一行），
        各账本的 ``paper_regime_ma`` 可能不同，让它们各自写会把主账本那行覆盖掉。
        """
        await self.check_stop_loss_target()
        detail = await self.account_detail()
        today = datetime.now().strftime("%Y-%m-%d")
        db.save_paper_equity(
            today, detail["total_asset"], detail["cash"], detail["market_value"]
        )
        if record_regime:
            await self._save_regime_snapshot(today, detail)
        logger.info("模拟盘收盘结算：总资产 %.2f（%+.2f%%）",
                    detail["total_asset"], detail["return_pct"])
        return detail

    async def _save_regime_snapshot(self, today: str, detail: dict) -> None:
        """把当日择时状态落一行，供事后区分「弱市没信号」与「本来就不弱」。

        为什么需要（BACKTEST_LOG 记录 #70 §8）：``paper_regime_paused:{date}``
        只在**发生拦截**时才写，于是「弱市但没有买入信号」的一天与「不是弱市」
        在库里长得一样（都没有键）。而 #70 冻结的对账项之一正是
        「弱市日总仓位是否 ≤30%」——缺了当日是否弱势这个前提，这个数核对不了。

        这里同时记录**判定输入**（指数收盘/均线）与**仓位结果**（总仓位比例），
        事后不必再靠日志反推判定是否正确。
        """
        n = int(getattr(self.settings, "paper_regime_ma", 0) or 0)
        if n > 0:
            info = await self._index_regime_detail(today, n)
        else:
            info = {"ma": 0, "prev_close": None, "prev_ma": None,
                    "weak": False, "reason": "择时未启用"}
        total = float(detail["total_asset"] or 0)
        market_value = float(detail["market_value"] or 0)
        payload = {
            "date": today,
            "enabled": bool(getattr(self.settings, "paper_regime_weak_pause", False)) and n > 0,
            "ma": info["ma"],
            "index_prev_close": info["prev_close"],
            "index_prev_ma": info["prev_ma"],
            "weak": bool(info["weak"]),
            "note": info["reason"],
            "weak_exposure": float(getattr(self.settings, "paper_weak_exposure", 0.0) or 0.0),
            "paused_count": int(self._regime_paused if self._regime_day == today else 0),
            # 硬性回撤熔断 + 政策托底强度（BACKTEST_LOG #85）：事后可区分"弱市没信号"与
            # "已被熔断锁仓"，以及当时托底强度是否支撑"托底型阴跌"判定。
            "circuit_breaker_tripped": bool(self._cb_tripped),
            "floor_score": (
                self._regime_dyn_cache[1].get("floor", {}).get("score")
                if bool(getattr(self.settings, "paper_policy_floor_enabled", False)) else None
            ),
            "total_asset": round(total, 2),
            "market_value": round(market_value, 2),
            "exposure": round(market_value / total, 4) if total > 0 else 0.0,
        }
        try:
            db.set_state(
                f"paper_regime_state:{today}", json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:  # noqa: BLE001 - 记录失败不能影响结算
            logger.debug("择时状态落库失败: %s", exc)
