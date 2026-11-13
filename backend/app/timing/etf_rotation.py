"""#5 ETF 轮动层（创业板 regime 闸门）。

robertquant/quant-research 的 ETF 轮动核心：30 日动量 + 60 日均线过滤（+ 原版 -8% 硬止损，
止损属持仓管理、非入场闸门，这里只取"是否处于可交易 regime"）。
作用在创业板指(sz399006) 上，作为创业板个股策略的板块级闸门（原版用创业板ETF 159915，
本系统无该 ETF 数据，创业板指更直接且数据完整）。

默认：创业板 close > 60MA 且 30 日动量 > 0 → 放行。
可选 cross_sectional：额外要求创业板 30 日动量 > 沪深300 30 日动量（创业板领涨）。
"""
from __future__ import annotations

import pandas as pd

from app.timing.indicators import _sma
from app.timing.loader import load_index


def etf_rotation_gate(
    mom_window: int = 30,
    ma_window: int = 60,
    cross_sectional: bool = False,
    kline_dir: str | None = None,
) -> set[str]:
    """返回创业板处于多头 regime 的允许交易日集合。"""
    df = load_index("sz399006", kline_dir)
    mom = df["close"] / df["close"].shift(mom_window) - 1.0
    ma = _sma(df["close"], ma_window)
    bull = (mom > 0) & (df["close"] > ma)

    if cross_sectional:
        df3 = load_index("sh000300", kline_dir)
        mom3 = df3["close"] / df3["close"].shift(mom_window) - 1.0
        # 对齐日期
        s_cyb = pd.Series(bull.values, index=df["date"])
        s_hs3 = pd.Series(mom3.values, index=df3["date"])
        mom_cyb = pd.Series(mom.values, index=df["date"])
        aligned = pd.concat([s_cyb.rename("bull"), mom_cyb.rename("mom_cyb"),
                             s_hs3.rename("mom_hs3")], axis=1).dropna()
        allowed_idx = aligned.index[(aligned["bull"]) & (aligned["mom_cyb"] > aligned["mom_hs3"])]
        return set(allowed_idx.tolist())

    return set(df["date"][bull].tolist())
