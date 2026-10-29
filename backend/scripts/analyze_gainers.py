"""分析近两个月涨幅前 100 股票的「启动前」特征，对照策略的选股/买点条件。

用法（在 backend 目录）：
    .venv/Scripts/python.exe scripts/analyze_gainers.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app.data.cache import kline_cache
from app.data.service import get_stock_pool

START = "2026-08-29"
END = "2026-10-29"


def main() -> None:
    pool = get_stock_pool()
    print(f"股票池: {len(pool)} 只")

    recs = []
    miss = 0
    for row in pool.itertuples(index=False):
        code = str(getattr(row, "code", "")).zfill(6)
        name = str(getattr(row, "name", "") or "")
        if not code:
            continue
        df = kline_cache.get(code)
        if df is None or len(df) < 80:
            miss += 1
            continue
        df = df[df["date"] <= END]
        if df.empty:
            continue
        seg = df[df["date"] >= START]
        if len(seg) < 15:
            continue
        first_close = float(seg["close"].iloc[0])
        last_close = float(seg["close"].iloc[-1])
        if first_close <= 0:
            continue
        recs.append({
            "code": code, "name": name,
            "chg": last_close / first_close - 1, "bars": len(seg),
        })

    print(f"有K线缓存: {len(recs)} 只, 缓存缺失/过短: {miss}")
    if not recs:
        print("无可用数据")
        return

    df_all = pd.DataFrame(recs)
    df_all = df_all[~df_all["name"].str.contains("ST|退", na=False, regex=True)]
    if df_all.empty:
        print("过滤后无样本")
        return
    top = df_all.sort_values("chg", ascending=False).head(100).reset_index(drop=True)
    print(f"前100区间涨幅: {top['chg'].min():.1%} ~ {top['chg'].max():.1%}")
    top["board"] = top["code"].str[:3]

    feats = []
    for r in top.itertuples(index=False):
        df = kline_cache.get(r.code)
        if df is None:
            continue
        df = df[df["date"] <= END].copy().reset_index(drop=True)
        starts = df.index[df["date"] >= START]
        if len(starts) == 0:
            continue
        start_pos = int(starts[0])
        seg = df.loc[start_pos:].copy()
        seg["dret"] = seg["close"] / seg["close"].shift(1) - 1
        seg["dret"] = seg["dret"].fillna(0.0)
        peak_pos = int(seg["dret"].idxmax())
        if peak_pos <= start_pos:
            peak_pos = start_pos + 1
        if peak_pos > len(df) - 1:
            peak_pos = len(df) - 1
        w0 = max(0, peak_pos - 20)
        win = df.loc[w0:peak_pos - 1]
        if len(win) < 10:
            continue

        prev_close = float(df.loc[peak_pos - 1, "close"])
        base_close = float(df.loc[w0, "close"])
        pre_ret20 = prev_close / base_close - 1 if base_close > 0 else np.nan

        amp = float(((win["high"] - win["low"]) / win["close"].replace(0, np.nan)).mean())
        v5 = float(win["volume"].tail(5).mean())
        v20 = float(win["volume"].mean())
        vol_ratio = v5 / v20 if v20 > 0 else np.nan

        ma20 = float(df.loc[max(0, peak_pos - 20):peak_pos - 1, "close"].mean())
        ma60 = float(df.loc[max(0, peak_pos - 60):peak_pos - 1, "close"].mean())
        last60 = df.loc[max(0, peak_pos - 60):peak_pos - 1, "close"]
        low60, high60 = float(last60.min()), float(last60.max())
        pos60 = (prev_close - low60) / (high60 - low60) if high60 > low60 else np.nan
        turnover = (
            float(win["turnover_rate"].mean())
            if "turnover_rate" in win.columns else np.nan
        )
        feats.append({
            "code": r.code, "name": r.name, "chg": float(r.chg),
            "board": r.code[:3],
            "pre_ret20": pre_ret20,
            "amp": amp if amp == amp else np.nan,
            "vol_ratio": vol_ratio,
            "above_ma20": prev_close / ma20 - 1 if ma20 > 0 else np.nan,
            "above_ma60": prev_close / ma60 - 1 if ma60 > 0 else np.nan,
            "pos60": pos60,
            "turnover": turnover,
        })

    f = pd.DataFrame(feats)
    print(f"\n可分析样本: {len(f)}")
    if f.empty:
        return

    print("\n=== 板块分布(前100) ===")
    print(top["board"].value_counts().head(10).to_string())

    print("\n=== 启动前特征（中位数 / 均值 / p25 / p75） ===")
    for col in ("pre_ret20", "amp", "vol_ratio", "above_ma20", "above_ma60", "pos60", "turnover"):
        s = f[col].dropna()
        if s.empty:
            continue
        print(
            f"{col:12s}: median={s.median():.3f} mean={s.mean():.3f} "
            f"p25={s.quantile(.25):.3f} p75={s.quantile(.75):.3f}"
        )

    s = f.dropna(subset=["pre_ret20", "vol_ratio", "above_ma20", "pos60"])
    n = len(s)

    def pct(mask) -> str:
        return f"{mask.sum()}/{n} = {mask.mean():.0%}"

    print("\n=== 启动前特征 vs 策略条件的吻合占比 ===")
    print("启动前20日涨幅 < 8% (不追已涨)      :", pct(s["pre_ret20"] < 0.08))
    print("启动前5日缩量 (vol_ratio<1)          :", pct(s["vol_ratio"] < 1.0))
    print("启动前贴/破 MA20 (above_ma20<=3%)    :", pct(s["above_ma20"] <= 0.03))
    print("启动前在 MA60 附近/下方 (<=5%)       :", pct(s["above_ma60"] <= 0.05))
    print("启动前处 60 日低位区 (pos60<0.35)    :", pct(s["pos60"] < 0.35))
    print("创业板(300) 占比                     :", f"{(top['board'] == '300').mean():.0%}")

    print("\n=== 前 20 名明细 ===")
    cols = ["code", "name", "chg", "pre_ret20", "vol_ratio", "above_ma20", "pos60", "turnover"]
    print(f.head(20)[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))


if __name__ == "__main__":
    main()
