# -*- coding: utf-8 -*-
"""观察池/持仓池分离双向做T 的稳健性检验（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\twin_t_decoupled_robust.py
    .venv\\Scripts\\python.exe scripts\\twin_t_decoupled_robust.py 2025-01-02 2026-11-09 500 0.0015

一次读盘 Top500 观察池，检验四件事：
A. **随机持仓池**：5 组固定 seed 的随机 10/30 只，排除「按代码取前 N 只」的选样偏差；
B. **成本梯度**：0.10% ~ 0.25%，定位盈亏平衡往返成本；
C. **q 梯度**：边缘是否只存在于最极端 1% 分位；
D. **n_select 梯度**：持仓池内每侧开仓数敏感度；
E. **集中度**：正贡献股票占比、前 3 只占总盈亏比例（判断是否靠少数妖股）。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.cross_sectional import _build_panel  # noqa: E402
from app.strategies.twin_t_decoupled import DecoupledParams, simulate_decoupled  # noqa: E402


def _line(tag: str, r: dict) -> str:
    a = r["aggregate"]
    return (f"{tag:<24}{a['rounds']:>7}{a['win_rate']:>8.1f}%"
            f"{a['avg_net_pct']:>11.4f}%{r['t_pnl_pct'] * 100:>11.3f}%"
            f"{r['hold_return'] * 100:>10.2f}%")


def _hdr(title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"{'配置':<24}{'回合':>7}{'胜率':>9}{'每笔净%':>12}"
          f"{'做T贡献%':>12}{'纯持有%':>11}")


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    obs_n = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    base_cost = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0015

    # 多读 30 只作为「与观察池完全不相交」的 holdout 持仓池
    codes = sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))[:obs_n + 30]
    if not codes:
        print("未找到本地 5 分钟数据。")
        return
    raw_all, days = _build_panel(codes, start, end)
    pool_all = list(raw_all)
    obs_codes = pool_all[:obs_n]
    holdout_codes = pool_all[obs_n:]
    raw_obs = {c: raw_all[c] for c in obs_codes}
    pool = obs_codes
    rng = np.random.default_rng(42)
    print(f"观察池 Top{obs_n}（有效 {len(pool)} 只，{len(days)} 天），"
          f"holdout 持仓池 {len(holdout_codes)} 只（与观察池不相交），"
          f"基准成本 {base_cost * 100:.2f}%，q=0.01")

    def run(hold, **kw):
        kwargs = dict(select_mode="quantile", quantile=0.01, n_select=3,
                      cost_per_round_pct=base_cost)
        kwargs.update(kw)
        p = DecoupledParams(**kwargs)
        raw_hold = {c: raw_obs[c] for c in hold}
        return simulate_decoupled(raw_hold=raw_hold, raw_observe=raw_obs,
                                  days=days, params=p)

    # A. 随机持仓池
    _hdr("A. 随机持仓池（排除前 N 只选样偏差）")
    for H in (10, 30):
        nets = []
        for s in range(5):
            sample = list(rng.choice(pool, size=min(H, len(pool)), replace=False))
            r = run(sample)
            nets.append(r["aggregate"]["avg_net_pct"])
            print(_line(f"H{H}-seed{s}", r))
        print(f"    H{H} 汇总：每笔净% 均值 {np.mean(nets):+.4f}%，"
              f"为正 {sum(1 for x in nets if x > 0)}/5 组，"
              f"最差 {min(nets):+.4f}%")

    # 固定一组池做 B/C/D/E 梯度
    rng2 = np.random.default_rng(7)
    fixed = list(rng2.choice(pool, size=30, replace=False))
    print(f"\n固定梯度池：seed=7 随机 30 只（观察池 {len(pool)} 只）")

    # B. 成本梯度
    _hdr("B. 成本梯度（定位盈亏平衡往返成本）")
    pts = []
    for c in (0.0010, 0.0015, 0.0018, 0.0020, 0.0025):
        r = run(fixed, cost_per_round_pct=c)
        pts.append((c, r["aggregate"]["avg_net_pct"]))
        print(_line(f"cost {c * 100:.2f}%", r))
    for (c1, p1), (c2, p2) in zip(pts, pts[1:]):
        if p1 > 0 >= p2:
            be = c1 + (c2 - c1) * (p1 / (p1 - p2))
            print(f"    → 盈亏平衡往返成本 ≈ {be * 100:.4f}%")

    # C. q 梯度
    _hdr("C. 观察池分位 q 梯度")
    for q in (0.005, 0.01, 0.015, 0.02, 0.03, 0.05):
        p = DecoupledParams(select_mode="quantile", quantile=q, n_select=3,
                            cost_per_round_pct=base_cost)
        r = simulate_decoupled(raw_hold={c: raw_obs[c] for c in fixed},
                               raw_observe=raw_obs, days=days, params=p)
        print(_line(f"q={q:.3f}", r))

    # D. n_select 梯度
    _hdr("D. 持仓池内每侧开仓数 n_select 梯度")
    for n in (1, 2, 3, 5, 10):
        r = run(fixed, n_select=n)
        print(_line(f"n_select={n}", r))

    # E. 集中度
    r = run(fixed)
    by_code = defaultdict(float)
    for t in r["trades"]:
        by_code[t.code] += t.pnl
    vals = sorted(by_code.values(), reverse=True)
    total = sum(v for v in vals if v > 0)
    print("\n=== E. 收益集中度 ===")
    print(f"    有交易股票 {len(vals)} 只，正贡献 {sum(1 for v in vals if v > 0)} 只，"
          f"负贡献 {sum(1 for v in vals if v <= 0)} 只")
    if total > 0:
        print(f"    前 3 只占总正盈亏 {sum(vals[:3]) / total * 100:.1f}%；"
              f"最大单只占比 {vals[0] / total * 100:.1f}%")
    print(f"    dev 脏值清洗比例 {r['invalid_dev_frac'] * 100:.3f}%")

    # F. 持仓池与观察池完全不相交：散户的真实情形（持仓股未必属于观察指数）
    _hdr("F. 持仓池与观察池完全不相交（散户泛化性）")
    for H in (5, 10, 30):
        hold = holdout_codes[:min(H, len(holdout_codes))]
        for q in (0.005, 0.01):
            p = DecoupledParams(select_mode="quantile", quantile=q, n_select=3,
                                cost_per_round_pct=base_cost)
            r = simulate_decoupled(
                raw_hold={code: raw_all[code] for code in hold},
                raw_observe=raw_obs, days=days, params=p)
            print(_line(f"H{H}(out) q={q:.3f}", r))
    hold10 = holdout_codes[:10]
    for cost in (0.0015, 0.0018, 0.0020):
        p = DecoupledParams(select_mode="quantile", quantile=0.005, n_select=3,
                            cost_per_round_pct=cost)
        r = simulate_decoupled(
            raw_hold={code: raw_all[code] for code in hold10},
            raw_observe=raw_obs, days=days, params=p)
        print(_line(f"H10(out) cost{cost * 100:.2f}%", r))

    print("\n纯研究产物，不参与交易。")


if __name__ == "__main__":
    main()
