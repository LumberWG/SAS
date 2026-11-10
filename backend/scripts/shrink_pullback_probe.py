# -*- coding: utf-8 -*-
"""「缩量下跌 → 次日大涨」假设检验（纯研究，不改线上参数）。

口径
----
对窗口内每个交易日 T（T 只用来评估结果，特征全部来自 T-1 及之前）：

- ``ret5`` = close[T-1] / close[T-6] - 1   （T-1 往前 5 个交易日累计）
- ``vr5``  = mean(volume[T-5..T-1]) / mean(volume[T-20..T-6])（近 5 日量 / 前 15 日量）
- 结果：
  - ``next_ret`` = T 日涨跌幅（T-1 收盘买入持有到 T 日收盘）
  - ``next_open_ret`` = T 日开盘买入到收盘
  - ``big`` = T 日涨幅 > 8%

按**同一天**把 ret5 / vr5 各分 5 档，统计 25 格的次日大涨率与平均收益——同日比较天然
控制了大盘状态。最后单列「严格缩量下跌」条件，对照基线看有没有提升。

注意：T 日结果只用于验证假设；实盘在 T-1 收盘只能看到 ret5/vr5。
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
from big_move_probe import _add_daily_features, _read_daily  # noqa: E402


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
        if df is None or len(df) < 40 or "pct_change" not in df.columns:
            continue
        out[code] = _add_daily_features(df)
    return out


def build_samples(daily, start: str, end: str) -> pd.DataFrame:
    rows = []
    for code, df in daily.items():
        n = len(df)
        if n < 30:
            continue
        close = df["close"].to_numpy(dtype=float)
        open_ = df["open"].to_numpy(dtype=float)
        vol = df["volume"].to_numpy(dtype=float)
        ret = df["d_ret1"].to_numpy(dtype=float)
        dates = df["date"].astype(str).to_numpy()
        v5 = df["volume"].astype(float).rolling(5).mean().shift(1).to_numpy()
        vbase = df["volume"].astype(float).rolling(15).mean().shift(6).to_numpy()
        for i in range(20, n):
            day = str(dates[i])
            if not (start <= day <= end):
                continue
            if not (close[i - 6] > 0 and vbase[i] > 0):
                continue
            rows.append((
                day, code,
                close[i - 1] / close[i - 6] - 1.0,   # ret5
                v5[i] / vbase[i],                    # vr5
                ret[i],                              # 次日涨跌幅
                close[i] / open_[i] - 1.0 if open_[i] > 0 else np.nan,
                bool(ret[i] > 0.08),
            ))
    return pd.DataFrame(rows, columns=[
        "date", "code", "ret5", "vr5", "next_ret", "next_open_ret", "big",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description="缩量下跌 → 次日大涨 假设检验")
    ap.add_argument("--start", default="2026-10-10")
    ap.add_argument("--end", default="2026-11-09")
    args = ap.parse_args()

    kline_dir = Path(get_settings().kline_dir)
    pad = str(pd.Timestamp(args.start) - pd.Timedelta(days=90))
    print(f"[1/3] 读日K {pad} ~ {args.end}")
    daily = load_daily(kline_dir, pad, args.end)
    print(f"      {len(daily)} 只")

    print("[2/3] 构建样本")
    d = build_samples(daily, args.start, args.end)
    print(f"      {len(d)} 个股票-日 / {d['date'].nunique()} 天")

    base_big = d["big"].mean()
    base_ret = d["next_ret"].mean()
    base_open = d["next_open_ret"].mean()

    # 按日分档，避免跨日比较
    d["ret_q"] = d.groupby("date")["ret5"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
    )
    d["vol_q"] = d.groupby("date")["vr5"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
    )

    print("\n## 「缩量下跌 → 次日大涨」检验\n")
    print(f"- 窗口 {args.start} ~ {args.end}，{len(d)} 个股票-日")
    print(f"- 基线：次日大涨率 **{base_big:.2%}**，次日均值 {base_ret:.2%}，"
          f"开盘买入到收盘 {base_open:.2%}\n")

    print("### 25 格矩阵：次日大涨率（行=5日涨跌，列=量比；1=最低/最缩量，5=最高/最放量）\n")
    grid = d.groupby(["ret_q", "vol_q"]).agg(
        big=("big", "mean"), ret=("next_ret", "mean"), n=("big", "size"),
    )
    head = "| 5日涨跌 \\ 量比 | " + " | ".join(f"Q{k + 1}" for k in range(5)) + " |"
    print(head)
    print("| --- | " + " | ".join("---" for _ in range(5)) + " |")
    for rq in range(5):
        cells = []
        for vq in range(5):
            if (rq, vq) in grid.index:
                g = grid.loc[(rq, vq)]
                cells.append(f"{g['big']:.2%} (n={int(g['n'])})")
            else:
                cells.append("—")
        label = ["最跌(Q1)", "偏跌(Q2)", "中性(Q3)", "偏涨(Q4)", "最涨(Q5)"][rq]
        print(f"| {label} | " + " | ".join(cells) + " |")

    print("\n### 25 格矩阵：T 日开盘买入到收盘的均值收益（同口径分档）\n")
    print(head)
    print("| --- | " + " | ".join("---" for _ in range(5)) + " |")
    omean = d.groupby(["ret_q", "vol_q"])["next_open_ret"].mean()
    for rq in range(5):
        cells = []
        for vq in range(5):
            v = omean.get((rq, vq), np.nan)
            cells.append("—" if pd.isna(v) else f"{v:+.2%}")
        label = ["最跌(Q1)", "偏跌(Q2)", "中性(Q3)", "偏涨(Q4)", "最涨(Q5)"][rq]
        print(f"| {label} | " + " | ".join(cells) + " |")

    print("\n### 关键格：最跌 × 最缩量\n")
    key = grid.loc[(0, 0)]
    print(f"- 次日大涨率 **{key['big']:.2%}**（基线 {base_big:.2%}，"
          f"提升 {key['big'] / base_big:.2f}x），n={int(key['n'])}")
    print(f"- 次日均值 {key['ret']:.2%}；T 日开盘买入到收盘 "
          f"{d[(d.ret_q == 0) & (d.vol_q == 0)]['next_open_ret'].mean():.2%}")

    print("\n### 严格「缩量下跌」条件对比\n")
    cases = [
        ("ret5 < 0 且 vr5 < 0.8", (d.ret5 < 0) & (d.vr5 < 0.8)),
        ("ret5 < -3% 且 vr5 < 0.8", (d.ret5 < -0.03) & (d.vr5 < 0.8)),
        ("ret5 < -5% 且 vr5 < 0.8", (d.ret5 < -0.05) & (d.vr5 < 0.8)),
        ("ret5 < -5% 且 vr5 < 0.7", (d.ret5 < -0.05) & (d.vr5 < 0.7)),
        ("ret5 < -8% 且 vr5 < 0.7", (d.ret5 < -0.08) & (d.vr5 < 0.7)),
        ("ret5 > 5% 且 vr5 > 1.2（放量上涨）", (d.ret5 > 0.05) & (d.vr5 > 1.2)),
    ]
    print("| 条件 | 样本 | 次日大涨率 | 相对基线 | 次日均值 | 开盘买均值 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, mask in cases:
        sub = d[mask]
        if len(sub) < 30:
            print(f"| {name} | {len(sub)} | 样本不足 | — | — | — |")
            continue
        print(f"| {name} | {len(sub)} | {sub['big'].mean():.2%} | "
              f"{sub['big'].mean() / base_big:.2f}x | {sub['next_ret'].mean():.2%} | "
              f"{sub['next_open_ret'].mean():.2%} |")

    print("\n### 非对称性：大涨率上升时，大跌率是否同步上升\n")
    d["abs_next_ret"] = d["next_ret"].abs()
    groups = [
        ("最跌 × 最缩量", (d.ret_q == 0) & (d.vol_q == 0)),
        ("最跌 × 最放量", (d.ret_q == 0) & (d.vol_q == 4)),
        ("最涨 × 最放量", (d.ret_q == 4) & (d.vol_q == 4)),
        ("放量上涨(ret5>5%,vr5>1.2)", (d.ret5 > 0.05) & (d.vr5 > 1.2)),
        ("全样本", pd.Series(True, index=d.index)),
    ]
    print("| 组 | n | P(>8%) | P(<-8%) | P(>0) | 均值 | 标准差 | 偏度 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, mask in groups:
        sub = d[mask]
        print(f"| {name} | {len(sub)} | {sub['big'].mean():.2%} | "
              f"{(sub['next_ret'] < -0.08).mean():.2%} | "
              f"{(sub['next_ret'] > 0).mean():.2%} | {sub['next_ret'].mean():.2%} | "
              f"{sub['next_ret'].std():.2%} | {sub['next_ret'].skew():.2f} |")

    print("\n### 日频 IC：特征预测的是「方向」还是「波动」\n")
    d["abs_next_ret"] = d["next_ret"].abs()

    def daily_ic(col, target):
        ics = [g[col].corr(g[target], method="spearman") for _, g in d.groupby("date")]
        s = pd.Series(ics).dropna()
        if len(s) < 3 or s.std(ddof=1) == 0:
            return np.nan, np.nan
        return float(s.mean()), float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))

    print("| 特征 | 目标 | 日均秩 IC | t 值 | 解读 |")
    print("| --- | --- | --- | --- | --- |")
    for col, cname in (("ret5", "ret5 近5日涨跌"), ("vr5", "vr5 量比")):
        for tgt, tname in (("next_ret", "次日涨跌幅（方向）"),
                           ("abs_next_ret", "次日涨跌幅绝对值（波动）")):
            ic, t = daily_ic(col, tgt)
            verdict = "接近随机" if abs(ic) < 0.03 else ("弱" if abs(ic) < 0.08 else "明显")
            print(f"| {cname} | {tname} | {ic:+.4f} | {t:+.2f} | {verdict} |")

    print("\n### 反向看：大涨股在 T-1 的分布\n")
    bigs = d[d.big]
    print(f"- 大涨股 {len(bigs)} 个；其中 ret5 < 0 占 {bigs['ret5'].lt(0).mean():.2%}、"
          f"vr5 < 0.8 占 {bigs['vr5'].lt(0.8).mean():.2%}、"
          f"两者同时占 {((bigs.ret5 < 0) & (bigs.vr5 < 0.8)).mean():.2%}")
    print(f"- 全样本对应比例：ret5 < 0 {d['ret5'].lt(0).mean():.2%}、"
          f"vr5 < 0.8 {d['vr5'].lt(0.8).mean():.2%}、"
          f"两者同时 {((d.ret5 < 0) & (d.vr5 < 0.8)).mean():.2%}")
    print(f"- 大涨股 T-1 的 ret5 中位 {bigs['ret5'].median():.2%}（全样本 "
          f"{d['ret5'].median():.2%}）、vr5 中位 {bigs['vr5'].median():.3f}（全样本 "
          f"{d['vr5'].median():.3f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
