"""Point-in-time 摆动点检测：把「区间极值」翻译成可交易的「已确认转折点」。

为什么不是直接找极值（BACKTEST_LOG #120）：
- 微积分的局部极值要求 x0 左右两侧邻域都已知，本身就是**事后**定义；
- 价格序列离散、带跳空、带噪声，任意小区间里都塞满局部极值，没有尺度就无从谈起；
- 因此先定尺度（``max(固定幅度, ATR 倍数)``），再定确认（右侧 K 根不破且反向幅度达标），
  每个点都带 ``confirmed_bar``，调用方按 ``confirmed_bar <= t`` 过滤即为逐日重放。

**不可撤销硬约束**：已输出的点不会因为后面出现更极端的同类点而被替换/删除。
否则逐日重放时历史信号会凭空消失，等于另一种形式的前视偏差。
本模块是纯函数，输入可以是价格，也可以是相对强弱 RS。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class SwingConfig:
    """摆动点尺度与确认参数；默认值与 ``Settings.swing_*`` 对齐。"""

    threshold_pct: float = 0.05
    atr_mult: float = 1.0
    confirm_bars: int = 3
    min_gap: int = 5

    def validate(self) -> None:
        if self.confirm_bars < 1:
            raise ValueError("confirm_bars 至少为 1")
        if self.min_gap < 1:
            raise ValueError("min_gap 至少为 1")
        if self.threshold_pct < 0 or self.atr_mult < 0:
            raise ValueError("摆动阈值不能为负")


@dataclass(frozen=True)
class SwingPoint:
    """一个**已确认**的摆动点。

    ``pivot_bar`` 是极值发生的位置，``confirmed_bar`` 是它变得可见的位置；
    两者之差就是确认滞后，实盘只能从 ``confirmed_bar`` 开始行动。
    """

    kind: str  # "low" | "high"
    pivot_bar: int
    confirmed_bar: int
    value: float
    threshold: float

    @property
    def lag_bars(self) -> int:
        return self.confirmed_bar - self.pivot_bar

    def as_dict(self) -> dict:
        data = asdict(self)
        data["lag_bars"] = self.lag_bars
        return data


def _thresholds(values: pd.Series, atr: pd.Series | None, cfg: SwingConfig) -> pd.Series:
    """每个 bar 的相对幅度阈值。ATR 是滚动因果量，不会引入未来数据。"""
    out = pd.Series(float(cfg.threshold_pct), index=values.index, dtype="float64")
    if atr is not None and cfg.atr_mult > 0:
        aligned = pd.Series(atr).astype(float).reset_index(drop=True)
        denom = values.abs().replace(0.0, float("nan"))
        rel = (float(cfg.atr_mult) * aligned.reindex(values.index)) / denom
        out = pd.concat([out, rel], axis=1).max(axis=1)
    return out.fillna(float(cfg.threshold_pct)).clip(lower=0.0)


def detect_swings(
    values: pd.Series,
    *,
    atr: pd.Series | None = None,
    config: SwingConfig | None = None,
) -> list[SwingPoint]:
    """返回按 ``confirmed_bar`` 升序排列的已确认摆动点。

    ``values`` 按时间升序；调用方负责把 index 重置成 0..n-1（或保证与 ``atr`` 同序）。
    返回的每个点都满足 ``confirmed_bar <= len(values)-1``，可安全用于逐日重放。
    """
    cfg = config or SwingConfig()
    cfg.validate()
    v = pd.Series(values).astype(float).reset_index(drop=True)
    n = len(v)
    k = int(cfg.confirm_bars)
    if n < 2 * k + 2:
        return []
    thr = _thresholds(v, atr, cfg)
    # 逐 bar 循环用 numpy 视图：300 只 × 上千根时，pandas.iloc 切片会慢一个数量级
    arr = v.to_numpy(dtype=float)
    scales = thr.to_numpy(dtype=float)
    # 前缀极值只用于「第一个摆动点」的初始化幅度；expanding 是 O(n) 且 skipna
    run_max = v.expanding(min_periods=1).max().to_numpy(dtype=float)
    run_min = v.expanding(min_periods=1).min().to_numpy(dtype=float)

    # 第一步只找「局部极值 + 右侧 k 根不破」的候选，不在 k 根里要求走完整段波段。
    # 早期版本把 5% 波段门槛套在 3 根确认窗口上，结果指数 1200 根只剩 1 个低点。
    raw: list[SwingPoint] = []
    for i in range(k, n - k):
        left = arr[i - k:i]
        right = arr[i + 1:i + k + 1]
        if right.size < k or left.size == 0:
            continue
        cur = float(arr[i])
        if cur < float(left.min()) and cur < float(right.min()):
            raw.append(SwingPoint("low", i, i + k, cur, float(scales[i])))
        elif cur > float(left.max()) and cur > float(right.max()):
            raw.append(SwingPoint("high", i, i + k, cur, float(scales[i])))

    # 第二步 ZigZag 式筛选：交替 + 最小间隔 + 相对上一摆动点的反转幅度。
    # 不回头替换已输出的点，保证前缀重算与全量过滤完全等价（无前视、信号不消失）。
    out: list[SwingPoint] = []
    last: SwingPoint | None = None
    last_pivot = -(10**9)
    for p in raw:
        if p.pivot_bar - last_pivot < int(cfg.min_gap):
            continue
        if last is not None and p.kind == last.kind:
            continue
        if last is None:
            hi, lo = float(run_max[p.pivot_bar]), float(run_min[p.pivot_bar])
            span = (hi - lo) / abs(lo) if lo else 0.0
        else:
            span = abs(p.value - last.value) / abs(last.value) if last.value else 0.0
        if span < p.threshold:
            continue
        out.append(p)
        last, last_pivot = p, p.pivot_bar
    return out


def visible_swings(points: list[SwingPoint], bar: int) -> list[SwingPoint]:
    """第 ``bar`` 根 K 线收盘时真正可见的点（唯一的正确过滤口径）。"""
    return [p for p in points if p.confirmed_bar <= bar]


def pair_swings(points: list[SwingPoint]) -> list[tuple[SwingPoint, SwingPoint]]:
    """每个已确认低点配其后第一个已确认高点，用于统计波段幅度。"""
    pairs: list[tuple[SwingPoint, SwingPoint]] = []
    for low in (p for p in points if p.kind == "low"):
        nxt = next((h for h in points if h.kind == "high" and h.pivot_bar > low.pivot_bar), None)
        if nxt is not None:
            pairs.append((low, nxt))
    return pairs


def remaining_amplitude(
    values: pd.Series, low: SwingPoint, high: SwingPoint
) -> dict | None:
    """确认滞后吃掉多少波段：``gross`` 是极值到极值，``net`` 是能买到的那一段。

    这是判断「极值思路有没有可交易空间」的核心数字：如果 net 长期接近 0 或为负，
    说明尺度/确认参数下根本没有波段可做，而不是选股不准。
    """
    v = pd.Series(values).astype(float).reset_index(drop=True)
    if low.value == 0 or low.confirmed_bar >= len(v) or high.pivot_bar <= low.pivot_bar:
        return None
    entry = float(v.iloc[low.confirmed_bar])
    if entry == 0:
        return None
    gross = (high.value / low.value - 1.0) * 100.0
    net = (high.value / entry - 1.0) * 100.0
    return {
        "gross_pct": round(gross, 3),
        "net_pct": round(net, 3),
        "eaten_pct": round(gross - net, 3),
        "lag_bars": low.lag_bars,
        "pivot_bar": low.pivot_bar,
        "confirmed_bar": low.confirmed_bar,
        "high_bar": high.pivot_bar,
    }
