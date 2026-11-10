# -*- coding: utf-8 -*-
"""#156 最终「可落地形态」回测（学习研究用途，不接实盘）。

把 #153/#154/#155 的收获取集成一个最接近散户可执行的形态，给出诚实的最终数字：
- 持仓：散户现实篮子 H=30（分散跨行业，或单一行业集中）
- 观察池：与持仓同行业的 sector-matched 标尺（#154 证明比 broad 尺更优，尤其持仓集中/在池外时）
- 成本：真实成本（佣金万2.5+最低5元 + 印花0.05% + 滑点0.05%，按单票名义额实算，#155）
对照：同一篮子下 naive-prefix 观察池 vs sector-matched 观察池，均用真实成本；
并附 flat 0.15% 固定成本基线便于与 #153 衔接。

用法（backend 目录）::
    .venv\\Scripts\\python.exe scripts\\twin_t_final_backtest.py
    .venv\\Scripts\\python.exe scripts\\twin_t_final_backtest.py 2025-01-02 2026-11-09 1000000
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.cross_sectional import _build_panel, load_industry_map  # noqa: E402
from app.strategies.twin_t_decoupled import (  # noqa: E402
    DecoupledParams,
    build_sector_observe_codes,
    simulate_decoupled,
)


def _all_codes() -> list[str]:
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet")) if MINUTE5_DIR.exists() else []


def _print_row(name: str, r: dict) -> None:
    a = r["aggregate"]
    print(
        f"{name:<28}"
        f"{a['rounds']:>7}"
        f"{a['win_rate']:>8.1f}%"
        f"{a['avg_net_pct']:>11.4f}%"
        f"{r['t_pnl_pct'] * 100:>12.3f}%"
        f"{r['hold_return'] * 100:>11.2f}%"
        f"{r['combined'] * 100:>11.2f}%"
        f"{'YES' if r['beats_hold'] else 'no':>6}"
    )


def _one(label, hold_codes, ind_map, all_codes, capital):
    raw_hold, _ = _build_panel(hold_codes, START, END)
    hold_codes = [c for c in hold_codes if c in raw_hold]
    if not hold_codes:
        print(f"{label}: 无本地数据，跳过"); return
    naive_obs, _ = _build_panel(all_codes[:500], START, END)
    sec_codes = build_sector_observe_codes(hold_codes, ind_map, all_codes, cap=500)
    sec_obs, _ = _build_panel(sec_codes, START, END)
    per_name = capital / len(hold_codes) * 0.30
    print(f"\n=== {label}（持仓 {len(hold_codes)} 只，单票名义额≈¥{per_name:,.0f}，"
          f"sector池 {len(sec_obs)} 只）===")
    print(f"{'变体':<28}{'回合':>7}{'胜率':>9}{'每笔净%':>12}"
          f"{'做T贡献%':>13}{'纯持有%':>12}{'组合%':>12}{'跑赢':>6}")
    p_quant = dict(mode="both", select_mode="quantile", quantile=0.01, n_select=3)
    flat = simulate_decoupled(raw_hold={c: raw_hold[c] for c in hold_codes},
                              raw_observe=naive_obs,
                              params=DecoupledParams(**p_quant, cost_per_round_pct=0.0015))
    _print_row("①naive尺+flat0.15%", flat)
    rn = simulate_decoupled(raw_hold={c: raw_hold[c] for c in hold_codes},
                            raw_observe=naive_obs,
                            params=DecoupledParams(**p_quant, realistic_cost=True,
                                                  total_capital=float(capital)))
    _print_row("②naive尺+真实成本", rn)
    rs = simulate_decoupled(raw_hold={c: raw_hold[c] for c in hold_codes},
                            raw_observe=sec_obs,
                            params=DecoupledParams(**p_quant, realistic_cost=True,
                                                  total_capital=float(capital)))
    _print_row("③同行业尺+真实成本", rs)
    if rn["aggregate"]["rounds"] and rs["aggregate"]["rounds"]:
        print(f"   ③ − ② 做T贡献差：{(rs['t_pnl_pct']-rn['t_pnl_pct'])*100:+.3f}%"
              f"（>0 即同行业尺在真实成本下仍更优）")


def main() -> None:
    global START, END
    START = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    END = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    capital = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000

    all_codes = _all_codes()
    if not all_codes:
        print("未找到本地 5 分钟数据。"); return
    ind_map = load_industry_map()
    print(f"区间 {START} ~ {END}，真实成本，总资金 ¥{capital:,}，行业映射 {len(ind_map)} 只\n")

    # 分散跨行业篮子（全市场前 30 有效）
    raw0, _ = _build_panel(all_codes[:500], START, END)
    diverse = list(raw0)[:30]
    _one("分散跨行业篮子 H30", diverse, ind_map, all_codes, capital)

    # 单一行业集中篮子（取成分≥40 的最大行业前 30）
    cnt = Counter(ind_map.get(c, "未知") for c in all_codes if ind_map.get(c))
    big = sorted(((k, v) for k, v in cnt.items() if v >= 40), key=lambda x: -x[1])
    if big:
        ind, _ = big[0]
        same = [c for c in all_codes if ind_map.get(c) == ind][:30]
        _one(f"单一行业集中({ind}) H30", same, ind_map, all_codes, capital)

    print("\n纯研究产物，不参与交易。#156 诚实终局：同行业标尺 + 真实成本下，散户形态的边缘还剩多少。")


if __name__ == "__main__":
    main()
