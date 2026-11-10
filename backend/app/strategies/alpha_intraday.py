# -*- coding: utf-8 -*-
"""与 MACD 动量**正交**的独立日内 alpha 校验模块（学习研究用途，不接实盘）。

背景
----
T0 MACD(6,38,6) 信号族经十六组对照（#139~#146）已被证伪：即便叠加纪律/regime 硬过滤/
信号源替换/盈亏比优化/60min 更高周期偏置，最优 K 仍每笔净 −0.0859%、赢亏比锁死 ~1.3x
（需 1.82x 才盈亏平衡）。根因是**信号源本身**——短周期 MACD 交叉/背离全是动量类，
在 A 股 5 分钟里赢亏不对称天然偏弱。

本模块转向**机制正交**的独立 alpha：全部仅用本地 5 分钟 OHLCV（open/high/low/close/
volume/amount，无筹码/L2/事件数据）派生，且四类信号与动量交叉零相关：

1. **VWAP 偏离均值回归**（axis="vwap"）：价格相对当日累计 VWAP 偏离超阈值即反向、
   回归 VWAP 平仓。机制=「公平价回归」，与动量完全正交。
2. **量价背离 / OBV 背离**（axis="obv"）：价格创新低但 OBV 不创新低→吸筹做多；
   价格创新高但 OBV 不创新高→派发做空。机制=「量能确认」，与 MACD 柱背离不同源。
3. **成交量异常冲击**（axis="volshock"）：成交量相对多日基线突增（非 20 根）作知情资金
   信号，配合当根方向做跟随。机制=「流动性异常」。
4. **开盘区间突破 / 波动扩张**（axis="orb"）：前 N 分钟区间突破 / 布林压缩后扩张。
   机制=「固定时段突破」，与 MACD 交叉不同周期尺度。

工程纪律（与 t0_macd 一致，保证可复现、无未来函数）：
- 指标在跨日连续 5 分钟序列上算，但 T 仓每日 ``clear_time``（默认 14:50）强制清零不过夜
  （符合 A 股 T+1：做T靠底仓）。
- 信号只用「当前及以前」数据；VWAP 用截至当前 bar 的日内累计额/量；OBV 用截至当前
  的累计；成交量基线用当前 bar **之前**的滚动窗口（shift1）；ORB 区间只用开盘前 N 根。
- 共用硬止损（−stop_pct）、时间止损（持仓超 max_hold_bars）、冷却（cooldown）、
  单笔成本（cost_per_round_pct）、T 仓比例（t_ratio）；可选 regime 硬闸（与 K 同款：
  前一日趋势/振幅/流动性决定当日）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from app.strategies.t0_macd import (
    Trade,
    _build_gate_map,
    _daily_context,
    _load_code_5m,
    _settle,
    aggregate_trades,
    clear_5m_cache,
)

__all__ = [
    "AlphaParams",
    "ComboParams",
    "simulate_series",
    "simulate_universe",
    "simulate_series_combo",
    "simulate_universe_combo",
    "generate_signals",
    "clear_5m_cache",
]


def _as_bool(x) -> np.ndarray:
    return np.asarray(x, dtype=bool)


@dataclass
class AlphaParams:
    """独立 alpha 参数。axis 选信号源；其余为共用纪律 + 各轴阈值。"""

    axis: str = "vwap"            # vwap / obv / volshock / orb
    # ---- 共用纪律（与 K 同基准，保证可比）----
    t_ratio: float = 0.30         # T 仓占底仓比例
    cost_per_round_pct: float = 0.0010  # 单笔往返成本 ≈0.10%
    max_hold_bars: int = 48       # 时间止损根数（默认放权给信号本身+硬止损，不靠短时限）
    stop_pct: float = 0.015       # 破位硬止损（反向 −1.5%）
    time_stop_eps: float = 0.0    # 时间止损「未涨」容差
    clear_time: str = "14:50"     # T 仓不过夜
    cooldown_bars: int = 1        # 每笔平仓后冷却
    allow_reverse: bool = True    # 允许反T（先卖后买）
    require_uptrend: bool = False  # 独立 alpha 默认不做「仅正T」限制
    # ---- regime 硬闸（可选，与 K 同款：用前一日信息决定当日）----
    trend_filter: bool = False
    min_daily_amp_pct: float = 0.0
    min_daily_amount: float = 0.0
    # ---- VWAP 均值回归 ----
    vwap_entry_pct: float = 0.005   # 偏离 VWAP 超 ±0.5% 触发反向
    vwap_exit_pct: float = 0.001    # 回归到 ±0.1% 内平仓
    # ---- OBV 背离 ----
    obv_lookback: int = 60          # 背离回看窗口（5 分钟 bar 数，≈半日）
    obv_tol: float = 0.003          # 价格新低/新高容差
    # ---- 成交量异常冲击 ----
    vs_baseline_bars: int = 240     # 基线窗口（≈5 个交易日）
    vs_ratio: float = 3.0           # 量能突增倍数
    vs_min_amount: float = 5e7      # 单根最低成交额门槛（过滤毛刺）
    # ---- 开盘区间突破 ----
    orb_bars: int = 6               # 开盘前 N 根（6=30 分钟）定义区间
    orb_trade_until: str = "14:30"  # 此后不再开新仓
    # ---- ATR 风险层（替代固定百分比硬止损，压缩 VWAP 等的小赢大亏尾）----
    atr_period: int = 14            # ATR 回看（5 分钟 bar 数）
    atr_stop_mult: float = 0.0      # >0 启用：硬止损 = entry ± ATR×mult（替代 stop_pct）


def default_params(axis: str = "vwap") -> AlphaParams:
    return AlphaParams(axis=axis)


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """5 分钟真实波幅 ATR（SMA），min_periods=1 避免前置 NaN 误剔。"""
    n = len(close)
    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    s = pd.Series(tr, dtype=float)
    return s.rolling(period, min_periods=1).mean().to_numpy(dtype=float)


# ---------------------------------------------------------------------------
# 信号生成（每个 axis 返回 4 个与 bars 等长的布尔数组）
#   enter_long / exit_long / enter_short / exit_short
# 全部只用「当前及以前」信息，无未来函数。
# ---------------------------------------------------------------------------
def _vwap_signals(bars: pd.DataFrame, p: AlphaParams):
    close = bars["close"].astype(float).to_numpy()
    vol = bars["volume"].astype(float).to_numpy()
    amt = bars["amount"].astype(float).to_numpy()
    day = pd.to_datetime(bars["date"]).dt.normalize()
    g = pd.DataFrame({"_d": day, "v": vol, "a": amt})
    cumvol = g.groupby("_d")["v"].cumsum().to_numpy(dtype=float)
    cumamt = g.groupby("_d")["a"].cumsum().to_numpy(dtype=float)
    # 截至当前 bar 的日内累计 VWAP（当前 close / 当前 VWAP 均在 bar 收盘时已知）
    vwap = np.divide(cumamt, cumvol, out=np.ones_like(cumamt), where=cumvol > 0)
    dev = close / vwap - 1.0
    el = dev <= -p.vwap_entry_pct
    es = dev >= p.vwap_entry_pct
    xl = dev >= -p.vwap_exit_pct
    xs = dev <= p.vwap_exit_pct
    return _as_bool(el), _as_bool(xl), _as_bool(es), _as_bool(xs)


def _obv_signals(bars: pd.DataFrame, p: AlphaParams):
    n = len(bars)
    close = bars["close"].astype(float).to_numpy()
    vol = bars["volume"].astype(float).to_numpy()
    # OBV：收涨加量、收跌减量（向量化）；obv[0]=0
    sign = np.zeros(n)
    sign[1:] = np.where(close[1:] > close[:-1], 1.0,
                       np.where(close[1:] < close[:-1], -1.0, 0.0))
    obv = np.cumsum(sign * vol)
    L = p.obv_lookback
    price_low = pd.Series(close).rolling(L, min_periods=1).min().to_numpy()
    price_high = pd.Series(close).rolling(L, min_periods=1).max().to_numpy()
    obv_min = pd.Series(obv).rolling(L, min_periods=1).min().to_numpy()
    obv_max = pd.Series(obv).rolling(L, min_periods=1).max().to_numpy()
    tol = p.obv_tol
    # 看多背离：价格处窗口低位但 OBV 不在低位（量能未确认下跌）→ 吸筹
    bull = (close <= price_low * (1 + tol)) & (obv > obv_min)
    # 看空背离：价格处窗口高位但 OBV 不在高位（量能未确认上涨）→ 派发
    bear = (close >= price_high * (1 - tol)) & (obv < obv_max)
    el, es, xl, xs = bull, bear, bear, bull
    return _as_bool(el), _as_bool(xl), _as_bool(es), _as_bool(xs)


def _volshock_signals(bars: pd.DataFrame, p: AlphaParams):
    n = len(bars)
    close = bars["close"].astype(float).to_numpy()
    op = bars["open"].astype(float).to_numpy()
    vol = bars["volume"].astype(float).to_numpy()
    amt = bars["amount"].astype(float).to_numpy()
    # 基线 = 当前 bar 之前的滚动均值（shift1，避免用到当前量）
    s = pd.Series(vol, dtype=float)
    baseline = s.shift(1).rolling(p.vs_baseline_bars, min_periods=1).mean().to_numpy()
    ratio = np.divide(vol, baseline, out=np.ones_like(baseline), where=baseline > 0)
    up = close > op  # 当根方向（收 > 开 = 买盘主导）
    shock = (ratio >= p.vs_ratio) & (amt >= p.vs_min_amount)
    el = shock & up
    es = shock & (~up)
    # 出场：出现反向冲击（量能反向爆发）即离场，否则交给定时/硬止损
    xl = shock & (~up)
    xs = shock & up
    return _as_bool(el), _as_bool(xl), _as_bool(es), _as_bool(xs)


def _orb_signals(bars: pd.DataFrame, p: AlphaParams):
    n = len(bars)
    close = bars["close"].astype(float).to_numpy()
    high = bars["high"].astype(float).to_numpy()
    low = bars["low"].astype(float).to_numpy()
    dt = pd.to_datetime(bars["date"])
    day = dt.dt.normalize()
    bar_min = (dt.dt.hour * 60 + dt.dt.minute).to_numpy()
    k = p.orb_bars
    # 每个交易日的开盘区间（前 k 根的高/低）
    dfx = pd.DataFrame({"_d": day, "h": high, "l": low})
    orb = dfx.groupby("_d").apply(
        lambda g: pd.Series({"oh": g["h"].iloc[:k].max(), "ol": g["l"].iloc[:k].min()}),
        include_groups=False,
    )
    orb_high = day.map(orb["oh"]).to_numpy(dtype=float)
    orb_low = day.map(orb["ol"]).to_numpy(dtype=float)
    rank = dfx.groupby("_d").cumcount().to_numpy()
    in_orb = rank < k
    tu_h, tu_m = (int(x) for x in p.orb_trade_until.split(":"))
    trade_min = tu_h * 60 + tu_m
    active = (~in_orb) & (bar_min <= trade_min)
    el = active & (close > orb_high)
    es = active & (close < orb_low)
    xl = active & (close <= orb_high)
    xs = active & (close >= orb_low)
    return _as_bool(el), _as_bool(xl), _as_bool(es), _as_bool(xs)


def generate_signals(bars: pd.DataFrame, p: AlphaParams):
    """按 axis 分发到对应信号生成器，返回 (enter_long, exit_long, enter_short, exit_short)。"""
    if p.axis == "vwap":
        return _vwap_signals(bars, p)
    if p.axis == "obv":
        return _obv_signals(bars, p)
    if p.axis == "volshock":
        return _volshock_signals(bars, p)
    if p.axis == "orb":
        return _orb_signals(bars, p)
    raise ValueError(f"unknown alpha axis: {p.axis}")


def _force_clear_mask(bars: pd.DataFrame, clear_time: str) -> np.ndarray:
    ts = pd.to_datetime(bars["date"])
    bar_min = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
    day_labels = np.array([str(x)[:10] for x in ts.to_numpy()])
    ch, cm = (int(x) for x in clear_time.split(":"))
    clear_min = ch * 60 + cm
    n = len(bars)
    fc = np.zeros(n, dtype=bool)
    for day in pd.unique(day_labels):
        idxs = np.where(day_labels == day)[0]
        if idxs.size == 0:
            continue
        hit = [j for j in idxs if bar_min[j] >= clear_min]
        fc[hit[0] if hit else idxs[-1]] = True
    return fc


# ---------------------------------------------------------------------------
# 共用 T+0 状态机（信号源无关）
# ---------------------------------------------------------------------------
def simulate_series(
    bars: pd.DataFrame, params: AlphaParams, code: str = "", base_qty: float = 1.0
) -> list[Trade]:
    """对一只股票的连续 5 分钟序列跑独立 alpha 状态机，返回所有完成的 T 回合。"""
    if bars is None or len(bars) < 30:
        return []
    df = bars.copy()
    df["date"] = pd.to_datetime(df["date"])
    n = len(df)
    close = df["close"].astype(float).to_numpy()
    ts = pd.to_datetime(df["date"])
    day_labels = np.array([str(x)[:10] for x in ts.to_numpy()])

    el, xl, es, xs = generate_signals(df, params)
    force_clear = _force_clear_mask(df, params.clear_time)

    # ATR 风险层（可选）：用 entry 时刻 ATR×mult 作硬止损，替代固定百分比
    use_atr = params.atr_stop_mult > 0
    if use_atr:
        atr = _atr(df["high"].astype(float).to_numpy(),
                   df["low"].astype(float).to_numpy(),
                   close, params.atr_period)

    # regime 硬闸（可选）
    gate_map = {}
    if params.trend_filter or params.min_daily_amp_pct > 0 or params.min_daily_amount > 0:
        gate_map = _build_gate_map(_daily_context(df), params)

    qty = base_qty * params.t_ratio
    trades: list[Trade] = []
    state = 0
    entry_price = 0.0
    entry_idx = -1
    atr_entry = 0.0
    cooldown = 0
    locked_day: Optional[str] = None

    for i in range(1, n):
        price = close[i]
        if price <= 0:
            continue
        day = day_labels[i]

        if state != 0 and force_clear[i]:
            pnl, pnl_pct = _settle(state, entry_price, price, qty, params)
            trades.append(Trade(code=code, day=day, side="long" if state == 1 else "short",
                                entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                                exit_price=price, bars_held=i - entry_idx, pnl=pnl,
                                pnl_pct=pnl_pct, forced=True))
            state = 0
            cooldown = params.cooldown_bars
            locked_day = day
            continue

        if state != 0:
            do_exit = False
            if state == 1:
                if xl[i]:
                    do_exit = True
                elif use_atr and price <= entry_price - atr_entry * params.atr_stop_mult:
                    do_exit = True
                elif (not use_atr) and price <= entry_price * (1 - params.stop_pct):
                    do_exit = True
                elif (i - entry_idx) >= params.max_hold_bars and \
                        price <= entry_price * (1 + params.time_stop_eps):
                    do_exit = True
            else:
                if xs[i]:
                    do_exit = True
                elif use_atr and price >= entry_price + atr_entry * params.atr_stop_mult:
                    do_exit = True
                elif (not use_atr) and price >= entry_price * (1 + params.stop_pct):
                    do_exit = True
                elif (i - entry_idx) >= params.max_hold_bars and \
                        price >= entry_price * (1 - params.time_stop_eps):
                    do_exit = True
            if do_exit:
                pnl, pnl_pct = _settle(state, entry_price, price, qty, params)
                trades.append(Trade(code=code, day=day, side="long" if state == 1 else "short",
                                    entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                                    exit_price=price, bars_held=i - entry_idx, pnl=pnl,
                                    pnl_pct=pnl_pct, forced=False))
                state = 0
                cooldown = params.cooldown_bars
            continue

        # 空仓：寻找入场
        if locked_day == day:
            continue
        if cooldown > 0:
            cooldown -= 1
            continue
        trend_up = True
        if gate_map:
            amp_ok, liq_ok, uptrend = gate_map.get(day, (False, False, False))
            if not (amp_ok and liq_ok):
                continue
            trend_up = uptrend if params.trend_filter else True
        if el[i] and (not params.require_uptrend or trend_up):
            state = 1
            entry_price, entry_idx = price, i
            atr_entry = atr[i] if use_atr else 0.0
        elif es[i] and params.allow_reverse and (not params.require_uptrend or not trend_up):
            state = -1
            entry_price, entry_idx = price, i
            atr_entry = atr[i] if use_atr else 0.0

    if state != 0:
        i = n - 1
        pnl, pnl_pct = _settle(state, entry_price, close[i], qty, params)
        trades.append(Trade(code=code, day=day_labels[i], side="long" if state == 1 else "short",
                            entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                            exit_price=close[i], bars_held=i - entry_idx, pnl=pnl,
                            pnl_pct=pnl_pct, forced=True))
    return trades


def simulate_universe(
    codes: list[str], start: str, end: str, params: AlphaParams,
    max_stocks: Optional[int] = None, base_qty: float = 1.0,
) -> dict:
    """对一组标的在 [start, end] 区间内回测独立 alpha 策略。"""
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
        "axis": params.axis,
        "params": {
            "axis": params.axis, "t_ratio": params.t_ratio,
            "cost_per_round_pct": params.cost_per_round_pct,
            "max_hold_bars": params.max_hold_bars, "stop_pct": params.stop_pct,
            "clear_time": params.clear_time, "cooldown_bars": params.cooldown_bars,
            "allow_reverse": params.allow_reverse, "require_uptrend": params.require_uptrend,
            "trend_filter": params.trend_filter,
            "min_daily_amp_pct": params.min_daily_amp_pct,
            "min_daily_amount": params.min_daily_amount,
            "vwap_entry_pct": params.vwap_entry_pct, "vwap_exit_pct": params.vwap_exit_pct,
            "obv_lookback": params.obv_lookback, "obv_tol": params.obv_tol,
            "vs_baseline_bars": params.vs_baseline_bars, "vs_ratio": params.vs_ratio,
            "vs_min_amount": params.vs_min_amount,
            "orb_bars": params.orb_bars, "orb_trade_until": params.orb_trade_until,
        },
        "start": start, "end": end, "scanned": scanned,
        "aggregate": agg, "per_stock": per_stock,
    }


# ===========================================================================
# 组合层：把多个「机制正交」的独立 alpha 合成一个执行体
#   - portfolio 模式：各轴作为独立 sleeve 同时运行、权益相加（研究用，假设等权资本）
#   - ensemble 模式：单仓，进出取各轴共识（OR），验证「多源共振」是否优于单源
# 共用纪律与 simulate_series 完全一致（无未来函数、T+1/14:50 强平）。
# ===========================================================================
@dataclass
class ComboParams:
    """组合 alpha 参数。axes 列出参与轴；combo_mode=ensemble|portfolio。"""
    axes: tuple = ("vwap", "orb")
    combo_mode: str = "ensemble"     # ensemble=单仓共识 / portfolio=多袖求和
    # ---- 共用纪律 ----
    t_ratio: float = 0.30
    cost_per_round_pct: float = 0.0010
    max_hold_bars: int = 48
    stop_pct: float = 0.015
    atr_period: int = 14
    atr_stop_mult: float = 0.0
    time_stop_eps: float = 0.0
    clear_time: str = "14:50"
    cooldown_bars: int = 1
    allow_reverse: bool = True
    require_uptrend: bool = False
    trend_filter: bool = False
    min_daily_amp_pct: float = 0.0
    min_daily_amount: float = 0.0
    # ---- 各轴阈值（直接复用 AlphaParams 默认，确保与单轴可比）----
    vwap_entry_pct: float = 0.005
    vwap_exit_pct: float = 0.001
    obv_lookback: int = 60
    obv_tol: float = 0.003
    vs_baseline_bars: int = 240
    vs_ratio: float = 3.0
    vs_min_amount: float = 5e7
    orb_bars: int = 6
    orb_trade_until: str = "14:30"


def _axis_params_from_combo(cp: "ComboParams", axis: str) -> "AlphaParams":
    return AlphaParams(
        axis=axis, t_ratio=cp.t_ratio, cost_per_round_pct=cp.cost_per_round_pct,
        max_hold_bars=cp.max_hold_bars, stop_pct=cp.stop_pct,
        atr_period=cp.atr_period, atr_stop_mult=cp.atr_stop_mult,
        time_stop_eps=cp.time_stop_eps, clear_time=cp.clear_time,
        cooldown_bars=cp.cooldown_bars, allow_reverse=cp.allow_reverse,
        require_uptrend=cp.require_uptrend, trend_filter=cp.trend_filter,
        min_daily_amp_pct=cp.min_daily_amp_pct, min_daily_amount=cp.min_daily_amount,
        vwap_entry_pct=cp.vwap_entry_pct, vwap_exit_pct=cp.vwap_exit_pct,
        obv_lookback=cp.obv_lookback, obv_tol=cp.obv_tol,
        vs_baseline_bars=cp.vs_baseline_bars, vs_ratio=cp.vs_ratio,
        vs_min_amount=cp.vs_min_amount, orb_bars=cp.orb_bars,
        orb_trade_until=cp.orb_trade_until,
    )


def simulate_series_combo(
    bars: pd.DataFrame, cp: "ComboParams", code: str = "", base_qty: float = 1.0
) -> list[Trade]:
    """组合层执行体。portfolio=各轴独立 sleeve 求和；ensemble=单仓共识 OR。"""
    if bars is None or len(bars) < 30:
        return []
    if cp.combo_mode == "portfolio":
        trades: list[Trade] = []
        for axis in cp.axes:
            trades.extend(simulate_series(
                bars, _axis_params_from_combo(cp, axis), code=code, base_qty=base_qty))
        return trades

    # ---- ensemble：单仓，共识进出 ----
    df = bars.copy()
    df["date"] = pd.to_datetime(df["date"])
    n = len(df)
    close = df["close"].astype(float).to_numpy()
    ts = pd.to_datetime(df["date"])
    day_labels = np.array([str(x)[:10] for x in ts.to_numpy()])

    # 各轴信号 → 共识（OR）
    el_all = np.zeros(n, dtype=bool)
    es_all = np.zeros(n, dtype=bool)
    xl_all = np.zeros(n, dtype=bool)
    xs_all = np.zeros(n, dtype=bool)
    for axis in cp.axes:
        el, xl, es, xs = generate_signals(df, _axis_params_from_combo(cp, axis))
        el_all |= el; es_all |= es; xl_all |= xl; xs_all |= xs

    force_clear = _force_clear_mask(df, cp.clear_time)
    use_atr = cp.atr_stop_mult > 0
    atr = None
    if use_atr:
        atr = _atr(df["high"].astype(float).to_numpy(),
                   df["low"].astype(float).to_numpy(), close, cp.atr_period)

    gate_map = {}
    if cp.trend_filter or cp.min_daily_amp_pct > 0 or cp.min_daily_amount > 0:
        gate_map = _build_gate_map(_daily_context(df), cp)

    qty = base_qty * cp.t_ratio
    trades = []
    state = 0
    entry_price = 0.0
    entry_idx = -1
    atr_entry = 0.0
    cooldown = 0
    locked_day: Optional[str] = None

    for i in range(1, n):
        price = close[i]
        if price <= 0:
            continue
        day = day_labels[i]

        if state != 0 and force_clear[i]:
            pnl, pnl_pct = _settle(state, entry_price, price, qty, cp)
            trades.append(Trade(code=code, day=day, side="long" if state == 1 else "short",
                                entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                                exit_price=price, bars_held=i - entry_idx, pnl=pnl,
                                pnl_pct=pnl_pct, forced=True))
            state = 0
            cooldown = cp.cooldown_bars
            locked_day = day
            continue

        if state != 0:
            do_exit = False
            if state == 1:
                if xl_all[i]:
                    do_exit = True
                elif use_atr and price <= entry_price - atr_entry * cp.atr_stop_mult:
                    do_exit = True
                elif (not use_atr) and price <= entry_price * (1 - cp.stop_pct):
                    do_exit = True
                elif (i - entry_idx) >= cp.max_hold_bars and \
                        price <= entry_price * (1 + cp.time_stop_eps):
                    do_exit = True
            else:
                if xs_all[i]:
                    do_exit = True
                elif use_atr and price >= entry_price + atr_entry * cp.atr_stop_mult:
                    do_exit = True
                elif (not use_atr) and price >= entry_price * (1 + cp.stop_pct):
                    do_exit = True
                elif (i - entry_idx) >= cp.max_hold_bars and \
                        price >= entry_price * (1 - cp.time_stop_eps):
                    do_exit = True
            if do_exit:
                pnl, pnl_pct = _settle(state, entry_price, price, qty, cp)
                trades.append(Trade(code=code, day=day, side="long" if state == 1 else "short",
                                    entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                                    exit_price=price, bars_held=i - entry_idx, pnl=pnl,
                                    pnl_pct=pnl_pct, forced=False))
                state = 0
                cooldown = cp.cooldown_bars
            continue

        # 空仓：寻找共识入场（多轴同向才进，冲突不开）
        if locked_day == day:
            continue
        if cooldown > 0:
            cooldown -= 1
            continue
        trend_up = True
        if gate_map:
            amp_ok, liq_ok, uptrend = gate_map.get(day, (False, False, False))
            if not (amp_ok and liq_ok):
                continue
            trend_up = uptrend if cp.trend_filter else True
        long_sig = el_all[i] and not es_all[i]
        short_sig = es_all[i] and not el_all[i]
        if long_sig and (not cp.require_uptrend or trend_up):
            state = 1
            entry_price, entry_idx = price, i
            atr_entry = atr[i] if use_atr else 0.0
        elif short_sig and cp.allow_reverse and (not cp.require_uptrend or not trend_up):
            state = -1
            entry_price, entry_idx = price, i
            atr_entry = atr[i] if use_atr else 0.0

    if state != 0:
        i = n - 1
        pnl, pnl_pct = _settle(state, entry_price, close[i], qty, cp)
        trades.append(Trade(code=code, day=day_labels[i], side="long" if state == 1 else "short",
                            entry_idx=entry_idx, entry_price=entry_price, exit_idx=i,
                            exit_price=close[i], bars_held=i - entry_idx, pnl=pnl,
                            pnl_pct=pnl_pct, forced=True))
    return trades


def simulate_universe_combo(
    codes: list[str], start: str, end: str, cp: "ComboParams",
    max_stocks: Optional[int] = None, base_qty: float = 1.0,
) -> dict:
    """对一组标的在 [start, end] 区间回测组合 alpha。"""
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
        trades = simulate_series_combo(bars, cp, code=code, base_qty=base_qty)
        in_range = [t for t in trades if start <= t.day <= end]
        if in_range:
            per_stock.append({"code": code, **aggregate_trades(in_range)})
        all_trades.extend(in_range)
    agg = aggregate_trades(all_trades)
    per_stock.sort(key=lambda d: d["total_net_pct"], reverse=True)
    return {
        "axes": cp.axes, "combo_mode": cp.combo_mode,
        "params": {
            "axes": list(cp.axes), "combo_mode": cp.combo_mode,
            "t_ratio": cp.t_ratio, "cost_per_round_pct": cp.cost_per_round_pct,
            "max_hold_bars": cp.max_hold_bars, "stop_pct": cp.stop_pct,
            "atr_stop_mult": cp.atr_stop_mult, "clear_time": cp.clear_time,
            "cooldown_bars": cp.cooldown_bars, "allow_reverse": cp.allow_reverse,
            "require_uptrend": cp.require_uptrend, "trend_filter": cp.trend_filter,
        },
        "start": start, "end": end, "scanned": scanned,
        "aggregate": agg, "per_stock": per_stock,
    }
