# -*- coding: utf-8 -*-
"""T0 MACD 做T模拟模块的单元测试：状态机逻辑（注入信号） + 真实数据冒烟。"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies import t0_macd as t0
from app.strategies.t0_macd import (
    T0Params,
    aggregate_trades,
    compute_macd,
    simulate_series,
    _build_gate_map,
    _atr,
    _near_support,
    _entry_qty,
    _hist_turn_signals,
    _vol_ratio_array,
    _recent_extremes,
    _divergence_signals,
    _higher_tf_bias,
    _settle,
)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def _aday_times() -> list[datetime]:
    """真实 A 股 5 分钟时间表（上午 24 根 + 下午 24 根，共 48 根）。"""
    out = []
    t = datetime(2025, 1, 2, 9, 35)
    for _ in range(24):           # 09:35 -> 11:30
        out.append(t)
        t += timedelta(minutes=5)
    t = datetime(2025, 1, 2, 13, 5)
    for _ in range(24):           # 13:05 -> 15:00
        out.append(t)
        t += timedelta(minutes=5)
    return out


def _md_times(date_str: str, n: int = 48) -> list[datetime]:
    """给定日期的 A 股 5 分钟时间表（默认 48 根）。"""
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, minute=35)
    out = []
    for i in range(24):
        out.append(base + timedelta(minutes=5 * i))
    base = base.replace(hour=13, minute=5)
    for i in range(24):
        out.append(base + timedelta(minutes=5 * i))
    return out[:n]


def _make_bars(close, times):
    df = pd.DataFrame({"close": np.asarray(close, dtype=float)})
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 1.0
    df["amount"] = 1.0
    df["dt"] = times
    return df


def _phase_bars(days_up: int = 10, days_down: int = 10) -> pd.DataFrame:
    """构造「前 days_up 天每天单调上行、后 days_down 天每天单调下行」的 5 分钟序列。

    关键：每天**日内不反转**（只会同向上/下行），使 60 分钟重采样序列呈干净的
    上行段/下行段，用于更高周期偏置的方向性测试（避免日内锯齿污染偏置）。
    """
    parts = []
    for d in range(days_up + days_down):
        date = (datetime(2025, 1, 2) + timedelta(days=d)).strftime("%Y-%m-%d")
        base = 10.0 + d * 0.4 if d < days_up else 12.0 - (d - days_up) * 0.4
        if d < days_up:
            c = np.linspace(base, base + 0.3, 48)   # 上行日：单调上行
        else:
            c = np.linspace(base, base - 0.3, 48)   # 下行日：单调下行
        b = _make_bars(c, _md_times(date))
        b["high"] = c * 1.01
        b["low"] = c * 0.99
        parts.append(b)
    return pd.concat(parts).reset_index(drop=True)


def _fake_macd(diff: np.ndarray, ema_slow: np.ndarray):
    """返回一个替身 compute_macd：用脚本化的 diff/ema 驱动金叉死叉。"""

    def _fake(bars, fast, slow, signal):
        out = bars.copy()
        out["dif"] = diff
        out["dea"] = np.zeros_like(diff)
        out["ema_slow"] = ema_slow
        return out

    return _fake


def _steady_price(n, base=10.0):
    return np.full(n, base, dtype=float)


# ---------------------------------------------------------------------------
# compute_macd 基本契约
# ---------------------------------------------------------------------------
def test_compute_macd_adds_columns():
    n = 60
    bars = _make_bars(_steady_price(n), [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    out = compute_macd(bars, 6, 38, 6)
    for col in ("dif", "dea", "macd_hist", "ema_slow"):
        assert col in out.columns
    assert len(out) == n


def test_short_series_returns_empty():
    bars = _make_bars(_steady_price(10), [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(10)])
    assert simulate_series(bars, T0Params()) == []


# ---------------------------------------------------------------------------
# 状态机：正T（先买后卖）基本回合
# ---------------------------------------------------------------------------
def test_long_round_on_golden_then_death(monkeypatch):
    n = 40
    close = np.concatenate([_steady_price(10, 10.0), np.linspace(10.0, 12.0, 20)[1:], _steady_price(10, 12.0)])
    # 让长度刚好 n
    close = np.pad(close, (0, max(0, n - len(close))), constant_values=12.0)[:n]
    diff = np.zeros(n)
    diff[10] = 1.0    # 金叉：diff[9]<=0, diff[10]>0
    diff[20] = -1.0   # 死叉：diff[19]>=0, diff[20]<0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    trades = simulate_series(bars, T0Params())
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "long"
    assert t.forced is False
    assert t.entry_idx == 10 and t.exit_idx == 20  # 死叉那根（DIF 转负）平仓
    assert t.pnl > 0  # 卖价(12) > 买价(11)


# ---------------------------------------------------------------------------
# 破位止损：价格反向击穿 -1.5% 无条件离场
# ---------------------------------------------------------------------------
def test_breakout_stop_exits_with_loss(monkeypatch):
    n = 40
    close = _steady_price(n, 10.0)
    close[11] = 9.8   # idx11 跌 2% 触发破位止损（-1.5%）
    diff = np.zeros(n)
    diff[10] = 1.0    # 金叉开多
    # 之后一直为正，不出现死叉
    diff[11:] = 1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    trades = simulate_series(bars, T0Params())
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "long" and t.forced is False
    assert t.pnl < 0  # 破位离场亏损


# ---------------------------------------------------------------------------
# 时间止损：持仓超 max_hold_bars 且价格未涨，主动离场
# ---------------------------------------------------------------------------
def test_time_stop_exits_when_flat(monkeypatch):
    n = 40
    close = _steady_price(n, 10.0)   # 价格一直持平
    diff = np.zeros(n)
    diff[10] = 1.0
    diff[11:] = 1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    trades = simulate_series(bars, T0Params(max_hold_bars=5))
    assert len(trades) == 1
    t = trades[0]
    # 金叉 idx10，持仓满 5 根（idx15）仍未涨 -> 时间止损
    assert t.exit_idx == 10 + 5
    assert t.forced is False


# ---------------------------------------------------------------------------
# 14:50 强制清零（不过夜）
# ---------------------------------------------------------------------------
def test_force_clear_at_1450(monkeypatch):
    times = _aday_times()
    n = len(times)
    close = _steady_price(n, 10.0)
    diff = np.zeros(n)
    diff[5] = 1.0       # idx5 金叉开多
    diff[6:] = 1.0      # 之后一直为正，不出现死叉
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, times)
    # 关闭时间止损/破位止损，确保持仓能存活到 14:50 触发强制清零
    p = T0Params(max_hold_bars=10 ** 9, stop_pct=10.0)  # clear_time=14:50，停用止损
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    t = trades[0]
    assert t.forced is True
    # 14:50 在真实时间表为第 45 根（索引 45）
    assert t.exit_idx == 45
    assert times[t.exit_idx].hour == 14 and times[t.exit_idx].minute == 50


# ---------------------------------------------------------------------------
# 反T（先卖后买）：死叉开空、金叉接回
# ---------------------------------------------------------------------------
def test_reverse_short_round(monkeypatch):
    n = 40
    close = np.concatenate([np.linspace(12.0, 12.0, 10), np.linspace(12.0, 10.0, 20)[1:], _steady_price(10, 10.0)])
    close = np.pad(close, (0, max(0, n - len(close))), constant_values=10.0)[:n]
    diff = np.zeros(n)
    diff[10] = -1.0   # 死叉开空（高位卖出）
    diff[20] = 1.0    # 金叉接回（低位买入）
    # require_uptrend=False 以允许反T
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    trades = simulate_series(bars, T0Params(require_uptrend=False))
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "short"
    assert t.pnl > 0  # 卖高(12)买低(10)


# ---------------------------------------------------------------------------
# 趋势网关：慢线向下时禁止正T
# ---------------------------------------------------------------------------
def test_require_uptrend_blocks_long(monkeypatch):
    n = 40
    close = _steady_price(n, 10.0)
    diff = np.zeros(n)
    diff[10] = 1.0    # 金叉，但慢线向下
    diff[11:] = 1.0
    ema_falling = -np.arange(n, dtype=float)  # 慢线持续下行
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, ema_falling))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    trades = simulate_series(bars, T0Params(require_uptrend=True, allow_reverse=False))
    assert trades == []  # 无正T（趋势不符），也无反T（关闭）-> 空仓


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def test_aggregate_trades_empty():
    agg = aggregate_trades([])
    assert agg["rounds"] == 0 and agg["win_rate"] == 0.0


# ---------------------------------------------------------------------------
# 真实数据冒烟（无本地数据则跳过）
# ---------------------------------------------------------------------------
def test_real_smoke_000001():
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过真实数据冒烟")
    trades = simulate_series(bars, T0Params())
    agg = aggregate_trades(trades)
    assert isinstance(agg, dict)
    assert "rounds" in agg
    # 不要求一定有信号，只验证整条链路不崩、可聚合
    assert agg["rounds"] >= 0


# ---------------------------------------------------------------------------
# 日线硬过滤：_build_gate_map 单元（用前一日信息决定当日，避免未来函数）
# ---------------------------------------------------------------------------
def _daily_frame(amps, amounts, closes, ma5, ma20, ma60, dates):
    return pd.DataFrame(
        {
            "amp": amps,
            "amount": amounts,
            "close": closes,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
        },
        index=pd.to_datetime(dates),
    )


def test_build_gate_map_trend_uptrend():
    daily = _daily_frame(
        amps=[0.03, 0.03, 0.03],
        amounts=[2e8, 2e8, 2e8],
        closes=[10.0, 10.5, 11.0],
        ma5=[9.8, 10.0, 10.2],
        ma20=[9.5, 9.6, 9.7],
        ma60=[9.0, 9.1, 9.2],
        dates=["2025-01-02", "2025-01-03", "2025-01-06"],
    )
    params = T0Params(trend_filter=True, min_daily_amp_pct=2.0, min_daily_amount=1e8)
    gm = _build_gate_map(daily, params)
    # 首日无前一日 -> (F,F,F)；其余用前一日信息，多头排列+振幅足+流动性足 -> (T,T,T)
    assert gm["2025-01-02"] == (False, False, False)
    assert gm["2025-01-03"] == (True, True, True)
    assert gm["2025-01-06"] == (True, True, True)


def test_build_gate_map_bearish_prev_blocks_next():
    daily = _daily_frame(
        amps=[0.03, 0.03, 0.03],
        amounts=[2e8, 2e8, 2e8],
        closes=[11.0, 9.5, 10.0],
        ma5=[10.2, 9.0, 9.5],
        ma20=[9.7, 9.6, 9.4],
        ma60=[9.2, 9.7, 9.3],
        dates=["2025-01-02", "2025-01-03", "2025-01-06"],
    )
    params = T0Params(trend_filter=True, min_daily_amp_pct=2.0, min_daily_amount=1e8)
    gm = _build_gate_map(daily, params)
    # day1(01-03) 空头排列 -> day2(01-06) 用其信息，uptrend=False
    assert gm["2025-01-06"][2] is False


def test_build_gate_map_liquidity_gate():
    daily = _daily_frame(
        amps=[0.03, 0.03],
        amounts=[5e7, 2e8],  # 首日成交额不足 1 亿
        closes=[10.0, 10.5],
        ma5=[9.8, 10.0],
        ma20=[9.5, 9.6],
        ma60=[9.0, 9.1],
        dates=["2025-01-02", "2025-01-03"],
    )
    params = T0Params(trend_filter=True, min_daily_amp_pct=2.0, min_daily_amount=1e8)
    gm = _build_gate_map(daily, params)
    # 次日(01-03)用首日(01-02)信息：amount 5e7 < 1e8 -> liq_ok False
    assert gm["2025-01-03"] == (True, False, True)


# ---------------------------------------------------------------------------
# 日线硬过滤：端到端（合成多日 5 分钟序列 + 注入信号）
# ---------------------------------------------------------------------------
def test_amplitude_gate_blocks_low_amp_day(monkeypatch):
    dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
    day_closes = [
        np.full(48, 10.0),                 # day A：高振幅捐赠日，无信号
        np.full(48, 10.0),                 # day B：低振幅捐赠日，无信号
        np.linspace(10.0, 12.0, 48),       # day C：信号日，上行
    ]
    parts = []
    for d, c in zip(dates, day_closes):
        b = _make_bars(c, _md_times(d))
        if d == "2025-01-02":
            b["high"], b["low"] = c * 1.03, c * 0.97   # 振幅 ~6%
        elif d == "2025-01-03":
            b["high"], b["low"] = c * 1.002, c * 0.998  # 振幅 ~0.4%
        else:
            b["high"], b["low"] = c * 1.03, c * 0.97
        parts.append(b)
    bars = pd.concat(parts).reset_index(drop=True)
    n = len(bars)
    diff = np.zeros(n)
    diff[100] = 1.0    # day C 金叉
    diff[120] = -1.0   # day C 死叉
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))

    # 高振幅门槛(5%)：day C 的前一日(day B)振幅仅 ~0.4% -> day C 被封，0 笔
    trades = simulate_series(bars, T0Params(min_daily_amp_pct=5.0, require_uptrend=False))
    assert trades == []
    # 关闭振幅门槛：day C 应有正T
    trades2 = simulate_series(bars, T0Params(require_uptrend=False))
    assert len(trades2) >= 1
    assert trades2[0].side == "long"


def test_trend_gate_blocks_long_when_prev_bearish(monkeypatch):
    # 3 日：day A 上行(捐赠日) / day B 下行(捐赠日) / day C 信号日
    dates = ["2025-01-02", "2025-01-03", "2025-01-06"]
    day_closes = [
        np.linspace(10.0, 12.0, 48),   # A 上行
        np.linspace(12.0, 10.0, 48),   # B 下行（前一日信息使 C 偏空）
        np.linspace(10.0, 12.0, 48),   # C 信号日
    ]
    parts = []
    for d, c in zip(dates, day_closes):
        b = _make_bars(c, _md_times(d))
        b["high"], b["low"] = c * 1.03, c * 0.97
        parts.append(b)
    bars = pd.concat(parts).reset_index(drop=True)
    n = len(bars)
    diff = np.zeros(n)
    # day C 金叉 idx ~ 2*48 + 10 = 106
    diff[106] = 1.0
    diff[126] = -1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))

    # 趋势闸门开：day C 用 day B(下行) 信息 -> 非多头 -> 正T被禁
    p = T0Params(trend_filter=True, min_daily_amp_pct=2.0,
                 require_uptrend=True, allow_reverse=False)
    trades = simulate_series(bars, p)
    assert trades == []
    # 关闭趋势闸门（仅振幅过滤）：day C 正T 成立
    p2 = T0Params(require_uptrend=False, min_daily_amp_pct=2.0)
    trades2 = simulate_series(bars, p2)
    assert any(t.side == "long" for t in trades2)


# ---------------------------------------------------------------------------
# 波动率自适应 + 支撑位附近金叉（变体 F 的两项增强）
# ---------------------------------------------------------------------------
def test_atr_basic():
    n = 40
    times = [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)]
    df = pd.DataFrame({"close": np.full(n, 10.0)})
    df["open"] = df["close"]
    df["high"] = df["close"] + 1.0
    df["low"] = df["close"] - 1.0
    df["volume"] = 1.0
    df["amount"] = 1.0
    df["dt"] = times
    atr = _atr(df, period=20)
    assert atr[19] == 2.0   # 前 20 根 TR 恒为 2
    assert atr[-1] == 2.0


def test_near_support_helper():
    assert _near_support(10.0, 9.9, 0.015) is True    # 支撑上方且 < +1.5%
    assert _near_support(10.2, 9.9, 0.015) is False   # 超 +3% 不算附近
    assert _near_support(9.8, 9.9, 0.015) is False    # 跌破支撑
    assert _near_support(10.0, 0.0, 0.015) is True    # 无支撑参考时不限


def test_entry_qty_vol_adaptive():
    base = 1.0
    p_off = T0Params(vol_adaptive=False, t_ratio=0.30)
    q0, s0 = _entry_qty(p_off, base, 10.0, 0.0, 1)
    assert q0 == pytest.approx(0.30)
    assert s0 == pytest.approx(10.0 * (1 - 0.015))

    p_on = T0Params(vol_adaptive=True, t_ratio=0.30, risk_per_round_pct=0.05,
                    atr_stop_mult=1.5, stop_pct=0.015)
    # 高波动：atr=1 -> 止损距=max(0.15,1.5)=1.5 -> q=0.05/1.5
    q_hi, s_hi = _entry_qty(p_on, base, 10.0, 1.0, 1)
    assert q_hi == pytest.approx(0.05 / 1.5)
    assert s_hi == pytest.approx(10.0 - 1.5)
    # 低波动：atr=0.05 -> 止损距=max(0.15,0.075)=0.15 -> q=0.05/0.15=0.333 -> 夹到 cap 0.30
    q_lo, _ = _entry_qty(p_on, base, 10.0, 0.05, 1)
    assert q_lo == pytest.approx(0.30)
    assert q_hi < q_lo  # 高波动仓位更小（恒定风险）


def test_support_gate_blocks_long_far_from_support(monkeypatch):
    times = _aday_times()
    n = len(times)
    close = np.concatenate([np.full(10, 10.0), np.full(n - 10, 12.0)])
    diff = np.zeros(n)
    diff[20] = 1.0    # 金叉（price=12，远离支撑 10）
    diff[30] = -1.0   # 死叉
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, times)
    # 支撑位过滤开：金叉处价格(12)远高于支撑(10) -> 不做正T
    trades = simulate_series(bars, T0Params(support_entry=True, require_uptrend=False, allow_reverse=False))
    assert trades == []
    # 支撑位过滤关：正常正T
    trades2 = simulate_series(bars, T0Params(require_uptrend=False, allow_reverse=False))
    assert len(trades2) == 1 and trades2[0].side == "long"


def test_vol_adaptive_near_support_executes(monkeypatch):
    times = _aday_times()
    n = len(times)
    close = np.full(n, 10.0)  # 价格=支撑，金叉在支撑附近
    diff = np.zeros(n)
    diff[20] = 1.0
    diff[30] = -1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, times)
    bars["high"] = close * 1.01   # 制造非零 ATR，走自适应仓位路径
    bars["low"] = close * 0.99
    trades = simulate_series(
        bars, T0Params(vol_adaptive=True, support_entry=True, require_uptrend=False,
                       allow_reverse=False)
    )
    assert len(trades) == 1
    assert trades[0].side == "long"


# ---------------------------------------------------------------------------
# 信号源替换：MACD 柱拐头 + 量能确认（变体 G）
# ---------------------------------------------------------------------------
def test_hist_turn_signals_unit():
    # hist: 0轴上行 -> 谷底拐头 -> 0轴下行 -> 峰顶拐头
    hist = np.array([0.0, -1.0, -2.0, -1.0, 0.0, 1.0, 2.0, 1.0, 0.0])
    up, down = _hist_turn_signals(hist)
    # 向上拐头在 idx3（-2 -> -1，前置 -1<=-2）；idx4..6 是单调上行并非拐头
    assert up[3]
    assert not up[6]
    # 向下拐头在 idx7（2 -> 1，前置 1>=2）
    assert down[7]
    # 不足前两根的 idx0/1 不应触发
    assert not up[0] and not up[1]


def test_vol_ratio_array_unit():
    vol = np.ones(25)
    vol[10] = 2.4   # 放量
    vol[20] = 0.5   # 缩量
    r = _vol_ratio_array(vol, period=20)
    assert r[10] > 1.2   # 放量处高于均线
    assert r[20] < 1.0   # 缩量处低于均线


def test_hist_turn_mode_enters_on_turn_not_cross(monkeypatch):
    n = 40
    close = np.concatenate([_steady_price(10, 10.0), np.linspace(10.0, 12.0, 20)[1:], _steady_price(10, 12.0)])
    close = np.pad(close, (0, max(0, n - len(close))), constant_values=12.0)[:n]
    # 0 轴下方的 MACD 柱拐头：hist 在 idx9/10/11 = -2/-1.5/-1.0 形状
    # （idx10 比前一根短 = 向上拐头），全程无金叉(均<=0)
    diff = np.full(n, -1.0)
    diff[9] = -2.0
    diff[10] = -1.5
    diff[11] = -1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    # cross 模式：无金叉 -> 0 笔
    trades_cross = simulate_series(bars, T0Params(require_uptrend=False, allow_reverse=False))
    assert trades_cross == []
    # hist_turn 模式：idx10 向上拐头(0轴下) -> 1 笔正T
    trades_turn = simulate_series(bars, T0Params(require_uptrend=False, allow_reverse=False,
                                                 signal_mode="hist_turn"))
    assert len(trades_turn) == 1
    assert trades_turn[0].side == "long"


def test_vol_confirm_requires_volume_spike(monkeypatch):
    n = 40
    close = np.concatenate([_steady_price(10, 10.0), np.linspace(10.0, 12.0, 20)[1:], _steady_price(10, 12.0)])
    close = np.pad(close, (0, max(0, n - len(close))), constant_values=12.0)[:n]
    diff = np.zeros(n)
    diff[10] = 1.0    # 金叉开多
    diff[11:] = 1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    bars["volume"] = 1.0   # 默认均量=1，无放量
    # 量能确认开：金叉处无放量 -> 不开正T（缩量死叉才是出场，这里只有开多信号）-> 0 笔
    trades_no_vol = simulate_series(
        bars, T0Params(vol_confirm=True, require_uptrend=False, allow_reverse=False)
    )
    assert trades_no_vol == []
    # 让 idx10 放量（volume 远大于均量）
    bars2 = bars.copy()
    bars2.loc[bars2.index[10], "volume"] = 5.0
    trades_vol = simulate_series(
        bars2, T0Params(vol_confirm=True, require_uptrend=False, allow_reverse=False)
    )
    assert len(trades_vol) == 1
    assert trades_vol[0].side == "long"


def test_g_variant_runs_on_real_data():
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    p = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="hist_turn", vol_confirm=True,
    )
    trades = simulate_series(bars, p)
    agg = aggregate_trades(trades)
    assert agg["rounds"] >= 0  # 不要求必须有信号，整条链路不崩即可


# ---------------------------------------------------------------------------
# 非顺势信号源：支撑/阻力均值回归（signal_mode=reversal）与 MACD 柱背离（divergence）
# ---------------------------------------------------------------------------
def test_recent_extremes_unit():
    times = _aday_times()
    n = len(times)
    df = pd.DataFrame({"close": np.full(n, 10.0)})
    df["open"] = df["close"]
    df["high"] = np.full(n, 10.2)     # 全段阻力参考 10.2
    df["low"] = np.concatenate([np.full(6, 9.5), np.full(n - 6, 10.0)])  # 前 6 根出现 9.5 低点
    df["volume"] = 1.0
    df["amount"] = 1.0
    df["dt"] = times
    rlow, rhigh = _recent_extremes(df, lookback=60)
    # 用 shift1 窗口：第 0 根无前序 -> bfill 取全局低 9.5；低点出现后的 bar 应取到 9.5
    assert rlow[0] == pytest.approx(9.5)
    assert rlow[10] == pytest.approx(9.5)
    # 阻力参考始终 10.2（低位 9.5 不影响 high 序列）
    assert rhigh[0] == pytest.approx(10.2)


def test_divergence_signals_unit():
    # 价格两次探底 10.0 -> 9.5（新低），但 MACD 柱 -1.0 -> -0.5（未新低）=> 看多背离
    close = np.array([10.0, 10.0, 9.5, 9.6, 10.0])
    hist = np.array([-1.0, -1.0, -0.5, -0.4, -0.3])
    bull, bear = _divergence_signals(close, hist, lookback=4, tol=0.005)
    # idx2 价格 9.5 <= 前低(10.0)*1.005 且 hist -0.5 > 前低 hist(-1.0) -> 看多背离
    assert bull[2]
    # 价格新高 + 柱未新高 的看空背离场景
    close2 = np.array([10.0, 10.5, 10.4, 10.6])
    hist2 = np.array([0.1, 0.4, 0.35, 0.3])  # 价格 10.6 新高，但柱 0.3 < 前高 0.4
    bull2, bear2 = _divergence_signals(close2, hist2, lookback=4, tol=0.005)
    assert bear2[3]


def test_reversal_mode_enters_at_support(monkeypatch):
    times = _aday_times()
    n = len(times)
    # 价格：前段平稳 10.0 -> 第 16 根探至 9.5(支撑) -> 随后回升破前高，触发阻力止盈
    close = np.concatenate([
        np.full(16, 10.0),
        np.linspace(9.5, 10.5, n - 16),
    ])
    low = close.copy()
    high = close.copy()
    diff = np.full(n, -1.0)
    diff[16] = -0.9   # 第 16 根动能转强(hist 由 -1.0 升至 -0.9)，且价格处于支撑 -> 买
    diff[17:] = -0.9  # 之后动能平稳，靠阻力位止盈出场
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, times)
    bars["low"] = low
    bars["high"] = high
    p = T0Params(
        signal_mode="reversal", require_uptrend=False, allow_reverse=False,
        support_entry=False, vol_confirm=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "long"
    assert t.entry_idx == 16
    assert t.pnl > 0   # 支撑买、阻力(破前高)卖，均值回归盈利


def test_divergence_mode_enters_on_bullish(monkeypatch):
    times = _aday_times()
    n = len(times)
    # 第 16 根价格创阶段新低 9.5，但 MACD 柱 -0.5 高于此前最低 -1.0 => 看多背离
    close = np.full(n, 10.0)
    close[16] = 9.5
    close[17:] = 10.0
    diff = np.full(n, -1.0)
    diff[16] = -0.5   # 柱未随价格创出新低
    diff[17] = -1.0   # 下一根动能回落 -> 触发出场（hist_down）
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, times)
    p = T0Params(
        signal_mode="divergence", require_uptrend=False, allow_reverse=False,
        support_entry=False, vol_confirm=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
        divergence_lookback=10, divergence_tol=0.005,
    )
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    assert trades[0].side == "long"
    assert trades[0].entry_idx == 16


def test_h_variant_runs_on_real_data():
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    for mode in ("reversal", "divergence"):
        p = T0Params(
            trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
            vol_adaptive=True, support_entry=True,
            signal_mode=mode, vol_confirm=True,
        )
        trades = simulate_series(bars, p)
        agg = aggregate_trades(trades)
        assert agg["rounds"] >= 0  # 整条链路不崩即可


# ---------------------------------------------------------------------------
# 更高周期方向偏置（变体 K）：_higher_tf_bias 单元 + 端到端方向闸门
# ---------------------------------------------------------------------------
def test_higher_tf_bias_unit():
    # 前 10 天每天单调上行、后 10 天每天单调下行；足够让 60 分钟 EMA(38) 充分预热
    bars = _phase_bars(days_up=10, days_down=10)
    bias_map, tf = _higher_tf_bias(bars, 60, 6, 38, 6, "macd")
    vals = list(bias_map.values())
    up_phase = vals[: len(vals) // 2]
    down_phase = vals[len(vals) // 2:]
    # 预热后：上行段应 predominantly 偏多(+1)，下行段应 predominantly 偏空(-1)
    assert sum(1 for v in up_phase if v == 1) >= 0.7 * len(up_phase)
    assert sum(1 for v in down_phase if v == -1) >= 0.7 * len(down_phase)
    assert tf == 60


def test_higher_tf_bias_blocks_against_trend(monkeypatch):
    # 上行日(更高周期偏多)放金叉应允许；下行日(偏空)放金叉应被偏置拦截。
    bars = _phase_bars(days_up=10, days_down=10)
    n = len(bars)
    diff = np.zeros(n)
    up_day = 8 * 48       # 上行日(已预热)
    down_day = 15 * 48    # 下行日
    diff[up_day + 10] = 1.0
    diff[up_day + 30] = -1.0    # 上行日金叉开多/死叉平 -> 偏多允许
    diff[down_day + 10] = 1.0
    diff[down_day + 30] = -1.0   # 下行日金叉开多 -> 偏空拦截；死叉无持仓
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))

    p_on = T0Params(
        bias_filter=True, require_uptrend=False, allow_reverse=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades_on = simulate_series(bars, p_on)
    # 仅上行日 1 笔正T（下行日金叉因偏空被拦截）
    assert len(trades_on) == 1
    assert trades_on[0].side == "long"

    p_off = T0Params(
        require_uptrend=False, allow_reverse=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades_off = simulate_series(bars, p_off)
    # 无偏置过滤：上行日+下行日各 1 笔 = 2 笔
    assert len(trades_off) == 2


def test_higher_tf_bias_unit_120():
    # 120 分钟偏置方向性：沿用 20 天单调相位数据，足够 EMA(38) 在 120min 上预热
    # （20 天 * (240/120)=4 根/日 = 80 根 >> 38）。
    bars = _phase_bars(days_up=10, days_down=10)
    bias_map, tf = _higher_tf_bias(bars, 120, 6, 38, 6, "macd")
    vals = list(bias_map.values())
    up_phase = vals[: len(vals) // 2]
    down_phase = vals[len(vals) // 2:]
    assert sum(1 for v in up_phase if v == 1) >= 0.7 * len(up_phase)
    assert sum(1 for v in down_phase if v == -1) >= 0.7 * len(down_phase)
    assert tf == 120


def test_higher_tf_bias_120_blocks_against_trend(monkeypatch):
    # 120 分钟偏置同样应只在更高周期同方向时放行（上行段金叉允许、下行段拦截）。
    bars = _phase_bars(days_up=10, days_down=10)
    n = len(bars)
    diff = np.zeros(n)
    up_day = 8 * 48
    down_day = 15 * 48
    diff[up_day + 10] = 1.0
    diff[up_day + 30] = -1.0
    diff[down_day + 10] = 1.0
    diff[down_day + 30] = -1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))

    p_on = T0Params(
        bias_filter=True, bias_tf=120, require_uptrend=False, allow_reverse=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades_on = simulate_series(bars, p_on)
    assert len(trades_on) == 1
    assert trades_on[0].side == "long"

    p_off = T0Params(
        require_uptrend=False, allow_reverse=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades_off = simulate_series(bars, p_off)
    assert len(trades_off) == 2


# ---------------------------------------------------------------------------
# K 变体真实数据冒烟（无本地数据则跳过）
# ---------------------------------------------------------------------------
def test_k_variant_runs_on_real_data():
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    p = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
        bias_filter=True, bias_tf=60, bias_mode="macd",
    )
    trades = simulate_series(bars, p)
    agg = aggregate_trades(trades)
    assert agg["rounds"] >= 0  # 整条链路不崩即可


def _k_variant_smoke(params: T0Params) -> None:
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    trades = simulate_series(bars, params)
    agg = aggregate_trades(trades)
    assert agg["rounds"] >= 0  # 整条链路不崩即可


def test_k3_variant_runs_on_real_data():
    _k_variant_smoke(T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.0, stop_pct=0.008,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.010,
        take_profit_pct=0.0,
        bias_filter=True, bias_tf=60, bias_mode="macd",
    ))


def test_k4_variant_runs_on_real_data():
    _k_variant_smoke(T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
        bias_filter=True, bias_tf=120, bias_mode="macd",
    ))


def test_k5_variant_runs_on_real_data():
    _k_variant_smoke(T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.0, stop_pct=0.008,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.010,
        take_profit_pct=0.0,
        bias_filter=True, bias_tf=120, bias_mode="macd",
    ))


# ---------------------------------------------------------------------------
# 盈亏比优化：追踪止盈 + 固定止盈（变体 I）
# ---------------------------------------------------------------------------
def test_trailing_tp_exits_on_pullback(monkeypatch):
    # 上行到 +1% 后回落触发追踪止盈（peak=11、回撤 0.5% -> 10.945 离场），盈利落袋
    n = 40
    close = np.concatenate([
        np.full(11, 10.0),            # 前 11 根 10.0（含金叉 idx10）
        np.linspace(10.0, 11.0, 5),   # 缓慢拉到 11.0（peak）
        np.linspace(11.0, 10.9, 4),   # 回落到 10.9（> 11*(1-0.005)=10.945 不触发）
        np.linspace(10.9, 10.93, 4),  # 继续回落到 10.93 < 10.945 -> 触发追踪止盈
        np.full(n - 24, 10.93),
    ])
    close = close[:n]
    diff = np.zeros(n)
    diff[10] = 1.0    # 金叉开多
    diff[11:] = 1.0   # 无死叉，仅靠追踪止盈出场
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    bars["high"] = np.maximum(close, np.roll(close, -1))  # 造非零 ATR，走自适应止损路径
    bars["low"] = np.minimum(close, np.roll(close, -1))
    p = T0Params(
        vol_adaptive=True, trailing_tp=True,
        trail_activate_pct=0.004, trail_pct=0.005,
        max_hold_bars=10 ** 9, stop_pct=10.0,  # 关时间/破位止损，仅看追踪
        require_uptrend=False, allow_reverse=False, support_entry=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    t = trades[0]
    assert t.pnl > 0   # 追踪止盈在高位回撤处锁定盈利
    # 离场价应在 10.93 附近（peak 11.0 回撤 0.5% = 10.945 附近），确为盈利出场
    assert t.exit_price > t.entry_price


def test_take_profit_locks_in_gain(monkeypatch):
    n = 40
    close = np.concatenate([
        np.full(11, 10.0),
        np.full(n - 11, 10.2),   # 直接到 +2%，超过固定止盈 1.5% 阈值
    ])
    diff = np.zeros(n)
    diff[10] = 1.0
    diff[11:] = 1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    bars["high"] = np.maximum(close, np.roll(close, -1))
    bars["low"] = np.minimum(close, np.roll(close, -1))
    p = T0Params(
        vol_adaptive=True, take_profit_pct=0.015,
        max_hold_bars=10 ** 9, stop_pct=10.0,
        require_uptrend=False, allow_reverse=False, support_entry=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades = simulate_series(bars, p)
    assert len(trades) == 1
    assert trades[0].pnl > 0
    # 在固定止盈价(10.0×1.015=10.15)附近离场（允许一个 tick 的浮点误差）
    assert 10.14 <= trades[0].exit_price <= 10.25


def test_trailing_not_activated_when_profit_small(monkeypatch):
    # 仅小幅盈利（< trail_activate_pct）不应触发追踪，持仓保持到时间止损/强制清零
    n = 40
    close = np.concatenate([np.full(11, 10.0), np.full(n - 11, 10.02)])  # +0.2%，未激活
    diff = np.zeros(n)
    diff[10] = 1.0
    diff[11:] = 1.0
    monkeypatch.setattr(t0, "compute_macd", _fake_macd(diff, np.arange(n, dtype=float)))
    bars = _make_bars(close, [datetime(2025, 1, 2, 9, 35) + timedelta(minutes=5 * i) for i in range(n)])
    p = T0Params(
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        max_hold_bars=10 ** 9, stop_pct=10.0,
        require_uptrend=False, allow_reverse=False, support_entry=False,
        min_daily_amp_pct=0.0, min_daily_amount=0.0, trend_filter=False,
    )
    trades = simulate_series(bars, p)
    # 未激活追踪，且无死叉 -> 仅 14:50 强制清零出场（forced）
    assert len(trades) == 1
    assert trades[0].forced is True


def test_i_variant_runs_on_real_data():
    bars = t0._load_code_5m("000001")
    if bars is None:
        pytest.skip("本地 5 分钟数据缺失，跳过")
    p = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
    )
    trades = simulate_series(bars, p)
    agg = aggregate_trades(trades)
    assert agg["rounds"] >= 0  # 整条链路不崩即可


def test_settle_helper_pays_cost():
    # 正T：买 10 卖 10.5，qty=1 -> 毛利 0.5，扣 0.1% 成本后净 < 0.5
    pnl, pct = _settle(1, 10.0, 10.5, 1.0, T0Params(cost_per_round_pct=0.001))
    assert pnl == pytest.approx(0.5 - 0.001 * 10.0)
    assert pct == pytest.approx((10.5 / 10.0 - 1) * 100 - 0.1)
    # 反T：卖 10.5 买 10 -> 毛利 0.5
    pnl2, _ = _settle(-1, 10.5, 10.0, 1.0, T0Params(cost_per_round_pct=0.001))
    assert pnl2 == pytest.approx(0.5 - 0.001 * 10.5)
