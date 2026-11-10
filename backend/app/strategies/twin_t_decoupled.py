# -*- coding: utf-8 -*-
"""观察池 / 持仓池分离的双向做T（学习研究用途，不接实盘、不依赖融券）。

背景
----
#152 找到全程第一个在「无融券 + 当日平仓 + 零售真实成本 0.15%」下活下来的配置
（双向做T，Top500 底仓篮子，每笔净 +0.063%）。但它要求 **500 只等权底仓** 才能把
截面排干净 —— 绕开了融券，却撞上持仓分散度，对散户同样不现实。

本模块把「信息面」与「执行面」拆开：
- **观察池**（大，如 Top500 或全市场）：只用来计算每个 5 分钟截面 dev 的**分位阈值**，
  衡量「此刻怎样才算全市场意义上的极端」，不产生交易；
- **持仓池**（小，如 10/30/50 只）：真正持有底仓、允许开仓做T 的标的。

三种选择口径（隔离「扩大观察池」的净效果）：
- ``hold_only``：只在持仓池内做截面排序（完全等价 #152 的小池基线，对照组）；
- ``quantile``：观察池 dev 的下 q 分位 / 上 (1−q) 分位作为门槛，持仓股越过门槛即入选
  （不必是全市场绝对 Top-N，只需「相对全市场足够极端」）；持仓池内再取每侧最多
  ``n_select`` 只；
- ``intersect``：严格取观察池绝对 Top-N（``_select``），再与持仓池取交集（最严的口径，
  用于诊断「全市场最极端」与「我恰好持有」的重合度）。

工程纪律（与前序一致，无未来函数）：
- dev = close / 日内累计 VWAP − 1，只用当前及以前信息；
- 观察池分位只用**当前截面**的 dev，无跨期/未来信息；
- 14:50 强制平仓、当日 round-trip、单笔硬止损、冷却；
- ``cap_per`` 与 ``hold_return`` 一律基于**持仓池**，保证与纯持有基线同量纲可加；
- dev 脏值清洗（本地 parquet 存在 close=0 / volume=0 使 VWAP 被置 1，dev 可达
  −100%~+2850%）：``|dev| > dev_cap`` 一律置为 NaN，hold 侧与 observe 侧用同一规则，
  避免污染截面分位与极端组。所有对照变体同等生效。
"""
from __future__ import annotations

import warnings
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
class DecoupledParams:
    """观察池/持仓池分离参数。

    select_mode: hold_only | quantile | intersect
    quantile:    观察池最极端分位（0.01 = 下 1% 分位算超卖、上 1% 算超买）
    n_select:    quantile 模式下持仓池每侧最多开仓数；intersect 模式下观察池每侧 Top-N
    """

    mode: str = "both"                  # both | long | short
    select_mode: str = "quantile"       # hold_only | quantile | intersect
    quantile: float = 0.01
    n_select: int = 3
    vwap_entry_pct: float = 0.005
    vwap_exit_pct: float = 0.001
    cost_per_round_pct: float = 0.0015  # 零售真实往返成本
    t_ratio: float = 0.30
    stop_pct: float = 0.015
    clear_time: str = "14:50"
    cooldown_bars: int = 1
    total_capital: float = 1_000_000.0
    # 数据清洗：本地 parquet 存在 close=0 / volume=0 导致 VWAP 被置 1 的脏 bar，
    # 其 dev 可达 −100% ~ +2850%，会严重拉歪截面分位并污染极端组。统一置为无效。
    clean_dev: bool = True
    dev_cap: float = 0.15          # |dev| > 15% 视为不可交易（覆盖涨跌停并留余量）

    # 真实成本模型（替代固定 cost_per_round_pct）：commission(含最低5元) + 印花税(卖出)
    # + 滑点。仅在 realistic_cost=True 时启用；notional = qty * price，qty 由 cap_per 派生。
    realistic_cost: bool = False
    commission_rate: float = 0.00025   # 万2.5
    min_commission: float = 5.0       # 单笔最低佣金（元）
    stamp_rate: float = 0.0005        # 印花税（仅卖出腿，0.05%）
    slippage_bps: float = 0.0005      # 滑点（往返，占名义额比例，5bps≈0.05%）


def _observe_dev_matrix(observe_raw: dict, days, M: int):
    """把观察池压成一个 (N_obs, D, M) 的 dev 矩阵（float32，省内存）。

    只保留 dev，不构建 close/high/low 等数组 —— 观察池只提供「截面分布」，
    不参与成交。返回 (devmat, codes)。
    """
    codes = list(observe_raw.keys())
    D = len(days)
    daypos = {d: i for i, d in enumerate(days)}
    devmat = np.full((len(codes), D, M), np.nan, dtype=np.float32)
    for j, code in enumerate(codes):
        df = observe_raw[code]
        mapped = df["date"].dt.normalize().map(daypos)
        mask = mapped.notna().to_numpy()
        if not mask.any():
            continue
        di = mapped[mask].to_numpy(dtype=int)
        kk = df["intraday_idx"].to_numpy()[mask]
        dv = df["dev"].to_numpy(dtype=float)[mask]
        ok = kk < M
        devmat[j, di[ok], kk[ok]] = dv[ok]
    return devmat, codes


def _quantile_thresholds(devmat: np.ndarray, q: float):
    """逐截面算观察池 dev 的下 q 分位 / 上 (1−q) 分位，返回两个 (D, M) 矩阵。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        lo = np.nanquantile(devmat, q, axis=0)
        hi = np.nanquantile(devmat, 1.0 - q, axis=0)
    return np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)


def build_sector_observe_codes(hold_codes, ind_map: dict, all_codes, cap: int = 500):
    """构造与持仓股「同行业」的观察池代码列表（用于同质性分位标尺）。

    hold_codes: 持仓池代码；ind_map: {code: industry}；all_codes: 全市场可用代码。
    返回观察池代码（含持仓自身，便于截面可比），按 all_codes 顺序截到 cap。
    若某持仓无行业映射，退化保留（不参与行业扩展）。
    """
    industries = {ind_map.get(c) for c in hold_codes if ind_map.get(c)}
    obs = [c for c in all_codes if ind_map.get(c) in industries]
    # 确保持仓自身一定在观察池里（即便其行业没人匹配）
    for c in hold_codes:
        if c not in obs:
            obs.append(c)
    return obs[:cap]


def _empty(params: DecoupledParams) -> dict:
    return {
        "mode": params.mode, "select_mode": params.select_mode,
        "quantile": params.quantile, "n_select": params.n_select,
        "aggregate": aggregate_trades([]),
        "t_pnl_pct": 0.0, "hold_return": 0.0, "combined": 0.0,
        "beats_hold": False, "trades": [], "scanned": 0,
        "observe_size": 0, "mean_thr_low": 0.0, "mean_thr_high": 0.0,
        "invalid_dev_frac": 0.0,
    }


def _cs_params(p: DecoupledParams, mode: str, n_select: int) -> CSParams:
    return CSParams(
        mode=mode, n_select=n_select,
        vwap_entry_pct=p.vwap_entry_pct, vwap_exit_pct=p.vwap_exit_pct,
    )


def _settle_realistic(state, entry_price, px, qty, params) -> tuple[float, float]:
    """真实成本结算：佣金(含最低5元) + 印花税(卖出腿) + 滑点(往返)。

    与 _settle 同接口，仅在 DecoupledParams.realistic_cost=True 时启用。
    pct 为占建仓名义额(notional_in)的百分比，与 _settle 口径一致。
    """
    if entry_price <= 0 or px <= 0 or qty <= 0:
        return 0.0, 0.0
    notional_in = entry_price * qty
    notional_out = px * qty
    comm = 2.0 * max(params.commission_rate * min(notional_in, notional_out),
                     params.min_commission)
    stamp = params.stamp_rate * notional_out          # 印花税仅卖出腿
    slip = params.slippage_bps * (notional_in + notional_out)
    cost = comm + stamp + slip
    if state == 1:   # 正T：低买高卖
        gross = (px - entry_price) * qty
        pct = (gross - cost) / notional_in * 100
    else:            # 反T：高卖低买
        gross = (entry_price - px) * qty
        pct = (gross - cost) / notional_in * 100
    return gross - cost, pct


def simulate_decoupled(
    hold_codes=None, observe_codes=None,
    start: str = "2025-01-02", end: str = "2026-11-09",
    params: Optional[DecoupledParams] = None,
    raw_hold: Optional[dict] = None, raw_observe: Optional[dict] = None,
    days=None,
) -> dict:
    """观察池/持仓池分离的双向做T 回测。

    ``raw_observe`` 可由调用方一次读盘后共享给多组对照；``raw_hold`` 通常取其子集。
    两者都传入时 ``days`` 由观察池推导（也可显式传入统一基准）。
    返回字段与 ``simulate_twin_t`` 一致，另加 observe_size / mean_thr_* 便于诊断。
    """
    if params is None:
        params = DecoupledParams()
    if raw_observe is None:
        raw_observe, obs_days = _build_panel(observe_codes or hold_codes, start, end)
    else:
        obs_days = sorted({d for df in raw_observe.values()
                           for d in df["date"].dt.normalize().unique()})
    if raw_hold is None:
        raw_hold, _ = _build_panel(hold_codes, start, end)
    if days is None:
        days = obs_days
    if not raw_observe or not raw_hold:
        return _empty(params)

    # 只保留观察池里也存在的持仓（避免持仓股无观察截面可比）
    hold_grid_full, slot_time, D, M, _liq = _panel_to_grid(raw_hold, days)
    devmat, obs_codes = _observe_dev_matrix(raw_observe, days, M)
    observe_size = len(obs_codes)

    # 统一清洗 dev 脏值：hold 侧与 observe 侧用同一规则，保证各变体对照公平
    invalid_dev_frac = 0.0
    if params.clean_dev:
        finite_obs = int(np.isfinite(devmat).sum())
        bad_obs = np.abs(devmat) > params.dev_cap
        devmat[bad_obs] = np.nan
        invalid_dev_frac = (int(bad_obs.sum()) / finite_obs) if finite_obs else 0.0
        for g in hold_grid_full.values():
            dv = g["dev"]
            dv[np.abs(dv) > params.dev_cap] = np.nan
    if devmat.shape[1] != D or devmat.shape[2] != M:
        # 理论不应发生，保底截断/补齐
        M = min(M, devmat.shape[2])
        D = min(D, devmat.shape[1])

    hold_set = set(hold_grid_full)
    use_quantile = params.select_mode == "quantile"
    use_intersect = params.select_mode == "intersect"
    thr_lo = thr_hi = None
    if use_quantile:
        thr_lo, thr_hi = _quantile_thresholds(devmat[:, :D, :M], params.quantile)

    lp_hold = _cs_params(params, "long", params.n_select)
    bp_hold = _cs_params(params, "longshort", params.n_select)
    # intersect 模式下观察池 Top-N 与 #152 的 n_select=5 对齐
    lp_obs = _cs_params(params, "long", 5)
    bp_obs = _cs_params(params, "longshort", 5)

    N = max(1, len(hold_grid_full))
    cap_per = params.total_capital / N
    ch, cm = (int(x) for x in params.clear_time.split(":"))
    clear_min = ch * 60 + cm

    # 持有基线：只用持仓池首/末收盘价
    first_close, last_close = {}, {}
    for c, df in raw_hold.items():
        cl = df["close"].astype(float).to_numpy()
        if len(cl):
            first_close[c] = float(cl[0])
            last_close[c] = float(cl[-1])

    state = {c: 0 for c in hold_grid_full}
    entry_price = {c: 0.0 for c in hold_grid_full}
    entry_kk = {c: -1 for c in hold_grid_full}
    cooldown = {c: 0 for c in hold_grid_full}
    trades: list[Trade] = []
    thr_lo_vals, thr_hi_vals = [], []

    for di in range(D):
        for c in hold_grid_full:      # 每日重置，隔夜无敞口
            state[c] = 0
            entry_price[c] = 0.0
            entry_kk[c] = -1
            cooldown[c] = 0

        for kk in range(M):
            present = []
            for c, g in hold_grid_full.items():
                d = g["dev"][di, kk]
                if not np.isnan(d):
                    present.append((c, float(d)))
            if not present:
                continue
            t = slot_time.get((di, kk))
            bar_min = (int(pd.Timestamp(t).hour) * 60 + int(pd.Timestamp(t).minute)) \
                if t is not None else 0
            force = bar_min >= clear_min

            if use_quantile:
                lo, hi = thr_lo[di, kk], thr_hi[di, kk]
                if np.isfinite(lo) and np.isfinite(hi):
                    thr_lo_vals.append(float(lo))
                    thr_hi_vals.append(float(hi))
                    cand_l = [(c, d) for c, d in present
                              if d <= lo and d <= -params.vwap_entry_pct]
                    cand_s = [(c, d) for c, d in present
                              if d >= hi and d >= params.vwap_entry_pct]
                    cand_l.sort(key=lambda x: x[1])
                    cand_s.sort(key=lambda x: x[1], reverse=True)
                    longset = {c for c, _ in cand_l[:params.n_select]}
                    shortset = {c for c, _ in cand_s[:params.n_select]}
                else:
                    longset, shortset = set(), set()
            elif use_intersect:
                col = devmat[:, di, kk]
                m = ~np.isnan(col)
                obs_present = [(obs_codes[i], float(col[i]))
                               for i in np.flatnonzero(m)]
                lg, _ = _select(obs_present, lp_obs)
                _, sh = _select(obs_present, bp_obs)
                longset, shortset = lg & hold_set, sh & hold_set
            else:  # hold_only：持仓池内自己排序（#152 小池基线）
                lg, _ = _select(present, lp_hold)
                _, sh = _select(present, bp_hold)
                longset, shortset = lg, sh

            for c, g in hold_grid_full.items():
                d = g["dev"][di, kk]
                if np.isnan(d):
                    continue
                px = g["close"][di, kk]
                if px <= 0:
                    continue  # 脏数据（收盘价为 0）

                if state[c] != 0:
                    do_exit = False
                    if force:
                        do_exit = True
                    elif state[c] == 1:
                        if d >= -params.vwap_exit_pct:
                            do_exit = True
                        elif px <= entry_price[c] * (1 - params.stop_pct):
                            do_exit = True
                    else:
                        if d <= params.vwap_exit_pct:
                            do_exit = True
                        elif px >= entry_price[c] * (1 + params.stop_pct):
                            do_exit = True
                    if do_exit:
                        qty = (cap_per * params.t_ratio) / entry_price[c]
                        if params.realistic_cost:
                            pnl, pct = _settle_realistic(state[c], entry_price[c], px, qty, params)
                        else:
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
                if params.mode in ("both", "long") and c in longset \
                        and d <= -params.vwap_entry_pct:
                    state[c] = 1
                    entry_price[c], entry_kk[c] = px, kk
                elif params.mode in ("both", "short") and c in shortset \
                        and d >= params.vwap_entry_pct:
                    state[c] = -1
                    entry_price[c], entry_kk[c] = px, kk

    agg = aggregate_trades(trades)
    t_pnl_total = sum(t.pnl for t in trades)
    t_pnl_pct = (t_pnl_total / params.total_capital) if params.total_capital else 0.0
    hold_rets = []
    for c in hold_grid_full:
        fc, lc = first_close.get(c), last_close.get(c)
        if fc and lc and fc > 0:
            hold_rets.append((lc - fc) / fc)
    hold_return = float(np.mean(hold_rets)) if hold_rets else 0.0

    return {
        "mode": params.mode, "select_mode": params.select_mode,
        "quantile": params.quantile, "n_select": params.n_select,
        "aggregate": agg,
        "t_pnl_pct": t_pnl_pct, "hold_return": hold_return,
        "combined": hold_return + t_pnl_pct, "beats_hold": t_pnl_pct > 0,
        "trades": trades, "scanned": len(hold_grid_full),
        "observe_size": observe_size,
        "mean_thr_low": float(np.mean(thr_lo_vals)) if thr_lo_vals else 0.0,
        "mean_thr_high": float(np.mean(thr_hi_vals)) if thr_hi_vals else 0.0,
        "invalid_dev_frac": invalid_dev_frac,
    }


__all__ = [
    "DecoupledParams",
    "simulate_decoupled",
    "build_sector_observe_codes",
    "load_industry_map",
]
