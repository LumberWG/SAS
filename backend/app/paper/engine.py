"""模拟交易引擎：自动执行买卖信号、管理仓位、记录净值。

设计目标：验证选股思路的准确性——系统分析产生的买卖建议
（或策略信号）由本引擎自动执行，形成可复盘的资金曲线。

交易规则（对齐 A 股实盘，简化但透明）:
  1. 初始资金默认 10 万，账户为单账户（个人使用）；
  2. 仓位管理：按建议评分加权（第 3 次排序）——评分高者先买、分到更多资金，
     预算 = min(可用现金 × 该股评分占比, 总资产×20%)，最多持有
     max_positions（默认10）只；
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
            if pos["buy_date"] >= today:
                results.append(TradeResult(
                    rec.code, pos["name"], "skip", 0,
                    0.0, 0.0, 0.0, "T+1 当日买入不可卖"))
                continue
            price = await self._latest_price(rec.code, prices)
            if price is None:
                results.append(TradeResult(
                    rec.code, pos["name"], "skip", 0,
                    0.0, 0.0, 0.0, "取价失败"))
                continue
            shares = int(pos["shares"])
            amount = price * shares
            fee = self.calc_fee("sell", amount)
            cash += amount - fee
            db.insert_paper_trade(
                rec.code, pos["name"], "sell", price, shares, amount, fee,
                getattr(rec, "reason", "卖出信号"), today,
            )
            db.remove_paper_position(rec.code)
            results.append(TradeResult(
                rec.code, pos["name"], "sell", shares, price, amount, fee,
                getattr(rec, "reason", "卖出信号")))

        # ---- 买入（第 3 次排序：评分加权仓位，受持仓数与单票上限约束） ----
        # 规则：评分高者先买；可用现金按评分比例分配（分数越高仓位越大），
        # 单票不超过总资产 20%，整手取整，费用从现金扣除。
        max_positions = self.settings.paper_max_positions
        candidates: list[tuple[float, object, float]] = []
        for rec in buy_candidates:
            if rec.code in positions:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, "已持仓"))
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
        weights = {rec.code: max(score, 1.0) for score, rec, _ in candidates}

        for _score, rec, price in candidates:
            weight = weights.pop(rec.code, 1.0)
            weight_total = weight + sum(weights.values())
            if len(positions) >= max_positions:
                results.append(TradeResult(
                    rec.code, getattr(rec, "name", ""), "skip", 0,
                    0.0, 0.0, 0.0, f"持仓已满({max_positions}只)"))
                continue
            # 目标金额 = 可用现金 × 评分占比，再受单票上限与剩余现金约束
            share_of_cash = weight / weight_total if weight_total > 0 else 1.0
            budget = min(
                cash * share_of_cash,
                total_asset * SINGLE_POSITION_MAX_RATIO,
                cash,
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
            name = getattr(rec, "name", "") or rec.code
            db.upsert_paper_position(
                rec.code, name, shares, cost_price, today,
                getattr(rec, "reason", "买入信号"),
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

        # ---- 卖出 ----
        if side == "sell":
            pos = db.get_paper_position(code)
            if pos is None:
                return TradeResult(code, name, "skip", 0, price, 0.0, 0.0, "无持仓可卖")
            if pos["buy_date"] >= today:
                return TradeResult(
                    code, pos["name"], "skip", 0, price, 0.0, 0.0, "T+1 当日买入不可卖"
                )
            held = int(pos["shares"])
            if not shares or int(shares) >= held:
                qty = held
            else:
                qty = int(shares) // LOT_SIZE * LOT_SIZE
            if qty <= 0:
                return TradeResult(code, pos["name"], "skip", 0, price, 0.0, 0.0, "股数不足一手")
            amt = price * qty
            fee = self.calc_fee("sell", amt)
            cash += amt - fee
            db.insert_paper_trade(
                code, pos["name"], "sell", price, qty, amt, fee, reason, today
            )
            if qty >= held:
                db.remove_paper_position(code)
            else:
                # 部分卖出：剩余仓位保持原成本与买入日
                db.upsert_paper_position(
                    code, pos["name"], held - qty, float(pos["cost_price"]),
                    pos["buy_date"], pos.get("strategy", ""),
                )
            db.update_paper_cash(round(cash, 2))
            logger.info("手动卖出 %s %d 股 @ %.2f", code, qty, price)
            return TradeResult(code, pos["name"], "sell", qty, price, amt, fee, reason)

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
        db.upsert_paper_position(code, name, qty, (amt + fee) / qty, today, reason)
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
        positions = db.list_paper_positions()
        if not positions:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        account = db.get_paper_account()
        cash = float(account["cash"])
        results: list[TradeResult] = []

        for pos in positions:
            if pos["buy_date"] >= today:
                continue  # T+1 不可卖
            price = await self._latest_price(pos["code"], prices)
            if price is None:
                continue
            cost = float(pos["cost_price"])
            change = price / cost - 1
            reason = None
            if change <= -STOP_LOSS_RATIO:
                reason = f"止损触发（成本{cost:.2f}，现价{price:.2f}，{change:+.1%}）"
            elif change >= TAKE_PROFIT_RATIO:
                reason = f"止盈触发（成本{cost:.2f}，现价{price:.2f}，{change:+.1%}）"
            if reason is None:
                continue
            shares = int(pos["shares"])
            amount = price * shares
            fee = self.calc_fee("sell", amount)
            cash += amount - fee
            db.insert_paper_trade(
                pos["code"], pos["name"], "sell", price, shares, amount, fee,
                reason, today,
            )
            db.remove_paper_position(pos["code"])
            results.append(TradeResult(
                pos["code"], pos["name"], "sell", shares, price, amount, fee,
                reason))

        if results:
            db.update_paper_cash(round(cash, 2))
            logger.info("止盈止损触发 %d 笔", len(results))
        return results

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
