"""检验「成交额前 50 + 四条件共振 + 空仓铁律」这套选股体系。

来源：一篇知乎短线选股法（2026-10-28 由用户提供，作者「海纳百川」），核心规则：

  1. 池子：每日**两市成交额前 50**（剔除银行/石油/保险巨无霸），把 5000 只砍到 50 只；
  2. 四条件**同时**满足才进观察池：
       ① 当日涨幅 > 5%；
       ② 30 日线向上（MA30 斜率正）；
       ③ 换手率 5%~15%；
       ④ 缩量回踩 5 日线企稳（收盘在 MA5 之上，且成交量 ≤ 5 日均量）；
  3. 空仓铁律：当天筛不出全部达标的票，就**空仓**，不做任何操作。

为什么先做横截面直测（不改 pipeline）：这条规则只给「选哪些票、何时空仓」，
没给仓位/止损/出场；直接塞进 `pipeline` 会被风控噪声盖住（记录 #25）。
这里先回答最底层的两个问题：
  (a) 达标日选出来的票，未来 1/5/10 日有没有**选股 alpha**（口径 A，只看交易日）；
  (b) 「空仓铁律」到底有没有用——把空仓日收益记 0，对比满仓基准（口径 B，含空仓净值）。

口径（尽量贴实盘）：
  - 信号：t 日收盘后用「≤t 的收盘价/成交额/换手/成交量」算，**无未来函数**；
  - 池子：t 日**当日成交额**全市场排名前 50（真实日频，不是 500 日并集）；
  - 成交：t+1 **开盘**买入、t+h 收盘卖出；
  - 买不进：t+1 开盘涨幅 ≥9.5% 视为买不进（涨停/接近涨停开盘），剔除；
  - 基准：**全市场当日等权**（同样 t+1 开盘买、t+h 收盘卖、同样剔除开盘涨停）；
  - 摩擦：报两档单边滑点 0 / 0.3%（与验收协议一致）。

不经过 `_prepare`：它按成交额 TopN 并集截断且只保留窗口最后 320 根 bar。
这里直接读全量 parquet 并**对齐到指数交易日历**（停牌一天不能把动量窗口整体前移）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/amount50_probe.py
    .venv\\Scripts\\python.exe scripts/amount50_probe.py --drop-giants 0   # 不剔除巨无霸
    .venv\\Scripts\\python.exe scripts/amount50_probe.py --start 2023-01-01 --top 50
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import warnings

warnings.filterwarnings("ignore")
np.seterr(all="ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

#: 文章点名的「巨无霸」：银行 / 石油 / 保险龙头。它们常年占成交额前排但波动小，
#: 难同时满足「涨幅>5% + 换手 5-15%」，剔除它们对结论影响极小；这里用代码黑名单近似
#: （项目里没有行业/名称映射文件，snapshot 尚未生成）。
GIANTS: set[str] = {
    # 银行
    "601398", "601288", "601988", "601939", "601328", "601658", "600036",
    "601166", "600000", "601998", "600016", "601229", "600015", "601169",
    "002142", "600928", "601077", "601187", "601528", "601825",
    # 石油石化
    "601857", "600028", "600938", "600346", "000703",
    # 保险
    "601318", "601628", "601601", "601336", "601319", "601628",
}
#: 成交额回看 TopN（文章口径 50）
TOP = 50
#: 30 日线 / 5 日线窗口
MA_TREND, MA_SUPPORT = 30, 5
#: t+1 开盘涨幅超过该比例视为买不进（涨停/接近涨停开盘）
MAX_OPEN_GAIN = 0.095
INDEX_FILE = "idx_sh000001.parquet"


def _normalize_turn(values: np.ndarray) -> np.ndarray:
    """换手率列量纲不统一：东财源存比例(0.57=0.57%)，新浪/腾讯源存百分比(6.3=6.3%)。
    统一归一为比例：>1 视为百分比存储，除以 100。"""
    return np.where(np.isfinite(values) & (values > 1.0), values / 100.0, values)


def _calendar(kline_dir: str) -> list[str]:
    path = os.path.join(kline_dir, INDEX_FILE)
    if not os.path.isfile(path):
        raise SystemExit("缺少指数K线（idx_sh000001），无法确定交易日历")
    df = pd.read_parquet(path)
    return [str(d)[:10] for d in sorted(df["date"])]


def _load_matrix(kline_dir: str, cal: list[str]) -> tuple:
    """把所有个股对齐成 ``(close, open, amount, turnover, volume)`` 矩阵（行=股票，列=交易日）。"""
    cal_np = np.array(cal)
    n = len(cal)
    codes: list[str] = []
    rows: list[tuple] = []
    for name in os.listdir(kline_dir):
        if not name.endswith(".parquet") or name == INDEX_FILE:
            continue
        code = name.split(".")[0]
        if not code.isdigit():
            continue
        path = os.path.join(kline_dir, name)
        try:
            df = pd.read_parquet(
                path, columns=["date", "open", "close", "amount", "turnover_rate", "volume"]
            )
        except Exception:  # noqa: BLE001 - 部分源（新浪/腾讯）无 turnover_rate 列，降级读全列
            try:
                df = pd.read_parquet(path)
            except Exception:  # noqa: BLE001 - 坏文件跳过
                continue
        if df is None or df.empty or "date" not in df:
            continue
        d = df["date"].astype(str).str.slice(0, 10).to_numpy()
        pos = np.searchsorted(cal_np, d)
        ok = (pos < n) & (cal_np[np.clip(pos, 0, n - 1)] == d)
        if not ok.any():
            continue
        pos = pos[ok]
        def _col(c: str) -> np.ndarray:
            return pd.to_numeric(df[c], errors="coerce").to_numpy(dtype="float64")[ok] \
                if c in df.columns else np.full(ok.sum(), np.nan)
        ca = np.full(n, np.nan); ca[pos] = _col("close")
        oa = np.full(n, np.nan); oa[pos] = _col("open")
        aa = np.full(n, np.nan); aa[pos] = _col("amount")
        ta = np.full(n, np.nan)
        ta[pos] = _normalize_turn(_col("turnover_rate"))
        va = np.full(n, np.nan); va[pos] = _col("volume")
        codes.append(code)
        rows.append((ca, oa, aa, ta, va))
    if not codes:
        raise SystemExit("本地缓存里没有可用个股K线")
    return (
        np.vstack([r[0] for r in rows]), np.vstack([r[1] for r in rows]),
        np.vstack([r[2] for r in rows]), np.vstack([r[3] for r in rows]),
        np.vstack([r[4] for r in rows]), codes,
    )


def probe(
    start: str, end: str, top: int, min_gain: float, turn_min: float, turn_max: float,
    horizons: tuple[int, ...], slips: tuple[float, ...], drop_giants: bool,
    relax_shrink: bool = False,
) -> dict:
    settings = get_settings()
    kline_dir = str(settings.kline_dir)
    cal = _calendar(kline_dir)
    close, open_, amount, turn, vol, codes = _load_matrix(kline_dir, cal)
    n_days = len(cal)
    giant_mask = np.array([c in GIANTS for c in codes]) if drop_giants else np.zeros(len(codes), bool)
    max_h = max(horizons)

    cal_arr = np.array(cal)
    t_lo = max(MA_TREND + 1, int(np.searchsorted(cal_arr, start)) if start else MA_TREND + 1)
    t_hi = min(n_days - max_h - 1, int(np.searchsorted(cal_arr, end, side="right")) - 1)

    tags = ("trade", "hold")
    daily_ex: dict[str, list[float]] = {f"{t}|{h}|{s}": [] for t in tags for h in horizons for s in slips}
    daily_ret: dict[str, list[float]] = {f"{t}|{h}|{s}": [] for t in tags for h in horizons for s in slips}
    strat_daily: dict[str, list[float]] = {f"{h}|{s}": [] for h in horizons for s in slips}
    bench_daily: dict[str, list[float]] = {f"{h}|{s}": [] for h in horizons for s in slips}
    days_used = 0
    trade_days = 0
    diag = {"cand": 0, "gain": 0, "ma": 0, "turn": 0, "ma5": 0, "shrink": 0, "all": 0, "turn_med": 0.0}

    for t in range(t_lo, t_hi + 1):
        close_t = close[:, t]
        with np.errstate(divide="ignore", invalid="ignore"):
            day_ret = close_t / close[:, t - 1] - 1.0
        amt = amount[:, t]
        valid_amt = (
            np.isfinite(amt) & (amt > 0) & ~giant_mask
            & np.isfinite(close_t) & (close_t > 0)
        )
        if valid_amt.sum() < top:
            continue
        k = min(top, int(valid_amt.sum()))
        cand_all = np.argpartition(np.where(valid_amt, -amt, np.inf), k - 1)[:k]
        cand = cand_all[valid_amt[cand_all]]
        if cand.size == 0:
            continue
        days_used += 1

        # 四条件（仅对候选）
        ma30 = np.nanmean(close[:, t - MA_TREND + 1:t + 1], axis=1)
        ma30_prev = np.nanmean(close[:, t - MA_TREND:t], axis=1)
        ma5 = np.nanmean(close[:, t - MA_SUPPORT + 1:t + 1], axis=1)
        vol_ma5 = np.nanmean(vol[:, t - MA_SUPPORT + 1:t + 1], axis=1)
        c_close, c_ma30, c_ma30p = close_t[cand], ma30[cand], ma30_prev[cand]
        c_ma5, c_vol, c_volma5 = ma5[cand], vol[:, t][cand], vol_ma5[cand]
        c_day, c_turn = day_ret[cand], turn[:, t][cand]
        # 缩量条件：默认要求「当日成交量 ≤ 5 日均量」（缩量企稳）；
        # relax 模式改为「非放量破位」——只排除「放量且收盘跌破 MA5」的破位阴线。
        if relax_shrink:
            shrink_cond = ~((c_vol > c_volma5) & (c_close < c_ma5))
        else:
            shrink_cond = (c_vol <= c_volma5)
        cond = (
            np.isfinite(c_ma30) & np.isfinite(c_ma30p) & np.isfinite(c_ma5) & np.isfinite(c_volma5)
            & np.isfinite(c_turn)
            & (c_day > min_gain)
            & (c_ma30 > c_ma30p)
            & (c_turn >= turn_min) & (c_turn <= turn_max)
            & (c_close >= c_ma5)
            & shrink_cond
        )
        picks = cand[cond]
        has_trade = picks.size > 0
        diag["cand"] += int(cand.size)
        diag["gain"] += int((c_day > min_gain).sum())
        diag["ma"] += int((c_ma30 > c_ma30p).sum())
        diag["turn"] += int(((c_turn >= turn_min) & (c_turn <= turn_max)).sum())
        diag["ma5"] += int((c_close >= c_ma5).sum())
        diag["shrink"] += int((c_vol <= c_volma5).sum())
        diag["all"] += int(cond.sum())
        diag["turn_med"] += float(np.nanmedian(c_turn)) if np.isfinite(np.nanmedian(c_turn)) else 0.0
        if has_trade:
            trade_days += 1

        for h in horizons:
            buy = open_[:, t + 1]
            sell = close[:, t + h]
            with np.errstate(divide="ignore", invalid="ignore"):
                open_gain = buy / close_t - 1.0
            ok = (
                np.isfinite(buy) & np.isfinite(sell) & (buy > 0) & (sell > 0)
                & np.isfinite(open_gain) & (open_gain < MAX_OPEN_GAIN)
            )
            if ok.sum() < 100:
                continue
            base = sell[ok] / buy[ok] - 1.0
            base_mean = float(base.mean())

            # 次日开盘全涨停买不进的标的，从 picks 中剔除；剩空则当日按空仓计
            sel = picks[ok[picks]] if has_trade else np.array([], dtype=int)
            traded = has_trade and sel.size > 0

            # 口径 A：达标且实际成交交易日的组合收益与超额
            if traded:
                for s in slips:
                    r = (sell[sel] * (1 - s)) / (buy[sel] * (1 + s)) - 1.0
                    mean_ret = float(r.mean())
                    key = f"trade|{h}|{s}"
                    daily_ret[key].append(mean_ret)
                    daily_ex[key].append(mean_ret - base_mean)
            # 口径 B：含空仓的策略净值 vs 满仓基准
            for s in slips:
                if traded:
                    r = (sell[sel] * (1 - s)) / (buy[sel] * (1 + s)) - 1.0
                    strat = float(r.mean())
                else:
                    strat = 0.0
                bench = base_mean
                strat_daily[f"{h}|{s}"].append(strat)
                bench_daily[f"{h}|{s}"].append(bench)

    out: dict[str, dict] = {}
    for tag in tags:
        for h in horizons:
            for s in slips:
                ex = daily_ex[f"{tag}|{h}|{s}"]
                if not ex:
                    out[f"{tag}|{h}|{s}"] = {"days": 0}
                    continue
                out[f"{tag}|{h}|{s}"] = {
                    "days": len(ex),
                    "mean_ret": round(float(np.mean(daily_ret[f"{tag}|{h}|{s}"])) * 100, 2),
                    "mean_excess": round(float(np.mean(ex)) * 100, 2),
                    "median_excess": round(statistics.median(ex) * 100, 2),
                    "positive_day_share": round(sum(1 for v in ex if v > 0) / len(ex) * 100, 1),
                    "worst_day": round(min(ex) * 100, 2),
                    "best_day": round(max(ex) * 100, 2),
                }
    # 口径 B 累计净值与年化超额（按 250 交易日/年近似）
    b_out: dict[str, dict] = {}
    for h in horizons:
        for s in slips:
            sd, bd = strat_daily[f"{h}|{s}"], bench_daily[f"{h}|{s}"]
            if len(sd) < 30:
                b_out[f"{h}|{s}"] = {"days": 0}
                continue
            strat_cum = float(np.prod(np.add(sd, 1.0)) - 1.0)
            bench_cum = float(np.prod(np.add(bd, 1.0)) - 1.0)
            excess_cum = strat_cum - bench_cum
            n = len(sd)
            base = 1.0 + excess_cum
            ann_excess = float(base ** (250.0 / n) - 1.0) if base > 0 else float(excess_cum * (250.0 / n))
            b_out[f"{h}|{s}"] = {
                "days": n,
                "trade_day_share": round(trade_days / days_used * 100, 1),
                "strat_cum": round(strat_cum * 100, 1),
                "bench_cum": round(bench_cum * 100, 1),
                "excess_cum": round(excess_cum * 100, 1),
                "ann_excess": round(ann_excess * 100, 1),
            }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"start": start, "end": end, "days_used": days_used,
                   "trade_days": trade_days, "calendar_days": n_days, "stocks": len(codes)},
        "rule": {
            "top": top, "min_gain": min_gain, "turn": [turn_min, turn_max],
            "ma_trend": MA_TREND, "ma_support": MA_SUPPORT,
            "drop_giants": drop_giants, "giants_count": int(giant_mask.sum()),
            "relax_shrink": relax_shrink,
            "max_open_gain": MAX_OPEN_GAIN, "fill": "t+1 开盘买 / t+h 收盘卖",
            "benchmark": "全市场当日等权（同口径剔除开盘涨停）",
        },
        "horizons": list(horizons), "slips": list(slips),
        "diag": {k: (round(v / days_used, 1) if days_used else 0) for k, v in diag.items()},
        "trade_stats": out, "hold_stats": b_out,
    }


def _print(d: dict) -> None:
    r = d["rule"]
    w = d["window"]
    print(f"\n### 规则：成交额前 {r['top']}（剔巨无霸 {r['giants_count']} 只）× "
          f"涨幅>{r['min_gain']:.0%} & MA{r['ma_trend']}向上 & 换手{r['turn'][0]:.0%}-{r['turn'][1]:.0%} "
          f"& 缩量站稳MA{r['ma_support']}；找不到则空仓")
    print(f"窗口 {w['start']} ~ {w['end']}：{w['days_used']} 个交易日，"
          f"其中达标可交易 {w['trade_days']} 天（{round(w['trade_days']/w['days_used']*100,1)}%），"
          f"{w['stocks']} 只股票，成交口径 {r['fill']}")
    print(f"基准：{r['benchmark']}\n")
    dg = d.get("diag", {})
    if dg:
        print("【诊断 · 每日前 50 候选里各条件的平均满足数】")
        print(f"  候选 {dg['cand']} | 涨幅>{r['min_gain']:.0%} {dg['gain']} | "
              f"MA{r['ma_trend']}向上 {dg['ma']} | 换手{r['turn'][0]:.0%}-{r['turn'][1]:.0%} {dg['turn']} "
              f"(候选换手中位 {dg['turn_med']:.2%}) | "
              f"收盘≥MA{r['ma_support']} {dg['ma5']} | 缩量 {dg['shrink']} | 四条件全 {dg['all']}\n")
    print("【口径 A · 只看达标交易日】")
    print("| 滑点 | 持有 | 平均收益 | 平均超额 | 中位超额 | 正超额日占比 | 最差日 | 最好日 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for h in d["horizons"]:
        for s in d["slips"]:
            c = d["trade_stats"].get(f"trade|{h}|{s}") or {}
            if not c.get("days"):
                print(f"| {s:.1%} | {h}日 | n/a | n/a | n/a | n/a | n/a | n/a |")
                continue
            print(f"| {s:.1%} | {h}日 | {c['mean_ret']:+.2f}% | {c['mean_excess']:+.2f}% | "
                  f"{c['median_excess']:+.2f}% | {c['positive_day_share']:.1f}% | "
                  f"{c['worst_day']:+.2f}% | {c['best_day']:+.2f}% |")
    print("\n【口径 B · 含空仓净值（空仓日收益记 0）】")
    print("| 滑点 | 持有 | 可交易日占比 | 策略累计 | 基准累计 | 累计超额 | 年化超额 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for h in d["horizons"]:
        for s in d["slips"]:
            c = d["hold_stats"].get(f"{h}|{s}") or {}
            if not c.get("days"):
                print(f"| {s:.1%} | {h}日 | n/a | n/a | n/a | n/a | n/a |")
                continue
            print(f"| {s:.1%} | {h}日 | {c['trade_day_share']:.1f}% | {c['strat_cum']:+.1f}% | "
                  f"{c['bench_cum']:+.1f}% | {c['excess_cum']:+.1f}% | {c['ann_excess']:+.1f}% |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-10-28")
    parser.add_argument("--top", type=int, default=TOP, help="每日成交额前 N")
    parser.add_argument("--min-gain", type=float, default=0.05, help="当日涨幅下限（文章 >5%）")
    parser.add_argument("--turn-min", type=float, default=0.05)
    parser.add_argument("--turn-max", type=float, default=0.15)
    parser.add_argument("--horizons", default="1,5,10")
    parser.add_argument("--slips", default="0,0.003")
    parser.add_argument("--drop-giants", type=int, default=1, help="1=剔除银行/石油/保险巨无霸")
    parser.add_argument("--relax-shrink", type=int, default=0,
                        help="1=缩量条件放宽为『非放量破位』（只排除放量跌破MA5）")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    slips = tuple(float(x) for x in args.slips.split(",") if x.strip())
    data = probe(args.start, args.end, args.top, args.min_gain, args.turn_min, args.turn_max,
                 horizons, slips, bool(args.drop_giants), bool(args.relax_shrink))
    _print(data)

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"amount50_probe{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
