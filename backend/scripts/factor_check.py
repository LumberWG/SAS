"""横截面因子检验：起点按「趋势/放量/超卖」分组，统计 2026-08-29~10-29 实际涨幅。

用法（在 backend 目录）：
    .venv/Scripts/python.exe scripts/factor_check.py
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
    rows = []
    for row in pool.itertuples(index=False):
        code = str(getattr(row, "code", "")).zfill(6)
        name = str(getattr(row, "name", "") or "")
        if "ST" in name or "退" in name:
            continue
        df = kline_cache.get(code)
        if df is None or len(df) < 80:
            continue
        df = df[df["date"] <= END].reset_index(drop=True)
        starts = df.index[df["date"] >= START]
        if len(starts) == 0:
            continue
        s = int(starts[0])
        if s < 60:
            continue
        prev = float(df.loc[s - 1, "close"])
        ma20 = float(df.loc[s - 20:s - 1, "close"].mean())
        ma60 = float(df.loc[s - 60:s - 1, "close"].mean())
        v5 = float(df.loc[s - 5:s - 1, "volume"].mean())
        v20 = float(df.loc[s - 20:s - 1, "volume"].mean())
        vr = v5 / v20 if v20 > 0 else np.nan
        pre20 = prev / float(df.loc[s - 20, "close"]) - 1 if float(df.loc[s - 20, "close"]) > 0 else np.nan
        fc = float(df.loc[s, "close"])
        lc = float(df.loc[len(df) - 1, "close"])
        chg = lc / fc - 1 if fc > 0 else np.nan
        rows.append({
            "code": code, "name": name, "board": code[:3], "chg": chg,
            "above20": prev / ma20 - 1 if ma20 > 0 else np.nan,
            "above60": prev / ma60 - 1 if ma60 > 0 else np.nan,
            "vr": vr, "pre20": pre20,
        })

    d = pd.DataFrame(rows).dropna(subset=["chg", "above20", "above60", "vr", "pre20"])
    print(f"有效样本: {len(d)} 只（区间 {START} ~ {END}）")

    def stat(mask, label):
        x = d.loc[mask, "chg"]
        if len(x) == 0:
            print(f"{label:40s} 空")
            return
        print(
            f"{label:40s} n={len(x):4d} mean={x.mean():+7.1%} "
            f"med={x.median():+7.1%} win={(x > 0).mean():5.0%} >20%={(x > 0.2).mean():5.0%}"
        )

    print()
    stat(pd.Series(True, index=d.index), "全市场")
    stat((d["above20"] > 0) & (d["above60"] > 0), "趋势多头(MA20&MA60上)")
    stat((d["above20"] > 0) & (d["above60"] > 0) & (d["vr"] > 1), "趋势多头+放量(量比>1)")
    stat(
        (d["above20"] > 0.05) & (d["above60"] > 0) & (d["pre20"] > 0.08) & (d["vr"] > 1),
        "右侧加速(涨5%+MA60上+20日涨8%+放量)",
    )
    stat((d["above20"] < 0) & (d["above60"] < 0), "超卖(双均线下)")
    stat(
        d["board"].isin(["300", "301"]) & (d["above20"] < 0)
        & (d["pre20"] < 0.08) & (d["vr"] >= 1),
        "本策略型(创业板+超卖+不追高+量比≥1)",
    )
    stat(d["board"].isin(["300", "301"]), "创业板(300/301)")
    stat(d["board"].isin(["002", "600", "601", "603", "605", "000", "001"]), "主板")


if __name__ == "__main__":
    main()
