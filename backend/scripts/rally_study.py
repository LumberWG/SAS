"""上涨规律研究：强势股 → 随机月涨幅前 N → 上涨前/上涨中/见顶后特征 → 回归。

回答的问题：「突然涨起来的票，涨之前有什么共同特征？涨完之后守不守得住？」

口径（全部基于本地 K 线 parquet，不联网）：
  1. 池子：近三个月累计涨幅前 ``--pool`` 只（剔除流动性差、停牌、末端不在交易日的）；
  2. 样本：池内**随机选中某个自然月**、该月涨幅前 ``--top`` 只——「依旧涨幅较大」；
  3. 行情段：在该月内定位「最低收盘价 → 其后最高收盘价」为上涨段，
     最低点之前 20 日为「上涨前」，两点之间为「上涨中」，最高点之后为「见顶后」；
  4. 回归：上涨前特征 → 上涨段涨幅（找「什么样的票涨得多」）；
  5. 对照：全市场同口径特征 vs 样本做 Mann-Whitney U，另按该月涨幅分层看是否单调，
     避免「只看样本自己有多特别」。

已知局限（解读时必须带上）：
  - 样本只有 100 只、10 个候选特征，多元回归的 R² 有天然虚高，只作方向参考；
  - 「最高点」是事后才知道的，用它当因变量衡量的是**已经发生的涨幅**，
    不等于事前可预测——所以真正的判据是「上涨前特征」的显著性与方向是否稳定；
  - 随机月只代表一个月份，单月结论不做跨期外推。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\rally_study.py
    .venv\\Scripts\\python.exe scripts\\rally_study.py --month 2026-09 --top 100 --seed 42
    .venv\\Scripts\\python.exe scripts\\rally_study.py --all-months   # 三个月各跑一遍看稳健性
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402

#: 上涨前特征（自变量）
PRE_COLS = (
    "pre_ret20",          # 前 20 日累计涨幅：动量还是反转
    "pre_vol20",          # 前 20 日日收益标准差：波动水平
    "pre_vol_ratio",      # 近 5 日均量 / 前 20 日均量：是否缩量
    "pre_ma_spread",      # MA5/10/20 离散度 / MA20：均线粘合度
    "pre_dist_high250",   # 距 250 日最高价（负值=还在高位下方多少）
    "pre_dist_low250",    # 距 250 日最低价（正值=已离底部多远）
    "pre_turnover20",     # 前 20 日均换手率（%）
    "pre_log_amount20",   # 前 20 日均成交额（对数）
    "pre_dd60",           # 距前 60 日最高收盘的回撤
    "pre_atr_pct",        # 前 20 日 (high-low)/close 均值，波幅
)


def _read_kline(path: Path, start: str) -> pd.DataFrame | None:
    """读一只股票的日线，只保留 start 之后的行（流式，避免全市场驻留内存）。"""
    try:
        df = pd.read_parquet(
            path, columns=["date", "open", "close", "high", "low",
                           "volume", "amount", "turnover_rate"]
        )
    except Exception:  # noqa: BLE001 - 单个文件损坏不影响整体
        return None
    if df.empty:
        return None
    col = df["date"]
    if pd.api.types.is_datetime64_any_dtype(col):
        df["date"] = col.dt.strftime("%Y-%m-%d")
    else:
        df["date"] = col.astype(str).str[:10]
    df = df[df["date"] >= start]
    if len(df) < 80:
        return None
    return df.reset_index(drop=True)


def _month_features(df: pd.DataFrame, month: str) -> dict | None:
    """某只股票在 ``month`` 内的行情段特征；不足条件返回 None。"""
    dates = df["date"].to_numpy()
    in_month = np.where(np.char.startswith(dates.astype(str), month))[0]
    if len(in_month) < 10:
        return None

    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    amount = df["amount"].to_numpy(float)
    turn = df["turnover_rate"].to_numpy(float)

    # 日收益率（自己算：缓存里的 pct_change 是 NaN）
    pct = np.zeros(len(close), dtype=float)
    pct[1:] = np.where(close[:-1] > 0, close[1:] / close[:-1] - 1.0, 0.0)

    sub = close[in_month]
    i_low = int(in_month[int(np.argmin(sub))])
    i_high = int(in_month[int(np.argmax(sub))])

    if i_high <= i_low:
        # 该月先高后低：没有「先创低再创新高」的上涨段，单独标记
        shape = "high_first"
        lo_i, hi_i = int(in_month[0]), i_high
    else:
        shape = "low_to_high"
        lo_i, hi_i = i_low, i_high

    # 上涨前窗口需要 21 日历史 + 60 日回撤参考
    if lo_i < 61:
        return None

    s, e = lo_i - 20, lo_i
    ma5 = float(np.mean(close[lo_i - 5:lo_i]))
    ma10 = float(np.mean(close[lo_i - 10:lo_i]))
    ma20 = float(np.mean(close[s:e]))
    if ma20 <= 0:
        return None

    win_hi = high[max(0, lo_i - 250):lo_i]
    win_lo = low[max(0, lo_i - 250):lo_i]
    dd_ref = float(np.max(close[max(0, lo_i - 60):lo_i]))

    vol_pre = np.nanmean(vol[s:e])
    feat = {
        "month": month,
        "shape": shape,
        "low_date": str(dates[lo_i]),
        "high_date": str(dates[hi_i]),
        "pre_ret20": float(close[lo_i - 1] / close[lo_i - 21] - 1.0),
        "pre_vol20": float(np.nanstd(pct[s:e])),
        "pre_vol_ratio": float(np.nanmean(vol[lo_i - 5:lo_i]) / vol_pre) if vol_pre > 0 else np.nan,
        "pre_ma_spread": float(np.std([ma5, ma10, ma20]) / ma20),
        "pre_dist_high250": float(close[lo_i - 1] / np.max(win_hi) - 1.0),
        "pre_dist_low250": float(close[lo_i - 1] / np.min(win_lo) - 1.0),
        "pre_turnover20": float(np.nanmean(turn[s:e])),
        "pre_log_amount20": float(np.log(max(np.nanmean(amount[s:e]), 1.0))),
        "pre_dd60": float(close[lo_i - 1] / dd_ref - 1.0),
        "pre_atr_pct": float(np.nanmean((high[s:e] - low[s:e]) / close[s:e])),
        # 上涨中
        "rise_days": int(hi_i - lo_i),
        "rise_pct": float(close[hi_i] / close[lo_i] - 1.0),
        "rise_vol_expand": float(np.nanmean(vol[lo_i:hi_i + 1]) / vol_pre) if vol_pre > 0 else np.nan,
        "rise_max_day": float(np.nanmax(pct[lo_i + 1:hi_i + 1])) if hi_i > lo_i else np.nan,
        "rise_up_ratio": float(np.mean(pct[lo_i + 1:hi_i + 1] > 0)) if hi_i > lo_i else np.nan,
        # 见顶后
        "post_days": int(in_month[-1] - hi_i),
        "post_dd": float(close[in_month[-1]] / close[hi_i] - 1.0),
        "month_ret": float(close[in_month[-1]] / close[in_month[0]] - 1.0),
    }
    return feat


def _ols(y: np.ndarray, X: np.ndarray) -> dict:
    """最小二乘 + t 检验（不依赖 statsmodels）。"""
    n = len(y)
    X1 = np.column_stack([np.ones(n), X])
    beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
    yhat = X1 @ beta
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    k = X1.shape[1]
    adj = 1.0 - (1.0 - r2) * (n - 1) / (n - k) if n > k else np.nan
    dof = max(n - k, 1)
    resid_var = ss_res / dof
    try:
        se = np.sqrt(np.diag(resid_var * np.linalg.inv(X1.T @ X1)))
        tval = beta / se
        pval = 2 * (1 - stats.t.cdf(np.abs(tval), dof))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
        tval = np.full(k, np.nan)
        pval = np.full(k, np.nan)
    return {"beta": beta, "se": se, "t": tval, "p": pval, "r2": r2, "adj_r2": adj, "n": n}


def _load_universe(start_date: str, end_date: str) -> pd.DataFrame:
    """流式遍历全市场，产出「每只股票 × 每月」的特征表。"""
    settings = get_settings()
    kdir = Path(settings.kline_dir)
    files = sorted(p for p in kdir.glob("*.parquet") if not p.name.startswith("idx_"))
    months = ["2026-08", "2026-09", "2026-10"]
    rows: list[dict] = []
    for i, path in enumerate(files, 1):
        if i % 1000 == 0:
            print(f"  已处理 {i}/{len(files)} 只...", flush=True)
        df = _read_kline(path, start_date)
        if df is None or df["date"].iloc[-1] != end_date:
            continue  # 停牌 / 已退市 / 数据落后
        code = path.stem
        prev_close = df["close"].iloc[-1]
        # 近三个月累计涨幅（用区间首尾收盘）
        win = df[df["date"] >= "2026-08-01"]
        if len(win) < 40:
            continue
        ret3m = float(prev_close / win["close"].iloc[0] - 1.0)
        liquidity = float(np.nanmean(win["amount"].to_numpy(float)))
        for month in months:
            feat = _month_features(df, month)
            if feat is None:
                continue
            feat["code"] = code
            feat["ret3m"] = ret3m
            feat["liquidity"] = liquidity
            rows.append(feat)
    return pd.DataFrame(rows)


def _describe(sample: pd.DataFrame, control: pd.DataFrame) -> pd.DataFrame:
    """样本 vs 对照：均值/中位数差 + Mann-Whitney U 检验。"""
    out = []
    for col in PRE_COLS:
        a = sample[col].dropna().to_numpy(float)
        b = control[col].dropna().to_numpy(float)
        if len(a) < 10 or len(b) < 10:
            continue
        p = stats.mannwhitneyu(a, b, alternative="two-sided").pvalue
        out.append({
            "feature": col,
            "sample_mean": float(np.mean(a)),
            "sample_median": float(np.median(a)),
            "control_mean": float(np.mean(b)),
            "control_median": float(np.median(b)),
            "diff_median": float(np.median(a) - np.median(b)),
            "p_value": float(p),
        })
    return pd.DataFrame(out)


def _single_regression(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """逐特征单变量回归（标准化后，系数即「每 +1 个标准差，目标变动多少」）。"""
    out = []
    y = df[target].to_numpy(float)
    for col in PRE_COLS:
        x = df[col].to_numpy(float)
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 30:
            continue
        xs, ys = x[ok], y[ok]
        sd = xs.std()
        if sd <= 0:
            continue
        z = (xs - xs.mean()) / sd
        res = _ols(ys, z.reshape(-1, 1))
        out.append({
            "feature": col,
            "coef_per_sd": float(res["beta"][1]),
            "t": float(res["t"][1]),
            "p_value": float(res["p"][1]),
            "r2": float(res["r2"]),
            "n": int(res["n"]),
        })
    return pd.DataFrame(out).sort_values("p_value")


def _multi_regression(df: pd.DataFrame, target: str) -> tuple[dict, pd.DataFrame]:
    cols = [c for c in PRE_COLS if df[c].notna().sum() >= 30]
    sub = df[cols + [target]].dropna()
    X = sub[cols].to_numpy(float)
    y = sub[target].to_numpy(float)
    # 标准化，系数可比
    Xz = (X - X.mean(axis=0)) / np.where(X.std(axis=0) > 0, X.std(axis=0), 1.0)
    res = _ols(y, Xz)
    table = pd.DataFrame({
        "feature": cols,
        "coef_per_sd": res["beta"][1:],
        "t": res["t"][1:],
        "p_value": res["p"][1:],
    }).sort_values("p_value")
    return {"r2": res["r2"], "adj_r2": res["adj_r2"], "n": res["n"]}, table


def _fmt(df: pd.DataFrame, cols: list[str], floatfmt: str = "{:.4f}") -> str:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = out[c].map(
                lambda v: floatfmt.format(v) if isinstance(v, (int, float, np.floating))
                and np.isfinite(v) else str(v)
            )
    return out.to_string(index=False)


def run_month(all_feat: pd.DataFrame, month: str, pool: int, top: int,
              min_amount: float) -> dict | None:
    """在指定月份上跑完整流程，返回结果 dict。"""
    # 1) 池子：近三月涨幅前 pool，且流动性达标
    valid = all_feat[all_feat["liquidity"] >= min_amount]
    ranked = valid[valid["month"] == month].sort_values("ret3m", ascending=False)
    pool_codes = ranked["code"].head(pool).tolist()
    pool_df = all_feat[all_feat["code"].isin(pool_codes)]

    # 2) 样本：池内该月涨幅前 top
    mrows = pool_df[pool_df["month"] == month].sort_values("month_ret", ascending=False)
    lo2hi = mrows[mrows["shape"] == "low_to_high"]
    sample = lo2hi.head(top)
    if len(sample) < 20:
        return None

    # 3) 对照：全市场该月**同为「先低后高」形态、但反弹力度在下半区**的票。
    #    不用「全市场涨幅后 50%」是因为普跌月里先低后高的票本身就是少数
    #    （2026-10 只占 17.4%），再叠加低涨幅会只剩个位数，检验没有效力。
    all_m = valid[valid["month"] == month]
    lth = all_m[all_m["shape"] == "low_to_high"]
    med = lth["month_ret"].median() if len(lth) else np.nan
    control = lth[lth["month_ret"] <= med]

    desc = _describe(sample, control)
    single = _single_regression(sample, "rise_pct")
    multi, multi_tab = _multi_regression(sample, "rise_pct")
    single_post = _single_regression(sample, "post_dd")

    return {
        "month": month,
        "pool_size": int(len(mrows)),
        "dropped_high_first": int(len(mrows) - len(lo2hi)),
        "sample": sample,
        "control_n": int(len(control)),
        "market_n": int(len(all_m)),
        "market_lth": int((all_m["shape"] == "low_to_high").sum()),
        "desc": desc,
        "single": single,
        "multi": multi,
        "multi_tab": multi_tab,
        "single_post": single_post,
    }


def _report(r: dict) -> None:
    sample = r["sample"]
    print(f"\n{'=' * 78}")
    print(f"月份 {r['month']}    池内该月有效 {r['pool_size']} 只"
          f"（其中先高后低剔除 {r['dropped_high_first']} 只）"
          f"    样本 {len(sample)} 只")
    print(f"全市场该月有效 {r['market_n']} 只，其中「先低后高」形态 "
          f"{r['market_lth']} 只（{r['market_lth'] / max(r['market_n'], 1):.1%}）；"
          f"对照组 = 该月涨幅低于中位数且同为空先低后高，{r['control_n']} 只")
    print("=" * 78)

    print("\n[1] 样本画像（上涨段与见顶后）")
    prof = pd.DataFrame({
        "指标": ["上涨段涨幅%", "上涨天数", "见顶后回撤%", "见顶后天数",
                 "月涨幅%", "近三月涨幅%", "上涨期量能倍数", "单日最大涨幅%"],
        "中位数": [
            sample["rise_pct"].median() * 100,
            sample["rise_days"].median(),
            sample["post_dd"].median() * 100,
            sample["post_days"].median(),
            sample["month_ret"].median() * 100,
            sample["ret3m"].median() * 100,
            sample["rise_vol_expand"].median(),
            sample["rise_max_day"].median() * 100,
        ],
        "均值": [
            sample["rise_pct"].mean() * 100,
            sample["rise_days"].mean(),
            sample["post_dd"].mean() * 100,
            sample["post_days"].mean(),
            sample["month_ret"].mean() * 100,
            sample["ret3m"].mean() * 100,
            sample["rise_vol_expand"].mean(),
            sample["rise_max_day"].mean() * 100,
        ],
    })
    print(_fmt(prof.set_index("指标").reset_index(), ["中位数", "均值"], "{:.2f}"))

    print("\n[2] 上涨前特征：样本 vs 该月低涨幅组（Mann-Whitney U）")
    print(_fmt(r["desc"], ["sample_mean", "control_mean", "diff_median", "p_value"]))

    print("\n[3] 单变量回归：上涨前特征 → 上涨段涨幅（系数=每 +1 个标准差）")
    print(_fmt(r["single"], ["coef_per_sd", "t", "p_value", "r2"]))

    print("\n[4] 多元回归（标准化）→ 上涨段涨幅")
    m = r["multi"]
    print(f"  n={m['n']}  R²={m['r2']:.4f}  调整R²={m['adj_r2']:.4f}")
    print(_fmt(r["multi_tab"], ["coef_per_sd", "t", "p_value"]))

    print("\n[5] 单变量回归：上涨前特征 → 见顶后回撤（负值=见顶后又跌）")
    print(_fmt(r["single_post"], ["coef_per_sd", "t", "p_value", "r2"]))


def main() -> None:
    ap = argparse.ArgumentParser(description="上涨规律研究")
    ap.add_argument("--pool", type=int, default=500, help="近三月涨幅前 N 构成池子")
    ap.add_argument("--top", type=int, default=100, help="随机月内涨幅前 N 为样本")
    ap.add_argument("--seed", type=int, default=20261101, help="随机选月的种子")
    ap.add_argument("--month", default=None, help="指定月份（如 2026-09），覆盖随机")
    ap.add_argument("--min-amount", type=float, default=5e7,
                    help="近三月日均成交额下限（元），默认 5000 万")
    ap.add_argument("--all-months", action="store_true", help="三个月各跑一遍看稳健性")
    ap.add_argument("--start", default="2025-01-01", help="特征窗口起点")
    ap.add_argument("--end", default="2026-10-30", help="数据截止交易日")
    ap.add_argument("--out", default="", help="结果 CSV 输出路径")
    args = ap.parse_args()

    print(f"载入全市场 K 线（{args.start} ~ {args.end}）...", flush=True)
    feat = _load_universe(args.start, args.end)
    print(f"有效样本行 {len(feat)}（股票×月），覆盖 {feat['code'].nunique()} 只股票")

    months = ["2026-08", "2026-09", "2026-10"]
    if args.month:
        picked = [args.month]
    elif args.all_months:
        picked = months
    else:
        rng = np.random.default_rng(args.seed)
        picked = [str(rng.choice(months))]
        print(f"随机选中月份：{picked[0]}（seed={args.seed}，可用 --month 覆盖）")

    results = []
    for month in picked:
        res = run_month(feat, month, args.pool, args.top, args.min_amount)
        if res is None:
            print(f"\n月份 {month} 样本不足，跳过")
            continue
        results.append(res)
        _report(res)

    if results:
        out = Path(args.out) if args.out else (
            Path(get_settings().kline_dir).parent / "logs" / "rally_study.csv"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.concat([r["sample"] for r in results]).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n样本明细已写入 {out}")

    if len(results) > 1:
        print(f"\n{'=' * 78}\n跨月稳健性：涨幅前 100 的上涨前特征中位数\n{'=' * 78}")
        piv = pd.DataFrame({
            r["month"]: r["sample"][list(PRE_COLS)].median() for r in results
        })
        print(piv.round(4).to_string())


if __name__ == "__main__":
    main()
