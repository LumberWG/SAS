# -*- coding: utf-8 -*-
"""事前预估的**特征敏感性网格**（BACKTEST_LOG #122），只观测不交易。

为什么需要网格：#121 的 5 个特征只做出 4.07% 的 Brier 技巧，命中率 30.1%
远低于 66.7% 门槛。但单组参数失败不等于方向失败，必须先回答
「换特征 / 换尺度能不能把信息量提上来」，再决定要不要继续投研究时间。

两条铁律：
1. **时间切分**：开发期（前 dev_frac）用来选参，样本外（后段）只对选出的
   top-K 与默认配置评估**一次**，绝不在样本外回头调参。样本入库仍按
   ``available_bar <= bar`` 推进，开发期看不到样本外样本。
2. **排序指标用 brier_skill**（概率信息量），不用 hit_rate / 实得净收益：
   后者只在发信号的子集上算、方差大，而且能被 entry_lift 直接刷高。

产物 ``data/logs/estimate_grid_<tag>.json``，只读接口 ``GET /api/ops/estimate-grid``。
网格只是研究工具：不进评分、不接撮合、不读写 ``paper_*`` 表、不注册调度。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts\\estimate_grid.py --start 2025-01-01 --tag live
    .venv\\Scripts\\python.exe scripts\\estimate_grid.py --tag live --report
    .venv\\Scripts\\python.exe scripts\\estimate_grid.py --max-symbols 20 --tag smoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import replace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from app.analysis.estimate import (  # noqa: E402
    BASE_FEATURES,
    EstimateConfig,
    EstimateDataset,
    build_dataset,
    evaluate_dataset,
    parse_features,
)
from app.config import get_settings  # noqa: E402
from swing_shadow import prepare_scans  # noqa: E402

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 阶段 1：#121 基线、三个基准/领先型新特征，以及对 5 个基线特征做 leave-one-out。
# ``z`` 必选的原因见 estimate.parse_features（它同时承担滚动窗口掩码）。
FEATURE_SETS: dict[str, str] = {
    "base": BASE_FEATURES,
    "full": "z,streak,since,vol,regime,bench_mom,lead,bench_z",
    "lead_only": "z,lead,bench_mom",
    "lead_focus": "z,streak,lead,bench_mom,bench_z",
    "bench_context": "z,streak,since,vol,regime,bench_mom,bench_z",
    "z_only": "z",
    "no_regime": "z,streak,since,vol",
    "no_since": "z,streak,vol,regime",
    "no_vol": "z,streak,since,regime",
    "no_streak": "z,since,vol,regime",
}

# 阶段 2：只动不影响 dataset 内容的参数 + horizon（horizon 会重建 dataset）。
# min_global_samples / symbol_cap / cost_pct / min_exp_net 固定，避免组合爆炸；
# 「样本外只评估一次」比「网格足够大」更重要。
HORIZONS = (2, 3, 5)
PRIOR_STRENGTHS = (20.0, 40.0, 80.0)
ENTRY_LIFTS = (1.5, 2.0, 3.0)
TOP_K = 3

_NOTE = (
    "纯研究产物：开发期选参、样本外只验证一次；排序用 brier_skill 而非命中率；"
    "概率校准为等频十分箱，predicted<actual 表示低估；"
    "网格不接评分/下单，样本外未达标即视为当前特征与尺度下无可交易边际。"
)

_SLIM_KEYS = (
    "estimated_queries", "predicted_signals", "hit_rate", "base_rate",
    "brier_score", "brier_baseline", "brier_skill",
    "calibration_ece", "calibration_bias", "calibration_slope",
    "mean_realized_net_pct", "median_confirmed_pct", "breakeven_hit_pct",
    "p_turn_p99", "verdict",
)


def _path(tag: str):
    log_dir = get_settings().kline_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"estimate_grid_{tag}.json"


def _save(path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def _slim(row: dict) -> dict:
    return {k: row.get(k) for k in _SLIM_KEYS}


def _rank(row: dict) -> tuple[float, float, float]:
    """brier_skill 越大越好、ECE 越小越好、有效重放次数越多越好。

    第三键是覆盖率：8 维全特征集会把分箱切得过稀疏，``min_global_samples``
    滤掉近一半 bar，此时的 skill 不是同一覆盖率下的比较，不该排在前面。
    """
    skill = row.get("brier_skill")
    ece = row.get("calibration_ece")
    return (
        -(skill if skill is not None else -9.0),
        ece if ece is not None else 9.0,
        -(row.get("estimated_queries") or 0),
    )


def _dedupe_configs(rows: list[dict], cfg0: EstimateConfig, k: int) -> list[dict]:
    """按 (特征集, horizon, 先验强度) 去重，代表行取门槛最接近默认值的那组。

    ``entry_lift`` 不影响 brier_skill（它只决定发不发信号），若不去重，
    开发期 top-K 会被同一配置的三个门槛占满，样本外验证名额全被浪费；
    也不能挑命中率最高的门槛当代表 —— 那等于用开发期的噪声偷偷调参。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (
            r["features"],
            r.get("horizon", cfg0.horizon),
            r.get("prior_strength", cfg0.prior_strength),
        )
        groups.setdefault(key, []).append(r)
    reps = [
        min(group, key=lambda r: abs(r.get("entry_lift", cfg0.entry_lift) - cfg0.entry_lift))
        for group in groups.values()
    ]
    reps.sort(key=_rank)
    return reps[:k]


def _cfg(base: EstimateConfig, *, features: str, horizon: int = 0,
         prior: float = 0.0, lift: float = 0.0) -> EstimateConfig:
    return replace(
        base,
        features=features,
        horizon=horizon or base.horizon,
        prior_strength=prior or base.prior_strength,
        entry_lift=lift or base.entry_lift,
    )


def run_grid(
    *,
    start: str = "",
    tag: str = "live",
    max_symbols: int | None = None,
    lookback: int = 1200,
    dev_frac: float = 0.6,
    stage1_only: bool = False,
    log=None,
) -> dict:
    emit = log or (lambda msg: None)
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"非法 tag: {tag}")
    if not 0.3 <= dev_frac <= 0.8:
        raise ValueError("--dev-frac 需在 [0.3, 0.8]")

    emit("读取真实 K 线并构造扫描输入…")
    prep = prepare_scans(
        start=start, codes="", max_symbols=max_symbols,
        lookback=lookback, log=emit,
    )
    scans = prep["scans"]
    bench_close = prep["bench_close"]
    dates = prep["dates"]
    cfg0: EstimateConfig = prep["est_cfg"]
    n, start_bar = prep["n"], prep["start_bar"]
    split = start_bar + int((n - start_bar) * dev_frac)
    if split <= start_bar + 20 or n - split < 20:
        raise ValueError(f"时间切分后区间过短：{[start_bar, split, n]}")
    emit(
        f"时间切分：开发期 bar [{start_bar}, {split})（{dates[start_bar]}~{dates[split - 1]}）"
        f" / 样本外 [{split}, {n})（{dates[split]}~{dates[-1]}）"
    )

    cache: dict[tuple[str, int], EstimateDataset] = {}

    def dataset_for(features: str, horizon: int) -> EstimateDataset:
        key = (features, horizon)
        if key not in cache:
            cache[key] = build_dataset(scans, bench_close, _cfg(cfg0, features=features, horizon=horizon))
        return cache[key]

    # ---- 阶段 1：特征集筛选（含 leave-one-out 与领先型特征） ----
    stage1: list[dict] = []
    for name, spec in FEATURE_SETS.items():
        parse_features(spec)
        cfg = _cfg(cfg0, features=spec)
        ds = dataset_for(spec, cfg.horizon)
        row = evaluate_dataset(ds, cfg, bar_max=split)
        stage1.append({
            "name": name, "features": spec,
            "n_features": len(parse_features(spec)), **_slim(row),
        })
        emit(f"  特征 {name:14} · skill {row.get('brier_skill')} · ECE {row.get('calibration_ece')}")
    stage1.sort(key=_rank)
    best_features = [stage1[0]["features"]]
    for item in stage1:
        if len(best_features) >= 2:
            break
        if item["features"] not in best_features:
            best_features.append(item["features"])
    if BASE_FEATURES not in best_features:
        best_features.append(BASE_FEATURES)

    # ---- 阶段 2：参数网格（只在开发期） ----
    stage2: list[dict] = []
    if not stage1_only:
        for spec in best_features:
            for horizon in HORIZONS:
                ds = dataset_for(spec, horizon)
                for prior in PRIOR_STRENGTHS:
                    for lift in ENTRY_LIFTS:
                        cfg = _cfg(
                            cfg0, features=spec, horizon=horizon,
                            prior=prior, lift=lift,
                        )
                        row = evaluate_dataset(ds, cfg, bar_max=split)
                        stage2.append({
                            "features": spec, "horizon": horizon,
                            "prior_strength": prior, "entry_lift": lift,
                            **_slim(row),
                        })
            emit(f"  参数网格完成：特征 {spec[:40]}… 累计 {len(stage2)} 组")
        stage2.sort(key=_rank)
    top_dev = _dedupe_configs(stage2 if stage2 else stage1, cfg0, TOP_K)
    emit(f"开发期最优 skill = {top_dev[0].get('brier_skill')}（{len(stage2) or len(stage1)} 组）")

    # 门槛敏感性只看开发期，且不参与选参：它存在的意义是证明「抬高 entry_lift
    # 能把命中率刷上去」，所以绝不能用命中率当排序指标。
    threshold_sensitivity: list[dict] = []
    if top_dev:
        t = top_dev[0]
        spec = t["features"]
        horizon = t.get("horizon", cfg0.horizon)
        prior = t.get("prior_strength", cfg0.prior_strength)
        ds = dataset_for(spec, horizon)
        for lift in ENTRY_LIFTS:
            row = evaluate_dataset(
                ds, _cfg(cfg0, features=spec, horizon=horizon, prior=prior, lift=lift),
                bar_max=split,
            )
            threshold_sensitivity.append({
                "entry_lift": lift,
                "predicted_signals": row.get("predicted_signals"),
                "hit_rate": row.get("hit_rate"),
                "mean_realized_net_pct": row.get("mean_realized_net_pct"),
                "verdict": row.get("verdict"),
            })

    # ---- 样本外：只对 top-K + 默认配置各评估一次，不回头调参 ----
    oos_rows: list[dict] = []
    seen: set[tuple] = set()

    def oos_one(label: str, spec: str, cfg: EstimateConfig) -> None:
        key = (spec, cfg.horizon, cfg.prior_strength, cfg.entry_lift)
        if key in seen:
            return
        seen.add(key)
        ds = dataset_for(spec, cfg.horizon)
        dev = evaluate_dataset(ds, cfg, bar_max=split)
        oos = evaluate_dataset(ds, cfg, bar_min=split)
        oos_rows.append({
            "label": label, "features": spec,
            "horizon": cfg.horizon, "prior_strength": cfg.prior_strength,
            "entry_lift": cfg.entry_lift,
            "dev_brier_skill": dev.get("brier_skill"),
            "dev_brier_score": dev.get("brier_score"),
            "dev_brier_baseline": dev.get("brier_baseline"),
            "oos": _slim(oos),
            "oos_calibration_bins": oos.get("calibration_bins") or [],
            "dev_to_oos_skill_delta": (
                round(oos["brier_skill"] - dev["brier_skill"], 4)
                if oos.get("brier_skill") is not None and dev.get("brier_skill") is not None
                else None
            ),
        })

    for i, item in enumerate(top_dev, 1):
        oos_one(
            f"top{i}", item["features"],
            _cfg(
                cfg0, features=item["features"],
                horizon=item.get("horizon", cfg0.horizon),
                prior=item.get("prior_strength", cfg0.prior_strength),
                lift=item.get("entry_lift", cfg0.entry_lift),
            ),
        )
    oos_one("default", BASE_FEATURES, _cfg(cfg0, features=BASE_FEATURES))

    default = next((r for r in oos_rows if r["label"] == "default"), None)
    best = oos_rows[0] if oos_rows else None
    def_skill = (default or {}).get("oos", {}).get("brier_skill")
    best_skill = (best or {}).get("oos", {}).get("brier_skill")
    best_verdict = (best or {}).get("oos", {}).get("verdict")
    if best is None or best_skill is None or best_skill <= 0:
        conclusion = "no_information_gain_out_of_sample"
        detail = "样本外 Brier 技巧 ≤ 0，概率不含相对基础率的增量信息。"
    elif best_verdict != "above_breakeven":
        conclusion = "no_tradeable_edge"
        detail = (
            "样本外仍有概率信息量，但命中率未过盈亏平衡门槛 / 样本不足，"
            "实得期望净收益不足以覆盖成本与等确认口径。"
        )
    elif def_skill is not None and best_skill <= def_skill:
        conclusion = "no_incremental_value_over_default"
        detail = "样本外最优组合未超过默认配置，网格带来的提升属于开发期过拟合。"
    else:
        conclusion = "needs_walk_forward_validation"
        detail = "样本外出现正向信号，但只验证过一次，需滚动 walk-forward 与多重检验校正后再判断。"
    detail += (
        " 注意：brier_skill 是相对各段自身常数基准的比值，跨时间段不可比 —— "
        "样本外 skill 变高不等于没有过拟合，必须同时看绝对 Brier、命中率与实得净收益。"
    )

    data = {
        "tag": tag,
        "start": start or dates[0],
        "as_of": dates[-1],
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "split": {
            "start_bar": start_bar, "split_bar": split, "n": n,
            "dev_frac": dev_frac,
            "dev": [dates[start_bar], dates[split - 1]],
            "oos": [dates[split], dates[-1]],
        },
        "symbols_scanned": prep["symbols_scanned"],
        "ranking_metric": "brier_skill",
        "stage1_feature_sets": stage1,
        "stage2_param_grid": stage2,
        "grid_size": len(stage2) or len(stage1),
        "top_dev": top_dev,
        "threshold_sensitivity": threshold_sensitivity,
        "oos_rows": oos_rows,
        "conclusion": conclusion,
        "conclusion_detail": detail,
        "note": _NOTE,
    }
    return data


def report(data: dict) -> None:
    sp = data.get("split") or {}
    print(f"预估敏感性网格 {data.get('tag')} · {data.get('start')} ~ {data.get('as_of')}（{data.get('run_at')}）")
    print(
        f"  切分：开发 {sp.get('dev')} / 样本外 {sp.get('oos')} · "
        f"{data.get('symbols_scanned')} 只票 · 网格 {data.get('grid_size')} 组 · 排序 {data.get('ranking_metric')}"
    )
    print("  阶段1 特征集（开发期 skill / ECE / 命中率）：")
    for r in (data.get("stage1_feature_sets") or [])[:6]:
        print(
            f"    {r['name']:14} dim={r['n_features']} skill={r.get('brier_skill')} "
            f"ECE={r.get('calibration_ece')} hit={r.get('hit_rate')}"
        )
    print("  阶段2 最优 3 组（开发期）：")
    for i, r in enumerate(data.get("top_dev") or [], 1):
        print(
            f"    #{i} skill={r.get('brier_skill')} h={r.get('horizon', '-')} "
            f"prior={r.get('prior_strength', '-')} lift={r.get('entry_lift', '-')} "
            f"features={str(r.get('features'))[:52]}"
        )
    if data.get("threshold_sensitivity"):
        print("  门槛敏感性（仅开发期，不参与选参）：")
        for r in data["threshold_sensitivity"]:
            print(
                f"    lift={r['entry_lift']} · 信号 {r['predicted_signals']} 条 · "
                f"命中 {r['hit_rate']} · 实得净 {r['mean_realized_net_pct']}%"
            )
    print("  样本外（只评估一次，不回头调参）：")
    for r in data.get("oos_rows") or []:
        o = r["oos"]
        print(
            f"    {r['label']:8} dev_skill={r.get('dev_brier_skill')} → oos_skill={o.get('brier_skill')} "
            f"(Δ {r.get('dev_to_oos_skill_delta')}) · 绝对 Brier {r.get('dev_brier_score')}→{o.get('brier_score')} "
            f"· hit={o.get('hit_rate')} 门槛={o.get('breakeven_hit_pct')}% "
            f"· 实得净={o.get('mean_realized_net_pct')}% · {o.get('verdict')}"
        )
    print(f"  结论：{data.get('conclusion')} — {data.get('conclusion_detail')}")
    print(f"  {data.get('note')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="事前预估特征敏感性网格：开发期选参 / 样本外验证一次")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--tag", default="live", help="产物标签")
    ap.add_argument("--max-symbols", type=int, default=0, help="覆盖 Settings.swing_max_symbols（试跑用）")
    ap.add_argument("--lookback", type=int, default=1200, help="基准窗口保留最近多少根日线")
    ap.add_argument("--dev-frac", type=float, default=0.6, help="开发期占比 [0.3,0.8]")
    ap.add_argument("--stage1-only", action="store_true", help="只跑特征集筛选，跳过参数网格")
    ap.add_argument("--report", action="store_true", help="只打印已有产物，不重跑")
    args = ap.parse_args()

    path = _path(args.tag)
    if args.report:
        if not path.is_file():
            print(f"尚无产物：{path}")
            return
        report(json.loads(path.read_text(encoding="utf-8")))
        return
    data = run_grid(
        start=args.start, tag=args.tag,
        max_symbols=args.max_symbols or None, lookback=args.lookback,
        dev_frac=args.dev_frac, stage1_only=args.stage1_only, log=print,
    )
    _save(path, data)
    print(f"已写入 {path}")
    report(data)


if __name__ == "__main__":
    main()
