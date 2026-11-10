# -*- coding: utf-8 -*-
"""双向做T 稳健性检验：Top500 下的成本梯度 / n_select 梯度 / 行业中性@真实成本。

学习研究用途，不接实盘。#152 续：#152 主回测发现 Top500 双向做T 在 0.15% 成本下
仍为正贡献（+0.77%），本脚本回答两个决定「可落地性」的问题：
1. 盈亏平衡成本在哪？（成本梯度线性外推需实测确认，含印花税/佣金/滑点的真实边界）
2. 是否只是 n_select=5 的过拟合？（不同截面宽度是否稳健）

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\twin_t_robust.py
    .venv\\Scripts\\python.exe scripts\\twin_t_robust.py 2025-01-02 2026-11-09 500

一次读盘构建 panel，所有变体共享（raw 注入），免重复 IO。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.cross_sectional import _build_panel  # noqa: E402
from app.strategies.twin_t import TwinTParams, load_industry_map, simulate_twin_t  # noqa: E402


def _list_codes(universe: int) -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))[:universe]


def _row(name, res):
    agg = res["aggregate"]
    print(
        f"{name:<24}"
        f"{agg['rounds']:>7}"
        f"{agg['win_rate']:>9.1f}%"
        f"{res['t_pnl_pct'] * 100:>11.4f}%"
        f"{res['hold_return'] * 100:>11.2f}%"
        f"{res['combined'] * 100:>11.2f}%"
        f"{'YES' if res['beats_hold'] else 'no':>5}"
    )


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    codes = _list_codes(universe)
    if not codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return
    raw, _ = _build_panel(codes, start, end)
    if not raw:
        print("区间内无有效数据。")
        return
    ind = load_industry_map()
    print(f"区间 {start} ~ {end}，底仓篮子 Top{universe}，panel {len(raw)} 只\n")
    hdr = (f"{'变体':<24}{'回合':>7}{'做T胜率':>10}{'做T贡献%':>12}"
           f"{'纯持有%':>12}{'组合%':>12}{'跑赢':>6}")
    print(hdr)
    print("-" * len(hdr))

    print("[成本梯度] mode=both, n_select=5")
    for c in (0.0010, 0.0015, 0.0018, 0.0020, 0.0025):
        res = simulate_twin_t(raw=raw, params=TwinTParams(mode="both", cost_per_round_pct=c))
        _row(f"成本 {c * 100:.2f}%", res)

    print("[截面宽度梯度] mode=both, 成本 0.15%")
    for n in (3, 5, 10, 20):
        res = simulate_twin_t(raw=raw, params=TwinTParams(
            mode="both", cost_per_round_pct=0.0015, n_select=n))
        _row(f"n_select={n}", res)

    print("[行业中性 @ 真实成本 0.15%]")
    for c in (0.0010, 0.0015):
        res = simulate_twin_t(raw=raw, params=TwinTParams(
            mode="both", cost_per_round_pct=c, industry_neutral=True),
            industry_map=ind or None)
        _row(f"中性 成本{c * 100:.2f}%", res)

    print("\n纯研究产物，不参与交易。做T贡献>0 即给持有组合加分。")


if __name__ == "__main__":
    main()
