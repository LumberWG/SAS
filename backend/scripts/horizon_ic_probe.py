# -*- coding: utf-8 -*-
"""不同前瞻周期的「方向 IC」检验（纯研究，不改线上参数）。

回答的问题
----------
日频（T+1）方向 IC ≈ 0（见 BACKTEST_LOG #137），那 5 / 20 / 60 日周期上
价量特征对**方向**还有没有信噪比？哪个周期才够用？

口径
----
- 信号日 T：只用 T 日收盘及之前的数据算特征（收盘后选股，线上就是这个时点）。
- 收益：**T+1 开盘买入** → T+k 收盘卖出（与线上 `entry_open_delay_minutes=5` 一致，
  不用 T 日收盘价买入这种乐观假设）：
  ``fwd_k = close[T+k] / open[T+1] - 1``
- 特征（全部截至 T 日收盘）：
  - ``ret1 / ret5 / ret20 / ret60``：过去 1/5/20/60 个交易日累计涨跌
  - ``vr5``  = mean(vol[T-4..T]) / mean(vol[T-24..T-5])   近 5 日量 / 前 20 日量
  - ``vr20`` = mean(vol[T-19..T]) / mean(vol[T-39..T-20]) 近 20 日量 / 前 20 日量
- IC：每个信号日在横截面上算 Spearman 秩相关，再对信号日取均值 / t 值。
- **重叠修正**：k 日前瞻收益在相邻日高度重叠，直接按日算 IC 会把 t 值放大 ~sqrt(k) 倍。
  所以 k 日周期只取**每 k 个交易日一个信号日**（非重叠采样），t 值才有意义。
  k=60 时独立观测只有十几期，只能看方向，不能看显著性。

输出
----
每个周期一张表：IC 均值 / IC 标准差 / ICIR / t / IC>0 占比 / 期数 / Q5-Q1 收益差。
Q5-Q1 = 每个信号日按特征分 5 档，最高档与最低档的 fwd_k 均值之差，再对信号日取平均。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_BACKEND = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_BACKEND), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import get_settings  # noqa: E402
from app.data.scope import is_tradable  # noqa: E402
from big_move_probe import _read_daily  # noqa: E402

FEATS = ("ret1", "ret5", "ret20", "ret60", "vr5", "vr20")
HORIZONS = (1, 5, 20, 60)
MIN_NAMES = 100  # 单日横截面至少这么多只才算一个有效观测


def load_daily(kline_dir: Path, pad: str, end: str) -> dict[str, pd.DataFrame]:
    out = {}
    for path in sorted(kline_dir.glob("*.parquet")):
        code = path.stem
        if not (code.isdigit() and len(code) == 6):
            continue
        ok, _ = is_tradable(code, "")
        if not ok:
            continue
        df = _read_daily(path, pad, end)
        if df is None or len(df) < 80:
            continue
        out[code] = df.sort_values("date").reset_index(drop=True)
    return out


def build(daily: dict[str, pd.DataFrame], start: int) -> pd.DataFrame:
    frames = []
    for code, df in daily.items():
        c = df["close"].astype(float)
        o = df["open"].astype(float)
        v = df["volume"].astype(float)
        dates = pd.to_datetime(df["date"])
        # 用「自 1970-01-01 起的天数」做日期 ID：排序/分组等价，且便于向量化
        dint = dates.to_numpy().astype("datetime64[D]").astype(np.int64)

        col = {
            "ret1": c.pct_change(),
            "ret5": c / c.shift(5) - 1.0,
            "ret20": c / c.shift(20) - 1.0,
            "ret60": c / c.shift(60) - 1.0,
            "vr5": v.rolling(5).mean() / v.rolling(20).mean().shift(5),
            "vr20": v.rolling(20).mean() / v.rolling(20).mean().shift(20),
        }
        o1 = o.shift(-1)
        for k in HORIZONS:
            col[f"fwd{k}"] = c.shift(-k) / o1 - 1.0

        part = pd.DataFrame({k: val.to_numpy(dtype=np.float32) for k, val in col.items()})
        part["date"] = dint
        part["code"] = np.int32(int(code))
        part = part[dint >= start]
        if len(part):
            frames.append(part)
    if not frames:
        return pd.DataFrame(columns=["date", "code", *FEATS])
    return pd.concat(frames, ignore_index=True)


def ic_table(d: pd.DataFrame, k: int) -> list[dict]:
    tgt = f"fwd{k}"
    sub = d[np.isfinite(d[tgt].to_numpy())]
    if sub.empty:
        return []
    dates = np.sort(sub["date"].unique())
    sig = set(dates[::k])  # 非重叠采样：每 k 个交易日一个信号日
    sub = sub[sub["date"].isin(sig)]

    rows = []
    groups = list(sub.groupby("date", sort=True))
    for feat in FEATS:
        ics, spreads = [], []
        for _, g in groups:
            g = g[np.isfinite(g[feat].to_numpy()) & np.isfinite(g[tgt].to_numpy())]
            if len(g) < MIN_NAMES:
                continue
            ic = g[feat].corr(g[tgt], method="spearman")
            if np.isfinite(ic):
                ics.append(ic)
            q = pd.qcut(g[feat].rank(method="first"), 5, labels=False)
            lo = g[tgt][q == 0].mean()
            hi = g[tgt][q == 4].mean()
            if np.isfinite(lo) and np.isfinite(hi):
                spreads.append(hi - lo)
        if len(ics) < 3:
            continue
        s = pd.Series(ics)
        sd = float(s.std(ddof=1))
        mean = float(s.mean())
        t = mean / (sd / np.sqrt(len(s))) if sd > 0 else np.nan
        rows.append({
            "feat": feat,
            "n_periods": len(s),
            "ic": mean,
            "ic_sd": sd,
            "icir": mean / sd if sd > 0 else np.nan,
            "t": t,
            "pos": float((s > 0).mean()),
            "spread": float(np.mean(spreads)) if spreads else np.nan,
        })
    return rows


def _sampled(d: pd.DataFrame, k: int, tgt: str) -> pd.DataFrame:
    """取该周期的非重叠信号日。"""
    sub = d[np.isfinite(d[tgt].to_numpy())]
    if sub.empty:
        return sub
    dates = np.sort(sub["date"].unique())
    return sub[sub["date"].isin(set(dates[::k]))]


def bucket_table(d: pd.DataFrame, k: int, feat: str) -> list[tuple]:
    """按特征分 5 档，返回每档的 fwd_k 均值 / 中位 / 胜率（对信号日取平均）。"""
    tgt = f"fwd{k}"
    acc: dict[int, list] = {i: [] for i in range(5)}
    for _, g in _sampled(d, k, tgt).groupby("date", sort=True):
        g = g[np.isfinite(g[feat].to_numpy()) & np.isfinite(g[tgt].to_numpy())]
        if len(g) < MIN_NAMES:
            continue
        q = pd.qcut(g[feat].rank(method="first"), 5, labels=False)
        for i in range(5):
            v = g[tgt][q == i]
            if len(v):
                acc[i].append((float(v.mean()), float(v.median()), float((v > 0).mean())))
    out = []
    for i in range(5):
        a = np.asarray(acc[i], dtype=float)
        if not len(a):
            continue
        out.append((i, float(a[:, 0].mean()), float(a[:, 1].mean()), float(a[:, 2].mean())))
    return out


def seg_ic(d: pd.DataFrame, k: int, feat: str, keep_dates) -> tuple[float, float, int]:
    """限定信号日集合后的 IC 均值 / t / 期数（用于分段稳定性）。"""
    tgt = f"fwd{k}"
    sub = d[np.isfinite(d[tgt].to_numpy()) & d["date"].isin(keep_dates)]
    ics = []
    for _, g in _sampled(sub, k, tgt).groupby("date", sort=True):
        g = g[np.isfinite(g[feat].to_numpy()) & np.isfinite(g[tgt].to_numpy())]
        if len(g) < MIN_NAMES:
            continue
        ic = g[feat].corr(g[tgt], method="spearman")
        if np.isfinite(ic):
            ics.append(ic)
    if len(ics) < 3:
        return np.nan, np.nan, len(ics)
    s = pd.Series(ics)
    sd = float(s.std(ddof=1))
    return float(s.mean()), (float(s.mean()) / (sd / np.sqrt(len(s))) if sd > 0 else np.nan), len(s)


def main() -> int:
    ap = argparse.ArgumentParser(description="多周期方向 IC 检验")
    ap.add_argument("--start", default="2023-07-03", help="信号日起点")
    ap.add_argument("--end", default="2026-11-09", help="数据终点（尾部自动留出 k 日）")
    ap.add_argument("--pad-days", type=int, default=150, help="预热天数，用于 ret60 / vr20")
    args = ap.parse_args()

    pad = str(pd.Timestamp(args.start) - pd.Timedelta(days=args.pad_days))
    kline_dir = Path(get_settings().kline_dir)
    print(f"[1/3] 读日K {pad} ~ {args.end}")
    daily = load_daily(kline_dir, pad, args.end)
    print(f"      {len(daily)} 只")

    print("[2/3] 构建样本")
    start_int = int(np.datetime64(pd.Timestamp(args.start), "D").astype(np.int64))
    d = build(daily, start_int)
    print(f"      {len(d)} 个股票-日 / {d['date'].nunique()} 个交易日")

    print("[3/3] 分周期算 IC\n")
    print(f"- 信号日 {args.start} ~ {args.end}，收益口径：T 日收盘选股、T+1 开盘买入、T+k 收盘卖出")
    print("- 每个周期只用**非重叠**信号日（每 k 个交易日取 1 天），t 值才是有效自由度\n")

    all_rows = []
    for k in HORIZONS:
        rows = ic_table(d, k)
        if not rows:
            print(f"### {k} 日周期：样本不足\n")
            continue
        n = rows[0]["n_periods"]
        note = ""
        if n < 30:
            note = "（期数 < 30，只可看方向，t 值不可信）"
        print(f"### {k} 日周期（独立观测 {n} 期）{note}\n")
        print("| 特征 | IC 均值 | IC 标准差 | ICIR | t | IC>0 | Q5-Q1 |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            print(f"| {r['feat']} | {r['ic']:+.4f} | {r['ic_sd']:.4f} | {r['icir']:+.3f} | "
                  f"{r['t']:+.2f} | {r['pos']:.0%} | {r['spread']:+.2%} |")
        for r in rows:
            all_rows.append({"k": k, **r})
        print()

    if all_rows:
        t = pd.DataFrame(all_rows)
        print("### 汇总：各特征在哪个周期上 |t| 最大\n")
        print("| 特征 | 最佳周期 | IC | t | Q5-Q1 | 判定 |")
        print("| --- | --- | --- | --- | --- | --- |")
        for feat in FEATS:
            sub = t[t.feat == feat]
            if sub.empty:
                continue
            best = sub.loc[sub["t"].abs().idxmax()]
            if abs(best["t"]) < 2:
                verdict = "各周期均不显著"
            elif best["n_periods"] < 30:
                verdict = "仅方向，样本太少"
            else:
                verdict = "显著"
            print(f"| {feat} | {int(best['k'])} 日 | {best['ic']:+.4f} | {best['t']:+.2f} | "
                  f"{best['spread']:+.2%} | {verdict} |")

    print("\n### 分档收益：把「IC 符号」翻译成「买哪档赚多少」\n")
    print("（Q1 = 特征值最低的档；fwd 为 T+1 开盘买入到 T+k 收盘，均值为各信号日平均）\n")
    for k, feat in ((20, "ret1"), (20, "ret20"), (60, "ret20"), (20, "vr5")):
        rows = bucket_table(d, k, feat)
        if not rows:
            continue
        print(f"**{k} 日周期 / 按 {feat} 分 5 档**\n")
        print("| 档 | fwd 均值 | fwd 中位 | 胜率 |")
        print("| --- | --- | --- | --- |")
        label = {0: "Q1 最低", 1: "Q2", 2: "Q3", 3: "Q4", 4: "Q5 最高"}
        for i, m, med, win in rows:
            print(f"| {label[i]} | {m:+.2%} | {med:+.2%} | {win:.1%} |")
        print()

    print("### 分段稳定性：反转效应是不是只在某一段行情里\n")
    years = pd.to_datetime(d["date"].unique(), unit="D").year
    uniq = d["date"].unique()
    print("| 周期 | 特征 | " + " | ".join(f"{y} 年（IC / t / 期数）" for y in sorted(set(years))) + " |")
    print("| --- | --- | " + " | ".join("---" for _ in sorted(set(years))) + " |")
    for k, feat in ((1, "ret1"), (20, "ret20"), (20, "ret60")):
        cells = []
        for y in sorted(set(years)):
            keep = uniq[years == y]
            ic, t, n = seg_ic(d, k, feat, keep)
            cells.append(f"{ic:+.4f} / {t:+.2f} / {n}" if np.isfinite(ic) else "—")
        print(f"| {k} 日 | {feat} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
