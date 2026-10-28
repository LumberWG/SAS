"""近三个月涨幅前 100 股票的「启动前特征」分析（事后赢家回溯）。

目的：回到项目数据本身，看最近三个月真正涨出来的赢家，在**启动前**长什么样，
验证项目已有的因子方向（记录 #22：1 个月尺度「空/缩量/超跌」因子 IC 为正）。

口径：
  - 区间：start ~ end（默认近三个月 2026-07-28 ~ 2026-10-28）。
  - 区间涨幅：区间内首个有数据的收盘价 → 末个有数据的收盘价；排除区间内数据不全
    （首/尾缺失、finite 占比 <80%）以及启动前 pre 窗口无数据（新股/复牌晚）的票。
  - 启动前特征（pre 窗口 = 区间开始前 60 个交易日）：
        pre_ret60 : 区间首日价 / 60 日前价 − 1（**超跌程度**，越负越超跌 → 反转因子看多）
        pre_turn  : 启动前 60 日平均换手率（比例，**缩量** → 越低越好）
        pre_amt   : 启动前 60 日平均日成交额（元，**市值代理**，越低越小盘）
  - 区间内特征：in_turn / in_amt（量能是否放大）、区间最大回撤 maxdd。
  - 因子选中检验：在 start 日横截面，把全体股票的 pre_ret60（反转）与 pre_turn（缩量）
    排分位，看前 100 赢家落在几分位 —— 若赢家集中在「更超跌 / 更低换手」的低分位，
    说明项目因子方向对「找赢家」有效。

不做复权（项目缓存是未复权 OHLCV）；三个月区间分红跳空影响有限，规律看相对排序。

用法::

    .venv\\Scripts\\python.exe scripts/top100_probe.py
    .venv\\Scripts\\python.exe scripts/top100_probe.py --start 2026-04-28 --topn 200
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


def _board(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "北交所"
    return "其他"


def _maxdd(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return float("nan")
    peak = np.maximum.accumulate(x)
    return float((x / peak - 1.0).min())


def analyze(start: str, end: str, topn: int, pre: int = 60, all_market: bool = False) -> dict:
    settings = get_settings()
    kline_dir = str(settings.kline_dir)
    cal = _calendar(kline_dir)
    close, _open, amount, turn, _vol, codes = _load_matrix(kline_dir, cal)
    codes_arr = np.array(codes)
    n = len(cal)
    cal_a = np.array(cal)
    t_end = int(np.searchsorted(cal_a, end, side="right")) - 1
    t_start = int(np.searchsorted(cal_a, start))
    t0 = t_start - pre
    if t0 < 0:
        raise SystemExit("start 太早，pre 窗口超出数据起点")

    seg = slice(t_start, t_end + 1)
    cl = close[:, seg]
    fin = np.isfinite(cl)
    ndays = cl.shape[1]
    first_idx = np.argmax(fin, axis=1)
    last_idx = (ndays - 1) - np.argmax(fin[:, ::-1], axis=1)
    data_valid = (
        fin.any(axis=1)
        & (first_idx <= 5)          # 起点不是中途复牌
        & (last_idx >= ndays - 6)   # 末尾有数据
        & (fin.sum(axis=1) >= ndays * 0.8)
        & np.isfinite(close[:, t0])  # 启动前 pre 窗口有数据（排除新股/复牌晚）
    )
    if all_market:
        valid = data_valid
        excluded_by_scope = 0
    else:
        # 项目原则：只分析沪深主板 + 创业板，剔除 ST 与巨无霸（见 app/data/scope.py）
        scope_ok = np.array([is_tradable(c)[0] for c in codes_arr], dtype=bool)
        valid = data_valid & scope_ok
        excluded_by_scope = int((data_valid & ~scope_ok).sum())
    idx = np.where(valid)[0]
    p0 = cl[idx, first_idx[idx]]
    p1 = cl[idx, last_idx[idx]]
    ret = p1 / p0 - 1.0

    pre_price = close[idx, t0]
    pre_ret60 = p0 / pre_price - 1.0
    pre_turn = np.nanmean(turn[idx, t0:t_start], axis=1)
    pre_amt = np.nanmean(amount[idx, t0:t_start], axis=1)
    in_turn = np.nanmean(turn[idx, t_start:t_end + 1], axis=1)
    in_amt = np.nanmean(amount[idx, t_start:t_end + 1], axis=1)

    order = np.argsort(ret)[::-1][:topn]
    win = idx[order]
    w_ret, w_p0, w_p1 = ret[order], p0[order], p1[order]
    w_pre_ret60, w_pre_turn, w_pre_amt = pre_ret60[order], pre_turn[order], pre_amt[order]
    w_in_turn, w_in_amt = in_turn[order], in_amt[order]

    # 全体（valid）分布，用于对比 + 因子分位检验
    all_pre_ret60 = pre_ret60
    all_pre_turn = pre_turn
    all_pre_amt = pre_amt

    def _pct(arr: np.ndarray, vals: np.ndarray) -> float:
        a = arr[np.isfinite(arr)]
        v = vals[np.isfinite(vals)]
        if a.size == 0 or v.size == 0:
            return float("nan")
        return float(np.mean([(a < x).mean() for x in v])) * 100.0

    def _med(arr: np.ndarray) -> float:
        a = arr[np.isfinite(arr)]
        return float(np.median(a)) if a.size else float("nan")

    # 赢家最大回撤（仅前 topn）
    w_maxdd = np.array([_maxdd(cl[i, :]) for i in win])

    board_cnt: dict[str, int] = {}
    for c in codes_arr[win]:
        board_cnt[_board(c)] = board_cnt.get(_board(c), 0) + 1
    price_band = {"<5": 0, "5-20": 0, ">20": 0}
    for v in w_p0:
        if v < 5:
            price_band["<5"] += 1
        elif v <= 20:
            price_band["5-20"] += 1
        else:
            price_band[">20"] += 1

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {"start": start, "end": end, "pre": pre, "topn": topn,
                   "scope_filter": not all_market, "excluded_by_scope": excluded_by_scope,
                   "actual_winners": int(len(win)),
                   "universe_valid": int(valid.sum()), "total_stocks": int(len(codes_arr))},
        "winners_summary": {
            "ret_med": round(_med(w_ret) * 100, 1), "ret_mean": round(float(np.mean(w_ret)) * 100, 1),
            "ret_min": round(float(np.min(w_ret)) * 100, 1),
            "p0_med": round(_med(w_p0), 2), "p1_med": round(_med(w_p1), 2),
            "pre_ret60_med": round(_med(w_pre_ret60) * 100, 1),
            "pre_turn_med": round(_med(w_pre_turn) * 100, 2),
            "pre_amt_med_yi": round(_med(w_pre_amt) / 1e8, 2),
            "in_turn_med": round(_med(w_in_turn) * 100, 2),
            "in_amt_med_yi": round(_med(w_in_amt) / 1e8, 2),
            "turn_multiplier": round(_med(w_in_turn) / (_med(w_pre_turn) + 1e-9), 1),
            "maxdd_med": round(_med(w_maxdd) * 100, 1),
            "board": board_cnt, "price_band": price_band,
        },
        "factor_rank_test": {
            "winner_pre_ret60_pctl": round(_pct(all_pre_ret60, w_pre_ret60), 1),
            "winner_pre_turn_pctl": round(_pct(all_pre_turn, w_pre_turn), 1),
            "winner_pre_amt_pctl": round(_pct(all_pre_amt, w_pre_amt), 1),
            "all_pre_ret60_med": round(_med(all_pre_ret60) * 100, 1),
            "all_pre_turn_med": round(_med(all_pre_turn) * 100, 2),
            "all_pre_amt_med_yi": round(_med(all_pre_amt) / 1e8, 2),
        },
        "top_list": [
            {"code": codes_arr[win[i]], "board": _board(codes_arr[win[i]]),
             "ret": round(float(w_ret[i]) * 100, 1), "p0": round(float(w_p0[i]), 2),
             "pre_ret60": round(float(w_pre_ret60[i]) * 100, 1),
             "pre_turn": round(float(w_pre_turn[i]) * 100, 2),
             "pre_amt_yi": round(float(w_pre_amt[i]) / 1e8, 2),
             "in_turn": round(float(w_in_turn[i]) * 100, 2),
             "maxdd": round(float(w_maxdd[i]) * 100, 1)}
            for i in range(len(win))
        ],
    }


def _print(d: dict) -> None:
    w = d["window"]; s = d["winners_summary"]; f = d["factor_rank_test"]
    scope_txt = "全市场（含科创板/北交所/ST/巨无霸）" if not w["scope_filter"] else \
        f"沪深主板+创业板（剔除 ST/巨无霸，排除 {w['excluded_by_scope']} 只范围外）"
    print(f"\n### 近三个月涨幅前 {w['topn']} 启动前特征（{w['start']} ~ {w['end']}，"
          f"pre={w['pre']}日，有效样本 {w['universe_valid']}/{w['total_stocks']}）")
    print(f"  范围口径：{scope_txt}；实得赢家 {w['actual_winners']} 只")
    print(f"\n【赢家概览】")
    print(f"  区间涨幅：中位 +{s['ret_med']}%（均值 +{s['ret_mean']}%，最小 +{s['ret_min']}%）")
    print(f"  起点价中位 {s['p0_med']} 元 → 终点 {s['p1_med']} 元")
    print(f"  启动前 60 日（超跌）：中位 {s['pre_ret60_med']}%；启动前换手中位 {s['pre_turn_med']}%；"
          f"启动前日均成交额中位 {s['pre_amt_med_yi']} 亿（市值代理）")
    print(f"  区间内换手中位 {s['in_turn_med']}%（启动前→区间放大 {s['turn_multiplier']} 倍）；"
          f"区间内日均成交 {s['in_amt_med_yi']} 亿；区间最大回撤中位 {s['maxdd_med']}%")
    print(f"  板块分布：{s['board']}")
    print(f"  起点价带：{s['price_band']}（<5 / 5-20 / >20 元）")
    print(f"\n【因子分位检验（赢家在 start 日横截面的位置，越低=越超跌/越低换手/越小盘）】")
    print(f"  赢家 pre_ret60 分位 {f['winner_pre_ret60_pctl']}%（全体中位 {f['all_pre_ret60_med']}%）")
    print(f"  赢家 pre_turn  分位 {f['winner_pre_turn_pctl']}%（全体中位 {f['all_pre_turn_med']}%）")
    print(f"  赢家 pre_amt   分位 {f['winner_pre_amt_pctl']}%（全体中位 {f['all_pre_amt_med_yi']} 亿）")
    print(f"  → 若分位显著 <50%，说明项目『反转/缩量/小盘』因子方向能提前选中赢家")
    print(f"\n【Top 30 明细】")
    print(f"  {'code':>8} {'板块':>4} {'涨幅%':>7} {'起点价':>7} {'启前60日%':>9} "
          f"{'启前换手%':>9} {'启前成交亿':>9} {'区间换手%':>9} {'最大回撤%':>9}")
    for r in d["top_list"][:30]:
        print(f"  {r['code']:>8} {r['board']:>4} {r['ret']:>7.1f} {r['p0']:>7.2f} "
              f"{r['pre_ret60']:>9.1f} {r['pre_turn']:>9.2f} {r['pre_amt_yi']:>9.2f} "
              f"{r['in_turn']:>9.2f} {r['maxdd']:>9.1f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-07-28")
    parser.add_argument("--end", default="2026-10-28")
    parser.add_argument("--topn", type=int, default=100)
    parser.add_argument("--pre", type=int, default=60)
    parser.add_argument("--all-market", action="store_true",
                        help="关闭范围过滤（对照：含科创板/北交所/ST/巨无霸）")
    args = parser.parse_args()

    data = analyze(args.start, args.end, args.topn, args.pre, args.all_market)
    _print(data)

    log_dir = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    out = os.path.join(log_dir, "top100_probe.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
