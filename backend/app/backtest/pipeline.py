"""全流水线回测：完整复刻线上「扫描 → 加自选 → 14因子排序 → 评分加权下单」。

不单独挑策略，而是用全部已注册策略同时扫描，与线上模拟盘同一条链路。

防“上帝视角”（未来函数）的三条硬约束：
  1) 每日股票池只看**当日成交额**（TopN），不使用任何未来成交数据；
  2) 策略与因子只喂 ``df.iloc[: 当日位置 + 1]`` 的K线切片——指标
     （MA/EMA/rolling/ewm）全部是因果计算，切片后取值与当日实时值一致；
  3) 当日**收盘后**产生的信号，只在**下一个交易日开盘**成交。

执行规则与线上 ``app.paper.engine`` 保持一致：
  - 评分分档：≥ full_score 全额、≥ half_score 半额、低于则只记录不下单；
  - 总仓位 ≤ paper_max_total_exposure，单票 ≤20%，整手（100 股），T+1；
  - 佣金万2.5（最低 5 元）+ 卖出印花税千1 + 双边过户费万0.1；
  - 止损/止盈 = 持仓成本 −8% / +20%（盘中触发，同日双触发止损优先）；
  - 满仓轮动：新票 ≥ min_score 且比最弱持仓建仓分高 ≥ gap，每日限次；
  - 涨停无法买入、跌停无法卖出（按开盘价近似判断）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import OrderedDict
from dataclasses import dataclass

import pandas as pd

from app.analysis.factors import compute_factors
from app.analysis.scoring import score_technical
from app.config import get_settings
from app.data.service import get_stock_pool
from app.indicators import add_all_indicators
from app.screener.scanner import rank_hits
from app.strategies import builtin  # noqa: F401  - 触发内置策略注册
from app.strategies import registry

logger = logging.getLogger("sas.backtest.pipeline")

KEEP_BARS = 320            # 每只股票保留的K线根数（足够 70 日 min_bars + 指标预热）
LOT = 100
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_RATE = 0.001
TRANSFER_RATE = 0.00001
STOP_LOSS_RATIO = 0.08     # 与线上 engine 一致：成本价 -8% 止损
TAKE_PROFIT_RATIO = 0.20   # 成本价 +20% 止盈


@dataclass
class PipelineParams:
    """全流水线回测参数（除时间段外均有线上默认值）。"""

    start_date: str
    end_date: str
    initial_capital: float | None = None   # 缺省取 settings.paper_initial_capital
    max_positions: int | None = None       # 缺省取 settings.paper_max_positions
    universe_size: int = 500               # 每日成交额 TopN 股票池
    watch_cap: int = 60                    # 自选股上限（FIFO）
    add_per_day: int = 50                  # 每日扫描后加自选上限
    stop_loss: float = STOP_LOSS_RATIO
    take_profit: float = TAKE_PROFIT_RATIO
    max_hold_days: int = 0                 # 时间止损，0=不限制


def calc_fee(side: str, amount: float) -> float:
    """A 股单笔费用：佣金（最低5元）+ 卖出印花税 + 双边过户费。"""
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_RATE if side == "sell" else 0.0
    return round(commission + stamp + amount * TRANSFER_RATE, 2)


def _load_klines(end_date: str, start_date: str, kline_dir: str) -> dict[str, pd.DataFrame]:
    """读取本地K线缓存（只保留窗口前 320 根），返回 code -> df。"""
    if not os.path.isdir(kline_dir):
        return {}
    data: dict[str, pd.DataFrame] = {}
    for name in os.listdir(kline_dir):
        if not name.endswith(".parquet"):
            continue
        code = name.split(".")[0]
        try:
            df = pd.read_parquet(os.path.join(kline_dir, name))
        except Exception:  # noqa: BLE001 - 坏文件跳过
            continue
        if df is None or df.empty or "date" not in df:
            continue
        df = df[df["date"] <= end_date]
        if len(df) < 120:
            continue
        df = df.tail(KEEP_BARS).reset_index(drop=True)
        if df["date"].iloc[-1] < start_date:
            continue  # 窗口内无数据（长期停牌/退市）
        data[code] = df
    return data


def _build_universe_by_date(
    raw: dict[str, pd.DataFrame], dates: list[str], top_n: int
) -> dict[str, list[str]]:
    """每日股票池：按**当日**成交额取前 N（仅用当日数据，无未来函数）。"""
    amount_by_date: dict[str, list[tuple[str, float]]] = {d: [] for d in dates}
    wanted = set(dates)
    for code, df in raw.items():
        sub = df[df["date"].isin(wanted)]
        for row in sub.itertuples(index=False):
            amount = getattr(row, "amount", None)
            if amount is not None and amount == amount and amount > 0:
                amount_by_date[str(row.date)].append((code, float(amount)))
    return {
        d: [c for c, _ in sorted(rows, key=lambda x: -x[1])[:top_n]]
        for d, rows in amount_by_date.items()
    }


def _run_sync(params: PipelineParams) -> dict:
    """同步执行回测（由 run_pipeline 丢进线程池调用）。"""
    settings = get_settings()
    initial_capital = float(params.initial_capital or settings.paper_initial_capital)
    max_positions = int(params.max_positions or settings.paper_max_positions)

    raw = _load_klines(params.end_date, params.start_date, str(settings.kline_dir))
    if not raw:
        raise RuntimeError("本地K线缓存为空，无法回测（请先预热K线）")
    logger.info("流水线回测读取K线：%d 只", len(raw))

    dates = sorted({d for df in raw.values() for d in df["date"]})
    dates = [d for d in dates if params.start_date <= d <= params.end_date]
    if len(dates) < 2:
        raise RuntimeError("区间内交易日不足，检查开始/结束日期")

    universe = _build_universe_by_date(raw, dates, params.universe_size)
    candidates = sorted({c for codes in universe.values() for c in codes})
    logger.info("流水线回测：%d 个交易日，候选股票 %d 只", len(dates), len(candidates))

    # 只对候选股票计算指标（KDJ 有 Python 循环，全量算太慢）
    klines: dict[str, pd.DataFrame] = {}
    for code in candidates:
        try:
            klines[code] = add_all_indicators(raw[code])
        except Exception:  # noqa: BLE001
            continue

    try:
        pool = get_stock_pool()
        names = dict(zip(pool["code"], pool["name"]))
    except Exception:  # noqa: BLE001
        names = {}

    pos_index = {
        code: {str(d): i for i, d in enumerate(df["date"])}
        for code, df in klines.items()
    }
    strategies = [registry.create(n) for n in registry.names()]

    cash = initial_capital
    positions: dict[str, dict] = {}
    watch: "OrderedDict[str, int]" = OrderedDict()  # code -> 0（FIFO 淘汰）
    trades: list[dict] = []
    equity_curve: list[dict] = []
    pending_buys: list[dict] = []
    pending_sells: dict[str, str] = {}
    bench_value = 1.0

    def price_on(code: str, date: str, field: str = "close") -> float | None:
        idx = pos_index.get(code, {}).get(date)
        if idx is None:
            return None
        value = klines[code].iloc[idx][field]
        return float(value) if value == value and value > 0 else None

    def prev_close(code: str, date: str) -> float | None:
        idx = pos_index.get(code, {}).get(date)
        if idx is None or idx == 0:
            return None
        value = klines[code].iloc[idx - 1]["close"]
        return float(value) if value == value else None

    def market_value(date: str) -> float:
        total = 0.0
        for code, p in positions.items():
            px = price_on(code, date) or p["cost"]
            total += p["shares"] * px
        return total

    def sell(code: str, date: str, price: float, reason: str) -> None:
        nonlocal cash
        p = positions.pop(code)
        amount = p["shares"] * price
        fee = calc_fee("sell", amount)
        cash += amount - fee
        cost = p["shares"] * p["cost"]
        pnl = amount - fee - cost
        trades.append({
            "code": code, "name": p["name"], "buy_date": p["buy_date"],
            "sell_date": date, "buy_price": round(p["cost"], 3),
            "sell_price": round(price, 3), "shares": p["shares"],
            "amount": round(amount, 2), "fee": fee, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 2) if cost else 0.0,
            "reason": reason, "score": p["score"],
        })

    day_no = {d: i for i, d in enumerate(dates)}

    for date in dates:
        rotation_used = 0  # 每日轮动次数上限独立计数

        # ---------- 1) 开盘执行昨日收盘产生的委托 ----------
        for code, reason in list(pending_sells.items()):
            px = price_on(code, date, "open")
            pc = prev_close(code, date)
            if px is None or code not in positions:
                pending_sells.pop(code, None)
                continue
            if pc and px <= pc * 0.905:
                continue  # 跌停卖不出，顺延
            sell(code, date, px, reason)
            pending_sells.pop(code, None)

        total_asset = cash + market_value(date)
        pool_budget = max(
            0.0, total_asset * settings.paper_max_total_exposure - market_value(date)
        )
        slots = max_positions - len(positions)
        ranked = sorted(pending_buys, key=lambda b: -b["score"])
        pending_buys = []
        eligible = [b for b in ranked if b["score"] >= settings.paper_half_score][:slots]
        weights = {
            b["code"]: b["score"] * (1.0 if b["score"] >= settings.paper_full_score else 0.5)
            for b in eligible
        }
        for b in eligible:
            if not weights:
                break
            code = b["code"]
            weight = weights.pop(code)
            weight_total = weight + sum(weights.values())
            if len(positions) >= max_positions or code in positions:
                continue
            px = price_on(code, date, "open")
            pc = prev_close(code, date)
            if px is None or (pc and px >= pc * 1.095):
                continue  # 涨停或停牌买不到
            budget = min(
                pool_budget * weight / weight_total if weight_total else 0,
                total_asset * 0.20, cash, pool_budget,
            )
            shares = int(budget / px / LOT) * LOT
            while shares >= LOT and px * shares + calc_fee("buy", px * shares) > cash:
                shares -= LOT
            if shares < LOT:
                continue
            amount = px * shares
            fee = calc_fee("buy", amount)
            cash -= amount + fee
            pool_budget = max(0.0, pool_budget - amount)
            cost = (amount + fee) / shares
            # 与线上 engine 一致：止损/止盈基于**实际持仓成本**，而非信号日价格
            positions[code] = {
                "name": b.get("name") or names.get(code, code),
                "shares": shares, "cost": cost,
                "buy_date": date, "score": b["score"],
                "stop": cost * (1 - params.stop_loss),
                "target": cost * (1 + params.take_profit),
            }

        # ---------- 2) 盘中止损/止盈（T+1：当日买入不卖） ----------
        for code in list(positions):
            p = positions[code]
            if p["buy_date"] >= date:
                continue
            open_px = price_on(code, date, "open")
            low = price_on(code, date, "low")
            high = price_on(code, date, "high")
            if open_px is None or low is None or high is None:
                continue
            if low <= p["stop"]:
                sell(code, date, min(open_px, p["stop"]), "止损")
            elif high >= p["target"]:
                sell(code, date, max(open_px, p["target"]), "止盈")

        # ---------- 3) 收盘后：扫描 → 自选 → 14因子排序 → 生成次日委托 ----------
        todays = universe.get(date, [])
        hits: list[dict] = []
        for code in todays:
            idx = pos_index.get(code, {}).get(date)
            if idx is None:
                continue
            df = klines[code]
            slice_df = df.iloc[: idx + 1]
            best = None
            for strategy in strategies:
                if len(slice_df) < strategy.min_bars:
                    continue
                sig = strategy.evaluate(slice_df)
                if sig is None:
                    continue
                try:
                    sc = score_technical(slice_df).score
                except Exception:  # noqa: BLE001
                    sc = 0.0
                if best is None or sc > best["extra"]["score"]:
                    best = {
                        "code": code, "name": names.get(code, code),
                        "strategy": sig.strategy,
                        "price": float(sig.price),
                        "extra": {"score": float(sc)},
                        "stop": float(sig.stop_loss or sig.price * 0.92),
                        "target": float(sig.target_price or sig.price * 1.15),
                    }
            if best is not None:
                hits.append(best)

        rank_hits(hits)
        # 每日把评分最高的前 N 只加入自选（去重、FIFO 上限）
        added = 0
        for hit in hits:
            if hit["code"] in watch:
                continue
            watch[hit["code"]] = 0
            added += 1
            if added >= params.add_per_day:
                break
        while len(watch) > params.watch_cap:
            watch.popitem(last=False)

        # 第 2 次排序：自选股按 14 因子总分排名
        scored: list[dict] = []
        for code in list(watch):
            idx = pos_index.get(code, {}).get(date)
            if idx is None:
                continue
            try:
                result = compute_factors(klines[code].iloc[: idx + 1])
            except Exception:  # noqa: BLE001
                continue
            scored.append({
                "code": code, "name": names.get(code, code),
                "score": float(result.total), "action": result.action,
            })
        scored.sort(key=lambda x: -x["score"])

        # 卖出委托（评分 ≤ -15，且非当日买入）
        pending_sells = {}
        for item in scored:
            if item["action"] != "sell":
                continue
            pos = positions.get(item["code"])
            if pos and pos["buy_date"] < date:
                pending_sells[item["code"]] = f"卖出信号(评分{item['score']:.0f})"

        # 时间止损：短线不恋战，持有满 N 个交易日即次日开盘离场
        # （N=1 表示买入当日收盘即发委托、次日开盘卖出，恰好满足 T+1）
        if params.max_hold_days > 0:
            for code, p in positions.items():
                held = day_no[date] - day_no.get(p["buy_date"], day_no[date]) + 1
                if held >= params.max_hold_days:
                    pending_sells.setdefault(code, f"时间止损(持有{held}日)")

        # 轮动替换：满仓时用显著更高分的新票换掉最弱持仓（每日 1 次）
        buys = [s for s in scored if s["action"] == "buy"]
        if (
            settings.paper_rotation_enabled
            and len(positions) >= max_positions
            and buys and rotation_used < settings.paper_rotation_max_per_day
        ):
            best = max(buys, key=lambda x: x["score"])
            weak_code, weak_score = None, 0.0
            for code, p in positions.items():
                if p["buy_date"] >= date:
                    continue
                if weak_code is None or p["score"] < weak_score:
                    weak_code, weak_score = code, p["score"]
            if (
                weak_code
                and best["score"] >= settings.paper_rotation_min_score
                and best["score"] - weak_score >= settings.paper_rotation_gap
            ):
                pending_sells[weak_code] = (
                    f"轮动替换(建仓{weak_score:.0f}→新票{best['score']:.0f})"
                )
                rotation_used += 1

        # 买入委托（次日开盘按分档/加权执行；止损止盈在成交后按成本价生成）
        pending_buys = [
            {"code": b["code"], "name": b["name"], "score": b["score"]}
            for b in buys
        ]

        # ---------- 4) 记录净值与等权基准 ----------
        mv = market_value(date)
        equity_curve.append({
            "date": date, "equity": round(cash + mv, 2),
            "cash": round(cash, 2), "positions": len(positions),
        })
        # 基准：当日股票池等权日收益（用 close/prev_close，不依赖 pct_change 字段）
        pcts = []
        for code in todays:
            idx = pos_index.get(code, {}).get(date)
            if idx is None or idx == 0:
                continue
            try:
                prev = float(klines[code].iloc[idx - 1]["close"])
                cur = float(klines[code].iloc[idx]["close"])
            except (TypeError, ValueError):
                continue
            if prev > 0 and cur > 0:
                pcts.append(cur / prev - 1)
        if pcts:
            bench_value *= 1 + sum(pcts) / len(pcts)

    # ---------- 收尾 ----------
    last = dates[-1]
    equity = [p["equity"] for p in equity_curve]
    final_equity = equity[-1] if equity else initial_capital
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = min(max_dd, e / peak - 1)

    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var > 0:
            sharpe = mean / math.sqrt(var) * math.sqrt(252)

    wins = [t for t in trades if t["pnl"] > 0]
    metrics = {
        "total_return": round((final_equity / initial_capital - 1) * 100, 2),
        "benchmark_return": round((bench_value - 1) * 100, 2),
        "annualized_return": round(
            ((final_equity / initial_capital) ** (252 / max(len(rets), 1)) - 1) * 100, 2
        ),
        "max_drawdown": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2) if trades else 0.0,
        "final_equity": round(final_equity, 2),
        "trading_days": len(dates),
        "initial_capital": round(initial_capital, 2),
        "open_positions": len(positions),
    }

    return {
        "strategy": "全策略流水线",
        "start_date": dates[0],
        "end_date": last,
        "params": {
            "initial_capital": round(initial_capital, 2),
            "max_positions": max_positions,
            "universe_size": params.universe_size,
            "watch_cap": params.watch_cap,
            "add_per_day": params.add_per_day,
            "stop_loss": params.stop_loss,
            "take_profit": params.take_profit,
            "max_hold_days": params.max_hold_days,
            "total_exposure": settings.paper_max_total_exposure,
            "strategies": registry.names(),
        },
        "metrics": metrics,
        "equity_curve": equity_curve,
        "trades": trades,
        "positions": positions,
    }


async def run_pipeline(params: PipelineParams) -> dict:
    """异步入口：把同步计算丢进线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(_run_sync, params)
