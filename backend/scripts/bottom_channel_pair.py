# -*- coding: utf-8 -*-
"""配对回测：无评分超跌通道 vs 14 因子打分通道（纯研究，不改线上参数）。

依据 `BACKTEST_LOG` #142 / `WORKLOG` #186：动量型 14 因子与超跌方向对立，
线上实证 14 只底反候选 0 只过 `half_score=25` 闸门。本实验在**同一套**
`pipeline._simulate`（次日开盘 / T+1 / 涨跌停 / 同池 / 同费用）里跑三组，
只就地替换 `prepared["signals"]`（#139 `hot20_exclude_probe.py` 已验证的范式），
因此**只有配对差值有效**，绝对收益不可与线上实盘或其它引擎的结果直接比。

| arm | 候选 | 评分 | 趋势过滤 | 离场 |
| --- | --- | --- | --- | --- |
| `default` | 全注册策略 + 自选池 | 14 因子（25/40 分档） | 无 | 线上默认（8% / 20% / 不限持有） |
| `bottom_scored` | 底反四重闸 | 14 因子（同阈值） | 无 | 同 `default` |
| `bottom_no_score` | 底反四重闸 | 无（固定 40 分 / action=buy） | 无 | 同 `default` |
| `bottom_no_score_trend` | 底反四重闸 | 无（固定 40 分 / action=buy） | 有 | 5 日硬到期 + 15% / 25% 止损止盈 |

`bottom_no_score − bottom_scored` 是**只差评分**的干净单变量对照；
`bottom_no_score_trend` 才是 `WORKLOG` #186 定义的完整「无评分平行通道」。
不带趋势过滤的两组刻意保留，否则近半年（合成指数 2026-02-27 后无上升日，#185）
全部空仓，评分效应会被趋势过滤完全掩盖。

- 四重闸与 `screener/bottom_reversal.py`、`BottomReversal.evaluate` 同口径：
  起点 ≤0.40、较 20 日高点回撤 ≥15%、换手 ≥5%、20 日均额 ≥5000 万。
- 趋势过滤 = 创业板等权合成指数 > MA60（#178 证伪官方 399006、#185 证伪关过滤）。
- 股票池统一 `board_prefixes=("cyb",)` + Top300（单 cyb 口径，#182 多板块 −49.88%）。
  基线因此是「同池的 14 因子策略」，不是线上全市场口径；配对差值不受影响。
- `entry_open_delay_minutes=5` 对齐 #108 / 线上冷静期、`--warmup` 伪影不适用
  （`pipeline._load_klines` 保留 320 根 K 线且直接读 parquet，见 #185）。

用法（backend 目录）:
    .venv\\Scripts\\python.exe scripts\\bottom_channel_pair.py --windows A --tag smoke
    .venv\\Scripts\\python.exe scripts\\bottom_channel_pair.py --windows A,B,C,D,E,F,G,H,I,J --jobs 4 --tag all10
产物：data/logs/bottom_channel_pair_<tag>.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import (  # noqa: E402
    PipelineParams,
    _ensure_signals,
    _prepare_cached,
    _rank_score,
    _simulate,
)
from app.config import get_settings  # noqa: E402
from scripts.search import WINDOWS, auto_jobs  # noqa: E402

START_POS_MAX = 0.40
DD20_MIN = 0.15
TURNOVER_MIN = 5.0
MIN_AVG_AMOUNT = 50_000_000.0

ARM_SPEC: dict[str, dict] = {
    "default": {"mode": None, "trend": False, "exit_short": False},
    "bottom_scored": {"mode": "scored", "trend": False, "exit_short": False},
    "bottom_no_score": {"mode": "no_score", "trend": False, "exit_short": False},
    "bottom_no_score_trend": {"mode": "no_score", "trend": True, "exit_short": True},
}
ARMS = tuple(ARM_SPEC)
_UPTREND: set[str] | None = None
_UPTREND_LOADED = False


def _data_root() -> str:
    s = get_settings()
    return os.path.dirname(str(s.kline_dir).rstrip("/\\")) or "."


def _uptrend_dates() -> set[str] | None:
    """创业板等权合成指数 > MA60 的日期集合；每进程只算一次。"""
    global _UPTREND, _UPTREND_LOADED
    if not _UPTREND_LOADED:
        from app.backtest.engine import compute_cyb_uptrend_dates

        try:
            _UPTREND = compute_cyb_uptrend_dates(
                str(get_settings().kline_dir), 60, index_symbol=None)
        except Exception as exc:  # noqa: BLE001 - 取不到就不过滤，但要留痕
            print(f"[!] 趋势指数取数失败，本次不做趋势过滤：{exc}", flush=True)
            _UPTREND = None
        _UPTREND_LOADED = True
    return _UPTREND


def _bottom_dates(prepared: dict) -> dict[str, set[str]]:
    """逐票向量化算四重闸，返回 {code: 信号日集合}（整表算完再按窗口切片，无预热伪影）。"""
    out: dict[str, set[str]] = {}
    for code, df in prepared["klines"].items():
        if len(df) < 121:
            continue
        try:
            close = df["close"].astype(float)
            roll_min = close.rolling(120).min()
            roll_max = close.rolling(120).max()
            start_pos = (close.shift(21) - roll_min) / (roll_max - roll_min)
            dd20 = 1.0 - close / close.rolling(20).max()
            turnover = df["turnover_rate"].astype(float)
            avg20 = df["amount"].astype(float).rolling(20).mean()
            sig = (
                (start_pos <= START_POS_MAX)
                & (dd20 >= DD20_MIN)
                & (turnover >= TURNOVER_MIN)
                & (avg20 >= MIN_AVG_AMOUNT)
            ).fillna(False)
            out[code] = set(df.loc[sig, "date"].astype(str))
        except Exception:  # noqa: BLE001 - 单票缺列不影响整体
            continue
    return out


def _make_signals(prepared: dict, params: PipelineParams,
                  bottoms: dict[str, set[str]], ups: set[str] | None,
                  mode: str) -> dict[str, list[dict]]:
    """构造 `signals`：{date: [{code,name,score,action}]}。

    `scored` 用 `_rank_score`（即线上同一套 14 因子）给底反票打分；
    `no_score` 直接给满分 + buy，绕过 `half_score` 分档闸。
    """
    settings = get_settings()
    half_score = float(params.half_score if params.half_score is not None
                       else settings.paper_half_score)
    full_score = float(params.full_score if params.full_score is not None
                       else settings.paper_full_score)
    names = prepared["names"]
    out: dict[str, list[dict]] = {}
    for date in prepared["dates"]:
        if ups is not None and date not in ups:
            out[date] = []
            continue
        scored: list[dict] = []
        for code in prepared["universe"].get(date, []):
            if date not in bottoms.get(code, ()):
                continue
            if mode == "no_score":
                scored.append({"code": code, "name": names.get(code, code),
                               "score": full_score, "action": "buy"})
                continue
            idx = prepared["pos_index"].get(code, {}).get(date)
            if idx is None:
                continue
            frame = prepared["klines"][code].iloc[: idx + 1]
            try:
                total, action = _rank_score(prepared, params, code, frame, date, half_score)
            except Exception:  # noqa: BLE001
                continue
            scored.append({"code": code, "name": names.get(code, code),
                           "score": float(total), "action": action})
        scored.sort(key=lambda x: -x["score"])
        out[date] = scored
    return out


def _base_params(start: str, end: str, universe: int, delay: int,
                 **overrides) -> PipelineParams:
    kw = dict(
        start_date=start, end_date=end,
        universe_size=universe,
        board_prefixes=("cyb",),
        entry_open_delay_minutes=delay,
        scoring_mode="factors",          # 显式固定，避免随线上 ML 开关漂移
    )
    kw.update(overrides)
    return PipelineParams(**kw)


def _run_window(task: tuple) -> tuple[str, list[dict]]:
    wname, start, end, universe, delay = task
    rows: list[dict] = []
    base = _base_params(start, end, universe, delay)
    t0 = time.time()
    prepared = _prepare_cached(base)
    original = _ensure_signals(prepared, base)
    bottoms = _bottom_dates(prepared)
    ups = _uptrend_dates()
    n_bottom_days = sum(len(v) for v in bottoms.values())
    print(f"[{wname}] 准备完成 {time.time() - t0:.1f}s，"
          f"底反信号(票,日)对 {n_bottom_days}，趋势日 {len(ups) if ups else 'NA'}",
          flush=True)

    for arm in ARMS:
        spec = ARM_SPEC[arm]
        overrides = (dict(max_hold_days=5, stop_loss=0.15, take_profit=0.25)
                     if spec["exit_short"] else {})
        params = _base_params(start, end, universe, delay, **overrides)
        if spec["mode"] is None:
            prepared["signals"] = original
        else:
            prepared["signals"] = _make_signals(
                prepared, params, bottoms, ups if spec["trend"] else None,
                spec["mode"])
        t1 = time.time()
        try:
            result = _simulate(prepared, params)
        except Exception as exc:  # noqa: BLE001 - 单组失败不影响其它组
            print(f"[{wname}/{arm}] 失败：{exc}", flush=True)
            continue
        m = result["metrics"]
        total = float(m.get("total_return") or 0)
        bench = float(m.get("benchmark_return") or 0)
        row = {
            "window": wname, "range": f"{start}~{end}", "arm": arm,
            "total_return": total, "benchmark_return": bench,
            "excess": round(total - bench, 2),
            "max_drawdown": float(m.get("max_drawdown") or 0),
            "sharpe": float(m.get("sharpe") or 0),
            "trade_count": int(m.get("trade_count") or 0),
            "win_rate": float(m.get("win_rate") or 0),
            "final_equity": float(m.get("final_equity") or 0),
            "elapsed": round(time.time() - t1, 1),
        }
        rows.append(row)
        print(f"[{wname}/{arm}] {total:+.2f}% | 基准 {bench:+.2f}% "
              f"| 超额 {row['excess']:+.2f}% | 回撤 {row['max_drawdown']:.2f}% "
              f"| {row['trade_count']} 笔 | 胜率 {row['win_rate']:.1f}% "
              f"| {row['elapsed']}s", flush=True)
    prepared["signals"] = original
    return wname, rows


def _paired(rows: list[dict], left: str, right: str,
            windows: list[str] | None = None) -> dict:
    """同窗口配对差值（left − right），只统计两边都有结果的窗口。"""
    allow = set(windows) if windows is not None else None
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        if allow is not None and r["window"] not in allow:
            continue
        by.setdefault(r["window"], {})[r["arm"]] = r
    diffs, samples = [], []
    for w, arms in sorted(by.items()):
        if left in arms and right in arms:
            d = arms[left]["total_return"] - arms[right]["total_return"]
            diffs.append(d)
            samples.append({"window": w, "diff": round(d, 2)})
    if not diffs:
        return {"n": 0}
    n = len(diffs)
    mean = sum(diffs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0.0
    s = sorted(diffs)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return {
        "n": n,
        "median": round(median, 2),
        "mean": round(mean, 2),
        "positive_pct": round(100.0 * sum(d > 0 for d in diffs) / n, 1),
        "t": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
        "samples": samples,
    }


def _slices(rows: list[dict]) -> dict:
    """切片统计：防止「单窗口暴涨」或「空仓在下跌市躺赢」被误读成通道 alpha。"""
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        by.setdefault(r["window"], {})[r["arm"]] = r
    best = None
    if by:
        diffs = {
            w: a["bottom_no_score_trend"]["total_return"] - a["default"]["total_return"]
            for w, a in by.items()
            if "bottom_no_score_trend" in a and "default" in a
        }
        if diffs:
            best = max(diffs, key=diffs.get)
    allw = sorted(by)
    groups = {
        "all": allw,
        f"exclude_best({best})": [w for w in allw if w != best],
        "in_sample_A_F": [w for w in allw if w in "ABCDEF"],
        "holdout_G_J": [w for w in allw if w in "GHIJ"],
    }
    pairs = {
        "trend_channel_minus_default": ("bottom_no_score_trend", "default"),
        "trend_minus_notrend": ("bottom_no_score_trend", "bottom_no_score"),
        "no_score_minus_scored": ("bottom_no_score", "bottom_scored"),
        "scored_minus_default": ("bottom_scored", "default"),
    }
    out: dict = {
        g: {name: _paired(rows, a, b, ws) for name, (a, b) in pairs.items()}
        for g, ws in groups.items() if ws
    }
    arms_out: dict = {}
    for arm in sorted({r["arm"] for r in rows}):
        rr = [r for r in rows if r["arm"] == arm]
        arms_out[arm] = {
            "mean_return": round(sum(r["total_return"] for r in rr) / len(rr), 2),
            "mean_drawdown": round(sum(r["max_drawdown"] for r in rr) / len(rr), 2),
            "mean_trades": round(sum(r["trade_count"] for r in rr) / len(rr), 1),
            "zero_trade_windows": sorted(r["window"] for r in rr if r["trade_count"] == 0),
        }
    out["arms"] = arms_out
    return out


def _print_slices(slices: dict) -> None:
    for group, stats in slices.items():
        if group == "arms":
            continue
        for name in ("trend_channel_minus_default", "trend_minus_notrend",
                     "no_score_minus_scored"):
            st = stats.get(name, {})
            print(f"  [{group}] {name}: n={st.get('n')} 中位={st.get('median')}pp "
                  f"均值={st.get('mean')}pp 正占比={st.get('positive_pct')}% "
                  f"t={st.get('t')}", flush=True)
    for arm, st in slices.get("arms", {}).items():
        print(f"  [{arm}] 均值收益={st['mean_return']}% 均值回撤={st['mean_drawdown']}% "
              f"均值笔数={st['mean_trades']} 空仓窗口={st['zero_trade_windows']}",
              flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="无评分超跌通道 vs 14 因子 配对回测")
    ap.add_argument("--windows", default="A,B,C,D,E,F,G,H,I,J")
    ap.add_argument("--universe", type=int, default=300, help="每日成交额 TopN（线上口径 300）")
    ap.add_argument("--delay", type=int, default=5, help="开盘后 N 分钟成交（对齐 #108）")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--analyze", default=None,
                    help="只重新分析已有产物 json（补写 slices 并打印），不跑回测")
    args = ap.parse_args()

    if args.analyze:
        with open(args.analyze, encoding="utf-8") as fh:
            payload = json.load(fh)
        payload["slices"] = _slices(payload["rows"])
        with open(args.analyze, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        _print_slices(payload["slices"])
        print(f"已补写切片统计：{args.analyze}", flush=True)
        return

    names = [w.strip().upper() for w in args.windows.split(",") if w.strip()]
    unknown = [w for w in names if w not in WINDOWS]
    if unknown:
        raise SystemExit(f"未知窗口 {unknown}，可用：{sorted(WINDOWS)}")
    tasks = [(w, *WINDOWS[w], args.universe, args.delay) for w in names]
    jobs = args.jobs or auto_jobs()

    t0 = time.time()
    rows: list[dict] = []
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            for _w, part in pool.map(_run_window, tasks):
                rows.extend(part)
    else:
        for task in tasks:
            _w, part = _run_window(task)
            rows.extend(part)

    payload = {
        "tag": args.tag,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "windows": names, "universe": args.universe, "delay": args.delay,
            "board_prefixes": ["cyb"], "scoring_mode": "factors",
            "arms": list(ARMS),
            "note": "同一 pipeline._simulate，只替换 prepared['signals']；"
                    "bottom_no_score_trend 另带趋势过滤 + max_hold_days=5 / stop 15% / take 25%",
        },
        "rows": rows,
        "paired": {
            "no_score_minus_scored": _paired(rows, "bottom_no_score", "bottom_scored"),
            "trend_channel_minus_default": _paired(rows, "bottom_no_score_trend", "default"),
            "trend_minus_notrend": _paired(rows, "bottom_no_score_trend", "bottom_no_score"),
            "scored_minus_default": _paired(rows, "bottom_scored", "default"),
        },
        "slices": _slices(rows),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out_dir = os.path.join(_data_root(), "logs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"bottom_channel_pair_{args.tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n产物：{path}（{payload['elapsed_sec']}s）", flush=True)
    for name, stat in payload["paired"].items():
        print(f"  配对 {name}: n={stat.get('n')} 中位={stat.get('median')}pp "
              f"均值={stat.get('mean')}pp 正占比={stat.get('positive_pct')}% "
              f"t={stat.get('t')}", flush=True)
    _print_slices(payload["slices"])


if __name__ == "__main__":
    main()
