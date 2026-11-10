# -*- coding: utf-8 -*-
"""大涨前 1~5 个交易日的盘面特征探查（纯研究，不接实盘、不改任何线上参数）。

问题
----
近一个月单日涨幅 > 8% 的票，在启动前 1~5 个交易日，日线 / 1 分钟 / 5 分钟上
有没有共同的盘面特征？这些特征能不能区分「即将大涨」和「同期普通票」？

口径
----
- 事件：日 K ``pct_change > threshold``（缓存是前复权价，涨跌幅口径连续），窗口内
  每个交易日算一次事件；同一只票一个月内可以多次触发。
- 回看：事件日往前 ``--lookback`` 个交易日，只取事件日之前的数据，完全不碰事件日
  及之后的行情（不存在未来函数问题）。
- 对照：日线特征用**同日全市场非事件股**（事件股在 T-k 日也是普通票，一并留在对照里，
  偏差可忽略）；分钟特征用固定随机抽样的 ``--control`` 只（1 分钟库一只约 5MB，
  全市场不现实），报告里所有分钟结论都标注为抽样对照。
- 范围：``app.data.scope.is_tradable``（沪深主板 + 创业板，剔科创/北交/巨无霸）。

产物
----
``data/logs/big_move_probe_<tag>.json`` + 控制台 markdown 摘要。
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
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config import get_settings  # noqa: E402
from app.data.minute import MINUTE1_DIR, MINUTE5_DIR  # noqa: E402
from app.data.scope import is_cyb, is_tradable  # noqa: E402

DAILY_COLS = [
    "date", "open", "close", "high", "low",
    "volume", "amount", "pct_change", "turnover_rate",
]
MIN_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

#: 特征名 → 中文短标签（报告表头用）
LABELS = {
    "ret1": "当日涨幅",
    "gap": "跳空",
    "amp": "振幅",
    "close_pos": "收盘位置",
    "vol_ratio": "量比(vs前5日)",
    "ret5": "前5日累计",
    "ma5_dev": "离MA5",
    "ma20_dev": "离MA20",
    "up5": "前5日涨天数",
    "turnover": "换手率",
    "i_vol": "1m波动率",
    "i_vwap_dev": "收盘偏离VWAP",
    "i_dev_std": "日内偏离离散度",
    "i_above_vwap": "VWAP上方时间占比",
    "i_open30_ret": "前30min涨幅",
    "i_last30_ret": "尾30min涨幅",
    "i_last30_vol": "尾30min量占比",
    "i_first30_vol": "前30min量占比",
    "i_high_pos": "最高点位置",
    "i_close_high": "收盘距最高",
    "i_up_ratio": "上涨分钟占比",
    "i_max_amt_share": "最大分钟额占比",
    "i_pm_ret": "午后涨幅",
    "m_up_ratio": "5m阳线占比",
    "m_open3_ret": "前15min(5m)涨幅",
    "m_max_streak": "最长连阳(5m)",
    "m_ret_skew": "5m收益偏度",
}
DAILY_FEATS = [
    "ret1", "gap", "amp", "close_pos", "vol_ratio", "ret5",
    "ma5_dev", "ma20_dev", "up5", "turnover",
]
MIN1_FEATS = [
    "i_vol", "i_vwap_dev", "i_dev_std", "i_above_vwap", "i_open30_ret",
    "i_last30_ret", "i_last30_vol", "i_first30_vol", "i_high_pos",
    "i_close_high", "i_up_ratio", "i_max_amt_share", "i_pm_ret",
]
MIN5_FEATS = [
    "m_up_ratio", "m_open3_ret", "m_max_streak", "m_ret_skew",
]
ALL_FEATS = DAILY_FEATS + MIN1_FEATS + MIN5_FEATS


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------
def _read_daily(path: Path, pad: str, end: str) -> pd.DataFrame | None:
    """读单只日 K（只留 pad~end，前面多留 60 天算 MA20 / 前 5 日累计）。"""
    try:
        return pd.read_parquet(
            path, columns=DAILY_COLS,
            filters=[("date", ">=", pad), ("date", "<=", end)],
        )
    except Exception:  # noqa: BLE001 - 个别文件缺列 / 过滤不支持，退全量读
        pass
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if "date" not in df.columns:
        return None
    return df[(df["date"] >= pad) & (df["date"] <= end)]


def _add_daily_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev = close.shift(1)
    df["d_ret1"] = pd.to_numeric(df["pct_change"], errors="coerce") / 100.0
    # pct_change 缺失的文件用收盘价兜底，避免一半样本在 ret1 统计里被丢掉
    df["d_ret1"] = df["d_ret1"].fillna(close / prev - 1.0)
    df["d_gap"] = df["open"].astype(float) / prev - 1.0
    df["d_amp"] = (high - low) / prev
    rng = (high - low).replace(0, np.nan)
    df["d_close_pos"] = (close - low) / rng
    vma5 = df["volume"].astype(float).rolling(5).mean().shift(1)
    df["d_vol_ratio"] = df["volume"].astype(float) / vma5
    df["d_ret5"] = close / close.shift(5) - 1.0
    df["d_ma5_dev"] = close / close.rolling(5).mean() - 1.0
    df["d_ma20_dev"] = close / close.rolling(20).mean() - 1.0
    df["d_up5"] = (df["d_ret1"] > 0).rolling(5).sum()
    if "turnover_rate" in df.columns:
        df["d_turnover"] = pd.to_numeric(df["turnover_rate"], errors="coerce")
    else:
        df["d_turnover"] = np.nan
    return df


def _daily_feats(df: pd.DataFrame, i: int) -> dict[str, float]:
    """第 i 行的日线特征（列名 d_* → 报告名）。"""
    return {
        "ret1": float(df["d_ret1"].iat[i]),
        "gap": float(df["d_gap"].iat[i]),
        "amp": float(df["d_amp"].iat[i]),
        "close_pos": float(df["d_close_pos"].iat[i]),
        "vol_ratio": float(df["d_vol_ratio"].iat[i]),
        "ret5": float(df["d_ret5"].iat[i]),
        "ma5_dev": float(df["d_ma5_dev"].iat[i]),
        "ma20_dev": float(df["d_ma20_dev"].iat[i]),
        "up5": float(df["d_up5"].iat[i]),
        "turnover": float(df["d_turnover"].iat[i]),
        # 只用于「同日、同成交额档」分层，不进报告表
        "amount": float(df["amount"].iat[i]),
    }


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
        if df is None or len(df) < 25 or "pct_change" not in df.columns:
            continue
        out[code] = _add_daily_features(df)
        if n % 1000 == 0:
            log(f"  日K {n}/{len(files)}，已收 {len(out)} 只，{time.time() - t0:.0f}s")
    return out


def scan_events(
    daily: dict[str, pd.DataFrame], start: str, end: str,
    threshold: float, lookback: int, min_amount: float,
) -> list[dict]:
    """窗口内 pct_change > threshold 的 (code, date)，并记下前 lookback 个交易日日期。"""
    need = max(lookback, 21)
    events: list[dict] = []
    lo = threshold * 100.0
    for code, df in daily.items():
        d = df["date"].to_numpy()
        pct = df["d_ret1"].to_numpy(dtype=float) * 100.0
        amt = df["amount"].to_numpy(dtype=float)
        n = len(df)
        for i in range(need, n):
            day = str(d[i])
            if not (start <= day <= end):
                continue
            p = pct[i]
            if not np.isfinite(p) or not (lo < p < 30.0):
                continue
            if min_amount and not (amt[i] >= min_amount):
                continue
            events.append({
                "code": code,
                "date": day,
                "i": int(i),
                "ret": float(p / 100.0),
                "amount": float(amt[i]),
                "cyb": bool(is_cyb(code)),
                "prev": [str(d[i - k]) for k in range(1, lookback + 1)],
            })
    return events


# ---------------------------------------------------------------------------
# 分钟线
# ---------------------------------------------------------------------------
def _read_minute(path: Path, lo: str, hi: str) -> dict[str, pd.DataFrame] | None:
    if not path.is_file():
        return None
    try:
        df = pd.read_parquet(
            path, columns=MIN_COLS,
            filters=[("date", ">=", f"{lo} 00:00:00"), ("date", "<=", f"{hi} 23:59:59")],
        )
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values("date")
    days = df["date"].astype(str).str.slice(0, 10)
    return {str(k): g.reset_index(drop=True) for k, g in df.groupby(days, sort=False)}


def _min1_feats(g: pd.DataFrame) -> dict[str, float] | None:
    if len(g) < 60:
        return None
    c = g["close"].to_numpy(dtype=float)
    o = g["open"].to_numpy(dtype=float)
    h = g["high"].to_numpy(dtype=float)
    v = g["volume"].to_numpy(dtype=float)
    a = g["amount"].to_numpy(dtype=float)
    n = len(c)
    vs = float(v.sum())
    if vs <= 0:
        return None
    r = np.diff(c) / c[:-1]
    cv = np.cumsum(v)
    ca = np.cumsum(a)
    vwap_t = np.divide(ca, cv, out=np.full_like(ca, np.nan), where=cv > 0)
    dev = c / vwap_t - 1.0
    mid = 120 if n >= 200 else n // 2
    last30 = v[-30:].sum() / vs
    return {
        "i_vol": float(np.nanstd(r)),
        "i_vwap_dev": float(c[-1] / (ca[-1] / cv[-1]) - 1.0) if cv[-1] > 0 else np.nan,
        "i_dev_std": float(np.nanstd(dev)),
        "i_above_vwap": float(np.nanmean((c > vwap_t).astype(float))),
        "i_open30_ret": float(c[min(29, n - 1)] / o[0] - 1.0) if o[0] > 0 else np.nan,
        "i_last30_ret": float(c[-1] / c[-31] - 1.0) if n > 31 else np.nan,
        "i_last30_vol": float(last30),
        "i_first30_vol": float(v[:30].sum() / vs),
        "i_high_pos": float(np.argmax(h) / max(n - 1, 1)),
        "i_close_high": float(c[-1] / h.max() - 1.0) if h.max() > 0 else np.nan,
        "i_up_ratio": float((r > 0).mean()),
        "i_max_amt_share": float(a.max() / a.sum()) if a.sum() > 0 else np.nan,
        "i_pm_ret": float(c[-1] / c[mid] - 1.0) if c[mid] > 0 else np.nan,
    }


def _max_streak_up(o: np.ndarray, c: np.ndarray) -> int:
    best = cur = 0
    for up in c > o:
        cur = cur + 1 if up else 0
        best = max(best, cur)
    return best


def _min5_feats(g: pd.DataFrame) -> dict[str, float] | None:
    if len(g) < 20:
        return None
    c = g["close"].to_numpy(dtype=float)
    o = g["open"].to_numpy(dtype=float)
    v = g["volume"].to_numpy(dtype=float)
    vs = float(v.sum())
    if vs <= 0:
        return None
    r = np.diff(c) / c[:-1]
    return {
        "m_up_ratio": float((c > o).mean()),
        "m_last6_ret": float(c[-1] / c[-7] - 1.0) if len(c) > 7 else np.nan,
        "m_last6_vol": float(v[-6:].sum() / vs),
        "m_open3_ret": float(c[2] / o[0] - 1.0) if o[0] > 0 else np.nan,
        "m_max_streak": float(_max_streak_up(o, c)),
        "m_ret_skew": float(pd.Series(r).skew()) if len(r) > 3 else np.nan,
    }


def _load_min_cross(
    codes: list[str], days_lo: str, days_hi: str, log, tag: str,
) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, dict]]]:
    """读一批票的 1m/5m，返回 {period: {date: {code: feats}}}。"""
    out1: dict[str, dict[str, dict]] = {}
    out5: dict[str, dict[str, dict]] = {}
    t0 = time.time()
    for n, code in enumerate(codes, 1):
        for base, feats_fn, bucket in (
            (MINUTE1_DIR, _min1_feats, out1),
            (MINUTE5_DIR, _min5_feats, out5),
        ):
            days = _read_minute(base / f"{code}.parquet", days_lo, days_hi)
            if not days:
                continue
            for day, g in days.items():
                f = feats_fn(g)
                if f:
                    bucket.setdefault(day, {})[code] = f
        if n % 100 == 0:
            log(f"  {tag} 分钟 {n}/{len(codes)}，{time.time() - t0:.0f}s")
    return out1, out5


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def _median(vals: list[float]) -> float:
    a = np.asarray([x for x in vals if np.isfinite(x)], dtype=float)
    return float(np.median(a)) if a.size else float("nan")


def _fmt(v: float, pct: bool = False) -> str:
    if not np.isfinite(v):
        return "—"
    return f"{v * 100:.2f}%" if pct else f"{v:.3f}"


#: 不是「比例」的特征：计数 / 位置 / 偏度，按原值展示
NO_PCT = {"up5", "i_high_pos", "m_max_streak", "m_ret_skew"}


def _fmt_feat(feat: str, v: float) -> str:
    if not np.isfinite(v):
        return "—"
    if feat == "turnover":
        return f"{v:.2f}%"          # turnover_rate 本身已是百分点（3.62 = 3.62%）
    if feat in NO_PCT:
        return f"{v:.3f}"
    return f"{v * 100:.2f}%"


def _precision(
    events: list[dict], cross_by_day: dict[str, dict[str, dict]],
    dec_groups: dict[str, dict[int, list[str]]], feats: list[str],
    next_pack: dict[str, dict[str, tuple[float, float]]], topq: float = 0.1,
) -> dict:
    """T-1 收盘按 feats 在同成交额档内取 top10%，看 T 日大涨的查准率与收益。

    和「分位」互补：分位说明大涨股的 T-1 特征在什么位置；查准率说明按这个特征
    真去选股能选中多少大涨股（基线 = 当日事件数 / 当日股票数）。多特征时先把
    各特征在组内转成百分位再相加，避免量纲差异。
    """
    ev_by_day: dict[str, set[str]] = {}
    for e in events:
        ev_by_day.setdefault(e["date"], set()).add(e["code"])
    next_day = {e["prev"][0]: e["date"] for e in events}
    pick = hit = 0
    base_num = base_den = 0
    rets: list[float] = []
    open_rets: list[float] = []
    for day, groups in dec_groups.items():
        tday = next_day.get(day)
        if not tday:
            continue
        ev_codes = ev_by_day.get(tday, set())
        all_n = len(cross_by_day.get(day, {}))
        for codes in groups.values():
            rows = []
            for c in codes:
                vals = [cross_by_day[day][c].get(f, np.nan) for f in feats]
                if all(np.isfinite(vals)):
                    rows.append((c, vals))
            if len(rows) < 30:
                continue
            score = np.zeros(len(rows))
            for fi in range(len(feats)):
                col = np.asarray([r[1][fi] for r in rows], dtype=float)
                score += (col.argsort().argsort() + 0.5) / len(col)
            order = np.argsort(-score)
            k = max(1, int(len(rows) * topq))
            picked = [rows[i][0] for i in order[:k]]
            pick += k
            hit += len(set(picked) & ev_codes)
            for c in picked:
                pack = next_pack.get(day, {}).get(c)
                if pack is not None:
                    rets.append(pack[0])
                    open_rets.append(pack[1])
            base_num += len(ev_codes)
            base_den += all_n
    precision = hit / pick if pick else float("nan")
    baseline = base_num / base_den if base_den else float("nan")
    return {
        "feats": feats,
        "topq": topq,
        "n_pick": pick,
        "hit": hit,
        "precision": precision,
        "baseline": baseline,
        "lift": precision / baseline if baseline and np.isfinite(precision) else float("nan"),
        "avg_ret": float(np.mean(rets)) if rets else float("nan"),
        "avg_open_ret": float(np.mean(open_rets)) if open_rets else float("nan"),
        "n_ret": len(rets),
    }


def build_report(
    events: list[dict], daily: dict[str, pd.DataFrame],
    min1: dict[str, dict[str, dict]], min5: dict[str, dict[str, dict]],
    lookback: int, meta: dict,
) -> dict:
    # 每条事件在各 lag 上的特征（事件组）
    ev_feats: list[dict[str, dict[str, float]]] = []
    for e in events:
        df = daily[e["code"]]
        row = {"0": _daily_feats(df, e["i"])}
        for k in range(1, lookback + 1):
            j = e["i"] - k
            d = e["prev"][k - 1]
            f = _daily_feats(df, j)
            f.update(min1.get(d, {}).get(e["code"], {}))
            f.update(min5.get(d, {}).get(e["code"], {}))
            row[str(k)] = f
        ev_feats.append(row)

    # 对照：保留「日期 → code → 特征」。**不能按 code 合并**——那样同一只票只会留下
    # 最后一个日期的值，等于拿某一天的市场截面去比跨 19 天的事件（曾因此得出
    # 「跳空 AUC 0.79」的假信号）。
    days_by_lag = {
        str(k): sorted({e["prev"][k - 1] for e in events})
        for k in range(1, lookback + 1)
    }
    needed_days = set().union(*days_by_lag.values())
    cross_by_day: dict[str, dict[str, dict]] = {d: {} for d in needed_days}
    # 下一交易日（= 事件日 T）的收益：(收盘/前收-1, 开盘买入到收盘)
    next_pack: dict[str, dict[str, tuple[float, float]]] = {d: {} for d in needed_days}
    for code, df in daily.items():
        dates = df["date"].to_numpy()
        closes = df["close"].to_numpy(dtype=float)
        opens = df["open"].to_numpy(dtype=float)
        rets = df["d_ret1"].to_numpy(dtype=float)
        for j in range(21, len(df)):
            day = str(dates[j])
            if day not in cross_by_day:
                continue
            cross_by_day[day][code] = _daily_feats(df, j)
            if j + 1 < len(df) and opens[j + 1] > 0:
                next_pack[day][code] = (
                    float(rets[j + 1]),
                    float(closes[j + 1] / opens[j + 1] - 1.0),
                )

    # 成交额十分位：把事件和「同日、同成交额档」的股票比。
    # 不做这层控制，「换手高 / 振幅大」很可能只是小盘股的代理。
    amt_edges: dict[str, np.ndarray] = {}
    dec_groups: dict[str, dict[int, list[str]]] = {}
    for day, cross in cross_by_day.items():
        vals = np.asarray([x.get("amount", np.nan) for x in cross.values()], dtype=float)
        vals = vals[np.isfinite(vals)]
        if vals.size < 100:
            continue
        edges = np.quantile(vals, np.linspace(0.1, 0.9, 9))
        amt_edges[day] = edges
        groups: dict[int, list[str]] = {}
        for code, x in cross.items():
            a = x.get("amount", np.nan)
            if not np.isfinite(a):
                continue
            dec = int(np.searchsorted(edges, a, side="right"))
            groups.setdefault(dec, []).append(code)
        dec_groups[day] = groups

    def _dec_of(day: str, amount: float) -> int | None:
        edges = amt_edges.get(day)
        if edges is None or not np.isfinite(amount):
            return None
        return int(np.searchsorted(edges, amount, side="right"))

    # (日期, 特征, 档位) → 对照值升序数组；多条事件复用同一天的分布
    sorted_cache: dict[tuple[str, str, int], np.ndarray] = {}

    def _ctrl_sorted(day: str, feat: str, dec: int | None) -> np.ndarray:
        ck = (day, feat, -1 if dec is None else dec)
        arr = sorted_cache.get(ck)
        if arr is not None:
            return arr
        if dec is not None and day in dec_groups:
            vals = [
                cross_by_day[day][c].get(feat, np.nan)
                for c in dec_groups[day].get(dec, [])
            ]
        else:
            if feat in DAILY_FEATS:
                cross = cross_by_day.get(day, {})
            elif feat in MIN1_FEATS:
                cross = min1.get(day, {})
            else:
                cross = min5.get(day, {})
            vals = [x.get(feat, np.nan) for x in cross.values()]
        arr = np.sort(np.asarray([x for x in vals if np.isfinite(x)], dtype=float))
        sorted_cache[ck] = arr
        return arr

    table: dict[str, dict] = {}
    for feat in ALL_FEATS:
        per_lag: dict[str, dict] = {}
        for k in range(1, lookback + 1):
            key = str(k)
            ev_vals: list[float] = []
            ranks: list[float] = []
            day_meds: list[float] = []
            n_ctrl = 0
            for e, r in zip(events, ev_feats):
                v = r[key].get(feat, np.nan)
                ev_vals.append(v)
                day = e["prev"][k - 1]
                dec = _dec_of(day, r[key].get("amount", np.nan)) if feat in DAILY_FEATS else None
                arr = _ctrl_sorted(day, feat, dec)
                n_ctrl += int(arr.size)
                if arr.size:
                    day_meds.append(float(np.median(arr)))
                if np.isfinite(v) and arr.size >= 20:
                    # 事件值在该事件「自己的 T-k 日 + 同成交额档」对照分布里的分位：
                    # 0.5 = 无区分度；平局取半（up5 / 连阳数这类离散值否则会虚高）
                    lo_r = np.searchsorted(arr, v, side="left")
                    hi_r = np.searchsorted(arr, v, side="right")
                    ranks.append((lo_r + hi_r) / (2.0 * arr.size))
            per_lag[key] = {
                "event_median": _median(ev_vals),
                "control_median": _median(day_meds),
                "pct": float(np.mean(ranks)) if ranks else float("nan"),
                "n_event": int(np.isfinite(np.asarray(ev_vals, dtype=float)).sum()),
                "n_ctrl": n_ctrl,
            }
        pcts = [v["pct"] for v in per_lag.values() if np.isfinite(v["pct"])]
        table[feat] = {
            "label": LABELS.get(feat, feat),
            "lags": per_lag,
            "pct_mean": float(np.mean(pcts)) if pcts else float("nan"),
        }

    ranked = sorted(
        (f for f in ALL_FEATS if np.isfinite(table[f]["pct_mean"])),
        key=lambda f: abs(table[f]["pct_mean"] - 0.5),
        reverse=True,
    )
    # 分层：创业板 vs 主板；涨幅 8~10 / 10~15 / >15
    def _subset(pred) -> dict:
        sub = [r for r, e in zip(ev_feats, events) if pred(e)]
        return {"n": len(sub)}

    precision = {
        label: _precision(events, cross_by_day, dec_groups, feats, next_pack)
        for label, feats in (
            ("amp", ["amp"]),
            ("turnover", ["turnover"]),
            ("amp+turnover", ["amp", "turnover"]),
            ("vol_ratio", ["vol_ratio"]),
            ("ret1", ["ret1"]),
        )
    }

    return {
        "meta": meta,
        "table": table,
        "ranked": ranked,
        "subsets": {
            "cyb": _subset(lambda e: e["cyb"]),
            "main": _subset(lambda e: not e["cyb"]),
            "ret_8_10": _subset(lambda e: e["ret"] <= 0.10),
            "ret_10_15": _subset(lambda e: 0.10 < e["ret"] <= 0.15),
            "ret_gt15": _subset(lambda e: e["ret"] > 0.15),
        },
        "precision": precision,
        "events": events,
    }





def print_report(rep: dict, lookback: int) -> None:
    meta = rep["meta"]
    ev = rep["events"]
    print("\n## 大涨前 1~5 日特征探查\n")
    print(f"- 窗口：{meta['start']} ~ {meta['end']}（日K/5m 到 {meta['data_last']}）")
    print(f"- 阈值：单日涨幅 > {meta['threshold'] * 100:.0f}%；事件 {len(ev)} 个 / "
          f"{len({e['code'] for e in ev})} 只股票；回看 {lookback} 个交易日")
    if ev:
        by_day = pd.Series([e["date"] for e in ev]).value_counts().sort_index()
        print(f"- 事件日 {len(by_day)} 天，日均 {len(ev) / len(by_day):.1f} 个；"
              f"最多 {by_day.idxmax()}（{by_day.max()} 个）")
        print(f"- 分布：创业板 {rep['subsets']['cyb']['n']} / 主板 {rep['subsets']['main']['n']}；"
              f"涨幅 8~10% {rep['subsets']['ret_8_10']['n']}、10~15% {rep['subsets']['ret_10_15']['n']}、"
              f">15% {rep['subsets']['ret_gt15']['n']}")
    print(f"- 对照口径：日线 = 同日 + 同成交额十分位；分钟 = 同日随机 {meta['control']} 只（抽样）")
    print("\n### 特征对比（事件中位数 / 同日对照中位数 / 分位；分位 0.5=无区分度）\n")
    head = ("| 特征 | " + " | ".join(f"T-{k}" for k in range(1, lookback + 1))
            + " | 对照(T-1) | 分位(T-1) | 分位均值 |")
    print(head)
    print("| --- | " + " | ".join("---" for _ in range(lookback)) + " | --- | --- | --- |")
    for feat in rep["ranked"]:
        row = rep["table"][feat]
        cells = [
            _fmt_feat(feat, row["lags"][str(k)]["event_median"])
            for k in range(1, lookback + 1)
        ]
        last = row["lags"]["1"]
        print(
            f"| {row['label']} | " + " | ".join(cells)
            + f" | {_fmt_feat(feat, last['control_median'])}"
            + f" | {_fmt(last['pct'], False)}"
            + f" | {_fmt(row['pct_mean'], False)} |"
        )
    print("\n### 区分度排序（|分位-0.5| 从大到小；分位按事件自身 T-k 日的对照分布算）\n")
    for feat in rep["ranked"][:12]:
        row = rep["table"][feat]
        p1 = row["lags"]["1"]["pct"]
        p5 = row["lags"][str(lookback)]["pct"]
        trend = "越临近越强" if np.isfinite(p1) and np.isfinite(p5) and abs(p1 - 0.5) > abs(p5 - 0.5) else "不稳定/反向"
        print(f"- **{row['label']}**（{feat}）：分位均值 {row['pct_mean']:.3f}，"
              f"T-1 {p1:.3f} → T-{lookback} {p5:.3f}，{trend}")
    prec = rep.get("precision") or {}
    if prec:
        print("\n### T-1 收盘选股 → T 日大涨（同成交额档内取 top10%）\n")
        print("| 排序特征 | 选中 | 命中 | 查准率 | 基线 | 提升 | T日均涨幅 | T日开盘买 |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in prec.items():
            lift = r["lift"]
            lift_s = f"{lift:.2f}x" if np.isfinite(lift) else "—"
            label = " + ".join(LABELS.get(f, f) for f in r["feats"])
            print(f"| {label} | {r['n_pick']} | {r['hit']} | "
                  f"{_fmt(r['precision'], True)} | {_fmt(r['baseline'], True)} | {lift_s} | "
                  f"{_fmt(r['avg_ret'], True)} | {_fmt(r['avg_open_ret'], True)} |")


def main() -> int:
    ap = argparse.ArgumentParser(description="大涨前 1~5 日盘面特征探查（纯研究）")
    ap.add_argument("--start", default="", help="事件起始日 YYYY-MM-DD（默认 end-30 天）")
    ap.add_argument("--end", default="", help="事件结束日（默认日K最新交易日）")
    ap.add_argument("--threshold", type=float, default=0.08, help="单日涨幅阈值（小数）")
    ap.add_argument("--lookback", type=int, default=5, help="回看交易日数")
    ap.add_argument("--control", type=int, default=300, help="分钟对照抽样只数（0=不抽）")
    ap.add_argument("--min-amount", type=float, default=0.0, help="事件日最小成交额（元，0=不过滤）")
    ap.add_argument("--max-codes", type=int, default=0, help="只跑前 N 只事件股（冒烟用，0=全部）")
    ap.add_argument("--exclude-days", default="", help="剔除的事件日，逗号分隔（敏感性检验）")
    ap.add_argument("--tag", default="live", help="产物 tag")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    kline_dir = Path(get_settings().kline_dir)
    if not kline_dir.is_dir():
        print(f"K线目录不存在：{kline_dir}")
        return 2

    def log(msg: str) -> None:
        print(msg, flush=True)

    end = args.end
    if not end:
        probe = kline_dir / "600000.parquet"
        end = str(pd.read_parquet(probe, columns=["date"])["date"].iloc[-1])
    start = args.start or str(pd.Timestamp(end) - pd.Timedelta(days=30))

    pad = str(pd.Timestamp(start) - pd.Timedelta(days=60))
    log(f"[1/4] 读日K：{pad} ~ {end}")
    daily = load_daily(kline_dir, pad, end, log)
    log(f"      候选股票 {len(daily)} 只")

    log("[2/4] 扫描大涨事件")
    events = scan_events(daily, start, end, args.threshold, args.lookback, args.min_amount)
    if args.exclude_days:
        drop = {d.strip() for d in args.exclude_days.split(",") if d.strip()}
        events = [e for e in events if e["date"] not in drop]
    if not events:
        log("      没有命中事件，检查 --threshold / --start / --end")
        return 1
    codes = sorted({e["code"] for e in events})
    if args.max_codes:
        codes = codes[: args.max_codes]
        keep = set(codes)
        events = [e for e in events if e["code"] in keep]
    log(f"      事件 {len(events)} 个 / {len(codes)} 只股票")

    lo = min(e["prev"][-1] for e in events)
    hi = max(e["prev"][0] for e in events)
    m1_last = "?"
    probe1 = MINUTE1_DIR / f"{codes[0]}.parquet"
    if probe1.is_file():
        m1_last = str(pd.read_parquet(probe1, columns=["date"])["date"].iloc[-1])[:10]
    log(f"[3/4] 读事件股分钟线：{lo} ~ {hi}（1m 实际到 {m1_last}）")
    min1, min5 = _load_min_cross(codes, lo, hi, log, "事件")

    rng = np.random.default_rng(args.seed)
    ctrl_codes: list[str] = []
    ctrl_min1: dict[str, dict[str, dict]] = {}
    ctrl_min5: dict[str, dict[str, dict]] = {}
    if args.control and args.control > 0:
        pool = [c for c in daily if c not in set(codes)]
        rng.shuffle(pool)
        ctrl_codes = pool[: args.control]
        log(f"[3/4] 读分钟对照：抽样 {len(ctrl_codes)} 只")
        ctrl_min1, ctrl_min5 = _load_min_cross(ctrl_codes, lo, hi, log, "对照")

    meta = {
        "start": start,
        "end": end,
        "data_last": str(pd.read_parquet(kline_dir / "600000.parquet", columns=["date"])
                         ["date"].iloc[-1])[:10],
        "threshold": args.threshold,
        "lookback": args.lookback,
        "control": len(ctrl_codes),
        "min_amount": args.min_amount,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    log("[4/4] 汇总")
    # 事件股分钟 + 对照分钟合并成一张「日期 → code → feats」的对照池
    for d, m in ctrl_min1.items():
        min1.setdefault(d, {}).update(m)
    for d, m in ctrl_min5.items():
        min5.setdefault(d, {}).update(m)
    rep = build_report(events, daily, min1, min5, args.lookback, meta)
    print_report(rep, args.lookback)

    out_dir = Path(get_settings().db_path).parent / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"big_move_probe_{args.tag}.json"
    payload = {
        "meta": meta,
        "table": rep["table"],
        "ranked": rep["ranked"],
        "subsets": rep["subsets"],
        "precision": rep["precision"],
        "events": rep["events"][:2000],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print(f"\n产物：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
