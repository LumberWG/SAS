"""研究版：量价共振2560「裸引擎」前向轮廓（仅去防御叠加层，不改策略本身）。

目的：量化「裸引擎上限」——在 P1·中证全指基线（#60~#65）同一套实盘默认参数之上，
只剥掉两层防御叠加层：
  * 四项上下文风险闸：params.context_enabled = False
  * 弱市 30% 压仓 + bear/bull 持仓数自适应：params.regime_ma = 0
    （regime_on 随之 False → weak_exposure 分支不触发；max_pos_bear/bull 默认 None → no-op）
弱市压仓的 weak_exposure 一并置 None 以防万一。

红线：本脚本【只读】生产库——
  * 不调用 db.save_backtest（不写回测记录）
  * 把 progress.report 打桩为 no-op（不写进度表）
  * 不改任何 Settings / app_state / 前向验证基线 JSON
结果与 #60~#65、#54~#59、#48~#53 同口径（基准一致），可直接比较超额。

用法::

    .venv\\Scripts\\python.exe -u scripts\\research_naked_engine.py            # 跑全部 6 段
    .venv\\Scripts\\python.exe -u scripts\\research_naked_engine.py --only S2   # 单段先试
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
from app.backtest import pipeline as bt_pipeline  # noqa: E402
from app.backtest import progress  # noqa: E402

# ===== 零生产库写入：打桩进度上报 =====
progress.report = lambda *a, **k: None  # type: ignore
for _f in ("bind", "unbind", "mark", "snapshot", "start", "finish"):
    if hasattr(progress, _f):
        setattr(progress, _f, lambda *a, **k: None)

OUT = Path(__file__).resolve().parent / "research_naked_engine.json"

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


def naked_params(start: str, end: str):
    """从实盘默认生成参数，再剥掉两层防御叠加层。"""
    p = build_params(BacktestRequest(start_date=start, end_date=end))
    p.context_enabled = False
    p.regime_ma = 0
    p.weak_exposure = None
    return p


def run_segment(tag, start, end, note) -> dict:
    params = naked_params(start, end)
    print("[%s] %s ~ %s  %s | 裸引擎：context_enabled=False regime_ma=0 weak_exposure=None"
          % (tag, start, end, note), flush=True)
    t0 = time.time()
    result = asyncio.run(bt_pipeline.run_pipeline(params))
    elapsed = round(time.time() - t0, 1)
    bt_pipeline._PREPARED = None
    bt_pipeline._PREPARED_KEY = None

    m = result["metrics"]
    ml = result.get("ml_diagnostics", {})
    print("[%s] 完成 耗时 %.1fs | 收益 %+.2f%% 基准 %+.2f%% 回撤 %.2f%% 夏普 %.2f "
          "%d笔 胜率 %.1f%% %d交易日 | ML-IC %.4f/%s" % (
              tag, elapsed, m["total_return"], m["benchmark_return"], m["max_drawdown"],
              m["sharpe"], m["trade_count"], m["win_rate"], m["trading_days"],
              ml.get("oos_rank_ic", 0.0), "生效" if ml.get("effective") else "回退"), flush=True)
    return {
        "status": "done", "tag": tag, "window": "%s ~ %s" % (start, end), "note": note,
        "elapsed": elapsed,
        "metrics": {k: m[k] for k in (
            "total_return", "benchmark_return", "max_drawdown", "sharpe",
            "trade_count", "win_rate", "trading_days", "final_equity",
            "annualized_return", "open_positions")},
        "ml_effective": bool(ml.get("effective")),
        "ml_oos_rank_ic": ml.get("oos_rank_ic", 0.0),
        "strategy": result.get("strategy"),
    }


def print_summary(results: dict) -> None:
    done = [r for r in results.values() if r.get("status") == "done"]
    if not done:
        print("（暂无已完成段）", flush=True)
        return
    print("\n" + "=" * 92, flush=True)
    print("裸引擎前向轮廓（context_enabled=False / regime_ma=0 / 仅量价共振2560，无防御叠加）", flush=True)
    print("=" * 92, flush=True)
    print("%-4s %-24s %8s %8s %9s %8s %7s %6s %7s %6s" % (
        "段", "区间", "收益%", "基准%", "超额pp", "回撤%", "夏普", "笔数", "胜率%", "交易日"), flush=True)
    for r in sorted(done, key=lambda x: x["tag"]):
        m = r["metrics"]
        print("%-4s %-24s %+8.2f %+8.2f %+9.2f %+8.2f %7.2f %6d %7.1f %6d" % (
            r["tag"], r["window"], m["total_return"], m["benchmark_return"],
            m["total_return"] - m["benchmark_return"], m["max_drawdown"],
            m["sharpe"], m["trade_count"], m["win_rate"], m["trading_days"]), flush=True)
    exc = [r["metrics"]["total_return"] - r["metrics"]["benchmark_return"] for r in done]
    rets = [r["metrics"]["total_return"] for r in done]
    pos = sum(1 for x in rets if x > 0)
    pos_exc = sum(1 for x in exc if x > 0)
    srt = sorted(exc)
    med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    dds = [r["metrics"]["max_drawdown"] for r in done]
    print("-" * 92, flush=True)
    print("正收益段 %d/%d | 跑赢基准 %d/%d | 平均超额 %+.2fpp | 中位超额 %+.2fpp "
          "| 平均回撤 %.2f%% | 最差回撤 %.2f%%" % (
              pos, len(done), pos_exc, len(done), sum(exc) / len(exc), med,
              sum(dds) / len(dds), min(dds)), flush=True)
    print("对照（同为含 ML、A-Only）：P1·中证全指 #60~#65 平均超额 −5.43pp / 2-6 盈利；"
          "P1 之前 #48~#53 平均超额 +1.61pp / 4-6 盈利（仍含 30%% 压仓）", flush=True)
    print("=" * 92, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="研究版：裸引擎前向轮廓（只读、不写生产库）")
    ap.add_argument("--only", default="", help="只跑指定段，如 S6 或 S1,S2")
    ap.add_argument("--force", action="store_true", help="重跑已完成的段")
    args = ap.parse_args()

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
            results[tag] = run_segment(tag, start, end, note)
        except Exception as exc:
            results[tag] = {"status": "error", "window": "%s ~ %s" % (start, end), "error": str(exc)}
            print("[%s] 失败：%s" % (tag, exc), flush=True)
        save_results(results)
    print_summary(results)


if __name__ == "__main__":
    main()
