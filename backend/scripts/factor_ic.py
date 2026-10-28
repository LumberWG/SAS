"""因子 IC 体检：在「沪深主板 + 创业板」池内，检验每个因子对未来的预测力。

为什么需要：`BACKTEST_LOG` 记录 #20 显示策略胜率只有 0~6%，大量成交以 −8% 止损离场。
这说明**买入信号的选股力本身可能为负**——不是止损、不是仓位，而是「买错了票」。
与其继续调参数，不如先量一下：这些因子在 A 股 1 个月尺度上到底是动量还是反转。

口径（刻意贴近用户的实际约束）：
  - 股票池：每日成交额 TopN 中的**沪深主板（600/601/603/605/000/001/002/003）+ 创业板（300/301）**；
    不含科创板(688)、北交所(920/43/83/87)、B 股。
  - 截面：每 ``--step`` 个交易日取一次（默认 5 日），每个截面在池内算 Spearman 秩相关。
  - 未来收益：``fwd{h}`` = 收盘价相对 h 个交易日后的涨跌幅（h = 5/10/20）。
  - IC 定义：factor_t 与 fwd_t 的秩相关。IC 均值 > 0 说明因子值越大、未来越涨。

判读标准（业界常用）：
  |IC| < 0.02 基本无效；0.02~0.05 弱有效；> 0.05 强；
  ICIR = IC 均值 / IC 标准差，> 0.3 才算稳定；> 0.5 很好。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\factor_ic.py
    .venv\\Scripts\\python.exe scripts\\factor_ic.py --start 2024-01-01 --step 5 --universe 300
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _prepare  # noqa: E402
from app.indicators.technical import add_all_indicators  # noqa: E402

#: 只在沪深主板 + 创业板 里选股（用户的实际交易范围）
ALLOWED_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301")

HORIZONS = (5, 10, 20)


def _allowed(code: str) -> bool:
    return code.startswith(ALLOWED_PREFIX)


def _factors(df: pd.DataFrame, i: int) -> dict[str, float] | None:
    """取第 i 行的因子值（df 需已加指标）；缺数据的因子跳过。"""
    row = df.iloc[i]
    close = float(row["close"])
    if close <= 0 or close != close:
        return None
    try:
        prev5 = float(df["close"].iloc[i - 5])
        prev20 = float(df["close"].iloc[i - 20])
    except IndexError:
        return None
    if prev5 <= 0 or prev20 <= 0:
        return None

    def val(name: str, scale: float = 1.0) -> float | None:
        if name not in df.columns:
            return None
        v = row[name]
        try:
            v = float(v) / scale
        except (TypeError, ValueError):
            return None
        return v if v == v else None

    # 距季线（MA60）：策略的「距季线 +10%」买入条件
    dist_ma60 = None
    if "ma60" in df.columns:
        ma60 = float(row["ma60"])
        if ma60 > 0:
            dist_ma60 = close / ma60 - 1

    out = {
        "mom5": close / prev5 - 1,          # 5 日涨幅（策略的动量因子）
        "mom20": close / prev20 - 1,        # 20 日涨幅
        "bias24": val("bias24", 100.0),     # 距 MA24（乖离率，超买超卖）
        "dist_ma60": dist_ma60,             # 距 MA60（「距季线 +10%」那条）
        "vol_ratio": val("vol_ratio"),      # 量比（量能因子）
        "macd_hist": val("macd_hist", close),  # MACD 柱体（趋势因子）
        "rsi6": val("rsi6"),                # 短线超买超卖
        "amplitude": val("atr", close),     # ATR%（波动）
        "turnover": val("turnover_rate"),   # 换手率
    }
    return {k: v for k, v in out.items() if v is not None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-10-27")
    parser.add_argument("--universe", type=int, default=500, help="每日成交额 TopN 池")
    parser.add_argument("--step", type=int, default=5, help="每隔几个交易日取一个截面")
    args = parser.parse_args()

    params = PipelineParams(
        start_date=args.start, end_date=args.end, universe_size=args.universe
    )
    print(f"准备数据 {args.start} ~ {args.end}、Top{args.universe} ...", flush=True)
    prepared = _prepare(params)
    dates: list[str] = prepared["dates"]
    universe: dict[str, list[str]] = prepared["universe"]
    klines: dict[str, pd.DataFrame] = prepared["klines"]
    pos_index: dict[str, dict[str, int]] = prepared["pos_index"]

    print("计算指标 ...", flush=True)
    ind = {c: add_all_indicators(df) for c, df in klines.items()}

    sample_dates = [d for d in dates[:: args.step] if d >= args.start]
    print(f"截面 {len(sample_dates)} 个，池内标的平均 "
          f"{sum(len(universe.get(d, [])) for d in sample_dates) / max(len(sample_dates), 1):.0f} 只/日",
          flush=True)

    rows: list[dict] = []
    for date in sample_dates:
        codes = [c for c in universe.get(date, []) if _allowed(c)]
        recs = []
        for code in codes:
            df = ind.get(code)
            idx = pos_index.get(code, {}).get(date)
            if df is None or idx is None or idx < 60:
                continue
            fac = _factors(df, idx)
            if not fac:
                continue
            rec: dict[str, float] = {"code": code, **fac}
            ok = True
            for h in HORIZONS:
                if idx + h >= len(df):
                    ok = False
                    break
                rec[f"fwd{h}"] = float(df["close"].iloc[idx + h]) / float(df["close"].iloc[idx]) - 1
            if ok:
                recs.append(rec)
        if len(recs) < 30:
            continue
        cross = pd.DataFrame(recs)
        for name in [c for c in cross.columns if c not in ("code",) and not c.startswith("fwd")]:
            for h in HORIZONS:
                # 无 scipy：秩变换后再算 Pearson 即 Spearman
                ic = cross[name].rank().corr(cross[f"fwd{h}"].rank())
                rows.append({"date": date, "factor": name, "h": h, "ic": ic,
                             "n": len(cross)})

    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("没有有效截面，检查数据区间")

    print(f"\n{'因子':<12}{'未来5日':>18}{'未来10日':>18}{'未来20日':>18}")
    print(f"{'':<12}{'IC  ICIR 胜率':>18}{'IC  ICIR 胜率':>18}{'IC  ICIR 胜率':>18}")
    print("-" * 66)
    factors = sorted(df["factor"].unique())
    for name in factors:
        cells = []
        for h in HORIZONS:
            sub = df[(df["factor"] == name) & (df["h"] == h)]["ic"].dropna()
            if sub.empty:
                cells.append(f"{'-':>18}")
                continue
            mean, std = sub.mean(), sub.std()
            ir = mean / std if std else 0.0
            cells.append(f"{mean:+.4f} {ir:+.2f} {sub.gt(0).mean():4.0%}".rjust(18))
        print(f"{name:<12}" + "".join(cells))


if __name__ == "__main__":
    main()
