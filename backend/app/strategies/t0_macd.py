# -*- coding: utf-8 -*-
"""5 分钟 MACD(6,38,6) T+0 做T策略 · 回测/模拟校验模块（学习研究用途，不接实盘）。

背景
----
源自「板哥/悟空战法」一类日内做T教程：把 MACD 默认 (12,26,9) 改为 (6,38,6) 以
加快信号反应，在 5 分钟级别用金叉买、死叉卖抓取日内波动差价。本模块把它量化成
可回测、可复现的状态机，并把原文强调的「五条死规矩」实现成可调参数。

关键工程点
----------
1. MACD(6,38,6) 的 EMA38 跨约 190 分钟（≈大半交易日），因此**必须在跨日连续的
   5 分钟序列上计算指标**，单日 48 根远远不够预热。模块对整段序列算指标，但只在
   每个交易日的 ``clear_time``（默认 14:50）强制平掉 T 仓，保证「不过夜、收盘还原」。
2. 指标状态跨日保留，T 仓状态每日清零（符合 A 股 T+1：做T靠底仓，当日一买一卖）。
3. 全部为纯函数 + 本地分钟数据，不联网、不引入未来函数，结果可复现。

纪律参数（即「五条死规矩」的可执行化）
--------------------------------------
- require_uptrend：只在慢线(EMA38)上行时做正T（先买后卖）；否则只做反T或休息。
- allow_reverse：是否允许反T（先卖后买，高位卖出、低位接回磨低持仓成本）。
- cost_per_round_pct：单笔 T 的往返成本（佣金双边+印花税+过户费），默认 0.10%。
- max_hold_bars：时间止损——持仓超过该根数(3~5 根=15~25 分)且价格未涨则主动离场。
- stop_pct：破位止损——价格反向击穿入场价该比例(默认 -1.5%) 无条件离场。
- clear_time：14:50 前 T 仓必须清零（收盘持股数=开盘数才算做过T）。
- t_ratio：T 仓占底仓比例（原文 20%~50%，默认 30%），绝不超过底仓。
- min_entry_move_pct：信号根最小波动过滤，过滤微小毛刺导致的频繁假信号（默认 0）。
- trend_filter：日线趋势硬闸门——仅当「均线多头排列(MA5>MA20>MA60) 且站上 MA60」
  时才允许做正T；否则只做反T或休息（对应原文"只在日线趋势向上才做T"）。
- min_daily_amp_pct：日振幅硬下限(%)。低于该振幅说明没有做T空间，整日禁止开仓。
- min_daily_amount：日成交额硬下限(元)。流动性不足（如 <1 亿）整日禁止开仓。
  以上三项由 5 分钟序列重采样出的日线派生，且**用前一交易日信息决定当日**
  （shift 1 日），避免未来函数；每日 14:50 仍强制清零不过夜。

- vol_adaptive：波动率自适应——止损距离取 max(固定%止损, atr_stop_mult×ATR)，
  仓位按「单笔恒定风险」反推（风险高则仓小、风险低则仓大），抑制高波动鞭锯。
- risk_per_round_pct：单笔目标风险占底仓比例（vol_adaptive 时生效）。
- support_entry：正T仅在「支撑位附近」金叉才入场（回踩低吸，改善盈亏比）；
  支撑位 = 近 support_lookback 根 5 分钟最低价（shift 1 防未来函数），价格须在其
  上方且不超过 +support_band_pct 才算「附近」。

- signal_mode：信号源替换，四选一——``cross``（默认，金叉死叉）/
  ``hist_turn``（MACD 柱拐头：柱由缩短转放长=向上拐头作买/接回，柱由放长转缩短=
  向下拐头作卖/开空）/ ``reversal``（支撑/阻力均值回归：在支撑附近且动能转强时
  买、在阻力附近且动能转弱时卖，典型**非顺势**策略）/ ``divergence``（MACD 柱
  背离：价格新低但柱不新低作买、价格新高但柱不新高作卖，抓反转，亦属非顺势）。
  非顺势信号直接回应「裸交叉/柱拐头仍属顺势类、震荡里难盈利」的诊断。
- reversal_lookback：均值回归窗口（近 N 根 5 分钟高低点作为动态支撑/阻力，默认 60）。
- divergence_lookback / divergence_tol：背离回看窗口与价格新低/新高容差。
- vol_confirm：量能确认——正T入场需放量（当前量 > 量能均线×vol_ratio），正T
  出场与反T接回需缩量（当前量 < 量能均线/vol_ratio），过滤无量假突破。

- 盈亏比优化（payoff structure）：入场信号收敛后，能否盈利取决于「赢亏比」。本模块
  提供三道杠杆——① 固定止盈 take_profit_pct（>0 启用，到达即锁定盈利）；② 追踪止盈
  trailing_tp（盈利超 trail_activate_pct 后，从峰值回撤 trail_pct 即止盈离场，让赢家跑、
  封住回吐）；③ ATR/固定% 硬止损封顶亏损（vol_adaptive 时由 atr_stop_mult×ATR 与
  stop_pct 取大）。三者配合，把「小赢大亏」扭成「让赢家跑、亏则封顶」。

- 更高周期方向偏置（K 变体）：在小周期信号之上，叠加更高周期（默认 60 分钟）
  的方向过滤——只做与更高周期方向一致的做T：正T(先买后卖) 需更高周期为多、反T
  (先卖后买) 需更高周期为空，否则整笔放弃。本质是「在更高周期趋势的回调里找小周期
  反转买点」，用更大时间框架的「势」压住 5 分钟噪声。偏置由 5 分钟序列重采样出
  更高周期后取 MACD(dif>dea) 或 dif 斜率判定；调用时取「当前 5 分钟 bar 所属更高
  周期组的**前一组**」作为可用偏置，绝不用正在形成的更高周期 bar（无未来函数）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from app.data.minute import MINUTE5_DIR
from app.indicators.technical import ema, macd

# 单只代码 5 分钟 parquet 缓存（整段序列只读一次，跨回测区间复用）
_LOCAL5: dict[str, pd.DataFrame] = {}
_LOCAL5_CAP = 300
_LOCK = threading.Lock()


@dataclass
class T0Params:
    """做T策略参数（默认即「板哥」原文设定，均可调）。"""

    fast: int = 6
    slow: int = 38
    signal: int = 6
    t_ratio: float = 0.30          # T 仓占底仓比例（20%~50%）
    require_uptrend: bool = True   # 仅慢线上行时做正T
    allow_reverse: bool = True     # 允许反T（先卖后买）
    cost_per_round_pct: float = 0.0010   # 单笔往返成本 ≈0.10%
    max_hold_bars: int = 5         # 时间止损根数（5 根≈25 分）
    stop_pct: float = 0.015        # 破位止损（反向 -1.5%）
    time_stop_eps: float = 0.0     # 时间止损时「未涨」判定容差
    clear_time: str = "14:50"      # 该时刻前 T 仓清零
    min_entry_move_pct: float = 0.0  # 信号根最小波动过滤（0=不过滤）
    cooldown_bars: int = 1         # 每笔平仓后冷却根数，抑制鞭锯反复开仓
    zero_axis_filter: str = "any"  # 正T金叉位置过滤: any/below(0轴下方更可靠)/above
    trend_filter: bool = False     # 日线趋势硬闸门：仅多头排列+站上MA60才做正T
    min_daily_amp_pct: float = 0.0  # 日振幅硬下限(%)，不足整日禁开仓（0=不过滤）
    min_daily_amount: float = 0.0   # 日成交额硬下限(元)，流动性不足整日禁开仓（0=不过滤）
    vol_adaptive: bool = False      # 波动率自适应：ATR 缩放止损+仓位，恒定风险/笔
    risk_per_round_pct: float = 0.003   # 单笔目标风险占底仓比例（vol_adaptive 时生效）
    atr_period: int = 20            # ATR 计算周期（5 分钟 bar 数）
    atr_stop_mult: float = 1.5      # 止损距离 = max(固定%止损, atr_stop_mult×ATR)
    support_entry: bool = False     # 正T仅"支撑位附近"金叉才入场（回踩低吸）
    support_lookback: int = 60      # 支撑位 = 近 N 根 5 分钟最低价（shift1 防未来函数）
    support_band_pct: float = 0.015 # 价格须在该支撑之上、且不超过其 +band 才算"附近"
    signal_mode: str = "cross"        # 信号源: cross / hist_turn / reversal / divergence
    hist_turn_zeroline: bool = True   # 柱拐头仅在 0 轴下方/附近有效(低位更可靠)
    vol_confirm: bool = False         # 量能确认: 正T入场放量、出场/接回缩量
    vol_ma_period: int = 20           # 量能均线周期(5分钟bar数)
    vol_ratio: float = 1.2            # 放量阈值(当前量 > 均量×该值)
    reversal_lookback: int = 60       # 均值回归: 近 N 根高低点作支撑/阻力
    divergence_lookback: int = 40     # 背离: 回看窗口(bar数)
    divergence_tol: float = 0.003     # 背离: 价格新低/新高容差
    # ---- 盈亏比优化（payoff structure）----
    take_profit_pct: float = 0.0     # 固定止盈比例(>0 启用)，到达即锁定盈利
    trailing_tp: bool = False         # 追踪止盈：盈利超激活阈值后，回撤触发止盈
    trail_activate_pct: float = 0.004 # 盈利达此比例方激活追踪止盈
    trail_pct: float = 0.005         # 从峰值回撤该比例即止盈离场
    # ---- 更高周期方向偏置（K 变体）----
    bias_filter: bool = False         # 仅当更高周期方向与此笔做T方向一致才交易
    bias_tf: int = 60                 # 更高周期分钟数（默认 60 分钟）
    bias_mode: str = "macd"           # 偏置判定: macd(dif>dea) / slope(dif 斜率)


def default_params() -> T0Params:
    return T0Params()


@dataclass
class Trade:
    """一笔完成的做T回合（正T=long / 反T=short）。"""

    code: str
    day: str
    side: str                       # 'long'(正T) / 'short'(反T)
    entry_idx: int
    entry_price: float
    exit_idx: int
    exit_price: float
    bars_held: int
    pnl: float                      # 该回合净盈亏（以 base_qty=1 计）
    pnl_pct: float                  # 占该笔名义本金的百分比
    forced: bool                    # 是否由 14:50 强制清零触发


# ---------------------------------------------------------------------------
# 本地 5 分钟数据加载（带缓存，免联网）
# ---------------------------------------------------------------------------
def _load_code_5m(code: str) -> Optional[pd.DataFrame]:
    """读取 code 全部历史 5 分钟 bar（升序），缓存复用；无数据返回 None。"""
    with _LOCK:
        cached = _LOCAL5.get(code)
    if cached is not None:
        return cached
    path = MINUTE5_DIR / f"{code}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - 坏文件按无数据处理
        return None
    if df is None or df.empty or "date" not in df.columns:
        return None
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in df.columns:
            return None
    df = df.copy()
    # 统一时间戳列
    df["dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("dt").reset_index(drop=True)
    with _LOCK:
        if len(_LOCAL5) >= _LOCAL5_CAP:
            _LOCAL5.clear()
        _LOCAL5[code] = df
    return df


def clear_5m_cache() -> None:
    with _LOCK:
        _LOCAL5.clear()


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def compute_macd(df: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.DataFrame:
    """在 5 分钟序列上计算 MACD，并附 slow-EMA 用于趋势网关。

    dif = EMA(fast) - EMA(slow)；dea = EMA(dif, signal)；柱 = 2*(dif-dea)。
    ema_slow 用于「正T 仅当慢线上行」的趋势判定。
    """
    out = macd(df, fast=fast, slow=slow, signal=signal)
    out["ema_slow"] = ema(out["close"], slow)
    return out


def _parse_time(t: str) -> tuple[int, int]:
    h, m = t.split(":")
    return int(h), int(m)


# ---------------------------------------------------------------------------
# 日线上下文（由 5 分钟序列重采样）— 用于「强趋势/高振幅/流动性」硬过滤
# ---------------------------------------------------------------------------
def _daily_context(bars: pd.DataFrame) -> pd.DataFrame:
    """由整段 5 分钟序列重采样出日线，计算趋势/振幅/流动性闸门所需字段。

    返回 DataFrame，索引为当日 00:00 的 Timestamp，含列：
    open/high/low/close/amount（日聚合）、amp（日振幅）、ma5/ma20/ma60（收盘均线）。
    """
    df = bars.copy()
    dt = pd.to_datetime(df["dt"])
    df["_date"] = dt.dt.normalize()
    g = df.groupby("_date")
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "amount": g["amount"].sum(),
    }).sort_index()
    prev_close = daily["close"].shift(1)
    daily["amp"] = (daily["high"] - daily["low"]) / prev_close.replace(0, np.nan)
    daily["ma5"] = daily["close"].rolling(5).mean()
    daily["ma20"] = daily["close"].rolling(20).mean()
    daily["ma60"] = daily["close"].rolling(60).mean()
    return daily


def _build_gate_map(daily: pd.DataFrame, params: T0Params) -> dict[str, tuple[bool, bool, bool]]:
    """由日线上下文生成「每个交易日」的开仓闸门 (amp_ok, liq_ok, uptrend)。

    关键：用**前一交易日**的信息决定当日（shift 1 日），避免未来函数。
    无前一日数据的首日闸门为 (False, False, False)（不交易）。
    """
    amp_min = params.min_daily_amp_pct / 100.0
    amt_min = params.min_daily_amount
    amp_ok = (daily["amp"] >= amp_min) if amp_min > 0 else pd.Series(True, index=daily.index)
    liq_ok = (daily["amount"] >= amt_min) if amt_min > 0 else pd.Series(True, index=daily.index)
    uptrend = (
        (daily["close"] > daily["ma60"])
        & (daily["ma5"] > daily["ma20"])
        & (daily["ma20"] > daily["ma60"])
    )
    src = pd.DataFrame(
        {"amp_ok": amp_ok, "liq_ok": liq_ok, "uptrend": uptrend},
        index=daily.index,
    )
    src = src.shift(1)  # 前一日信息决定当日
    src = src.fillna(False)  # 无前一日数据的首日闸门全 False（避免 np.nan 被 bool() 判真）
    gate_map: dict[str, tuple[bool, bool, bool]] = {}
    for d, row in src.iterrows():
        gate_map[str(d)[:10]] = (bool(row["amp_ok"]), bool(row["liq_ok"]), bool(row["uptrend"]))
    return gate_map


# ---------------------------------------------------------------------------
# 波动率 / 支撑位辅助（用于 vol_adaptive 与 support_entry 增强）
# ---------------------------------------------------------------------------
def _atr(bars: pd.DataFrame, period: int = 20) -> np.ndarray:
    """True Range 的滚动均值（ATR），向量化实现。"""
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1).fillna(close)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    atr = atr.bfill().fillna(0.0)
    return atr.to_numpy(dtype=float)


def _support_low(bars: pd.DataFrame, lookback: int) -> np.ndarray:
    """近 lookback 根 5 分钟最低价作为支撑位；shift(1) 保证不用到当前 bar（防未来函数）。"""
    low = bars["low"].astype(float)
    s = low.shift(1).rolling(lookback).min()
    s = s.bfill().fillna(low.min() if len(low) else 0.0)
    return s.to_numpy(dtype=float)


def _near_support(price: float, sup: float, band: float) -> bool:
    """价格是否在支撑位附近：须站在支撑之上、且不超过 +band。"""
    if sup <= 0:
        return True  # 无支撑参考时不限
    return sup <= price <= sup * (1 + band)


def _entry_qty(
    params: T0Params, base_qty: float, entry_price: float, atr_val: float, side: int
) -> tuple[float, float]:
    """计算一笔入场的 (仓位, 止损价)。

    - 非自适应：固定 t_ratio 仓位 + 固定%止损。
    - 自适应：止损距离 = max(固定%止损, atr_stop_mult×ATR)；仓位按单笔恒定风险反推，
      并夹在 [t_ratio×10%, t_ratio] 区间（绝不超底仓）。
    """
    if not params.vol_adaptive or atr_val <= 0:
        qty = base_qty * params.t_ratio
        stop_price = entry_price * (1 - params.stop_pct) if side == 1 \
            else entry_price * (1 + params.stop_pct)
        return qty, stop_price
    risk_dist = max(entry_price * params.stop_pct, params.atr_stop_mult * atr_val)
    risk_notional = base_qty * params.risk_per_round_pct
    qty = risk_notional / risk_dist
    cap = base_qty * params.t_ratio
    qty = min(qty, cap)
    qty = max(qty, cap * 0.1)
    stop_price = entry_price - risk_dist if side == 1 else entry_price + risk_dist
    return qty, stop_price


def _hist_turn_signals(hist: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """由 MACD 柱序列(=dif-dea)推导拐头信号。

    向上拐头：hist[i] > hist[i-1] 且 hist[i-1] <= hist[i-2]（由缩短转放长）。
    向下拐头：hist[i] < hist[i-1] 且 hist[i-1] >= hist[i-2]（由放长转缩短）。
    前两根不足时标记为 False（避免边界误判）。
    """
    n = len(hist)
    h_prev1 = np.concatenate([[np.nan], hist[:-1]])
    h_prev2 = np.concatenate([[np.nan, np.nan], hist[:-2]])
    valid = ~np.isnan(h_prev2)
    up = np.zeros(n, dtype=bool)
    down = np.zeros(n, dtype=bool)
    up[valid] = (hist[valid] > h_prev1[valid]) & (h_prev1[valid] <= h_prev2[valid])
    down[valid] = (hist[valid] < h_prev1[valid]) & (h_prev1[valid] >= h_prev2[valid])
    return up, down


def _vol_ratio_array(vol: np.ndarray, period: int) -> np.ndarray:
    """当前成交量 / 量能均线(滚动均值)，向量化；无均线时返回 1（不约束）。"""
    s = pd.Series(np.asarray(vol, dtype=float))
    vma = s.rolling(period, min_periods=1).mean().to_numpy(dtype=float)
    # where=vma>0 分支才做除法，避免停牌/无成交 bar(vma=0) 触发除零 warning
    return np.divide(s.to_numpy(), vma, out=np.ones_like(vma), where=vma > 0)


def _recent_extremes(bars: pd.DataFrame, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """近期动态支撑(近 lookback 根最低价) 与阻力(近 lookback 根最高价)，shift1 防未来函数。

    用于 ``reversal`` 模式：在支撑附近且动能转强时买、在阻力附近且动能转弱时卖。
    """
    low = bars["low"].astype(float)
    high = bars["high"].astype(float)
    rlow = low.shift(1).rolling(lookback).min()
    rhigh = high.shift(1).rolling(lookback).max()
    rlow = rlow.bfill().fillna(low.min() if len(low) else 0.0)
    rhigh = rhigh.bfill().fillna(high.max() if len(high) else 0.0)
    return rlow.to_numpy(dtype=float), rhigh.to_numpy(dtype=float)


def _divergence_signals(
    close: np.ndarray, hist: np.ndarray, lookback: int, tol: float
) -> tuple[np.ndarray, np.ndarray]:
    """MACD 柱背离信号（非顺势，抓反转）。

    看多背离(bull)：价格在回看窗口内创出新低，但 MACD 柱未创出新低（动能不再恶化）；
    看空背离(bear)：价格创出新高，但 MACD 柱未创出新高。
    均用 shift1 窗口（不含当前 bar）避免未来函数。
    """
    c = pd.Series(np.asarray(close, dtype=float))
    h = pd.Series(np.asarray(hist, dtype=float))
    rlow = c.shift(1).rolling(lookback, min_periods=1).min().to_numpy()
    rhigh = c.shift(1).rolling(lookback, min_periods=1).max().to_numpy()
    hmin = h.shift(1).rolling(lookback, min_periods=1).min().to_numpy()
    hmax = h.shift(1).rolling(lookback, min_periods=1).max().to_numpy()
    bull = np.zeros(len(close), dtype=bool)
    bear = np.zeros(len(close), dtype=bool)
    valid = ~np.isnan(rlow)
    c_arr = c.to_numpy()
    h_arr = h.to_numpy()
    bull[valid] = (c_arr[valid] <= rlow[valid] * (1 + tol)) & (h_arr[valid] > hmin[valid])
    bear[valid] = (c_arr[valid] >= rhigh[valid] * (1 - tol)) & (h_arr[valid] < hmax[valid])
    return bull, bear


def _higher_tf_bias(
    bars: pd.DataFrame, tf: int, fast: int, slow: int, signal: int, mode: str = "macd"
) -> tuple[dict, int]:
    """由 5 分钟序列重采样出更高周期(默认 60 分钟)的方向偏置。

    Returns:
        (bias_map, tf)：bias_map 以「更高周期组起始时间(Timestamp)」为键，值为
        +1(多) / -1(空)；调用方应取**当前 5 分钟 bar 所属更高周期组的前一组**作为
        可用偏置，避免使用正在形成的更高周期 bar（未来函数）。

    偏置判定：
    - ``macd``：更高周期 dif > dea（MACD 多头）为多(+1)，否则为空(-1)；
    - ``slope``：更高周期 dif 斜率为正为多(+1)、为负为空(-1)。
    """
    df = bars.copy()
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt")
    grp = df.resample(f"{tf}min").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), amount=("amount", "sum"),
    ).dropna()
    if grp.empty:
        return {}, tf
    md = macd(grp, fast=fast, slow=slow, signal=signal)
    dif = md["dif"].to_numpy(dtype=float)
    dea = md["dea"].to_numpy(dtype=float)
    if mode == "slope":
        prev = np.concatenate([[np.nan], dif[:-1]])
        valid = ~np.isnan(prev)
        sig = np.zeros(len(dif), dtype=int)
        sig[valid] = np.where(dif[valid] > prev[valid], 1, -1)
    else:
        sig = np.where(dif > dea, 1, -1)
    bias_map: dict = {}
    for ts, s in zip(grp.index, sig):
        bias_map[pd.Timestamp(ts)] = int(s)
    return bias_map, tf


# ---------------------------------------------------------------------------
# 单序列状态机（跨日连续，每日 clear_time 强制清零 T 仓）
# ---------------------------------------------------------------------------
def simulate_series(
    bars: pd.DataFrame, params: T0Params, code: str = "", base_qty: float = 1.0
) -> list[Trade]:
    """对一只股票的整段连续 5 分钟序列跑做T状态机，返回所有完成的 T 回合。

    指标在整段序列上计算（保证 EMA38 预热），但 T 仓在每个交易日的
    clear_time 被强制清零，不过夜。
    """
    if bars is None or len(bars) < params.slow + 2:
        return []

    df = compute_macd(bars, params.fast, params.slow, params.signal)
    n = len(df)
    diff = df["dif"] - df["dea"]
    prev = diff.shift(1)
    golden = ((prev <= 0) & (diff > 0)).to_numpy()
    death = ((prev >= 0) & (diff < 0)).to_numpy()
    dif = df["dif"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    ema_slow = df["ema_slow"].to_numpy(dtype=float)
    ts = pd.to_datetime(df["dt"])
    hist = (df["dif"] - df["dea"]).to_numpy(dtype=float)
    hist_turn_up, hist_turn_down = _hist_turn_signals(hist)
    # 非顺势信号源所需序列
    if params.signal_mode in ("reversal", "divergence"):
        recent_low, recent_high = _recent_extremes(bars, params.reversal_lookback)
    else:
        recent_low = recent_high = np.zeros(n)
    if params.signal_mode == "divergence":
        div_bull, div_bear = _divergence_signals(
            close, hist, params.divergence_lookback, params.divergence_tol
        )
    else:
        div_bull = div_bear = np.zeros(n, dtype=bool)
    hist_up = np.zeros(n, dtype=bool)
    hist_down = np.zeros(n, dtype=bool)
    if n > 1:
        hist_up[1:] = hist[1:] > hist[:-1]
        hist_down[1:] = hist[1:] < hist[:-1]
    if params.vol_confirm:
        vol_ratio_arr = _vol_ratio_array(bars["volume"].to_numpy(dtype=float), params.vol_ma_period)
    else:
        vol_ratio_arr = np.ones(n)
    dt = ts.to_numpy()
    bar_min = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()

    # 每天首个到达 clear_time 的 bar 触发强制清零；若该日无此 bar 则取末根
    clear_h, clear_m = _parse_time(params.clear_time)
    clear_min = clear_h * 60 + clear_m
    day_labels = np.array([str(x)[:10] for x in dt])
    force_clear = np.zeros(n, dtype=bool)
    for day in pd.unique(day_labels):
        idxs = np.where(day_labels == day)[0]
        if idxs.size == 0:
            continue
        hit = [j for j in idxs if bar_min[j] >= clear_min]
        if hit:
            force_clear[hit[0]] = True
        else:
            force_clear[idxs[-1]] = True

    # 日线硬过滤（强趋势/高振幅/流动性）：用前一交易日信息决定当日，避免未来函数
    daily_active = params.trend_filter or params.min_daily_amp_pct > 0 or params.min_daily_amount > 0
    gate_map: dict[str, tuple[bool, bool, bool]] = {}
    if daily_active:
        gate_map = _build_gate_map(_daily_context(bars), params)

    # 波动率 / 支撑位序列（增强项，按需计算）
    atr = _atr(bars, params.atr_period) if params.vol_adaptive else np.zeros(n)
    sup_low = _support_low(bars, params.support_lookback) if params.support_entry else np.zeros(n)

    # 更高周期方向偏置（K 变体）：取当前 5 分钟 bar 所属更高周期组的**前一组**作偏置，
    # 绝不用正在形成的更高周期 bar（避免未来函数）。
    bias_arr = np.zeros(n, dtype=int)
    if params.bias_filter:
        bias_map, _ = _higher_tf_bias(
            bars, params.bias_tf, params.fast, params.slow, params.signal, params.bias_mode
        )
        tf_str = f"{params.bias_tf}min"
        floor_arr = ts.dt.floor(tf_str).to_numpy()
        tf_delta = pd.Timedelta(minutes=params.bias_tf)
        for i in range(n):
            key = pd.Timestamp(floor_arr[i]) - tf_delta
            bias_arr[i] = bias_map.get(key, 0)

    qty = base_qty * params.t_ratio
    stop_price = 0.0
    trades: list[Trade] = []
    state = 0            # 0=平, 1=正T持仓(long), -1=反T持仓(short)
    entry_price = 0.0
    entry_idx = -1
    peak_price = 0.0      # 正T持仓期间见过的最高价（追踪止盈用）
    trough_price = 0.0    # 反T持仓期间见过的最低价（追踪止盈用）
    cooldown = 0
    locked_day: Optional[str] = None

    for i in range(1, n):
        price = close[i]
        if price <= 0:      # 跳过异常/缺失价格（避免除零与脏信号）
            continue
        day = day_labels[i]

        # 1) 强制清零（不过夜）
        if state != 0 and force_clear[i]:
            pnl, pnl_pct = _settle(state, entry_price, price, qty, params)
            trades.append(Trade(
                code=code, day=day, side="long" if state == 1 else "short",
                entry_idx=entry_idx, entry_price=entry_price,
                exit_idx=i, exit_price=price, bars_held=i - entry_idx,
                pnl=pnl, pnl_pct=pnl_pct, forced=True,
            ))
            state = 0
            cooldown = params.cooldown_bars
            locked_day = day
            continue

        if state != 0:
            # 2) 持仓中：检查出场（信号源依赖 / 破位 / 时间止损）
            if params.signal_mode == "hist_turn":
                long_exit_sig = bool(hist_turn_down[i])
                short_exit_sig = bool(hist_turn_up[i])
            elif params.signal_mode == "reversal":
                near_resist = price >= recent_high[i] * (1 - params.support_band_pct)
                near_support = price <= recent_low[i] * (1 + params.support_band_pct)
                long_exit_sig = bool(near_resist or hist_down[i])
                short_exit_sig = bool(near_support or hist_up[i])
            elif params.signal_mode == "divergence":
                long_exit_sig = bool(hist_down[i])
                short_exit_sig = bool(hist_up[i])
            else:  # cross
                long_exit_sig = bool(death[i])
                short_exit_sig = bool(golden[i])
            do_exit = False
            if state == 1:  # 正T持仓
                if params.trailing_tp:
                    peak_price = max(peak_price, price)
                # 亏损封顶：ATR/固定% 硬止损（vol_adaptive 时由 risk_dist 给出）
                eff_stop = stop_price
                # 固定止盈
                tp_hit = params.take_profit_pct > 0 and price >= entry_price * (
                    1 + params.take_profit_pct
                )
                # 追踪止盈：盈利超阈值后，从峰值回撤即离场（让赢家跑、封回吐）
                trail_hit = False
                if params.trailing_tp and peak_price >= entry_price * (
                    1 + params.trail_activate_pct
                ):
                    trail_stop = peak_price * (1 - params.trail_pct)
                    eff_stop = max(eff_stop, trail_stop)  # 只抬不止降
                    if price <= trail_stop:
                        trail_hit = True
                # 出场信号需缩量确认（量能过滤无量假突破）；硬止损/止盈不受量能约束
                exit_ok = long_exit_sig and (
                    not params.vol_confirm or vol_ratio_arr[i] <= (1.0 / params.vol_ratio)
                )
                if exit_ok or tp_hit or trail_hit:
                    do_exit = True
                elif price <= eff_stop:
                    do_exit = True
                elif (i - entry_idx) >= params.max_hold_bars and \
                        price <= entry_price * (1 + params.time_stop_eps):
                    do_exit = True
            else:  # 反T持仓（高位卖出后等低位接回）
                if params.trailing_tp:
                    trough_price = min(trough_price, price)
                eff_stop = stop_price
                tp_hit = params.take_profit_pct > 0 and price <= entry_price * (
                    1 - params.take_profit_pct
                )
                trail_hit = False
                if params.trailing_tp and trough_price <= entry_price * (
                    1 - params.trail_activate_pct
                ):
                    trail_stop = trough_price * (1 + params.trail_pct)
                    eff_stop = min(eff_stop, trail_stop)  # 只降不止抬
                    if price >= trail_stop:
                        trail_hit = True
                exit_ok = short_exit_sig and (
                    not params.vol_confirm or vol_ratio_arr[i] <= (1.0 / params.vol_ratio)
                )
                if exit_ok or tp_hit or trail_hit:
                    do_exit = True
                elif price >= eff_stop:
                    do_exit = True
                elif (i - entry_idx) >= params.max_hold_bars and \
                        price >= entry_price * (1 - params.time_stop_eps):
                    do_exit = True
            if do_exit:
                pnl, pnl_pct = _settle(state, entry_price, price, qty, params)
                trades.append(Trade(
                    code=code, day=day, side="long" if state == 1 else "short",
                    entry_idx=entry_idx, entry_price=entry_price,
                    exit_idx=i, exit_price=price, bars_held=i - entry_idx,
                    pnl=pnl, pnl_pct=pnl_pct, forced=False,
                ))
                state = 0
                cooldown = params.cooldown_bars
            continue

        # 3) 空仓：寻找入场（受当日锁定与冷却约束）
        if locked_day == day:
            continue
        if cooldown > 0:
            cooldown -= 1
            continue
        up = ema_slow[i] > ema_slow[i - 1]
        # 日线硬过滤：振幅/流动性不足整日禁开仓；趋势闸门决定可否做正T
        if daily_active:
            amp_ok, liq_ok, uptrend = gate_map.get(day, (False, False, False))
            if not (amp_ok and liq_ok):
                continue  # 振幅/流动性不足，整日不做T
            trend_up = uptrend if params.trend_filter else up
        else:
            trend_up = up
        move = abs(close[i] - close[i - 1]) / close[i - 1] if close[i - 1] else 0.0
        move_ok = params.min_entry_move_pct <= 0 or move >= params.min_entry_move_pct
        # 选信号源
        if params.signal_mode == "hist_turn":
            long_sig = bool(hist_turn_up[i]) and (not params.hist_turn_zeroline or dif[i] <= 0)
            short_sig = bool(hist_turn_down[i])
        elif params.signal_mode == "reversal":
            near_sup = price <= recent_low[i] * (1 + params.support_band_pct)
            near_res = price >= recent_high[i] * (1 - params.support_band_pct)
            long_sig = bool(near_sup and hist_up[i])
            short_sig = bool(near_res and hist_down[i])
        elif params.signal_mode == "divergence":
            long_sig = bool(div_bull[i])
            short_sig = bool(div_bear[i])
        else:  # cross
            long_sig = bool(golden[i])
            short_sig = bool(death[i])
        # 量能确认：正T入场需放量（当前量 > 均量×vol_ratio）
        long_vol_ok = (not params.vol_confirm) or (vol_ratio_arr[i] >= params.vol_ratio)
        # 更高周期方向偏置：仅允许与高周期方向一致的做T（正T需多、反T需空）
        if params.bias_filter:
            b = bias_arr[i]
            long_ok = (b == 1)
            short_ok = (b == -1)
        else:
            long_ok = True
            short_ok = True
        if long_sig and long_vol_ok and move_ok and long_ok and (not params.require_uptrend or trend_up):
            # 仅 cross / hist_turn 模式支持「支撑位附近」过滤（regime 模式已含位置信息，跳过）
            if params.signal_mode in ("cross", "hist_turn") and params.support_entry \
                    and not _near_support(price, sup_low[i], params.support_band_pct):
                pass  # 不在支撑位附近，放弃本次正T
            elif params.signal_mode == "cross" and params.zero_axis_filter == "below" and dif[i] > 0:
                pass  # 仅 0 轴下方金叉：当前在轴上方，放弃
            elif params.signal_mode == "cross" and params.zero_axis_filter == "above" and dif[i] <= 0:
                pass  # 仅 0 轴上方金叉：当前在轴下方，放弃
            else:
                state = 1
                entry_price, entry_idx = price, i
                peak_price = price
                qty, stop_price = _entry_qty(params, base_qty, price, atr[i], 1)
        elif (short_sig and move_ok and params.allow_reverse and short_ok
              and (not params.require_uptrend or not trend_up)):
            state = -1
            entry_price, entry_idx = price, i
            trough_price = price
            qty, stop_price = _entry_qty(params, base_qty, price, atr[i], -1)

    # 序列末尾若仍持仓（理论上已被当日 clear 清零），按末价平仓
    if state != 0:
        i = n - 1
        pnl, pnl_pct = _settle(state, entry_price, close[i], qty, params)
        trades.append(Trade(
            code=code, day=day_labels[i], side="long" if state == 1 else "short",
            entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
            exit_price=close[i], bars_held=i - entry_idx, pnl=pnl,
            pnl_pct=pnl_pct, forced=True,
        ))
    return trades


def _settle(state: int, entry_price: float, exit_price: float, qty: float,
            params: T0Params) -> tuple[float, float]:
    """结算一笔 T 回合的净盈亏与百分比。"""
    if entry_price <= 0 or exit_price <= 0:
        return 0.0, 0.0
    cost = params.cost_per_round_pct * entry_price * qty
    if state == 1:  # 正T：低买高卖
        gross = (exit_price - entry_price) * qty
        pct = (exit_price / entry_price - 1) * 100 - params.cost_per_round_pct * 100
    else:            # 反T：高卖低买
        gross = (entry_price - exit_price) * qty
        pct = (entry_price / exit_price - 1) * 100 - params.cost_per_round_pct * 100
    return gross - cost, pct


# ---------------------------------------------------------------------------
# 指标聚合 + 多标的回测
# ---------------------------------------------------------------------------
def aggregate_trades(trades: list[Trade]) -> dict:
    """把 T 回合列表聚合成可读指标体系。"""
    if not trades:
        return {
            "rounds": 0, "wins": 0, "win_rate": 0.0,
            "avg_net_pct": 0.0, "total_net_pct": 0.0,
            "total_pnl": 0.0, "forced": 0,
            "long_rounds": 0, "short_rounds": 0,
            "max_drawdown_pct": 0.0, "days_with_trade": 0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "win_loss_ratio": 0.0,
        }
    rounds = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    net = [t.pnl for t in trades]
    pcts = [t.pnl_pct for t in trades]
    cum = np.cumsum(net)
    peak = np.maximum.accumulate(cum)
    dd = float((peak - cum).max()) if len(cum) else 0.0
    # 赢亏比诊断：盈亏平衡要求 avg_win/avg_loss >= (1-win_rate)/win_rate
    win_pcts = [t.pnl_pct for t in trades if t.pnl > 0]
    loss_pcts = [t.pnl_pct for t in trades if t.pnl <= 0]
    avg_win = float(np.mean(win_pcts)) if win_pcts else 0.0
    avg_loss = abs(float(np.mean(loss_pcts))) if loss_pcts else 0.0
    wl_ratio = round(avg_win / avg_loss, 3) if avg_loss > 0 else 0.0
    # 以 base_qty=1 计，pnl 即「每底仓股」的累计 T 收益（元）；折算成百分比需基准价
    # 这里直接给出「每笔平均净百分比」与「回合累计净百分比」两个稳定量。
    days = {t.day for t in trades}
    return {
        "rounds": rounds,
        "wins": wins,
        "win_rate": round(wins / rounds * 100, 2),
        "avg_net_pct": round(float(np.mean(pcts)), 4),
        "total_net_pct": round(float(np.sum(pcts)), 4),
        "total_pnl": round(float(np.sum(net)), 6),
        "forced": sum(1 for t in trades if t.forced),
        "long_rounds": sum(1 for t in trades if t.side == "long"),
        "short_rounds": sum(1 for t in trades if t.side == "short"),
        "max_drawdown_pct": round(dd, 4),
        "days_with_trade": len(days),
        "avg_win_pct": round(avg_win, 4),
        "avg_loss_pct": round(avg_loss, 4),
        "win_loss_ratio": wl_ratio,
    }


def simulate_universe(
    codes: list[str], start: str, end: str, params: T0Params,
    max_stocks: Optional[int] = None, base_qty: float = 1.0,
) -> dict:
    """对一组标的在 [start, end] 区间内回测做T策略。

    Returns:
        dict: 含 aggregate（全样本指标）、per_stock（每只汇总）、coverage 等。
    """
    if max_stocks is not None:
        codes = codes[:max_stocks]
    all_trades: list[Trade] = []
    per_stock: list[dict] = []
    scanned = 0
    for code in codes:
        bars = _load_code_5m(code)
        if bars is None:
            continue
        scanned += 1
        trades = simulate_series(bars, params, code=code, base_qty=base_qty)
        in_range = [t for t in trades if start <= t.day <= end]
        if in_range:
            agg = aggregate_trades(in_range)
            per_stock.append({"code": code, **agg})
        all_trades.extend(in_range)

    agg = aggregate_trades(all_trades)
    per_stock.sort(key=lambda d: d["total_net_pct"], reverse=True)
    return {
        "params": {
            "fast": params.fast, "slow": params.slow, "signal": params.signal,
            "t_ratio": params.t_ratio, "require_uptrend": params.require_uptrend,
            "allow_reverse": params.allow_reverse,
            "cost_per_round_pct": params.cost_per_round_pct,
            "max_hold_bars": params.max_hold_bars, "stop_pct": params.stop_pct,
            "clear_time": params.clear_time,
            "min_entry_move_pct": params.min_entry_move_pct,
            "zero_axis_filter": params.zero_axis_filter,
            "trend_filter": params.trend_filter,
            "min_daily_amp_pct": params.min_daily_amp_pct,
            "min_daily_amount": params.min_daily_amount,
            "vol_adaptive": params.vol_adaptive,
            "risk_per_round_pct": params.risk_per_round_pct,
            "atr_period": params.atr_period,
            "atr_stop_mult": params.atr_stop_mult,
            "support_entry": params.support_entry,
            "support_lookback": params.support_lookback,
            "support_band_pct": params.support_band_pct,
            "signal_mode": params.signal_mode,
            "hist_turn_zeroline": params.hist_turn_zeroline,
            "vol_confirm": params.vol_confirm,
            "vol_ma_period": params.vol_ma_period,
            "vol_ratio": params.vol_ratio,
            "reversal_lookback": params.reversal_lookback,
            "divergence_lookback": params.divergence_lookback,
            "divergence_tol": params.divergence_tol,
            "take_profit_pct": params.take_profit_pct,
            "trailing_tp": params.trailing_tp,
            "trail_activate_pct": params.trail_activate_pct,
            "trail_pct": params.trail_pct,
            "bias_filter": params.bias_filter,
            "bias_tf": params.bias_tf,
            "bias_mode": params.bias_mode,
        },
        "start": start, "end": end,
        "scanned": scanned,
        "aggregate": agg,
        "per_stock": per_stock,
    }
