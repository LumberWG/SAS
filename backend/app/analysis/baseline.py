"""大盘基准锚 + 相对强弱（RS）：把「按大盘基准找个股极值」变成可计算口径。

核心翻译（BACKTEST_LOG #120）：不做「基准找一次极值、个股再找一次极值然后对齐」，
而是直接对 ``RS = 个股收盘 / 基准收盘`` 找摆动点。这样有三个好处：
  1. 大盘共同波动被分母消掉，剩下的才是个股自身的相对强弱；
  2. 只需**一层**确认滞后，不会把「基准确认 + 个股确认」两段滞后叠加到买点上；
  3. 「个股偏离基准多远」变成 RS 的滚动 Z-score，可直接量化、可分档、可剔除。

方向口径：**RS 低点 = 相对弱势结束**，不是绝对低价；RS 高点 = 相对强势结束。
所以本模块的信号必须叠加 regime 闸门使用（弱市里 RS 会系统性失真），
且评估一律用相对基准的超额收益，不能用绝对收益——否则「共振胜率高」只是 beta 幻觉。

本模块只产出观测信号，不接任何下单 / 评分 / 撮合路径。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import pandas as pd

from app.analysis.swing import (
    SwingConfig,
    SwingPoint,
    detect_swings,
    pair_swings,
    remaining_amplitude,
)

_NAN = float("nan")


@dataclass(frozen=True)
class BaselineConfig:
    """偏离度、共振窗口与剔除阈值；默认值与 ``Settings.swing_*`` 对齐。"""

    z_window: int = 60
    band: float = 1.0
    extreme_z: float = 2.0
    anchor_bars: int = 3
    corr_window: int = 60
    min_corr: float = 0.2
    beta_window: int = 60

    def validate(self) -> None:
        if self.z_window < 5 or self.corr_window < 5 or self.beta_window < 5:
            raise ValueError("滚动窗口至少 5 根")
        if self.anchor_bars < 0:
            raise ValueError("anchor_bars 不能为负")
        if not 0 < self.band < self.extreme_z:
            raise ValueError("需满足 0 < band < extreme_z")
        if not -1.0 <= self.min_corr <= 1.0:
            raise ValueError("min_corr 需在 [-1, 1]")


def relative_strength(stock_close: pd.Series, bench_close: pd.Series) -> pd.Series:
    """RS 曲线，归一到首个有效值为 1（乘常数不改变摆动点形状）。"""
    s = pd.Series(stock_close).astype(float).reset_index(drop=True)
    b = pd.Series(bench_close).astype(float).reset_index(drop=True)
    rs = s / b.replace(0.0, _NAN)
    valid = rs.dropna()
    if valid.empty:
        return rs
    return rs / float(valid.iloc[0])


def wilder_atr(frame: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder ATR，纯因果（只用截至当前 bar 的 high/low/close）。"""
    high = pd.Series(frame["high"]).astype(float).reset_index(drop=True)
    low = pd.Series(frame["low"]).astype(float).reset_index(drop=True)
    close = pd.Series(frame["close"]).astype(float).reset_index(drop=True)
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / max(1, window), adjust=False).mean()


def rolling_zscore(values: pd.Series, window: int) -> pd.Series:
    """相对自身滚动均值的偏离度；rolling 天然只看左侧，不会前视。"""
    s = pd.Series(values).astype(float).reset_index(drop=True)
    mean = s.rolling(window, min_periods=window).mean()
    std = s.rolling(window, min_periods=window).std().replace(0.0, _NAN)
    return (s - mean) / std


def rolling_beta(stock_close: pd.Series, bench_close: pd.Series, window: int) -> pd.Series:
    """滚动 beta = cov(个股收益, 基准收益) / var(基准收益)，只用 T 日及之前数据。"""
    rs = pd.Series(stock_close).astype(float).reset_index(drop=True).pct_change()
    rb = pd.Series(bench_close).astype(float).reset_index(drop=True).pct_change()
    cov = rs.rolling(window, min_periods=window).cov(rb)
    var = rb.rolling(window, min_periods=window).var().replace(0.0, _NAN)
    return cov / var


def rolling_corr(stock_close: pd.Series, bench_close: pd.Series, window: int) -> pd.Series:
    """滚动收益相关度：过低说明这只票根本不跟基准走，共振无从谈起。"""
    rs = pd.Series(stock_close).astype(float).reset_index(drop=True).pct_change()
    rb = pd.Series(bench_close).astype(float).reset_index(drop=True).pct_change()
    return rs.rolling(window, min_periods=window).corr(rb)


def deviation_class(z: float, cfg: BaselineConfig) -> str:
    """五档：跟随带 / 温和正偏离 / 弱于基准 / 两类极端偏离。"""
    if z != z:
        return "unknown"
    if z > cfg.extreme_z:
        return "extreme_positive"
    if z < -cfg.extreme_z:
        return "extreme_negative"
    if z > cfg.band:
        return "mild_positive"
    if z < -cfg.band:
        return "weak"
    return "resonance"


def anchor_windows(bench_lows: list[SwingPoint], cfg: BaselineConfig) -> list[dict]:
    """基准确认低点后开一个 ±k 根的**时间窗**，只用来筛候选，不直接触发买卖。"""
    cfg.validate()
    return [
        {
            "pivot_bar": p.pivot_bar,
            "confirmed_bar": p.confirmed_bar,
            "start": p.confirmed_bar,
            "end": p.confirmed_bar + int(cfg.anchor_bars),
        }
        for p in bench_lows
    ]


def find_anchor(bar: int, windows: list[dict]) -> dict | None:
    return next((w for w in windows if w["start"] <= bar <= w["end"]), None)


def _finite(value: float) -> bool:
    return value == value and abs(value) != float("inf")


def _num(value: float, digits: int = 3) -> float | None:
    return round(float(value), digits) if _finite(float(value)) else None


def classify_status(
    z: float, corr: float, anchor: dict | None, cfg: BaselineConfig
) -> tuple[str, str]:
    """先剔除例外，再谈共振；顺序即优先级，避免「数据不足」被误判成有效信号。"""
    cls = deviation_class(z, cfg)
    if not _finite(z) or not _finite(corr):
        return cls, "excluded_insufficient"
    if cls.startswith("extreme_"):
        return cls, "excluded_extreme"
    if corr < cfg.min_corr:
        return cls, "excluded_low_corr"
    if anchor is None:
        return cls, "no_anchor"
    return cls, "resonance_buy"


def configs_from_settings() -> tuple[SwingConfig, BaselineConfig]:
    """参数唯一入口：``.env`` / app_state 覆盖层 / 代码默认值都在这里收口。"""
    from app.config import get_settings

    s = get_settings()
    swing = SwingConfig(
        threshold_pct=float(s.swing_threshold_pct),
        atr_mult=float(s.swing_atr_mult),
        confirm_bars=int(s.swing_confirm_bars),
        min_gap=int(s.swing_min_gap),
    )
    baseline = BaselineConfig(
        z_window=int(s.swing_z_window),
        band=float(s.swing_band),
        extreme_z=float(s.swing_extreme_z),
        anchor_bars=int(s.swing_anchor_bars),
        corr_window=int(s.swing_z_window),
        min_corr=float(s.swing_min_corr),
        beta_window=int(s.swing_z_window),
    )
    return swing, baseline


def scan_symbol(
    code: str,
    stock_close: pd.Series,
    bench_close: pd.Series,
    *,
    swing_cfg: SwingConfig,
    base_cfg: BaselineConfig,
    windows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """扫描一只票的全部已确认 RS 低点，返回 (信号明细, 波段幅度明细)。

    状态：``resonance_buy`` / ``no_anchor`` / ``excluded_extreme`` /
    ``excluded_low_corr`` / ``excluded_insufficient``。
    """
    base_cfg.validate()
    rs = relative_strength(stock_close, bench_close)
    z = rolling_zscore(rs, base_cfg.z_window)
    beta = rolling_beta(stock_close, bench_close, base_cfg.beta_window)
    corr = rolling_corr(stock_close, bench_close, base_cfg.corr_window)
    points = detect_swings(rs, config=swing_cfg)

    signals: list[dict] = []
    for low in (p for p in points if p.kind == "low"):
        bar = low.confirmed_bar
        zv = float(z.iloc[bar]) if bar < len(z) else _NAN
        cv = float(corr.iloc[bar]) if bar < len(corr) else _NAN
        bv = float(beta.iloc[bar]) if bar < len(beta) else _NAN
        anchor = find_anchor(bar, windows)
        cls, status = classify_status(zv, cv, anchor, base_cfg)
        signals.append({
            "code": code,
            "bar": bar,
            "pivot_bar": low.pivot_bar,
            "confirmed_bar": bar,
            "lag_bars": low.lag_bars,
            "rs": _num(float(rs.iloc[low.pivot_bar]), 4),
            "rs_z": _num(zv),
            "beta": _num(bv),
            "corr": _num(cv),
            "threshold": round(float(low.threshold), 4),
            "deviation": cls,
            "anchor_bar": anchor["confirmed_bar"] if anchor else None,
            "status": status,
        })

    amplitudes: list[dict] = []
    for low, high in pair_swings(points):
        item = remaining_amplitude(rs, low, high)
        if item is not None:
            amplitudes.append(item)
    return signals, amplitudes


def summarize(signals: list[dict], amplitudes: list[dict]) -> dict:
    """汇总影子观测结果；样本不足时只能看分布，不能下策略结论。"""
    by_status: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for row in signals:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        by_class[row["deviation"]] = by_class.get(row["deviation"], 0) + 1
    net = [a["net_pct"] for a in amplitudes]
    gross = [a["gross_pct"] for a in amplitudes]
    codes = {r["code"] for r in signals}
    return {
        "symbols_with_signal": len(codes),
        "signals": len(signals),
        "by_status": by_status,
        "by_deviation": by_class,
        "amplitude_samples": len(amplitudes),
        "median_gross_pct": round(statistics.median(gross), 3) if gross else None,
        "median_net_pct": round(statistics.median(net), 3) if net else None,
        "net_positive_ratio": (
            round(sum(1 for x in net if x > 0) / len(net), 3) if net else None
        ),
        "median_lag_bars": (
            round(statistics.median(a["lag_bars"] for a in amplitudes), 2)
            if amplitudes else None
        ),
    }
