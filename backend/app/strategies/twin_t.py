# -*- coding: utf-8 -*-
"""双向做T（正T + 反T）回测：在持有的「底仓篮子」上做当日 round-trip，对比纯持有。

学习研究用途，不接实盘、不依赖融券。

背景
----
#151 用户指出「散户做不了融券」，故 #149 的截面 long-short 边缘对散户不可落地。
但边缘本质是截面均值回归（超买回落 / 超卖反弹），可用板哥做T方式在**自有底仓**上捕获：
- 正T：持有股当天最超卖 → 先买后卖，吃反弹；
- 反T：持有股当天最超买 → 先卖底仓后买回，吃回落（= 合成空单，零融券、T+1 合法）。

真做T 必须当日 round-trip，只能吃「日内」回归，吃不到多日完整回归——这是可落地版本
相对 #149 毛边缘的最大代价。本模块把做T P&L 与「纯持有底仓篮子」的基线对照，回答：
> 双向做T 是否能在不融券前提下，给持有组合加分？

工程纪律（与前序模块一致，无未来函数）：
- dev = close / 日内累计 VWAP − 1，只用当前及以前信息；
- 截面排序只用当前截面 dev，无跨期/未来信息；
- 14:50 强制平仓，只做当日 round-trip（隔夜无敞口）；单笔硬止损；冷却。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from app.strategies.cross_sectional import (
    CSParams,
    _build_panel,
    _panel_to_grid,
    _select,
    load_industry_map,
)
from app.strategies.t0_macd import Trade, _settle, aggregate_trades


@dataclass
class TwinTParams:
    """双向做T 参数。mode=both 正T+反T / long 仅正T / short 仅反T（合成空单）。

    t_ratio：每次做T 动用该股票底仓资本的比例（正T 买 additions / 反T 卖底仓）。
    底仓篮子内各股票等权分配 total_capital / N 的资本。
    """

    mode: str = "both"              # both | long | short
    n_select: int = 5
    vwap_entry_pct: float = 0.005   # 偏离需超 ±0.5% 才入选
    vwap_exit_pct: float = 0.001    # |dev| 缩到 ±0.1% 内平仓
    cost_per_round_pct: float = 0.0010  # 往返成本（乐观 0.1%；零售真实 ~0.15%）
    t_ratio: float = 0.30
    stop_pct: float = 0.015         # 单笔破位硬止损
    clear_time: str = "14:50"
    cooldown_bars: int = 1
    industry_neutral: bool = False
    liq_weight: bool = False
    liq_quantile: float = 0.5
    total_capital: float = 1_000_000.0


def _empty(params: TwinTParams) -> dict:
    return {
        "mode": params.mode, "n_select": params.n_select,
        "aggregate": aggregate_trades([]),
        "t_pnl_pct": 0.0, "hold_return": 0.0, "combined": 0.0,
        "beats_hold": False, "trades": [], "scanned": 0,
    }


def _cs_long_params(p: TwinTParams) -> CSParams:
    return CSParams(
        mode="long", n_select=p.n_select,
        vwap_entry_pct=p.vwap_entry_pct, vwap_exit_pct=p.vwap_exit_pct,
        industry_neutral=p.industry_neutral, liq_weight=p.liq_weight,
        liq_quantile=p.liq_quantile,
    )


def _cs_both_params(p: TwinTParams) -> CSParams:
    return CSParams(
        mode="longshort", n_select=p.n_select,
        vwap_entry_pct=p.vwap_entry_pct, vwap_exit_pct=p.vwap_exit_pct,
        industry_neutral=p.industry_neutral, liq_weight=p.liq_weight,
        liq_quantile=p.liq_quantile,
    )


def simulate_twin_t(
    codes=None, start: str = "2025-01-02", end: str = "2026-11-09",
    params: Optional[TwinTParams] = None, industry_map: Optional[dict] = None,
    raw: Optional[dict] = None,
) -> dict:
    """双向做T 回测：在 codes（或预构建 raw 面板）构成的底仓篮子上做当日 round-trip，对比纯持有。

    返回含 aggregate（做T 回合统计）、t_pnl_pct（做T 贡献，占 total_capital%）、
    hold_return（等权纯持有收益）、combined（hold + t_pnl）、beats_hold、trades。
    """
    if params is None:
        params = TwinTParams()
    if raw is None:
        raw, days = _build_panel(codes, start, end)
    else:
        days = sorted({d for df in raw.values()
                       for d in df["date"].dt.normalize().unique()})
    if not raw:
        return _empty(params)

    grid, slot_time, D, M, liq = _panel_to_grid(raw, days)
    N = len(grid)
    cap_per = params.total_capital / N

    ch, cm = (int(x) for x in params.clear_time.split(":"))
    clear_min = ch * 60 + cm
    use_neutral = params.industry_neutral and industry_map is not None
    use_liq = params.liq_weight

    lp = _cs_long_params(params)
    bp = _cs_both_params(params)

    # 持有基线：每只股票窗口内首/末收盘价
    first_close: dict = {}
    last_close: dict = {}
    for c, df in raw.items():
        cl = df["close"].astype(float).to_numpy()
        if len(cl):
            first_close[c] = float(cl[0])
            last_close[c] = float(cl[-1])

    state = {c: 0 for c in grid}
    entry_price = {c: 0.0 for c in grid}
    entry_kk = {c: -1 for c in grid}
    cooldown = {c: 0 for c in grid}
    trades: list[Trade] = []

    for di in range(D):
        # 每日重置做T 仓位（隔夜无敞口，符合 T+0 纪律）
        for c in grid:
            state[c] = 0
            entry_price[c] = 0.0
            entry_kk[c] = -1
            cooldown[c] = 0

        for kk in range(M):
            present = []
            for c, g in grid.items():
                d = g["dev"][di, kk]
                if not np.isnan(d):
                    present.append((c, d))
            if not present:
                continue
            t = slot_time.get((di, kk))
            bar_min = (int(pd.Timestamp(t).hour) * 60 + int(pd.Timestamp(t).minute)) if t is not None else 0
            force = bar_min >= clear_min

            liq_present = {c: liq.get(c, 0.0) for c, _ in present} if use_liq else None
            longset, _ = _select(present, lp,
                                 industries=industry_map if use_neutral else None,
                                 liq=liq_present)
            _, shortset = _select(present, bp,
                                  industries=industry_map if use_neutral else None,
                                  liq=liq_present)

            for c, g in grid.items():
                d = g["dev"][di, kk]
                if np.isnan(d):
                    continue
                px = g["close"][di, kk]
                if px <= 0:
                    continue  # 脏数据（收盘价为 0）跳过本 bar

                if state[c] != 0:
                    do_exit = False
                    if force:
                        do_exit = True
                    elif state[c] == 1:  # 正T 多仓
                        if d >= -params.vwap_exit_pct:
                            do_exit = True
                        elif px <= entry_price[c] * (1 - params.stop_pct):
                            do_exit = True
                    else:                # 反T 空仓（合成空单）
                        if d <= params.vwap_exit_pct:
                            do_exit = True
                        elif px >= entry_price[c] * (1 + params.stop_pct):
                            do_exit = True
                    if do_exit:
                        qty = (cap_per * params.t_ratio) / entry_price[c]
                        pnl, pct = _settle(state[c], entry_price[c], px, qty, params)
                        trades.append(Trade(
                            code=c, day=str(days[di])[:10],
                            side="long" if state[c] == 1 else "short",
                            entry_idx=entry_kk[c], entry_price=entry_price[c],
                            exit_idx=kk, exit_price=px,
                            bars_held=(kk - entry_kk[c]),
                            pnl=pnl, pnl_pct=pct, forced=force))
                        state[c] = 0
                        cooldown[c] = params.cooldown_bars
                    continue

                if cooldown[c] > 0:
                    cooldown[c] -= 1
                    continue

                # 开仓：正T（超卖）/ 反T（超买，合成空单）
                if params.mode in ("both", "long") and c in longset and d <= -params.vwap_entry_pct:
                    state[c] = 1
                    entry_price[c], entry_kk[c] = px, kk
                elif params.mode in ("both", "short") and c in shortset and d >= params.vwap_entry_pct:
                    state[c] = -1
                    entry_price[c], entry_kk[c] = px, kk

    agg = aggregate_trades(trades)
    t_pnl_total = sum(t.pnl for t in trades)
    t_pnl_pct = (t_pnl_total / params.total_capital) if params.total_capital else 0.0

    hold_rets = []
    for c in grid:
        fc = first_close.get(c)
        lc = last_close.get(c)
        if fc and lc and fc > 0:
            hold_rets.append((lc - fc) / fc)
    hold_return = float(np.mean(hold_rets)) if hold_rets else 0.0
    combined = hold_return + t_pnl_pct

    return {
        "mode": params.mode, "n_select": params.n_select,
        "aggregate": agg,
        "t_pnl_pct": t_pnl_pct, "hold_return": hold_return,
        "combined": combined, "beats_hold": t_pnl_pct > 0,
        "trades": trades, "scanned": N,
    }


def simulate_universe_twin(codes, start, end, params: TwinTParams,
                           max_stocks: Optional[int] = None,
                           industry_map: Optional[dict] = None):
    """兼容接口：对前 max_stocks 只构建底仓篮子并做双向做T。"""
    if max_stocks is not None:
        codes = codes[:max_stocks]
    return simulate_twin_t(codes, start=start, end=end, params=params,
                           industry_map=industry_map)


__all__ = [
    "TwinTParams",
    "simulate_twin_t",
    "simulate_universe_twin",
    "load_industry_map",
]
