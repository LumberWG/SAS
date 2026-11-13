"""研究版：IC 门控 ML 叠加（第三条路 v2）前向验证（只读、零生产库写入）。

目的：在关闸·压仓·纯因子（已知最优 −1.40pp）之上，叠加「运行时滚动已实现 ML-IC」
门控——因子为底，仅当滚动 IC ≥ 阈值时切 ML 打分，否则回退 14 因子。验证 oracle
显示的 +2pp 是否能在真实滚动 IC 下复现。

红线：本脚本【只读】生产库——
  * 不调用 db.save_backtest（不写回测记录）
  * 把 progress.report 打桩为 no-op（不写进度表）
  * 不改任何 Settings / app_state / 前向验证基线 JSON

用法::

    .venv\\Scripts\\python.exe -u scripts\\research_ic_gate.py                 # 跑全部 6 段
    .venv\\Scripts\\python.exe -u scripts\\research_ic_gate.py --only S2      # 单段先试
    .venv\\Scripts\\python.exe -u scripts\\research_ic_gate.py --thresh 0.12  # 调阈值
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先加载 app_state 覆盖层，之后 build_params 拿到的即实盘生效默认值
from app.param_specs import apply_persisted_overrides  # noqa: E402
apply_persisted_overrides()

from app.api.backtest import BacktestRequest, build_params  # noqa: E402
from app.backtest import ic_gate as ic_gate_mod  # noqa: E402
from app.backtest import pipeline as bt_pipeline  # noqa: E402
from app.backtest import progress  # noqa: E402

# ===== 零生产库写入：打桩进度上报 =====
progress.report = lambda *a, **k: None  # type: ignore
for _f in ("bind", "unbind", "mark", "snapshot", "start", "finish"):
    if hasattr(progress, _f):
        setattr(progress, _f, lambda *a, **k: None)

OUT = Path(__file__).resolve().parent / "research_ic_gate.json"


def out_path(mode: str) -> Path:
    if mode == "segment":
        return Path(__file__).resolve().parent / "research_ic_gate_segment.json"
    return Path(__file__).resolve().parent / "research_ic_gate.json"

SEGMENTS = [
    ("S1", "2025-01-02", "2025-04-30", "#47 之前 · 样本外"),
    ("S2", "2025-05-06", "2025-08-29", "#47 之前 · 样本外"),
    ("S3", "2025-09-01", "2025-12-31", "与 #47 起点重叠的前段"),
    ("S4", "2026-01-05", "2026-04-30", "#47 区间中段"),
    ("S5", "2026-05-06", "2026-08-31", "#47 区间后段"),
    ("S6", "2026-09-01", "2026-11-12", "#47 终点之后 · 样本外"),
]


def load_results() -> dict:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_results(data: dict) -> None:
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def gate_params(start: str, end: str, thresh: float, window: int, min_samples: int, mode: str):
    """从实盘默认生成参数，叠加 IC 门控 ML 叠加（因子为底）。

    mode="rolling"：每日滚动已实现 IC 门控（默认）；
    mode="segment"：段前训练 OOS-IC 一次性决定整段（与 oracle 同口径，更干净）。
    """
    p = build_params(BacktestRequest(start_date=start, end_date=end))
    p.context_enabled = False          # 关四项闸（同 ctx_factors 口径）
    p.scoring_mode = "factors"         # 默认走纯因子；门控命中时才切 ML
    p.ml_ic_gate_thresh = thresh
    if mode == "segment":
        p.ml_ic_gate_segment = True    # 段级 OOS-IC 门控
    else:
        p.ml_ic_gate = True            # 滚动 IC 门控
        p.ml_ic_gate_window = window
        p.ml_ic_gate_min_samples = min_samples
    return p


def run_segment(tag, start, end, note, thresh, window, min_samples, mode) -> dict:
    ic_gate_mod.ICGate.reset_telemetry()
    params = gate_params(start, end, thresh, window, min_samples, mode)
    print("[%s] %s ~ %s  %s | 关闸·压仓·IC门控ML[%s]：context_enabled=False + ml_ic_gate(thresh=%.2f)"
          % (tag, start, end, note, mode, thresh), flush=True)
    t0 = time.time()
    result = asyncio.run(bt_pipeline.run_pipeline(params))
    elapsed = round(time.time() - t0, 1)
    bt_pipeline._PREPARED = None
    bt_pipeline._PREPARED_KEY = None

    m = result["metrics"]
    ml = result.get("ml_diagnostics", {})
    oos = float(ml.get("oos_rank_ic", 0.0))
    if mode == "segment":
        use_pct = 100.0 if oos >= thresh else 0.0
        avg_ic = oos
    else:
        tel = dict(ic_gate_mod.ICGate.TELEMETRY)
        use_pct = (tel["use_days"] / tel["total_days"] * 100.0) if tel["total_days"] else 0.0
        avg_ic = (tel["ic_sum"] / tel["ic_n"]) if tel["ic_n"] else 0.0
    print("[%s] 完成 耗时 %.1fs | 收益 %+.2f%% 基准 %+.2f%% 回撤 %.2f%% 夏普 %.2f "
          "%d笔 胜率 %.1f%% %d交易日 | ML-IC(oos) %.4f | 门控:用ML %.1f%%天 滚动IC均 %.3f"
          % (tag, elapsed, m["total_return"], m["benchmark_return"], m["max_drawdown"],
              m["sharpe"], m["trade_count"], m["win_rate"], m["trading_days"],
              oos, use_pct, avg_ic), flush=True)
    return {
        "status": "done", "tag": tag, "window": "%s ~ %s" % (start, end), "note": note,
        "mode": mode,
        "elapsed": elapsed,
        "metrics": {k: m[k] for k in (
            "total_return", "benchmark_return", "max_drawdown", "sharpe",
            "trade_count", "win_rate", "trading_days", "final_equity",
            "annualized_return", "open_positions")},
        "ml_effective": bool(ml.get("effective")),
        "ml_oos_rank_ic": oos,
        "gate_use_pct": round(use_pct, 1),
        "gate_avg_ic": round(avg_ic, 3),
        "strategy": result.get("strategy"),
    }


def print_summary(results: dict) -> None:
    done = [r for r in results.values() if r.get("status") == "done"]
    if not done:
        print("（暂无已完成段）", flush=True)
        return
    mode = done[0].get("mode", "rolling")
    md = {"rolling": "每日滚动已实现IC", "segment": "段前OOS-IC"}[mode]
    print("\n" + "=" * 100, flush=True)
    print("关闸·压仓·IC门控ML[%s] 基线（context_enabled=False / 仅量价共振2560）" % md, flush=True)
    print("=" * 100, flush=True)
    print("%-4s %-24s %8s %8s %9s %8s %7s %6s %7s %6s %7s %7s" % (
        "段", "区间", "收益%", "基准%", "超额pp", "回撤%", "夏普", "笔数", "胜率%", "交易日", "用ML%", "IC均"), flush=True)
    for r in sorted(done, key=lambda x: x["tag"]):
        m = r["metrics"]
        print("%-4s %-24s %+8.2f %+8.2f %+9.2f %+8.2f %7.2f %6d %7.1f %6d %7.1f %7.3f" % (
            r["tag"], r["window"], m["total_return"], m["benchmark_return"],
            m["total_return"] - m["benchmark_return"], m["max_drawdown"],
            m["sharpe"], m["trade_count"], m["win_rate"], m["trading_days"],
            r.get("gate_use_pct", 0.0), r.get("gate_avg_ic", 0.0)), flush=True)
    exc = [r["metrics"]["total_return"] - r["metrics"]["benchmark_return"] for r in done]
    rets = [r["metrics"]["total_return"] for r in done]
    pos = sum(1 for x in rets if x > 0)
    pos_exc = sum(1 for x in exc if x > 0)
    srt = sorted(exc)
    med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    dds = [r["metrics"]["max_drawdown"] for r in done]
    avg_use = sum(r.get("gate_use_pct", 0.0) for r in done) / len(done)
    print("-" * 100, flush=True)
    print("正收益段 %d/%d | 跑赢基准 %d/%d | 平均超额 %+.2fpp | 中位超额 %+.2fpp "
          "| 平均回撤 %.2f%% | 最差回撤 %.2f%% | 平均用ML占比 %.1f%%" % (
              pos, len(done), pos_exc, len(done), sum(exc) / len(exc), med,
              sum(dds) / len(dds), min(dds), avg_use), flush=True)
    print("对照：关闸·压仓·纯因子 ctx_factors = −1.40pp（当前实盘）；关闸·压仓·ML ctx_off = −3.24pp；"
          "regime-aware = −1.82pp。IC门控 oracle(IC>0.15) ≈ +2.0pp。", flush=True)
    print("=" * 100, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="研究版：IC 门控 ML 叠加（只读、不写生产库）")
    ap.add_argument("--only", default="", help="只跑指定段，如 S6 或 S1,S2")
    ap.add_argument("--force", action="store_true", help="重跑已完成的段")
    ap.add_argument("--mode", default="rolling", choices=["rolling", "segment"],
                    help="rolling=每日滚动已实现 IC 门控；segment=段前 OOS-IC 一次决定整段")
    ap.add_argument("--thresh", type=float, default=0.12, help="IC 启用阈值（rank-IC），默认 0.12")
    ap.add_argument("--window", type=int, default=400, help="滚动缓冲已实现样本对上限，默认 400")
    ap.add_argument("--min", type=int, default=20, help="冷启动最小样本数，默认 20")
    args = ap.parse_args()

    global OUT
    OUT = out_path(args.mode)
    results = load_results()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    todo = [s for s in SEGMENTS if not only or s[0] in only]
    for tag, start, end, note in todo:
        if not args.force and results.get(tag, {}).get("status") == "done":
            print("[%s] 已完成，跳过（--force 可重跑）" % tag, flush=True)
            continue
        results[tag] = {"status": "running", "window": "%s ~ %s" % (start, end)}
        save_results(results)
        try:
            results[tag] = run_segment(tag, start, end, note, args.thresh, args.window, args.min, args.mode)
        except Exception as exc:
            results[tag] = {"status": "error", "window": "%s ~ %s" % (start, end), "error": str(exc)}
            print("[%s] 失败：%s" % (tag, exc), flush=True)
        save_results(results)
    print_summary(results)


if __name__ == "__main__":
    main()
