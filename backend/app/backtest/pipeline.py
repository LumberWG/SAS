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
import random
from collections import OrderedDict
from dataclasses import dataclass

import pandas as pd

from app.analysis.factors import BUY_THRESHOLD, SELL_THRESHOLD, compute_factors, thresholds
from app.analysis.scoring import score_technical
from app.backtest.review import analyze
from app.config import get_settings
from app.data.service import get_stock_pool
from app.data.minute import get_minute_day_bars  # E45：分钟级强势确认（需当日 OHLCV）
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

#: 原始日线列（不含指标）。全时段买入要「截断当日 bar 后重算指标」，
#: 历史部分必须只取原始列——klines 里预计算的 ma/atr/rsi 等是用整表
#: （含当日及之后数据）算的，直接拼进去等于把未来信息带进盘中。
_RAW_COLS = (
    "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate",
)

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
    # ---- E49：组合级回撤断路器（记录 #69）----
    #: 组合权益相对自身历史峰值的回撤达到该比例时，**暂停新开仓** N 个交易日（仓位与卖出不受影响）。
    #: None = 关闭（默认）。与 #59 的弱市择时的区别：那个看指数，这个看**策略自己的净值**。
    #: 目的：不提升中位，只压尾部（#61 §7 点名的「组合级回撤断路器」是剩下未测的形态方向）。
    dd_pause_at: float | None = None
    dd_pause_days: int = 10                # 触发后暂停买入的交易日数
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
    # ---- E44：弱转强爬坡（ramp） ----
    #: 弱市转强后，总仓位上限在 D 个交易日内从 ``weak_exposure`` 线性爬回 ``max_total_exposure``。
    #: 0=关闭（转强当天直接满仓，即 #59/#60 的行为）；D=1 与关闭等价（自检用）。
    #: 动机：MA60 判定天然滞后，转强当天全部候选会挤在一天成交（#59 H 段 11 笔、回撤 −16.48%），
    #: 用「摊成本」替代「猜时点」。只用前一交易日收盘推状态，不留未来函数。
    regime_ramp_days: int = 0
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
    # ---- E46：做T 的分钟路径口径 ----
    #: True 时做T 改用**当日 5 分钟序列逐点推进**（与线上 ``t_tick`` 同序），
    #: 无本地分钟数据的票当天不做T（不回落日K，保证可复现）。
    #: False（默认）= 旧的日K四价近似：倒T 成交价取 ``开盘×(1+t_up_sell)``、买回与减仓按**次日开盘**，
    #: 无法区分「先高后低」与「先低后高」，对做T这种路径敏感策略系统性乐观。
    t_minute_path: bool = False
    #: 做T 路径用的分钟周期（"5" 或 "1"）。1 分钟线每天 240 个价格点、5 分钟 48 个，
    #: 用于「采样频率敏感度」检验：若 5m 与 1m 结论差很多，说明做T 的收益主要来自采样精度，
    #: 线上 5 秒 tick 会给出第三种答案，历史回测不足以定论（记录 #66）。
    t_minute_period: str = "5"
    # ---- E47：开盘段成交价口径 ----
    #: "open"（默认，历史口径：按开盘价成交，再乘 slippage）
    #: "vwap1m"：按开盘后 ``exec_fill_minutes`` 分钟的 1m VWAP 成交（更贴近线上开盘窗口的真实成交价）。
    #: 只影响「以开盘价成交」的动作（开盘段买入、开盘跌破止损/涨破止盈的成交价），
    #: 盘中触发价（止损/止盈/做T）不变。无本地 1m 数据的票回落开盘价。
    exec_fill: str = "open"
    exec_fill_minutes: int = 30
    # ---- E48：成交价随机扰动（稳健性检验，记录 #67 §8）----
    #: 每笔成交价乘以 (1 + ε)，ε ~ N(0, σ)，σ = fill_noise_bps / 10000。0 = 关闭（默认）。
    #: 目的：判断「超额」是否只是执行价的抽样噪声——若 ±10bp 扰动下中位超额跨 0，
    #: 则任何 <2.5pp 的结论都不构成 alpha 证据。
    fill_noise_bps: float = 0.0
    #: 扰动随机种子（同种子结果完全可复现）。
    fill_noise_seed: int = 0
    # ---- 五段式③④⑤（None = 不启用，与历史回测记录保持可比） ----
    #: ③ 交易队列容量：每日最多生成多少条买入委托（按评分降序截断，模拟线上 topM）。
    queue_top_m: int | None = None
    #: ⑤ 不追高：次日开盘涨幅上限（相对信号日收盘价）。与 ``max_open_gain`` 同口径，
    #: 区别是这里属于「切入点」参数组，便于一次性打开五段式执行规则做统一对照。
    entry_max_chase: float | None = None
    #: ⑤ 回踩：要求次日开盘价 ≤ 信号日收盘 × (1 − entry_pullback)，即**低开才买**。
    #: 0/None = 不要求回踩（照价买，线上旧行为）。
    entry_pullback: float | None = None
    #: ⑤ 分钟级切入点窗口（分钟）：需同时开 ``intraday_minutes``。设了以后买入不再用开盘价
    #: 一次性成交，而是扫描开盘后前 N 分钟的 5 分钟收盘价：
    #: ``entry_pullback>0`` 时要求**窗口内曾回踩**到 ``信号价×(1-pullback)``，以首次触及价成交；
    #: 未触及则放弃（当天不买）。线上对应「tick 累积最低价、触及回踩线后放开买入」。
    entry_window_minutes: int | None = None
    #: ⑤ 分钟级**强势确认**（与 ``entry_window_minutes`` 的「等回踩」方向相反）：
    #: 需同时开 ``intraday_minutes``。开盘后只观察前 N 分钟，满足 ``entry_confirm_mode``
    #: 的条件才以**观察窗最后一根的收盘价**成交，不满足则当天不买。
    #: 记录 #54 已证「等回踩」是逆向选择（等到回踩的票次日继续走弱），这里测相反假设：
    #: 买的是「开盘后 N 分钟站住了」的强势票，代价是成交价从开盘价挪到 N 分钟后。
    entry_confirm_minutes: int | None = None
    #: 强势确认口径：``"hold_open"`` = 窗口内 5 分钟收盘**从未跌破当日开盘价**；
    #: ``"above_avg"`` = 窗口末收盘 **≥ 当日累计成交均价**（分时均价，真 VWAP）；
    #: ``"both"`` = 两条同时满足。``None`` = 不启用。
    entry_confirm_mode: str | None = None
    # ---- E-intraday-entry：全时段买入（把「只在开盘成交」放开到盘中多时点） ----
    #: True = 除开盘一次性成交外，还在 ``intraday_entry_times`` 各时点用**当日截至
    #: 该时点**的 5 分钟线把日线截断后重算 14 因子：仍判 ``buy`` 就以该时点分钟价成交。
    #: 这复刻实盘「盘中分析（每分钟）= 用当时实时价重算指标 → 立刻成交」的链路，
    #: 与「前一日收盘信号延后到盘中成交」是两件不同的事（后者不含盘中新信息）。
    #: 候选只取**前一交易日收盘已在自选池且判 buy** 的票，故无未来函数。
    intraday_entry: bool = False
    #: 全时段买入的尝试时点（``HH:MM``，升序）。午休不产生 bar，内部按交易分钟折算。
    #: 需要本地 5 分钟线（``data/minute5_fsdb``）；缺数据的时点直接跳过，不猜。
    intraday_entry_times: tuple[str, ...] = (
        "09:45", "10:30", "11:15", "13:30", "14:30",
    )
    #: 每个时点最多重算多少只候选（按昨收评分降序截断）。重算含
    #: ``add_all_indicators`` + ``compute_factors``，是纯 CPU 成本，用它能控住回测时长。
    intraday_entry_top_n: int = 10
    #: 开盘成交窗口（分钟，0=只用开盘价，即历史口径）：开盘一字涨停时，
    #: 只要窗口内某根 5 分钟 bar 打开过就以它的收盘价成交。
    #: 对应线上 ``paper_buy_open_window_minutes`` 的「窗口内多次撮合」语义。
    open_fill_minutes: int = 0


def calc_fee(side: str, amount: float) -> float:
    """A 股单笔费用：佣金（最低5元）+ 卖出印花税 + 双边过户费。

    费率取 ``Settings.trade_fee_*``（与实盘 engine 同一份），改一次两边同时生效；
    原先这里和 ``paper/engine.py`` 各写一份常量，改了一边就会让回测与实盘对不上。
    """
    s = get_settings()
    commission = max(amount * s.trade_fee_commission_rate, s.trade_fee_commission_min)
    stamp = amount * s.trade_fee_stamp_rate if side == "sell" else 0.0
    return round(commission + stamp + amount * s.trade_fee_transfer_rate, 2)


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


def _intraday_bar_count(hhmm: str) -> int:
    """``HH:MM`` → 截至该时刻（含）已产生的 5 分钟 bar 根数。

    上午 09:30-11:30 共 24 根、下午 13:00-15:00 共 24 根，午休不产生 bar，
    所以不能简单用「相对 09:30 的分钟数 / 5」（13:30 会算成第 48 根而实际是第 30 根）。
    返回 0 表示该时刻早于开盘或格式非法，调用方跳过。
    """
    text = str(hhmm or "").strip()
    try:
        hour, minute = text.split(":")[:2]
        now_min = int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 0
    am_open, am_close = 9 * 60 + 30, 11 * 60 + 30
    pm_open, pm_close = 13 * 60, 15 * 60
    if now_min < am_open:
        return 0
    if now_min <= am_close:
        return math.ceil((now_min - am_open) / 5)
    if now_min < pm_open:
        return 24                       # 午休：上午 24 根已全部产生
    if now_min > pm_close:
        now_min = pm_close
    return 24 + math.ceil((now_min - pm_open) / 5)


def _open_window_fill(
    code: str, date: str, prev_close_px: float, minutes: int
) -> float | None:
    """开盘窗口内「开板补单」：返回窗口内第一根打开涨停的 5 分钟收盘价。

    线上开盘窗口（``paper_buy_open_window_minutes``）里每 5 秒撮合一次，
    开盘一字涨停也可能在 09:35~09:35+N 之间开板成交；回测原来只取开盘价一个点，
    等于把这种机会全部判死。这里用本地 5 分钟线还原：窗口内出现过
    ``close < 前收 × 1.095`` 就以该价成交，否则仍算买不到。

    无本地分钟数据时返回 ``None``（宁可保守判定买不到，也不降级成开盘价成交）。
    """
    bars = get_minute_day_bars(code, date)
    if not bars:
        return None
    closes = bars.get("close") or []
    if not closes or prev_close_px <= 0:
        return None
    k = min(len(closes), max(1, math.ceil(max(1, int(minutes)) / 5)))
    limit = prev_close_px * 1.095
    for raw in closes[:k]:
        value = float(raw)
        if value > 0 and value < limit:
            return value
    return None


def _vwap_1m(code: str, date: str, minutes: int = 30) -> float | None:
    """开盘后前 ``minutes`` 分钟的 1 分钟成交量加权均价（真实可成交价的代理）。

    线上开盘窗口内每 5 秒撮合一次，成交价落在开盘后一段时间的均价附近，
    而不是「开盘价」这个单点（``#35`` 实测开盘 30 分内已有 −6.7bp 偏离）。
    这里用 1m 的 ``amount / volume`` 还原，无本地 1m 数据返回 ``None``（调用方回落开盘价）。
    """
    bars = get_minute_day_bars(code, date, period="1")
    if not bars:
        return None
    vols = [float(x) for x in (bars.get("volume") or [])]
    amts = [float(x) for x in (bars.get("amount") or [])]
    if not vols or not amts:
        return None
    k = min(len(vols), len(amts), max(1, int(minutes)))
    v = sum(vols[:k])
    a = sum(amts[:k])
    if v <= 0 or a <= 0:
        return None
    px = a / v
    return px if px > 0 else None


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
                buy_th, sell_th = thresholds()
                action = (
                    "buy" if total >= buy_th
                    else ("sell" if total <= sell_th else "hold")
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

    # E44 弱转强爬坡：``ramp_frac[date]`` = 当天应当恢复到满仓的比例（0=弱市档位，1=满仓）。
    # 口径：连续**非弱**交易日数 k / D（D=regime_ramp_days），k 从 1 开始计，
    # 所以转强第 D 天回到满仓。只依赖 regime（即前一日收盘），同样无未来函数。
    ramp_days = max(0, int(params.regime_ramp_days or 0))
    ramp_frac: dict[str, float] = {}
    if ramp_days > 0 and params.regime_ma is not None and regime:
        n_ramp = max(2, int(params.regime_ma))
        idx_df_ramp = prepared.get("index")
        if idx_df_ramp is not None and not idx_df_ramp.empty:
            ma_ramp = idx_df_ramp["close"].rolling(n_ramp).mean().shift(1)
            streak = ramp_days
            for d, c, m in zip(
                idx_df_ramp["date"], idx_df_ramp["close"].shift(1), ma_ramp
            ):
                if c != c or m != m:
                    streak = ramp_days            # 预热期无法判定 → 按满仓，不制造假的「刚转强」
                elif c < m:
                    streak = 0
                else:
                    streak = min(ramp_days, streak + 1)
                ramp_frac[str(d)] = streak / ramp_days

    # 大盘极速下跌护栏（做T引擎用）：指数收盘序列按交易日对齐（缺失 ffill）
    # 注意：crash guard 只用 idx_open[date] / idx_close[date-1]，即「开盘时已知」的跌幅，
    # 不用当日收盘（那是未来函数，会凭空决定全天不做T —— 记录 #56 的口径教训）。
    idx_close: pd.Series = pd.Series(dtype=float)
    idx_open: pd.Series = pd.Series(dtype=float)
    _idx_df = prepared.get("index")
    if _idx_df is not None and not _idx_df.empty and "close" in _idx_df.columns and "date" in _idx_df.columns:
        idx_close = _idx_df.set_index("date")["close"].reindex(dates).ffill()
        if "open" in _idx_df.columns:
            idx_open = _idx_df.set_index("date")["open"].reindex(dates).ffill()

    def _idx_open_on(date: str):
        """指数当日开盘（缺失时退回收盘，仅用于做T的 crash guard）。"""
        v = idx_open.get(date)
        if v is None or pd.isna(v):
            v = idx_close.get(date)
        return v

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
    paused_until: int | None = None     # E49：暂停买入到该 day_idx（不含）
    dd_peak = initial_capital           # E49：组合净值的历史峰值

    def price_on(code: str, date: str, field: str = "close") -> float | None:
        idx = pos_index.get(code, {}).get(date)
        if idx is None:
            return None
        value = klines[code].iloc[idx][field]
        return float(value) if value == value and value > 0 else None

    def exec_open_price(code: str, date: str) -> float | None:
        """开盘段的成交基准价：``exec_fill="vwap1m"`` 时用开盘后 N 分钟 1m VWAP，否则开盘价。"""
        if params.exec_fill == "vwap1m":
            vw = _vwap_1m(code, date, int(params.exec_fill_minutes or 30))
            if vw is not None:
                return vw
        return price_on(code, date, "open")

    # 成交价随机扰动（E48）：σ = fill_noise_bps，均值 0，种子固定 → 完全可复现
    _noise_bps = float(params.fill_noise_bps or 0.0)
    _noise_rng = random.Random(int(params.fill_noise_seed or 0) * 7919 + 13)

    def _noisy(price: float) -> float:
        if _noise_bps <= 0 or price is None or price <= 0:
            return price
        return price * (1.0 + _noise_rng.gauss(0.0, _noise_bps / 10000.0))

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
        price = _noisy(price) * (1 - slippage)   # E39：卖出按不利方向滑点成交
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
        price = _noisy(price) * (1 - slippage)   # E39：部分卖出同样按不利方向成交
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
    def _t_buy(code, date, price, shares) -> int:
        """做T加仓 / 买回：买指定股数（受现金限制），返回实际买入股数。"""
        nonlocal cash
        shares = int(shares / LOT) * LOT
        if shares < LOT or code not in positions:
            return 0
        p = positions[code]
        px = _noisy(price) * (1 + slippage)
        amount = px * shares
        fee = calc_fee("buy", amount)
        while shares >= LOT and amount + fee > cash:
            shares -= LOT
            amount = px * shares
            fee = calc_fee("buy", amount)
        if shares < LOT:
            return 0
        cash -= amount + fee
        cost = (amount + fee) / shares
        old = p["shares"]
        new = old + shares
        p["cost"] = (p["cost"] * old + cost * shares) / new
        p["shares"] = new
        p["t_added_shares"] = (p.get("t_added_shares") or 0) + shares
        # 当日新增加仓（今仓，T+1 不可卖）：反弹减仓只能减「昨日及更早」的加仓批次
        p["t_today_added"] = (p.get("t_today_added") or 0) + shares
        p["peak"] = max(float(p.get("peak") or 0.0), px)
        return shares

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
        # 涨停卖半每天最多一次（原先用无日期的 t_half → 该票会**永久**停做T，线上是每日重置）
        if p.get("t_half_date") == date or not params.t_active:
            return
        size = float(params.t_size or 0.25)
        open_chg = (open_px / pc - 1) if pc and pc > 0 else 0.0
        # 大盘极速下跌护栏：指数当日跌幅超阈值则暂停做T
        if params.t_crash_guard is not None:
            ic = _idx_open_on(date)             # 开盘跌幅（无未来函数）
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
                p["t_half_date"] = date
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
            # 反弹减仓：只减「昨日及更早」的加仓批次（今仓 T+1 不可卖）
            sellable_added = max(
                0, int(p.get("t_added_shares") or 0) - int(p.get("t_today_added") or 0)
            )
            if (
                params.t_rebound_sell and sellable_added >= LOT
                and close_px is not None and pc
                and close_px >= pc * (1 - params.t_rebound_sell)
            ):
                ratio = sellable_added / p["shares"] if p["shares"] else 1.0
                sell_part(code, date, close_px, min(1.0, ratio), "反弹减仓")
                pp = positions.get(code)
                if pp is not None:
                    pp["t_added_shares"] = int(pp.get("t_added_shares") or 0) - sellable_added

    # ---- E46：做T（5 分钟路径版）----
    def _t_sell_shares(code, date, price, want, reason) -> int:
        """按**股数**卖出，且只动可卖底仓（当日做T加仓的今仓不可卖）。返回实际卖出股数。"""
        nonlocal cash
        p = positions[code]
        shares = int(p["shares"])
        base = max(0, shares - int(p.get("t_today_added") or 0))
        want = min(int(want / LOT) * LOT, (base // LOT) * LOT)
        if want < LOT:
            return 0
        if shares - want < LOT:
            want = shares                      # 剩余不足一手 → 整笔清掉
        px = _noisy(price) * (1 - slippage)    # 与 sell/sell_part 同口径：按不利方向成交
        amount = want * px
        fee = calc_fee("sell", amount)
        cash += amount - fee
        cost = want * p["cost"]
        pnl = amount - fee - cost
        rest = shares - want
        if rest < LOT:
            positions.pop(code)
        else:
            p["shares"] = rest
            # 加仓批次随之收缩，但不能小于 0
            p["t_added_shares"] = min(int(p.get("t_added_shares") or 0), rest)
        trades.append({
            "code": code, "name": p["name"], "buy_date": p["buy_date"],
            "sell_date": date, "buy_price": round(p["cost"], 3),
            "sell_price": round(px, 3), "shares": want,
            "amount": round(amount, 2), "fee": fee, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 2) if cost else 0.0,
            "reason": reason, "score": p["score"],
        })
        return want

    def _t_engine_min(code, date, day_idx, bars, pc):
        """做T（5 分钟路径版）：与线上 ``t_tick`` **同序同条件**，只是把 tick 换成 5m 收盘。

        与日K版的关键差异：路径由序列顺序决定（能区分「先冲高后回落」与「先杀跌后反弹」），
        且「当日卖出 → 当日回落买回」可以成立（日K版只能等次日开盘）。
        """
        p = positions[code]
        if not params.t_active:
            return
        closes = bars.get("close") or []
        opens = bars.get("open") or []
        if not closes or not pc or pc <= 0:
            return
        day_open = float(opens[0]) if opens else float(closes[0])
        if day_open <= 0:
            return
        size = float(params.t_size or 0.25)
        open_chg = day_open / pc - 1
        # 每日重置：今仓股数与「当日已加仓」标记（涨停卖半的标记也按日）
        if p.get("t_today_date") != date:
            p["t_today_date"] = date
            p["t_today_added"] = 0
            p["t_down_armed"] = 0
        # 大盘极速下跌护栏：暂停开新做T（已卖出待买回的仍按买回线走）
        crash = False
        if params.t_crash_guard is not None:
            ic = _idx_open_on(date)             # 开盘跌幅（无未来函数）
            ip = idx_close.get(dates[day_idx - 1]) if day_idx > 0 else None
            if (ic is not None and not pd.isna(ic)
                    and ip is not None and not pd.isna(ip) and ip > 0):
                crash = (float(ic) / float(ip) - 1) <= -float(params.t_crash_guard)
        sold_today = 0
        buyback_line = max(day_open, pc) * (1 + float(params.t_up_buyback or 0.0))
        for raw in closes:
            px = float(raw)
            if px != px or px <= 0:
                continue
            # 1) 当日买回：回落到买回线以内才接；crash 当日不接刀（顺延次日开盘）
            if sold_today > 0:
                if not crash and px <= buyback_line:
                    bought = _t_buy(code, date, px, sold_today)
                    sold_today -= bought
                continue
            if crash:
                continue
            # 2) 涨停卖半：盘中触涨停价（+9.5%）且可卖底仓够 → 卖一半，当日只做一次
            base = max(0, int(p["shares"]) - int(p.get("t_today_added") or 0))
            if (params.t_limit_sell_half and p.get("t_half_date") != date
                    and px >= pc * 1.095 and base >= LOT):
                got = _t_sell_shares(code, date, px, base // 2, "涨停卖半")
                sold_today += got
                p = positions.get(code)
                if p is None:
                    return
                if got:
                    p["t_half_date"] = date
                continue
            # 3) 倒T：开盘涨且盘中触 +t_up_sell → 卖 t_size 的可卖底仓
            if (params.t_up_sell and open_chg > 0
                    and px >= day_open * (1 + float(params.t_up_sell))):
                want = int(int(p["shares"]) * size / LOT) * LOT
                got = _t_sell_shares(code, date, px, want, "做T卖出")
                sold_today += got
                p = positions.get(code)
                if p is None:
                    return
                continue
            # 4) 下跌加仓 / 反弹减仓（两者都只在低开日做，与线上一致）
            if open_chg < 0:
                if params.t_rebound_sell and px >= pc * (1 - float(params.t_rebound_sell)):
                    sellable_added = max(
                        0, int(p.get("t_added_shares") or 0) - int(p.get("t_today_added") or 0)
                    )
                    if sellable_added >= LOT:
                        got = _t_sell_shares(code, date, px, sellable_added, "反弹减仓")
                        if got:
                            p = positions.get(code)
                            if p is None:
                                return
                            p["t_added_shares"] = max(
                                0, int(p.get("t_added_shares") or 0) - got
                            )
                            continue
                if int(p.get("t_down_armed") or 0) == 0:
                    budget = 0.0
                    if params.t_down_full and px <= day_open * (1 - float(params.t_down_full)):
                        budget = cash * 0.95
                    elif params.t_down_add and px <= day_open * (1 - float(params.t_down_add)):
                        budget = cash * float(params.t_down_add)
                    if budget > 0:
                        est = int(budget / px / LOT) * LOT
                        if est >= LOT:
                            bought = _t_buy(code, date, px, est)
                            if bought:
                                p = positions.get(code)
                                if p is None:
                                    return
                                p["t_down_armed"] = 1
        # 收尾：当日没买回的股数 → 次日开盘无条件恢复底仓（线上 stale 规则）
        p = positions.get(code)
        if sold_today > 0 and p is not None:
            p["t_buyback_shares"] = int(p.get("t_buyback_shares") or 0) + sold_today

    def _intraday_buy(date: str, day_idx: int, day_exposure: float) -> None:
        """全时段买入：在各时点用「当日截断日线」重算候选评分，达标即以分钟价成交。

        与开盘段共用同一套约束（名额 / 总仓位 / 单票上限 / 评分分档 / 涨停 /
        切入点 / 整手 / T+1），只有两处不同：
          - 成交价取该时点的 5 分钟收盘价，而不是开盘价；
          - 评分用「历史原始日线 + 当日截至该时点的聚合 bar」重算，
            因此盘中可以出现开盘时还没有的信号（这正是实盘盘中分析的行为）。

        候选 = 前一交易日收盘已在自选池且判 ``buy`` 的票（``signals[prev]``），
        所以因果链是「昨日收盘入选 → 今日盘中重算确认 → 达标才买」，无未来函数。
        """
        nonlocal cash
        prev = dates[day_idx - 1]
        base = [s for s in signals.get(prev, []) if s.get("action") == "buy"]
        if not base:
            return
        base.sort(key=lambda s: -s["score"])
        top_n = max(1, int(params.intraday_entry_top_n or 10))
        cands = base[:top_n]
        bought: set[str] = set()
        for hhmm in (params.intraday_entry_times or ()):
            k = _intraday_bar_count(hhmm)
            if k <= 0:
                continue
            for cand in cands:
                code = str(cand.get("code") or "")
                if not code or code in positions or code in bought:
                    continue
                if len(positions) >= max_positions:
                    break
                idx = pos_index.get(code, {}).get(date)
                if idx is None:
                    continue
                bars = get_minute_day_bars(code, date)
                if not bars or len(bars.get("close") or []) < k:
                    continue
                px = float(bars["close"][k - 1])
                if not (px > 0):
                    continue
                pc = prev_close(code, date)
                if pc and px >= pc * 1.095:
                    continue                      # 涨停买不到（与开盘段同口径）
                # 截断日线：历史**原始** OHLCV（不含当日）+ 当日截至该时点的聚合 bar。
                # 必须剔除 klines 里预计算的指标列——它们是用整表（含当日及之后）算的。
                cols = [c for c in _RAW_COLS if c in klines[code].columns]
                hist = klines[code].iloc[:idx][cols]
                if hist.empty:
                    continue
                closes = [float(x) for x in (bars.get("close") or [])[:k]]
                highs = [float(x) for x in (bars.get("high") or [])[:k]] or closes
                lows = [float(x) for x in (bars.get("low") or [])[:k]] or closes
                vols = [float(x) for x in (bars.get("volume") or [])[:k]]
                amts = [float(x) for x in (bars.get("amount") or [])[:k]]
                row = {
                    "date": date,
                    "open": float((bars.get("open") or closes)[0]),
                    "high": max(highs), "low": min(lows), "close": px,
                    "volume": sum(vols), "amount": sum(amts),
                }
                try:
                    frame = add_all_indicators(
                        pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
                    )
                    res = compute_factors(frame, params.factor_weights)
                except Exception:  # noqa: BLE001 - 单票算不出来就跳过，不影响整轮
                    continue
                total = float(res.total)
                if params.invert_score:
                    total = -total
                    buy_th, sell_th = thresholds()
                    action = ("buy" if total >= buy_th
                              else ("sell" if total <= sell_th else "hold"))
                else:
                    action = res.action
                if action != "buy" or total < half_score:
                    continue
                # ⑤ 切入点：与开盘段同口径，基准都是前一日收盘
                if (params.entry_max_chase is not None and pc
                        and px / pc - 1 > params.entry_max_chase):
                    continue
                if params.entry_pullback and pc and px > pc * (1 - float(params.entry_pullback)):
                    continue
                fill_px = _noisy(px) * (1 + slippage)
                # 盘中估值只能用开盘价（当日收盘/最高要收盘才知道，用它们是未来函数）
                mv = market_value(date, "open")
                total_asset = cash + mv
                pool_budget = max(0.0, total_asset * day_exposure - mv)
                budget = min(pool_budget, total_asset * single_ratio, cash)
                shares = int(budget / fill_px / LOT) * LOT
                while (shares >= LOT
                       and fill_px * shares + calc_fee("buy", fill_px * shares) > cash):
                    shares -= LOT
                if shares < LOT:
                    continue
                amount = fill_px * shares
                fee = calc_fee("buy", amount)
                cash -= amount + fee
                cost = (amount + fee) / shares
                positions[code] = {
                    "name": cand.get("name") or names.get(code, code),
                    "shares": shares, "cost": cost, "buy_date": date,
                    "score": total,
                    "stop": cost * (1 - stop_distance(code, date)),
                    "target": cost * (1 + params.take_profit),
                    # 盘中买入时当日最高价尚未走完，peak 只能从成交价起步（避免未来函数）
                    "peak": px,
                }
                bought.add(code)

    day_no = {d: i for i, d in enumerate(dates)}

    rebalance_every = max(1, int(params.rebalance_days or 1))

    for day_idx, date in enumerate(dates):
        rotation_used = 0  # 每日轮动次数上限独立计数
        rebalance = day_idx % rebalance_every == 0  # 交易日序号对齐，避免周末/停牌导致漂移
        # E49 组合级回撤断路器：暂停期内不生成买入委托（止损/止盈/信号卖出照常）
        if paused_until is not None and day_idx < paused_until:
            rebalance = False

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
            elif ramp_days > 0:
                # E44：转强后不一次性满仓，按 ramp_frac 从弱市档位线性爬回 exposure
                frac = ramp_frac.get(date, 1.0)
                if frac < 1.0:
                    day_exposure = min(
                        exposure,
                        params.weak_exposure
                        + (exposure - params.weak_exposure) * frac,
                    )
        # 开盘下单只能用开盘价估值：用当日收盘价算总资产/仓位上限等于提前知道收盘
        mv_open = market_value(date, "open")
        total_asset = cash + mv_open
        pool_budget = max(0.0, total_asset * day_exposure - mv_open)
        slots = max_positions - len(positions)
        ranked = sorted(pending_buys, key=lambda b: -b["score"])
        if params.queue_top_m is not None and int(params.queue_top_m) > 0:
            ranked = ranked[: int(params.queue_top_m)]   # ③ 交易队列容量 topM
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
            if px is None:
                continue                          # 停牌/当日无数据
            forced_fill: float | None = None       # 开板补单：已有确定成交价，不再套执行价模型
            if pc and px >= pc * 1.095:
                # 涨停或停牌买不到（判定用原始开盘价，滑点不影响能否成交）。
                # 线上窗口内每 5 秒有一次撮合，「开盘价一次性成交」把窗口压成一个点：
                # 开盘一字板就当天放弃，忽略了 09:35 之后可能开板的事实。
                if not params.open_fill_minutes:
                    continue
                opened = _open_window_fill(code, date, pc, params.open_fill_minutes)
                if opened is None:
                    continue                      # 窗口内一直封板，当天确实买不到
                px = opened
                forced_fill = opened
            # E2：高开太多不追（次日开盘涨幅上限），缺席即沿用线上「照价买入」
            if params.max_open_gain is not None and pc and px / pc - 1 > params.max_open_gain:
                continue
            # ⑤ 切入点：日K 粒度（不追高 + 回踩）/ 分钟级等回踩 / 分钟级强势确认
            # E47：成交基准价按 exec_fill 取（开盘价 / 开盘后 N 分钟 1m VWAP）
            fill_px = forced_fill if forced_fill is not None else exec_open_price(code, date)
            minute_window = int(params.entry_window_minutes or 0)
            confirm_window = int(params.entry_confirm_minutes or 0)
            if (params.intraday_minutes and confirm_window > 0
                    and params.entry_confirm_mode):
                # E45：确认强势才买。观察窗内条件不成立 → 当天不买（不追、不降级）。
                bars = get_minute_day_bars(code, date)
                if not bars:
                    continue              # 无分钟数据时不猜，直接放弃（保证可复现）
                k = max(1, confirm_window // 5)
                cls = bars["close"][:k]
                if not cls:
                    continue
                mode = str(params.entry_confirm_mode)
                ok = True
                if mode in ("hold_open", "both"):
                    day_open = float(bars["open"][0]) if bars["open"] else px
                    ok = all(c >= day_open for c in cls)      # 盘中从未破开盘价
                if ok and mode in ("above_avg", "both"):
                    cum_v = sum(bars["volume"][:k])
                    cum_a = sum(bars["amount"][:k])
                    ok = cum_v > 0 and cls[-1] >= cum_a / cum_v   # 站上分时均价
                if not ok:
                    continue              # 窗口内没站住，当天不买
                fill_px = float(cls[-1])  # 以观察窗最后一根的收盘价成交
                if (params.entry_max_chase is not None and pc
                        and fill_px / pc - 1 > params.entry_max_chase):
                    continue
            elif params.intraday_minutes and minute_window > 0 and pc:
                bars = get_minute_prices(code, date, "5", local_only=True)
                if bars:
                    target = pc * (1 - float(params.entry_pullback or 0.0))
                    fill_px = None
                    for bar_px in bars[: max(1, minute_window // 5)]:
                        if (params.entry_max_chase is not None
                                and float(bar_px) / pc - 1 > params.entry_max_chase):
                            continue      # 这一刻在追高，不算切入点
                        if params.entry_pullback and float(bar_px) > target:
                            continue      # 还没回踩到位，继续等
                        fill_px = float(bar_px)
                        break
                    if fill_px is None:
                        continue          # 窗口内没等到切入点，当天不买
            else:
                if (params.entry_max_chase is not None and pc
                        and px / pc - 1 > params.entry_max_chase):
                    continue
                if params.entry_pullback and pc and px > pc * (1 - float(params.entry_pullback)):
                    continue
            px = _noisy(fill_px) * (1 + slippage)   # E39：买入按不利方向滑点成交（股数按滑点后价格算）
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

        # ---------- 1.5) 全时段买入（可选）：按当日分钟线截断重算自选池评分 ----------
        # 实盘「盘中分析」每 N 分钟用当时实时价重算 14 因子，达标立刻成交；
        # 回测不能复用收盘信号（那是「次日开盘」口径），必须把**当日日线截断到该时点**
        # 再算一遍指标，否则等于拿收盘信息在盘中下单。
        if params.intraday_entry and day_idx > 0:
            _intraday_buy(date, day_idx, day_exposure)

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
                # 只读本地缓存：联网取数会让同一参数的结果随时间漂移（不可复现）
                ms = get_minute_prices(code, date, "5", local_only=True)
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
            # E47：开盘段成交基准价（止损/止盈在开盘瞬间成交时用它，而不是开盘价单点）
            fill_open = exec_open_price(code, date) or open_px
            # 做T引擎（遵循 A 股 T+1）：
            #   - t_minute_path=True：按当日 5m 序列逐点推进（路径可信），无本地分钟数据当天不做T；
            #   - 否则沿用日K四价近似（倒T 成交价取阈值价、买回/减仓按次日开盘）。
            if params.t_active and params.t_minute_path:
                _bars = get_minute_day_bars(code, date, period=params.t_minute_period)
                if _bars and _bars.get("close"):
                    _t_engine_min(code, date, day_idx, _bars, prev_close(code, date))
            else:
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
                fill = stop_hit if real else min(fill_open, stop_px)
                sell(code, date, fill, stop_reason)
                continue
            if partial_hit:
                fill = partial_hit if real else max(fill_open, partial_px)
                sell_part(code, date, fill, params.partial_tp_ratio, "分段止盈")
                if code not in positions:
                    continue
                p = positions[code]
            if tp_hit:
                fill = tp_hit if real else max(fill_open, tp_px)
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
        eq_close = cash + mv
        equity_curve.append({
            "date": date, "equity": round(eq_close, 2),
            "benchmark": round(bench_value * initial_capital, 2),
            "cash": round(cash, 2), "positions": len(positions),
        })
        # E49：组合级回撤断路器——当日收盘触发，次日起暂停新开仓 N 个交易日
        if params.dd_pause_at is not None:
            dd_peak = max(dd_peak, eq_close)
            drawdown = eq_close / dd_peak - 1 if dd_peak > 0 else 0.0
            if drawdown <= -float(params.dd_pause_at):
                n_pause = max(1, int(params.dd_pause_days or 10))
                if paused_until is None or paused_until <= day_idx:
                    paused_until = day_idx + n_pause
                    logger.info(
                        "回撤断路器触发 %s：回撤 %.2f%% ≤ −%.0f%%，暂停买入至第 %d 个交易日",
                        date, drawdown * 100, float(params.dd_pause_at) * 100, paused_until,
                    )

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
            "queue_top_m": params.queue_top_m,
            "entry_max_chase": params.entry_max_chase,
            "entry_pullback": params.entry_pullback,
            "entry_window_minutes": params.entry_window_minutes,
            "entry_confirm_minutes": params.entry_confirm_minutes,
            "entry_confirm_mode": params.entry_confirm_mode,
            "intraday_entry": params.intraday_entry,
            "intraday_entry_times": list(params.intraday_entry_times or ()),
            "intraday_entry_top_n": params.intraday_entry_top_n,
            "open_fill_minutes": params.open_fill_minutes,
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
            "dd_pause_at": params.dd_pause_at,
            "dd_pause_days": int(params.dd_pause_days or 0),
            "regime_fast_ma": params.regime_fast_ma,
            "regime_tiers": list(params.regime_tiers) if params.regime_tiers else None,
            "regime_flat_below": params.regime_flat_below,
            "regime_ramp_days": ramp_days,
            # 做T（None/0/False = 关闭）：结果里必须能看到，否则无法判断某次回测到底开没开做T
            "t_active": bool(params.t_active),
            "t_minute_path": bool(params.t_minute_path),
            "t_minute_period": params.t_minute_period,
            "exec_fill": params.exec_fill,
            "fill_noise_bps": float(params.fill_noise_bps or 0.0),
            "fill_noise_seed": int(params.fill_noise_seed or 0),
            "exec_fill_minutes": int(params.exec_fill_minutes or 0),
            "t_size": params.t_size,
            "t_up_sell": params.t_up_sell,
            "t_up_buyback": params.t_up_buyback,
            "t_down_add": params.t_down_add,
            "t_down_full": params.t_down_full,
            "t_rebound_sell": params.t_rebound_sell,
            "t_limit_sell_half": params.t_limit_sell_half,
            "t_crash_guard": params.t_crash_guard,
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
