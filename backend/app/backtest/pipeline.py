"""全流水线回测：完整复刻线上「扫描 → 加自选 → 14因子排序 → 评分加权下单」。

不单独挑策略，而是用全部已注册策略同时扫描，与线上模拟盘同一条链路。

防“上帝视角”（未来函数）的三条硬约束：
  1) 每日股票池只看**当日成交额**（TopN），不使用任何未来成交数据；
  2) 策略与因子只喂 ``df.iloc[: 当日位置 + 1]`` 的K线切片——指标
     （MA/EMA/rolling/ewm）全部是因果计算，切片后取值与当日实时值一致；
  3) 当日**收盘后**产生的信号，只在**下一个交易日开盘**成交。

执行规则与线上 ``app.paper.engine`` 保持一致：
  - 评分分档：≥ full_score 全额、≥ half_score 半额、低于则只记录不下单；
  - 总仓位 ≤ max_total_exposure，单票 ≤ single_position_ratio，整手（100 股），T+1；
  - 佣金万2.5（最低 5 元）+ 卖出印花税千1 + 双边过户费万0.1；
  - 止损/止盈 = 持仓成本 −stop_loss / +take_profit（盘中触发，同日双触发止损优先）；
  - 满仓轮动：新票 ≥ rotation_min_score 且比最弱持仓建仓分高 ≥ rotation_gap，每日限次；
  - 涨停无法买入、跌停无法卖出（按开盘价近似判断）。

参数覆盖：``PipelineParams`` 里所有风控参数默认 ``None`` = 沿用线上配置，
只有显式传入时才对**本次回测**生效（线上参数不受影响），用于回归测试与参数实验。
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
from app.backtest.review import analyze
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

#: 单组「K线 + 指标 + 每日股票池」缓存。
#: 回归测试里要跑多组参数，数据准备（读 5000+ parquet、算指标）占大头，
#: 只缓存最近一组，避免内存无限增长。
_PREPARED: dict | None = None
_PREPARED_KEY: tuple | None = None


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
    buy_threshold: float | None = None     # 14因子买入阈值，None=沿用 factors.BUY_THRESHOLD
    # ---- E2/E5：买点与筛选过滤（None=不过滤，即线上当前行为） ----
    max_open_gain: float | None = None     # E2 次日开盘涨幅上限，超过则不买（不追高）
    max_gain_5d: float | None = None       # E5 近 5 日涨幅上限，超过则不进买入池（不追已涨）
    min_volume_ratio: float | None = None  # E5 量比下限（当日量 / 前 5 日均量）
    min_turnover_rate: float | None = None # E5 换手率下限（%），过滤无量的僵尸票
    # ---- E9：卖出侧加固（None=关闭，即线上当前的「固定 -8% / +20%」） ----
    breakeven_trigger: float | None = None # 浮盈曾达该比例后，止损上移到成本价（保本止损）
    trail_pct: float | None = None         # 从持仓期最高价回撤该比例即卖出（移动止损）
    trail_arm: float = 0.0                 # 移动止损生效门槛：浮盈曾达该比例后才启用
    regime_ma: int | None = None           # 大盘择时：仅当上证指数 < MA(N) 时才启用上述防守规则
    # ---- E10/E11：分段止盈与止损触发方式（None/False=线上当前行为） ----
    partial_tp_at: float | None = None     # 分段止盈触发涨幅（相对成本），首次达到时卖出一部分
    partial_tp_ratio: float = 0.5          # 分段止盈卖出比例（按整手取整）
    stop_confirm_close: bool = False       # 止损改收盘确认：收盘跌破止损线，次日开盘卖出
    # ---- E13：组合层面择时（大盘走弱时降总仓位，而不是砍个股） ----
    weak_exposure: float | None = None     # 弱市总仓位上限（需同时设置 regime_ma）
    # ---- 以下默认为 None，表示沿用线上配置（回测参数实验用） ----
    max_total_exposure: float | None = None
    single_position_ratio: float | None = None
    full_score: float | None = None
    half_score: float | None = None
    rotation_gap: float | None = None
    rotation_min_score: float | None = None
    # ---- 选股模型实验（None = 线上行为） ----
    factor_weights: dict[str, float] | None = None  # 14 因子权重覆盖，自动归一化到 100
    strategies: tuple[str, ...] | None = None       # 只启用指定策略，None = 全部注册策略


def calc_fee(side: str, amount: float) -> float:
    """A 股单笔费用：佣金（最低5元）+ 卖出印花税 + 双边过户费。"""
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_RATE if side == "sell" else 0.0
    return round(commission + stamp + amount * TRANSFER_RATE, 2)


def _load_klines(end_date: str, start_date: str, kline_dir: str) -> dict[str, pd.DataFrame]:
    """读取本地K线缓存（只保留窗口前 320 根），返回 code -> df。

    跳过 ``idx_*``（指数）缓存文件：指数与个股共用同一目录（见
    ``api/market.py`` 的 ``idx_`` 前缀），但指数成交额是全市场合计，
    混进「按成交额取 TopN」的股票池会直接排在第一位，等于把指数当成一只
    可以买入的股票——必须排除。
    """
    if not os.path.isdir(kline_dir):
        return {}
    data: dict[str, pd.DataFrame] = {}
    for name in os.listdir(kline_dir):
        if not name.endswith(".parquet"):
            continue
        code = name.split(".")[0]
        if not code.isdigit():
            continue  # idx_sh000001 之类：指数，不是可交易标的
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


def _load_index(kline_dir: str) -> pd.DataFrame:
    """读取上证指数日线（用于大盘择时），缺失时返回空表。"""
    path = os.path.join(kline_dir, "idx_sh000001.parquet")
    if not os.path.isfile(path):
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if df is None or df.empty or "close" not in df:
        return pd.DataFrame()
    return df.sort_values("date").reset_index(drop=True)


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


def _prepare(params: PipelineParams) -> dict:
    """准备回测数据：K线、指标、每日股票池、名称与策略实例。"""
    settings = get_settings()
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
    return {
        "dates": dates,
        "universe": universe,
        "klines": klines,
        "names": names,
        "pos_index": pos_index,
        "index": _load_index(str(settings.kline_dir)),
        "strategies": [registry.create(n) for n in registry.names()],
    }


def _pick_strategies(all_strategies: list, wanted: tuple[str, ...] | None) -> list:
    """按参数筛选参与扫描的策略（``None`` = 全部注册策略，即线上行为）。"""
    if not wanted:
        return all_strategies
    known = {s.name for s in all_strategies}
    unknown = [n for n in wanted if n not in known]
    if unknown:
        raise RuntimeError(f"未知策略 {unknown}，可选 {sorted(known)}")
    keep = set(wanted)
    return [s for s in all_strategies if s.name in keep]


def _signals_key(params: PipelineParams) -> tuple:
    """影响「选股结果」的参数指纹：变了就必须重算 signals。

    仓位/止损/阈值这类只影响交易循环的参数不在指纹里，所以普通参数搜索仍然是
    「准备一次、多组复用」；只有动到选股模型（因子权重、策略集）才会重算。
    """
    return (tuple(params.strategies or ()),
            tuple(sorted((params.factor_weights or {}).items())))


def _ensure_signals(prepared: dict, params: PipelineParams) -> dict[str, list[dict]]:
    """取当前参数对应的 signals（指纹变了就重算）。"""
    key = _signals_key(params)
    if prepared.get("signals_key") == key:
        return prepared["signals"]
    prepared["signals"] = _build_signals(prepared, params)
    prepared["signals_key"] = key
    return prepared["signals"]


def _build_signals(prepared: dict, params: PipelineParams) -> dict[str, list[dict]]:
    """预算每日「扫描 → 加自选 → 14因子排序」的结果。

    只依赖 K 线、指标与**选股模型**（策略集、因子权重），不依赖买入阈值/仓位/
    止损等交易参数，所以普通参数搜索时可以只算一次；动到选股模型时由
    ``_ensure_signals`` 按指纹重算。
    返回 ``{date: scored}``，``scored`` 已按 14 因子总分降序排列。
    """
    dates: list[str] = prepared["dates"]
    universe: dict[str, list[str]] = prepared["universe"]
    klines: dict[str, pd.DataFrame] = prepared["klines"]
    names: dict[str, str] = prepared["names"]
    pos_index: dict[str, dict[str, int]] = prepared["pos_index"]
    strategies = _pick_strategies(prepared["strategies"], params.strategies)

    watch: "OrderedDict[str, int]" = OrderedDict()  # code -> 0（FIFO 淘汰）
    signals: dict[str, list[dict]] = {}

    for date in dates:
        todays = universe.get(date, [])
        hits: list[dict] = []
        for code in todays:
            idx = pos_index.get(code, {}).get(date)
            if idx is None:
                continue
            slice_df = klines[code].iloc[: idx + 1]
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
                result = compute_factors(
                    klines[code].iloc[: idx + 1], params.factor_weights
                )
            except Exception:  # noqa: BLE001
                continue
            scored.append({
                "code": code, "name": names.get(code, code),
                "score": float(result.total), "action": result.action,
            })
        scored.sort(key=lambda x: -x["score"])
        signals[date] = scored

    return signals


def _prepare_cached(params: PipelineParams) -> dict:
    """同一区间/股票池只准备一次（回归测试/参数搜索要跑多组参数）。"""
    global _PREPARED, _PREPARED_KEY
    key = (params.start_date, params.end_date, params.universe_size)
    if _PREPARED is not None and _PREPARED_KEY == key:
        logger.info("复用已准备的K线与指标（%s ~ %s，Top%d）", *key)
        return _PREPARED
    prepared = _prepare(params)
    prepared["signals"] = _build_signals(prepared, params)
    prepared["signals_key"] = _signals_key(params)
    _PREPARED = prepared
    _PREPARED_KEY = key
    return _PREPARED


def _simulate(prepared: dict, params: PipelineParams) -> dict:
    """在准备好的数据上跑一遍交易模拟，返回结果 dict。"""
    settings = get_settings()
    dates: list[str] = prepared["dates"]
    universe: dict[str, list[str]] = prepared["universe"]
    klines: dict[str, pd.DataFrame] = prepared["klines"]
    names: dict[str, str] = prepared["names"]
    pos_index: dict[str, dict[str, int]] = prepared["pos_index"]
    signals: dict[str, list[dict]] = _ensure_signals(prepared, params)

    # 大盘择时（regime）：用**前一交易日**上证指数收盘 vs MA(N) 判断强弱。
    # 当日盘中只能看到前一日收盘，所以统一 shift(1)，不留未来函数。
    regime: dict[str, bool] = {}
    if params.regime_ma is not None:
        idx_df = prepared.get("index")
        if idx_df is not None and not idx_df.empty:
            n = max(2, int(params.regime_ma))
            ma = idx_df["close"].rolling(n).mean().shift(1)
            weak = idx_df["close"].shift(1) < ma
            regime = {str(d): bool(w) for d, w in zip(idx_df["date"], weak.fillna(False))}
        else:
            logger.warning("缺少 idx_sh000001 指数K线，regime_ma 失效并退化为「始终防守」")

    initial_capital = float(params.initial_capital or settings.paper_initial_capital)
    max_positions = int(params.max_positions or settings.paper_max_positions)
    exposure = float(
        params.max_total_exposure
        if params.max_total_exposure is not None
        else settings.paper_max_total_exposure
    )
    single_ratio = float(
        params.single_position_ratio
        if params.single_position_ratio is not None
        else 0.20
    )
    full_score = float(
        params.full_score if params.full_score is not None else settings.paper_full_score
    )
    half_score = float(
        params.half_score if params.half_score is not None else settings.paper_half_score
    )
    rotation_gap = float(
        params.rotation_gap
        if params.rotation_gap is not None
        else settings.paper_rotation_gap
    )
    rotation_min_score = float(
        params.rotation_min_score
        if params.rotation_min_score is not None
        else settings.paper_rotation_min_score
    )

    cash = initial_capital
    positions: dict[str, dict] = {}
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

    def pass_filters(code: str, date: str) -> bool:
        """E5：量比 / 换手率 / 近 5 日涨幅过滤（阈值全为 None 时直接放行）。

        数据缺失（无换手率字段、历史不足 6 根）时**放行**而不是剔除，
        避免因为原始数据缺列把股票池整体清空、得出错误的对比结论。
        """
        if (
            params.max_gain_5d is None
            and params.min_volume_ratio is None
            and params.min_turnover_rate is None
        ):
            return True
        idx = pos_index.get(code, {}).get(date)
        if idx is None:
            return False
        df = klines[code]
        row = df.iloc[idx]
        if idx >= 5:
            if params.max_gain_5d is not None:
                prev5 = float(df.iloc[idx - 5]["close"])
                cur = float(row["close"])
                if prev5 > 0 and cur / prev5 - 1 > params.max_gain_5d:
                    return False
            if params.min_volume_ratio is not None:
                vol = float(row["volume"])
                base = df.iloc[idx - 5: idx]["volume"].astype(float).mean()
                if base == base and base > 0 and vol / base < params.min_volume_ratio:
                    return False
        if params.min_turnover_rate is not None and "turnover_rate" in df.columns:
            tr = row["turnover_rate"]
            tr = float(tr) if tr == tr else None
            if tr is not None and tr < params.min_turnover_rate:
                return False
        return True

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

    def sell_part(code: str, date: str, price: float, ratio: float, reason: str) -> None:
        """部分卖出（分段止盈）：按整手取整，剩余不足一手则整笔清掉。"""
        nonlocal cash
        p = positions[code]
        want = int(p["shares"] * ratio / LOT) * LOT
        rest = p["shares"] - want
        if want < LOT or rest < LOT:
            sell(code, date, price, reason)
            return
        amount = want * price
        fee = calc_fee("sell", amount)
        cash += amount - fee
        cost = want * p["cost"]
        pnl = amount - fee - cost
        p["shares"] = rest
        p["partial_done"] = True
        trades.append({
            "code": code, "name": p["name"], "buy_date": p["buy_date"],
            "sell_date": date, "buy_price": round(p["cost"], 3),
            "sell_price": round(price, 3), "shares": want,
            "amount": round(amount, 2), "fee": fee, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 2) if cost else 0.0,
            "reason": reason, "score": p["score"],
        })

    day_no = {d: i for i, d in enumerate(dates)}

    for day_idx, date in enumerate(dates):
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

        # E13：大盘走弱时把总仓位上限压低（组合层面防守，不砍个股）
        day_exposure = exposure
        if params.weak_exposure is not None and params.regime_ma is not None:
            if regime.get(date, True):
                day_exposure = min(exposure, params.weak_exposure)
        total_asset = cash + market_value(date)
        pool_budget = max(0.0, total_asset * day_exposure - market_value(date))
        slots = max_positions - len(positions)
        ranked = sorted(pending_buys, key=lambda b: -b["score"])
        pending_buys = []
        eligible = [b for b in ranked if b["score"] >= half_score][:slots]
        weights = {
            b["code"]: b["score"] * (1.0 if b["score"] >= full_score else 0.5)
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
            # E2：高开太多不追（次日开盘涨幅上限），缺席即沿用线上「照价买入」
            if params.max_open_gain is not None and pc and px / pc - 1 > params.max_open_gain:
                continue
            budget = min(
                pool_budget * weight / weight_total if weight_total else 0,
                total_asset * single_ratio, cash, pool_budget,
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
                # E9：买入日最高价（当日已知，供次日移动止损使用）
                "peak": price_on(code, date, "high") or 0.0,
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
            cost = p["cost"]
            stop_px, stop_reason = p["stop"], "止损"
            # E9 的峰值只用**之前交易日**已知的最高价（买入日算已知），
            # 不用当日 high，否则等于用当天最高价决定当天卖点 = 未来函数。
            peak = float(p.get("peak") or 0.0)
            # 大盘走弱时才防守（regime_ma=None 表示不择时、始终防守）
            defensive = regime.get(date, True) if params.regime_ma is not None else True
            if (
                defensive
                and params.breakeven_trigger is not None
                and peak >= cost * (1 + params.breakeven_trigger)
                and cost > stop_px
            ):
                stop_px, stop_reason = cost, "保本止损"
            if defensive and params.trail_pct is not None and peak >= cost * (1 + params.trail_arm):
                trail_px = peak * (1 - params.trail_pct)
                if trail_px > stop_px:
                    stop_px, stop_reason = trail_px, "移动止损"
            # 止损优先（与线上「同日双触发止损优先」一致）；E11 收盘确认模式下不走盘中止损
            if not params.stop_confirm_close and low <= stop_px:
                sell(code, date, min(open_px, stop_px), stop_reason)
                continue
            # E10 分段止盈：首次触及目标涨幅先卖一部分，剩余仓位继续按 +20% / 移动止损跑
            if (
                params.partial_tp_at is not None
                and not p.get("partial_done")
                and high >= cost * (1 + params.partial_tp_at)
            ):
                trigger = max(open_px, cost * (1 + params.partial_tp_at))
                sell_part(code, date, trigger, params.partial_tp_ratio, "分段止盈")
                if code not in positions:
                    continue
                p = positions[code]
            if high >= p["target"]:
                sell(code, date, max(open_px, p["target"]), "止盈")
            else:
                p["peak"] = max(peak, high)

        # ---------- 3) 收盘后：取预算好的「扫描 → 自选 → 14因子排序」结果 ----------
        # 该步骤只依赖 K 线与指标，与交易参数无关，已在 _build_signals 里算过一次；
        # 参数搜索时同一区间的不同参数组合可复用，交易循环只需毫秒级。
        scored = signals.get(date, [])

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

        # E11 止损收盘确认：收盘跌破止损线才发委托（次日开盘卖），避开盘中插针
        if params.stop_confirm_close:
            for code, p in positions.items():
                if p["buy_date"] >= date:
                    continue
                close_px = price_on(code, date)
                if close_px is not None and close_px <= p["stop"]:
                    pending_sells.setdefault(code, "止损(收盘确认)")

        # 轮动替换：满仓时用显著更高分的新票换掉最弱持仓（每日 1 次）
        buys = [
            s for s in scored
            if s["action"] == "buy"
            and (params.buy_threshold is None or s["score"] >= params.buy_threshold)
            and pass_filters(s["code"], date)
        ]
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
                and best["score"] >= rotation_min_score
                and best["score"] - weak_score >= rotation_gap
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
        # 基准：**前一交易日收盘时确定的**股票池，等权持有到今日收盘的日收益。
        # 当日成交额排名要收盘后才知道，用「当日池」算「当日收益」等于用未来信息
        # 选股（成交额与当日涨跌幅强相关，会把基准抬得虚高），故整体滞后一天。
        bench_codes = universe.get(dates[day_idx - 1], []) if day_idx > 0 else []
        pcts = []
        for code in bench_codes:
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

    result = {
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
            "buy_threshold": params.buy_threshold,
            "max_open_gain": params.max_open_gain,
            "max_gain_5d": params.max_gain_5d,
            "min_volume_ratio": params.min_volume_ratio,
            "min_turnover_rate": params.min_turnover_rate,
            "breakeven_trigger": params.breakeven_trigger,
            "trail_pct": params.trail_pct,
            "trail_arm": params.trail_arm,
            "regime_ma": params.regime_ma,
            "partial_tp_at": params.partial_tp_at,
            "partial_tp_ratio": params.partial_tp_ratio,
            "stop_confirm_close": params.stop_confirm_close,
            "weak_exposure": params.weak_exposure,
            "max_total_exposure": round(exposure, 2),
            "single_position_ratio": round(single_ratio, 2),
            "full_score": full_score,
            "half_score": half_score,
            "rotation_gap": rotation_gap,
            "rotation_min_score": rotation_min_score,
            "strategies": list(params.strategies or registry.names()),
            "factor_weights": dict(params.factor_weights or {}),
        },
        "metrics": metrics,
        "equity_curve": equity_curve,
        "trades": trades,
        "positions": positions,
    }
    # 亏损归因 + 调参建议（每次回测都产出，便于写复盘记录）
    result["review"] = analyze(result)
    return result


def _run_sync(params: PipelineParams) -> dict:
    """同步执行回测（由 run_pipeline 丢进线程池调用）。"""
    return _simulate(_prepare_cached(params), params)


async def run_pipeline(params: PipelineParams) -> dict:
    """异步入口：把同步计算丢进线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(_run_sync, params)
