"""#151 买卖路径归因：买入后仍在跌，还是卖出时机不对（事后诊断）。

只读本地日线与 #151 回测产物 JSON，产物落 ``data/logs``；零生产写入。
**含未来数据**：买入 / 卖出后 N 日、反事实持有到窗口末都是事后观察，
仅用于归因，不构成可交易信号，也不改任何线上规则。

指标口径（全部以百分数表示）：

- ``buy_day_ret`` / ``mae`` / ``mfe``：买入日收盘、持仓期最低 low、最高 high 相对买入价。
- ``hold_N``：买入后第 N 个交易日收盘相对**买入价**，不受出场规则影响 → 回答「买入后是否还在跌」。
- ``fwd_N``：卖出后第 N 个交易日收盘相对**卖出价** → 回答「是否卖早了」。
- ``to_end``：卖出价 → 窗口末收盘；``market_to_end``：同期上证指数；
  ``excess_if_held = to_end - market_to_end``，用来剔掉 beta 反弹，判断个股是否真被卖错。
- ``counterfactual``：买入价 → 窗口末收盘；``cf_minus_actual`` 是它与实际单笔盈亏之差。
  反事实忽略资金占用与最多 10 只的约束，**不是可交易策略**，只作路径对照。
- ``hold_N`` / ``fwd_N`` 允许越过窗口末（事后观察），``*_to_end`` / ``counterfactual`` 严格停在窗口末。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\trade_path_attribution.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from statistics import mean

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DATA_DIR  # noqa: E402

KLINES = DATA_DIR / "klines"
LOGS = DATA_DIR / "logs"
HORIZONS = (1, 3, 5, 10)
TAGS = ("2026-11-noamp3", "2026-11-noamp3-oos")
NUM_KEYS = (
    "pnl_pct", "buy_day_ret", "mae", "mfe", "counterfactual", "cf_minus_actual",
    "to_end", "market_to_end", "excess_if_held",
    *(f"hold_{k}" for k in HORIZONS),
    *(f"fwd_{k}" for k in HORIZONS),
)
POS_KEYS = ("buy_day_ret", "hold_5", "fwd_5", "to_end", "excess_if_held", "cf_minus_actual")
PROTOCOL = {
    "scope": "BACKTEST_LOG #151 两轮共 40 笔平仓（三条件低位选股，已去掉 c4）",
    "data": "本地日线 K 线 + 上证指数收盘（idx_sh000001），T+1 开盘价来自回测产物",
    "lookahead": "含未来数据，仅事后归因；不是策略信号，不参与任何线上判定",
    "hold_N": "买入后第 N 个交易日收盘 / 买入价 - 1；不受出场规则影响",
    "fwd_N": "卖出后第 N 个交易日收盘 / 卖出价 - 1",
    "to_end": "窗口末收盘 / 卖出价 - 1（停在窗口末）",
    "market_to_end": "上证指数窗口末 / 卖出日 - 1，用于剔除 beta",
    "excess_if_held": "to_end - market_to_end；>0 才说明个股相对市场被卖早",
    "counterfactual": "窗口末收盘 / 买入价 - 1；忽略资金占用与最多 10 只约束，不是可交易策略",
    "caveat": "单窗口 22 交易日、40 笔、零滑点、日线边界；只作方向性归因，不作显著性检验",
}


def _calendar() -> list[str]:
    idx = pd.read_parquet(KLINES / "idx_sh000001.parquet")
    return [str(d)[:10] for d in idx["date"]]


def _index_close() -> pd.Series:
    idx = pd.read_parquet(KLINES / "idx_sh000001.parquet")
    idx["date"] = idx["date"].astype(str).str[:10]
    return idx.set_index("date")["close"].astype(float)


def _reason_key(reason: str) -> str:
    if reason.startswith("时间止损"):
        return "time"
    if reason == "止损":
        return "stop"
    if reason == "止盈":
        return "take"
    return "other"


def _scalar(df: pd.DataFrame, day: str, col: str = "close") -> float | None:
    if day not in df.index:
        return None
    v = df.loc[day, col]
    c = float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
    return c if pd.notna(c) else None


def _at(df: pd.DataFrame, days: list[str], day_no: dict, anchor: str, k: int, base: float) -> float | None:
    i = day_no.get(anchor)
    if i is None or i + k >= len(days) or base <= 0:
        return None
    c = _scalar(df, days[i + k])
    return round((c / base - 1) * 100, 2) if c is not None else None


def _last_close(df: pd.DataFrame, days_upto_end: list[str]) -> float | None:
    for d in reversed(days_upto_end):
        c = _scalar(df, d)
        if c is not None:
            return c
    return None


def _one_trade(
    trade: dict, tag: str, days: list[str], day_no: dict,
    days_upto_end: list[str], idx_close: pd.Series, end: str,
) -> dict:
    base_row = {"tag": tag, "code": trade["code"], "reason": trade["reason"]}
    path = KLINES / f"{trade['code']}.parquet"
    if not path.exists():
        return {**base_row, "error": "missing_kline"}
    df = pd.read_parquet(path)
    df["date"] = df["date"].astype(str).str[:10]
    df = df.set_index("date").sort_index()
    for c in ("close", "high", "low"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    buy, sell = trade["buy_date"], trade["sell_date"]
    px, sell_px = float(trade["buy_price"]), float(trade["sell_price"])
    if buy not in df.index or px <= 0 or sell_px <= 0:
        return {**base_row, "error": "missing_buy_bar"}

    held = df.loc[(df.index >= buy) & (df.index <= sell)]
    mae = round((float(held["low"].min()) / px - 1) * 100, 2) if not held.empty else None
    mfe = round((float(held["high"].max()) / px - 1) * 100, 2) if not held.empty else None
    last = _last_close(df, days_upto_end)
    to_end = round((last / sell_px - 1) * 100, 2) if last else None
    cf = round((last / px - 1) * 100, 2) if last else None
    s0 = _scalar_idx(idx_close, sell)
    s1 = _scalar_idx(idx_close, end)
    mkt = round((s1 / s0 - 1) * 100, 2) if s0 and s1 else None
    pnl = float(trade["pnl_pct"])

    row = {
        **base_row,
        "reason_key": _reason_key(trade["reason"]),
        "buy_date": buy, "sell_date": sell,
        "holding_days": day_no.get(sell, 0) - day_no.get(buy, 0) + 1,
        "pnl_pct": pnl,
        "buy_day_ret": _at(df, days, day_no, buy, 0, px),
        "mae": mae, "mfe": mfe,
        "to_end": to_end, "market_to_end": mkt,
        "excess_if_held": round(to_end - mkt, 2) if to_end is not None and mkt is not None else None,
        "counterfactual": cf,
        "cf_minus_actual": round(cf - pnl, 2) if cf is not None else None,
    }
    for k in HORIZONS:
        row[f"hold_{k}"] = _at(df, days, day_no, buy, k, px)
        row[f"fwd_{k}"] = _at(df, days, day_no, sell, k, sell_px)
    return row


def _scalar_idx(series: pd.Series, day: str) -> float | None:
    if day not in series.index:
        return None
    v = series.loc[day]
    c = float(v.iloc[0]) if isinstance(v, pd.Series) else float(v)
    return c if pd.notna(c) and c > 0 else None


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 2) if values else None


def _pos_ratio(values: list[float]) -> float | None:
    return round(100 * sum(v > 0 for v in values) / len(values), 1) if values else None


def _summarize(rows: list[dict]) -> dict:
    out: dict = {
        "n": len(rows),
        "reason_counts": dict(Counter(r["reason_key"] for r in rows)),
    }
    for key in NUM_KEYS:
        vals = [r[key] for r in rows if r.get(key) is not None]
        out[f"avg_{key}"] = _avg(vals)
        if key in POS_KEYS:
            out[f"pos_{key}_pct"] = _pos_ratio(vals)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", default=list(TAGS), help="回测产物 tag 列表")
    ap.add_argument("--prefix", default="low_position_probe_", help="回测产物文件名前缀")
    ap.add_argument("--out", default="trade_path_attribution_2026-11.json")
    ap.add_argument("--scope", default=None, help="覆盖产物 protocol.scope")
    args = ap.parse_args()

    days = _calendar()
    day_no = {d: i for i, d in enumerate(days)}
    idx_close = _index_close()
    pooled: list[dict] = []
    errors: list[dict] = []
    by_tag: dict[str, dict] = {}
    meta: dict = {}

    for tag in args.tags:
        payload = json.loads((LOGS / f"{args.prefix}{tag}.json").read_text(encoding="utf-8"))
        start, end = payload["conditions"]["start"], payload["conditions"]["end"]
        days_upto_end = [d for d in days if d <= end]
        meta[tag] = {"start": start, "end": end, "benchmark_return": payload["metrics"]["benchmark_return"]}
        rows = [
            _one_trade(t, tag, days, day_no, days_upto_end, idx_close, end)
            for t in payload["trades"]
        ]
        ok = [r for r in rows if "error" not in r]
        errors.extend(r for r in rows if "error" in r)
        reason_groups: dict[str, list[dict]] = defaultdict(list)
        for r in ok:
            reason_groups[r["reason_key"]].append(r)
        by_tag[tag] = {
            "window": [start, end],
            "summary": _summarize(ok),
            "by_reason": {k: _summarize(v) for k, v in sorted(reason_groups.items())},
            "trades": rows,
        }
        pooled.extend(ok)

    reason_groups = defaultdict(list)
    for r in pooled:
        reason_groups[r["reason_key"]].append(r)
    proto = dict(PROTOCOL)
    if args.scope:
        proto["scope"] = args.scope
    out = {
        "protocol": proto,
        "meta": meta,
        "by_tag": by_tag,
        "pooled": {
            "summary": _summarize(pooled),
            "by_reason": {k: _summarize(v) for k, v in sorted(reason_groups.items())},
        },
        "errors": errors,
    }
    path = LOGS / args.out
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {
        "out": str(path),
        "errors": len(errors),
        "by_tag": {k: v["summary"] for k, v in by_tag.items()},
        "pooled": out["pooled"]["summary"],
        "pooled_by_reason": out["pooled"]["by_reason"],
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
