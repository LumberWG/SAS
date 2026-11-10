# -*- coding: utf-8 -*-
"""截面统计套利（cross-sectional stat-arb）校验模块（学习研究用途，不接实盘）。

背景
----
单标的时序技术信号（MACD 动量家族 #139~#146、独立 alpha 家族 #147~#148）在本地 5 分钟
OHLCV + ~0.1% 往返成本下，共 34 组配置**全部负期望**。瓶颈是「成本 + 逆向选择 +
5 分钟 OHLCV 信息有限」，不是信号设计问题。

本模块换一个**机制正交**的轴：截面相对价值（cross-sectional relative value）。
- 在每一个 5 分钟截面（同一交易时点），对所有标的计算「价格相对当日累计 VWAP 的偏离
  dev = close/VWAP − 1」，按 dev 排序；
- **做多**偏离最负（最超卖）的一批、**做空**偏离最正（最超买）的一批（longshort 模式），
  或仅做多最超卖（long 模式，契合 A 股 T+1 难做空现实）；
- 标的偏离回归（|dev| 缩到阈值内）或跌出极端分组（再平衡）即平仓；14:50 强制清零不过夜。

关键差异：单标的 VWAP 均值回归赌「绝对回归」，被个股趋势/逆向选择拖累；截面法赌
「相对回归」——同一时点最超卖 vs 最超买，多空对冲掉市场方向 beta，只吃截面离散度的
收敛。这是与前述所有时序信号**根本不同**的 alpha 来源，也是 34 组失败后仍唯一未验证的轴。

工程纪律（与前述一致，保证可复现、无未来函数）：
- dev 用截至当前 bar 的日内累计额/量算 VWAP，只用「当前及以前」信息；
- 截面排序只用当前截面的当前 dev，无跨期/未来信息；
- 共用 T+1/14:50 强平、单笔成本、硬止损、冷却。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.strategies.t0_macd import (
    Trade,
    _load_code_5m,
    _settle,
    aggregate_trades,
)

__all__ = [
    "CSParams",
    "simulate_cross_sectional",
    "simulate_universe_cs",
    "load_industry_map",
]


def load_industry_map(path=None) -> dict:
    """读取 data/industry_map.csv（code,industry,name）为 {code: industry}。

    缺失时返回空字典（调用方退化为非中性）。路径默认 <data_dir>/industry_map.csv。
    """
    if path is None:
        try:
            from app.config import get_settings
            path = str(get_settings().kline_dir.parent / "industry_map.csv")
        except Exception:  # noqa: BLE001
            return {}
    p = Path(path)
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype={"code": str})
    if "code" not in df.columns or "industry" not in df.columns:
        return {}
    return {str(r["code"]).strip().zfill(6): str(r["industry"]) for _, r in df.iterrows()}


def _prepare_df(df: pd.DataFrame, start, end) -> Optional[pd.DataFrame]:
    """计算单标的的日内累计 VWAP 偏离与截面序号（只在 [start,end] 内）。"""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    if len(df) < 30:
        return None
    day = df["date"].dt.normalize()
    vol = df["volume"].astype(float).to_numpy()
    amt = df["amount"].astype(float).to_numpy()
    cv = pd.Series(vol).groupby(day).cumsum().to_numpy(dtype=float)
    ca = pd.Series(amt).groupby(day).cumsum().to_numpy(dtype=float)
    vwap = np.divide(ca, cv, out=np.ones_like(ca), where=cv > 0)
    df["vwap"] = vwap
    df["dev"] = df["close"].astype(float).to_numpy() / vwap - 1.0
    df["intraday_idx"] = df.groupby(day).cumcount()
    return df


def _build_panel(codes, start, end):
    raw: dict = {}
    days_set = set()
    for code in codes:
        bars = _load_code_5m(code)
        if bars is None:
            continue
        df = _prepare_df(bars, start, end)
        if df is None:
            continue
        raw[code] = df
        days_set.update(df["date"].dt.normalize().unique())
    return raw, sorted(days_set)


def _panel_to_grid(raw: dict, days):
    M = 1 + max(int(df["intraday_idx"].max()) for df in raw.values())
    D = len(days)
    daypos = {d: i for i, d in enumerate(days)}
    grid: dict = {}
    slot_time: dict = {}
    liq: dict = {}  # code -> 日成交额中位数（流动性度量）
    for code, df in raw.items():
        dev = np.full((D, M), np.nan)
        close = np.full((D, M), np.nan)
        high = np.full((D, M), np.nan)
        low = np.full((D, M), np.nan)
        open_ = np.full((D, M), np.nan)
        ts = np.full((D, M), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
        day_arr = df["date"].dt.normalize().map(daypos).to_numpy()
        kk_arr = df["intraday_idx"].to_numpy()
        dev[day_arr, kk_arr] = df["dev"].to_numpy()
        close[day_arr, kk_arr] = df["close"].astype(float).to_numpy()
        high[day_arr, kk_arr] = df["high"].astype(float).to_numpy()
        low[day_arr, kk_arr] = df["low"].astype(float).to_numpy()
        open_[day_arr, kk_arr] = df["open"].astype(float).to_numpy()
        ts[day_arr, kk_arr] = df["date"].to_numpy()
        grid[code] = {"dev": dev, "close": close, "high": high, "low": low,
                      "open": open_, "ts": ts}
        # 日成交额中位数
        dlv = df["amount"].astype(float).to_numpy()
        daykey = df["date"].dt.normalize().to_numpy()
        daily_amt = defaultdict(float)
        for dk, a in zip(daykey, dlv):
            daily_amt[dk] += a
        liq[code] = float(np.median(list(daily_amt.values()))) if daily_amt else 0.0
        for (di, kk), t in zip(zip(day_arr.tolist(), kk_arr.tolist()), df["date"].to_numpy()):
            slot_time[(int(di), int(kk))] = t
    return grid, slot_time, D, M, liq


@dataclass
class CSParams:
    """截面统计套利参数。mode=long 仅做多超卖 / longshort 多空配对。

    稳健性增强（#149 续）：
    - industry_neutral：把每标的 dev 对其所在行业的截面均值做残差化，剔除行业 beta；
      需配合 industry_map（code->行业）传入。未覆盖的标的归入 UNKNOWN 桶，退化为原始 dev。
    - liq_weight：仅在截面内流动性前 liq_quantile 分位的标的里选极端偏离，降低薄标的滑点。
      liq 为各标的日成交额中位数（precomputed）。
    - short_cost_pct：空腿融券/借券融资费（按开仓名义额的额外比例，覆盖持有期）。
    """
    mode: str = "long"            # long | longshort
    n_select: int = 5             # 每侧入选标的数
    vwap_entry_pct: float = 0.005  # 偏离需超 ±0.5% 才入选（过滤噪声）
    vwap_exit_pct: float = 0.001   # |dev| 缩到 ±0.1% 内平仓
    cost_per_round_pct: float = 0.0010
    t_ratio: float = 0.30
    max_hold_bars: int = 48
    stop_pct: float = 0.015       # 单标的破位硬止损
    clear_time: str = "14:50"
    cooldown_bars: int = 1
    allow_reverse: bool = True
    require_uptrend: bool = False
    # ---- 稳健性增强 ----
    industry_neutral: bool = False
    short_cost_pct: float = 0.0   # 空腿额外融资费（占名义额比例）
    liq_weight: bool = False
    liq_quantile: float = 0.5     # 仅取流动性前 liq_quantile 分位


def _select(present, params: CSParams, industries=None, liq=None):
    """纯函数：给定当前截面 (code, dev) 列表，返回 (longset, shortset)。便于单测。

    present: list[(code, dev)]，dev 已按需完成行业中性残差化。
    industries: dict code->行业（仅 industry_neutral 时用到，用于残差化）。
    liq: dict code->日成交额中位数（仅 liq_weight 时用到，用于流动性分位过滤）。
    """
    if params.industry_neutral and industries is not None:
        # 同行业截面均值残差化：dev' = dev - mean(dev within industry)
        ind_dev: dict = defaultdict(list)
        ind_of: dict = {}
        for c, d in present:
            ind = industries.get(c, "UNKNOWN")
            ind_dev[ind].append(d)
            ind_of[c] = ind
        ind_mean = {ind: (sum(v) / len(v) if v else 0.0)
                    for ind, v in ind_dev.items()}
        present = [(c, d - ind_mean[ind_of[c]]) for c, d in present]
    if params.liq_weight and liq is not None and len(present) > 1:
        vals = np.array([liq.get(c, 0.0) for c, _ in present], dtype=float)
        if vals.max() > 0:
            thresh = np.quantile(vals, 1.0 - params.liq_quantile)
            present = [(c, d) for c, d in present if liq.get(c, 0.0) >= thresh]
    present = sorted(present, key=lambda x: x[1])
    n = params.n_select
    longs = [c for c, d in present[:n] if d <= -params.vwap_entry_pct]
    if params.mode == "longshort":
        shorts = [c for c, d in present[-n:] if d >= params.vwap_entry_pct]
    else:
        shorts = []
    return set(longs), set(shorts)


def _simulate_grid(grid, slot_time, days, D, M, params: CSParams, base_qty: float = 1.0,
                   industry_map=None, liq=None):
    ch, cm = (int(x) for x in params.clear_time.split(":"))
    clear_min = ch * 60 + cm
    qty = base_qty * params.t_ratio

    state = {c: 0 for c in grid}
    entry_price = {c: 0.0 for c in grid}
    entry_di = {c: -1 for c in grid}
    entry_kk = {c: -1 for c in grid}
    cooldown = {c: 0 for c in grid}
    last_di = {c: -1 for c in grid}
    last_kk = {c: -1 for c in grid}
    last_px = {c: 0.0 for c in grid}
    trades: list[Trade] = []

    use_neutral = params.industry_neutral and industry_map is not None
    use_liq = params.liq_weight and liq is not None

    for di in range(D):
        for kk in range(M):
            # 收集本截面有数据的标的及其 dev
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

            # 行业中性残差化：dev' = dev - 行业截面均值
            if use_neutral:
                ind_dev: dict = defaultdict(list)
                ind_of: dict = {}
                for c, d in present:
                    ind = industry_map.get(c, "UNKNOWN")
                    ind_dev[ind].append(d)
                    ind_of[c] = ind
                ind_mean = {ind: (sum(v) / len(v) if v else 0.0)
                            for ind, v in ind_dev.items()}
                present = [(c, d - ind_mean[ind_of[c]]) for c, d in present]

            # 流动性过滤后的候选池（仅取前 liq_quantile 分位）
            liq_present = {c: liq.get(c, 0.0) for c, _ in present} if use_liq else None

            # 截面排序：做多最负、做空最正
            longset, shortset = _select(present, params,
                                       industries=industry_map if use_neutral else None,
                                       liq=liq_present)

            for c, g in grid.items():
                d = g["dev"][di, kk]
                if np.isnan(d):
                    continue
                px = g["close"][di, kk]
                last_di[c], last_kk[c], last_px[c] = di, kk, px

                if state[c] != 0:
                    do_exit = False
                    if force:
                        do_exit = True
                    elif state[c] == 1:
                        if d >= -params.vwap_exit_pct:
                            do_exit = True
                        elif c not in longset:
                            do_exit = True
                        elif px <= entry_price[c] * (1 - params.stop_pct):
                            do_exit = True
                    else:
                        if d <= params.vwap_exit_pct:
                            do_exit = True
                        elif c not in shortset:
                            do_exit = True
                        elif px >= entry_price[c] * (1 + params.stop_pct):
                            do_exit = True
                    if do_exit:
                        pnl, pct = _settle(state[c], entry_price[c], px, qty, params)
                        # 空腿融券/借券融资费（占名义额）
                        if state[c] == -1 and params.short_cost_pct > 0:
                            extra = qty * entry_price[c] * params.short_cost_pct
                            pnl -= extra
                            denom = qty * entry_price[c]
                            if denom > 0:
                                pct = pnl / denom * 100
                        trades.append(Trade(
                            code=c, day=str(days[di])[:10],
                            side="long" if state[c] == 1 else "short",
                            entry_idx=entry_kk[c], entry_price=entry_price[c],
                            exit_idx=kk, exit_price=px,
                            bars_held=(di - entry_di[c]) * M + (kk - entry_kk[c]),
                            pnl=pnl, pnl_pct=pct, forced=force))
                        state[c] = 0
                        cooldown[c] = params.cooldown_bars
                    continue

                # 空仓：截面入选则入场
                if cooldown[c] > 0:
                    cooldown[c] -= 1
                    continue
                if c in longset and (not params.require_uptrend):
                    state[c] = 1
                    entry_price[c], entry_di[c], entry_kk[c] = px, di, kk
                elif c in shortset and params.allow_reverse and (not params.require_uptrend):
                    state[c] = -1
                    entry_price[c], entry_di[c], entry_kk[c] = px, di, kk

    # 收尾：仍持仓的以最后有效截面价平仓
    for c, g in grid.items():
        if state[c] != 0 and last_di[c] >= 0:
            px = last_px[c]
            pnl, pct = _settle(state[c], entry_price[c], px, qty, params)
            if state[c] == -1 and params.short_cost_pct > 0:
                extra = qty * entry_price[c] * params.short_cost_pct
                pnl -= extra
                denom = qty * entry_price[c]
                if denom > 0:
                    pct = pnl / denom * 100
            trades.append(Trade(
                code=c, day=str(days[last_di[c]])[:10],
                side="long" if state[c] == 1 else "short",
                entry_idx=entry_kk[c], entry_price=entry_price[c],
                exit_idx=last_kk[c], exit_price=px,
                bars_held=(last_di[c] - entry_di[c]) * M + (last_kk[c] - entry_kk[c]),
                pnl=pnl, pnl_pct=pct, forced=True))

    agg = aggregate_trades(trades)
    per_stock = []
    for c in grid:
        ct = [t for t in trades if t.code == c]
        if ct:
            per_stock.append({"code": c, **aggregate_trades(ct)})
    per_stock.sort(key=lambda x: x["total_net_pct"], reverse=True)
    return {"mode": params.mode, "n_select": params.n_select,
            "aggregate": agg, "per_stock": per_stock, "trades": trades, "scanned": len(grid)}


def simulate_cross_sectional(
    codes=None, start: str = "2025-01-02", end: str = "2026-11-09",
    params: Optional[CSParams] = None, raw: Optional[dict] = None, base_qty: float = 1.0,
    industry_map: Optional[dict] = None, liq: Optional[dict] = None,
) -> dict:
    """截面统计套利回测。可直接传 codes（读盘）或预构建的 raw panel（测试注入）。

    industry_map: dict code->行业，配合 params.industry_neutral 做残差化。
    liq: dict code->日成交额中位数；若未传但需流动性加权，则从 raw 估算。
    """
    if params is None:
        params = CSParams()
    if raw is None:
        raw, days = _build_panel(codes, start, end)
    else:
        days = sorted({d for df in raw.values()
                       for d in df["date"].dt.normalize().unique()})
    if not raw:
        return {"mode": params.mode, "n_select": params.n_select,
                "aggregate": aggregate_trades([]), "per_stock": [], "trades": [], "scanned": 0}
    grid, slot_time, D, M, auto_liq = _panel_to_grid(raw, days)
    if liq is None:
        liq = auto_liq
    return _simulate_grid(grid, slot_time, days, D, M, params, base_qty=base_qty,
                          industry_map=industry_map, liq=liq)


def simulate_universe_cs(codes, start, end, params: CSParams, max_stocks: int = None,
                         industry_map: Optional[dict] = None):
    """兼容接口：对前 max_stocks 只回测截面套利（等价于整池截面，因截面本就跨标的）。"""
    if max_stocks is not None:
        codes = codes[:max_stocks]
    return simulate_cross_sectional(codes=codes, start=start, end=end, params=params,
                                    industry_map=industry_map)
