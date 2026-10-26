"""第 2 次排序：自选股多因子加权评分（14 因子 / 100 分制）。

与 ``analysis/scoring.py`` 的关系:
  - ``score_technical`` 是 ±8 的「技术面快评」，供个股页 LLM 仪表盘使用；
  - 本模块是「自选股再排序」的完整模型：每个因子先归一化到 [-1, +1]，
    再按权重（合计 100）加权求和，总分落在 [-100, +100]。

分级（默认阈值 ±15）:
  total >=  15  → 买入建议
  total <= -15  → 卖出建议
  其他           → 持有观察

因子分组与权重（合计 100）:
  A 趋势 30%：均线排列 12 / 中期斜率 10 / 长期位置 8
  B 动量 25%：MACD 10 / RSI 8 / 20日涨幅 7
  C 量能 20%：量比 8 / 量能趋势 7 / 换手 5
  D 风险 15%：波动率 5 / 回撤 5 / 乖离 5
  E 位置 10%：突破 6 / 布林位置 4

调整权重或阈值时，请同步 ``tests/test_factors.py`` 与 README「智能建议」章节。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

#: 因子权重（百分比，合计 100）
FACTOR_WEIGHTS: dict[str, float] = {
    # A 趋势
    "ma_trend": 12.0,
    "ma_slope": 10.0,
    "long_position": 8.0,
    # B 动量
    "macd": 10.0,
    "rsi": 8.0,
    "gain20": 7.0,
    # C 量能
    "vol_ratio": 8.0,
    "vol_trend": 7.0,
    "turnover": 5.0,
    # D 风险
    "atr": 5.0,
    "drawdown": 5.0,
    "bias": 5.0,
    # E 位置
    "breakout": 6.0,
    "boll": 4.0,
}

#: 因子中文名与分组（前端展示用）
FACTOR_LABELS: dict[str, tuple[str, str]] = {
    "ma_trend": ("均线排列", "趋势"),
    "ma_slope": ("中期斜率", "趋势"),
    "long_position": ("长期位置", "趋势"),
    "macd": ("MACD 强度", "动量"),
    "rsi": ("RSI 区间", "动量"),
    "gain20": ("20日涨幅", "动量"),
    "vol_ratio": ("量比", "量能"),
    "vol_trend": ("量能趋势", "量能"),
    "turnover": ("换手率", "量能"),
    "atr": ("波动率", "风险"),
    "drawdown": ("最大回撤", "风险"),
    "bias": ("乖离率", "风险"),
    "breakout": ("突破强度", "位置"),
    "boll": ("布林位置", "位置"),
}

#: 买入 / 卖出阈值（总分 ±100）
BUY_THRESHOLD = 15.0
SELL_THRESHOLD = -15.0


@dataclass
class FactorItem:
    """单个因子的评分结果。"""

    key: str
    label: str
    group: str
    weight: float
    sub: float          # 归一化后得分 [-1, 1]
    detail: str         # 人类可读的取值说明

    @property
    def contrib(self) -> float:
        """该因子对总分的贡献（weight × sub）。"""
        return round(self.weight * self.sub, 2)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "weight": self.weight,
            "sub": round(self.sub, 3),
            "contrib": self.contrib,
            "detail": self.detail,
        }


@dataclass
class FactorScore:
    """一次多因子评分的完整结果。"""

    total: float
    items: list[FactorItem] = field(default_factory=list)

    @property
    def action(self) -> str:
        """按阈值给出分级：buy / sell / hold。"""
        if self.total >= BUY_THRESHOLD:
            return "buy"
        if self.total <= SELL_THRESHOLD:
            return "sell"
        return "hold"

    def breakdown(self) -> list[dict]:
        """分项明细（按贡献绝对值降序，前端展开查看）。"""
        return [
            item.to_dict()
            for item in sorted(self.items, key=lambda i: abs(i.contrib), reverse=True)
        ]

    def top_reasons(self, positive: bool, limit: int = 2) -> list[str]:
        """取贡献最大的正/负因子文本。"""
        pool = [i for i in self.items if (i.contrib > 0) == positive]
        pool.sort(key=lambda i: abs(i.contrib), reverse=True)
        return [f"{i.label}: {i.detail}" for i in pool[:limit]]


def compute_factors(df: pd.DataFrame) -> FactorScore:
    """对含指标的K线 DataFrame 做多因子加权评分。

    Args:
        df: 已按日期升序、已通过 ``add_all_indicators`` 追加指标列，
            且至少 30 根K线。缺失列/NaN 的因子按 0 分处理，不抛异常。
    """
    items = [
        _ma_trend(df),
        _ma_slope(df),
        _long_position(df),
        _macd(df),
        _rsi(df),
        _gain20(df),
        _vol_ratio(df),
        _vol_trend(df),
        _turnover(df),
        _atr(df),
        _drawdown(df),
        _bias(df),
        _breakout(df),
        _boll(df),
    ]
    total = round(sum(i.contrib for i in items), 2)
    return FactorScore(total=total, items=items)


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _num(row, key: str) -> float | None:
    """安全取数：缺失列 / NaN / None 一律返回 None。"""
    try:
        value = row.get(key) if hasattr(row, "get") else None
    except Exception:  # noqa: BLE001 - 列名异常视为缺失
        return None
    if value is None or value != value:  # NaN 自身不等于自身
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _item(key: str, sub: float, detail: str) -> FactorItem:
    label, group = FACTOR_LABELS[key]
    return FactorItem(key, label, group, FACTOR_WEIGHTS[key], _clip(round(sub, 4)), detail)


# ----------------------------------------------------------------------
# A 趋势
# ----------------------------------------------------------------------
def _ma_trend(df: pd.DataFrame) -> FactorItem:
    """均线排列：ma5>ma10>ma20>ma60 满分，空头排列 -1。"""
    last = df.iloc[-1]
    ma5, ma10, ma20, ma60 = (_num(last, k) for k in ("ma5", "ma10", "ma20", "ma60"))
    if None in (ma5, ma10, ma20, ma60):
        return _item("ma_trend", 0.0, "均线数据不足")
    if ma5 > ma10 > ma20 > ma60:
        return _item("ma_trend", 1.0, "多头排列 5>10>20>60")
    if ma5 > ma10 > ma20:
        return _item("ma_trend", 0.6, "短期多头 5>10>20")
    if ma5 < ma10 < ma20 and ma20 < ma60:
        return _item("ma_trend", -1.0, "空头排列 5<10<20<60")
    if ma5 < ma10 < ma20:
        return _item("ma_trend", -0.6, "短期空头 5<10<20")
    return _item("ma_trend", 0.0, "均线交织")


def _ma_slope(df: pd.DataFrame) -> FactorItem:
    """中期趋势斜率：近 5 日 ma20 变化率，±2% 饱和。"""
    if len(df) < 6:
        return _item("ma_slope", 0.0, "数据不足")
    now = _num(df.iloc[-1], "ma20")
    ago = _num(df.iloc[-6], "ma20")
    if now is None or ago is None or ago <= 0:
        return _item("ma_slope", 0.0, "均线数据不足")
    change = (now / ago - 1) * 100
    return _item("ma_slope", change / 2.0, f"MA20 5日 {change:+.2f}%")


def _long_position(df: pd.DataFrame) -> FactorItem:
    """长期位置：收盘价相对 ma60 的偏离，±8% 饱和。"""
    last = df.iloc[-1]
    close = _num(last, "close")
    ma60 = _num(last, "ma60")
    if close is None or ma60 is None or ma60 <= 0:
        return _item("long_position", 0.0, "季线数据不足")
    dev = (close / ma60 - 1) * 100
    return _item("long_position", dev / 8.0, f"距季线 {dev:+.2f}%")


# ----------------------------------------------------------------------
# B 动量
# ----------------------------------------------------------------------
def _macd(df: pd.DataFrame) -> FactorItem:
    """MACD：柱体幅度（相对股价）+ 连续放大天数。"""
    last = df.iloc[-1]
    close = _num(last, "close")
    hist = _num(last, "macd_hist")
    if close is None or close <= 0 or hist is None:
        return _item("macd", 0.0, "MACD 数据不足")
    strength = _clip((hist / close) / 0.01)  # 柱体占股价 1% 记满分
    # 连续同向放大天数（最多 3 天）
    streak = 0
    if len(df) >= 4:
        hists = [
            _num(df.iloc[-i], "macd_hist") for i in range(1, 5)
        ]
        if all(h is not None for h in hists):
            direction = 1 if hists[0] > 0 else -1
            for prev, cur in zip(hists[1:], hists):
                if (cur - prev) * direction > 0:
                    streak += 1
                else:
                    break
    streak_sub = _clip(streak / 3.0) * (1 if hist > 0 else -1)
    sub = 0.7 * strength + 0.3 * streak_sub
    return _item("macd", sub, f"柱体{hist / close * 100:+.2f}% 连续{streak}日")


def _rsi(df: pd.DataFrame) -> FactorItem:
    """RSI12 区间：50~70 强势满分，超买扣分，极度超卖小幅加分。"""
    rsi = _num(df.iloc[-1], "rsi12")
    if rsi is None:
        return _item("rsi", 0.0, "RSI 数据不足")
    if rsi > 80:
        sub = -1.0
    elif rsi > 70:
        sub = -0.5
    elif rsi >= 55:
        sub = 1.0
    elif rsi >= 50:
        sub = 0.2
    elif rsi >= 45:
        sub = 0.0
    elif rsi >= 40:
        sub = -0.2
    elif rsi >= 30:
        sub = -0.5
    else:
        sub = 0.3  # 超跌反弹机会（但需趋势配合）
    return _item("rsi", sub, f"RSI12={rsi:.0f}")


def _gain20(df: pd.DataFrame) -> FactorItem:
    """20 日涨幅：±20% 饱和，涨幅过大（>30%）额外扣分。"""
    if len(df) < 21:
        return _item("gain20", 0.0, "数据不足")
    close = _num(df.iloc[-1], "close")
    base = _num(df.iloc[-21], "close")
    if close is None or base is None or base <= 0:
        return _item("gain20", 0.0, "数据不足")
    gain = (close / base - 1) * 100
    sub = _clip(gain / 20.0)
    if gain > 30:
        sub = _clip(sub - 0.3)
    return _item("gain20", sub, f"20日 {gain:+.2f}%")


# ----------------------------------------------------------------------
# C 量能
# ----------------------------------------------------------------------
def _vol_ratio(df: pd.DataFrame) -> FactorItem:
    """量比：>1.5 放量满分，<0.7 缩量扣分。"""
    ratio = _num(df.iloc[-1], "vol_ratio")
    if ratio is None:
        return _item("vol_ratio", 0.0, "量比数据不足")
    if ratio > 1.5:
        sub = 1.0
    elif ratio < 0.7:
        sub = -1.0
    else:
        sub = _clip((ratio - 1.0) / 0.5)
    return _item("vol_ratio", sub, f"量比 {ratio:.2f}")


def _vol_trend(df: pd.DataFrame) -> FactorItem:
    """量能趋势：近 5 日均量 / 近 20 日均量，>1.2 算放量。"""
    if len(df) < 20:
        return _item("vol_trend", 0.0, "数据不足")
    vol = df["volume"] if "volume" in df.columns else None
    if vol is None:
        return _item("vol_trend", 0.0, "成交量缺失")
    short = float(vol.tail(5).mean())
    long = float(vol.tail(20).mean())
    if long <= 0 or short != short or long != long:
        return _item("vol_trend", 0.0, "成交量缺失")
    ratio = short / long
    return _item("vol_trend", _clip((ratio - 1.0) / 0.5), f"5/20日均量 {ratio:.2f}")


def _turnover(df: pd.DataFrame) -> FactorItem:
    """换手率：2%~8% 活跃健康，过低/过高都扣分。"""
    turnover = _num(df.iloc[-1], "turnover_rate")
    if turnover is None:
        return _item("turnover", 0.0, "换手率缺失")
    if 2.0 <= turnover <= 8.0:
        sub = 1.0
    elif 1.0 <= turnover < 2.0:
        sub = 0.3
    elif 8.0 < turnover <= 15.0:
        sub = 0.0
    elif turnover > 25.0:
        sub = -1.0
    elif turnover > 15.0:
        sub = -0.5
    else:
        sub = -0.6  # 换手过低，流动性差
    return _item("turnover", sub, f"换手 {turnover:.2f}%")


# ----------------------------------------------------------------------
# D 风险
# ----------------------------------------------------------------------
def _atr(df: pd.DataFrame) -> FactorItem:
    """波动率：ATR/收盘价，≤2% 加分，≥5% 扣分。"""
    last = df.iloc[-1]
    atr = _num(last, "atr")
    close = _num(last, "close")
    if atr is None or close is None or close <= 0:
        return _item("atr", 0.0, "ATR 数据不足")
    ratio = atr / close * 100
    if ratio <= 2.0:
        sub = 0.5
    elif ratio >= 5.0:
        sub = -1.0
    else:
        sub = 0.5 - (ratio - 2.0) / 3.0 * 1.5
    return _item("atr", sub, f"ATR {ratio:.2f}%")


def _drawdown(df: pd.DataFrame) -> FactorItem:
    """近 20 日最大回撤：≤5% 满分，≥20% 扣满。"""
    if len(df) < 5:
        return _item("drawdown", 0.0, "数据不足")
    closes = pd.to_numeric(df["close"].tail(20), errors="coerce").dropna()
    if len(closes) < 5:
        return _item("drawdown", 0.0, "数据不足")
    running_max = closes.cummax()
    drawdown = float((closes / running_max - 1).min()) * 100  # 负值
    dd = abs(drawdown)
    sub = 1.0 - _clip((dd - 5.0) / 15.0, 0.0, 1.0) * 2.0
    return _item("drawdown", sub, f"20日回撤 {dd:.1f}%")


def _bias(df: pd.DataFrame) -> FactorItem:
    """乖离率 BIAS6：>+6% 超涨扣分，<-6% 超跌加分。"""
    bias6 = _num(df.iloc[-1], "bias6")
    if bias6 is None:
        return _item("bias", 0.0, "乖离数据不足")
    if bias6 > 6:
        sub = -1.0
    elif bias6 < -6:
        sub = 1.0
    else:
        sub = _clip(-bias6 / 6.0)
    return _item("bias", sub, f"BIAS6 {bias6:+.2f}%")


# ----------------------------------------------------------------------
# E 位置
# ----------------------------------------------------------------------
def _breakout(df: pd.DataFrame) -> FactorItem:
    """突破强度：距 20 日最高价的位置，创新高满分。"""
    last = df.iloc[-1]
    close = _num(last, "close")
    if close is None or close <= 0 or "high" not in df.columns:
        return _item("breakout", 0.0, "数据不足")
    highs = pd.to_numeric(df["high"].tail(20), errors="coerce").dropna()
    if highs.empty:
        return _item("breakout", 0.0, "数据不足")
    hh = float(highs.max())
    if hh <= 0:
        return _item("breakout", 0.0, "数据不足")
    pos = close / hh  # 1.0 = 创 20 日新高
    if pos >= 1.0:
        sub = 1.0
    elif pos >= 0.97:
        sub = 0.4
    elif pos >= 0.90:
        sub = 0.0
    else:
        sub = -1.0
    return _item("breakout", sub, f"距20日高 {pos * 100:.1f}%")


def _boll(df: pd.DataFrame) -> FactorItem:
    """布林位置：通道位置 0.5~0.8 最优，跌破下轨/冲出上轨扣分。"""
    last = df.iloc[-1]
    close = _num(last, "close")
    up = _num(last, "boll_up")
    dn = _num(last, "boll_dn")
    if close is None or up is None or dn is None or up - dn <= 0:
        return _item("boll", 0.0, "布林数据不足")
    pos = (close - dn) / (up - dn)
    if 0.5 <= pos <= 0.8:
        sub = 1.0
    elif pos < 0.5:
        sub = _clip((pos - 0.5) / 0.5)
    elif pos <= 1.0:
        sub = -0.5
    else:
        sub = -1.0
    return _item("boll", sub, f"通道位置 {pos:.2f}")
