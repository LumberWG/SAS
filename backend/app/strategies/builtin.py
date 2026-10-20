"""内置经典策略库。

设计借鉴 Sequoia-X 的 6 种经典选股策略，全部用 Pandas 实现。
每个策略只看截至信号日的数据，不使用未来信息（避免未来函数）。
df 已由 add_all_indicators 附加指标列（ma5/ma10/ma20/dif/dea/macd_hist/
kdj_k/kdj_d/kdj_j/boll_*/rsi*/bias*/atr/vol_ratio）。
"""
from __future__ import annotations

import pandas as pd

from app.strategies.base import Signal, Strategy
from app.strategies.registry import registry


@registry.register
class TurtleBreakout(Strategy):
    """海龟突破：收盘价创 20 日新高 + 成交额过亿 + 阳线。

    逻辑（唐奇安通道上轨突破）:
      1) 收盘价 = 最近 20 日收盘最高价（向上突破，趋势启动信号）；
      2) 成交额 > 1 亿元（保证流动性，过滤冷门股）；
      3) 收盘 > 开盘（阳线，确认买盘主导）。
    """

    name = "海龟突破"
    description = "收盘价创20日新高，成交额过亿且收阳线，捕捉趋势启动"
    category = "趋势突破"
    min_bars = 30

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        last = df.iloc[-1]
        if len(df) < self.min_bars:
            return None

        high_20 = df["close"].tail(20).max()
        is_breakout = last["close"] >= high_20          # 创20日收盘新高
        amount_ok = (last.get("amount") or 0) > 1e8     # 成交额过亿
        is_yang = last["close"] > last["open"]          # 阳线

        if not (is_breakout and amount_ok and is_yang):
            return None

        stop, target = self.default_stop_target(df, lookback=10)
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"收盘 {last['close']:.2f} 突破20日新高 {high_20:.2f}，"
                   f"成交额 {last['amount'] / 1e8:.2f} 亿，阳线确认",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"amount_yi": round(last["amount"] / 1e8, 2),
                   "vol_ratio": _safe(last.get("vol_ratio"))},
        )


@registry.register
class MaVolumeBreakout(Strategy):
    """均线放量突破：均线多头排列 + 放量突破。

    逻辑:
      1) MA5 > MA10 > MA20（多头排列，上升趋势成立）；
      2) 收盘价站上 5 日线且 5 日线在 20 日线上方（强势）；
      3) 量比 > 1.5（放量，资金关注度提升）。
    """

    name = "均线放量"
    description = "MA5>MA10>MA20多头排列，收盘站上5日线且放量"
    category = "趋势突破"
    min_bars = 30

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]

        bullish = last["ma5"] > last["ma10"] > last["ma20"]   # 多头排列
        above_ma5 = last["close"] > last["ma5"]               # 站上5日线
        volume_up = _safe(last.get("vol_ratio"), 0) > 1.5     # 放量

        if not (bullish and above_ma5 and volume_up):
            return None

        stop, target = self.default_stop_target(df, lookback=10)
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"均线多头排列(MA5={last['ma5']:.2f}>MA10={last['ma10']:.2f}"
                   f">MA20={last['ma20']:.2f})，放量{_safe(last.get('vol_ratio')):.1f}倍",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"ma5": round(last["ma5"], 2), "ma10": round(last["ma10"], 2),
                   "ma20": round(last["ma20"], 2)},
        )


@registry.register
class MacdGoldenCross(Strategy):
    """MACD 金叉：DIF 上穿 DEA，且 MACD 柱由负转正。

    逻辑:
      1) 前一日 DIF <= DEA，当日 DIF > DEA（金叉）；
      2) 当日 MACD 柱 > 0（红柱出现，动量转多）。
    金叉发生在零轴下方时为低位金叉，反弹信号更强。
    """

    name = "MACD金叉"
    description = "DIF上穿DEA且MACD红柱出现，动量转多信号"
    category = "指标信号"
    min_bars = 40

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < 3:
            return None
        prev, last = df.iloc[-2], df.iloc[-1]

        cross_up = prev["dif"] <= prev["dea"] and last["dif"] > last["dea"]
        hist_positive = last["macd_hist"] > 0

        if not (cross_up and hist_positive):
            return None

        stop, target = self.default_stop_target(df, lookback=10)
        position = "零轴上方" if last["dif"] > 0 else "零轴下方"
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"DIF({last['dif']:.3f})上穿DEA({last['dea']:.3f})，"
                   f"{position}金叉，MACD柱 {last['macd_hist']:.3f}",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"dif": round(last["dif"], 3), "dea": round(last["dea"], 3),
                   "macd_hist": round(last["macd_hist"], 3)},
        )


@registry.register
class KdjOversoldCross(Strategy):
    """KDJ 超卖金叉：J 值进入超卖区后 K 上穿 D。

    逻辑:
      1) 当日 J < 20（超卖区，短期跌幅过大）；
      2) K 值上穿 D 值（金叉，跌势衰竭信号）。
    属于左侧博弈反转的策略，配合止损使用。
    """

    name = "KDJ超卖金叉"
    description = "J值低于20超卖区且K上穿D，捕捉短期反弹"
    category = "指标信号"
    min_bars = 30

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < 3:
            return None
        prev, last = df.iloc[-2], df.iloc[-1]

        oversold = last["kdj_j"] < 20            # 超卖区
        cross_up = prev["kdj_k"] <= prev["kdj_d"] and last["kdj_k"] > last["kdj_d"]

        if not (oversold and cross_up):
            return None

        stop, target = self.default_stop_target(df, lookback=10)
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"J={last['kdj_j']:.1f} 超卖，K({last['kdj_k']:.1f})"
                   f"上穿D({last['kdj_d']:.1f})",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"k": round(last["kdj_k"], 1), "d": round(last["kdj_d"], 1),
                   "j": round(last["kdj_j"], 1)},
        )


@registry.register
class MomentumBreakout(Strategy):
    """动量突破（RPS 简化版）：中期涨幅居前且接近新高。

    欧奈尔 RPS 思路: 相对强度排名靠前的股票有强者恒强效应。
    单股无排名信息，用绝对涨幅近似:
      1) 60 日涨幅 >= 30%（中期动量充足）；
      2) 收盘价不低于 60 日最高价的 95%（贴近新高，未大幅回落）。
    """

    name = "动量突破"
    description = "60日涨幅≥30%且贴近60日新高，强者恒强动量选股"
    category = "趋势突破"
    min_bars = 70

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]

        ret_60 = (last["close"] / df["close"].iloc[-61] - 1) * 100
        high_60 = df["close"].tail(60).max()
        near_high = last["close"] >= high_60 * 0.95

        if not (ret_60 >= 30 and near_high):
            return None

        stop, target = self.default_stop_target(df, lookback=20)
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"60日涨幅 {ret_60:.1f}%，贴近60日新高 {high_60:.2f}",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"ret_60": round(ret_60, 1), "high_60": round(high_60, 2)},
        )


@registry.register
class PlatformBreakout(Strategy):
    """平台突破：长期横盘整理后放量向上突破。

    逻辑:
      1) 此前 20 个交易日（不含今日）振幅 < 15%（缩量横盘蓄势）；
      2) 今日收盘价突破前 20 日最高价（平台破位向上）；
      3) 今日成交量 > 前 20 日均量（放量突破，有效性确认）。
    """

    name = "平台突破"
    description = "20日横盘整理后放量突破平台上沿，捕捉主升浪起点"
    category = "趋势突破"
    min_bars = 30

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]
        # 平台期 = 截至昨日的 20 个交易日（不含今日）
        platform = df.iloc[-21:-1]
        if len(platform) < 20:
            return None

        p_high, p_low = platform["high"].max(), platform["low"].min()
        amplitude = (p_high - p_low) / p_low * 100      # 平台振幅%
        breakout = last["close"] > p_high               # 突破平台上沿
        avg_vol = platform["volume"].mean()
        volume_confirm = last["volume"] > avg_vol       # 放量确认

        if not (amplitude < 15 and breakout and volume_confirm):
            return None

        stop, target = self.default_stop_target(df, lookback=20)
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"20日平台振幅仅 {amplitude:.1f}%，放量突破平台上沿 {p_high:.2f}",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"platform_amplitude": round(amplitude, 1),
                   "platform_high": round(p_high, 2)},
        )


def _safe(value, default=None):
    """安全取值：NaN/None → default。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    return value
