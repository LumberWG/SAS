# -*- coding: utf-8 -*-
"""出场规则实验：固定持有 vs ATR 跟踪 vs 前高移动止盈（BACKTEST_LOG #124），只研究不交易。

要回答的问题只有一个：#123 的归因说「确认后剩余波段 3.5%~6.0%，扣误报与成本需 60%+
命中率」，那**换出场规则能不能把这段波段变厚**？换特征、换校准都不行（#122/#123 已证），
因为那两者改的是「概率准不准」，不是「这段行情本身就这么多」。

三条与前面几轮的口径差异，是本轮结论能不能信的关键：

1. **入场改成可交易口径**：信号在 ``q.bar`` 收盘产生，``q.bar + 1`` 收盘买入。
   #120~#123 用「低点确认后买入」，那个 ``entry_bar`` 是事后才知道的极值确认点，
   相当于提前知道了哪根是低点 —— 用来衡量「波段够不够厚」可以，用来算期望值会高估。
2. **误报必须计入**：所有发信号的查询都模拟出场，不管 ``y_truth`` 是 0 还是 1。
   只统计命中样本的期望值等于把亏损单扔掉，是自欺。
3. **绝对与超额并列**：绝对赚钱但超额亏钱 = 赚的是 beta，不是这套方法的价值
   （``baseline.py`` 开篇就把这条写死了，本轮才真正执行）。

产物 ``data/logs/exit_lab_<tag>.json``，只读接口 ``GET /api/ops/exit-lab``。
不接评分、不接下单、不读写 ``paper_*``、不注册调度。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts\\exit_lab.py --start 2025-01-01 --tag live
    .venv\\Scripts\\python.exe scripts\\exit_lab.py --tag live --report
    .venv\\Scripts\\python.exe scripts\\exit_lab.py --max-symbols 30 --tag smoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from app.analysis.estimate import (  # noqa: E402
    TurnEstimator,
    build_dataset,
    estimate_config_from_settings,
)
from app.analysis.exit import EXIT_PRESETS, ExitResult, ExitSpec, simulate_exit  # noqa: E402
from app.config import get_settings  # noqa: E402
from swing_shadow import prepare_scans  # noqa: E402

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: 入场口径。``signal_next`` 是唯一可交易的那个：信号日收盘后决策、次日收盘买入。
#: ``confirmed_low`` 保留 #120~#123 的口径做对照，用来量化「事后知道低点」高估了多少。
ENTRY_BASES: tuple[str, ...] = ("signal_next", "confirmed_low")

_NOTE = (
    "纯研究产物：入场用次日收盘（可交易口径），误报计入，绝对与超额收益并列；"
    "开发段选出场规则、样本外只验证一次，再按时间等频分 5 折看符号一致性；"
    "不接评分/下单，超额不行为正即视为只有 beta、无可交易 alpha。"
)


def _path(tag: str):
    log_dir = get_settings().kline_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"exit_lab_{tag}.json"


def _save(path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def _entry_bar(q, basis: str, scan, last_bar: int) -> int | None:
    """入场位置；``None`` 表示不可交易（越界，或落在个股退市/停牌后的填充段）。"""
    if basis == "signal_next":
        bar = q.bar + 1
    else:
        bar = q.entry_bar
    if bar is None or bar < 0 or bar > last_bar:
        return None
    if scan.last_valid_bar is not None and bar > scan.last_valid_bar:
        return None
    return int(bar)


def _stats(results: list[ExitResult], cost: float) -> dict:
    """一笔一组出场结果的分布；净收益已扣成本，超额收益不重复扣（成本只在净里算一次）。"""
    n = len(results)
    if not n:
        return {
            "n": 0, "mean_pct": None, "median_pct": None, "mean_net_pct": None,
            "median_net_pct": None, "win_rate": None, "mean_excess_pct": None,
            "median_excess_pct": None, "profit_factor": None,
            "median_holding": None, "median_mfe_pct": None, "median_mae_pct": None,
            "tail_p10_pct": None, "tail_p90_pct": None,
        }
    gross = [r.exit_pct for r in results]
    net = [r.exit_pct - cost for r in results]
    excess = [r.excess_pct for r in results]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    ordered = sorted(net)

    def _q(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 3)

    return {
        "n": n,
        "mean_pct": round(statistics.mean(gross), 3),
        "median_pct": round(statistics.median(gross), 3),
        "mean_net_pct": round(statistics.mean(net), 3),
        "median_net_pct": round(statistics.median(net), 3),
        "win_rate": round(len(wins) / n, 4),
        "mean_excess_pct": round(statistics.mean(excess), 3),
        "median_excess_pct": round(statistics.median(excess), 3),
        # 盈亏比：总盈利 / 总亏损，比胜率更能反映"厚不厚"
        "profit_factor": (
            round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else None
        ),
        "median_holding": round(statistics.median(r.holding_bars for r in results), 1),
        "median_mfe_pct": round(statistics.median(r.mfe_pct for r in results), 3),
        "median_mae_pct": round(statistics.median(r.mae_pct for r in results), 3),
        "tail_p10_pct": _q(0.10),
        "tail_p90_pct": _q(0.90),
    }


def _verdict(row: dict, cost: float) -> str:
    if not row.get("n") or row["n"] < 30:
        return "insufficient_samples"
    net = row.get("mean_net_pct")
    excess = row.get("mean_excess_pct")
    if net is None or excess is None:
        return "insufficient_samples"
    # 超额得先扣掉自己的成本：绝对净>0 但超额净<=0，说明赚的全是大盘给的
    if net > 0 and excess - cost > 0:
        return "positive_expectancy"
    if net > 0 >= excess - cost:
        return "beta_only"
    return "negative_expectancy"


def _simulate_all(queries, scans_by_code, specs, bench, basis: str, last_bar: int):
    """对每条查询 × 每个出场规则模拟一次；返回 ``({spec.name: [ExitResult]}, 跳过数)``。

    同一条查询在 ``signal_next`` 与 ``confirmed_low`` 下入场点不同，必须分别跑，
    否则会把「事后知道低点」的收益混进可交易口径。跳过数是退市/停牌后的填充段
    与越界样本，单列出来而不是静默丢弃。
    """
    out: dict[str, list[ExitResult]] = {s.name: [] for s in specs}
    skipped = 0
    for q in queries:
        scan = scans_by_code.get(q.code)
        if scan is None or scan.close is None:
            skipped += 1
            continue
        bar = _entry_bar(q, basis, scan, last_bar)
        if bar is None:
            skipped += 1
            continue
        for spec in specs:
            res = simulate_exit(
                scan.close, bar, spec,
                atr=scan.atr, bench_close=bench, price_highs=scan.price_highs,
            )
            if res is not None:
                out[spec.name].append(res)
    return out, skipped


def _rows(sim: dict[str, list[ExitResult]], specs: tuple[ExitSpec, ...], cost: float) -> list[dict]:
    rows = []
    for spec in specs:
        st = _stats(sim.get(spec.name, []), cost)
        rows.append({"exit": spec.name, "mode": spec.mode, "hold_bars": spec.hold_bars,
                     **st})
        rows[-1]["verdict"] = _verdict(rows[-1], cost)
    return rows


def _split(queries, dev_frac: float) -> tuple[int, list, list]:
    bars = sorted(q.bar for q in queries)
    if not bars:
        return 0, [], []
    cut = bars[min(len(bars) - 1, int(len(bars) * dev_frac))]
    return cut, [q for q in queries if q.bar < cut], [q for q in queries if q.bar >= cut]


def _folds(queries, k: int) -> list[list]:
    ordered = sorted(queries, key=lambda q: (q.bar, q.code))
    total = len(ordered)
    return [ordered[i * total // k:(i + 1) * total // k] for i in range(k)]


def _replay_signals(ds, cfg) -> list:
    """重放估计器，返回真正会发信号的查询（含误报）。

    这里刻意不用 ``evaluate_dataset``：它只回传汇总数字与最后 20 条信号，
    而出场实验需要每条信号的 ``bar / code`` 才能定位入场点。
    重放顺序、``advance_to`` / ``observe`` 的时序与 #123 完全一致。
    """
    est = TurnEstimator(cfg, ds.samples)
    picked = []
    for q in ds.queries:
        est.advance_to(q.bar)
        pred = est.estimate(q.code, q.key)
        if pred is None:
            continue
        est.observe(q.available_bar, pred.p_raw, q.y_truth)
        threshold = est.base_rate() * cfg.entry_lift
        if pred.p_turn >= threshold and pred.exp_net_pct > cfg.min_exp_net:
            picked.append(q)
    return picked


def run_lab(
    *,
    start: str = "",
    tag: str = "live",
    max_symbols: int | None = None,
    lookback: int = 1200,
    dev_frac: float = 0.6,
    folds: int = 5,
    log=None,
) -> dict:
    emit = log or (lambda msg: None)
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"非法 tag: {tag}")
    if not 0.3 <= dev_frac <= 0.8:
        raise ValueError("--dev-frac 需在 [0.3, 0.8]")
    if not 2 <= folds <= 10:
        raise ValueError("--folds 需在 [2, 10]")

    t0 = time.time()
    emit("读取真实 K 线并构造扫描输入…")
    prep = prepare_scans(
        start=start, codes="", max_symbols=max_symbols,
        lookback=lookback, log=emit,
    )
    scans = prep["scans"]
    bench = prep["bench_close"]
    dates = prep["dates"]
    last_bar = int(prep["n"]) - 1
    cfg = estimate_config_from_settings()

    emit(f"构造预估数据集（{len(scans)} 只）…")
    ds = build_dataset(scans, bench, cfg)
    scans_by_code = {s.code: s for s in scans}
    emit(f"重放发信号（{len(ds.queries)} 条查询）…")
    signals = _replay_signals(ds, cfg)
    if not signals:
        raise ValueError("没有发出任何信号，无法评估出场规则")
    emit(f"  发信号 {len(signals)} 条，命中 {sum(q.y_truth for q in signals)} 条")

    cut, dev_q, oos_q = _split(signals, dev_frac)
    dev_q = dev_q or signals
    oos_q = oos_q or signals
    specs = EXIT_PRESETS
    cost = float(specs[0].cost_pct)

    emit("模拟全区间各出场规则…")
    dev_rows: list[dict] = []
    oos_rows: list[dict] = []
    all_rows: list[dict] = []
    skipped_by_basis: dict[str, int] = {}
    for basis in ENTRY_BASES:
        sim_dev, skip_dev = _simulate_all(dev_q, scans_by_code, specs, bench, basis, last_bar)
        sim_oos, skip_oos = _simulate_all(oos_q, scans_by_code, specs, bench, basis, last_bar)
        sim_all, skip_all = _simulate_all(signals, scans_by_code, specs, bench, basis, last_bar)
        skipped_by_basis[basis] = skip_all
        for row in _rows(sim_dev, specs, cost):
            dev_rows.append({"entry_basis": basis, **row})
        for row in _rows(sim_oos, specs, cost):
            oos_rows.append({"entry_basis": basis, **row})
        for row in _rows(sim_all, specs, cost):
            all_rows.append({"entry_basis": basis, **row})

    # 开发段只在可交易口径上选规则：confirmed_low 是事后口径，不能用来定实盘规则
    tradeable_dev = [r for r in dev_rows if r["entry_basis"] == "signal_next" and r["n"]]
    best = max(tradeable_dev, key=lambda r: r["mean_net_pct"]) if tradeable_dev else None
    best_name = best["exit"] if best else "fixed3"
    best_spec = next((s for s in specs if s.name == best_name), specs[0])
    baseline_spec = next(s for s in specs if s.name == "fixed3")

    emit(f"样本外验证 {best_name}（开发段选出的最优）…")
    oos_folds: list[dict] = []
    for i, part in enumerate(_folds(oos_q, folds), 1):
        if not part:
            continue
        sim = _simulate_all(part, scans_by_code, [best_spec, baseline_spec], bench,
                            "signal_next", last_bar)[0]
        st_best = _stats(sim[best_spec.name], cost)
        st_base = _stats(sim[baseline_spec.name], cost)
        oos_folds.append({
            "fold": i,
            "n": st_best["n"],
            "mean_net_pct": st_best["mean_net_pct"],
            "mean_excess_pct": st_best["mean_excess_pct"],
            "baseline_net_pct": st_base["mean_net_pct"],
            "baseline_excess_pct": st_base["mean_excess_pct"],
        })

    best_oos = next(
        (r for r in oos_rows if r["entry_basis"] == "signal_next" and r["exit"] == best_name),
        None,
    )
    net_folds = [f["mean_net_pct"] for f in oos_folds if f["mean_net_pct"] is not None]
    pos_folds = sum(1 for x in net_folds if x > 0)
    consistency = round(pos_folds / len(net_folds), 3) if net_folds else None

    if best_oos is None or not best_oos.get("n") or best_oos["n"] < 30:
        conclusion = "insufficient_samples"
    elif best_oos["mean_net_pct"] is not None and best_oos["mean_net_pct"] > 0:
        if consistency is not None and consistency >= 0.6:
            conclusion = "positive_expectancy_candidate"
        else:
            conclusion = "unstable_out_of_sample"
    elif best_oos.get("verdict") == "beta_only":
        conclusion = "beta_only"
    else:
        conclusion = "no_tradeable_edge"

    conclusion_detail = {
        "positive_expectancy_candidate": (
            "样本外均值为正且 ≥60% 的折为正；仍需独立区间复核，且本实验未计入滑点与冲击成本"
        ),
        "unstable_out_of_sample": "样本外均值为正但折间符号不一致，不能据此改口径",
        "beta_only": "绝对收益为正、超额为负：赚的是大盘 beta，不是这套方法的信息",
        "no_tradeable_edge": "样本外均值为负，换出场规则也补不上波段厚度的缺口",
        "insufficient_samples": "样本外有效笔数不足 30，无法判断",
    }.get(conclusion, "")

    elapsed = round(time.time() - t0, 1)
    emit(f"完成，用时 {elapsed}s → {conclusion}")
    return {
        "as_of": dates[last_bar] if dates else None,
        "start": start or None,
        "dev_cut_bar": cut,
        "dev_cut_date": dates[cut] if 0 <= cut < len(dates) else None,
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": elapsed,
        "tag": tag,
        "symbols_scanned": prep["symbols_scanned"],
        "symbols_skipped": prep["symbols_skipped"],
        "queries": len(ds.queries),
        "signals": len(signals),
        "signal_hit_rate": round(sum(q.y_truth for q in signals) / len(signals), 4),
        "skipped_stale": skipped_by_basis,
        "folds": folds,
        "dev_frac": dev_frac,
        "entry_bases": list(ENTRY_BASES),
        "all_rows": all_rows,
        "dev_rows": dev_rows,
        "oos_rows": oos_rows,
        "dev_selection": {
            "entry_basis": "signal_next",
            "exit": best_name,
            "dev_mean_net_pct": best["mean_net_pct"] if best else None,
            "dev_mean_excess_pct": best["mean_excess_pct"] if best else None,
        },
        "oos_best": best_oos,
        "oos_folds": oos_folds,
        "fold_positive": pos_folds,
        "fold_consistency": consistency,
        "conclusion": conclusion,
        "conclusion_detail": conclusion_detail,
        "note": _NOTE,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出场规则实验（只研究不交易）")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--tag", default="live", help="产物标签，写入 data/logs/exit_lab_<tag>.json")
    ap.add_argument("--max-symbols", type=int, default=None, help="最多扫描多少只")
    ap.add_argument("--lookback", type=int, default=1200, help="基准窗口根数")
    ap.add_argument("--dev-frac", type=float, default=0.6, help="开发段占比")
    ap.add_argument("--folds", type=int, default=5, help="样本外分折数")
    ap.add_argument("--report", action="store_true", help="只打印已有产物摘要")
    args = ap.parse_args(argv)

    path = _path(args.tag)
    if args.report:
        if not path.is_file():
            print(f"没有产物：{path}")
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"tag={data['tag']} as_of={data['as_of']} signals={data['signals']} "
              f"conclusion={data['conclusion']}")
        for row in data.get("oos_rows", []):
            if row["entry_basis"] != "signal_next":
                continue
            print(f"  {row['exit']:<12} n={row['n']:<5} "
                  f"net={row['mean_net_pct']}% excess={row['mean_excess_pct']}% "
                  f"win={row['win_rate']} pf={row['profit_factor']} {row['verdict']}")
        return 0

    data = run_lab(
        start=args.start, tag=args.tag, max_symbols=args.max_symbols,
        lookback=args.lookback, dev_frac=args.dev_frac, folds=args.folds,
        log=print,
    )
    _save(path, data)
    print(f"已写入 {path}")
    print(f"结论：{data['conclusion']} — {data['conclusion_detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
