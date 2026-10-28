"""一键验收：按「验收协议」跑候选方案，自动给三条通过线的判定。

协议见 `BACKTEST_LOG.md` 文末（2026-10-28 定稿）：

    冻结方案 → ≥30 段不重叠抽样 → 0 与 0.3% 两档摩擦 → 分布 + 配对 → 三条通过线

判定（缺一不采纳）：

    ① 配对中位差值 ≥ 0            （候选 − 基准，同一批段内相减）
    ② 差值 > 0 的段占比 ≥ 60%
    ③ 最差段不比基准差超过 5pp      （容差 --tolerance，默认 5.0）

多给 `--seeds` 时，要求**每个种子都通过**才算通过（对应记录 #28 的"换种子即关闭"）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/accept.py --profile wk
    .venv\\Scripts\\python.exe scripts/accept.py --profile rev_inv --seeds 20261029,20261030
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.random_dist import _segments, _stats, _trading_dates  # noqa: E402

#: 候选方案的摩擦档位（单边），协议要求两档都报
SLIPPAGE = (0.0, 0.003)
#: 通过线
MIN_POSITIVE_SHARE = 60.0
WORST_TOLERANCE_PP = 5.0


def _metrics(params: PipelineParams) -> dict:
    m = _run_sync(params)["metrics"]
    total = float(m.get("total_return") or 0.0)
    bench = float(m.get("benchmark_return") or 0.0)
    return {
        "excess": round(total - bench, 2),
        "max_drawdown": float(m.get("max_drawdown") or 0.0),
        "trade_count": int(m.get("trade_count") or 0),
        "win_rate": float(m.get("win_rate") or 0.0),
    }


def _run_task(task: tuple) -> dict:
    """单段 × 多方案（子进程执行；同段内 `_prepare_cached` 复用）。"""
    tag, start, end, variants, universe = task
    out: dict[str, dict] = {}
    for name, overrides in variants.items():
        params = PipelineParams(
            start_date=start, end_date=end, universe_size=universe, **overrides
        )
        try:
            out[name] = _metrics(params)
        except Exception as exc:  # noqa: BLE001 - 单段失败不影响其它段
            print(f"[{tag}] {name} 失败：{type(exc).__name__} {exc}", flush=True)
    print(
        f"[{tag}] {start}~{end} " + " | ".join(f"{n} {v['excess']:+.2f}%" for n, v in out.items()),
        flush=True,
    )
    return {"tag": tag, "start": start, "end": end, "profiles": out}


def verdict(base: list[float], cand: list[float], tolerance: float = WORST_TOLERANCE_PP) -> dict:
    """三条通过线（纯函数，便于单测）。"""
    pairs = [(c, b) for c, b in zip(cand, base) if c is not None and b is not None]
    if not pairs:
        return {"ok": False, "reason": "无配对样本"}
    diffs = [c - b for c, b in pairs]
    st = _stats(diffs)
    checks = {
        "median_diff_ge_0": {"value": st["median"], "pass": st["median"] >= 0},
        "positive_share_ge_60": {"value": st["positive_rate"], "pass": st["positive_rate"] >= MIN_POSITIVE_SHARE},
        "worst_not_much_worse": {
            "value": round(min(c for c, _ in pairs), 2),
            "base_worst": round(min(b for _, b in pairs), 2),
            "pass": min(c for c, _ in pairs) >= min(b for _, b in pairs) - tolerance,
        },
    }
    ok = all(c["pass"] for c in checks.values())
    return {"ok": ok, "n": st["n"], "checks": checks}


def _variant_overrides(profile: str, slippage: float) -> dict:
    overrides = dict(PROFILES[profile])
    overrides["slippage"] = slippage
    return overrides


def run_seed(
    seed: int, profile: str, baseline: str, draws: int, span: int, universe: int, jobs: int
) -> dict:
    dates = _trading_dates()
    segs = _segments(dates, span, draws, seed)
    variants = {
        baseline: _variant_overrides(baseline, 0.0),
        f"{profile}@0": _variant_overrides(profile, SLIPPAGE[0]),
        f"{profile}@30": _variant_overrides(profile, SLIPPAGE[1]),
    }
    tasks = [(i, s, e, variants, universe) for i, (s, e) in enumerate(segs, 1)]
    workers = max(1, min(jobs, len(tasks)))
    if workers == 1:
        results = [_run_task(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_run_task, tasks))
    results.sort(key=lambda r: r["tag"])

    out: dict = {"seed": seed, "segments": len(segs), "variants": {}}
    base_vals = [r["profiles"][baseline]["excess"] for r in results if baseline in r["profiles"]]
    for slippage, label in zip(SLIPPAGE, ("@0", "@30")):
        name = f"{profile}{label}"
        vals = [r["profiles"][name]["excess"] for r in results if name in r["profiles"]]
        n = min(len(vals), len(base_vals))
        out["variants"][label] = {
            "profile": name,
            "stats": _stats(vals),
            "baseline_stats": _stats(base_vals),
            "verdict": verdict(base_vals[:n], vals[:n]),
        }
    return out


def _print_seed_report(res: dict, profile: str, baseline: str) -> None:
    print(f"\n### 种子 {res['seed']}（{res['segments']} 段）\n")
    print(f"| 档位 | 方案中位 | 基准中位 | 配对中位差 | 差值>0 占比 | 最差段(方案/基准) | ① | ② | ③ | 结论 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, label_text in (("@0", "0 滑点"), ("@30", "0.3% 单边")):
        v = res["variants"][label]
        chk = v["verdict"].get("checks") or {}
        if not chk:
            print(f"| {label_text} | n/a | n/a | n/a | n/a | n/a | - | - | - | {v['verdict'].get('reason')} |")
            continue
        print(
            f"| {label_text} | {v['stats'].get('median', 0):+.2f}% | "
            f"{v['baseline_stats'].get('median', 0):+.2f}% | "
            f"{chk['median_diff_ge_0']['value']:+.2f}% | "
            f"{chk['positive_share_ge_60']['value']:.1f}% | "
            f"{chk['worst_not_much_worse']['value']:+.2f}% / "
            f"{chk['worst_not_much_worse']['base_worst']:+.2f}% | "
            + " | ".join("通过" if c["pass"] else "**不过**" for c in chk.values())
            + f" | {'通过' if v['verdict']['ok'] else '**不采纳**'} |"
        )
    print(f"\n（基准 = `{baseline}`，判定线：中位差 ≥0、差值>0 占比 ≥60%、最差段容差 "
          f"{WORST_TOLERANCE_PP:.0f}pp）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, help="候选方案名（profiles.py 里已注册）")
    parser.add_argument("--baseline", default="default", help="基准方案名")
    parser.add_argument("--draws", type=int, default=30, help="目标段数（不重叠，实际可能更少）")
    parser.add_argument("--span", type=int, default=21, help="每段交易日数")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN")
    parser.add_argument("--seeds", default="20261029", help="逗号分隔的种子；多个则要求全部通过")
    parser.add_argument("--jobs", type=int, default=4, help="并行段数")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    args = parser.parse_args()

    for name in (args.profile, args.baseline):
        if name not in PROFILES:
            raise SystemExit(f"未知方案 {name}，可选 {list(PROFILES)}")
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    host = socket.gethostname()

    results = [
        run_seed(seed, args.profile, args.baseline, args.draws, args.span, args.universe, args.jobs)
        for seed in seeds
    ]
    for res in results:
        _print_seed_report(res, args.profile, args.baseline)

    all_ok = all(
        all(v["verdict"]["ok"] for v in res["variants"].values()) for res in results
    )
    print(f"\n### 总判定：{'通过（可进入部署讨论）' if all_ok else '不采纳'}\n")
    print(f"- 候选 `{args.profile}` 需在两档摩擦、{len(seeds)} 个种子上同时满足三条线。")

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"accept_{args.profile}{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "host": host,
            "profile": args.profile,
            "baseline": args.baseline,
            "draws": args.draws,
            "span": args.span,
            "universe": args.universe,
            "seeds": seeds,
            "tolerance_pp": WORST_TOLERANCE_PP,
            "min_positive_share": MIN_POSITIVE_SHARE,
            "passed": all_ok,
            "runs": results,
        }, fh, ensure_ascii=False, indent=1)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
