"""多段「不重叠」前向验证：检验 #47 的 +13.28% 是普遍现象还是单段特有。

设计约束（与用户「回测 ↔ 实盘严格 1:1」红线一致）：

* **绝不调参**：每段参数都由 ``app.api.backtest.build_params()`` 从实盘
  ``Settings`` / ``app_state`` 覆盖层实时生成，唯一变化的是 start_date / end_date。
  脚本逐字段断言各段与锚点（#47 同区间）除日期外完全相同，任何漂移立即中止。
* **同门槛**：沿用 #45/#47 的前置检查 ``apply_persisted_overrides()`` +
  ``audit_alignment()``（含 ST 口径 / 止损门控两类行为探针），不零漂移就拒绝跑。
* **前向**：段按时间顺序排列、互不重叠；pipeline 的 ML 打分只用「段起始日之前」
  的历史训练，不会把未来数据泄漏进本段。所有段在参数搜索停止之后才跑，
  都是样本外。
* **可复现**：每段结果落库（页面历史可读），指标另存 JSON，支持断点续跑。

用法（backend 目录）::

    .venv\\Scripts\\python.exe -u scripts\\forward_validation.py --only S6   # 先测一段
    .venv\\Scripts\\python.exe -u scripts\\forward_validation.py              # 跑全部
    .venv\\Scripts\\python.exe -u scripts\\forward_validation.py --summary    # 只看已有结果

输出文件：``scripts/forward_validation_results.json``（逐段指标 + run_id）。

基线版本（重要，禁止混用）：

* **P1 之前**：#48~#53 六段，结果另存 ``forward_validation_results_pre_p1.json``。
  当时只有 ST / 止损行为闸，没有四项上下文风险闸。
* **P1 之后（当前）**：2026-11-13 重跑，四项上下文闸已接入并默认开启，
  作为 2026-12-01 判读窗口的新基线。两轮规则不同，**不得直接比较收益数字**，
  只能分别用来判断「各自规则下超额是否稳定」。
* 舆情新闻历史客观不可得（仅 2026-11-04 起有快照），早期段两侧一致地不触发，
  这是数据边界而非规则漂移；事件日历与热度历史覆盖 2022 年至今。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须先应用 app_state 覆盖层，之后所有 get_settings() 拿到的才是实盘生效值
from app.param_specs import apply_persisted_overrides  # noqa: E402

apply_persisted_overrides()

from app.backtest.alignment import audit_alignment  # noqa: E402
from app.api.backtest import BacktestRequest, build_params  # noqa: E402
from app.backtest import pipeline as bt_pipeline  # noqa: E402
from app.backtest import progress  # noqa: E402
from app.backtest.pipeline import run_pipeline  # noqa: E402
from app.data.cache import db  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "forward_validation_results.json"

# 锚点：#47 的区间，用来证明「每段参数 = 实盘默认 = #47 口径」
ANCHOR_START, ANCHOR_END = "2025-10-09", "2026-10-20"

# 段间不重叠、按时间前向滚动；约每段 4 个月（≈75~85 交易日）。
# 数据末日 = 2026-11-12，个股 K 线自 2022-09-01 起，pipeline 取窗口前 320 根预热。
SEGMENTS: list[tuple[str, str, str, str]] = [
    ("S1", "2025-01-02", "2025-04-30", "#47 之前 · 样本外"),
    ("S2", "2025-05-06", "2025-08-29", "#47 之前 · 样本外"),
    ("S3", "2025-09-01", "2025-12-31", "与 #47 起点重叠的前段"),
    ("S4", "2026-01-05", "2026-04-30", "#47 区间中段"),
    ("S5", "2026-05-06", "2026-08-31", "#47 区间后段"),
    ("S6", "2026-09-01", "2026-11-12", "#47 终点之后 · 样本外"),
]

# #45/#47 锁定的关键口径，逐项与实盘现值断言（防止 app_state 再次被写脏）
LOCKS = {
    "strategies": ("量价共振2560",),
    "board_prefixes": ("cyb",),
    "universe_size": 6000,
    "scoring_mode": "ml",
    "max_positions": 10,
    "watch_cap": 30,
    "add_per_day": 10,
    "stop_loss": 0.08,
    "stop_atr_min": 0.08,
    "breakeven_trigger": 0.1,
    "trail_pct": 0.1,
    "regime_ma": 60,
    "regime_index": "sh000985",
    "weak_exposure": 0.3,
    # P1 四项上下文风险闸（2026-11-12 接入）：同样锁进锚点，防止 app_state 被写脏
    "context_enabled": True,
    "context_max_industry_weight": 0.40,
    "context_max_corr": 0.85,
    "context_hot_exclude_pct": 0.20,
    "context_event_cooldown_days": 3,
    "context_news_block": 0.60,
}


def _norm(v):
    return tuple(v) if isinstance(v, (list, tuple)) else v


def preflight() -> dict:
    """跑之前的对齐自检 + 参数基线锁定，返回锚点的非日期字段快照。"""
    rep = audit_alignment()
    if not rep["ok"]:
        print("对齐自检未零漂移，拒绝跑回测:",
              json.dumps(rep.get("drifts", []), ensure_ascii=False), flush=True)
        raise SystemExit(1)
    summ = rep["summary"]
    print("对齐自检 OK | 绑定 %s 项 | ST 口径 %s | 止损门控 %s | 上下文闸 %s" % (
        summ.get("bound"), summ.get("scope_parity"), summ.get("stop_parity"),
        summ.get("context_parity")), flush=True)
    # audit_alignment() 里是 "ok"（CLI 才渲染成中文「一致」）
    parity = (summ.get("scope_parity"), summ.get("stop_parity"), summ.get("context_parity"))
    if any(v not in ("ok", "一致", True) for v in parity):
        print("行为口径探针未通过，拒绝跑回测", flush=True)
        raise SystemExit(1)

    anchor = build_params(BacktestRequest(start_date=ANCHOR_START, end_date=ANCHOR_END))
    for key, want in LOCKS.items():
        got = _norm(getattr(anchor, key, None))
        if got != want:
            print("关键参数与 #45/#47 不一致，拒绝跑验证：%s = %r（期望 %r）"
                  % (key, got, want), flush=True)
            raise SystemExit(1)
    print("参数锁定 OK：%d 项与 #45/#47 逐项一致" % len(LOCKS), flush=True)

    base = asdict(anchor)
    base.pop("start_date", None)
    base.pop("end_date", None)
    return base


def load_results() -> dict:
    if RESULTS_PATH.exists():
        try:
            return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件重来
            return {}
    return {}


def save_results(data: dict) -> None:
    RESULTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def print_summary(results: dict) -> None:
    done = [r for r in results.values() if r.get("status") == "done"]
    if not done:
        print("（暂无已完成段）", flush=True)
        return
    print("\n" + "=" * 92, flush=True)
    print("多段前向验证汇总（参数与实盘零差异，仅日期变化）", flush=True)
    print("=" * 92, flush=True)
    print("%-4s %-24s %8s %8s %9s %8s %7s %6s %7s %6s %s" % (
        "段", "区间", "收益%", "基准%", "超额pp", "回撤%", "夏普", "笔数", "胜率%", "交易日", "run_id"),
        flush=True)
    for r in sorted(done, key=lambda x: x["tag"]):
        m = r["metrics"]
        print("%-4s %-24s %+8.2f %+8.2f %+9.2f %+8.2f %7.2f %6d %7.1f %6d #%s" % (
            r["tag"], r["window"], m["total_return"], m["benchmark_return"],
            m["total_return"] - m["benchmark_return"], m["max_drawdown"],
            m["sharpe"], m["trade_count"], m["win_rate"], m["trading_days"], r["run_id"]),
            flush=True)
    exc = [r["metrics"]["total_return"] - r["metrics"]["benchmark_return"] for r in done]
    rets = [r["metrics"]["total_return"] for r in done]
    pos = sum(1 for x in rets if x > 0)
    pos_exc = sum(1 for x in exc if x > 0)
    srt = sorted(exc)
    med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    print("-" * 92, flush=True)
    print("正收益段 %d/%d | 跑赢基准 %d/%d | 平均超额 %+.2fpp | 中位超额 %+.2fpp "
          "| 最差 %+.2fpp | 最好 %+.2fpp" % (
              pos, len(done), pos_exc, len(done), sum(exc) / len(exc), med, srt[0], srt[-1]),
          flush=True)
    print("对照：#47 全年 +13.28% vs -6.47% = +19.75pp（P1 之前口径，仅历史参考）", flush=True)
    print("旧基线 #48~#53（P1 之前）：平均超额 +1.61pp / 中位 +3.15pp / 极差 31.32pp",
          flush=True)
    print("⚠ 两轮规则不同（本轮含四项上下文风险闸），收益数字不可直接相减比较", flush=True)
    bad_ml = [r["tag"] for r in done if not r.get("ml_effective")]
    if bad_ml:
        print("注意：ML 打分在这些段未生效（回退 14 因子）：%s" % ", ".join(bad_ml), flush=True)
    print("=" * 92, flush=True)


def run_segment(tag: str, start: str, end: str, note: str, base: dict) -> dict:
    """跑一段：参数与锚点逐字段一致 → 执行 → 落库 → 返回精简指标。"""
    params = build_params(BacktestRequest(start_date=start, end_date=end))
    now = asdict(params)
    now.pop("start_date", None)
    now.pop("end_date", None)
    diff = {k: (base.get(k), v) for k, v in now.items() if base.get(k, "<absent>") != v}
    if diff:
        print("参数漂移，拒绝跑 %s：%s" % (tag, json.dumps(diff, ensure_ascii=False, default=str)),
              flush=True)
        raise SystemExit(1)
    print("[%s] %s ~ %s  %s | 参数 %d 项与锚点零差异" % (
        tag, start, end, note, len(now)), flush=True)

    job_id = "fw-%s-p1-20261113" % tag.lower()
    token = progress.bind(job_id, note="前向验证 %s / %s~%s" % (tag, start, end))

    async def watch():
        while True:
            await asyncio.sleep(60)
            s = progress.snapshot()
            print("[%s][progress] %-10s %5.1f%% %s/%s %s" % (
                tag, s["phase"], s["percent"], s["done"], s["total"], s["step"]), flush=True)

    async def main():
        w = asyncio.create_task(watch())
        try:
            return await run_pipeline(params)
        finally:
            w.cancel()

    t0 = time.time()
    result = asyncio.run(main())
    elapsed = round(time.time() - t0, 1)
    result["job_id"] = job_id
    result["elapsed"] = elapsed
    run_id = db.save_backtest(result)
    progress.mark(job_id, ok=True, note="%s 完成" % tag)
    progress.unbind(token)

    m = result["metrics"]
    ml = result.get("ml_diagnostics", {})
    print("[%s] 完成 run_id=#%s 耗时 %.1fs | 收益 %+.2f%% 基准 %+.2f%% "
          "回撤 %.2f%% 夏普 %.2f %d笔 胜率 %.1f%% %d交易日 | ML-IC %.4f/%s" % (
              tag, run_id, elapsed, m["total_return"], m["benchmark_return"],
              m["max_drawdown"], m["sharpe"], m["trade_count"], m["win_rate"],
              m["trading_days"], ml.get("oos_rank_ic", 0.0),
              "生效" if ml.get("effective") else "回退"), flush=True)

    # 释放 pipeline 的 prepared 缓存（含全池 K 线），避免多段累积占内存
    bt_pipeline._PREPARED = None
    bt_pipeline._PREPARED_KEY = None

    return {
        "status": "done",
        "tag": tag,
        "baseline": "p1_context_gates",
        "window": "%s ~ %s" % (start, end),
        "note": note,
        "run_id": run_id,
        "elapsed": elapsed,
        "metrics": {k: m[k] for k in (
            "total_return", "benchmark_return", "max_drawdown", "sharpe",
            "trade_count", "win_rate", "trading_days", "final_equity",
            "annualized_return", "open_positions")},
        "ml_effective": bool(ml.get("effective")),
        "ml_oos_rank_ic": ml.get("oos_rank_ic", 0.0),
        "strategy": result.get("strategy"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="多段不重叠前向验证（严格对齐实盘）")
    ap.add_argument("--only", default="", help="只跑指定段，如 S6 或 S1,S2")
    ap.add_argument("--force", action="store_true", help="重跑已完成的段")
    ap.add_argument("--summary", action="store_true", help="只打印已有结果")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    results = load_results()
    if args.summary:
        print_summary(results)
        return

    base = preflight()
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    todo = [s for s in SEGMENTS if not only or s[0] in only]
    if not todo:
        print("没有匹配的段：%s" % args.only, flush=True)
        raise SystemExit(1)

    for tag, start, end, note in todo:
        if not args.force and results.get(tag, {}).get("status") == "done":
            print("[%s] 已完成，跳过（--force 可重跑）" % tag, flush=True)
            continue
        results[tag] = {"status": "running", "window": "%s ~ %s" % (start, end)}
        save_results(results)
        try:
            results[tag] = run_segment(tag, start, end, note, base)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - 单段失败不拖垮其余段
            results[tag] = {"status": "error", "window": "%s ~ %s" % (start, end),
                            "error": str(exc)}
            print("[%s] 失败：%s" % (tag, exc), flush=True)
        save_results(results)

    print_summary(results)


if __name__ == "__main__":
    main()
