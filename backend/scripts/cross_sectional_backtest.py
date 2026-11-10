# -*- coding: utf-8 -*-
"""截面统计套利回测（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\cross_sectional_backtest.py
    .venv\\Scripts\\python.exe scripts\\cross_sectional_backtest.py 2025-01-02 2026-11-09 500

默认回测本地 5 分钟数据（D:\\Backup\\SAS\\data\\minute5_fsdb）前 N 只构成的截面池，
在每一 5 分钟截面按 VWAP 偏离排序、做多最超卖/做空最超买（longshort）或仅做多（long）。

基线变体（无需行业映射）：
- CS-L5 / CS-L10   仅做多最超卖（契合 A 股 T+1 难做空），每侧 5/10 只
- CS-LS5 / CS-LS10 多空配对，每侧 5/10 只
- CS-L5-strict     仅做多最超卖且偏离阈值放宽到 0.8%（只吃最极端边缘）

稳健性变体（需 data/industry_map.csv，由 scripts/fetch_industry_map.py 抓取）：
- CS-LS5-N    行业中性（dev 对行业截面均值残差化，剔除行业 beta）
- CS-LS5-L    流动性加权（仅取截面内流动性前 50% 标的）
- CS-LS5-S02  计入空腿融券费 0.02%/笔（≈日内融资）
- CS-LS5-S05  计入空腿融券费 0.05%/笔（≈持有至收盘）
- CS-LS5-NL   行业中性 + 流动性加权
- CS-LS5-NLS  行业中性 + 流动性加权 + 融券费 0.03%/笔（最接近实盘可落地版）

对比参照：K 天花板（每笔 −0.0859%、赢亏比 1.31x）；VWAP 单标的（每笔 −0.0482%、0.60x）。
所有数据走本地 parquet，免联网、可复现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.cross_sectional import (  # noqa: E402
    CSParams,
    load_industry_map,
    simulate_cross_sectional,
)


def _list_codes(universe: int) -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))[:universe]


def _print_row(name: str, m: dict) -> None:
    need = (100.0 - m["win_rate"]) / m["win_rate"] if m["win_rate"] > 0 else float("inf")
    need_s = f"{need:>7.2f}x" if need != float("inf") else "    inf"
    print(
        f"{name:<18}"
        f"{m['rounds']:>7}"
        f"{m['win_rate']:>8.1f}%"
        f"{m['avg_net_pct']:>10.4f}%"
        f"{m['win_loss_ratio']:>7.2f}x"
        f"{need_s:>8}"
        f"{m['max_drawdown_pct']:>10.2f}"
    )


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    codes = _list_codes(universe)
    if not codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return
    print(f"区间 {start} ~ {end}，截面池 Top{universe}，本地 5 分钟覆盖 {len(codes)} 只\n")
    print(
        f"{'变体':<18}{'回合':>7}{'胜率':>9}{'均净%':>11}"
        f"{'赢亏比':>8}{'需':>8}{'最大回撤':>11}"
    )
    print(f"{'K天花板(参照)':<18}{'':>7}{'35.4%':>9}{'-0.0859%':>11}{'1.31x':>8}{'1.82x':>8}{'2.81':>11}")
    print(f"{'VWAP单标的(参照)':<18}{'':>7}{'59.7%':>9}{'-0.0482%':>11}{'0.60x':>8}{'0.68x':>8}{'267':>11}")

    baseline = [
        ("CS-L5", CSParams(mode="long", n_select=5)),
        ("CS-L10", CSParams(mode="long", n_select=10)),
        ("CS-LS5", CSParams(mode="longshort", n_select=5)),
        ("CS-LS10", CSParams(mode="longshort", n_select=10)),
        ("CS-L5-strict", CSParams(mode="long", n_select=5, vwap_entry_pct=0.008)),
    ]
    for name, p in baseline:
        res = simulate_cross_sectional(codes, start, end, p, base_qty=1.0)
        _print_row(name, res["aggregate"])

    ind_map = load_industry_map()
    if not ind_map:
        print("\n[提示] data/industry_map.csv 缺失，稳健性(行业中性)变体跳过；"
              "先运行 scripts/fetch_industry_map.py 抓取。流动性加权/融券费变体仍照常。")
        robust = [
            ("CS-LS5-L", CSParams(mode="longshort", n_select=5, liq_weight=True, liq_quantile=0.5)),
            ("CS-LS5-S02", CSParams(mode="longshort", n_select=5, short_cost_pct=0.0002)),
            ("CS-LS5-S05", CSParams(mode="longshort", n_select=5, short_cost_pct=0.0005)),
        ]
    else:
        covered = sum(1 for c in codes if c in ind_map)
        print(f"\n[行业映射] 加载 {len(ind_map)} 只行业标签；本池覆盖 {covered}/{len(codes)} 只\n")
        robust = [
            ("CS-LS5-N", CSParams(mode="longshort", n_select=5, industry_neutral=True)),
            ("CS-LS5-L", CSParams(mode="longshort", n_select=5, liq_weight=True, liq_quantile=0.5)),
            ("CS-LS5-S02", CSParams(mode="longshort", n_select=5, short_cost_pct=0.0002)),
            ("CS-LS5-S05", CSParams(mode="longshort", n_select=5, short_cost_pct=0.0005)),
            ("CS-LS5-NL", CSParams(mode="longshort", n_select=5, industry_neutral=True, liq_weight=True, liq_quantile=0.5)),
            ("CS-LS5-NLS", CSParams(mode="longshort", n_select=5, industry_neutral=True, liq_weight=True, liq_quantile=0.5, short_cost_pct=0.0003)),
        ]
    for name, p in robust:
        res = simulate_cross_sectional(codes, start, end, p, base_qty=1.0, industry_map=ind_map)
        _print_row(name, res["aggregate"])

    print("\n纯研究产物，不参与交易。对比：K 天花板每笔 −0.0859% / VWAP单标的每笔 −0.0482%。")


if __name__ == "__main__":
    main()
