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

from app.analysis.factors import BUY_THRESHOLD, SELL_THRESHOLD, compute_factors
from app.analysis.scoring import score_technical
from app.backtest.review import analyze
from app.config import get_settings
from app.data.service import get_stock_pool
from app.data.minute import get_minute_prices  # E-intraday：真实分钟线（可选，失败降级）
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

#: 用户实际可交易范围：沪深主板 + 创业板（与 ``scripts/factor_ic.py`` 同一口径）。
#: 科创板(688)/北交所(920/43/83/87)/B 股不进池——否则结论可能建立在买不到的票上。
MAIN_BOARD_PREFIXES = (
    "600", "601", "603", "605", "000", "001", "002", "003", "300", "301",
)

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
    #: 只在这些代码前缀里选股（None=全市场，即线上行为）。
    #: 过滤发生在**排序取 TopN 之前**，所以池子容量不会因为剔除板块而缩水。
    board_prefixes: tuple[str, ...] | None = None
    watch_cap: int = 60                    # 自选股上限（FIFO）
    add_per_day: int = 50                  # 每日扫描后加自选上限
    stop_loss: float = STOP_LOSS_RATIO
    take_profit: float = TAKE_PROFIT_RATIO
    max_hold_days: int = 0                 # 时间止损，0=不限制
    #: 调仓周期（交易日）：1=每日（线上行为），5≈周频。只在**调仓日收盘后**生成买入委托、
    #: 次日开盘成交。卖出（止损/止盈/信号/择时清仓）不受限制——风控不能因为没到调仓日而延迟。
    #: 目的：记录 #29 显示月换手约 2.2 个资本、摩擦是主要漏损，降频是最直接的减法。
    rebalance_days: int = 1
    buy_threshold: float | None = None     # 14因子买入阈值，None=沿用 factors.BUY_THRESHOLD
    #: 分位数门槛：当日自选池打分排前 X（0~1）才算买入候选。
    #: 设了就**替代**上面两个绝对阈值（action=="buy" 与 buy_threshold）——绝对分在
    #: 权重重标定后不可比（见 BACKTEST_LOG 记录 #24），权重类实验必须用这个口径。
    buy_top_pct: float | None = None
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
    # ---- E33：反转版选股（把 14 因子总分的符号翻转） ----
    #: True 时 score = −score：原来最弱的「空头排列 + 缩量 + 超跌」变成高分买入对象。
    #: 依据 BACKTEST_LOG 记录 #22：9 个量价因子在 1 个月尺度上 IC 全为负。
    invert_score: bool = False
    # ---- E32：大盘分档仓位（快慢两条均线把「强弱」拆成 4 档） ----
    regime_fast_ma: int = 20               # 快速均线周期（与 regime_ma 组成快慢双线）
    #: 四档总仓位上限，按 state 索引：(0=双线之下, 1=破慢线站上快线, 2=破快线站上慢线, 3=双线之上)
    regime_tiers: tuple[float, float, float, float] | None = None
    regime_flat_below: bool = False        # 最弱档(0)是否清仓：次日开盘卖出全部持仓
    # ---- E30：波动率自适应止损（None=关闭，用固定 stop_loss） ----
    # 机制参数：止损距离 = N × ATR%（买入日前一日），比单点百分比更可迁移。
    stop_atr_mult: float | None = None     # ATR 倍数；None 时用 stop_loss
    stop_atr_min: float = 0.03             # 止损距离下限（相对成本），防止 ATR 过小时过度交易
    stop_atr_max: float = 0.15             # 止损距离上限，防止 ATR 过大时止损形同虚设
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
    # ---- E39：成交摩擦（0 = 线上当前假设：按开盘价/触发价原价成交） ----
    #: 单边滑点：买入价 ×(1+s)、卖出价 ×(1−s)。涨跌停判定仍用原始价格（交易所规则），
    #: 只有「我们的成交价」变差，用于检验正超额是否只是摩擦假设的产物。
    slippage: float = 0.0
    # ---- E-intraday：日内过程建模 ----
    #: False（默认）：用当日 open/low/high/close 边界构造日内路径，已能捕捉盘中触及
    #:（日K 的 low/high 含当日最低/最高，含插针）。True：对持仓日按需拉取真实 5 分钟线，
    #: 按时间顺序检测止损/止盈首次触发，成交价取精确分钟价（带滑点）。
    intraday_minutes: bool = False
    #: 满仓轮动开关（None = 沿用 settings.paper_rotation_enabled）。
    #: 轮动是**强制卖出**，也是换手与摩擦的最大来源（记录 #29：月换手约 2.2 个资本）。
    rotation_enabled: bool | None = None
    # ---- 做T引擎（参数化主动交易；全部 None = 关闭，沿用被动持有） ----
    #: 启用后，对已有持仓在「次日开盘→收盘」做日内做T / 分级网格（遵循 A 股 T+1：
    #: 做T 只动用**底仓**做「先卖后买」的倒T，下跌加仓为隔夜行为，反弹减仓在之后交易日卖出）。
    t_active: bool | None = None                       # 启用做T/分级网格
    t_size: float | None = None                        # 单次做T比例（相对总仓，0.25=1/4）
    t_up_sell: float | None = None                    # 开盘涨幅超此比例先卖 t_size 锁利
    t_up_buyback: float | None = None                 # 从高点回调超此比例买回 t_size（恢复底仓）
    t_down_add: float | None = None                   # 开盘跌幅达此用现金加 t_size 仓（隔夜）
    t_down_full: float | None = None                  # 跌幅达此加满仓（隔夜）
    t_rebound_sell: float | None = None               # 反弹至跌幅小于此比例减 t_size 仓
    t_limit_sell_half: bool | None = None             # 涨停且封板弱则卖 1/2（底仓，T+1 可卖）
    t_crash_guard: float | None = None                # 大盘当日跌幅超此比例暂停做T（防极速下跌）


def calc_fee(side: str, amount: float) -> float:
    """A 股单笔费用：佣金（最低5元）+ 卖出印花税 + 双边过户费。"""
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_RATE if side == "sell" else 0.0
    return round(commission + stamp + amount * TRANSFER_RATE, 2)


def _intraday_first_trigger(series, stop_px, tp_px, partial_px):
    """在按时间升序的日内价格序列上，找止损/止盈/分段止盈的首次触发。

    返回 ``(type, price)``，``type`` 为 ``"stop"``/``"tp"``/``"partial"``/``None``，
    ``price`` 为首次触发的那一笔价格（用于真实分钟线成交价；日K 边界模式会忽略它，
    改用 min(开盘,止损)/max(开盘,目标) 以与历史行为完全一致）。

    ``stop_px`` 为 ``None`` 表示跳过止损检测（如 E11 收盘确认模式）。
    同日双触发时取**最早出现**者；止损与止盈同级时因序列顺序（low 在 high 前）
    自然实现「止损优先」，与线上 ``pipeline`` 原逻辑一致。
    """
    si = ti = pi = None
    sp = tp = pp = None
    for i, px in enumerate(series):
        if si is None and stop_px is not None and px <= stop_px:
            si, sp = i, px
        if ti is None and tp_px is not None and px >= tp_px:
            ti, tp = i, px
        if pi is None and partial_px is not None and px >= partial_px:
            pi, pp = i, px
        if si is not None and ti is not None and pi is not None:
            break
    cands = {}
    if si is not None:
        cands["stop"] = (si, sp)
    if ti is not None:
        cands["tp"] = (ti, tp)
    if pi is not None:
        cands["partial"] = (pi, pp)
    if not cands:
        return (None, None)
    best = min(cands, key=lambda k: cands[k][0])
    return (best, cands[best][1])


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
    raw: dict[str, pd.DataFrame],
    dates: list[str],
    top_n: int,
    board_prefixes: tuple[str, ...] | None = None,
) -> dict[str, list[str]]:
    """每日股票池：按**当日**成交额取前 N（仅用当日数据，无未来函数）。

    ``board_prefixes`` 非空时先按代码前缀排除板块，再排名取 TopN
    （先剔除再排名，池子容量不变；先排名再剔除会让池子缩水）。
    """
    amount_by_date: dict[str, list[tuple[str, float]]] = {d: [] for d in dates}
    wanted = set(dates)
    prefixes = tuple(board_prefixes) if board_prefixes else None
    for code, df in raw.items():
        if prefixes is not None and not code.startswith(prefixes):
            continue
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

    universe = _build_universe_by_date(
        raw, dates, params.universe_size, params.board_prefixes
    )
    candidates = sorted({c for codes in universe.values() for c in codes})
    logger.info("流水线回测：%d 个交易日，候选股票 %d 只", len(dates), len(candidates))
    try:
        from app.data.scope import get_universe_stats
        _s = get_universe_stats()
        logger.info("可交易范围口径（沪深主板+创业板/剔ST/巨无霸）：%d 只（主板%d/创业板%d）；"
                    "实际候选受 board_prefixes/universe_size 约束",
                    _s["in_scope"], _s["mainboard"], _s["cyb"])
    except Exception:  # noqa: BLE001 - 规模日志不影响回测
        pass

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


def _top_pct(items: list[dict], pct: float) -> list[dict]:
    """按打分取前 ``pct`` 比例（至少 1 只；``items`` 需已按分数降序）。

    分位数门槛的意义：打分权重一变，绝对分（±15）的分布就变，跨方案不可比；
    按「当日自选池内部相对排名」选股则与分数分布无关。
    """
    if not items:
        return []
    ratio = min(1.0, max(0.0, float(pct)))
    return items[: max(1, math.ceil(len(items) * ratio))]


def _signals_key(params: PipelineParams) -> tuple:
    """影响「选股结果」的参数指纹：变了就必须重算 signals。

    仓位/止损/阈值这类只影响交易循环的参数不在指纹里，所以普通参数搜索仍然是
    「准备一次、多组复用」；只有动到选股模型（因子权重、策略集）才会重算。
    """
    return (tuple(params.strategies or ()),
            tuple(sorted((params.factor_weights or {}).items())),
            bool(params.invert_score))


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
            sc: float | None = None   # 分数与策略无关，同一 (code, date) 只算一次
            for strategy in strategies:
                if len(slice_df) < strategy.min_bars:
                    continue
                sig = strategy.evaluate(slice_df)
                if sig is None:
                    continue
                if sc is None:
                    try:
                        sc = float(score_technical(slice_df).score)
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
            total = float(result.total)
            if params.invert_score:
                # E33 反转版：翻转总分后重新判级（buy 变成「原来最弱的那批」）
                total = -total
                action = (
                    "buy" if total >= BUY_THRESHOLD
                    else ("sell" if total <= SELL_THRESHOLD else "hold")
                )
            else:
                action = result.action
            scored.append({
                "code": code, "name": names.get(code, code),
                "score": total, "action": action,
            })
        scored.sort(key=lambda x: -x["score"])
        signals[date] = scored

    return signals


def _prepare_cached(params: PipelineParams) -> dict:
    """同一区间/股票池只准备一次（回归测试/参数搜索要跑多组参数）。"""
    global _PREPARED, _PREPARED_KEY
    key = (
        params.start_date, params.end_date, params.universe_size,
        tuple(params.board_prefixes or ()),
    )
    if _PREPARED is not None and _PREPARED_KEY == key:
        logger.info("复用已准备的K线与指标（%s ~ %s，Top%d）", *key[:3])
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
    slippage = max(0.0, float(params.slippage or 0.0))   # E39：单边成交摩擦
    rotation_on = (
        settings.paper_rotation_enabled
        if params.rotation_enabled is None
        else bool(params.rotation_enabled)
    )

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

    # 大盘极速下跌护栏（做T引擎用）：指数收盘序列按交易日对齐（缺失 ffill）
    idx_close: pd.Series = pd.Series(dtype=float)
    _idx_df = prepared.get("index")
    if _idx_df is not None and not _idx_df.empty and "close" in _idx_df.columns and "date" in _idx_df.columns:
        idx_close = _idx_df.set_index("date")["close"].reindex(dates).ffill()

    # E32 大盘分档：0=双均线之下（最弱）1=仅破快速线 2=仅破慢速线 3=双均线之上
    # 同样只用前一交易日收盘，与上面的二值 regime 同一口径，不留未来函数。
    tiers_state: dict[str, int] = {}
    if params.regime_tiers is not None or params.regime_flat_below:
        idx_df = prepared.get("index")
        if idx_df is not None and not idx_df.empty:
            slow_n = max(2, int(params.regime_ma or 60))
            fast_n = max(2, int(params.regime_fast_ma))
            close_prev = idx_df["close"].shift(1)
            slow = idx_df["close"].rolling(slow_n).mean().shift(1)
            fast = idx_df["close"].rolling(fast_n).mean().shift(1)
            for d, c, f, s in zip(idx_df["date"], close_prev, fast, slow):
                if c != c or f != f or s != s:
                    tiers_state[str(d)] = 3      # 预热期按满仓处理
                elif c >= s and c >= f:
                    tiers_state[str(d)] = 3
                elif c >= s:
                    tiers_state[str(d)] = 2      # 仍在慢线上方，只是短期走弱
                elif c >= f:
                    tiers_state[str(d)] = 1      # 破慢线但站上快线（反弹）
                else:
                    tiers_state[str(d)] = 0      # 双线之下
        else:
            logger.warning("缺少 idx_sh000001 指数K线，regime_tiers 失效并按满仓处理")

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

    def stop_distance(code: str, date: str) -> float:
        """E30：买入时的止损距离。``stop_atr_mult`` 为 None 时即固定 ``stop_loss``。

        ATR 取**前一日**的值：买入按当日开盘价成交，当日 high/low 还不可知，
        用当日 ATR 等于提前知道当天振幅（未来函数）。缺失时回落到固定止损。
        """
        if params.stop_atr_mult is None:
            return params.stop_loss
        idx = pos_index.get(code, {}).get(date)
        dist: float | None = None
        if idx is not None and idx > 0:
            row = klines[code].iloc[idx - 1]
            atr, close = row.get("atr"), row.get("close")
            if atr == atr and close == close and atr and close > 0:
                dist = float(params.stop_atr_mult) * float(atr) / float(close)
        if dist is None:
            dist = params.stop_loss
        return max(float(params.stop_atr_min), min(float(params.stop_atr_max), dist))

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

    def market_value(date: str, field: str = "close") -> float:
        """持仓市值。``field="open"`` 用于开盘下单时的资金测算：
        当日收盘价要到收盘才知道，开盘按收盘价算仓位=未来函数。"""
        total = 0.0
        for code, p in positions.items():
            px = price_on(code, date, field) or p["cost"]
            total += p["shares"] * px
        return total

    def sell(code: str, date: str, price: float, reason: str) -> None:
        nonlocal cash
        p = positions.pop(code)
        price = price * (1 - slippage)   # E39：卖出按不利方向滑点成交
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
        price = price * (1 - slippage)   # E39：部分卖出同样按不利方向成交
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

    # ---- 做T引擎：次日开盘→收盘做T / 分级网格（遵循 A 股 T+1） ----
    def _t_buy(code, date, price, shares):
        """做T加仓 / 买回：买指定股数（受现金限制），加到期初持仓（隔夜，T+1 可卖）。"""
        nonlocal cash
        shares = int(shares / LOT) * LOT
        if shares < LOT or code not in positions:
            return
        p = positions[code]
        px = price * (1 + slippage)
        amount = px * shares
        fee = calc_fee("buy", amount)
        while shares >= LOT and amount + fee > cash:
            shares -= LOT
            amount = px * shares
            fee = calc_fee("buy", amount)
        if shares < LOT:
            return
        cash -= amount + fee
        cost = (amount + fee) / shares
        old = p["shares"]
        new = old + shares
        p["cost"] = (p["cost"] * old + cost * shares) / new
        p["shares"] = new
        p["t_added_shares"] = (p.get("t_added_shares") or 0) + shares
        p["peak"] = max(float(p.get("peak") or 0.0), px)

    def _t_sell(code, date, price, ratio, reason):
        """做T减仓：按当前持仓比例卖出；记录买回股数，次日开盘恢复底仓。"""
        p = positions[code]
        s = p["shares"]
        want = int(s * ratio / LOT) * LOT
        if want < LOT or (s - want) < LOT:
            want = s
        sell_part(code, date, price, (want / s) if s else 1.0, reason)
        p = positions.get(code)
        if p is not None:
            p["t_buyback_shares"] = (p.get("t_buyback_shares") or 0) + want

    def _t_engine(code, date, day_idx, open_px, low, high, close_px, pc):
        """做T核心：涨停卖半 / 上涨倒T / 下跌分级加仓 / 反弹减仓（T+1）。"""
        p = positions[code]
        if p.get("t_half") or not params.t_active:
            return
        size = float(params.t_size or 0.25)
        open_chg = (open_px / pc - 1) if pc and pc > 0 else 0.0
        # 大盘极速下跌护栏：指数当日跌幅超阈值则暂停做T
        if params.t_crash_guard is not None:
            ic = idx_close.get(date)
            ip = idx_close.get(dates[day_idx - 1]) if day_idx > 0 else None
            if (
                ic is not None and not pd.isna(ic)
                and ip is not None and not pd.isna(ip) and ip > 0
                and (float(ic) / float(ip) - 1) <= -float(params.t_crash_guard)
            ):
                return
        # 涨停且封板弱 → 卖 1/2（底仓可卖），回调买回（次日开盘）
        if params.t_limit_sell_half and high is not None and pc and high >= pc * 1.095:
            _t_sell(code, date, close_px, 0.5, "涨停卖半")
            p = positions.get(code)
            if p is not None:
                p["t_half"] = True
            return
        # 上涨倒T：开盘涨且盘中触 +t_up_sell → 卖 t_size 锁利，次日开盘买回
        if params.t_up_sell and open_chg > 0 and high is not None and high >= open_px * (1 + params.t_up_sell):
            _t_sell(code, date, open_px * (1 + params.t_up_sell), size, "做T卖")
            return
        # 下跌分级加仓（隔夜）
        if open_chg < 0:
            if params.t_down_add and low is not None and low <= open_px * (1 - params.t_down_add):
                _t_buy(code, date, open_px * (1 - params.t_down_add), cash * float(params.t_down_add))
            if params.t_down_full and low is not None and low <= open_px * (1 - params.t_down_full):
                _t_buy(code, date, open_px * (1 - params.t_down_full), cash * 0.95)
            # 反弹减仓：之前加仓且反弹至 -t_rebound_sell（之后交易日可卖，T+1 OK）
            if params.t_rebound_sell and p.get("t_added_shares") and close_px is not None and pc and close_px >= pc * (1 - params.t_rebound_sell):
                added = p["t_added_shares"]
                ratio = added / p["shares"] if p["shares"] else 1.0
                sell_part(code, date, close_px, min(1.0, ratio), "反弹减仓")
                pp = positions.get(code)
                if pp is not None:
                    pp["t_added_shares"] = 0

    day_no = {d: i for i, d in enumerate(dates)}

    rebalance_every = max(1, int(params.rebalance_days or 1))

    for day_idx, date in enumerate(dates):
        rotation_used = 0  # 每日轮动次数上限独立计数
        rebalance = day_idx % rebalance_every == 0  # 交易日序号对齐，避免周末/停牌导致漂移

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
        # E32：分档优先——按快慢均线给出 4 档总仓位上限
        day_exposure = exposure
        if params.regime_tiers is not None:
            state = tiers_state.get(date, 3)
            day_exposure = exposure * float(params.regime_tiers[min(max(state, 0), 3)])
        elif params.weak_exposure is not None and params.regime_ma is not None:
            if regime.get(date, True):
                day_exposure = min(exposure, params.weak_exposure)
        # 开盘下单只能用开盘价估值：用当日收盘价算总资产/仓位上限等于提前知道收盘
        mv_open = market_value(date, "open")
        total_asset = cash + mv_open
        pool_budget = max(0.0, total_asset * day_exposure - mv_open)
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
                continue  # 涨停或停牌买不到（判定用原始开盘价，滑点不影响能否成交）
            # E2：高开太多不追（次日开盘涨幅上限），缺席即沿用线上「照价买入」
            if params.max_open_gain is not None and pc and px / pc - 1 > params.max_open_gain:
                continue
            px = px * (1 + slippage)   # E39：买入按不利方向滑点成交（股数按滑点后价格算）
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
                "stop": cost * (1 - stop_distance(code, date)),
                "target": cost * (1 + params.take_profit),
                # E9：买入日最高价（当日已知，供次日移动止损使用）
                "peak": price_on(code, date, "high") or 0.0,
            }

        # 做T买回：涨停卖半 / 上涨倒T 在「次日开盘」恢复底仓（T+1：当日卖、次日买回）
        for code in list(positions):
            rb = positions[code].get("t_buyback_shares") or 0
            if rb >= LOT:
                px = price_on(code, date, "open")
                if px is not None:
                    _t_buy(code, date, px, rb)
                positions[code]["t_buyback_shares"] = 0

        # ---------- 2) 盘中止损/止盈（T+1：当日买入不卖） ----------
        # 日内序列：默认日K 边界（open→low→high→close，止损优先）；
        # 开启 intraday_minutes 且取到真实分钟线时改用分钟序列（按时间顺序，成交价取分钟价）。
        def intraday_series(code, date, open_px, low, high, close_px):
            if params.intraday_minutes:
                ms = get_minute_prices(code, date, "5")
                if ms:
                    return ms, True
            seq = [x for x in (open_px, low, high, close_px) if x is not None]
            return seq, False

        for code in list(positions):
            p = positions[code]
            if p["buy_date"] >= date:
                continue
            open_px = price_on(code, date, "open")
            low = price_on(code, date, "low")
            high = price_on(code, date, "high")
            close_px = price_on(code, date, "close")
            if open_px is None or low is None or high is None:
                continue
            # 做T引擎：次日开盘→收盘做T / 分级网格（遵循 A 股 T+1）
            _t_engine(code, date, day_idx, open_px, low, high, close_px, prev_close(code, date))
            if code not in positions:
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
            # 收盘确认模式下止损不走盘中止损（留到步骤 3 收盘判定）
            stop_check = stop_px if (defensive and not params.stop_confirm_close) else None
            series, real = intraday_series(code, date, open_px, low, high, close_px)
            tp_px = p["target"]
            partial_px = (
                cost * (1 + params.partial_tp_at)
                if (params.partial_tp_at is not None and not p.get("partial_done"))
                else None
            )

            def _hit(cond):
                # real=True 时返回首个触发价；real=False（日K 边界）时返回 True/False
                for px in series:
                    if cond(px):
                        return px if real else True
                return None if real else False

            stop_hit = _hit(lambda px: stop_check is not None and px <= stop_check)
            partial_hit = _hit(lambda px: partial_px is not None and px >= partial_px)
            tp_hit = _hit(lambda px: px >= tp_px)
            if stop_hit:
                # 边界模式沿用历史成交价 min(开盘, 止损)；真实分钟线用触发分钟价（sell 内已含滑点）
                fill = stop_hit if real else min(open_px, stop_px)
                sell(code, date, fill, stop_reason)
                continue
            if partial_hit:
                fill = partial_hit if real else max(open_px, partial_px)
                sell_part(code, date, fill, params.partial_tp_ratio, "分段止盈")
                if code not in positions:
                    continue
                p = positions[code]
            if tp_hit:
                fill = tp_hit if real else max(open_px, tp_px)
                sell(code, date, fill, "止盈")
            else:
                p["peak"] = max(peak, high)

        # ---------- 3) 收盘后：取预算好的「扫描 → 自选 → 14因子排序」结果 ----------
        # 该步骤只依赖 K 线与指标，与交易参数无关，已在 _build_signals 里算过一次；
        # 参数搜索时同一区间的不同参数组合可复用，交易循环只需毫秒级。
        scored = signals.get(date, [])

        # 卖出委托（评分 ≤ -15，且非当日买入）
        # 注意：不能在这里 `pending_sells = {}`——开盘因跌停没卖掉的委托要顺延到
        # 下一个交易日，清空会让它永远卖不出去（只剩已成交的被 pop 掉）。
        for item in scored:
            if item["action"] != "sell":
                continue
            pos = positions.get(item["code"])
            if pos and pos["buy_date"] < date:
                pending_sells[item["code"]] = f"卖出信号(评分{item['score']:.0f})"

        # E32：最弱档清仓——收盘确认大盘双线之下，次一交易日开盘卖出全部持仓。
        # 只用前一交易日指数收盘判定（tiers_state 已 shift(1)），不留未来函数；
        # 当日买入的仓位受 T+1 限制不动。
        if params.regime_flat_below and tiers_state.get(date, 3) == 0:
            for code, p in positions.items():
                if p["buy_date"] < date:
                    pending_sells.setdefault(code, "大盘极弱清仓")

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

        # 买入池：分位数门槛优先（权重可比），否则按绝对分档（线上行为）
        if not rebalance:
            buys = []   # 非调仓日不生成买入委托（卖出/风控照常）
        elif params.buy_top_pct is not None:
            buys = [
                s for s in _top_pct(scored, params.buy_top_pct)
                if pass_filters(s["code"], date)
            ]
        else:
            buys = [
                s for s in scored
                if s["action"] == "buy"
                and (params.buy_threshold is None or s["score"] >= params.buy_threshold)
                and pass_filters(s["code"], date)
            ]
        # 轮动替换：满仓时用显著更高分的新票换掉最弱持仓（每日 1 次）
        if (
            rotation_on
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
        # 基准净值与策略净值同轴（均按 initial_capital 起算），前端可叠加对比
        mv = market_value(date)
        equity_curve.append({
            "date": date, "equity": round(cash + mv, 2),
            "benchmark": round(bench_value * initial_capital, 2),
            "cash": round(cash, 2), "positions": len(positions),
        })

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
            "board_prefixes": list(params.board_prefixes) if params.board_prefixes else None,
            "watch_cap": params.watch_cap,
            "add_per_day": params.add_per_day,
            "stop_loss": params.stop_loss,
            "take_profit": params.take_profit,
            "max_hold_days": params.max_hold_days,
            "buy_threshold": params.buy_threshold,
            "buy_top_pct": params.buy_top_pct,
            "slippage": params.slippage,
            "rotation_enabled": rotation_on,
            "intraday_minutes": params.intraday_minutes,
            "rebalance_days": rebalance_every,
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
            "regime_fast_ma": params.regime_fast_ma,
            "regime_tiers": list(params.regime_tiers) if params.regime_tiers else None,
            "regime_flat_below": params.regime_flat_below,
            "stop_atr_mult": params.stop_atr_mult,
            "stop_atr_min": params.stop_atr_min,
            "stop_atr_max": params.stop_atr_max,
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
