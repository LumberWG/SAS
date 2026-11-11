# -*- coding: utf-8 -*-
"""纯净超卖抄底回测：唯一条件 = 较近 20 日最高价回撤 ≥18%，分钟线定当日入场时点。

用户口径（2026-11-11）：「如果当日股价在 20 日内低于最高价 18% 的股票就认为是超卖了，
一旦当天止跌或者继续下跌 1% 时入场，不要考虑其他因素」。本脚本**不加**趋势过滤、
14 因子、换手率、成交额、板块、市值等任何其它条件（见 WORKLOG #188 的阴性证据清单）。

固定下来的时点假设（参数化，改了重跑即可）
--------------------------------------------------
1. 超卖基准 ``hh20`` = **T-20 ~ T-1 共 20 个交易日**的日线 ``high`` 最大值，
   不含当日（盘中最高价尚未走完，含当日会引入未来函数）。
2. 盘中首次出现 ``5m close <= hh20 × (1 - drop)`` 才算「当日确认超卖」；
   日线 ``low <= hh20 × (1-drop)`` 只用于加速筛选候选票，不参与成交判定。
3. 确认超卖后，同一根 5m bar 起按顺序检查两个入场触发点，**先到先得、每票每日最多一次**：
   - ``stop_fall``（止跌）：``close >= 当日第一根 5m 的 open``（回到今开、不再跌）；
   - ``down_1pct``（继续下跌 1%）：``close <= 昨收 × (1 - dip)``。
   两者都没出现则当日**不入场**。
4. 成交价 = 触发那根 5m bar 的收盘价；不考虑滑点、涨跌停、停牌。
5. 出场用户未指定：主口径按**持有 hold 个交易日后当日收盘价**卖出（默认 5 日），
   无止损止盈；另给 +1/+3/+5/+10 日的**毛前向收益**（纯价格，不扣费）作事件研究。
6. **三层分解**（避免把市场 beta 当成抄底 alpha）：
   ``intraday`` = 入场价→T+N 收盘（用户口径）；``close`` = 当日收盘→T+N 收盘（纯选股）；
   ``benchmark`` = 同日全市场等权中位；``excess_close = close - benchmark`` 是选股 alpha 的
   最干净判据；``timing = intraday - close`` 是入场时点贡献。显著性**按交易日聚类**
   （同日多票高度相关，用事件级 t 会虚高）。
7. 组合回测：初始 100 万、最多同时持仓 ``--max-positions`` 只、每笔等额、100 股整手、
   当日信号**随机抽样**（固定 seed，避免代码顺序偏差）；佣金万 2.5（最低 5 元）+
   卖出印花千 1；模拟窗末强制平仓，并给同期全市场等权基准。

数据：日线 ``klines/*.parquet``、5m ``minute5_fsdb/*.parquet``（或 ``--freq 1m``）。
分钟库只到 2026-11-09，故随机月在该覆盖内抽取。

用法（backend 目录）:
    .venv\\Scripts\\python.exe scripts\\oversold_intraday_probe.py --tag run1
    .venv\\Scripts\\python.exe scripts\\oversold_intraday_probe.py --month 2026-09 --tag check
产物：data/logs/oversold_intraday_<tag>.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app.config import get_settings  # noqa: E402

DAILY_COLS = ["date", "open", "high", "low", "close"]
COMM = 0.00025
STAMP = 0.0005
MIN_FEE = 5.0
LOT = 100


def _paths() -> tuple[str, str, str]:
    data = str(get_settings().kline_dir).rstrip("/\\")
    root = os.path.dirname(data)
    return data, os.path.join(root, "logs"), root


def _codes(freq: str) -> list[str]:
    """日线与分钟库都有文件的 6 位代码（排除指数文件）。"""
    kdir, _, root = _paths()
    mdir = os.path.join(root, f"minute{freq[0]}_fsdb")
    min_codes = {
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(mdir, "*.parquet"))
    }
    out = []
    for p in glob.glob(os.path.join(kdir, "*.parquet")):
        c = os.path.splitext(os.path.basename(p))[0]
        if len(c) == 6 and c.isdigit() and c in min_codes:
            out.append(c)
    return sorted(out)


def _daily(code: str) -> pd.DataFrame:
    kdir, _, _ = _paths()
    df = pd.read_parquet(os.path.join(kdir, f"{code}.parquet"), columns=DAILY_COLS)
    df["date"] = df["date"].astype(str)
    return df.sort_values("date").reset_index(drop=True)


def _calendar() -> list[str]:
    """用 000001 日线取交易日历，并截到分钟库覆盖末日。"""
    df = _daily("000001")
    days = df["date"].tolist()
    m = pd.read_parquet(
        os.path.join(_paths()[2], "minute5_fsdb", "000001.parquet"),
        columns=["date"])
    last = pd.to_datetime(m["date"]).max().strftime("%Y-%m-%d")
    return [d for d in days if d <= last]


def _pick_month(seed: int, month: str | None) -> tuple[str, list[str]]:
    cal = _calendar()
    months = sorted({d[:7] for d in cal})
    # 需留够 hold+N 日的前向收益观察窗，去掉最后一个月
    months = months[:-1]
    chosen = month or random.Random(seed).choice(months)
    if chosen not in months:
        raise SystemExit(f"月份 {chosen} 不在可用范围 {months[0]}~{months[-1]}")
    return chosen, [d for d in cal if d.startswith(chosen)]


def _scan_candidates(codes: list[str], days: list[str],
                     drop: float) -> tuple[dict, dict]:
    """第一遍：日线算 hh20（T-20~T-1 high max），筛候选；顺带缓存全市场收盘价作基准。"""
    kdir, _, _ = _paths()
    start, end = days[0], days[-1]
    out: dict[str, dict[str, dict]] = {}
    closes: dict[str, dict[str, float]] = {}
    for code in codes:
        try:
            df = pd.read_parquet(os.path.join(kdir, f"{code}.parquet"),
                                 columns=DAILY_COLS)
        except Exception:  # noqa: BLE001
            continue
        df["date"] = df["date"].astype(str)
        df = df.sort_values("date").reset_index(drop=True)
        closes[code] = dict(zip(df["date"], df["close"]))
        hh20 = df["high"].rolling(20).max().shift(1)
        prev_close = df["close"].shift(1)
        mask = (df["date"] >= start) & (df["date"] <= end) & hh20.notna() \
            & (df["low"] <= hh20 * (1.0 - drop))
        if not mask.any():
            continue
        rows = {}
        for i in df.index[mask]:
            rows[str(df.at[i, "date"])] = {
                "hh20": float(hh20.at[i]),
                "prev_close": float(prev_close.at[i]) if pd.notna(prev_close.at[i]) else None,
            }
        out[code] = rows
    return out, closes


def _entries(candidates: dict, days: list[str], drop: float, dip: float,
             freq: str) -> tuple[list[dict], dict]:
    """第二遍：只读候选票的分钟数据，逐日逐 bar 定位入场点。"""
    root = _paths()[2]
    mdir = os.path.join(root, f"minute{freq[0]}_fsdb")
    start, end = days[0], days[-1]
    events: list[dict] = []
    stats = {"armed_days": 0, "no_trigger": 0, "stop_fall": 0, "down_1pct": 0}
    for code, daymap in candidates.items():
        path = os.path.join(mdir, f"{code}.parquet")
        if not os.path.isfile(path):
            continue
        try:
            m = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
        m["date"] = pd.to_datetime(m["date"])
        m = m[(m["date"] >= pd.Timestamp(start)) &
              (m["date"] <= pd.Timestamp(end) + pd.Timedelta(days=1))]
        if m.empty:
            continue
        m["d"] = m["date"].dt.strftime("%Y-%m-%d")
        m["hhmm"] = m["date"].dt.strftime("%H:%M")
        for day, g in m.groupby("d", sort=True):
            info = daymap.get(day)
            if info is None or info["prev_close"] is None:
                continue
            g = g.sort_values("date")
            day_open = float(g.iloc[0]["open"])
            hh20, prev_close = info["hh20"], info["prev_close"]
            if hh20 <= 0:
                continue
            armed = False
            for row in g.itertuples(index=False):
                p = float(row.close)
                if not armed:
                    if p <= hh20 * (1.0 - drop):
                        armed = True
                        stats["armed_days"] += 1
                    else:
                        continue
                if p >= day_open:
                    branch = "stop_fall"
                elif p <= prev_close * (1.0 - dip):
                    branch = "down_1pct"
                else:
                    continue
                stats[branch] += 1
                events.append({"code": code, "date": day, "time": row.hhmm,
                               "price": round(p, 3), "branch": branch,
                               "hh20": round(hh20, 3),
                               "prev_close": round(prev_close, 3),
                               "day_open": round(day_open, 3)})
                break
            else:
                if armed:
                    stats["no_trigger"] += 1
    events.sort(key=lambda e: (e["date"], e["time"], e["code"]))
    return events, stats


def _benchmark(closes: dict, cal: list[str], horizons) -> dict:
    """每个 (日期, N) 的全市场等权中位收益：同日收盘买入、T+N 收盘卖出，用于扣 beta。"""
    idx = {d: i for i, d in enumerate(cal)}
    out: dict[tuple[str, int], float] = {}
    for n in horizons:
        for date in cal:
            i = idx[date]
            if i + n >= len(cal):
                continue
            d2 = cal[i + n]
            vals = []
            for cmap in closes.values():
                p0, p1 = cmap.get(date), cmap.get(d2)
                if p0 and p1 and float(p0) > 0:
                    vals.append((float(p1) / float(p0) - 1.0) * 100)
            if vals:
                out[(date, n)] = float(pd.Series(vals).median())
    return out


def _stat(vals: list[float], by_date: dict[str, list[float]]) -> dict:
    """显著性按**交易日聚类**（同日多票高度相关，用事件级 t 会严重虚高）。"""
    if not vals:
        return {"n": 0}
    s = pd.Series(vals, dtype=float)
    dm = [float(pd.Series(v).mean()) for v in by_date.values() if v]
    t = None
    if len(dm) > 1:
        sd = float(pd.Series(dm).std(ddof=1))
        if sd > 0:
            t = round(float(pd.Series(dm).mean()) / (sd / math.sqrt(len(dm))), 2)
    return {
        "n": len(vals), "median": round(float(s.median()), 2),
        "mean": round(float(s.mean()), 2),
        "win_rate": round(float((s > 0).mean()) * 100, 1),
        "p25": round(float(s.quantile(0.25)), 2),
        "p75": round(float(s.quantile(0.75)), 2),
        "t_daily": t, "n_days": len(dm),
    }


def _forward(events: list[dict], cal: list[str], closes: dict,
             bench: dict, horizons=(1, 3, 5, 10)) -> dict:
    """三层分解：`intraday`(用户口径) / `close`(纯选股) / `benchmark`(全市场 beta)。

    - ``excess_close = close - benchmark``：超卖**选股**有没有 alpha（最干净的判据）
    - ``excess_intraday = intraday - benchmark``：含盘中入场时点的总超额
    - ``timing = intraday - close``：「止跌 / 再跌 1% 入场」这一时点选择的贡献
    """
    idx = {d: i for i, d in enumerate(cal)}
    keys = ("intraday", "close", "benchmark", "excess_close",
            "excess_intraday", "timing")
    buckets: dict[int, dict[str, list[float]]] = {
        n: {k: [] for k in keys} for n in horizons}
    by_date: dict[int, dict[str, dict[str, list[float]]]] = {
        n: {k: {} for k in keys} for n in horizons}

    def add(n: int, k: str, date: str, v: float) -> None:
        buckets[n][k].append(v)
        by_date[n][k].setdefault(date, []).append(v)

    for e in events:
        code, date, px = e["code"], e["date"], e["price"]
        cmap = closes.get(code, {})
        i0 = idx.get(date)
        if i0 is None or px <= 0:
            continue
        c0 = cmap.get(date)
        for n in horizons:
            if i0 + n >= len(cal):
                continue
            pN = cmap.get(cal[i0 + n])
            if pN is None:
                continue
            r_in = (float(pN) / px - 1.0) * 100
            b = bench.get((date, n))
            add(n, "intraday", date, r_in)
            if b is not None:
                add(n, "benchmark", date, b)
                add(n, "excess_intraday", date, r_in - b)
            if c0 and float(c0) > 0:
                r_cl = (float(pN) / float(c0) - 1.0) * 100
                add(n, "close", date, r_cl)
                add(n, "timing", date, r_in - r_cl)
                if b is not None:
                    add(n, "excess_close", date, r_cl - b)
    return {
        f"T+{n}": {k: _stat(buckets[n][k], by_date[n][k]) for k in keys}
        for n in horizons
    }


def _portfolio(events: list[dict], cal: list[str], days: list[str],
               closes: dict, hold: int, max_pos: int, initial: float,
               seed: int) -> dict:
    """每日信号**随机抽样**（固定 seed，避免代码顺序偏差）、等额、持有 hold 日后收盘卖。"""
    idx = {d: i for i, d in enumerate(cal)}
    i0 = idx[days[0]]
    i1 = min(idx[days[-1]] + hold + 2, len(cal) - 1)
    sim_days = cal[i0: i1 + 1]
    by_day: dict[str, list[dict]] = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)
    rng = random.Random(seed)
    cash = initial
    pos: dict[str, dict] = {}
    trades: list[dict] = []
    equity: list[float] = []
    for day in sim_days:
        last = day == sim_days[-1]
        # 先卖出到期持仓（停牌无价顺延，模拟窗末强平）
        for code, p in list(pos.items()):
            px = closes.get(code, {}).get(day)
            if not last and (px is None or idx[day] < p["sell_idx"]):
                continue
            if px is None:
                px = p["price"]
            gross = p["shares"] * float(px)
            fee = max(MIN_FEE, gross * COMM) + gross * STAMP
            cash += gross - fee
            trades.append({"code": code, "buy_date": p["buy_date"],
                           "sell_date": day, "buy_price": p["price"],
                           "sell_price": round(float(px), 3),
                           "pnl_pct": round((float(px) / p["price"] - 1) * 100, 2)})
            del pos[code]
        day_events = list(by_day.get(day, []))
        rng.shuffle(day_events)
        for e in day_events:
            if len(pos) >= max_pos or e["code"] in pos:
                continue
            budget = initial / max_pos
            shares = math.floor(budget / (e["price"] * LOT)) * LOT
            if shares <= 0:
                continue
            gross = shares * e["price"]
            fee = max(MIN_FEE, gross * COMM)
            if gross + fee > cash:
                continue
            cash -= gross + fee
            pos[e["code"]] = {
                "shares": shares, "price": e["price"], "buy_date": day,
                "sell_idx": idx.get(day, 0) + hold,
            }
        mv = sum(p["shares"] * float(closes.get(c, {}).get(day) or p["price"])
                 for c, p in pos.items())
        equity.append(cash + mv)
    if not equity:
        return {"error": "无净值序列"}
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    total = (equity[-1] / initial - 1.0) * 100
    mkt = []
    for cmap in closes.values():
        p0, p1 = cmap.get(days[0]), cmap.get(sim_days[-1])
        if p0 and p1 and float(p0) > 0:
            mkt.append((float(p1) / float(p0) - 1.0) * 100)
    mkt_med = float(pd.Series(mkt).median()) if mkt else None
    return {
        "initial": initial, "sim_days": [sim_days[0], sim_days[-1]],
        "final_equity": round(equity[-1], 2),
        "total_return_pct": round(total, 2),
        "market_median_pct": round(mkt_med, 2) if mkt_med is not None else None,
        "excess_vs_market_pct": round(total - mkt_med, 2) if mkt_med is not None else None,
        "max_drawdown_pct": round(mdd * 100, 2),
        "trade_count": len(trades),
        "win_rate_pct": round(100 * wins / len(trades), 1) if trades else None,
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2)
        if trades else None,
        "open_positions_at_end": len(pos),
        "trades": trades,
    }


def _mc_pair(closes: dict, cal: list[str], days: list[str],
             events: list[dict], hold: int, max_pos: int, initial: float,
             seed: int, n_sims: int) -> dict:
    """同口径蒙特卡洛配对：每日随机抽 N 只超卖票 vs N 只全市场票，均以当日收盘买入。

    单次组合回测只有几十笔，抽样噪声极大；这里重复 n_sims 次给出差值分布，
    是「超卖选股有没有 alpha」最直接的判据（两组唯一差别 = 票池）。
    """
    over: dict[str, list[str]] = {}
    for e in events:
        over.setdefault(e["date"], []).append(e["code"])
    full: dict[str, list[str]] = {
        day: [c for c, cm in closes.items() if cm.get(day)] for day in days
    }

    def build(pool: dict[str, list[str]], rng: random.Random) -> list[dict]:
        ev = []
        for day in days:
            codes = pool.get(day) or []
            if not codes:
                continue
            for c in rng.sample(codes, min(max_pos, len(codes))):
                px = closes.get(c, {}).get(day)
                if px and float(px) > 0:
                    ev.append({"code": c, "date": day, "time": "15:00",
                               "price": float(px), "branch": "mc"})
        return ev

    diffs: list[float] = []
    ro: list[float] = []
    rm: list[float] = []
    for i in range(n_sims):
        s = seed + i * 7919
        a = _portfolio(build(over, random.Random(s)), cal, days, closes,
                       hold, max_pos, initial, s)
        b = _portfolio(build(full, random.Random(s + 1)), cal, days, closes,
                       hold, max_pos, initial, s + 1)
        if "total_return_pct" not in a or "total_return_pct" not in b:
            continue
        d = a["total_return_pct"] - b["total_return_pct"]
        diffs.append(d)
        ro.append(a["total_return_pct"])
        rm.append(b["total_return_pct"])
    if not diffs:
        return {"error": "蒙特卡洛无有效样本"}
    s = pd.Series(diffs, dtype=float)
    t = None
    if len(diffs) > 1 and s.std(ddof=1) > 0:
        t = round(float(s.mean()) / (float(s.std(ddof=1)) / math.sqrt(len(diffs))), 2)
    return {
        "n_sims": len(diffs),
        "oversold_mean_pct": round(float(pd.Series(ro).mean()), 2),
        "market_mean_pct": round(float(pd.Series(rm).mean()), 2),
        "diff_median_pct": round(float(s.median()), 2),
        "diff_mean_pct": round(float(s.mean()), 2),
        "diff_p10_pct": round(float(s.quantile(0.10)), 2),
        "diff_p90_pct": round(float(s.quantile(0.90)), 2),
        "positive_pct": round(float((s > 0).mean()) * 100, 1),
        "t": t,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="纯净超卖抄底 · 分钟级入场回测")
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--month", default=None, help="指定 YYYY-MM；不填则按 seed 随机")
    ap.add_argument("--drop", type=float, default=0.18, help="较 20 日最高价回撤阈值")
    ap.add_argument("--dip", type=float, default=0.01, help="继续下跌幅度")
    ap.add_argument("--hold", type=int, default=5, help="持有交易日数")
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--initial", type=float, default=1_000_000)
    ap.add_argument("--freq", choices=("5m", "1m"), default="5m")
    ap.add_argument("--mc", type=int, default=200,
                    help="同口径蒙特卡洛配对次数（0 = 跳过）")
    ap.add_argument("--tag", default="run1")
    args = ap.parse_args()

    t0 = time.time()
    cal = _calendar()
    month, days = _pick_month(args.seed, args.month)
    print(f"[*] 随机月 = {month}（{len(days)} 个交易日：{days[0]} ~ {days[-1]}），"
          f"drop={args.drop:.0%} dip={args.dip:.0%} hold={args.hold}日 "
          f"freq={args.freq}", flush=True)
    codes = _codes(args.freq)
    print(f"[*] 日线 ∩ 分钟库 = {len(codes)} 只", flush=True)
    cand, closes = _scan_candidates(codes, days, args.drop)
    n_days = sum(len(v) for v in cand.values())
    print(f"[*] 曾满足超卖的 (票,日) = {n_days}，涉及 {len(cand)} 只票", flush=True)
    events, stats = _entries(cand, days, args.drop, args.dip, args.freq)
    print(f"[*] 入场 {len(events)} 笔，触发统计 {stats}", flush=True)
    horizons = (1, 3, 5, 10)
    bench = _benchmark(closes, cal, horizons)
    fwd = _forward(events, cal, closes, bench, horizons)
    pf = _portfolio(events, cal, days, closes, args.hold,
                    args.max_positions, args.initial, args.seed)
    mc = _mc_pair(closes, cal, days, events, args.hold,
                  args.max_positions, args.initial, args.seed, args.mc) \
        if args.mc > 0 else {"skipped": True}

    payload = {
        "tag": args.tag, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "seed": args.seed, "month": month, "days": [days[0], days[-1]],
            "n_trading_days": len(days), "drop": args.drop, "dip": args.dip,
            "hold": args.hold, "max_positions": args.max_positions,
            "initial": args.initial, "freq": args.freq, "universe": len(codes),
            "hh20_def": "T-20~T-1 日线 high 最大值（不含当日）",
            "entry_def": "首次 5m close <= hh20*(1-drop) 后，"
                         "close>=当日首根 open(止跌) 或 close<=昨收*(1-dip)，先到先得",
            "benchmark_def": "同日全市场等权（收盘买 / T+N 收盘卖）中位收益",
            "significance": "按交易日聚类的 t（同日多票不独立）",
            "portfolio_selection": "当日信号随机抽样（固定 seed），不引入排序因子",
            "mc_sims": args.mc,
            "no_other_filters": True,
        },
        "scan": {"candidate_tickets": len(cand), "oversold_pairs": n_days,
                 "trigger_stats": stats, "entries": len(events)},
        "forward_decomposition": fwd,
        "portfolio": pf,
        "monte_carlo": mc,
        "events": events,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _, logdir, _ = _paths()
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, f"oversold_intraday_{args.tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    print("\n--- 三层分解（单位 %，t 按交易日聚类）---", flush=True)
    for k, v in fwd.items():
        ic, cl, bm = v["intraday"], v["close"], v["benchmark"]
        ec, ei, tm = v["excess_close"], v["excess_intraday"], v["timing"]
        print(f"  {k}: 入场口径中位={ic.get('median')} | 纯选股(收盘买)中位={cl.get('median')} "
              f"| 全市场中位={bm.get('median')} | **超额(选股)** 中位={ec.get('median')} "
              f"均值={ec.get('mean')} t={ec.get('t_daily')} 胜率={ec.get('win_rate')}%",
              flush=True)
        print(f"        含时点超额中位={ei.get('median')} t={ei.get('t_daily')} "
              f"| 时点增益中位={tm.get('median')} t={tm.get('t_daily')}", flush=True)
    print("--- 单次组合（随机抽样 / 等额 / 扣费）---", flush=True)
    pf_brief = {k: v for k, v in pf.items() if k != "trades"}
    print(f"  {pf_brief}", flush=True)
    print("--- 蒙特卡洛配对（超卖票池 vs 全市场票池，同口径同 seed）---", flush=True)
    print(f"  {mc}", flush=True)
    print(f"\n产物：{path}（{payload['elapsed_sec']}s）", flush=True)


if __name__ == "__main__":
    main()
