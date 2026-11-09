# -*- coding: utf-8 -*-
"""出场规则：把「确认后能拿到多少波段」从固定持有改成可比的出场实验（#124）。

为什么必须动出场而不是继续调概率：#123 的归因已经写死 —— 确认后剩余波段中位数只有
3.5%~6.0%，扣掉误报下探 1.6% 与双边成本 0.3%，需要 60%+ 命中率才划算，而模型给的是
30%~48%。这 25~35 个百分点的缺口在概率层面补不上，只能从「波段本身有多厚」里找：
  - ``fixed``   ：现状基线，确认后固定持有 N 根收盘卖出；
  - ``atr_trail``：入场时冻结一个 ATR 回撤宽度，之后跟踪最高收盘，回撤触发即走。
    右尾不被截断（趋势能跑多远就跑多远），左尾被宽度兜住 —— 唯一可能同时抬高
    中位数与期望值的规则；
  - ``swing_trail``：跟踪最近一个**已确认**的价格摆动高点，跌破前高才走。
    与 ``atr_trail`` 的区别是锚在结构位而非波动率，更贴近「做波段」的原意。

因果红线（tests/test_exit_lab.py 锁死）：
1. 入场一律是 ``entry_bar`` 收盘（低点确认那根），出场只看 ``entry_bar`` 之后；
2. 跟踪峰值只累加已走到的收盘，回撤宽度用**入场时**可见的 ATR 冻结，
   不允许用出场时的波动率反推宽度（那是用未来数据调止损）；
3. ``swing_trail`` 只认 ``confirmed_bar <= i`` 的摆动高点，未确认的结构位看不见。

本模块是纯函数，不读写任何持仓表、不产生委托。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.analysis.swing import SwingPoint

#: 出场模式；``fixed`` 是 #120~#123 的既有口径，其余为本轮新增。
EXIT_MODES: tuple[str, ...] = ("fixed", "atr_trail", "swing_trail")


@dataclass(frozen=True)
class ExitSpec:
    """一组出场规则。

    ``hold_bars`` 在 ``fixed`` 下是持有根数，在两种跟踪模式下是**最长持有上限**
    （防止一直不触发回撤而把资金无限期占住）。
    """

    name: str
    mode: str = "fixed"
    hold_bars: int = 3
    atr_k: float = 1.5          # 跟踪回撤宽度 = k × 入场时 ATR 占价格比例
    floor_pct: float = 2.0      # 回撤宽度下限（ATR 很小时不至于被噪声扫掉）
    take_profit_pct: float = 0.0  # >0 时触及该涨幅直接止盈，0 = 不设上限
    cost_pct: float = 0.3

    def validate(self) -> None:
        if self.mode not in EXIT_MODES:
            raise ValueError(f"mode 只支持 {EXIT_MODES}")
        if self.hold_bars < 1:
            raise ValueError("hold_bars 至少为 1")
        if self.atr_k <= 0 or self.floor_pct < 0:
            raise ValueError("atr_k 必须为正，floor_pct 不能为负")
        if self.take_profit_pct < 0:
            raise ValueError("take_profit_pct 不能为负")


@dataclass(frozen=True)
class ExitResult:
    """一笔出场的完整记录；绝对与超额收益并列，避免 beta 混进 alpha 评价。"""

    exit_bar: int
    exit_pct: float        # 相对入场收盘的绝对收益（%）
    excess_pct: float      # 减去基准同期收益后的超额（%）
    net_pct: float         # 绝对收益 - 双边成本
    holding_bars: int
    mfe_pct: float         # 区间最大有利偏移
    mae_pct: float         # 区间最大不利偏移
    reason: str            # trail / stop / take_profit / time_cap / no_exit / no_data

    def as_dict(self) -> dict:
        return {
            "exit_bar": self.exit_bar,
            "exit_pct": round(self.exit_pct, 3),
            "excess_pct": round(self.excess_pct, 3),
            "net_pct": round(self.net_pct, 3),
            "holding_bars": self.holding_bars,
            "mfe_pct": round(self.mfe_pct, 3),
            "mae_pct": round(self.mae_pct, 3),
            "reason": self.reason,
        }


def _bench_return(bench: np.ndarray | None, a: int, b: int) -> float:
    if bench is None or a < 0 or b < 0 or a >= len(bench) or b >= len(bench):
        return 0.0
    p0, p1 = float(bench[a]), float(bench[b])
    if p0 <= 0:
        return 0.0
    return (p1 / p0 - 1.0) * 100.0


def _next_confirmed_high(highs: list[SwingPoint] | None, bar: int) -> float | None:
    """第 ``bar`` 根收盘时可见的最近一个价格摆动高点（只看已确认的）。"""
    if not highs:
        return None
    best = None
    for p in highs:
        if p.confirmed_bar <= bar and (best is None or p.pivot_bar > best.pivot_bar):
            best = p
    return best.value if best is not None else None


def simulate_exit(
    close,
    entry_bar: int,
    spec: ExitSpec,
    *,
    atr=None,
    bench_close=None,
    price_highs: list[SwingPoint] | None = None,
) -> ExitResult | None:
    """从 ``entry_bar`` 收盘入场，按 ``spec`` 模拟一次出场。

    ``close`` / ``atr`` / ``bench_close`` 可以是 Series 或 ndarray，统一转 numpy 视图。
    返回 ``None`` 只表示「入场价无效」，不表示亏损 —— 缺失数据不参与统计。
    """
    spec.validate()
    arr = np.asarray(close, dtype=float)
    n = arr.size
    if entry_bar < 0 or entry_bar >= n:
        return None
    entry = float(arr[entry_bar])
    if not np.isfinite(entry) or entry <= 0:
        return None
    atr_arr = np.asarray(atr, dtype=float) if atr is not None else None
    bench = np.asarray(bench_close, dtype=float) if bench_close is not None else None

    last_bar = n - 1
    if spec.mode == "fixed":
        exit_bar = min(entry_bar + spec.hold_bars, last_bar)
        reason = "time_cap" if exit_bar == entry_bar + spec.hold_bars else "no_exit"
        path = arr[entry_bar:exit_bar + 1]
        peak, trough = float(np.nanmax(path)), float(np.nanmin(path))
        return _result(entry, entry_bar, exit_bar, arr, bench, spec, peak, trough, reason)

    # 跟踪模式：宽度在入场时冻结，峰值只累加已经走到的收盘
    width = spec.floor_pct / 100.0
    if spec.mode == "atr_trail" and atr_arr is not None:
        a0 = float(atr_arr[entry_bar]) if entry_bar < atr_arr.size else float("nan")
        if np.isfinite(a0) and a0 > 0:
            width = max(width, spec.atr_k * a0 / entry)
    if spec.mode == "swing_trail":
        width = max(width, 0.0)

    peak = entry
    trough = entry
    exit_bar, reason = last_bar, "no_exit"
    for i in range(entry_bar + 1, n):
        px = float(arr[i])
        if not np.isfinite(px):
            continue
        if px > peak:
            peak = px
        if px < trough:
            trough = px
        if spec.take_profit_pct > 0 and px >= entry * (1.0 + spec.take_profit_pct / 100.0):
            exit_bar, reason = i, "take_profit"
            break
        if spec.mode == "swing_trail":
            anchor = _next_confirmed_high(price_highs, i)
            if anchor is not None and px < anchor:
                exit_bar, reason = i, ("trail" if peak > entry else "stop")
                break
        elif px <= peak * (1.0 - width):
            # 峰值没抬过入场价就是止损，抬过才是跟踪止盈，两者混在一起会看不懂归因
            exit_bar, reason = i, ("trail" if peak > entry else "stop")
            break
        if i - entry_bar >= spec.hold_bars:
            exit_bar, reason = i, "time_cap"
            break
    if exit_bar - entry_bar >= spec.hold_bars and reason == "no_exit":
        reason = "time_cap"
    return _result(entry, entry_bar, exit_bar, arr, bench, spec, peak, trough, reason)


def _result(
    entry: float,
    entry_bar: int,
    exit_bar: int,
    arr: np.ndarray,
    bench: np.ndarray | None,
    spec: ExitSpec,
    peak: float,
    trough: float,
    reason: str,
) -> ExitResult:
    exit_px = float(arr[exit_bar])
    if not np.isfinite(exit_px):
        exit_px = entry
    gross = (exit_px / entry - 1.0) * 100.0
    err = exit_bar - entry_bar
    excess = gross - _bench_return(bench, entry_bar, exit_bar)
    return ExitResult(
        exit_bar=exit_bar,
        exit_pct=gross,
        excess_pct=excess,
        net_pct=gross - spec.cost_pct,
        holding_bars=err,
        mfe_pct=(peak / entry - 1.0) * 100.0,
        mae_pct=(trough / entry - 1.0) * 100.0,
        reason=reason,
    )


#: 研究用出场预设。``fixed3`` 是 #120~#123 的既有基线（确认后持有 3 根），
#: 其余用于回答「换出场能不能把中位数抬到成本线以上」。
EXIT_PRESETS: tuple[ExitSpec, ...] = (
    ExitSpec("fixed3", "fixed", hold_bars=3),
    ExitSpec("fixed5", "fixed", hold_bars=5),
    ExitSpec("fixed10", "fixed", hold_bars=10),
    ExitSpec("atr12", "atr_trail", hold_bars=20, atr_k=1.2, floor_pct=2.0),
    ExitSpec("atr18", "atr_trail", hold_bars=20, atr_k=1.8, floor_pct=2.5),
    ExitSpec("atr25", "atr_trail", hold_bars=30, atr_k=2.5, floor_pct=3.0),
    ExitSpec("atr18_tp12", "atr_trail", hold_bars=20, atr_k=1.8, floor_pct=2.5,
             take_profit_pct=12.0),
    ExitSpec("swing_trail", "swing_trail", hold_bars=30, floor_pct=0.0),
)


def exit_config_from_settings() -> ExitSpec:
    """线上默认出场口径（只用于报告对照，不接交易）；参数可在设置页热更。"""
    from app.config import get_settings

    s = get_settings()
    return ExitSpec(
        name="settings",
        mode=str(s.swing_exit_mode),
        hold_bars=int(s.swing_exit_hold_bars),
        atr_k=float(s.swing_exit_atr_k),
        floor_pct=float(s.swing_exit_floor_pct),
        take_profit_pct=float(s.swing_exit_take_profit_pct),
        cost_pct=float(s.swing_est_cost_pct),
    )
