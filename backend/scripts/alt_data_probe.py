"""另类数据事件研究探针（只研究、零生产写入）。

检验「大涨前 1~2 个月是否有资金埋伏」：`docs/analysis/BIG_MOVE_PROBE_LONG_2026-11.md`
用分钟线代理做的结论是「没有证据」，并点名缺龙虎榜 / 股东户数 / 融资余额。
本探针用 `fetch_alt_data.py` 落库的这三类数据复验同一假设。

事件定义：本地日 K 上，T 日买入并持有 20 个交易日涨幅 ≥ 20%（`fwd20`）。
特征一律取 **T 之前**可得的数据（走 `alt_store.all_features` 的 as_of 截断），
对照为**同日**随机抽取的其它股票，计算事件票在当日横截面的分位。
分位均值若仍在 0.5±0.05，说明这批外部数据也没有「埋伏」信号，直接归档不采纳，
不进入 18 段配对回测（避免重复投入）。

用法：
    .venv\\Scripts\\python.exe scripts\\alt_data_probe.py --self-test
    .venv\\Scripts\\python.exe scripts\\alt_data_probe.py --max-events 2000 --controls 20
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.data.alt_store import (  # noqa: E402
    ASOF_RULES,
    DATA_DIR,
    DEFAULT_ALT_DB,
    FEATURE_NAMES,
    SCHEMA_SQL,
    all_features,
    connect,
)

KLINE_DIR = DATA_DIR / "klines"
OUT_FILE = DATA_DIR / "logs" / "alt_data_probe.json"
HORIZON = 20
THRESHOLD = 0.20
NEUTRAL_BAND = 0.05


def _percentile(event_val, control_vals) -> float | None:
    """事件票在当日对照横截面中的中秩分位；缺值或全并列返回 None。

    稀疏计数特征（上榜次数等）经常出现「事件票与对照全是 0」，若按朴素分位
    会算出 0.000 并被误读成强信号，因此全并列一律视为无横截面信息、跳过。
    """
    vals = [v for v in control_vals if v is not None]
    if event_val is None or not vals:
        return None
    if all(v == event_val for v in vals):
        return None
    return (sum(v < event_val for v in vals) + 0.5 * sum(v == event_val for v in vals)) / len(vals)


def _cluster_t(by_day: dict[str, list[float]]) -> float | None:
    """按交易日先取日均分位、再做 t 检验，避免同日样本虚增显著性。"""
    means = [sum(v) / len(v) for v in by_day.values() if v]
    if len(means) < 3:
        return None
    mean = sum(means) / len(means)
    var = sum((x - mean) ** 2 for x in means) / (len(means) - 1)
    se = math.sqrt(var / len(means))
    return None if se == 0 else (mean - 0.5) / se


def _verdict(mean_pct: float | None, n: int) -> str:
    if mean_pct is None or n < 30:
        return "样本不足，不下结论"
    if abs(mean_pct - 0.5) <= NEUTRAL_BAND:
        return "无信号（分位未偏离 0.5，归档不采纳，不进 18 段配对）"
    return "有偏离（需进一步做 OOS rank-IC + 18 段 × 21 日配对复核）"


def _load_klines(kline_dir: Path, max_codes: int) -> dict[str, pd.DataFrame]:
    files = sorted(kline_dir.glob("[0-9]" * 6 + ".parquet"))
    if max_codes:
        files = files[:max_codes]
    out: dict[str, pd.DataFrame] = {}
    for p in files:
        try:
            df = pd.read_parquet(p, columns=["date", "close"])
        except Exception:
            continue
        df["date"] = df["date"].astype(str).str[:10]
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date")
        if len(df) > HORIZON + 5:
            out[p.stem] = df.reset_index(drop=True)
    return out


def _build_events(klines: dict[str, pd.DataFrame]) -> list[tuple[str, str]]:
    events = []
    for code, df in klines.items():
        close = df["close"].astype(float)
        fwd = close.shift(-HORIZON) / close - 1
        for day in df.loc[fwd >= THRESHOLD, "date"]:
            events.append((code, day))
    return events


def _self_test() -> int:
    """离线自测：不依赖网络与真实数据，验证分位 / 聚类 t / 零前视接线。"""
    import sqlite3
    import tempfile

    assert _percentile(2.0, [1.0, 1.5, 3.0]) == 2 / 3
    assert _percentile(None, [1.0]) is None
    assert _percentile(0.0, [0.0] * 5) is None  # 全并列：无信息，不算 0.000
    assert _percentile(1.0, [0.0, 1.0, 2.0]) == 0.5  # 中秩处理并列
    assert _cluster_t({"a": [0.6], "b": [0.4], "c": [0.7]}) is not None
    assert "样本不足" in _verdict(0.5, 3)
    assert "归档不采纳" in _verdict(0.5, 100)
    assert "有偏离" in _verdict(0.62, 100)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "alt.db"
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO alt_margin(trade_date, code, margin_balance) VALUES (?,?,?)",
            ("2026-01-05", "000001", 100.0),
        )
        conn.execute(
            "INSERT INTO alt_margin(trade_date, code, margin_balance) VALUES (?,?,?)",
            ("2026-01-06", "000001", 200.0),
        )
        conn.commit()
        # as_of = 01-06 时只能看到 01-05，绝不能看到当日盘后的 200
        assert all_features(conn, "000001", "2026-01-06")["margin_last"] == 100.0
        assert "trade_date < as_of" in ASOF_RULES["alt_margin"]
        conn.close()
    print("self-test OK：分位 / 聚类 t / 判定 / as_of 零前视接线")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="另类数据「资金埋伏」事件研究探针")
    ap.add_argument("--db", default=str(DEFAULT_ALT_DB))
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--max-events", type=int, default=2000)
    ap.add_argument("--controls", type=int, default=20)
    ap.add_argument("--max-codes", type=int, default=0, help="0=全部本地日K")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(OUT_FILE))
    ap.add_argument("--event-from", default="", help="只取该日期及之后的事件（对齐采集窗口）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    conn = connect(args.db)
    klines = _load_klines(Path(args.kline_dir), args.max_codes)
    if not klines:
        print(f"本地日 K 为空：{args.kline_dir}")
        return 2
    events = _build_events(klines)
    if args.event_from:
        events = [(c, d) for c, d in events if d >= args.event_from]
    rng = random.Random(args.seed)
    if len(events) > args.max_events:
        events = rng.sample(events, args.max_events)

    event_days = {d for _, d in events}
    by_day_pool: dict[str, list[str]] = defaultdict(list)
    for code, df in klines.items():
        for day in df["date"]:
            if day in event_days:
                by_day_pool[day].append(code)

    cache: dict[tuple[str, str], dict] = {}

    def feat(code: str, day: str) -> dict:
        key = (code, day)
        if key not in cache:
            cache[key] = all_features(conn, code, day)
        return cache[key]

    per_feature: dict[str, dict[str, list[float]]] = {
        f: defaultdict(list) for f in FEATURE_NAMES
    }
    covered = dict.fromkeys(FEATURE_NAMES, 0)
    for code, day in events:
        pool = [c for c in by_day_pool.get(day, []) if c != code]
        if len(pool) < args.controls:
            continue
        controls = rng.sample(pool, args.controls)
        ev, ctl = feat(code, day), [feat(c, day) for c in controls]
        for name in FEATURE_NAMES:
            pct = _percentile(ev[name], [c[name] for c in ctl])
            if pct is None:
                continue
            per_feature[name][day].append(pct)
            covered[name] += 1

    result: dict = {
        "generated_at": date.today().isoformat(),
        "event_definition": f"fwd{HORIZON} >= {THRESHOLD:.0%}（T 日买入持有 {HORIZON} 个交易日）",
        "event_from": args.event_from or None,
        "as_of_rules": ASOF_RULES,
        "universe_codes": len(klines),
        "events_sampled": len(events),
        "controls_per_event": args.controls,
        "neutral_band": NEUTRAL_BAND,
        "interpretation_note": (
            "cluster_t 检验分位是否偏离 0.5；样本大时极小的偏离也会显著，"
            "是否可交易以 mean_percentile 相对 0.5±0.05 的效应量为准。"
        ),
        "features": {},
    }
    for name in FEATURE_NAMES:
        by_day = per_feature[name]
        flat = [v for vals in by_day.values() for v in vals]
        mean = sum(flat) / len(flat) if flat else None
        result["features"][name] = {
            "coverage": covered[name] / len(events) if events else 0.0,
            "n": len(flat),
            "n_days": len(by_day),
            "mean_percentile": mean,
            "cluster_t": _cluster_t(by_day),
            "verdict": _verdict(mean, len(flat)),
        }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, row in result["features"].items():
        p = row["mean_percentile"]
        print(
            f"{name}: 覆盖 {row['coverage']:.1%} n={row['n']} "
            f"分位={p:.3f} " if p is not None else f"{name}: 无有效样本",
            row["verdict"],
        )
    print(f"产物 {out_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
