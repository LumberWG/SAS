# -*- coding: utf-8 -*-
"""大涨前 1~2 个月的「资金埋伏」痕迹探查（只研究，不看 T 日及之后）。

和 ``big_move_probe.py`` 的区别
------------------------------
短窗口版本看 T-1~T-5，并评估「T 日能不能赚」；本脚本按用户口径**只看事件日之前**，
把窗口拉到 T-40~T-1（约 2 个月），分 4 段看「越临近越明显」还是「早就埋伏」：

- 日线：换手率水平/累计、成交额相对基准放大、涨放量跌缩量、振幅、收盘位置、
  相对全市场超额、OBV 斜率、段内最大回撤、距段内高低点。
- 分钟：全天/尾盘净流入代理（分钟涨跌 × 成交额）、尾盘 30min 量占比与涨幅、
  大单分钟（成交额 top5%）的涨跌方向、VWAP 偏离、日内形态。

对照：随机 300 只（按最后交易日成交额分 10 档、每档 30 只，保证规模分布一致），
在**同一个锚点 T** 上算同样的段特征；指标按「事件 - 同档对照」的分位再对事件平均。
0.5 = 没有区别。日线对照额外限制「同日 + 同成交额档」。

产物：``data/logs/big_move_probe_long_<tag>.json`` + 控制台 markdown。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_BACKEND = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_BACKEND), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import get_settings  # noqa: E402
from app.data.minute import MINUTE1_DIR  # noqa: E402
from app.data.scope import is_cyb, is_tradable  # noqa: E402
from big_move_probe import _add_daily_features, _read_daily, _read_minute  # noqa: E402

DAILY_KEYS = [
    "turn_mean", "turn_sum", "amt_ratio", "vol_ratio", "ret_sum", "vol_std",
    "updown_vol", "amp_mean", "close_pos_mean", "above_ma60", "rel_str",
    "obv_slope", "high_pos", "max_dd",
]
MIN_KEYS = [
    "m_flow", "m_tail_flow", "m_tail_vol", "m_tail_ret", "m_first_vol",
    "m_big_buy", "m_vwap_dev", "m_dev_std", "m_up_ratio", "m_high_pos",
    "m_close_high", "m5_up_ratio", "m5_max_streak", "m5_ret_skew",
]
ALL_KEYS = DAILY_KEYS + MIN_KEYS
LABELS = {
    "turn_mean": "日均换手率",
    "turn_sum": "段内累计换手",
    "amt_ratio": "成交额/基准",
    "vol_ratio": "成交量/基准",
    "ret_sum": "段内累计涨幅",
    "vol_std": "日收益波动",
    "updown_vol": "涨跌日量比",
    "amp_mean": "日均振幅",
    "close_pos_mean": "日均收盘位置",
    "above_ma60": "站上MA60天数占比",
    "rel_str": "相对全市场超额",
    "obv_slope": "OBV斜率(归一)",
    "high_pos": "段内高低位置",
    "max_dd": "段内最大回撤",
    "m_flow": "全天净流入代理",
    "m_tail_flow": "尾盘30min净流入",
    "m_tail_vol": "尾盘30min量占比",
    "m_tail_ret": "尾盘30min涨幅",
    "m_first_vol": "前30min量占比",
    "m_big_buy": "大单分钟上涨占比",
    "m_vwap_dev": "收盘偏离VWAP",
    "m_dev_std": "日内偏离离散度",
    "m_up_ratio": "上涨分钟占比",
    "m_high_pos": "最高点位置",
    "m_close_high": "收盘距最高",
    "m5_up_ratio": "5m阳线占比",
    "m5_max_streak": "5m最长连阳",
    "m5_ret_skew": "5m收益偏度",
}
#: 单位已经是「百分点」的特征（turnover_rate 本身是 %，不能再 ×100）
PCT_POINT = {"turn_mean", "turn_sum"}
#: 倍数 / 位置 / 计数 / 偏度，按原值展示
NO_PCT = {"amt_ratio", "vol_ratio", "updown_vol", "obv_slope", "high_pos",
          "close_pos_mean", "m_high_pos", "m5_max_streak", "m5_ret_skew"}


def seg_defs(lookback: int) -> list[tuple[int, int]]:
    out = []
    lo = 1
    while lo <= lookback:
        hi = min(lo + 9, lookback)
        out.append((lo, hi))
        lo = hi + 1
    return out


def load_daily(kline_dir: Path, pad: str, end: str, log) -> dict[str, pd.DataFrame]:
    files = sorted(kline_dir.glob("*.parquet"))
    out: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    for n, path in enumerate(files, 1):
        code = path.stem
        if not (code.isdigit() and len(code) == 6):
            continue
        ok, _ = is_tradable(code, "")
        if not ok:
            continue
        df = _read_daily(path, pad, end)
        if df is None or len(df) < 90 or "pct_change" not in df.columns:
            continue
        df = _add_daily_features(df)
        close = df["close"].astype(float)
        df["d_ma60"] = close.rolling(60).mean()
        out[code] = df
        if n % 1200 == 0:
            log(f"  日K {n}/{len(files)}，已收 {len(out)} 只，{time.time() - t0:.0f}s")
    return out


def scan_events(daily, start: str, end: str, threshold: float, lookback: int,
                side: str = "up") -> list[dict]:
    need = max(lookback, 60)
    events = []
    lo = threshold * 100.0
    for code, df in daily.items():
        d = df["date"].to_numpy()
        pct = df["d_ret1"].to_numpy(dtype=float) * 100.0
        for i in range(need, len(df)):
            day = str(d[i])
            if not (start <= day <= end):
                continue
            p = pct[i]
            if not np.isfinite(p):
                continue
            if side == "up":
                if not (lo < p < 30.0):
                    continue
            elif not (-30.0 < p < -lo):
                continue
            events.append({
                "code": code, "date": day, "i": int(i), "ret": float(p / 100.0),
                "cyb": bool(is_cyb(code)),
                "prev": [str(d[i - k]) for k in range(1, lookback + 1)],
            })
    return events


def market_returns(daily) -> dict[str, float]:
    """全市场等权日收益，用于「相对强度」。"""
    acc: dict[str, list[float]] = {}
    for df in daily.values():
        for day, r in zip(df["date"].to_numpy(), df["d_ret1"].to_numpy(dtype=float)):
            if np.isfinite(r):
                acc.setdefault(str(day), []).append(float(r))
    return {d: float(np.mean(v)) for d, v in acc.items()}


def _nmean(x) -> float:
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else float("nan")


def _nstd(x) -> float:
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.std()) if a.size else float("nan")


def seg_daily(df: pd.DataFrame, i: int, lo: int, hi: int,
              base_amt: float, base_vol: float, mkt: dict[str, float]) -> dict:
    a, b = i - hi, i - lo + 1
    s = df.iloc[a:b]
    if len(s) < 3:
        return {}
    ret = s["d_ret1"].to_numpy(dtype=float)
    vol = s["volume"].to_numpy(dtype=float)
    amt = s["amount"].to_numpy(dtype=float)
    turn = s["d_turnover"].to_numpy(dtype=float)
    close = s["close"].to_numpy(dtype=float)
    high = s["high"].to_numpy(dtype=float)
    low = s["low"].to_numpy(dtype=float)
    up, down = ret > 0, ret < 0
    up_vol = float(vol[up].mean()) if up.any() else np.nan
    down_vol = float(vol[down].mean()) if down.any() else np.nan
    cum = np.cumprod(1.0 + np.nan_to_num(ret))
    peak = np.maximum.accumulate(cum)
    dd = float((cum / peak - 1.0).min()) if len(cum) else np.nan
    dates = s["date"].astype(str).to_numpy()
    mkt_sum = float(sum(mkt.get(x, 0.0) for x in dates))
    obv = np.cumsum(np.sign(ret) * vol)
    slope = float(np.polyfit(np.arange(len(obv)), obv, 1)[0] / (vol.mean() + 1e-9)) \
        if len(obv) > 2 and vol.mean() > 0 else np.nan
    hi_p, lo_p = float(high.max()), float(low.min())
    return {
        "turn_mean": _nmean(turn),
        "turn_sum": float(np.nansum(turn)) if np.isfinite(turn).any() else np.nan,
        "amt_ratio": float(_nmean(amt) / base_amt) if base_amt else np.nan,
        "vol_ratio": float(_nmean(vol) / base_vol) if base_vol else np.nan,
        "ret_sum": float(cum[-1] - 1.0) if len(cum) else np.nan,
        "vol_std": _nstd(ret),
        "updown_vol": float(up_vol / down_vol) if down_vol and np.isfinite(up_vol) else np.nan,
        "amp_mean": _nmean(s["d_amp"]),
        "close_pos_mean": _nmean(s["d_close_pos"]),
        "above_ma60": _nmean((close > s["d_ma60"].to_numpy(dtype=float)).astype(float)),
        "rel_str": float((cum[-1] - 1.0) - mkt_sum) if len(cum) else np.nan,
        "obv_slope": slope,
        "high_pos": float((close[-1] - lo_p) / (hi_p - lo_p)) if hi_p > lo_p else np.nan,
        "max_dd": dd,
    }


def min1_day(g: pd.DataFrame) -> dict | None:
    """单日 1 分钟 → 资金流/尾盘/大单代理 + 5m 聚合特征。"""
    if len(g) < 60:
        return None
    c = g["close"].to_numpy(dtype=float)
    o = g["open"].to_numpy(dtype=float)
    h = g["high"].to_numpy(dtype=float)
    v = g["volume"].to_numpy(dtype=float)
    a = g["amount"].to_numpy(dtype=float)
    n = len(c)
    vs, asum = float(v.sum()), float(a.sum())
    if vs <= 0 or asum <= 0:
        return None
    r = np.diff(c) / c[:-1]
    flow = a[1:] * np.sign(r)
    cv, ca = np.cumsum(v), np.cumsum(a)
    vwap_t = np.divide(ca, cv, out=np.full_like(ca, np.nan), where=cv > 0)
    dev = c / vwap_t - 1.0
    big = a >= np.quantile(a, 0.95)
    big_dir = r[big[1:]] if big[1:].any() else np.array([])
    # 5m 聚合：每 5 根取最后一根收盘 / 第一根开盘
    c5, o5 = c[4::5], o[0::5]
    m = min(len(c5), len(o5))
    c5, o5 = c5[:m], o5[:m]
    r5 = np.diff(c5) / c5[:-1] if m > 1 else np.array([0.0])
    streak = cur = 0
    for up in c5 > o5:
        cur = cur + 1 if up else 0
        streak = max(streak, cur)
    mid = 120 if n >= 200 else n // 2
    return {
        "m_flow": float(flow.sum() / asum),
        "m_tail_flow": float(flow[-30:].sum() / a[-30:].sum()) if a[-30:].sum() > 0 else np.nan,
        "m_tail_vol": float(v[-30:].sum() / vs),
        "m_tail_ret": float(c[-1] / c[-31] - 1.0) if n > 31 else np.nan,
        "m_first_vol": float(v[:30].sum() / vs),
        "m_big_buy": float((big_dir > 0).mean()) if big_dir.size else np.nan,
        "m_vwap_dev": float(c[-1] / (ca[-1] / cv[-1]) - 1.0) if cv[-1] > 0 else np.nan,
        "m_dev_std": float(np.nanstd(dev)),
        "m_up_ratio": float((r > 0).mean()),
        "m_high_pos": float(np.argmax(h) / max(n - 1, 1)),
        "m_close_high": float(c[-1] / h.max() - 1.0) if h.max() > 0 else np.nan,
        "m5_up_ratio": float((c5 > o5).mean()) if m else np.nan,
        "m5_max_streak": float(streak),
        "m5_ret_skew": float(pd.Series(r5).skew()) if len(r5) > 3 else np.nan,
    }


def agg_min(vals: list[dict | None]) -> dict:
    vals = [x for x in vals if x]
    if not vals:
        return {}
    return {
        k: float(np.nanmean([x[k] for x in vals if k in x and np.isfinite(x[k])]))
        if any(k in x and np.isfinite(x[k]) for x in vals) else np.nan
        for k in MIN_KEYS
    }


def load_min_days(code: str, lo: str, hi: str) -> dict[str, dict]:
    days = _read_minute(MINUTE1_DIR / f"{code}.parquet", lo, hi)
    if not days:
        return {}
    out = {}
    for day, g in days.items():
        f = min1_day(g)
        if f:
            out[day] = f
    return out


def _median(vals) -> float:
    a = np.asarray([x for x in vals if x is not None and np.isfinite(x)], dtype=float)
    return float(np.median(a)) if a.size else float("nan")


def _fmt(key: str, v: float) -> str:
    if not np.isfinite(v):
        return "—"
    if key in PCT_POINT:
        return f"{v:.2f}%"
    if key in NO_PCT:
        return f"{v:.3f}"
    return f"{v * 100:.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="大涨前 1~2 个月资金埋伏痕迹（纯研究）")
    ap.add_argument("--start", default="", help="事件起始日（默认 end-30 天）")
    ap.add_argument("--end", default="", help="事件结束日（默认日K最新）")
    ap.add_argument("--threshold", type=float, default=0.08)
    ap.add_argument("--side", choices=["up", "down"], default="up", help="up=单日大涨，down=单日大跌")
    ap.add_argument("--lookback", type=int, default=40, help="回看交易日数（默认 40≈2 个月）")
    ap.add_argument("--control", type=int, default=300, help="对照股数量")
    ap.add_argument("--max-codes", type=int, default=0, help="冒烟：只跑前 N 只事件股")
    ap.add_argument("--tag", default="long")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    kline_dir = Path(get_settings().kline_dir)
    end = args.end or str(
        pd.read_parquet(kline_dir / "600000.parquet", columns=["date"])["date"].iloc[-1]
    )
    start = args.start or str(pd.Timestamp(end) - pd.Timedelta(days=30))
    pad = str(pd.Timestamp(start) - pd.Timedelta(days=320))

    def log(m: str) -> None:
        print(m, flush=True)

    log(f"[1/5] 读日K：{pad} ~ {end}")
    daily = load_daily(kline_dir, pad, end, log)
    log(f"      候选 {len(daily)} 只")

    log("[2/5] 扫描事件")
    events = scan_events(daily, start, end, args.threshold, args.lookback, args.side)
    if args.max_codes:
        keep = {e["code"] for e in events[: args.max_codes]}
        events = [e for e in events if e["code"] in keep]
    codes = sorted({e["code"] for e in events})
    log(f"      事件 {len(events)} 个 / {len(codes)} 只")

    mkt = market_returns(daily)
    segs = seg_defs(args.lookback)
    seg_labels = [f"T-{hi}~T-{lo}" for lo, hi in segs]
    log(f"      分段：{seg_labels}")

    lo_day = min(e["prev"][-1] for e in events)
    hi_day = max(e["prev"][0] for e in events)

    log(f"[3/5] 读事件股 1m（{lo_day} ~ {hi_day}），5m 特征由 1m 聚合")
    min_feat: dict[str, dict[str, dict]] = {}
    t0 = time.time()
    for n, code in enumerate(codes, 1):
        min_feat[code] = load_min_days(code, lo_day, hi_day)
        if n % 200 == 0:
            log(f"      事件分钟 {n}/{len(codes)}，{time.time() - t0:.0f}s")

    # 对照：按最后交易日成交额分 10 档、每档等量抽，保证规模分布一致
    ev_codes = set(codes)
    last_day = max(mkt)
    pool = []
    for code, df in daily.items():
        if code in ev_codes:
            continue
        row = df[df["date"].astype(str) == last_day]
        if len(row) and np.isfinite(float(row["amount"].iloc[0])):
            pool.append((code, float(row["amount"].iloc[0])))
    pool.sort(key=lambda x: x[1])
    per = max(1, args.control // 10)
    ctrl_codes = []
    step = max(1, len(pool) // 10)
    for k in range(10):
        chunk = pool[k * step:(k + 1) * step] if k < 9 else pool[9 * step:]
        ctrl_codes.extend(c for c, _ in chunk[:per])
    log(f"[3/5] 对照 {len(ctrl_codes)} 只（10 档 × {per}）")

    ctrl_min: dict[str, dict[str, dict]] = {}
    for n, code in enumerate(ctrl_codes, 1):
        ctrl_min[code] = load_min_days(code, lo_day, hi_day)
        if n % 100 == 0:
            log(f"      对照分钟 {n}/{len(ctrl_codes)}，{time.time() - t0:.0f}s")

    # 事件段特征
    log("[4/5] 汇总事件段特征")
    ev_rows: list[dict[str, dict]] = []
    for e in events:
        df = daily[e["code"]]
        i = e["i"]
        base_amt = float(df["amount"].iloc[max(0, i - 120):max(1, i - 40)].mean())
        base_vol = float(df["volume"].iloc[max(0, i - 120):max(1, i - 40)].mean())
        row = {}
        for (lo, hi), label in zip(segs, seg_labels):
            d = seg_daily(df, i, lo, hi, base_amt, base_vol, mkt)
            if d:
                d.update(agg_min([min_feat[e["code"]].get(x) for x in e["prev"][lo - 1:hi]]))
            row[label] = d
        ev_rows.append(row)

    # 对照段特征（同一锚点 T）
    anchors = sorted({e["date"] for e in events})
    log(f"[4/5] 汇总对照段特征：{len(anchors)} 个锚点 × {len(ctrl_codes)} 只")
    ctrl_seg: dict[str, dict[str, dict[str, dict]]] = {}
    t0 = time.time()
    for n, code in enumerate(ctrl_codes, 1):
        df = daily.get(code)
        if df is None:
            continue
        dts = df["date"].astype(str).to_numpy()
        idx = {str(v): j for j, v in enumerate(dts)}
        per_anchor = {}
        for anchor in anchors:
            j = idx.get(anchor)
            if j is None or j < max(args.lookback, 60):
                continue
            i = j
            base_amt = float(df["amount"].iloc[max(0, i - 120):max(1, i - 40)].mean())
            base_vol = float(df["volume"].iloc[max(0, i - 120):max(1, i - 40)].mean())
            prev = [str(dts[i - k]) for k in range(1, args.lookback + 1)]
            row = {}
            for (lo, hi), label in zip(segs, seg_labels):
                d = seg_daily(df, i, lo, hi, base_amt, base_vol, mkt)
                if d:
                    d.update(agg_min([ctrl_min.get(code, {}).get(x) for x in prev[lo - 1:hi]]))
                row[label] = d
            per_anchor[anchor] = row
        ctrl_seg[code] = per_anchor
        if n % 100 == 0:
            log(f"      对照段 {n}/{len(ctrl_codes)}，{time.time() - t0:.0f}s")

    # 成交额十分位：按锚点 T-1 日的全市场成交额分 10 档，做同档对照
    need_days = {e["prev"][0] for e in events}
    amt_map: dict[str, dict[str, float]] = {}
    for code, df in daily.items():
        m = {}
        for d, a in zip(df["date"].astype(str).to_numpy(), df["amount"].to_numpy(dtype=float)):
            if d in need_days and np.isfinite(a):
                m[d] = float(a)
        if m:
            amt_map[code] = m
    edges: dict[str, np.ndarray] = {}
    for day in need_days:
        vals = [m[day] for m in amt_map.values() if day in m]
        if len(vals) >= 100:
            edges[day] = np.quantile(np.asarray(vals, dtype=float), np.linspace(0.1, 0.9, 9))

    def dec_of(day: str, amount: float) -> int:
        e = edges.get(day)
        if e is None or not np.isfinite(amount):
            return -1
        return int(np.searchsorted(e, amount, side="right"))

    # 对照按 (锚点, 同档) 预分组，统计时只遍历同档，避免 2066×4×300 次查找
    anchor_prev = {e["date"]: e["prev"][0] for e in events}
    ctrl_pool: dict[tuple[str, int], list[str]] = {}
    for c in ctrl_codes:
        for anchor in ctrl_seg.get(c, {}):
            day = anchor_prev.get(anchor)
            if not day:
                continue
            dec = dec_of(day, amt_map.get(c, {}).get(day, np.nan))
            ctrl_pool.setdefault((anchor, dec), []).append(c)

    log("[5/5] 统计")
    table: dict[str, dict] = {}
    for key in ALL_KEYS:
        per_seg = {}
        for label in seg_labels:
            ev_vals, ranks = [], []
            for e, row in zip(events, ev_rows):
                v = row[label].get(key, np.nan)
                ev_vals.append(v)
                day = e["prev"][0]
                dec = dec_of(day, amt_map.get(e["code"], {}).get(day, np.nan))
                ctrl_vals = [
                    ctrl_seg[c][e["date"]][label].get(key, np.nan)
                    for c in ctrl_pool.get((e["date"], dec), [])
                ]
                arr = np.sort(np.asarray(
                    [x for x in ctrl_vals if np.isfinite(x)], dtype=float
                ))
                if np.isfinite(v) and arr.size >= 8:
                    l = np.searchsorted(arr, v, side="left")
                    r = np.searchsorted(arr, v, side="right")
                    ranks.append((l + r) / (2.0 * arr.size))
            per_seg[label] = {
                "event_median": _median(ev_vals),
                "pct": float(np.mean(ranks)) if ranks else float("nan"),
                "n_event": int(np.isfinite(np.asarray(ev_vals, dtype=float)).sum()),
            }
        pcts = [v["pct"] for v in per_seg.values() if np.isfinite(v["pct"])]
        table[key] = {
            "label": LABELS.get(key, key),
            "segs": per_seg,
            "pct_mean": float(np.mean(pcts)) if pcts else float("nan"),
        }

    ranked = sorted(
        (k for k in ALL_KEYS if np.isfinite(table[k]["pct_mean"])),
        key=lambda k: abs(table[k]["pct_mean"] - 0.5), reverse=True,
    )

    # 输出
    side_cn = "大涨" if args.side == "up" else "大跌"
    move_cn = "涨幅" if args.side == "up" else "跌幅"
    flow_cn = "埋伏" if args.side == "up" else "出逃"
    print(f"\n## {side_cn}前 1~2 个月「资金{flow_cn}」痕迹\n")
    by_day = pd.Series([e["date"] for e in events]).value_counts().sort_index()
    print(f"- 窗口：{start} ~ {end}；单日{move_cn} > {args.threshold:.0%} 事件 "
          f"{len(events)} 个 / {len(codes)} 只 / {len(by_day)} 天")
    print(f"- 回看 {args.lookback} 个交易日，分段 {seg_labels}")
    print(f"- 对照 {len(ctrl_codes)} 只（按成交额 10 档等量抽），指标 = 事件在同锚点同档对照中的分位（0.5=无区别）")
    head = "| 特征 | " + " | ".join(seg_labels) + " | 分位均值 |"
    print("\n" + head)
    print("| --- | " + " | ".join("---" for _ in seg_labels) + " | --- |")
    for key in ranked:
        row = table[key]
        cells = [_fmt(key, row["segs"][l]["event_median"]) for l in seg_labels]
        print(f"| {row['label']} | " + " | ".join(cells)
              + f" | {row['pct_mean']:.3f} |")

    print("\n### 分位演化（|分位-0.5| 从大到小）\n")
    for key in ranked[:15]:
        row = table[key]
        cells = [f"{row['segs'][l]['pct']:.3f}" for l in seg_labels]
        early = row["segs"][seg_labels[0]]["pct"]
        late = row["segs"][seg_labels[-1]]["pct"]
        trend = "越早越强(提前埋伏)" if abs(late - 0.5) > abs(early - 0.5) + 0.01 else (
            "越临近越强" if abs(early - 0.5) > abs(late - 0.5) + 0.01 else "平稳"
        )
        print(f"- **{row['label']}**（{key}）：{' → '.join(cells)}，{trend}")

    out_dir = Path(get_settings().db_path).parent / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"big_move_probe_long_{args.tag}.json"
    out.write_text(json.dumps({
        "meta": {
            "start": start, "end": end, "lookback": args.lookback, "side": args.side,
            "threshold": args.threshold, "control": len(ctrl_codes),
            "segments": seg_labels, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "events": len(events), "stocks": len(codes),
        },
        "table": table, "ranked": ranked, "events": events[:2000],
    }, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\n产物：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
