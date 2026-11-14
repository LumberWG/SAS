# -*- coding: utf-8 -*-
"""market_env 择时配对：空仓 / 当前线上 / 上升市才开仓（纯研究，不改线上参数）。

依据 `BACKTEST_LOG` #145 的 `market_env_eval.json`：隔夜环境对当日收益有统计信号，
但主要在 09:30 开盘跳空，脚本明确 `actionable_after_open=false`。本实验不再直接
用 env_score 打分，而是把它当作「是否允许当日新开仓」的择时闸，与纯价格 MA60
趋势闸做同口径对照，检验 09:30 后链路是否仍可利用。

四臂（三臂 + 两种「上升市」定义）：

| arm | 当日 signals | 含义 |
| --- | --- | --- |
| `empty` | 永远空 | 完全不参与，风险下限对照 |
| `current` | 原始信号 | 当前线上规则基线 |
| `env_risk_on` | risk_on 日才放行 | T 05:30 环境快照（美股隔夜 < T，零前视） |
| `ma60_uptrend` | T-1 中证全指收盘 > MA60 才放行 | 纯价格趋势对照 |

所有臂共用同一份 `prepared`、同一 `_simulate`、同池 / 同费用 / 同 T+1 / 同涨跌停，
只替换 `prepared["signals"]`；因此**配对差值有效**，绝对收益不可与线上实盘直接比。
非放行日只拦「新开仓」，不强制清仓；已有持仓仍按原止损 / 止盈 / 做T 规则处置。

用法（backend 目录）:
    .venv\\Scripts\\python.exe scripts\\market_env_pair.py --windows A --tag smoke
    .venv\\Scripts\\python.exe scripts\\market_env_pair.py --windows A,B,C,D,E,F,G,H,I,J --jobs 4 --tag all10
产物：D:/Backup/SAS/data/logs/market_env_pair_<tag>.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from app.api.backtest import BacktestRequest, build_params  # noqa: E402
from app.backtest.pipeline import (  # noqa: E402
    PipelineParams,
    _ensure_signals,
    _prepare_cached,
    _simulate,
)
from app.config import get_settings  # noqa: E402
from app.data.cache import db  # noqa: E402
from app.param_specs import apply_persisted_overrides  # noqa: E402
from scripts.search import WINDOWS, auto_jobs  # noqa: E402

ARM_SPEC: dict[str, dict] = {
    "empty": {"source": "none"},
    "current": {"source": "all"},
    "env_risk_on": {"source": "env"},
    "ma60_uptrend": {"source": "ma60"},
}
ARMS = tuple(ARM_SPEC)
_ENV_DATES: set[str] | None = None
_MA60_DATES: set[str] | None = None
_DATES_LOADED = False


def _data_root() -> str:
    s = get_settings()
    return os.path.dirname(str(s.kline_dir).rstrip("/\\")) or "."


def _env_risk_on_dates() -> set[str]:
    """T 05:30 market_env 快照：只保留 us_session_date < trade_date 的 risk_on。"""
    rows = db.market_env_rows("2022-01-01", "2026-12-31")
    out: set[str] = set()
    for r in rows:
        if r.get("env_label") != "risk_on":
            continue
        day = str(r.get("trade_date") or "")[:10]
        sess = str(r.get("us_session_date") or "")[:10]
        if not day or (sess and sess >= day):
            continue
        out.add(day)
    return out


def _ma60_uptrend_dates() -> set[str]:
    """中证全指 T-1 收盘 > MA60，映射到 T 是否允许新开仓（严格滞后一日）。"""
    kdir = Path(str(get_settings().kline_dir))
    path = kdir / "idx_sh000985.parquet"
    if not path.exists():
        path = kdir / "idx_sh000001.parquet"
    df = pd.read_parquet(path)
    df["date"] = df["date"].astype(str).str.split(" ").str[0]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    up = df["close"] > df["close"].rolling(60, min_periods=60).mean()
    signal_for_today = up.shift(1).fillna(False)
    return set(df.loc[signal_for_today, "date"].tolist())


def _allowed_sets() -> dict[str, set[str] | None]:
    global _ENV_DATES, _MA60_DATES, _DATES_LOADED
    if not _DATES_LOADED:
        try:
            _ENV_DATES = _env_risk_on_dates()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] market_env 取数失败，risk_on 臂全空仓：{exc}", flush=True)
            _ENV_DATES = set()
        try:
            _MA60_DATES = _ma60_uptrend_dates()
        except Exception as exc:  # noqa: BLE001
            print(f"[!] MA60 指数取数失败，ma60 臂全空仓：{exc}", flush=True)
            _MA60_DATES = set()
        _DATES_LOADED = True
        print(f"[dates] risk_on={len(_ENV_DATES or ())} ma60_uptrend={len(_MA60_DATES or ())}", flush=True)
    return {
        "empty": set(),
        "current": None,
        "env_risk_on": _ENV_DATES or set(),
        "ma60_uptrend": _MA60_DATES or set(),
    }


def _base_params(start: str, end: str, universe: int, delay: int, slippage: float) -> PipelineParams:
    apply_persisted_overrides()
    params = build_params(BacktestRequest(
        start_date=start, end_date=end,
        universe_size=universe,
        entry_open_delay_minutes=delay,
        scoring_mode="factors",
    ))
    params.scoring_mode = "factors"
    params.ml_ic_gate = False
    params.ml_ic_gate_segment = False
    params.ml_regime_aware = False
    params.ml_seed = 42
    params.ml_strict_lookahead = True
    params.max_positions = 10
    params.rotation_enabled = False
    params.slippage = float(slippage)
    params.fill_noise_bps = 0.0
    params.fill_noise_seed = 0
    return params


def _make_signals(original: dict[str, list[dict]], allowed: set[str] | None) -> dict[str, list[dict]]:
    if allowed is None:
        return {d: list(v) for d, v in original.items()}
    if not allowed:
        return {d: [] for d in original}
    return {d: (list(v) if d in allowed else []) for d, v in original.items()}


def _run_window(task: tuple) -> tuple[str, list[dict]]:
    wname, start, end, universe, delay, slippages = task
    rows: list[dict] = []
    allowed_sets = _allowed_sets()
    for slip in slippages:
        base = _base_params(start, end, universe, delay, slip)
        t0 = time.time()
        prepared = _prepare_cached(base)
        original = _ensure_signals(prepared, base)
        dates: list[str] = prepared["dates"]
        print(f"[{wname}/slip={slip}] 准备完成 {time.time() - t0:.1f}s，交易日 {len(dates)}", flush=True)
        for arm in ARMS:
            allowed = allowed_sets[arm]
            allowed_days = len(dates) if allowed is None else sum(d in allowed for d in dates)
            prepared["signals"] = _make_signals(original, allowed)
            t1 = time.time()
            try:
                result = _simulate(prepared, base)
            except Exception as exc:  # noqa: BLE001
                print(f"[{wname}/{arm}/slip={slip}] 失败：{exc}", flush=True)
                continue
            m = result["metrics"]
            total = float(m.get("total_return") or 0)
            bench = float(m.get("benchmark_return") or 0)
            row = {
                "window": wname, "range": f"{start}~{end}", "arm": arm,
                "slippage": float(slip),
                "allowed_days": int(allowed_days),
                "window_days": len(dates),
                "allowed_pct": round(100.0 * allowed_days / max(len(dates), 1), 1),
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
            print(
                f"[{wname}/{arm}/slip={slip}] {total:+.2f}% | 基准 {bench:+.2f}% "
                f"| 超额 {row['excess']:+.2f}% | 放行 {allowed_days}/{len(dates)} "
                f"| {row['trade_count']} 笔 | {row['elapsed']}s",
                flush=True,
            )
        prepared["signals"] = original
    return wname, rows


def _bootstrap_ci(values: list[float], n_boot: int = 5000, seed: int = 20261114) -> list[float | None]:
    if len(values) < 2:
        return [None, None]
    rnd = random.Random(seed)
    means = []
    for _ in range(n_boot):
        means.append(sum(values[rnd.randrange(len(values))] for _ in values) / len(values))
    means.sort()
    return [round(means[int(0.025 * len(means))], 2), round(means[int(0.975 * len(means))], 2)]


def _paired(rows: list[dict], left: str, right: str, slippage: float,
            windows: list[str] | None = None) -> dict:
    allow = set(windows) if windows is not None else None
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        if abs(float(r["slippage"]) - float(slippage)) > 1e-12:
            continue
        if allow is not None and r["window"] not in allow:
            continue
        by.setdefault(r["window"], {})[r["arm"]] = r
    diffs, samples = [], []
    for w, arms in sorted(by.items()):
        if left in arms and right in arms:
            d = arms[left]["excess"] - arms[right]["excess"]
            diffs.append(d)
            samples.append({"window": w, "diff": round(d, 2)})
    if not diffs:
        return {"n": 0, "ci95": [None, None]}
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
        "ci95": _bootstrap_ci(diffs),
        "samples": samples,
    }


def _slices(rows: list[dict], slippages: list[float]) -> dict:
    out: dict = {}
    for slip in slippages:
        rr = [r for r in rows if abs(float(r["slippage"]) - float(slip)) < 1e-12]
        by: dict[str, dict[str, dict]] = {}
        for r in rr:
            by.setdefault(r["window"], {})[r["arm"]] = r
        allw = sorted(by)
        diffs = {
            w: a["ma60_uptrend"]["excess"] - a["current"]["excess"]
            for w, a in by.items() if "ma60_uptrend" in a and "current" in a
        }
        best = max(diffs, key=diffs.get) if diffs else None
        groups = {
            "all": allw,
            "in_sample_A_F": [w for w in allw if w in "ABCDEF"],
            "holdout_G_J": [w for w in allw if w in "GHIJ"],
            "exclude_best": [w for w in allw if w != best],
        }
        pairs = {
            "env_risk_on_minus_current": ("env_risk_on", "current"),
            "ma60_uptrend_minus_current": ("ma60_uptrend", "current"),
            "empty_minus_current": ("empty", "current"),
        }
        out[str(slip)] = {
            g: {name: _paired(rr, a, b, slip, ws) for name, (a, b) in pairs.items()}
            for g, ws in groups.items() if ws
        }
        arms_out = {}
        for arm in ARMS:
            ar = [r for r in rr if r["arm"] == arm]
            if not ar:
                continue
            arms_out[arm] = {
                "mean_return": round(sum(r["total_return"] for r in ar) / len(ar), 2),
                "mean_excess": round(sum(r["excess"] for r in ar) / len(ar), 2),
                "mean_drawdown": round(sum(r["max_drawdown"] for r in ar) / len(ar), 2),
                "mean_trades": round(sum(r["trade_count"] for r in ar) / len(ar), 1),
                "mean_allowed_pct": round(sum(r["allowed_pct"] for r in ar) / len(ar), 1),
                "zero_trade_windows": sorted(r["window"] for r in ar if r["trade_count"] == 0),
            }
        out[str(slip)]["arms"] = arms_out
    return out


def _verdict(rows: list[dict], slippages: list[float]) -> dict:
    checks = []
    for slip in slippages:
        for name, (left, right) in (
            ("env_risk_on_minus_current", ("env_risk_on", "current")),
            ("ma60_uptrend_minus_current", ("ma60_uptrend", "current")),
        ):
            st = _paired(rows, left, right, slip)
            ci_low = st.get("ci95", [None, None])[0]
            ok = bool(
                st.get("n", 0) >= 8
                and (st.get("median") or 0) > 3.5
                and (st.get("positive_pct") or 0) > 50
                and ci_low is not None and ci_low > 0
            )
            checks.append({
                "slippage": slip, "name": name, "median": st.get("median"),
                "positive_pct": st.get("positive_pct"), "ci95": st.get("ci95"), "ok": ok,
            })
    passed = bool(checks) and all(c["ok"] for c in checks)
    return {
        "decision": "adopt" if passed else "not_adopted",
        "reason": (
            "两种趋势定义在双滑点下都需满足配对超额中位 >3.5pp、正占比 >50%、bootstrap CI 不含 0"
            if passed else
            "未同时满足双滑点 / 双趋势定义的 3.5pp 配对超额门槛；空仓臂只作风险对照，不视为 alpha"
        ),
        "checks": checks,
    }


def _print_summary(payload: dict) -> None:
    for slip in payload["config"]["slippages"]:
        s = payload["slices"].get(str(slip), {})
        for group in ("all", "holdout_G_J"):
            if group not in s:
                continue
            for name in ("env_risk_on_minus_current", "ma60_uptrend_minus_current", "empty_minus_current"):
                st = s[group].get(name, {})
                print(
                    f"  [slip={slip}/{group}] {name}: n={st.get('n')} 中位={st.get('median')}pp "
                    f"正占比={st.get('positive_pct')}% ci95={st.get('ci95')}",
                    flush=True,
                )
        for arm, st in s.get("arms", {}).items():
            print(
                f"  [slip={slip}/{arm}] 均值收益={st['mean_return']}% 均值超额={st['mean_excess']}% "
                f"均值回撤={st['mean_drawdown']}% 均值笔数={st['mean_trades']} "
                f"放行={st['mean_allowed_pct']}% 空仓窗口={st['zero_trade_windows']}",
                flush=True,
            )
    print(f"verdict={payload['verdict']}", flush=True)


def _out_path(tag: str) -> str:
    root = Path(_data_root()) / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"market_env_pair_{tag}.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="market_env 择时三臂配对回测")
    ap.add_argument("--windows", default="A,B,C,D,E,F,G,H,I,J")
    ap.add_argument("--universe", type=int, default=300)
    ap.add_argument("--delay", type=int, default=5)
    ap.add_argument("--slippages", default="0,0.003")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--analyze", default=None)
    args = ap.parse_args()

    if args.analyze:
        with open(args.analyze, encoding="utf-8") as fh:
            payload = json.load(fh)
        slippages = payload["config"]["slippages"]
        payload["slices"] = _slices(payload["rows"], slippages)
        payload["verdict"] = _verdict(payload["rows"], slippages)
        with open(args.analyze, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        _print_summary(payload)
        print(f"已补写统计：{args.analyze}", flush=True)
        return

    names = [w.strip().upper() for w in args.windows.split(",") if w.strip()]
    unknown = [w for w in names if w not in WINDOWS]
    if unknown:
        raise SystemExit(f"未知窗口 {unknown}，可用：{sorted(WINDOWS)}")
    slippages = [float(x) for x in args.slippages.split(",") if x.strip()]
    tasks = [(w, *WINDOWS[w], args.universe, args.delay, slippages) for w in names]
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
            "slippages": slippages, "arms": list(ARMS),
            "scoring_mode": "factors",
            "entry_open_delay_minutes": args.delay,
            "note": "同一 pipeline._simulate，只替换 prepared['signals']；非放行日只拦新开仓，不强制清仓；env_risk_on 用 T 05:30 快照，ma60 用 T-1 中证全指 > MA60",
        },
        "rows": rows,
        "slices": _slices(rows, slippages),
        "verdict": _verdict(rows, slippages),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = _out_path(args.tag)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _print_summary(payload)
    print(f"saved={out} elapsed={payload['elapsed_sec']}s", flush=True)


if __name__ == "__main__":
    main()
