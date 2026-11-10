# -*- coding: utf-8 -*-
"""观察池 / 持仓池分离的双向做T 对照回测（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\twin_t_decoupled_backtest.py
    .venv\\Scripts\\python.exe scripts\\twin_t_decoupled_backtest.py 2025-01-02 2026-11-09 10,30,50,80 500 0.0015

一次读盘构建 TopN 观察池（默认 500），持仓池取其前 H 只（默认 10/30/50/80），
对每个 H 跑 5 组口径对照：
- hold_only：只在持仓池内排序（等价 #152 小池基线，对照组）；
- quantile q=0.01 / 0.02 / 0.05：用观察池截面分位做「极端」门槛；
- intersect：观察池绝对 Top5 与持仓池取交集（最严口径）。

核心问题：散户只持有 10~50 只，能否借「全市场截面信息」保住 #152 在 Top500 篮子
才出现的正边缘？做T贡献%>0 即跑赢纯持有。

所有数据走本地 parquet，免联网、可复现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.cross_sectional import _build_panel  # noqa: E402
from app.strategies.twin_t_decoupled import (  # noqa: E402
    DecoupledParams,
    simulate_decoupled,
)


def _list_codes(n: int) -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))[:n]


def _print_row(name: str, r: dict) -> None:
    a = r["aggregate"]
    print(
        f"{name:<26}"
        f"{a['rounds']:>7}"
        f"{a['win_rate']:>8.1f}%"
        f"{a['avg_net_pct']:>11.4f}%"
        f"{r['t_pnl_pct'] * 100:>12.3f}%"
        f"{r['hold_return'] * 100:>11.2f}%"
        f"{r['combined'] * 100:>11.2f}%"
        f"{'YES' if r['beats_hold'] else 'no':>6}"
    )


def _variants(cost: float) -> list[tuple[str, DecoupledParams]]:
    def mk(**kw):
        return DecoupledParams(cost_per_round_pct=cost, **kw)
    return [
        ("hold_only (基线)", mk(select_mode="hold_only", n_select=3)),
        ("quantile q=0.01", mk(select_mode="quantile", quantile=0.01, n_select=3)),
        ("quantile q=0.02", mk(select_mode="quantile", quantile=0.02, n_select=3)),
        ("quantile q=0.05", mk(select_mode="quantile", quantile=0.05, n_select=3)),
        ("intersect (Top5∩持仓)", mk(select_mode="intersect", n_select=5)),
    ]


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    holds = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3
                              else "10,30,50,80").split(",")]
    observe_size = int(sys.argv[4]) if len(sys.argv) > 4 else 500
    cost = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0015

    codes = _list_codes(observe_size)
    if not codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return

    # 一次读盘，共享给所有持仓池 / 所有变体
    raw_obs, days = _build_panel(codes, start, end)
    obs_codes = list(raw_obs)
    print(f"区间 {start} ~ {end}，观察池 Top{observe_size}（有效 {len(obs_codes)} 只，"
          f"{len(days)} 个交易日），往返成本 {cost * 100:.2f}%\n")

    header = (f"{'变体':<26}{'回合':>7}{'胜率':>9}{'每笔净%':>12}"
              f"{'做T贡献%':>13}{'纯持有%':>12}{'组合%':>12}{'跑赢':>6}")
    variants = _variants(cost)

    for H in holds:
        if H > len(obs_codes):
            continue
        raw_hold = {c: raw_obs[c] for c in obs_codes[:H]}
        print(f"=== 持仓池 Top{H} / 观察池 Top{len(obs_codes)} ===")
        print(header)
        results = {}
        for name, p in variants:
            r = simulate_decoupled(raw_hold=raw_hold, raw_observe=raw_obs,
                                   days=days, params=p)
            results[name] = r
            _print_row(name, r)
        probe = results["quantile q=0.01"]
        print(f"    观察池 q=0.01 平均门槛：下 {probe['mean_thr_low'] * 100:.3f}% "
              f"/ 上 +{probe['mean_thr_high'] * 100:.3f}%\n")

    print("纯研究产物，不参与交易。对照问题：扩大观察池能否让小持仓池跑赢纯持有。")


if __name__ == "__main__":
    main()
