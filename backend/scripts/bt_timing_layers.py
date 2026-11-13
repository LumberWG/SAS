"""分层回测：量价共振2560 (A) + 现有趋势滤网(Mna30等权) 之上叠加两个择时层。

- 层0 base：A + 现有 bottom_trend_filter（MA30 等权 300/301，与 live 一致）
- 层1 +指数择时(#6)：再叠加 index_timing_gate（创业板指 8 指标多数投票）
- 层2 +ETF轮动(#5)：再叠加 etf_rotation_gate（创业板 30d动量>0 且 >60MA）
- 层3 +两者：三层交集

机制：把每个择时层算成「允许交易日集合」，与现有 trend filter 的 cyb 集合取交集，
直接喂给同一个 BacktestEngine._simulate（无需改引擎）。所有数据离线（D: 盘 idx_*.parquet）。

用法：
    N=12 SAMPLE=200 SEED=20261112 .venv\\Scripts\\python.exe scripts/bt_timing_layers.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 离线：快照后台刷新立即失败，复用本地缓存 ----
import app.data.akshare_source as aks
import app.data.snapshot_store as snap


class _FakeDS:
    def get_realtime_snapshot(self, *a, **k):
        raise RuntimeError("offline")

    def get_realtime_indices(self, *a, **k):
        raise RuntimeError("offline")


aks.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]
snap.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]

import app.strategies  # noqa: F401
from app.backtest.engine import (  # noqa: E402
    BacktestEngine, BacktestParams, compute_cyb_uptrend_dates, _WARMUP_BARS,
)
from app.data.service import get_kline_cached, get_stock_pool  # noqa: E402
from app.indicators import add_all_indicators  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.timing import index_timing_gate, etf_rotation_gate  # noqa: E402

N_MONTHS = int(os.environ.get("N", "12"))
SAMPLE = int(os.environ.get("SAMPLE", "200"))
SEED = os.environ.get("SEED")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
_SEED = int(SEED) if SEED else random.randint(1, 10**9)

MAX_POS = 8
TRAILING = 0.06
TIME_STOP = 10
COMM = 0.0003
STAMP = 0.001
INIT_CAP = 100_000.0
STRAT_A = "量价共振2560"


def shift(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


async def fetch_klines(codes, warmup_start, end):
    klines = {}

    async def one(code):
        df = await asyncio.to_thread(get_kline_cached, code)
        if df is None or df.empty:
            return
        df = df[df["date"] >= warmup_start]
        df = df[df["date"] <= end]
        if len(df) < 60:
            return
        df = df.reset_index(drop=True)
        df = await asyncio.to_thread(add_all_indicators, df)
        klines[code] = df

    await asyncio.gather(*(one(c) for c in codes))
    return klines


def collect(strategy, klines, start):
    engine = BacktestEngine()
    evs = []
    for code, df in klines.items():
        start_idx = engine._first_index_on_or_after(df, start)
        evs.extend(engine._collect_events(strategy, df, start_idx, code, ""))
    return evs


def sim(params, events, klines, board_uptrend):
    engine = BacktestEngine()
    equity, trades = engine._simulate(params, events, klines, board_uptrend)
    metrics = engine._compute_metrics(equity, trades, params)
    return metrics, len(trades)


def make_params():
    return BacktestParams(
        strategy="__combo__", start_date="2000-01-01", end_date="2099-01-01",
        trend_filter=True, trend_ma=30, trend_index="",
        universe_size=SAMPLE, initial_capital=INIT_CAP, codes=[],
        max_positions=MAX_POS, trailing_pct=TRAILING, time_stop_days=TIME_STOP,
        max_hold_days=0, commission=COMM, stamp_tax=STAMP,
    )


async def main() -> None:
    settings = get_settings()
    kdir = str(settings.kline_dir)
    rng = random.Random(_SEED)

    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if SAMPLE and len(codes) > SAMPLE:
        codes = random.Random(20261001).sample(codes, SAMPLE)

    lo, hi = "2999-01-01", "2000-01-01"
    for c in ["300750", "300059", "300999", "301089"]:
        df = await asyncio.to_thread(get_kline_cached, c)
        if df is None or df.empty or "date" not in df:
            continue
        d = df["date"].astype(str).str[:10]
        lo, hi = min(lo, d.min()), max(hi, d.max())

    lo_d, hi_d = datetime.strptime(lo, "%Y-%m-%d"), datetime.strptime(hi, "%Y-%m-%d")
    earliest_end = lo_d + timedelta(days=120)
    latest_end = hi_d
    span = (latest_end - earliest_end).days
    print(f"数据范围: {lo} ~ {hi} | 可用窗口 end ∈ [{earliest_end.date()} ~ {latest_end.date()}] ({span}天)")
    if span <= 40:
        print("区间过短，退出。"); return

    chosen = set(); windows = []
    tries = 0
    while len(windows) < N_MONTHS and tries < N_MONTHS * 50:
        tries += 1
        end_d = earliest_end + timedelta(days=rng.randint(0, span))
        key = end_d.strftime("%Y-%m-%d")
        if key in chosen:
            continue
        chosen.add(key)
        windows.append(((end_d - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d"), key))
    windows.sort()
    print(f"随机种子 SEED={_SEED} | 抽取 {len(windows)} 个窗口（≈{WINDOW_DAYS}自然日）\n")

    # 现有趋势滤网（MA30 等权）+ 两个择时层（全数据范围各算一次）
    base_cyb = compute_cyb_uptrend_dates(kdir, 30, index_symbol="")
    idx_set = index_timing_gate(kline_dir=kdir)
    etf_set = etf_rotation_gate(kline_dir=kdir)
    print(f"base(MA30等权) 放行日: {len(base_cyb)} | 指数择时 放行日: {len(idx_set)} | ETF轮动 放行日: {len(etf_set)}")

    from app.strategies import registry
    stratA = registry.create(STRAT_A)

    # 四层闸门（cyb 维度交集）
    layers = {
        "base(A+MA30)": base_cyb,
        "base+指数择时": base_cyb & idx_set,
        "base+ETF轮动": base_cyb & etf_set,
        "base+两者": base_cyb & idx_set & etf_set,
    }

    rows = []
    for i, (start, end) in enumerate(windows):
        warmup_start = shift(start, -_WARMUP_BARS)
        klines = await fetch_klines(codes, warmup_start, end)
        evA = collect(stratA, klines, start)
        row = {"win": (start, end), "nA": len(evA)}
        for lname, allowed in layers.items():
            bu = {"cyb": allowed}
            m, n = sim(make_params(), evA, klines, bu)
            row[lname] = m
            row[lname + "_n"] = n
        rows.append(row)
        print(f"[{start}~{end}]  A信号{row['nA']}笔")
        for lname in layers:
            m = row[lname]
            print(f"   {lname:14}: 收益{m.get('total_return',0):+7.2f}% | 胜率{m.get('win_rate',0):5.1f}% "
                  f"| 成交{row[lname+'_n']:>3}笔 | 回撤{m.get('max_drawdown',0):6.2f}%")

    # 聚合
    print(f"\n===== 跨 {len(rows)} 窗口聚合 =====")
    print("（含被滤网挡掉的 0 成交窗口；月均收益 = 各窗 total_return 均值）")
    for lname in layers:
        rets = [r[lname].get("total_return", 0) for r in rows]
        ns = [r[lname + "_n"] for r in rows]
        pos = sum(1 for x in rets if x > 0)
        active = [r for r in rows if r[lname + "_n"] > 0]
        pos_a = sum(1 for r in active if r[lname].get("total_return", 0) > 0)
        print(f"  {lname:14}: 月均{sum(rets)/len(rets):+7.2f}% | 正收益窗{pos}/{len(rows)} "
              f"| 活跃窗{len(active)} 其中正{pos_a} | 累计成交{sum(ns)}笔")
    print("\n注：风控 最大持仓8/跟踪6%/时间止损10日；A=量价共振2560。四层共用同一批 A 信号，仅闸门不同。")


if __name__ == "__main__":
    asyncio.run(main())
