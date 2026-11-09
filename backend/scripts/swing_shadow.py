# -*- coding: utf-8 -*-
"""摆动影子：在真实日线上重放「大盘基准锚 + 个股 RS 摆动共振」，只记录不交易。

与 ``scripts/shadow.py`` 的分工：那个冻结参数逐日重跑整段回测、比较净值；
这个只算**信号与确认滞后后还剩多少波段幅度**，不构造组合、不算收益、
不读写 ``paper_*`` 表、不进评分与下单路径。产物 ``data/logs/swing_shadow_<tag>.json``。

前视偏差口径（见 tests/test_swing_shadow.py）：先用全量序列算出所有摆动点，
再按 ``confirmed_bar <= t`` 过滤 —— 单测已证明它与「每个交易日只用截至当天的数据
重算」逐 bar 等价，且点一旦输出不会被后面的数据撤销。所以本脚本虽只算一遍，
语义上仍是逐日重放，不存在「先标好全区间高低点再对齐买卖」。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts\\swing_shadow.py --start 2026-09-01 --tag live
    .venv\\Scripts\\python.exe scripts\\swing_shadow.py --tag live --report
    .venv\\Scripts\\python.exe scripts\\swing_shadow.py --codes 000001,600519 --start 2026-09-01
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app.analysis.baseline import (  # noqa: E402
    anchor_windows,
    configs_from_settings,
    relative_strength,
    scan_symbol,
    summarize,
    wilder_atr,
)
from app.analysis.estimate import (  # noqa: E402
    SymbolScan,
    estimate_config_from_settings,
    evaluate,
)
from app.analysis.regime import detect_regime  # noqa: E402
from app.analysis.swing import detect_swings  # noqa: E402
from app.config import BASE_DIR, get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_INDEX_KEY = "idx_sh000001"
_POOL_PATH = BASE_DIR / "backend" / "app" / "data" / "cache" / "pool_latest.parquet"
_MAX_SIGNALS = 500
_NOTE = (
    "纯观测：只记录 RS 摆动信号、确认滞后后的剩余波段幅度与事前预估的条件分布，"
    "不参与选股、评分、撮合；预估不是点位预测，标签窗口未走完的样本不入库；"
    "RS 上升不等于价格上涨，弱市须叠加 regime 闸门；评估一律用相对基准的超额收益。"
)


def _path(tag: str):
    log_dir = get_settings().kline_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"swing_shadow_{tag}.json"


def _save(path, data: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    os.replace(tmp, path)


def _symbols(codes: str, max_n: int) -> list[str]:
    if codes:
        return [c.strip().zfill(6) for c in codes.split(",") if c.strip()][:max_n]
    if _POOL_PATH.is_file():
        pool = pd.read_parquet(_POOL_PATH)
        return pool["code"].astype(str).str.zfill(6).tolist()[:max_n]
    kdir = get_settings().kline_dir
    names = [p.stem for p in sorted(kdir.glob("*.parquet")) if not p.stem.startswith("idx_")]
    return names[:max_n]


def _align(frame: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    data = frame.copy()
    data["date"] = data["date"].astype(str).str[:10]
    return data.drop_duplicates("date", keep="last").set_index("date").reindex(dates)


def prepare_scans(
    *,
    start: str = "",
    end: str = "",
    codes: str = "",
    max_symbols: int | None = None,
    lookback: int = 1200,
    log=None,
) -> dict:
    """读取真实 K 线，产出摆动影子与敏感性网格**共用**的中间数据。

    抽出来是为了让 #122 的网格与 #121 的线上产物用完全相同的对齐、warmup、
    摆动尺度与 RS 序列 —— 口径不一致时网格结论无法与线上结果对照。
    """
    emit = log or (lambda msg: None)
    settings = get_settings()
    swing_cfg, base_cfg = configs_from_settings()
    est_cfg = estimate_config_from_settings()

    bench = kline_cache.get(_INDEX_KEY)
    if bench is None or bench.empty:
        raise ValueError(f"缺少指数日线 {_INDEX_KEY}.parquet")
    bench = bench.copy()
    bench["date"] = bench["date"].astype(str).str[:10]
    bench = bench.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if end:
        bench = bench[bench["date"] <= end].reset_index(drop=True)
    # 只保留最近 lookback 根：摆动点与滚动窗口都是局部量，1990 年代的 bar
    # 既不影响今天，又会让 300 只票的逐 bar 循环白跑几十倍。
    if lookback > 0 and len(bench) > lookback:
        bench = bench.tail(lookback).reset_index(drop=True)
    dates = bench["date"].tolist()
    if start and start < dates[0]:
        raise ValueError(f"--start {start} 早于基准窗口起点 {dates[0]}，请加大 --lookback")
    n = len(dates)
    warmup = max(base_cfg.z_window, base_cfg.corr_window, base_cfg.beta_window) + 2 * swing_cfg.confirm_bars
    if n < warmup + 10:
        raise ValueError(f"指数历史不足：{n} 根，至少需要 {warmup + 10} 根")
    start_bar = next((i for i, d in enumerate(dates) if d >= start), warmup) if start else warmup
    scans: list[SymbolScan] = []

    bench_close = bench["close"].astype(float).reset_index(drop=True)
    bench_atr = wilder_atr(bench, 14)
    bench_lows = [
        p for p in detect_swings(bench_close, atr=bench_atr, config=swing_cfg)
        if p.kind == "low"
    ]
    windows = anchor_windows(bench_lows, base_cfg)

    limit = int(max_symbols or settings.swing_max_symbols)
    symbols = _symbols(codes, limit)
    signals: list[dict] = []
    amplitudes: list[dict] = []
    skipped = 0
    for idx, code in enumerate(symbols, 1):
        df = kline_cache.get(code)
        if df is None or df.empty:
            skipped += 1
            continue
        aligned = _align(df, dates)
        close = aligned["close"].astype(float).reset_index(drop=True)
        first_valid = close.first_valid_index()
        if first_valid is None or int(first_valid) > n - warmup - 5:
            skipped += 1
            continue
        # 上市前的空缺用「首个有效收盘」补常数（不引入任何未来变动），
        # 并靠 warmup 过滤保证这段永不产生输出信号。
        close = close.bfill().ffill()
        sig, amp = scan_symbol(
            code, close, bench_close,
            swing_cfg=swing_cfg, base_cfg=base_cfg, windows=windows,
        )
        for row in sig:
            row["date"] = dates[row["bar"]]
        signals.extend(s for s in sig if s["bar"] >= int(first_valid) + warmup)
        amplitudes.extend(a for a in amp if a["pivot_bar"] >= int(first_valid) + warmup)
        rs = relative_strength(close, bench_close)
        scans.append(SymbolScan(
            code=code,
            rs=rs,
            points=detect_swings(rs, config=swing_cfg),
            first_bar=max(int(first_valid) + warmup, start_bar),
        ))
        if idx % 100 == 0:
            emit(f"  已扫描 {idx}/{len(symbols)} 只 · 累计信号 {len(signals)}")

    return {
        "dates": dates,
        "bench_close": bench_close,
        "scans": scans,
        "signals": signals,
        "amplitudes": amplitudes,
        "swing_cfg": swing_cfg,
        "base_cfg": base_cfg,
        "est_cfg": est_cfg,
        "bench_lows": bench_lows,
        "windows": windows,
        "n": n,
        "warmup": warmup,
        "start_bar": start_bar,
        "limit": limit,
        "symbols_scanned": len(symbols) - skipped,
        "symbols_skipped": skipped,
    }


def run(
    *,
    start: str = "",
    end: str = "",
    tag: str = "live",
    codes: str = "",
    max_symbols: int | None = None,
    lookback: int = 1200,
    log=None,
) -> dict:
    """扫描并落盘一份影子观测结果。"""
    emit = log or (lambda msg: None)
    if not _TAG_RE.fullmatch(tag):
        raise ValueError(f"非法 tag: {tag}")
    prep = prepare_scans(
        start=start, end=end, codes=codes, max_symbols=max_symbols,
        lookback=lookback, log=emit,
    )
    dates = prep["dates"]
    bench_close = prep["bench_close"]
    scans = prep["scans"]
    signals = prep["signals"]
    amplitudes = prep["amplitudes"]
    swing_cfg, base_cfg, est_cfg = prep["swing_cfg"], prep["base_cfg"], prep["est_cfg"]
    n, warmup, limit = prep["n"], prep["warmup"], prep["limit"]
    bench_lows, windows = prep["bench_lows"], prep["windows"]
    skipped, symbols_scanned = prep["symbols_skipped"], prep["symbols_scanned"]

    if start:
        signals = [s for s in signals if s["date"] >= start]
        amplitudes = [a for a in amplitudes if dates[a["pivot_bar"]] >= start]
    window_days = [d for d in dates if d >= start] if start else dates[warmup:]
    per_day: dict[str, dict] = {}
    for row in signals:
        day = per_day.setdefault(row["date"], {"signals": 0, "resonance": 0})
        day["signals"] += 1
        if row["status"] == "resonance_buy":
            day["resonance"] += 1

    estimate_data = evaluate(scans, bench_close, est_cfg) if scans else {}
    emit(
        f"  事前预估：{estimate_data.get('estimated_queries')} 次 / "
        f"观测信号 {estimate_data.get('predicted_signals')} 条"
    )
    stats = summarize(signals, amplitudes)
    stats["symbols_scanned"] = symbols_scanned
    stats["symbols_skipped"] = skipped
    density = {
        "trading_days": len(window_days),
        "days_with_signal": len(per_day),
        "signals_per_day": round(len(signals) / len(window_days), 3) if window_days else None,
        "resonance_per_day": (
            round(sum(v["resonance"] for v in per_day.values()) / len(window_days), 3)
            if window_days else None
        ),
    }
    regime = detect_regime(bench_close.iloc[:-1])
    ma60 = float(bench_close.rolling(60, min_periods=60).mean().iloc[-1])
    data = {
        "tag": tag,
        "start": start or dates[0],
        "end": end or dates[-1],
        "as_of": dates[-1],
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            **asdict(swing_cfg), **asdict(base_cfg), **asdict(est_cfg),
            "max_symbols": limit,
        },
        "regime": regime,
        "benchmark": {
            "bars": n,
            "swing_lows": len(bench_lows),
            "anchor_windows": len(windows),
            "latest_close": round(float(bench_close.iloc[-1]), 2),
            # 与线上弱市闸门（收盘 < MA60）同口径，方便和 regime 的机制层四态对照
            "ma60": round(ma60, 2),
            "below_ma60": bool(float(bench_close.iloc[-1]) < ma60),
        },
        "stats": stats,
        "signal_density": density,
        "signals": sorted(signals, key=lambda r: (r["date"], r["code"]))[-_MAX_SIGNALS:],
        "estimate": estimate_data,
        "note": _NOTE,
    }
    emit(f"扫描完成：{stats['symbols_scanned']} 只 / {len(signals)} 条信号 / {density['trading_days']} 个交易日")
    return data


def report(data: dict) -> None:
    """打印人读摘要；样本不足时明确不下策略结论。"""
    s = data.get("stats") or {}
    d = data.get("signal_density") or {}
    b = data.get("benchmark") or {}
    print(f"摆动影子 {data.get('tag')} · {data.get('start')} ~ {data.get('as_of')}（{data.get('run_at')}）")
    print(
        f"  基准：{b.get('bars')} 根 / 已确认低点 {b.get('swing_lows')} 个 / "
        f"锚窗 {b.get('anchor_windows')} 个 · 收盘 {b.get('latest_close')} / MA60 {b.get('ma60')} "
        f"（{'在均线下方' if b.get('below_ma60') else '在均线上方'}）"
    )
    print(f"  regime（机制层四态，与线上 MA60 闸门不同口径）：{(data.get('regime') or {}).get('state')} · 敞口上限 {(data.get('regime') or {}).get('exposure_cap')}")
    print(f"  扫描：{s.get('symbols_scanned')} 只（跳过 {s.get('symbols_skipped')} 只）/ 信号 {s.get('signals')} 条")
    print(f"  密度：{d.get('signals_per_day')} 条/日 · 共振 {d.get('resonance_per_day')} 条/日 · 有信号 {d.get('days_with_signal')} 天")
    print(f"  状态分布：{s.get('by_status')}")
    print(f"  偏离分档：{s.get('by_deviation')}")
    print(
        f"  波段幅度（RS）：样本 {s.get('amplitude_samples')} · 极值到极值中位数 "
        f"{s.get('median_gross_pct')}% · 确认后剩余 {s.get('median_net_pct')}% · "
        f"净段为正占比 {s.get('net_positive_ratio')} · 滞后 {s.get('median_lag_bars')} 根"
    )
    e = data.get("estimate") or {}
    if e:
        print(
            f"  事前预估（条件分布）：样本 {e.get('samples')} / 重放 {e.get('estimated_queries')} 次 · "
            f"Brier {e.get('brier_score')}（常数基准 {e.get('brier_baseline')} / "
            f"技巧 {e.get('brier_skill')}） · 校准 {len(e.get('calibration_bins') or [])} 箱"
        )
        print(
            f"  基础率 {e.get('base_rate')} · 发信号阈值中位数 {e.get('median_entry_threshold')} · "
            f"p_turn p50/p90/p99 {e.get('p_turn_p50')}/{e.get('p_turn_p90')}/{e.get('p_turn_p99')}"
        )
        print(
            f"  观测信号 {e.get('predicted_signals')} 条 · 命中率 {e.get('hit_rate')} · "
            f"门槛 {e.get('breakeven_hit_pct')}% · 判定 {e.get('verdict')}"
        )
        print(
            f"  命中收益 {e.get('median_hit_forward_pct')}% · 误报下探 {e.get('median_miss_loss_pct')}% · "
            f"实得期望净 {e.get('mean_realized_net_pct')}% / 模型预估净 {e.get('median_exp_net_pct')}% · "
            f"等确认对照 {e.get('median_confirmed_pct')}% · 个股权重 {e.get('median_symbol_weight')}"
        )
    print(f"  {data.get('note')}")
    if (s.get("amplitude_samples") or 0) < 30:
        print("  样本不足 30 段：只记录分布，不对策略有效性下结论。")


def main() -> None:
    ap = argparse.ArgumentParser(description="摆动影子：只记录不交易的 RS 极值共振观测")
    ap.add_argument("--start", default="", help="起始日期 YYYY-MM-DD，空=全历史")
    ap.add_argument("--end", default="", help="截止日期 YYYY-MM-DD，空=最新")
    ap.add_argument("--tag", default="live", help="产物标签（字母数字下划线）")
    ap.add_argument("--codes", default="", help="只扫指定票，逗号分隔")
    ap.add_argument("--max-symbols", type=int, default=0, help="覆盖 Settings.swing_max_symbols")
    ap.add_argument("--lookback", type=int, default=1200, help="基准窗口保留最近多少根日线")
    ap.add_argument("--report", action="store_true", help="只打印已有产物，不重扫")
    args = ap.parse_args()

    path = _path(args.tag)
    if args.report:
        if not path.is_file():
            print(f"尚无产物：{path}")
            return
        report(json.loads(path.read_text(encoding="utf-8")))
        return
    data = run(
        start=args.start, end=args.end, tag=args.tag, codes=args.codes,
        max_symbols=args.max_symbols or None, lookback=args.lookback, log=print,
    )
    _save(path, data)
    print(f"已写入 {path}")
    report(data)


if __name__ == "__main__":
    main()
