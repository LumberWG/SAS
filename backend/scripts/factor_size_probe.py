"""「反转 + 缩量 + 小盘」横截面月度因子选股，走 accept.py 的验收口径。

动机（记录 #41）：项目数据里近三个月涨幅前 100 的赢家，启动前是
「超跌（pre_ret60 中位 −26%）+ 低换手（4.7%）+ 小盘（日均成交 1.22 亿）」。
这三条恰好对应三个可量化的横截面因子：
  - 反转  ：过去 L 日收益越低越好（买超跌）
  - 缩量  ：过去 L 日平均换手率越低越好
  - 小盘  ：过去 L 日平均成交额越低越好（规模代理；项目 14 因子框架缺规模维度，
           且 pipeline 的 universe_size 是大票优先，故独立实现而非塞进 profiles）

实现：
  - 每 rebalance 个交易日（默认 21≈1 月）用 ≤当日数据算三因子，横截面 z-score 合成
    score = −(z_ret + z_turn + z_amt)，取 top_n 持有到下一换仓日；
  - 成交：换仓日**开盘**卖旧、开盘买新（无未来函数）；组合等权；
  - 基准：全市场等权（同换仓频率、同开盘买卖口径），比协议 universe=300 更严格；
  - 摩擦：单边滑点 0 / 0.3% 两档；
  - 验收：**复用 accept.py 的 `_segments` / `verdict`**（30 段不重叠、三条通过线）。

为什么不复用 pipeline profile：pipeline 的选股依赖 14 因子打分 + 大票 universe，
既没有规模因子、也把小盘票挡在候选池外，与本文「小盘优先」的发现直接冲突。
这里用同一套验收协议判定，策略实现保持独立、零风险。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/factor_size_probe.py
    .venv\\Scripts\\python.exe scripts/factor_size_probe.py --topn 30 --lookback 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.data.scope import is_tradable  # noqa: E402
from scripts.amount50_probe import _load_matrix, _calendar  # noqa: E402
from scripts.accept import verdict  # noqa: E402
from scripts.random_dist import _segments, _trading_dates, _stats  # noqa: E402

SLIPPAGE = (0.0, 0.003)
MIN_POSITIVE_SHARE = 60.0
WORST_TOLERANCE_PP = 5.0


def build(topn: int, lookback: int, rebalance: int, weights: tuple[float, ...],
          universe: int = 0, scope: bool = True):
    settings = get_settings()
    kline_dir = str(settings.kline_dir)
    cal = _calendar(kline_dir)
    close, open_, amount, turn, _vol, codes = _load_matrix(kline_dir, cal)
    m, n = close.shape
    cal_a = np.array(cal)
    # 项目原则：只分析沪深主板 + 创业板，剔除 ST 与巨无霸（见 app/data/scope.py）
    scope_ok = np.array([is_tradable(c)[0] for c in codes], dtype=bool) if scope else np.ones(m, bool)

    R = list(range(lookback, n - rebalance, rebalance))
    if not R:
        return None

    ret_close = np.full((m, n), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret_close[:, 1:] = close[:, 1:] / close[:, :-1] - 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        ret_buy = np.where(open_ > 0, close / open_ - 1.0, np.nan)
    ret_sell = np.full((m, n), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret_sell[:, 1:] = np.where(close[:, :-1] > 0, open_[:, 1:] / close[:, :-1] - 1.0, np.nan)

    H: dict[int, np.ndarray] = {}
    for i, t in enumerate(R):
        base = close[:, t - lookback]
        cl = close[:, t]
        retL = np.where((base > 0) & np.isfinite(base) & np.isfinite(cl), cl / base - 1.0, np.nan)
        tavg = np.nanmean(turn[:, t - lookback + 1:t + 1], axis=1)
        aavg = np.nanmean(amount[:, t - lookback + 1:t + 1], axis=1)
        valid = np.isfinite(retL) & np.isfinite(tavg) & np.isfinite(aavg)
        valid = valid & scope_ok
        if universe and universe > 0:
            # 对齐 pipeline 的 universe_size：每日成交额 TopN 大票优先池
            order = np.argsort(-np.where(np.isfinite(amount[:, t]), amount[:, t], -np.inf))
            top = min(universe, m)
            top_mask = np.zeros(m, bool)
            top_mask[order[:top]] = True
            valid = valid & top_mask
        if valid.sum() < topn:
            continue
        zr = (retL - np.nanmean(retL[valid])) / np.nanstd(retL[valid])
        zt = (tavg - np.nanmean(tavg[valid])) / np.nanstd(tavg[valid])
        za = (aavg - np.nanmean(aavg[valid])) / np.nanstd(aavg[valid])
        score = -(weights[0] * zr + weights[1] * zt + weights[2] * za)
        score = np.where(valid, score, -np.inf)
        H[i] = np.argpartition(score, -topn)[-topn:]

    H_mat = np.zeros((m, n))
    for i, t in enumerate(R):
        if i not in H:
            continue
        sell, buy = t, t + 1
        nxt = R[i + 1] if i + 1 < len(R) else n - 1
        if i == 0:
            H_mat[H[0], buy:nxt + 1] = 1
        else:
            if (i - 1) in H:
                H_mat[H[i - 1], sell:sell + 1] = 1
            H_mat[H[i], buy:nxt + 1] = 1

    buy_day = np.zeros(n, bool)
    sell_day = np.zeros(n, bool)
    for i, t in enumerate(R):
        if t + 1 < n:
            buy_day[t + 1] = True
        if i >= 1:
            sell_day[t] = True
    return {
        "ret_close": ret_close, "ret_buy": ret_buy, "ret_sell": ret_sell,
        "H_mat": H_mat, "buy_day": buy_day, "sell_day": sell_day,
        "cal_a": cal_a, "n": n,
    }


def _nav(ret_close, ret_buy, ret_sell, buy_day, sell_day, H_mat, s: float):
    r_mat = np.where(
        buy_day[None, :], (ret_buy + 1.0) / (1.0 + s) - 1.0,
        np.where(sell_day[None, :], (ret_sell + 1.0) * (1.0 - s) - 1.0, ret_close),
    )
    valid_h = (H_mat * np.isfinite(r_mat)).sum(0)
    r_combo = np.where(valid_h > 0, (r_mat * H_mat).sum(0) / valid_h, 0.0)
    rb = np.nanmean(r_mat, axis=0)
    nav_s = np.cumprod(np.where(np.isfinite(r_combo), 1.0 + r_combo, 1.0))
    nav_b = np.cumprod(np.where(np.isfinite(rb), 1.0 + rb, 1.0))
    return nav_s, nav_b


def run(topn, lookback, rebalance, weights, seed, draws, span, tag, universe=0, scope=True):
    data = build(topn, lookback, rebalance, weights, universe, scope)
    if data is None:
        raise SystemExit("样本不足，无法生成换仓日")
    cal_a = data["cal_a"]

    dates = _trading_dates()
    segs = _segments(dates, span, draws, seed)

    out = {}
    for s in SLIPPAGE:
        nav_s, nav_b = _nav(
            data["ret_close"], data["ret_buy"], data["ret_sell"],
            data["buy_day"], data["sell_day"], data["H_mat"], s,
        )
        cand, base = [], []
        for st, en in segs:
            ts = int(np.searchsorted(cal_a, st))
            te = int(np.searchsorted(cal_a, en, side="right")) - 1
            if te <= ts:
                continue
            cand.append(float(nav_s[te] / nav_s[ts] - 1.0))
            base.append(float(nav_b[te] / nav_b[ts] - 1.0))
        label = "@0" if s == 0 else "@30"
        out[label] = {
            "stats": _stats(cand), "baseline_stats": _stats(base),
            "verdict": verdict(base, cand),
        }

    # 打印
    uni_txt = f" universe=Top{universe}" if universe else " universe=全市场"
    print(f"\n### 反转+缩量+小盘 横截面（topn={topn} lookback={lookback} rebal={rebalance} "
          f"w={weights} seed={seed} {draws}段×{span}日{uni_txt}）")
    print("| 档位 | 方案中位 | 基准中位 | 配对中位差 | 差值>0占比 | 最差(方案/基准) | ① | ② | ③ | 结论 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in SLIPPAGE:
        label = "@0" if s == 0 else "@30"
        v = out[label]
        chk = v["verdict"].get("checks") or {}
        if not chk:
            print(f"| {label} | n/a | n/a | n/a | n/a | n/a | - | - | - | {v['verdict'].get('reason')} |")
            continue
        print(
            f"| {label} | {v['stats'].get('median', 0):+.2f}% | {v['baseline_stats'].get('median', 0):+.2f}% | "
            f"{chk['median_diff_ge_0']['value']:+.2f}% | {chk['positive_share_ge_60']['value']:.1f}% | "
            f"{chk['worst_not_much_worse']['value']:+.2f}% / {chk['worst_not_much_worse']['base_worst']:+.2f}% | "
            + " | ".join("通过" if c["pass"] else "**不过**" for c in chk.values())
            + f" | {'通过' if v['verdict']['ok'] else '**不采纳**'} |"
        )
    all_ok = all(v["verdict"]["ok"] for v in out.values())
    print(f"\n### 总判定：{'通过（可进入部署讨论）' if all_ok else '不采纳'}")
    print(f"（基准=全市场等权；判定线：中位差≥0、差值>0占比≥60%、最差段容差 {WORST_TOLERANCE_PP}pp）")

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{tag}" if tag else ""
    outp = os.path.join(log_dir, f"factor_size_probe{suffix}.json")
    with open(outp, "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "params": {"topn": topn, "lookback": lookback, "rebalance": rebalance,
                       "weights": list(weights), "universe": universe,
                       "seed": seed, "draws": draws, "span": span},
            "passed": all_ok, "variants": out,
        }, fh, ensure_ascii=False, indent=1, default=float)
    print(f"\n结果已保存：{os.path.abspath(outp)}")
    return all_ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--topn", type=int, default=50)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--rebalance", type=int, default=21)
    p.add_argument("--w-rev", type=float, default=1.0)
    p.add_argument("--w-turn", type=float, default=1.0)
    p.add_argument("--w-size", type=float, default=1.0)
    p.add_argument("--universe", type=int, default=0,
                   help="每日成交额 TopN 候选池；0=全市场（对齐 pipeline 的 universe_size，默认500）")
    p.add_argument("--seed", default="20261029", help="逗号分隔多个种子；协议要求全部通过")
    p.add_argument("--draws", type=int, default=30)
    p.add_argument("--span", type=int, default=21)
    p.add_argument("--tag", default="")
    p.add_argument("--all-market", action="store_true",
                   help="关闭范围过滤（对照：含科创板/北交所/ST/巨无霸）")
    a = p.parse_args()
    seeds = [int(s) for s in a.seed.split(",") if s.strip()]
    oks = []
    for sd in seeds:
        oks.append(run(a.topn, a.lookback, a.rebalance, (a.w_rev, a.w_turn, a.w_size),
                        sd, a.draws, a.span, a.tag, a.universe, not a.all_market))
    if len(seeds) > 1:
        print(f"\n### 多种子总判定：{'通过（可进入部署讨论）' if all(oks) else '不采纳'} "
              f"（{sum(oks)}/{len(oks)} 种子通过）")


if __name__ == "__main__":
    main()
