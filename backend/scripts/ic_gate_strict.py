# -*- coding: utf-8 -*-
"""IC 门控 ML 严格验收：先排前视、再验多 seed / 全新窗口，未过前置不进交易回测。

依据 `ALPHA_CONCLUSIONS.md` 的验收门槛与 `BACKTEST_LOG` #109 / #117 的 ML 判读纪律：
- 看板里的「事后 oracle」不能当证据；先要求训练标签 embargo + CV purge gap + 显式 as_of。
- 多 seed：同一批 LightGBM 随机源必须全部可复现，不能只报一个 42。
- 全新窗口：只用 2024 年未参与 S1~S6 阈值调参的连续 21 交易日窗口。
- 双端接线：pipeline / live_ml / recommender 必须共用同一套零前视、seed、行业中性、TopN 截面。
- 零漂移：`audit_alignment()` 与双端 parity 自检都通过，才允许进入完整配对回测。

产物：`D:/Backup/SAS/data/logs/ic_gate_strict_<tag>.json`。
本脚本不写生产库、不改线上参数；线上 IC 门控未接线时明确判为「不采纳」。
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.analysis.live_ml import live_ml_status  # noqa: E402
from app.analysis.recommender import Recommender  # noqa: E402
from app.backtest import ml_scorer as ml_scorer_mod  # noqa: E402
from app.backtest import pipeline as pipeline_mod  # noqa: E402
from app.backtest.alignment import audit_alignment  # noqa: E402
from app.backtest.ic_gate import ICGate  # noqa: E402
from app.backtest.ml_scorer import (  # noqa: E402
    _date_key,
    _prepare_samples,
    build_live_ml_predictions,
    build_ml_predictions,
    purged_group_cv,
)
from app.backtest.pipeline import MAIN_BOARD_PREFIXES  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.data.scope import is_st_name  # noqa: E402
from app.param_specs import apply_persisted_overrides  # noqa: E402

SEEDS = (42, 20261114, 777)
FRESH_FROM = "2024-01-02"
FRESH_TO = "2024-12-31"
WINDOW_DAYS = 21
WINDOW_COUNT = 6
CODES = 80
HORIZON = 15
GATE_THRESH = 0.12
PROTOCOL = {
    "fresh_windows": f"{FRESH_FROM}~{FRESH_TO}，非重叠 {WINDOW_DAYS} 交易日，未参与 S1~S6 阈值调参",
    "seeds": list(SEEDS),
    "codes": CODES,
    "horizon": HORIZON,
    "model": "lightgbm",
    "strict_lookahead": True,
    "require_ic": 0.0,
    "gate_thresh": GATE_THRESH,
    "acceptance": {
        "oos_rank_ic_mean_gt": 0,
        "posthoc_daily_ic_ci_low_gt": 0,
        "paired_excess_median_gt_pp": 3.5,
        "positive_window_pct_gt": 50,
        "live_gate_wired": True,
        "zero_drift": True,
    },
    "note": "posthoc rank-IC 只用于研究判读，不进入训练；未过前置不运行完整交易回测，避免把泄漏当 alpha。",
}


def _data_root() -> str:
    s = get_settings()
    return os.path.dirname(str(s.kline_dir).rstrip("/\\")) or "."


def _date_plus(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _bootstrap_ci(values: list[float], n_boot: int = 5000, seed: int = 20261114) -> list[float | None]:
    if len(values) < 2:
        return [None, None]
    rnd = random.Random(seed)
    means = []
    for _ in range(n_boot):
        means.append(sum(values[rnd.randrange(len(values))] for _ in values) / len(values))
    means.sort()
    return [round(means[int(0.025 * len(means))], 6), round(means[int(0.975 * len(means))], 6)]


def _trading_dates() -> list[str]:
    kdir = Path(str(get_settings().kline_dir))
    path = kdir / "idx_sh000985.parquet"
    if not path.exists():
        path = kdir / "idx_sh000001.parquet"
    df = pd.read_parquet(path)
    df["date"] = df["date"].astype(str).str.split(" ").str[0]
    return sorted(d for d in df["date"].tolist())


def _fresh_windows(dates: list[str], count: int) -> list[tuple[str, str]]:
    pool = [d for d in dates if FRESH_FROM <= d <= FRESH_TO]
    if len(pool) < WINDOW_DAYS * count:
        raise RuntimeError(f"全新窗口交易日不足：{len(pool)}")
    rnd = random.Random(20261114)
    offset = rnd.randrange(0, min(WINDOW_DAYS, len(pool) - WINDOW_DAYS * count + 1))
    out: list[tuple[str, str]] = []
    i = offset
    while i + WINDOW_DAYS <= len(pool) and len(out) < count:
        out.append((pool[i], pool[i + WINDOW_DAYS - 1]))
        i += WINDOW_DAYS
    return out


def _fixed_codes(limit: int) -> list[str]:
    snap_path = Path(__file__).resolve().parents[1] / "app" / "data" / "cache" / "snapshot_latest.parquet"
    snap = pd.read_parquet(snap_path)
    snap["code"] = snap["code"].astype(str).str.zfill(6)
    snap = snap[snap["code"].str.startswith(MAIN_BOARD_PREFIXES)]
    snap = snap[~snap["name"].astype(str).map(lambda x: is_st_name(x))]
    return snap.nlargest(limit, "amount")["code"].tolist()


def _load_klines(codes: list[str], max_end: str) -> dict[str, pd.DataFrame]:
    kdir = Path(str(get_settings().kline_dir))
    required = {"date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"}
    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = kdir / f"{code}.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        if not required <= set(df.columns):
            continue
        df["date"] = df["date"].astype(str).str.split(" ").str[0]
        df = df[df["date"] <= max_end].copy().reset_index(drop=True)
        if len(df) < 200:
            continue
        out[code] = df
    return out


def _close_index(klines: dict[str, pd.DataFrame]) -> tuple[dict, dict, dict]:
    dates, closes, pos = {}, {}, {}
    for code, df in klines.items():
        d = df["date"].astype(str).str.split(" ").str[0].tolist()
        c = pd.to_numeric(df["close"], errors="coerce").astype(float).to_numpy()
        dates[code] = d
        closes[code] = c
        pos[code] = {x: i for i, x in enumerate(d)}
    return dates, closes, pos


def _posthoc_ic(
    ml_pred: dict[tuple[str, str], float],
    klines: dict[str, pd.DataFrame],
    window_dates: list[str],
    horizon: int,
) -> dict:
    dates, closes, pos = _close_index(klines)
    wanted = set(window_dates)
    by_date: dict[str, list[tuple[float, float]]] = {}
    for (code, day), pred in ml_pred.items():
        if day not in wanted or code not in pos:
            continue
        i = pos[code].get(day)
        if i is None or i + horizon >= len(dates[code]):
            continue
        c0 = closes[code][i]
        c1 = closes[code][i + horizon]
        if not np.isfinite(c0) or not np.isfinite(c1) or c0 <= 0:
            continue
        by_date.setdefault(day, []).append((float(pred), float(c1 / c0 - 1.0)))
    ics: list[float] = []
    for day in sorted(by_date):
        pairs = by_date[day]
        if len(pairs) < 5:
            continue
        a = pd.Series([p[0] for p in pairs])
        b = pd.Series([p[1] for p in pairs])
        ic = a.corr(b, method="spearman")
        if ic is not None and np.isfinite(ic):
            ics.append(float(ic))
    return {
        "days": len(ics),
        "mean": round(sum(ics) / len(ics), 6) if ics else None,
        "positive_pct": round(100.0 * sum(x > 0 for x in ics) / len(ics), 1) if ics else None,
        "ci95": _bootstrap_ci(ics),
    }


def _prediction_signature(ml_pred: dict[tuple[str, str], float]) -> str:
    raw = "|".join(f"{k[0]}:{k[1]}:{v:.6f}" for k, v in sorted(ml_pred.items()))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _synthetic_klines(n_codes: int = 8, n_days: int = 180, seed: int = 7) -> dict[str, pd.DataFrame]:
    dates = pd.bdate_range("2023-01-02", periods=n_days).strftime("%Y-%m-%d").tolist()
    out: dict[str, pd.DataFrame] = {}
    for c in range(n_codes):
        rng = np.random.default_rng(seed + c)
        close = 10.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n_days)))
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.004, n_days)))
        low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.004, n_days)))
        volume = rng.integers(1_000_000, 5_000_000, n_days).astype(float)
        out[f"{600000 + c:06d}"] = pd.DataFrame({
            "date": dates,
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "amount": volume * close,
            "turnover_rate": rng.uniform(1.0, 5.0, n_days),
        })
    return out


def _parity_check() -> dict:
    klines = _synthetic_klines()
    all_dates = sorted({d for df in klines.values() for d in df["date"]})
    as_of = max(all_dates)
    kw = dict(
        horizon=5,
        alphas=(0.1, 1.0),
        n_splits=3,
        require_ic=-1.0,
        model="ridge",
        industry_neutral=False,
        seed=42,
        strict_lookahead=True,
    )
    back_pred, back_ic = build_ml_predictions(klines, all_dates, as_of, **kw)
    live_pred, live_ic = build_live_ml_predictions(klines, as_of=as_of, **kw)
    keys = [(c, as_of) for c in klines]
    diffs = [abs(back_pred.get(k, float("nan")) - live_pred.get(k, float("nan"))) for k in keys]
    ok = bool(back_pred and live_pred and all(np.isfinite(d) and d < 1e-6 for d in diffs))
    return {
        "ok": ok and abs(back_ic - live_ic) < 1e-6,
        "codes": len(keys),
        "max_abs_diff": round(max(diffs), 8) if diffs else None,
        "back_oos_ic": round(float(back_ic), 6),
        "live_oos_ic": round(float(live_ic), 6),
        "note": "ridge 确定性模型；同一 as_of、同一 embargo/purge/中性化下两端预测必须逐票一致",
    }


def _preflight() -> dict:
    alignment = audit_alignment()
    live_src = inspect.getsource(__import__("app.analysis.live_ml", fromlist=["get_live_ml_scores"]).get_live_ml_scores)
    rec_src = inspect.getsource(Recommender.analyze_watchlist)
    pipe_src = inspect.getsource(pipeline_mod._prepare)
    ml_src = "".join([
        inspect.getsource(ml_scorer_mod._prepare_samples),
        inspect.getsource(ml_scorer_mod.purged_group_cv),
        inspect.getsource(ml_scorer_mod.build_ml_predictions),
        inspect.getsource(ml_scorer_mod.build_live_ml_predictions),
    ])
    src_checks = {
        "live_explicit_as_of": "as_of=as_of" in live_src,
        "live_seed": "seed=_SEED" in live_src,
        "live_strict": "strict_lookahead=_STRICT_LOOKAHEAD" in live_src,
        "recommender_topn_snapshot": "paper_pool_top_n" in rec_src and "snapshot_store.get_snapshot" in rec_src,
        "pipeline_seed": "seed=int(params.ml_seed or 42)" in pipe_src,
        "pipeline_strict": "strict_lookahead=bool(params.ml_strict_lookahead)" in pipe_src,
        "train_label_embargo": "label_before=segment_start if strict_lookahead else None" in ml_src,
        "cv_purge_gap": "purge_gap = max(0, int(horizon)) if strict_lookahead else 0" in ml_src,
        "prediction_without_future_label": "need_label=False" in ml_src,
    }
    parity = _parity_check()
    status = live_ml_status()
    live_gate_wired = bool(status.get("ic_gate_enabled"))
    blockers: list[str] = []
    if not alignment["ok"]:
        blockers.append("alignment_drift")
    bad_src = [k for k, v in src_checks.items() if not v]
    if bad_src:
        blockers.append("source_checks_failed:" + ",".join(bad_src))
    if not parity["ok"]:
        blockers.append("dual_end_parity_failed")
    if not live_gate_wired:
        blockers.append("live_ic_gate_not_wired")
    return {
        "alignment_ok": bool(alignment["ok"]),
        "alignment_drifts": alignment.get("drifts", [])[:5],
        "source_checks": src_checks,
        "parity": parity,
        "live_status": {
            k: status.get(k)
            for k in ("industry_neutral", "strict_lookahead", "ml_seed", "ic_gate_enabled", "universe_mode")
        },
        "live_gate_wired": live_gate_wired,
        "blockers": blockers,
        "pass": not blockers,
    }


def _prediction_audit(windows: list[tuple[str, str]], klines: dict[str, pd.DataFrame],
                      seeds: tuple[int, ...]) -> dict:
    all_dates = sorted({d for df in klines.values() for d in df["date"]})
    industry_neutral = bool(getattr(get_settings(), "live_ml_industry_neutral", True))
    rows: list[dict] = []
    for wi, (start, end) in enumerate(windows, 1):
        wdates = [d for d in all_dates if start <= d <= end]
        for seed in seeds:
            t0 = time.time()
            ICGate.reset_telemetry()
            try:
                pred, oos_ic = build_ml_predictions(
                    klines, all_dates, start,
                    horizon=HORIZON,
                    n_splits=3,
                    require_ic=0.0,
                    model="lightgbm",
                    industry_neutral=industry_neutral,
                    seed=seed,
                    strict_lookahead=True,
                )
                post = _posthoc_ic(pred, klines, wdates, HORIZON)
                err = None
            except Exception as exc:  # noqa: BLE001
                pred, oos_ic, post, err = {}, 0.0, {"days": 0, "mean": None, "ci95": [None, None]}, repr(exc)
            rows.append({
                "window": f"W{wi:02d}", "range": f"{start}~{end}", "seed": seed,
                "oos_rank_ic": round(float(oos_ic), 6),
                "pred_pairs": len(pred),
                "pred_days": len({d for _c, d in pred}),
                "pred_signature": _prediction_signature(pred),
                "posthoc_daily_ic": post,
                "segment_gate_use": float(oos_ic) >= GATE_THRESH,
                "elapsed_sec": round(time.time() - t0, 2),
                "error": err,
            })
            print(
                f"[W{wi:02d}/seed {seed}] oos_ic={oos_ic:.4f} pred={len(pred)} "
                f"posthoc={post['mean']} days={post['days']} elapsed={rows[-1]['elapsed_sec']}s",
                flush=True,
            )
    daily_means = [r["posthoc_daily_ic"]["mean"] for r in rows
                   if r["posthoc_daily_ic"]["mean"] is not None]
    oos = [r["oos_rank_ic"] for r in rows if r["error"] is None]
    ci = _bootstrap_ci(daily_means)
    seed_medians = {}
    for seed in seeds:
        vals = [r["posthoc_daily_ic"]["mean"] for r in rows
                if r["seed"] == seed and r["posthoc_daily_ic"]["mean"] is not None]
        seed_medians[str(seed)] = round(float(np.median(vals)), 6) if vals else None
    rank_ic_ok = bool(daily_means and sum(daily_means) / len(daily_means) > 0 and ci[0] is not None and ci[0] > 0)
    seed_ok = all(v is not None and v > 0 for v in seed_medians.values())
    gate_rate = round(100.0 * sum(bool(r["segment_gate_use"]) for r in rows) / len(rows), 1) if rows else 0.0
    sig_by_window: dict[str, set[str]] = {}
    for r in rows:
        sig_by_window.setdefault(r["window"], set()).add(r["pred_signature"])
    return {
        "windows": [{"name": f"W{i:02d}", "start": a, "end": b} for i, (a, b) in enumerate(windows, 1)],
        "rows": rows,
        "summary": {
            "runs": len(rows),
            "errors": sum(r["error"] is not None for r in rows),
            "oos_rank_ic_mean": round(sum(oos) / len(oos), 6) if oos else None,
            "posthoc_daily_ic_mean": round(sum(daily_means) / len(daily_means), 6) if daily_means else None,
            "posthoc_daily_ic_ci95": ci,
            "segment_gate_use_pct": gate_rate,
            "seed_medians": seed_medians,
            "distinct_signatures_by_window": {k: len(v) for k, v in sig_by_window.items()},
            "rank_ic_ok": rank_ic_ok,
            "seed_ok": seed_ok,
        },
    }


def _verdict(pre: dict, audit: dict | None) -> dict:
    if not pre["pass"]:
        return {
            "decision": "not_adopted",
            "reason": "前置验收未通过；不进入完整交易回测。",
            "blockers": pre["blockers"],
            "full_backtest_eligible": False,
        }
    if audit is None or not audit["summary"]["rank_ic_ok"] or not audit["summary"]["seed_ok"]:
        return {
            "decision": "not_adopted",
            "reason": "多 seed / 全新窗口的 rank-IC 未过线；不进入完整交易回测。",
            "full_backtest_eligible": False,
        }
    return {
        "decision": "needs_full_paired_backtest",
        "reason": "前置与预测层已过线，仍需按 PROTOCOL 做完整交易配对回测与 3.5pp 门槛。",
        "full_backtest_eligible": True,
    }


def _out_path(tag: str) -> str:
    root = Path(_data_root()) / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"ic_gate_strict_{tag}.json")


def _print_summary(payload: dict) -> None:
    pre = payload["preflight"]
    print(f"preflight_pass={pre['pass']} blockers={pre['blockers']}", flush=True)
    if payload.get("prediction_audit"):
        s = payload["prediction_audit"]["summary"]
        print(
            f"runs={s['runs']} errors={s['errors']} oos_mean={s['oos_rank_ic_mean']} "
            f"posthoc_mean={s['posthoc_daily_ic_mean']} ci95={s['posthoc_daily_ic_ci95']} "
            f"gate_use={s['segment_gate_use_pct']}% rank_ic_ok={s['rank_ic_ok']} seed_ok={s['seed_ok']}",
            flush=True,
        )
    print(f"verdict={payload['verdict']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="IC 门控 ML 严格验收（前置 + 多 seed 全新窗口）")
    ap.add_argument("--tag", default="2026-11")
    ap.add_argument("--windows", type=int, default=WINDOW_COUNT)
    ap.add_argument("--codes", type=int, default=CODES)
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--force-prediction-audit", action="store_true",
                    help="前置失败时仍跑多 seed / 全新窗口预测层诊断；结论仍判不采纳")
    args = ap.parse_args()

    seeds = tuple(int(x) for x in args.seeds.split(",") if x.strip())
    window_count = int(args.windows)
    code_limit = int(args.codes)
    PROTOCOL["seeds"] = list(seeds)
    PROTOCOL["codes"] = code_limit
    PROTOCOL["fresh_windows"] = (
        f"{FRESH_FROM}~{FRESH_TO}，{window_count} 段非重叠 {WINDOW_DAYS} 交易日，"
        "未参与 S1~S6 阈值调参"
    )

    apply_persisted_overrides()
    t0 = time.time()
    dates = _trading_dates()
    windows = _fresh_windows(dates, window_count)
    pre = _preflight()
    print(f"preflight={pre['pass']} blockers={pre['blockers']}", flush=True)

    audit = None
    run_audit = not args.preflight_only and (pre["pass"] or args.force_prediction_audit)
    if run_audit:
        if not pre["pass"]:
            print("前置未通过，本次仅作强制预测层诊断；结论仍判不采纳。", flush=True)
        max_end = _date_plus(max(b for _a, b in windows), 45)
        codes = _fixed_codes(code_limit)
        klines = _load_klines(codes, max_end)
        print(f"loaded {len(klines)}/{len(codes)} codes, windows={windows}, max_end={max_end}", flush=True)
        audit = _prediction_audit(windows, klines, seeds)
    elif not args.preflight_only:
        print("前置未通过，跳过多 seed / 全新窗口预测审计（可加 --force-prediction-audit 做诊断）。", flush=True)

    payload = {
        "tag": args.tag,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "protocol": PROTOCOL,
        "forced_prediction_audit": bool(run_audit and not pre["pass"]),
        "fresh_windows": [{"start": a, "end": b} for a, b in windows],
        "preflight": pre,
        "prediction_audit": audit,
        "verdict": _verdict(pre, audit),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = _out_path(args.tag)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _print_summary(payload)
    print(f"saved={out}", flush=True)


if __name__ == "__main__":
    main()
