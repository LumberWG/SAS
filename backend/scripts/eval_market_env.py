"""隔夜市场环境的离线增量价值评估（影子研究，只读、不进决策）。

被解释变量：A 股交易日 T 的**中证全指当日收益**（T 收盘才实现）。
解释变量：T 凌晨生成的 market_env 快照（严格早于 09:30，零未来函数）。
对照变量：T-1 的 A 股自身收益——若环境分的解释力只是 A 股动量的镜像，
就不构成「隔夜增量信息」的证据。

⚠ 本脚本只产出研究报告，**不改任何策略参数、不写 app_state、不碰买卖链路**，
   因此不会作废正在使用的前向验证基线。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.data.cache import db  # noqa: E402

REPORT_PATH = Path(__file__).resolve().parent / "market_env_eval.json"


def load_bench() -> pd.DataFrame:
    """中证全指日线 → 当日收益 / 开盘跳空 / 开盘后收益（index=交易日）。

    拆开「跳空」与「开盘后」很关键：凌晨隔夜信息若只体现在 09:25 集合竞价
    的跳空里，那对 09:30 之后才下单的实盘链路就没有可交易价值。
    """
    p = Path(get_settings().kline_dir) / "idx_sh000985.parquet"
    df = pd.read_parquet(p, columns=["date", "open", "close"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = (df.dropna().sort_values("date")
          .drop_duplicates("date", keep="last").set_index("date"))
    prev = df["close"].shift(1)
    out = pd.DataFrame(index=df.index)
    out["bench_ret"] = (df["close"] / prev - 1.0) * 100.0
    out["bench_gap"] = (df["open"] / prev - 1.0) * 100.0
    out["bench_intraday"] = (df["close"] / df["open"] - 1.0) * 100.0
    return out


def _ic(a: pd.Series, b: pd.Series) -> float | None:
    pair = pd.concat([a, b], axis=1).dropna()
    if len(pair) < 20 or pair.iloc[:, 0].nunique() < 3:
        return None
    v = pair.iloc[:, 0].corr(pair.iloc[:, 1], method="spearman")
    return None if pd.isna(v) else round(float(v), 4)


def evaluate(start: str, end: str) -> dict:
    rows = db.market_env_rows(start, end)
    if not rows:
        return {"error": "market_env 无数据，请先跑 fetch_market_env.py --backfill"}
    env = pd.DataFrame(rows).set_index("trade_date")
    df = pd.DataFrame(index=env.index)
    for col in ("env_score", "us_risk_score", "policy_score", "spx_ret", "ndx_ret", "dji_ret"):
        df[col] = pd.to_numeric(env.get(col), errors="coerce")
    bench = load_bench().reindex(df.index)
    for col in ("bench_ret", "bench_gap", "bench_intraday"):
        df[col] = bench[col]
    df["prev_ret"] = df["bench_ret"].shift(1)
    valid = df.dropna(subset=["bench_ret", "env_score"]).copy()

    ics = {
        "env_score": _ic(valid["env_score"], valid["bench_ret"]),
        "us_risk_score": _ic(valid["us_risk_score"], valid["bench_ret"]),
        "spx_ret": _ic(valid["spx_ret"], valid["bench_ret"]),
        "a_share_prev_ret(对照)": _ic(valid["prev_ret"], valid["bench_ret"]),
        "policy_score": _ic(valid["policy_score"], valid["bench_ret"]),
        "env->gap(开盘跳空)": _ic(valid["env_score"], valid["bench_gap"]),
        "env->intraday(开盘后)": _ic(valid["env_score"], valid["bench_intraday"]),
    }
    gap_ic = ics["env->gap(开盘跳空)"]
    intr_ic = ics["env->intraday(开盘后)"]
    if gap_ic is not None and intr_ic is not None and abs(gap_ic) > abs(intr_ic) * 2:
        transmission = "主要在开盘跳空：对 09:30 后下单的实盘链路不可直接利用"
    elif intr_ic is not None and abs(intr_ic) >= 0.05:
        transmission = "开盘后仍有延续，具备进一步研究的价值"
    else:
        transmission = "开盘后无明显延续，信号偏弱"

    # 三档分层：只看收益均值是否随环境分单调上升
    try:
        valid["bucket"] = pd.qcut(valid["env_score"], 3,
                                  labels=["risk_off", "neutral", "risk_on"],
                                  duplicates="drop")
        g = valid.groupby("bucket", observed=True).agg(
            n=("bench_ret", "size"),
            mean_ret=("bench_ret", "mean"),
            median_ret=("bench_ret", "median"),
            win_rate=("bench_ret", lambda s: float((s > 0).mean()) * 100.0),
            mean_env=("env_score", "mean"),
        )
        buckets = [
            {"bucket": str(k),
             "n": int(r["n"]),
             "mean_ret": round(float(r["mean_ret"]), 4),
             "median_ret": round(float(r["median_ret"]), 4),
             "win_rate": round(float(r["win_rate"]), 2),
             "mean_env": round(float(r["mean_env"]), 4)}
            for k, r in g.iterrows()
        ]
        means = [b["mean_ret"] for b in buckets]
        monotone = bool(len(means) >= 3 and all(means[i] <= means[i + 1]
                                               for i in range(len(means) - 1)))
    except (ValueError, TypeError):
        buckets, monotone = [], False

    main_ic = ics["env_score"]
    has_signal = main_ic is not None and abs(main_ic) >= 0.05 and monotone
    actionable = has_signal and transmission.startswith("开盘后")
    report = {
        "purpose": "评估『凌晨隔夜市场环境』对当日 A 股收益是否有增量预测力（影子研究）",
        "window": "%s ~ %s" % (start, end),
        "sample_days": int(len(valid)),
        "policy_days": int(valid["policy_score"].notna().sum()),
        "ics": ics,
        "buckets": buckets,
        "monotone": monotone,
        "transmission": transmission,
        "actionable_after_open": actionable,
        "verdict": (
            "有初步信号且开盘后延续，仍需在样本外与个股横截面复核后再谈接入"
            if actionable else
            "有统计信号但主要在开盘跳空/不可交易，建议维持影子研究、不接入决策"
            if has_signal else
            "无证据支持接入：IC 偏弱或分档不单调，建议维持影子研究"),
        "caveats": [
            "政策/快讯历史客观不可得，policy_score 仅近期有值；历史段一致退化为 NULL。",
            "本评估只用中证全指当日收益，未检验对个股/创业板横截面收益的预测力。",
            "信号若集中在开盘跳空，实盘 09:30 后买入无法利用（开盘价已消化）。",
            "结论不构成策略参数，任何接入都必须回测↔实盘 1:1 同步并重建前向验证基线。",
        ],
        "baseline_untouched": True,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="隔夜环境离线 IC / 分层评估（只读）")
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default=date.today().isoformat())
    args = ap.parse_args()
    rep = evaluate(args.start, args.end)
    print(json.dumps(rep, ensure_ascii=False, indent=2), flush=True)
    print("\n报告已写入 %s" % REPORT_PATH, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
