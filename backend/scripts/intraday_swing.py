# -*- coding: utf-8 -*-
"""日内摆动实验（BACKTEST_LOG #125）：更短周期能否让 RS 摆动低点重新获得预测力，只研究不交易。

思路来源：#124 收口时给的下一条未试路径 —— 把日线换成更短周期（30 / 60 分钟）。
日线 RS 摆动低点在样本外无预测力（#120~#124），一个自然猜想是「日线太粗，把转折都抹平了，
日内级别能看到日线看不到的局部极值」。本实验直接验证这个猜想是否成立。

与 #124 的口径完全一致（否则不可比、会回到事后口径的假阳性）：
1. **可交易入场**：信号在 ``q.bar`` 收盘产生、次日（下一根）收盘买入（``signal_next``）；
   ``confirmed_low`` 仅作事后对照，不参与选规则。
2. **误报计入**：所有发信号的查询都模拟出场，不管 ``y_truth``。
3. **绝对与超额并列**：绝对赚、超额亏 = 只赚到 beta（``verdict=beta_only``）。
4. **日内基准线**：日线用的是 ``idx_sh000001``（上证指数），但它的分钟级版本**没有存盘**，
   所以这里用「扫描宇宙的等权均价」作为大盘代理（self-inclusion 在 300 只量级可忽略，
   已在文档标注）。这是与 #120~#124 唯一的方法论差异，且是数据可得性逼出来的。
5. **参数按周期缩放**：z_window / confirm_bars / horizon 按「每交易日根数」倍增
   （30min→8 根/日，60min→4 根/日），其余预估与出场参数沿用 #123 默认值。

数据：``data/minute5_fsdb/{code}.parquet``（5 分钟 bar，2025-01-02~2026-11-06）。
框架本身是频率无关的（detect_swings / relative_strength / build_dataset / simulate_exit
都只吃序列），所以 #120~#124 那套预估 + 出场评估可原样复用，仅换输入序列与缩放参数。

产物 ``data/logs/intraday_swing_{period}_{tag}.json``，只读接口 ``GET /api/ops/intraday-swing``。
不接评分、不接下单、不读写 ``paper_*``、不注册调度。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\intraday_swing.py --period 30 --start 2025-01-01 --tag live
    .venv\\Scripts\\python.exe scripts\\intraday_swing.py --period 60 --tag live --report
    .venv\\Scripts\\python.exe scripts\\intraday_swing.py --period 30 --max-symbols 20 --tag smoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.analysis.baseline import (  # noqa: E402
    configs_from_settings,
    relative_strength,
    wilder_atr,
)
from app.analysis.estimate import (  # noqa: E402
    EstimateConfig,
    SymbolScan,
    build_dataset,
    estimate_config_from_settings,
    evaluate,
)
from app.analysis.exit import ExitSpec, simulate_exit  # noqa: E402
from app.analysis.swing import detect_swings  # noqa: E402
from app.config import get_settings  # noqa: E402
from exit_lab import (  # noqa: E402  # #124 的评估/出场回放逻辑，原样复用
    ENTRY_BASES,
    _entry_bar,
    _folds,
    _replay_signals,
    _rows,
    _simulate_all,
    _split,
    _stats,
    _verdict,
)
from swing_shadow import _symbols  # noqa: E402  # 同款宇宙选取，保证与 #120~#124 对齐

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: 每个交易日的分钟 bar 数（240 交易分钟 / 周期）。用于把日线级别参数缩放到日内。
_BARS_PER_DAY = {30: 8, 60: 4}

_NOTE = (
    "纯研究产物：5 分钟聚合到 30/60 分钟，用扫描宇宙等权均价作大盘代理（无上证指数分钟数据）；"
    "入场用下一根收盘（可交易口径），误报计入，绝对与超额收益并列；开发段选出场规则、"
    "样本外只验证一次再等频分 5 折；不接评分/下单，超额不行为正即视为只有 beta、无可交易 alpha。"
)


def _minute_dir() -> "os.PathLike":
    return get_settings().kline_dir.parent / "minute5_fsdb"


def _load_aggregated(code: str, agg: int) -> pd.DataFrame | None:
    """读 5 分钟 parquet，聚合到 ``agg`` 根一组（30min=6，60min=12），返回带时间戳索引的 OHLCV。

    按连续 bar 分组（不按墙钟对齐）：午休/隔夜缺口恰好落在组边界上，不会让一根聚合 bar
    跨过缺口，所以 OHLC 仍是连续 30/60 交易分钟。
    """
    path = _minute_dir() / f"{code}.parquet"
    if not path.is_file():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - 坏文件按无数据处理
        return None
    if df is None or df.empty or "date" not in df.columns:
        return None
    # 个别指数类文件只有 (date, close)，缺 OHLC → 无法算 ATR 与聚合 bar，跳过。
    if not {"open", "high", "low", "close", "volume", "amount"}.issubset(df.columns):
        return None
    df = df.sort_values("date").reset_index(drop=True)
    gid = np.arange(len(df)) // agg
    g = df.groupby(gid).agg(
        date=("date", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    ).reset_index(drop=True)
    g["date"] = g["date"].astype(str)
    return g.set_index("date")


def prepare_intraday_scans(
    *,
    period: int = 30,
    start: str = "",
    max_symbols: int | None = None,
    lookback: int = 0,
    log=None,
) -> dict:
    """读 5 分钟 K 线 → 聚合 → 对齐 → 篮子基准 → RS 摆动 → SymbolScan。

    与 ``swing_shadow.prepare_scans`` 平行：返回 ``{dates, bench_close, scans, ...}``，
    下游 ``build_dataset`` / ``TurnEstimator`` / ``simulate_exit`` 原样可用（频率无关）。
    """
    emit = log or (lambda msg: None)
    agg = {30: 6, 60: 12}[period]
    bpd = _BARS_PER_DAY[period]
    swing_cfg, base_cfg = configs_from_settings()
    est_cfg = estimate_config_from_settings()
    swing_cfg = swing_cfg.__class__(confirm_bars=max(2, 2 * bpd))

    limit = int(max_symbols or get_settings().swing_max_symbols)
    universe = [c for c in _symbols("", limit) if (_minute_dir() / f"{c}.parquet").is_file()]
    if not universe:
        raise ValueError("分钟库为空或宇宙与分钟库无交集")

    emit(f"加载并聚合 {len(universe)} 只 × 5 分钟 → {period} 分钟…")
    frames: dict[str, pd.DataFrame] = {}
    raw_close: dict[str, pd.Series] = {}
    for code in universe:
        g = _load_aggregated(code, agg)
        if g is None or len(g) < 60:
            continue
        frames[code] = g
        raw_close[code] = g["close"].astype(float)
    if not frames:
        raise ValueError("没有任何分钟数据可加载")

    master = next(iter(frames.values())).index  # 所有文件日历一致（同为 2025-01-02~）
    n = len(master)
    warmup = max(base_cfg.z_window, base_cfg.corr_window, base_cfg.beta_window) * bpd + 2 * swing_cfg.confirm_bars
    if n < warmup + 10:
        raise ValueError(f"分钟历史不足：{n} 根，至少需要 {warmup + 10} 根")
    start_bar = 0
    if start:
        hit = next((i for i, d in enumerate(master) if d[:10] >= start), None)
        if hit is None:
            raise ValueError(f"--start {start} 晚于数据末端")
        start_bar = hit

    # 对齐到 master 时间线；上市前/退市后用 bfill/ffill 补常数（不引入未来变动），
    # 并记录真实有效区间，出场回放时跳过退市后那段被填成常数的僵尸样本。
    aligned_close: dict[str, pd.Series] = {}
    last_valid: dict[str, int] = {}
    for code, s in raw_close.items():
        reidx = s.reindex(master)
        valid = reidx.notna()
        last_valid[code] = int(np.where(valid.values)[0][-1]) if valid.any() else len(master) - 1
        aligned_close[code] = reidx.bfill().ffill()

    # 篮子基准：扫描宇宙等权均价（self-inclusion 在 300 只量级可忽略）。
    mat = np.vstack([aligned_close[c].to_numpy(dtype=float) for c in aligned_close])
    baseline = pd.Series(np.nanmean(mat, axis=0), index=master)

    emit("计算 RS 与摆动点…")
    scans: list[SymbolScan] = []
    skipped = len(universe) - len(frames)
    for code in frames:
        close = aligned_close[code].reset_index(drop=True)
        rs = relative_strength(close, baseline).reset_index(drop=True)
        atr = wilder_atr(frames[code].assign(close=close), 14).reindex(master).bfill().ffill().reset_index(drop=True)
        price_pts = detect_swings(close, atr=atr, config=swing_cfg)
        lv = last_valid[code]
        scans.append(SymbolScan(
            code=code,
            rs=rs,
            points=detect_swings(rs, config=swing_cfg),
            first_bar=max(warmup, start_bar),
            close=close,
            atr=atr,
            price_highs=[p for p in price_pts if p.kind == "high"],
            last_valid_bar=lv,
        ))
        if len(scans) % 100 == 0:
            emit(f"  已处理 {len(scans)} 只")

    return {
        "dates": list(master),
        "bench_close": baseline,
        "scans": scans,
        "swing_cfg": swing_cfg,
        "base_cfg": base_cfg,
        "est_cfg": est_cfg,
        "n": n,
        "warmup": warmup,
        "start_bar": start_bar,
        "limit": limit,
        "period": period,
        "bars_per_day": bpd,
        "symbols_scanned": len(frames),
        "symbols_skipped": skipped,
        "baseline_kind": "equal_weight_universe_mean",
    }


def _intraday_estimate_config(period: int, base_est: EstimateConfig) -> EstimateConfig:
    """把日线级预估参数按周期缩放到日内（其余沿用 #123 默认值）。"""
    bpd = _BARS_PER_DAY[period]
    return base_est.__class__(
        horizon=max(2, 3 * bpd),
        prior_strength=base_est.prior_strength,
        symbol_cap=base_est.symbol_cap,
        entry_lift=base_est.entry_lift,
        cost_pct=base_est.cost_pct,
        min_exp_net=base_est.min_exp_net,
        z_window=60 * bpd,
        confirm_bars=max(2, 2 * bpd),
        min_global_samples=base_est.min_global_samples,
        features=base_est.features,
        calibration=base_est.calibration,
        calib_min_samples=base_est.calib_min_samples,
        calib_refit_every=base_est.calib_refit_every,
        calib_l2=base_est.calib_l2,
    )


def _intraday_exit_presets(period: int, cost_pct: float) -> tuple[ExitSpec, ...]:
    """按周期缩放的出场预设：持有以「交易日」为单位。

    ATR 跟踪宽度**不**按 bpd 放大（那是 bug：atr_k 乘 bpd 后止损宽到约 8%~16%，
    等于变相长持，把「日内择时」偷换成「周级持有」）。ATR 作为价格比例，紧跟踪就该
    用 2~4 倍——这才是真正的跟踪止盈。持有上限设 10×bpd（约 10 个交易日）兜底。
    """
    bpd = _BARS_PER_DAY[period]
    cap = 10 * bpd
    return (
        ExitSpec(f"fixed{bpd}", "fixed", hold_bars=bpd, cost_pct=cost_pct),
        ExitSpec(f"fixed{2 * bpd}", "fixed", hold_bars=2 * bpd, cost_pct=cost_pct),
        ExitSpec(f"fixed{4 * bpd}", "fixed", hold_bars=4 * bpd, cost_pct=cost_pct),
        ExitSpec("atr2", "atr_trail", hold_bars=cap, atr_k=2.0, floor_pct=1.5, cost_pct=cost_pct),
        ExitSpec("atr4", "atr_trail", hold_bars=cap, atr_k=4.0, floor_pct=2.0, cost_pct=cost_pct),
        ExitSpec("swing_trail", "swing_trail", hold_bars=cap, cost_pct=cost_pct),
    )


def _path(period: int, tag: str):
    log_dir = get_settings().kline_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"intraday_swing_{period}_{tag}.json"


def _save(path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def run_lab(
    *,
    period: int = 30,
    start: str = "",
    tag: str = "live",
    max_symbols: int | None = None,
    lookback: int = 0,
    dev_frac: float = 0.6,
    folds: int = 5,
    log=None,
) -> dict:
    emit = log or (lambda msg: None)
    if period not in (30, 60):
        raise ValueError("--period 只支持 30 或 60")
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"非法 tag: {tag}")

    t0 = time.time()
    prep = prepare_intraday_scans(
        period=period, start=start, max_symbols=max_symbols, lookback=lookback, log=emit,
    )
    scans = prep["scans"]
    bench = prep["bench_close"]
    dates = prep["dates"]
    last_bar = int(prep["n"]) - 1
    base_est = estimate_config_from_settings()
    est_cfg = _intraday_estimate_config(period, base_est)
    est_cfg.validate()

    emit(f"构造预估数据集（{len(scans)} 只，{len(dates)} 根 {period}min bar）…")
    ds = build_dataset(scans, bench, est_cfg)

    emit("核心评估（Brier skill / 命中率 / walk-forward / 多重检验）…")
    est_eval = evaluate(scans, bench, est_cfg, dataset=ds)

    scans_by_code = {s.code: s for s in scans}
    emit(f"重放发信号（{len(ds.queries)} 条查询）…")
    signals = _replay_signals(ds, est_cfg)
    if not signals:
        raise ValueError("没有发出任何信号，无法评估")
    emit(f"  发信号 {len(signals)} 条，命中 {sum(q.y_truth for q in signals)} 条")

    cut, dev_q, oos_q = _split(signals, dev_frac)
    dev_q = dev_q or signals
    oos_q = oos_q or signals
    specs = _intraday_exit_presets(period, float(est_cfg.cost_pct))
    cost = float(est_cfg.cost_pct)

    emit("模拟全区间各出场规则（可交易口径 + 事后对照）…")
    dev_rows, oos_rows, all_rows, skipped_by_basis = [], [], [], {}
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

    tradeable_dev = [r for r in dev_rows if r["entry_basis"] == "signal_next" and r.get("n")]
    best = max(tradeable_dev, key=lambda r: r["mean_net_pct"]) if tradeable_dev else None
    best_name = best["exit"] if best else specs[0].name
    best_spec = next((s for s in specs if s.name == best_name), specs[0])
    baseline_spec = next(s for s in specs if s.name.startswith("fixed"))

    emit(f"样本外验证 {best_name}…")
    oos_folds = []
    for i, part in enumerate(_folds(oos_q, folds), 1):
        if not part:
            continue
        sim = _simulate_all(part, scans_by_code, [best_spec, baseline_spec], bench,
                            "signal_next", last_bar)[0]
        st_best = _stats(sim[best_spec.name], cost)
        st_base = _stats(sim[baseline_spec.name], cost)
        oos_folds.append({
            "fold": i, "n": st_best["n"],
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

    # 结论：以「可交易口径下场外净期望」为主，叠加核心评估（估计器自身 verdict）
    est_verdict = est_eval.get("verdict") if isinstance(est_eval, dict) else None
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
        "positive_expectancy_candidate": "样本外均值为正且 ≥60% 折为正；仍需独立区间复核，且未计滑点/冲击",
        "unstable_out_of_sample": "样本外均值为正但折间符号不一致，不能据此改口径",
        "beta_only": "绝对收益为正、超额为负：赚的是大盘 beta，不是这套方法的信息",
        "no_tradeable_edge": "样本外均值为负，更短周期也没能让 RS 摆动低点重新产生 alpha",
        "insufficient_samples": "样本外有效笔数不足 30，无法判断",
    }.get(conclusion, "")

    elapsed = round(time.time() - t0, 1)
    emit(f"完成，用时 {elapsed}s → {conclusion}")
    return {
        "period": period,
        "bars_per_day": prep["bars_per_day"],
        "baseline_kind": prep["baseline_kind"],
        "as_of": dates[last_bar] if dates else None,
        "start": start or None,
        "dev_cut_bar": cut,
        "dev_cut_date": dates[cut] if 0 <= cut < len(dates) else None,
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": elapsed,
        "tag": tag,
        "symbols_scanned": prep["symbols_scanned"],
        "symbols_skipped": prep["symbols_skipped"],
        "bars": prep["n"],
        "queries": len(ds.queries),
        "signals": len(signals),
        "signal_hit_rate": round(sum(q.y_truth for q in signals) / len(signals), 4),
        "skipped_stale": skipped_by_basis,
        "folds": folds,
        "dev_frac": dev_frac,
        "entry_bases": list(ENTRY_BASES),
        "scaled_config": {
            "z_window": est_cfg.z_window,
            "confirm_bars": est_cfg.confirm_bars,
            "horizon": est_cfg.horizon,
            "cost_pct": est_cfg.cost_pct,
            "entry_lift": est_cfg.entry_lift,
            "features": est_cfg.features,
        },
        "estimate": est_eval,
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
    ap = argparse.ArgumentParser(description="日内摆动实验（更短周期，只研究不交易）")
    ap.add_argument("--period", type=int, default=30, choices=(30, 60), help="聚合周期（分钟）")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--tag", default="live", help="产物标签，写入 data/logs/intraday_swing_<period>_<tag>.json")
    ap.add_argument("--max-symbols", type=int, default=None, help="最多扫描多少只（默认沿用 swing_max_symbols）")
    ap.add_argument("--lookback", type=int, default=0, help="保留最近 N 根（0=全量）")
    ap.add_argument("--dev-frac", type=float, default=0.6, help="开发段占比")
    ap.add_argument("--folds", type=int, default=5, help="样本外分折数")
    ap.add_argument("--report", action="store_true", help="只打印已有产物摘要")
    args = ap.parse_args(argv)

    path = _path(args.period, args.tag)
    if args.report:
        if not path.is_file():
            print(f"没有产物：{path}")
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"period={data['period']} as_of={data['as_of']} signals={data['signals']} "
              f"conclusion={data['conclusion']}")
        est = data.get("estimate", {})
        if isinstance(est, dict):
            print("  评估:", {k: est.get(k) for k in (
                "verdict", "brier_skill", "hit_rate", "hit_rate_threshold",
                "exp_net_pct", "predicted_signals")})
        for row in data.get("oos_rows", []):
            if row["entry_basis"] != "signal_next":
                continue
            print(f"  {row['exit']:<12} n={row['n']:<5} net={row['mean_net_pct']}% "
                  f"excess={row['mean_excess_pct']}% win={row['win_rate']} "
                  f"pf={row['profit_factor']} {row['verdict']}")
        return 0

    data = run_lab(
        period=args.period, start=args.start, tag=args.tag, max_symbols=args.max_symbols,
        lookback=args.lookback, dev_frac=args.dev_frac, folds=args.folds, log=print,
    )
    _save(path, data)
    print(f"已写入 {path}")
    print(f"结论：{data['conclusion']} — {data['conclusion_detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
