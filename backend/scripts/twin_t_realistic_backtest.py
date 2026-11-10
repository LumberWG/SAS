# -*- coding: utf-8 -*-
"""#155 真实成本模型对照回测（学习研究用途，不接实盘）。

固定成本 0.15% 只是近似。真实零售成本 = 佣金(万2.5，最低5元/笔) + 印花税(卖出0.05%)
+ 滑点(往返 ~0.05%)。小额持仓时「最低5元」会显著抬高有效成本 —— 这是 #154/#155 要量化
的关键：散户资金小 → 单票名义额小 → 最低佣金占比飙升 → 边缘被吞噬。

用法（backend 目录）::
    .venv\\Scripts\\python.exe scripts\\twin_t_realistic_backtest.py
    .venv\\Scripts\\python.exe scripts\\twin_t_realistic_backtest.py 2025-01-02 2026-11-09 500 10,30,50 300000,1000000

对每档 (持仓 H, 总资金 C) 跑两组：
- flat：固定 0.15% 往返（与 #153 同口径，基线）
- realistic：佣金(万2.5,最低5元) + 印花0.05% + 滑点0.05%，按单票名义额 = C/H*0.3 实算
观察池固定 Top500（sector-matched 留待 #156）。做T贡献%>0 即跑赢纯持有。

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


def _all_codes() -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))


def _print_row(name: str, r: dict, note: str = "") -> None:
    a = r["aggregate"]
    print(
        f"{name:<16}"
        f"{a['rounds']:>7}"
        f"{a['win_rate']:>8.1f}%"
        f"{a['avg_net_pct']:>11.4f}%"
        f"{r['t_pnl_pct'] * 100:>12.3f}%"
        f"{r['hold_return'] * 100:>11.2f}%"
        f"{r['combined'] * 100:>11.2f}%"
        f"{'YES' if r['beats_hold'] else 'no':>6}"
        f"{note}"
    )


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    observe_n = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    holds = [int(x) for x in (sys.argv[4] if len(sys.argv) > 4 else "10,30,50").split(",")]
    capitals = [int(x) for x in (sys.argv[5] if len(sys.argv) > 5 else "300000,1000000").split(",")]

    all_codes = _all_codes()
    if not all_codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return
    codes = all_codes[:observe_n]
    raw_obs, days = _build_panel(codes, start, end)
    valid = list(raw_obs)
    print(f"区间 {start} ~ {end}，观察池 Top{observe_n}（有效 {len(valid)} 只，"
          f"{len(days)} 交易日）\n")

    header = (f"{'变体':<16}{'回合':>7}{'胜率':>9}{'每笔净%':>12}"
              f"{'做T贡献%':>13}{'纯持有%':>12}{'组合%':>12}{'跑赢':>6}  备注")

    for H in holds:
        if H > len(valid):
            continue
        raw_hold = {c: raw_obs[c] for c in valid[:H]}
        for C in capitals:
            per_name = C / H * 0.30
            print(f"\n=== 持仓 H={H} | 总资金 ¥{C:,} | 单票名义额 ≈¥{per_name:,.0f} "
                  f"(佣金最低5元≈{5*2/per_name*100:.3f}%往返) ===")
            print(header)
            flat = simulate_decoupled(
                raw_hold=raw_hold, raw_observe=raw_obs, days=days,
                params=DecoupledParams(mode="both", select_mode="quantile",
                                      quantile=0.01, n_select=3,
                                      cost_per_round_pct=0.0015))
            _print_row("flat 0.15%", flat)
            real = simulate_decoupled(
                raw_hold=raw_hold, raw_observe=raw_obs, days=days,
                params=DecoupledParams(mode="both", select_mode="quantile",
                                      quantile=0.01, n_select=3,
                                      realistic_cost=True, total_capital=float(C)))
            _print_row("realistic", real,
                       note=f"  (佣万2.5/最低5元+印花0.05%+滑0.05%)")
            if flat["aggregate"]["rounds"] and real["aggregate"]["rounds"]:
                d = (real["t_pnl_pct"] - flat["t_pnl_pct"]) * 100
                print(f"    realistic − flat 做T贡献差：{d:+.3f}%（<0 即真实成本更贵）")

    print("\n纯研究产物，不参与交易。检验假设：最低佣金使小额持仓的真实成本高于固定0.15%，"
          "吞噬边缘。")


if __name__ == "__main__":
    main()
