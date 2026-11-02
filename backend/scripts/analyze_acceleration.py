"""近一年「突然加速启动」个股的启动前 / 上涨后特征分析。

这个脚本直接读 SAS 的**本地 K 线缓存**（`kline_cache`），不需要联网，因此只能在
已经同步过 K 线的机器（你的本地 Windows 机 / 真机）上跑。它解决的问题正是：
「近一年来突然启动加速上涨的股票，启动前和上涨后各有什么特点？」

方法论
------
对股票池里每一只满足历史长度要求的个股，用滚动窗口找「加速启动事件」：

  启动日 t 需同时满足：
    1. 启动前 25 日（PRE）：横盘/微幅波动 —— 区间累计收益 ∈ [-12%, +6%]，
       且日收益波动(标准差) ≤ 阈值（波动率压缩 / 地量）；
    2. 启动后 20 日（POST）：暴力拉升 —— 累计收益 ≥ 25%，日均收益 ≥ 1%，
       且成交量相对基线放大 ≥ 1.3 倍（放量确认）。

即「从安静 → 暴起」才是「突然加速」，已在高位的趋势股不算。

对命中的每一事件，提取两组特征：
    · 启动前（PRE + 更长基线 120 日）：横盘度、波动率压缩、缩量比、均线粘合度、
      距一年高点的距离（低位/洗盘充分度）、启动前最大回撤；
    · 上涨后（POST 20日 / POST2 40日 / 前向 60日）：累计涨幅、放量倍数、波动率放大、
      见顶所需天数、见顶后最大回撤、40 日后是否守住涨幅。

最后对所有事件做中位数/分位聚合，得到「典型画像」并导出 CSV。

用法（backend 目录）：：

    # 仅用本地已缓存的 K 线（默认，离线、快），分析全池
    .venv\\Scripts\\python.exe scripts/analyze_acceleration.py

    # 先小样本试水（只看前 200 只，且有本地缓存的）
    .venv\\Scripts\\python.exe scripts/analyze_acceleration.py --limit 200

    # 允许联网补齐缺失的 K 线（慢，可能触发数据源限流）
    .venv\\Scripts\\python.exe scripts/analyze_acceleration.py --fetch

    # 只看近 6 个月、打印前 15 个样例、输出到指定路径
    .venv\\Scripts\\python.exe scripts/analyze_acceleration.py --months 6 --top 15 --out D:/accel.csv

    # 内嵌合成数据自测（不依赖任何外部数据，验证检测逻辑正确）
    .venv\\Scripts\\python.exe scripts/analyze_acceleration.py --selftest

可调阈值（见下方 --help）：`--pre-vol-max` `--post-ret-min` `--vol-expand-min` 等。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data.cache import kline_cache  # noqa: E402


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
@dataclass
class Params:
    base: int = 120          # 基线窗口（用于成交量/换手率基准）
    pre: int = 25            # 启动前安静窗口
    post: int = 20           # 启动后暴力拉升窗口
    post2: int = 40          # 延长观察窗口
    fwd: int = 60            # 前向窗口（找顶 / 见顶后回撤）
    pre_ret_lo: float = -0.12
    pre_ret_hi: float = 0.06
    pre_vol_max: float = 0.025   # 日收益波动上限（波动率压缩）
    post_ret_min: float = 0.25   # 启动 20 日累计涨幅下限
    post_daily_min: float = 0.01  # 启动期日均收益下限（确实在涨）
    vol_expand_min: float = 1.3   # 放量确认倍数
    gap: int = 40            # 同一只票两次事件的最小间隔（交易日）


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_klines(code: str, fetch: bool) -> pd.DataFrame | None:
    """优先读本地缓存；`fetch=True` 时缓存缺失再回源（联网）。"""
    df = kline_cache.get(code)
    if df is None or len(df) < 60:
        if not fetch:
            return None
        try:
            from app.data.service import get_kline_cached
            df = get_kline_cached(code)
        except Exception:
            return None
    if df is None or len(df) < 60:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 统一数值列
    for c in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "close" not in df.columns or df["close"].isna().all():
        return None
    return df


def _ma(series: pd.Series, n: int) -> float:
    if len(series) < n:
        return float("nan")
    return float(series.tail(n).mean())


# ---------------------------------------------------------------------------
# 事件检测 + 特征
# ---------------------------------------------------------------------------
def detect_events(df: pd.DataFrame, p: Params, recent_start_idx: int) -> list[dict]:
    """返回该票命中的加速启动事件列表（含全部特征）。"""
    n = len(df)
    close = df["close"]
    vol = df["volume"] if "volume" in df.columns else None
    turn = df["turnover_rate"] if "turnover_rate" in df.columns else None
    ret = close.pct_change()

    # 扫描区间：仅在「近一年」内寻找启动日，但基线/前窗可用更早数据
    t_start = max(recent_start_idx, p.base + p.pre)
    t_end = n - (p.post2 + p.fwd)
    if t_end <= t_start:
        return []

    events: list[dict] = []
    last_t = -10 ** 9
    for t in range(t_start, t_end + 1):
        if t - last_t < p.gap:
            continue  # 同一波行情只取一处

        pre_slice = close.iloc[t - p.pre: t]
        base_slice = close.iloc[t - p.pre - p.base: t - p.pre]
        post_slice = close.iloc[t: t + p.post]
        post2_slice = close.iloc[t: t + p.post2]
        fwd_slice = close.iloc[t: t + p.post2 + p.fwd]

        if len(pre_slice) < p.pre or len(base_slice) < p.base or len(post_slice) < p.post:
            continue

        # ---- 启动前特征 ----
        pre_ret = pre_slice.iloc[-1] / pre_slice.iloc[0] - 1
        pre_ret_series = pre_slice.pct_change().dropna()
        pre_vol = float(pre_ret_series.std()) if len(pre_ret_series) > 3 else float("nan")
        # 启动前最大回撤
        pre_run = pre_slice.cummax()
        pre_dd = float((pre_slice / pre_run - 1).min())
        # 缩量比（成交量）：前窗均值 / 基线均值
        if vol is not None:
            base_vol = float(vol.iloc[t - p.pre - p.base: t - p.pre].mean())
            pre_vol_mean = float(vol.iloc[t - p.pre: t].mean())
            post_vol_mean = float(vol.iloc[t: t + p.post].mean())
            vol_ratio = pre_vol_mean / base_vol if base_vol > 0 else float("nan")
            vol_expand = post_vol_mean / base_vol if base_vol > 0 else float("nan")
        else:
            vol_ratio = float("nan")
            vol_expand = float("nan")
        # 换手率缩量（若有）
        if turn is not None:
            base_turn = float(turn.iloc[t - p.pre - p.base: t - p.pre].mean(skipna=True))
            pre_turn = float(turn.iloc[t - p.pre: t].mean(skipna=True))
            turn_ratio = pre_turn / base_turn if base_turn and base_turn > 0 else float("nan")
        else:
            turn_ratio = float("nan")
        # 均线粘合度（t-1 处 MA5/10/20/60 的变异系数，越小越粘合）
        mas = [_ma(close.iloc[:t], m) for m in (5, 10, 20, 60)]
        mas = [m for m in mas if not np.isnan(m)]
        if len(mas) >= 2 and np.mean(mas) > 0:
            ma_conv = float(np.std(mas) / np.mean(mas))
        else:
            ma_conv = float("nan")
        # 距一年高点距离（低位 / 洗盘充分度）
        win_hi = float(close.iloc[max(0, t - 250): t].max())
        dist_high = float(close.iloc[t - 1] / win_hi - 1) if win_hi > 0 else float("nan")

        # ---- 启动后特征 ----
        post_ret20 = post_slice.iloc[-1] / post_slice.iloc[0] - 1
        post_ret40 = post2_slice.iloc[-1] / post2_slice.iloc[0] - 1
        post_daily = float(post_slice.pct_change().dropna().mean())
        post_vol = float(post_slice.pct_change().dropna().std())

        # 前向窗口找顶
        fwd_arr = fwd_slice.to_numpy()
        peak_rel = int(np.argmax(fwd_arr))
        days_to_peak = peak_rel
        peak_ret = fwd_arr[peak_rel] / post_slice.iloc[0] - 1
        end_ret = fwd_arr[-1] / post_slice.iloc[0] - 1
        after = fwd_arr[peak_rel:]
        dd_after = float((after.min() / fwd_arr[peak_rel] - 1)) if len(after) else 0.0
        still_up = bool(fwd_arr[-1] > post_slice.iloc[0])

        # ---- 加速启动判定 ----
        if not (p.pre_ret_lo <= pre_ret <= p.pre_ret_hi):
            continue
        if np.isnan(pre_vol) or pre_vol > p.pre_vol_max:
            continue
        if post_ret20 < p.post_ret_min:
            continue
        if post_daily < p.post_daily_min:
            continue
        if np.isnan(vol_expand) or vol_expand < p.vol_expand_min:
            continue

        events.append({
            "launch_date": df["date"].iloc[t].strftime("%Y-%m-%d"),
            "pre_cum_ret": round(pre_ret, 4),
            "pre_vol": round(pre_vol, 5),
            "pre_max_dd": round(pre_dd, 4),
            "vol_ratio_pre_vs_base": round(vol_ratio, 3) if not np.isnan(vol_ratio) else None,
            "turn_ratio_pre_vs_base": round(turn_ratio, 3) if not np.isnan(turn_ratio) else None,
            "ma_conv_t_minus1": round(ma_conv, 4) if not np.isnan(ma_conv) else None,
            "dist_from_1y_high": round(dist_high, 4) if not np.isnan(dist_high) else None,
            "post_ret_20d": round(post_ret20, 4),
            "post_ret_40d": round(post_ret40, 4),
            "post_daily_mean": round(post_daily, 4),
            "post_vol": round(post_vol, 5),
            "vol_expand": round(vol_expand, 3) if not np.isnan(vol_expand) else None,
            "days_to_peak": days_to_peak,
            "peak_ret": round(peak_ret, 4),
            "dd_after_peak": round(dd_after, 4),
            "ret_held_40d": round(end_ret, 4),
            "still_up_40d": still_up,
        })
        last_t = t
    return events


# ---------------------------------------------------------------------------
# 聚合与输出
# ---------------------------------------------------------------------------
def summarize(events: list[dict], out_csv: str | None, top: int):
    if not events:
        print("\n未检测到符合条件的「突然加速启动」事件。"
              "可能原因：本地缓存的 K 线不足 / 阈值过严。\n"
              "建议：① 真机上先让 SAS 同步过 K 线；② 放宽 --pre-vol-max / --post-ret-min；"
              "③ 加 --fetch 联网补齐。")
        return

    df = pd.DataFrame(events)
    print(f"\n=== 共检测到 {len(df)} 个「突然加速启动」事件 ===\n")

    num_cols = [
        "pre_cum_ret", "pre_vol", "pre_max_dd", "vol_ratio_pre_vs_base",
        "turn_ratio_pre_vs_base", "ma_conv_t_minus1", "dist_from_1y_high",
        "post_ret_20d", "post_ret_40d", "post_daily_mean", "post_vol",
        "vol_expand", "days_to_peak", "peak_ret", "dd_after_peak", "ret_held_40d",
    ]
    print("【启动前 · 典型画像（中位数）】")
    pre_keys = ["pre_cum_ret", "pre_vol", "pre_max_dd", "vol_ratio_pre_vs_base",
                "turn_ratio_pre_vs_base", "ma_conv_t_minus1", "dist_from_1y_high"]
    for k in pre_keys:
        s = df[k].dropna()
        if len(s):
            print(f"  {k:24s} 中位 {s.median():.4f}   均值 {s.mean():.4f}   "
                  f"p25 {s.quantile(.25):.4f}  p75 {s.quantile(.75):.4f}")

    print("\n【上涨后 · 典型画像（中位数）】")
    post_keys = ["post_ret_20d", "post_ret_40d", "post_daily_mean", "post_vol",
                 "vol_expand", "days_to_peak", "peak_ret", "dd_after_peak", "ret_held_40d"]
    for k in post_keys:
        s = df[k].dropna()
        if len(s):
            print(f"  {k:24s} 中位 {s.median():.4f}   均值 {s.mean():.4f}   "
                  f"p25 {s.quantile(.25):.4f}  p75 {s.quantile(.75):.4f}")

    print("\n【上涨后结局分布】")
    print(f"  40 日后仍高于启动价的比例 : {df['still_up_40d'].mean()*100:.1f}%")
    print(f"  见顶后最大回撤中位数      : {df['dd_after_peak'].median()*100:.1f}%"
          f"（p75 = {df['dd_after_peak'].quantile(.75)*100:.1f}%）")
    print(f"  从启动到见顶中位天数      : {df['days_to_peak'].median():.0f} 天")
    print(f"  启动 20 日涨幅中位数      : {df['post_ret_20d'].median()*100:.1f}%")

    if top and "code" in df.columns:
        print(f"\n【样例（前 {top} 个，按 20 日涨幅降序）】")
        ex = df.sort_values("post_ret_20d", ascending=False).head(top)
        cols = ["code", "name", "launch_date", "pre_cum_ret", "dist_from_1y_high",
                "vol_expand", "post_ret_20d", "days_to_peak", "dd_after_peak"]
        print(ex[cols].to_string(index=False))

    if out_csv:
        df.to_csv(out_csv, index=False, encoding="utf-8-sig")
        print(f"\n已导出全部事件到：{out_csv}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="近一年突然加速启动个股的前后特征分析")
    ap.add_argument("--fetch", action="store_true", help="缓存缺失时联网补齐 K 线（慢）")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描前 N 只股票（0=全池）")
    ap.add_argument("--months", type=int, default=12, help="只看近 N 个月的启动（默认 12）")
    ap.add_argument("--top", type=int, default=10, help="打印前 N 个样例（0=不打印）")
    ap.add_argument("--out", default=None, help="事件 CSV 输出路径")
    ap.add_argument("--pre-vol-max", type=float, default=Params.pre_vol_max)
    ap.add_argument("--post-ret-min", type=float, default=Params.post_ret_min)
    ap.add_argument("--vol-expand-min", type=float, default=Params.vol_expand_min)
    ap.add_argument("--selftest", action="store_true", help="用合成数据验证检测逻辑后退出")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    from app.data.service import get_stock_pool
    pool = get_stock_pool()
    codes = pool["code"].tolist()
    names = dict(zip(pool["code"], pool.get("name", pool["code"])))
    if args.limit:
        codes = codes[:args.limit]

    p = Params(pre_vol_max=args.pre_vol_max, post_ret_min=args.post_ret_min,
               vol_expand_min=args.vol_expand_min)

    cutoff = pd.Timestamp.now() - pd.Timedelta(days=args.months * 31)
    all_events: list[dict] = []
    scanned = 0
    t0 = time.time()
    for code in codes:
        df = load_klines(code, args.fetch)
        if df is None or len(df) < p.base + p.pre + p.post2 + p.fwd:
            continue
        # 近 N 个月起始索引
        mask = df["date"] >= cutoff
        if not mask.any():
            continue
        recent_start_idx = int(np.argmax(mask.to_numpy()))
        evs = detect_events(df, p, recent_start_idx)
        for e in evs:
            e["code"] = code
            e["name"] = names.get(code, code)
        all_events.extend(evs)
        scanned += 1
        if scanned % 200 == 0:
            print(f"  已扫描 {scanned} 只，命中 {len(all_events)} 事件 …", flush=True)

    print(f"\n扫描完成：{scanned} 只有效历史，耗时 {time.time()-t0:.1f}s")
    summarize(all_events, args.out, args.top)


# ---------------------------------------------------------------------------
# 合成数据自测
# ---------------------------------------------------------------------------
def _selftest():
    """构造「横盘 200 日 → 暴拉 20 日 → 见顶回落」的序列，验证能检出。"""
    rng = np.random.default_rng(0)
    # 1) 横盘 200 日：低波动，约 ±0.8% 价格噪声
    flat = 10.0 + rng.normal(0, 0.08, 200)
    # 2) 暴拉 22 日：日均 +1.8%，累计约 +43%
    ramp = flat[-1] * np.cumprod(1.0 + rng.normal(0.018, 0.01, 22))
    # 3) 见顶后回落 100 日
    fall = ramp[-1] * np.cumprod(1.0 + rng.normal(-0.004, 0.012, 100))
    close = np.concatenate([flat, ramp, fall])
    # 成交量：横盘期缩量、暴拉期放量
    vol = np.concatenate([
        np.full(200, 1.0e6),
        np.full(22, 3.0e6),
        np.full(100, 1.5e6),
    ])
    # 日期落在「近一年」窗口内（结束于今天附近），确保 recent_start_idx=0
    dates = pd.bdate_range(end=pd.Timestamp.now(), periods=len(close))
    df = pd.DataFrame({"date": dates, "close": close, "volume": vol,
                       "turnover_rate": vol / 1e8})
    p = Params()
    evs = detect_events(df, p, 0)
    assert evs, "自测失败：未检出合成加速启动事件"
    e = evs[0]
    print("自测通过 ✅  检出事件：")
    for k, v in e.items():
        print(f"  {k}: {v}")
    assert e["post_ret_20d"] > 0.25, "涨幅异常"
    assert e["vol_expand"] >= 1.3, "放量异常"
    print("\n核心断言均满足：横盘→暴拉→放量 被正确识别为一次「突然加速启动」。")


if __name__ == "__main__":
    main()
