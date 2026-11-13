"""广发证券《交易性择时策略研究之十五》8 个经典技术指标择时。

每个函数输入含 OHLCV 的 DataFrame，返回 0/1 持仓序列（1=看多/持仓，0=看空/空仓），
逻辑统一为"上穿→持仓、下穿→空仓"，与原始仓库的 generate_signals 一致。
所有计算仅用历史数据，无未来函数。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------- 基础工具
def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 上穿 b（前一根 <=，本根 >）。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 下穿 b（前一根 >=，本根 <）。"""
    return (a < b) & (a.shift(1) >= b.shift(1))


def _position_from_cross(cross_up: pd.Series, cross_down: pd.Series) -> pd.Series:
    """由上穿/下穿布尔序列推出 0/1 持仓（穿越后保持直至反向穿越）。"""
    pos = pd.Series(0, index=cross_up.index, dtype=int)
    cur = 0
    cu = np.asarray(cross_up, dtype=bool)
    cd = np.asarray(cross_down, dtype=bool)
    for i in range(len(pos)):
        if cu[i]:
            cur = 1
        elif cd[i]:
            cur = 0
        pos.iloc[i] = cur
    return pos


# ---------------------------------------------------------------------- 1) VWAP
def vwap_position(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """成交量加权平均价。Typical=(H+L+C)/3；VWAP=Σ(MF)/Σ(V)；收盘价上穿/下穿 VWAP。"""
    typ = (df["high"] + df["low"] + df["close"]) / 3
    mf = df["volume"] * typ
    vwap = mf.rolling(period).sum() / df["volume"].rolling(period).sum()
    return _position_from_cross(_cross_up(df["close"], vwap), _cross_down(df["close"], vwap))


# ---------------------------------------------------------------------- 2) DPO
def dpo_position(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """去趋势价格震荡。DPO = CLOSE - REF(MA(CLOSE,n), n//2+1)；DPO 上穿/下穿 0。"""
    ma = _sma(df["close"], n)
    dpo = df["close"] - ma.shift(n // 2 + 1)
    zero = pd.Series(0.0, index=df.index)
    return _position_from_cross(_cross_up(dpo, zero), _cross_down(dpo, zero))


# ---------------------------------------------------------------------- 3) ER (Elder Ray)
def er_position(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """BullPower=H-EMA(C,n)；BearPower=L-EMA(C,n)。
    买入：BullPower>0 且 BearPower>前值；卖出：BearPower<0 且 BullPower<前值。"""
    ema = _ema(df["close"], n)
    bull = df["high"] - ema
    bear = df["low"] - ema
    up = (bull > 0) & (bear > bear.shift(1))
    down = (bear < 0) & (bull < bull.shift(1))
    return _position_from_cross(up, down)


# ---------------------------------------------------------------------- 4) TII (Trend Intensity Index)
def tii_position(df: pd.DataFrame, n1: int = 40, n2: int = 9) -> pd.Series:
    """TII = 100·Σmax(H-前收,0) / (Σmax(H-前收,0)+Σmax(前收-L,0))；TII 上穿/下穿其 MA(n2) 信号线。"""
    pc = df["close"].shift(1)
    up = (df["high"] - pc).clip(lower=0)
    dn = (pc - df["low"]).clip(lower=0)
    up_r = up.rolling(n1).sum()
    dn_r = dn.rolling(n1).sum()
    tii = 100 * up_r / (up_r + dn_r + 1e-12)
    sig = _sma(tii, n2)
    return _position_from_cross(_cross_up(tii, sig), _cross_down(tii, sig))


# ---------------------------------------------------------------------- 5) PO (Price Oscillator)
def po_position(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """价格震荡：PO = EMA(fast)-EMA(slow)；PO 上穿/下穿其 MA(signal) 信号线。"""
    po = _ema(df["close"], fast) - _ema(df["close"], slow)
    sig = _sma(po, signal)
    return _position_from_cross(_cross_up(po, sig), _cross_down(po, sig))


# ---------------------------------------------------------------------- 6) MADisplaced
def madisplaced_position(df: pd.DataFrame, n: int = 20, m: int = 10) -> pd.Series:
    """位移均线：MA(C,n) 向右位移 m 期；收盘价上穿/下穿位移均线。"""
    displaced = _sma(df["close"], n).shift(m)
    return _position_from_cross(_cross_up(df["close"], displaced), _cross_down(df["close"], displaced))


# ---------------------------------------------------------------------- 7) MAAMT
def maamt_position(df: pd.DataFrame, n: int = 40) -> pd.Series:
    """成交额均线：amount=V·C（近似）；成交额上穿/下穿其 N 日均值。"""
    amount = df["volume"] * df["close"]
    maamt = amount.rolling(n).mean()
    return _position_from_cross(_cross_up(amount, maamt), _cross_down(amount, maamt))


# ---------------------------------------------------------------------- 8) SuperTrend
def _supertrend_direction(df: pd.DataFrame, atr_period: int = 10, mult: float = 3.0) -> pd.Series:
    """返回 1(上升)/-1(下降) 的趋势方向序列（ATR 基于经典 True Range）。"""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()
    hl_avg = (df["high"] + df["low"]) / 2
    basic_ub = hl_avg.to_numpy() + (mult * atr).to_numpy()
    basic_lb = hl_avg.to_numpy() - (mult * atr).to_numpy()
    close = df["close"].to_numpy()

    n = len(df)
    fu = np.empty(n, dtype=float)
    fl = np.empty(n, dtype=float)
    fd = np.empty(n, dtype=int)
    # ATR 前 atr_period-1 行为 NaN：warmup 期间直接用基础带、方向置空仓，避免 NaN 永久传播
    valid = ~np.isnan(atr.to_numpy())
    k = int(np.argmax(valid)) if valid.any() else n
    for i in range(n):
        if i < k:
            fu[i] = basic_ub[i]
            fl[i] = basic_lb[i]
            fd[i] = -1
            continue
        if i == k:
            fu[i] = basic_ub[i]
            fl[i] = basic_lb[i]
        else:
            fu[i] = basic_ub[i] if (basic_ub[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = basic_lb[i] if (basic_lb[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if i == k:
            fd[i] = 1 if close[i] > fl[i] else -1
        else:
            if fd[i - 1] == 1:
                fd[i] = -1 if close[i] <= fl[i] else 1
            else:
                fd[i] = 1 if close[i] >= fu[i] else -1
    return pd.Series(fd, index=df.index)


def supertrend_position(df: pd.DataFrame, atr_period: int = 10, mult: float = 3.0) -> pd.Series:
    """SuperTrend 方向：上升趋势(1)持仓，下降趋势(-1)空仓。"""
    direction = _supertrend_direction(df, atr_period, mult)
    return (direction == 1).astype(int)


# ---------------------------------------------------------------------- 合成
ALL_INDICATORS = [
    vwap_position, dpo_position, er_position, tii_position,
    po_position, madisplaced_position, maamt_position, supertrend_position,
]


def composite_position(df: pd.DataFrame, threshold: int = 5) -> pd.Series:
    """8 指标多数投票：>=threshold 个看多则看多（默认过半=5）。"""
    total = pd.Series(0, index=df.index, dtype=int)
    for f in ALL_INDICATORS:
        total = total + f(df).fillna(0).astype(int)
    return (total >= threshold).astype(int)
