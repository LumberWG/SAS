"""检验「10 日涨幅前 10 × 四板块 + 当日涨幅≥10% 领涨龙头」这套短线选股体系。

来源：一篇知乎短线选股法（2026-10-28 由用户提供），核心规则：

  1. 池子：**四大板块分开统计**（沪主板 / 创业板 / 科创板 / 北交所），各取 10 日涨幅前 10，
     合计 40 只观察池；
  2. 只做其中「当日涨幅 ≥10%、率先涨停」的领涨龙头，后排跟风不碰；
  3. 40 只是观察池不是交易池，每天更新，趋势/量能/情绪共振才进。

为什么先做横截面直测（不改 pipeline）：这套规则只给了「选哪些票」，没给仓位/止损/择时，
直接塞进 `pipeline` 会被仓位与风控噪声盖住（记录 #25 的教训）。这里先回答最底层的问题：
**按这套规则选出来的票，未来 1/5/10 日到底有没有超额**。

口径（尽量贴实盘，不做理想化）：
  - 信号：t 日收盘后用「≤t 的收盘价」算 10 日涨幅与当日涨幅，**无未来函数**；
  - 成交：t+1 **开盘**买入、t+h 收盘卖出（h 个交易日）；
  - 买不进：t+1 开盘涨幅 ≥9.5%（涨停/接近涨停开盘）视为无法成交，剔除；
  - 基准：**全市场当日等权**（同样 t+1 开盘买、t+h 收盘卖、同样剔除开盘涨停），
    这样超额里不含 beta；
  - 摩擦：报两档单边滑点 0 / 0.3%（与验收协议一致）。

不经过 `_prepare`：它按成交额 TopN 截断且只保留窗口最后 320 根 bar，
算不了 2023 年的截面。这里直接读全量 parquet 并对齐到指数交易日历。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/mom10_probe.py
    .venv\\Scripts\\python.exe scripts/mom10_probe.py --start 2023-01-01 --top 10
    .venv\\Scripts\\python.exe scripts/mom10_probe.py --min-day-gain 0   # 关掉「≥10%」条件
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

#: 四大板块（文章口径：主板 / 创业板 / 科创板 / 北交所）。
#: 「主板」= 沪深主板合并（600/601/603/605 + 000/001/002/003），
#: 北交所含 43/83/87/88 与 920 新代码段；沪 B（900）不在四板块内。
BOARDS: dict[str, tuple[str, ...]] = {
    "主板": ("600", "601", "603", "605", "000", "001", "002", "003"),
    "创业板": ("300", "301"),
    "科创板": ("688", "689"),
    "北交所": ("43", "83", "87", "88", "92"),
}
#: 动量回看窗口（交易日）——文章的核心论点是「5 日噪音太大、20 日高位、10 日最优」
LOOKBACK = 10
#: t+1 开盘涨幅超过该比例视为买不进（涨停/接近涨停开盘）
MAX_OPEN_GAIN = 0.095
INDEX_FILE = "idx_sh000001.parquet"


def _board_index(code: str) -> int:
    for i, prefixes in enumerate(BOARDS.values()):
        if code.startswith(prefixes):
            return i
    return -1


def _calendar(kline_dir: str) -> list[str]:
    """交易日历取上证指数（已纳入日更；见记录 #37）。"""
    path = os.path.join(kline_dir, INDEX_FILE)
    if not os.path.isfile(path):
        raise SystemExit("缺少指数K线（idx_sh000001），无法确定交易日历")
    df = pd.read_parquet(path)
    return [str(d)[:10] for d in sorted(df["date"])]


def _load_matrix(kline_dir: str, cal: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """把所有个股对齐成 ``(close, open, board)`` 矩阵（行=股票，列=交易日）。

    对齐（而不是只留有 bar 的日子）是必须的：10 日涨幅要按**交易日**算，
    停牌一天不能把窗口整体前移。
    """
    cal_np = np.array(cal)
    n = len(cal)
    codes: list[str] = []
    close_rows: list[np.ndarray] = []
    open_rows: list[np.ndarray] = []
    board_rows: list[int] = []
    for name in os.listdir(kline_dir):
        if not name.endswith(".parquet") or name == INDEX_FILE:
            continue
        code = name.split(".")[0]
        if not code.isdigit():
            continue
        b = _board_index(code)
        if b < 0:
            continue
        try:
            df = pd.read_parquet(
                os.path.join(kline_dir, name), columns=["date", "open", "close"]
            )
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
        c = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype="float64")[ok]
        o = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype="float64")[ok]
        ca = np.full(n, np.nan)
        oa = np.full(n, np.nan)
        ca[pos] = c
        oa[pos] = o
        codes.append(code)
        close_rows.append(ca)
        open_rows.append(oa)
        board_rows.append(b)
    if not codes:
        raise SystemExit("本地缓存里没有可用个股K线")
    return (
        np.vstack(close_rows), np.vstack(open_rows), np.array(board_rows), codes,
    )


def probe(
    start: str, end: str, top: int, min_day_gain: float,
    horizons: tuple[int, ...], slips: tuple[float, ...], lookback: int = LOOKBACK,
) -> dict:
    settings = get_settings()
    kline_dir = str(settings.kline_dir)
    cal = _calendar(kline_dir)
    close, open_, board, codes = _load_matrix(kline_dir, cal)
    n_days = len(cal)
    max_h = max(horizons)

    cal_arr = np.array(cal)
    t_lo = max(lookback + 1, int(np.searchsorted(cal_arr, start)) if start else lookback)
    t_hi = min(n_days - max_h - 1, int(np.searchsorted(cal_arr, end, side="right")) - 1)

    tags = ("all40", "leader")
    daily: dict[str, list[float]] = {f"{t}|{h}|{s}": [] for t in tags for h in horizons for s in slips}
    rets: dict[str, list[float]] = {f"{t}|{h}|{s}": [] for t in tags for h in horizons for s in slips}
    days_used = 0

    for t in range(t_lo, t_hi + 1):
        close_t = close[:, t]
        with np.errstate(divide="ignore", invalid="ignore"):
            ret10 = close_t / close[:, t - lookback] - 1.0
            day = close_t / close[:, t - 1] - 1.0
        tradable = (
            np.isfinite(ret10) & np.isfinite(day) & np.isfinite(close_t) & (close_t > 0)
        )
        if tradable.sum() < 100:
            continue

        # 各板块按 10 日涨幅取前 top（用 argpartition，避免整列排序）
        picks: list[int] = []
        for b in range(len(BOARDS)):
            mask = tradable & (board == b)
            k = int(mask.sum())
            if k < 2:
                continue
            key = np.where(mask, ret10, -np.inf)
            take = min(top, k)
            cand = np.argpartition(-key, take - 1)[:take]
            picks.extend(int(i) for i in cand if mask[i])
        if not picks:
            continue
        picks_arr = np.array(picks, dtype=int)
        days_used += 1

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

            for tag in tags:
                if tag == "all40":
                    sel = picks_arr[ok[picks_arr]]
                else:
                    sel = picks_arr[ok[picks_arr] & (day[picks_arr] >= min_day_gain)]
                if sel.size == 0:
                    continue
                for s in slips:
                    r = (sell[sel] * (1 - s)) / (buy[sel] * (1 + s)) - 1.0
                    mean_ret = float(r.mean())
                    key = f"{tag}|{h}|{s}"
                    rets[key].append(mean_ret)
                    daily[key].append(mean_ret - base_mean)

    out: dict[str, dict] = {}
    for tag in tags:
        for h in horizons:
            for s in slips:
                key = f"{tag}|{h}|{s}"
                ex = daily[key]
                if not ex:
                    out[key] = {"days": 0}
                    continue
                out[key] = {
                    "days": len(ex),
                    "mean_ret": round(float(np.mean(rets[key])) * 100, 2),
                    "mean_excess": round(float(np.mean(ex)) * 100, 2),
                    "median_excess": round(statistics.median(ex) * 100, 2),
                    "positive_day_share": round(sum(1 for v in ex if v > 0) / len(ex) * 100, 1),
                    "worst_day": round(min(ex) * 100, 2),
                    "best_day": round(max(ex) * 100, 2),
                }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"start": start, "end": end, "days_used": days_used,
                   "calendar_days": n_days, "stocks": len(codes)},
        "rule": {
            "lookback": lookback, "top_per_board": top, "boards": list(BOARDS),
            "min_day_gain": min_day_gain, "max_open_gain": MAX_OPEN_GAIN,
            "fill": "t+1 开盘买 / t+h 收盘卖",
            "benchmark": "全市场当日等权（同口径剔除开盘涨停）",
        },
        "horizons": list(horizons), "slips": list(slips),
        "results": out,
    }


def _print(d: dict) -> None:
    r = d["rule"]
    print(f"\n### 规则：{r['lookback']} 日涨幅前 {r['top_per_board']} × "
          f"{len(r['boards'])} 板块（共 {r['top_per_board'] * len(r['boards'])} 只），"
          f"当日涨幅 ≥ {r['min_day_gain']:.0%} 才算龙头")
    print(f"窗口 {d['window']['start']} ~ {d['window']['end']}：{d['window']['days_used']} 个交易日，"
          f"{d['window']['stocks']} 只股票，成交口径 {r['fill']}，"
          f"t+1 开盘涨幅 ≥{r['max_open_gain']:.1%} 视为买不进")
    print(f"基准：{r['benchmark']}\n")
    print("| 口径 | 滑点 | 持有 | 平均收益 | 平均超额 | 中位超额 | 正超额日占比 | 最差日 | 最好日 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for tag, label in (("all40", "40 只观察池"), ("leader", "仅≥10% 龙头")):
        for h in d["horizons"]:
            for s in d["slips"]:
                c = d["results"].get(f"{tag}|{h}|{s}") or {}
                if not c.get("days"):
                    print(f"| {label} | {s:.1%} | {h}日 | n/a | n/a | n/a | n/a | n/a | n/a |")
                    continue
                print(f"| {label} | {s:.1%} | {h}日 | {c['mean_ret']:+.2f}% | "
                      f"{c['mean_excess']:+.2f}% | {c['median_excess']:+.2f}% | "
                      f"{c['positive_day_share']:.1f}% | {c['worst_day']:+.2f}% | "
                      f"{c['best_day']:+.2f}% |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-10-28")
    parser.add_argument("--top", type=int, default=10, help="每板块取前 N")
    parser.add_argument("--lookback", type=int, default=LOOKBACK,
                        help="涨幅回看窗口（文章主张 10 日最优；可传 5/20 做对照）")
    parser.add_argument("--min-day-gain", type=float, default=0.10,
                        help="当日涨幅下限（0=关闭；文章口径 0.10）")
    parser.add_argument("--horizons", default="1,5,10")
    parser.add_argument("--slips", default="0,0.003", help="单边滑点，逗号分隔")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    slips = tuple(float(x) for x in args.slips.split(",") if x.strip())
    data = probe(args.start, args.end, args.top, args.min_day_gain, horizons, slips,
                 args.lookback)
    _print(data)

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"mom10_probe{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
