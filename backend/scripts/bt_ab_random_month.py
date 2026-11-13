"""随机一个月周期回测：量价共振2560 (A) + 创业板涨停后5日 (B)。

设计要点：
- 引擎自动在 start_date 前补 _WARMUP_BARS=90 根预热（含趋势滤网所需历史），
  所以一个月(≈22 交易日)窗口完全可行（A=65/B=60 均 < 90+22）。
- 每个随机窗口只抓一次 kline（带预热+指标），A/B 在同一份 kline 上各自 evaluate，
  再分别 simulate（共享同一套风控/资金），并对 A+B 合并事件做一次合并组合回测。
- 趋势滤网沿用用户 live 设置：MA30 + 300/301 等权合成指数，全程只算一次复用。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\bt_ab_random_month.py
    N=10 SAMPLE=200 SEED=  .venv\\Scripts\\python.exe scripts\\bt_ab_random_month.py
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

import app.strategies  # noqa: F401  (触发 github_strategies 注册)
from app.backtest.engine import (  # noqa: E402
    BacktestEngine, BacktestParams, compute_cyb_uptrend_dates, _WARMUP_BARS,
)
from app.data.service import get_kline_cached, get_stock_pool  # noqa: E402
from app.indicators import add_all_indicators  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.analysis.board_index import classify_board  # noqa: E402

N_MONTHS = int(os.environ.get("N", "10"))
SAMPLE = int(os.environ.get("SAMPLE", "200"))
SEED = os.environ.get("SEED")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))  # 随机窗口长度（自然日）
_SEED = int(SEED) if SEED else random.randint(1, 10**9)

# 风控：与用户 live / 先前对照一致
MAX_POS = 8
TRAILING = 0.06
TIME_STOP = 10
COMM = 0.0003
STAMP = 0.001
INIT_CAP = 100_000.0

STRAT_A = "量价共振2560"
STRAT_B = "创业板涨停后5日"


def shift(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


async def fetch_klines(codes: list[str], warmup_start: str, end: str) -> dict:
    klines: dict[str, object] = {}

    async def one(code: str):
        df = await asyncio.to_thread(get_kline_cached, code)
        if df is None or df.empty:
            return
        df = df[df["date"] >= warmup_start]
        df = df[df["date"] <= end]
        if len(df) < 60:  # 至少够 B 的 min_bars
            return
        df = df.reset_index(drop=True)
        df = await asyncio.to_thread(add_all_indicators, df)
        klines[code] = df

    await asyncio.gather(*(one(c) for c in codes))
    return klines


def collect(strategy, klines: dict, start: str) -> list[dict]:
    engine = BacktestEngine()
    evs: list[dict] = []
    for code, df in klines.items():
        start_idx = engine._first_index_on_or_after(df, start)
        found = engine._collect_events(strategy, df, start_idx, code, "")
        evs.extend(found)
    return evs


def sim(params: BacktestParams, events: list[dict], klines: dict, board_uptrend):
    engine = BacktestEngine()
    equity, trades = engine._simulate(params, events, klines, board_uptrend)
    metrics = engine._compute_metrics(equity, trades, params)
    return metrics, len(trades)


def make_params(max_hold: int) -> BacktestParams:
    return BacktestParams(
        strategy="__combo__",
        start_date="2000-01-01", end_date="2099-01-01",
        trend_filter=True, trend_ma=30, trend_index="",
        universe_size=SAMPLE, initial_capital=INIT_CAP,
        codes=[], max_positions=MAX_POS, trailing_pct=TRAILING,
        time_stop_days=TIME_STOP, max_hold_days=max_hold,
        commission=COMM, stamp_tax=STAMP,
    )


async def data_range(proxies: list[str]) -> tuple[str, str]:
    """用几只代表性创业板票的 kline 估计全局交易日范围（同源日历一致）。"""
    lo, hi = "2999-01-01", "2000-01-01"
    for c in proxies:
        df = await asyncio.to_thread(get_kline_cached, c)
        if df is None or df.empty or "date" not in df:
            continue
        d = df["date"].astype(str).str[:10]
        lo = min(lo, d.min())
        hi = max(hi, d.max())
    return lo, hi


async def main() -> None:
    settings = get_settings()
    kdir = str(settings.kline_dir)
    rng = random.Random(_SEED)

    # 股票池：创业板 300/301 抽样
    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if SAMPLE and len(codes) > SAMPLE:
        codes = random.Random(20261001).sample(codes, SAMPLE)

    lo, hi = await data_range(["300750", "300059", "300999", "301089"])
    lo_d = datetime.strptime(lo, "%Y-%m-%d")
    hi_d = datetime.strptime(hi, "%Y-%m-%d")
    # 窗口 end 必须在 [lo+120天, hi-0] 内，保证 start-90>=lo
    earliest_end = lo_d + timedelta(days=120)
    latest_end = hi_d
    span_days = (latest_end - earliest_end).days
    print(f"数据范围: {lo} ~ {hi} | 可用随机窗口 end ∈ [{earliest_end.date()} ~ {latest_end.date()}] ({span_days}天可选)")
    if span_days <= 40:
        print("可用区间过短，无法抽取多个月份，退出。")
        return

    # 抽取 N 个不重复随机 end 日期
    chosen = set()
    windows = []
    tries = 0
    while len(windows) < N_MONTHS and tries < N_MONTHS * 50:
        tries += 1
        end_d = earliest_end + timedelta(days=rng.randint(0, span_days))
        key = end_d.strftime("%Y-%m-%d")
        if key in chosen:
            continue
        chosen.add(key)
        start_d = end_d - timedelta(days=WINDOW_DAYS)
        windows.append((start_d.strftime("%Y-%m-%d"), key))
    windows.sort()
    print(f"随机种子 SEED={_SEED} | 抽取 {len(windows)} 个窗口（每个≈{WINDOW_DAYS}自然日）\n")

    # 趋势滤网只算一次（全数据，等权合成 MA30），所有窗口复用
    board_uptrend = {
        "cyb": compute_cyb_uptrend_dates(kdir, 30, index_symbol=""),
    }

    from app.strategies import registry
    stratA = registry.create(STRAT_A)
    stratB = registry.create(STRAT_B)

    rows = []
    for i, (start, end) in enumerate(windows):
        warmup_start = shift(start, -_WARMUP_BARS)
        klines = await fetch_klines(codes, warmup_start, end)

        evA = collect(stratA, klines, start)
        evB = collect(stratB, klines, start)
        evAB = evA + evB

        mA, nA = sim(make_params(0), evA, klines, board_uptrend)
        mB, nB = sim(make_params(5), evB, klines, board_uptrend)
        mAB, nAB = sim(make_params(0), evAB, klines, board_uptrend)

        rows.append((start, end, mA, nA, mB, nB, mAB, nAB))
        mark = "  ← 随机抽中代表窗口" if i == 0 else ""
        print(f"[{start}~{end}]{mark}")
        print(f"   A 量价共振2560 : 收益{mA.get('total_return',0):+7.2f}% | 胜率{mA.get('win_rate',0):5.1f}% | 成交{nA:>3}笔 | 回撤{mA.get('max_drawdown',0):6.2f}%")
        print(f"   B 涨停后5日   : 收益{mB.get('total_return',0):+7.2f}% | 胜率{mB.get('win_rate',0):5.1f}% | 成交{nB:>3}笔 | 回撤{mB.get('max_drawdown',0):6.2f}%")
        print(f"   A+B 合并组合  : 收益{mAB.get('total_return',0):+7.2f}% | 胜率{mAB.get('win_rate',0):5.1f}% | 成交{nAB:>3}笔 | 回撤{mAB.get('max_drawdown',0):6.2f}%")

    # ---- 聚合 ----
    def agg(idx_trades, idx_ret, idx_win, subset=None):
        rs = subset if subset is not None else rows
        if not rs:
            return (0.0, 0, 0, [])
        rets = [r[idx_ret].get("total_return", 0) for r in rs]
        tr = [r[idx_trades] for r in rs]
        wins = [r[idx_win].get("win_rate", 0) for r in rs]
        pos = sum(1 for x in rets if x > 0)
        return (sum(rets) / len(rets), pos, sum(tr), wins)

    active = [r for r in rows if r[7] > 0]  # 合并组合有成交的窗口
    print(f"\n===== 跨随机窗口聚合（{len(rows)} 个窗口，其中 {len(active)} 个被趋势滤网放行有成交）=====")
    print("【全样本·含被滤网挡掉的0成交月】")
    for label, it, ir, iw in [("A 量价共振2560", 3, 2, 2),
                              ("B 涨停后5日", 5, 4, 4),
                              ("A+B 合并组合", 7, 6, 6)]:
        mean_ret, pos, tot, winrates = agg(it, ir, iw)
        print(f"  {label:14}: 月均收益{mean_ret:+7.2f}% | 正收益月{pos}/{len(rows)} "
              f"| 累计成交{tot}笔 | 各窗胜率{', '.join(f'{w:.0f}%' for w in winrates)}")
    print("【活跃月·仅滤网放行的有成交窗口】")
    for label, it, ir, iw in [("A 量价共振2560", 3, 2, 2),
                              ("B 涨停后5日", 5, 4, 4),
                              ("A+B 合并组合", 7, 6, 6)]:
        mean_ret, pos, tot, winrates = agg(it, ir, iw, active)
        print(f"  {label:14}: 月均收益{mean_ret:+7.2f}% | 正收益月{pos}/{len(active)} "
              f"| 累计成交{tot}笔 | 各窗胜率{', '.join(f'{w:.0f}%' for w in winrates)}")

    print(f"\n注：合并组合中 B 的5日强制退出被放宽(max_hold=0)，仅作组合近似；"
          f"A/B 单独回测为 faithful（B 严格5日）。风控: 最大持仓{MAX_POS}/跟踪{TRAILING}/时间止损{TIME_STOP}日。")


if __name__ == "__main__":
    asyncio.run(main())
