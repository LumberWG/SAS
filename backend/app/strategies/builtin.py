"""内置经典策略库（全部参数可调）。

设计借鉴 Sequoia-X 的 6 种经典选股策略，全部用 Pandas 实现。
每个策略只看截至信号日的数据，不使用未来信息（避免未来函数）。
df 已由 add_all_indicators 附加指标列（ma5/ma10/ma20/dif/dea/macd_hist/
kdj_k/kdj_d/kdj_j/boll_*/rsi*/bias*/atr/vol_ratio）。

参数化约定:
  - 每个策略通过 parameters 声明可调参数（ParamSpec）；
  - evaluate 中一律通过 self.param("xxx") 读取，不得再硬编码常量；
  - 未传参数时取默认值，行为与旧版完全一致。
"""
from __future__ import annotations

import pandas as pd

from app.strategies.base import ParamSpec, Signal, Strategy
from app.strategies.registry import registry


@registry.register
class TurtleBreakout(Strategy):
    """海龟突破：收盘价创 N 日新高 + 成交额过阈 + 阳线。

    逻辑（唐奇安通道上轨突破）:
      1) 收盘价 = 最近 N 日收盘最高价（向上突破，趋势启动信号）；
      2) 成交额 > 阈值（保证流动性，过滤冷门股）；
      3) 收盘 > 开盘（阳线，确认买盘主导，可关闭）。
    """

    name = "海龟突破"
    description = "收盘价创N日新高，成交额过阈且收阳线，捕捉趋势启动"
    category = "趋势突破"
    min_bars = 30

    parameters = {
        "breakout_period": ParamSpec(
            type="int", default=20, label="突破周期（创N日收盘新高）",
            min=5, max=120, step=1, unit="日",
        ),
        "min_amount_yi": ParamSpec(
            type="float", default=1.0, label="最低成交额",
            min=0.1, max=50, step=0.5, unit="亿",
        ),
        "require_yang": ParamSpec(
            type="bool", default=True, label="要求阳线（防诱多）",
        ),
        "stop_lookback": ParamSpec(
            type="int", default=10, label="止损回看天数",
            min=3, max=30, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        last = df.iloc[-1]
        if len(df) < self.min_bars:
            return None

        period = int(self.param("breakout_period"))
        high_n = df["close"].tail(period).max()
        is_breakout = last["close"] >= high_n                    # 创N日收盘新高
        amount_ok = (last.get("amount") or 0) > float(self.param("min_amount_yi")) * 1e8
        is_yang = last["close"] > last["open"]                   # 阳线
        yang_ok = is_yang or not self.param("require_yang")      # 关闭时跳过阳线条件

        if not (is_breakout and amount_ok and yang_ok):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"收盘 {last['close']:.2f} 突破{period}日新高 {high_n:.2f}，"
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
      3) 量比 > 阈值（放量，资金关注度提升）。
    """

    name = "均线放量"
    description = "MA5>MA10>MA20多头排列，收盘站上5日线且放量"
    category = "趋势突破"
    min_bars = 30

    parameters = {
        "vol_ratio_min": ParamSpec(
            type="float", default=1.5, label="最低量比（放量阈值）",
            min=0.5, max=10, step=0.1,
        ),
        "stop_lookback": ParamSpec(
            type="int", default=10, label="止损回看天数",
            min=3, max=30, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]

        bullish = last["ma5"] > last["ma10"] > last["ma20"]   # 多头排列
        above_ma5 = last["close"] > last["ma5"]               # 站上5日线
        volume_up = _safe(last.get("vol_ratio"), 0) > float(self.param("vol_ratio_min"))

        if not (bullish and above_ma5 and volume_up):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
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
      2) 当日 MACD 柱 > 0（红柱出现，动量转多，可关闭）；
      3) 可选只保留零轴下方（低位）金叉，反弹信号更强。
    金叉发生在零轴下方时为低位金叉，反弹信号更强。
    """

    name = "MACD金叉"
    description = "DIF上穿DEA且MACD红柱出现，动量转多信号"
    category = "指标信号"
    min_bars = 40

    parameters = {
        "require_hist_positive": ParamSpec(
            type="bool", default=True, label="要求 MACD 红柱（柱>0）",
        ),
        "zero_axis_filter": ParamSpec(
            type="select", default="any", label="金叉位置过滤",
            options=[("any", "不限"), ("below", "仅零轴下方（低位金叉）"),
                     ("above", "仅零轴上方（强势金叉）")],
        ),
        "stop_lookback": ParamSpec(
            type="int", default=10, label="止损回看天数",
            min=3, max=30, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < 3:
            return None
        prev, last = df.iloc[-2], df.iloc[-1]

        cross_up = prev["dif"] <= prev["dea"] and last["dif"] > last["dea"]
        hist_positive = last["macd_hist"] > 0
        hist_ok = hist_positive or not self.param("require_hist_positive")

        # 零轴位置过滤
        axis = self.param("zero_axis_filter")
        if axis == "below" and last["dif"] > 0:
            return None
        if axis == "above" and last["dif"] <= 0:
            return None

        if not (cross_up and hist_ok):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
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
      1) 当日 J < 阈值（超卖区，短期跌幅过大）；
      2) K 值上穿 D 值（金叉，跌势衰竭信号）。
    属于左侧博弈反转的策略，配合止损使用。
    """

    name = "KDJ超卖金叉"
    description = "J值低于阈值超卖区且K上穿D，捕捉短期反弹"
    category = "指标信号"
    min_bars = 30

    parameters = {
        "oversold_threshold": ParamSpec(
            type="float", default=20, label="超卖 J 值阈值",
            min=5, max=40, step=1,
        ),
        "require_cross": ParamSpec(
            type="bool", default=True, label="要求 K 上穿 D（金叉）",
        ),
        "stop_lookback": ParamSpec(
            type="int", default=10, label="止损回看天数",
            min=3, max=30, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < 3:
            return None
        prev, last = df.iloc[-2], df.iloc[-1]

        oversold = last["kdj_j"] < float(self.param("oversold_threshold"))  # 超卖区
        cross_up = prev["kdj_k"] <= prev["kdj_d"] and last["kdj_k"] > last["kdj_d"]
        cross_ok = cross_up or not self.param("require_cross")

        if not (oversold and cross_ok):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
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
      1) N 日涨幅 >= 阈值%（中期动量充足）；
      2) 收盘价不低于 N 日最高价的 比例%（贴近新高，未大幅回落）。
    """

    name = "动量突破"
    description = "60日涨幅≥30%且贴近60日新高，强者恒强动量选股"
    category = "趋势突破"
    min_bars = 70

    parameters = {
        "momentum_days": ParamSpec(
            type="int", default=60, label="动量周期",
            min=20, max=250, step=5, unit="日",
        ),
        "momentum_min_pct": ParamSpec(
            type="float", default=30, label="最低涨幅",
            min=5, max=200, step=5, unit="%",
        ),
        "near_high_pct": ParamSpec(
            type="float", default=95, label="贴近新高程度",
            min=80, max=100, step=1, unit="%",
        ),
        "stop_lookback": ParamSpec(
            type="int", default=20, label="止损回看天数",
            min=3, max=60, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]

        days = int(self.param("momentum_days"))
        ret_n = (last["close"] / df["close"].iloc[-(days + 1)] - 1) * 100
        high_n = df["close"].tail(days).max()
        near_high = last["close"] >= high_n * float(self.param("near_high_pct")) / 100

        if not (ret_n >= float(self.param("momentum_min_pct")) and near_high):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"{days}日涨幅 {ret_n:.1f}%，贴近{days}日新高 {high_n:.2f}",
            stop_loss=stop, target_price=target,
            date=str(df["date"].iloc[-1]),
            extra={"ret_60": round(ret_n, 1), "high_60": round(high_n, 2)},
        )


@registry.register
class PlatformBreakout(Strategy):
    """平台突破：长期横盘整理后放量向上突破。

    逻辑:
      1) 此前 N 个交易日（不含今日）振幅 < 阈值%（缩量横盘蓄势）；
      2) 今日收盘价突破前 N 日最高价（平台破位向上）；
      3) 今日成交量 > 前 N 日均量（放量突破，有效性确认，可关闭）。
    """

    name = "平台突破"
    description = "20日横盘整理后放量突破平台上沿，捕捉主升浪起点"
    category = "趋势突破"
    min_bars = 30

    parameters = {
        "platform_days": ParamSpec(
            type="int", default=20, label="平台周期",
            min=5, max=60, step=1, unit="日",
        ),
        "max_amplitude_pct": ParamSpec(
            type="float", default=15, label="平台最大振幅",
            min=3, max=50, step=1, unit="%",
        ),
        "require_volume": ParamSpec(
            type="bool", default=True, label="要求放量确认",
        ),
        "stop_lookback": ParamSpec(
            type="int", default=20, label="止损回看天数",
            min=3, max=60, step=1, unit="日",
        ),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        last = df.iloc[-1]
        # 平台期 = 截至昨日的 N 个交易日（不含今日）
        days = int(self.param("platform_days"))
        platform = df.iloc[-(days + 1):-1]
        if len(platform) < days:
            return None

        p_high, p_low = platform["high"].max(), platform["low"].min()
        amplitude = (p_high - p_low) / p_low * 100      # 平台振幅%
        breakout = last["close"] > p_high               # 突破平台上沿
        avg_vol = platform["volume"].mean()
        volume_confirm = last["volume"] > avg_vol       # 放量确认
        volume_ok = volume_confirm or not self.param("require_volume")

        if not (amplitude < float(self.param("max_amplitude_pct")) and breakout and volume_ok):
            return None

        stop, target = self.default_stop_target(df, lookback=int(self.param("stop_lookback")))
        return Signal(
            code="", name="", strategy=self.name, price=float(last["close"]),
            reason=f"{days}日平台振幅仅 {amplitude:.1f}%，放量突破平台上沿 {p_high:.2f}",
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
