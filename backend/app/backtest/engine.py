"""轻量向量化回测引擎（日级，信号驱动）。

交易规则（简化但透明，便于学习理解）:
  - 买入: 策略在某日收盘后发出信号，以当日收盘价成交；
    资金等分给最多 max_positions 只股票；A股整手（100股）交易。
  - 卖出: 触及止损价 / 目标价 / 回测结束，以当日收盘价成交。
  - 费用: 佣金万2.5（commission，双边）+ 印花税千0.5（stamp_tax，仅卖出）。

绩效指标（公式见各函数注释）:
  - 总收益率 / 年化收益率 / 最大回撤 / 夏普比率 / 胜率 / 交易明细。

局限: 日级回测不考虑日内滑点与涨跌停无法成交的情况，
信号用收盘价成交已包含一定理想化假设，实盘需更严格建模。
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.service import get_kline_cached
from app.indicators import add_all_indicators
from app.strategies.base import Strategy

logger = logging.getLogger("sas.backtest")

#: 指标预热所需的历史根数（保证回测起点指标已填满）
_WARMUP_BARS = 90


@dataclass
class BacktestParams:
    """回测参数。"""

    strategy: str                       # 策略名
    start_date: str                     # YYYY-MM-DD
    end_date: str                       # YYYY-MM-DD
    initial_capital: float = 1_000_000  # 初始资金（元）
    max_positions: int = 5              # 最大持仓股票数
    commission: float = 0.00025         # 佣金率（双边）
    stamp_tax: float = 0.0005           # 印花税率（仅卖出）
    codes: list[str] | None = None      # 限定股票池；缺省用成交额前 N
    universe_size: int = 300            # 缺省股票池大小（按成交额）


@dataclass
class Position:
    """持仓记录。"""

    code: str
    name: str
    shares: int
    cost: float          # 买入均价（含佣金）
    stop_loss: float
    target_price: float
    buy_date: str


@dataclass
class Trade:
    """已完成交易（平仓）记录。"""

    code: str
    name: str
    buy_date: str
    sell_date: str
    buy_price: float
    sell_price: float
    shares: int
    pnl: float
    pnl_pct: float
    reason: str          # 平仓原因：止损/止盈/到期


class BacktestEngine:
    """回测引擎。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run(self, params: BacktestParams) -> dict:
        """执行回测，返回结果 dict（含净值曲线与绩效指标）。"""
        from app.data.akshare_source import get_data_source
        from app.strategies import registry

        strategy: Strategy = registry.create(params.strategy)
        codes = params.codes or await self._default_universe(params.universe_size)

        # 预热期起点（指标需要额外历史）
        warmup_start = self._shift_date(params.start_date, -_WARMUP_BARS)

        semaphore = asyncio.Semaphore(self.settings.scan_concurrency)
        events: list[dict] = []          # 全部买入信号事件
        klines: dict[str, pd.DataFrame] = {}  # code -> 区间K线（止盈止损判断用）

        async def fetch_one(code: str, name: str) -> None:
            async with semaphore:
                try:
                    df = await asyncio.to_thread(get_kline_cached, code)
                    if df is None or df.empty:
                        return
                    df = df[df["date"] >= warmup_start]
                    df = df[df["date"] <= params.end_date]
                    if len(df) < strategy.min_bars + _WARMUP_BARS // 2:
                        return
                    # 重置索引，保证后续 iloc 位置与标签一致
                    df = df.reset_index(drop=True)
                    df = await asyncio.to_thread(add_all_indicators, df)
                    klines[code] = df
                    # 逐日评估：仅对回测区间内的日期发信号
                    start_idx = self._first_index_on_or_after(df, params.start_date)
                    found = await asyncio.to_thread(
                        self._collect_events, strategy, df, start_idx, code, name
                    )
                    events.extend(found)
                except Exception as exc:  # noqa: BLE001 - 单股失败跳过
                    logger.debug("回测取数失败 %s: %s", code, exc)

        pool = await asyncio.to_thread(self._resolve_pool, codes)
        await asyncio.gather(*(fetch_one(r.code, r.name) for r in pool.itertuples()))
        logger.info("回测信号生成完毕: %d 只股票, %d 个信号事件", len(klines), len(events))

        # ---- 模拟交易（放线程：逐日撮合是 CPU 密集，不能堵住事件循环） ----
        equity_curve, trades = await asyncio.to_thread(
            self._simulate, params, events, klines
        )

        # ---- 绩效指标 ----
        metrics = await asyncio.to_thread(
            self._compute_metrics, equity_curve, trades, params
        )

        return {
            "strategy": params.strategy,
            "start_date": params.start_date,
            "end_date": params.end_date,
            "params": {
                "initial_capital": params.initial_capital,
                "max_positions": params.max_positions,
                "commission": params.commission,
                "stamp_tax": params.stamp_tax,
                "universe_size": len(pool),
            },
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": trades,
        }

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    async def _default_universe(self, size: int) -> list[str]:
        """缺省股票池：实时快照按成交额取前 N（流动性优先）。"""
        from app.data.snapshot_store import snapshot_store

        snapshot = await asyncio.to_thread(snapshot_store.get_snapshot)
        top = snapshot.nlargest(size, "amount")
        return top["code"].tolist()

    def _resolve_pool(self, codes: list[str]) -> pd.DataFrame:
        """codes → 含名称的 DataFrame。"""
        from app.data.service import get_stock_pool

        pool = get_stock_pool()
        return pool[pool["code"].isin(set(codes))]

    @staticmethod
    def _shift_date(date: str, days: int) -> str:
        """自然日平移（简化：不处理节假日，仅用于截取数据起点）。"""
        return (datetime.strptime(date, "%Y-%m-%d")
                + pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    @staticmethod
    def _first_index_on_or_after(df: pd.DataFrame, date: str) -> int:
        """第一个日期 >= date 的行位置（iloc 位置，无则返回 len(df)）。"""
        mask = (df["date"] >= date).to_numpy()
        if not mask.any():
            return len(df)
        return int(mask.argmax())  # argmax 返回位置而非索引标签

    @staticmethod
    def _collect_events(
        strategy: Strategy,
        df: pd.DataFrame,
        start_idx: int,
        code: str,
        name: str,
    ) -> list[dict]:
        """逐日滚动评估单只股票（CPU 密集，由上层丢进线程池执行）。

        只喂 ``df.iloc[: i + 1]`` 前缀切片，保证指标取值与当日实时值一致。
        """
        out: list[dict] = []
        for i in range(start_idx, len(df)):
            signal = strategy.evaluate(df.iloc[: i + 1])
            if signal is None:
                continue
            out.append({
                "code": code, "name": name,
                "date": signal.date,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "target_price": signal.target_price,
            })
        return out

    # ------------------------------------------------------------------
    # 交易模拟
    # ------------------------------------------------------------------
    def _simulate(
        self,
        params: BacktestParams,
        events: list[dict],
        klines: dict[str, pd.DataFrame],
    ) -> tuple[list[dict], list[dict]]:
        """按日期推进模拟交易，返回 (净值曲线, 交易明细)。

        每日流程:
          1. 用当日收盘价检查持仓是否触发止损/止盈/到期，触发则卖出；
          2. 处理当日到期的买入信号（资金等分、整手交易）；
          3. 记录当日总资产（现金 + 持仓市值）。
        """
        all_dates = sorted({d for df in klines.values() for d in df["date"]})
        events_by_date: dict[str, list[dict]] = {}
        for ev in events:
            events_by_date.setdefault(ev["date"], []).append(ev)

        # 收盘价速查: close[(code, date)]
        close: dict[tuple[str, str], float] = {}
        for code, df in klines.items():
            for r in df.itertuples():
                close[(code, r.date)] = float(r.close)

        cash = params.initial_capital
        positions: dict[str, Position] = {}
        trades: list[Trade] = []
        equity_curve: list[dict] = []
        last_date = all_dates[-1] if all_dates else params.end_date

        for date in all_dates:
            # ---- 1) 卖出检查 ----
            for code in list(positions):
                pos = positions[code]
                price = close.get((code, date))
                if price is None:
                    continue  # 当日停牌无价格，顺延
                reason = None
                if price <= pos.stop_loss:
                    reason = "止损"
                elif price >= pos.target_price:
                    reason = "止盈"
                elif date == last_date:
                    reason = "到期"
                if reason is not None:
                    proceeds = pos.shares * price * (1 - params.stamp_tax
                                                     - params.commission)
                    cash += proceeds
                    pnl = proceeds - pos.shares * pos.cost
                    trades.append(Trade(
                        code=code, name=pos.name,
                        buy_date=pos.buy_date, sell_date=date,
                        buy_price=pos.cost, sell_price=price,
                        shares=pos.shares, pnl=round(pnl, 2),
                        pnl_pct=round(pnl / (pos.shares * pos.cost) * 100, 2),
                        reason=reason,
                    ))
                    del positions[code]

            # ---- 2) 买入检查 ----
            for ev in events_by_date.get(date, []):
                if len(positions) >= params.max_positions:
                    break
                if ev["code"] in positions:
                    continue
                price = close.get((ev["code"], date), ev["price"])
                slots = params.max_positions - len(positions)
                alloc = cash / slots                     # 等分剩余资金
                shares = math.floor(alloc / price / 100) * 100  # 整手
                if shares <= 0:
                    continue
                cost = shares * price * (1 + params.commission)
                if cost > cash:
                    continue
                cash -= cost
                positions[ev["code"]] = Position(
                    code=ev["code"], name=ev.get("name", ""),
                    shares=shares,
                    cost=round(price * (1 + params.commission), 4),
                    stop_loss=ev.get("stop_loss") or price * 0.9,
                    target_price=ev.get("target_price") or price * 1.15,
                    buy_date=date,
                )

            # ---- 3) 记录净值 ----
            market_value = sum(
                p.shares * close.get((code, date), p.cost)
                for code, p in positions.items()
            )
            equity_curve.append({
                "date": date,
                "equity": round(cash + market_value, 2),
            })

        return equity_curve, [t.__dict__ for t in trades]

    # ------------------------------------------------------------------
    # 绩效指标
    # ------------------------------------------------------------------
    def _compute_metrics(
        self,
        equity_curve: list[dict],
        trades: list[dict],
        params: BacktestParams,
    ) -> dict:
        """计算绩效指标。

        公式:
          总收益率   = 期末净值 / 初始资金 - 1
          年化收益率 = (1 + 总收益率) ** (252 / 交易日数) - 1
          最大回撤   = min(净值_t / 历史最高净值 - 1)
          夏普比率   = 日收益率均值 / 日收益率标准差 × sqrt(252)
                      （无风险利率取 0，年化按 252 个交易日）
          胜率       = 盈利交易数 / 总交易数
        """
        if not equity_curve:
            return {}
        equity = np.array([p["equity"] for p in equity_curve], dtype=float)
        initial = params.initial_capital
        n_days = max(len(equity) - 1, 1)

        total_return = equity[-1] / initial - 1
        annualized = (1 + total_return) ** (252 / n_days) - 1

        # 最大回撤
        cummax = np.maximum.accumulate(equity)
        drawdowns = equity / cummax - 1
        max_drawdown = float(drawdowns.min())

        # 夏普（日收益序列）
        daily_returns = np.diff(equity) / equity[:-1]
        if daily_returns.size > 1 and daily_returns.std() > 0:
            sharpe = float(daily_returns.mean() / daily_returns.std()
                           * math.sqrt(252))
        else:
            sharpe = 0.0

        # 胜率
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / len(trades) * 100 if trades else 0.0

        # 全部转成原生 float：np.float64 无法被 json.dumps / 前端 JSON 序列化
        return {
            "total_return": round(float(total_return) * 100, 2),       # %
            "annualized_return": round(float(annualized) * 100, 2),    # %
            "max_drawdown": round(max_drawdown * 100, 2),              # %
            "sharpe": round(float(sharpe), 2),
            "trade_count": len(trades),
            "win_rate": round(float(win_rate), 1),                     # %
            "final_equity": round(float(equity[-1]), 2),
            "trading_days": n_days,
        }


# 全局单例
engine = BacktestEngine()
