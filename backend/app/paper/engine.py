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
     评分高出 paper_rotation_gap，则卖出最弱持仓换入（T+1、每日限次由配置控制）；
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
import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.config import get_settings
from app.data.cache import db
from app.data.service import get_kline_cached

logger = logging.getLogger("sas.paper")

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

    def __init__(self) -> None:
        self.settings = get_settings()

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
        """下单前的准入检查：板块范围 / 北交所 / 名称可信度 / 成交额 TopN 池。

        板块范围与选股 scanner 统一口径（app.data.scope.is_tradable）：只做
        沪深主板+创业板，排除科创板(688)/北交所(92,43,83,87,88)/B股/ST/巨无霸。
        """
        code = str(code or "").zfill(6)
        if getattr(self.settings, "paper_exclude_bj", False) and _is_bj(code):
            return False, "北交所股票已排除（PAPER_EXCLUDE_BJ）"
        from app.data.scope import is_tradable
        if not is_tradable(code, name)[0]:
            return False, "不在沪深主板+创业板范围（scope 过滤）"
        if not _name_ok(name, code):
            return False, "名称缺失或乱码，拒绝下单（股票池可能不完整）"
        pool = self._top_amount_codes()
        if pool is not None and code not in pool:
            top_n = int(getattr(self.settings, "paper_pool_top_n", 0) or 0)
            return False, f"不在当日成交额Top{top_n}池（与回测口径对齐）"
        return True, ""

    def _stop_distance(self, code: str) -> float:
        """止损距离：固定 8%，开启 ATR 止损时取 max(8%, N×ATR%)，上限可配。

        高波动票的日振幅常达 5~10%，固定 8% ≈ 1×ATR，止损必然被正常波动打掉
        （实盘 2 天 5 笔 −8% 止损全是「刚碰线就砍」）。ATR 版本只放宽不收紧，
        与回测里 ``e30_a15`` 的机制一致。
        """
        base = STOP_LOSS_RATIO
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

        指数文件 ``idx_sh000001.parquet`` 缺失时退化为「不暂停」（避免数据源抖动直接停交易）。
        """
        n = int(getattr(self.settings, "paper_regime_ma", 0) or 0)
        if n <= 0:
            return False
        try:
            idx_path = self.settings.kline_dir / "idx_sh000001.parquet"
            if not idx_path.exists():
                return False
            df = await asyncio.to_thread(pd.read_parquet, idx_path)
            if df is None or df.empty or "close" not in df.columns or len(df) < n + 1:
                return False
            close = df["close"].astype(float)
            ma = close.rolling(n).mean().shift(1)  # 前一交易日，避免未来函数
            last = df[df["date"] <= today] if "date" in df.columns else df
            i = (len(last) - 1) if not last.empty else (len(df) - 1)
            if i <= 0:
                return False
            prev_close = float(close.iloc[i - 1])
            prev_ma = float(ma.iloc[i - 1])
            if prev_ma != prev_ma or prev_ma <= 0:
                return False
            return prev_close < prev_ma
        except Exception as exc:  # noqa: BLE001
            logger.debug("大盘择时判断失败: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 费用计算
    # ------------------------------------------------------------------
    @staticmethod
    def calc_fee(side: str, amount: float) -> float:
        """按 A 股标准费率计算单笔费用。

        佣金万2.5（最低5元，双边）+ 印花税千1（仅卖出）+ 过户费万0.1（双边）。
        """
        commission = max(amount * FEE_COMMISSION_RATE, FEE_COMMISSION_MIN)
        stamp = amount * FEE_STAMP_RATE if side == "sell" else 0.0
        transfer = amount * FEE_TRANSFER_RATE
        return round(commission + stamp + transfer, 2)

    # ------------------------------------------------------------------
    # 信号执行（核心）
    # ------------------------------------------------------------------
    async def execute_recommendations(
        self, recommendations: list, prices: dict[str, float] | None = None
    ) -> list[TradeResult]:
        """执行一批买卖建议（智能建议引擎的输出）。

        Args:
            recommendations: Recommendation 对象列表（action/price 等字段）
            prices: 可选的逐股最新价（实时撮合传入），缺省时内部取最新价

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

        # ---- 大盘择时买入闸门：弱市暂停自动买入与轮动（仅保留卖出） ----
        market_weak = (
            bool(self.settings.paper_regime_weak_pause)
            and int(self.settings.paper_regime_ma) > 0
            and await self._index_is_weak(today)
        )
        if market_weak:
            logger.info("大盘弱势（指数在 MA 下），暂停自动买入/轮动，仅执行卖出")

        # ---- 轮动替换：满仓时用显著更高分的新票换掉最弱持仓 ----
        # 触发条件（全部满足）：开关开启 + 持仓已满 + 存在未持仓的买入候选 +
        #   新票评分 >= rotation_min_score 且比最弱持仓的建仓评分高出 rotation_gap +
        #   最弱持仓非当日买入（T+1）+ 当日轮动次数未达上限。
        if (
            self.settings.paper_rotation_enabled
            and not market_weak
            and len(positions) >= self.settings.paper_max_positions
        ):
            fresh = [r for r in buy_candidates if r.code not in positions]
            if fresh:
                best = max(fresh, key=_rec_score)
                best_score = _rec_score(best)
                weak_code: str | None = None
                weak_pos: dict | None = None
                weak_score = 0.0
                for code, pos in positions.items():
                    # 手动下单建仓的持仓不打分，也不允许被自动轮动卖出
                    if "手动" in str(pos.get("strategy") or ""):
                        continue
                    score = float(pos.get("score") or 0.0)
                    if weak_pos is None or score < weak_score:
                        weak_code, weak_pos, weak_score = code, pos, score
                used = db.count_paper_trades("轮动替换", today, side="sell")
                can_rotate = (
                    weak_code is not None
                    and best_score >= self.settings.paper_rotation_min_score
                    and best_score - weak_score >= self.settings.paper_rotation_gap
                    and str(weak_pos.get("buy_date") or "") < today
                    and used < self.settings.paper_rotation_max_per_day
                )
                if can_rotate:
                    price = await self._latest_price(weak_code, prices)
                    if price and price > 0:
                        # 严格 T+1：只卖可卖批次，今仓留到次日
                        sold = db.reduce_paper_sellable(weak_code, int(weak_pos["shares"]), today)
                        if sold > 0:
                            amount = price * sold
                            fee = self.calc_fee("sell", amount)
                            cash += amount - fee
                            reason = (
                                f"轮动替换(建仓{weak_score:.0f}分→新票{best_score:.0f}分)"
                            )
                            db.insert_paper_trade(
                                weak_code, weak_pos.get("name", ""), "sell", price,
                                sold, amount, fee, reason, today,
                            )
                            positions.pop(weak_code, None)
                            results.append(TradeResult(
                                weak_code, weak_pos.get("name", ""), "sell", sold,
                                price, amount, fee, reason,
                            ))
                            logger.info(
                                "轮动替换：卖出 %s（建仓 %.0f 分），换入 %s（%.0f 分）",
                                weak_code, weak_score, best.code, best_score,
                            )

        # ---- 买入（第 3 次排序：评分加权仓位 + 评分分档 + 总仓位上限） ----
        # 规则：
        #   1) 评分 < half_score(25) 只记录不下单；half~full(25~40) 权重打 5 折；
        #   2) 高分先买，可投预算按「评分×档位系数」比例分配；
        #   3) 新建仓市值不超过总资产 × paper_max_total_exposure(80%)；
        #   4) 单票不超过总资产 20%，整手取整，费用从现金扣除。
        max_positions = self.settings.paper_max_positions
        full_score = self.settings.paper_full_score
        half_score = self.settings.paper_half_score
        # 现有持仓市值 = 总资产 - 现金；总仓位上限扣掉已有仓位后才是本轮可投预算
        exposure_cap = total_asset * self.settings.paper_max_total_exposure
        available_budget = max(0.0, exposure_cap - max(0.0, total_asset - cash))
        if market_weak:
            for rec in buy_candidates:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", "") or rec.code, "skip", 0,
                    0.0, 0.0, 0.0, "大盘弱势，暂停买入"))
            buy_candidates = []

        candidates: list[tuple[float, object, float]] = []
        for rec in buy_candidates:
            if rec.code in positions:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, "已持仓"))
                continue
            allowed, why = self._buy_allowed(rec.code, getattr(rec, "name", ""))
            if not allowed:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", "") or rec.code, "skip", 0,
                    0.0, 0.0, 0.0, why))
                continue
            if len(positions) >= max_positions:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, f"持仓已满({max_positions}只)"))
                continue
            price = await self._latest_price(rec.code, prices)
            if price is None or price <= 0:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, "取价失败"))
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
                    f"总仓位已达上限({self.settings.paper_max_total_exposure:.0%})"))
                continue
            # 目标金额 = 本轮可投预算 × 加权占比，再受单票上限与剩余现金约束
            share_of_pool = weight / weight_total if weight_total > 0 else 1.0
            budget = min(
                available_budget * share_of_pool,
                total_asset * SINGLE_POSITION_MAX_RATIO,
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
            # 建仓时定死的止损线（含 ATR 放宽）；老持仓没有该字段时按固定 8% 现算
            stop_price = float(pos.get("stop_price") or 0) or cost * (1 - STOP_LOSS_RATIO)
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
            elif change >= TAKE_PROFIT_RATIO:
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

        # 大盘当日涨跌幅（护栏）：取最弱主要指数当日跌幅
        idx_drop = None
        try:
            indices = snapshot_store.peek_indices()
            if indices is not None and not indices.empty and "pct_change" in indices.columns:
                vals = [
                    float(getattr(r, "pct_change", 0) or 0)
                    for r in indices.itertuples(index=False)
                ]
                if vals:
                    idx_drop = -min(vals)
        except Exception:  # noqa: BLE001
            idx_drop = None

        account = db.get_paper_account()
        cash = float(account["cash"])
        trades = []

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
                    budget = 0.0
                    if settings.paper_t_down_full and low <= open_px * (1 - float(settings.paper_t_down_full)):
                        budget = cash * 0.95
                    elif settings.paper_t_down_add and low <= open_px * (1 - float(settings.paper_t_down_add)):
                        budget = cash * float(settings.paper_t_down_add)
                    if budget > 0:
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
    # 账户查询与结算
    # ------------------------------------------------------------------
    async def total_asset(self, prices: dict[str, float] | None = None) -> float:
        """总资产 = 现金 + 持仓市值（按最新价）。"""
        account = self.ensure_account()
        cash = float(account["cash"])
        market_value = 0.0
        for pos in db.list_paper_positions():
            price = await self._latest_price(pos["code"], prices)
            if price is not None:
                market_value += price * int(pos["shares"])
        return cash + market_value

    async def account_detail(self) -> dict:
        """账户详情（含持仓浮盈与收益率）。"""
        account = self.ensure_account()
        cash = float(account["cash"])
        initial = float(account["initial_capital"])

        positions = []
        market_value = 0.0
        cost_total = 0.0
        for pos in db.list_paper_positions():
            price = await self._latest_price(pos["code"]) or float(pos["cost_price"])
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

    async def settle(self) -> dict:
        """每日收盘结算：止盈止损检查 + 记录净值快照。"""
        await self.check_stop_loss_target()
        detail = await self.account_detail()
        today = datetime.now().strftime("%Y-%m-%d")
        db.save_paper_equity(
            today, detail["total_asset"], detail["cash"], detail["market_value"]
        )
        logger.info("模拟盘收盘结算：总资产 %.2f（%+.2f%%）",
                    detail["total_asset"], detail["return_pct"])
        return detail
