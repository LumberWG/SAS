# -*- coding: utf-8 -*-
"""双向做T（正T+反T，仅当日平仓）回测，对比纯持有底仓篮子（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\twin_t_backtest.py
    .venv\\Scripts\\python.exe scripts\\twin_t_backtest.py 2025-01-02 2026-11-09 80
    .venv\\Scripts\\python.exe scripts\\twin_t_backtest.py 2025-01-02 2026-11-09 80,200,500

默认回测本地 5 分钟数据（D:\\Backup\\SAS\\data\\minute5_fsdb）前 N 只作为「底仓篮子」，
跑五组对照，直接回答「不融券前提下，双向做T 是否给持有组合加分」：

- T1 双向做T（乐观成本 0.1%）
- T2 双向做T（零售真实成本 0.15%）
- T3 双向做T + 行业中性（乐观成本 0.1%）
- T4 仅正T（无合成空单，对应无融券现实）
- T5 仅反T（仅靠合成空单吃超买回落）

对照指标：做T 回合 / 胜率 / 做T贡献% / 纯持有% / 组合% / 是否跑赢持有。
所有数据走本地 parquet，免联网、可复现。
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


def _print_row(name: str, r: dict) -> None:
    agg = r["aggregate"]
    print(
        f"{name:<18}"
        f"{agg['rounds']:>7}"
        f"{agg['win_rate']:>8.1f}%"
        f"{r['t_pnl_pct'] * 100:>10.4f}%"
        f"{r['hold_return'] * 100:>10.2f}%"
        f"{(r['combined']) * 100:>10.2f}%"
        f"{'YES' if r['beats_hold'] else 'no':>5}"
    )


def _variants(ind):
    """返回 (名称, params, industry_map) 五组对照，共享同一 panel。"""
    return [
        ("T1 双向(0.1%)", TwinTParams(mode="both", cost_per_round_pct=0.0010), None),
        ("T2 双向(0.15%)", TwinTParams(mode="both", cost_per_round_pct=0.0015), None),
        ("T3 双向+中性", TwinTParams(mode="both", cost_per_round_pct=0.0010,
                                     industry_neutral=True), ind or None),
        ("T4 仅正T", TwinTParams(mode="long", cost_per_round_pct=0.0015), None),
        ("T5 仅反T", TwinTParams(mode="short", cost_per_round_pct=0.0015), None),
    ]


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    # 第 3 参支持逗号分隔多池，一次读盘、跨池对照（如 "80,200,500"）
    pools = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "80").split(",")]
    ind = load_industry_map()
    variants = _variants(ind)

    for universe in pools:
        codes = _list_codes(universe)
        if not codes:
            print("未找到本地 5 分钟数据，请先预下载数据。")
            return
        # 关键提速：一次读盘构建 panel，5 个变体共享（raw 注入，不再各自 _build_panel）
        raw, _ = _build_panel(codes, start, end)
        if not raw:
            print(f"Top{universe} 区间内无有效数据，跳过。")
            continue
        print(f"\n区间 {start} ~ {end}，底仓篮子 Top{universe}，本地覆盖 {len(codes)} 只"
              f"，panel {len(raw)} 只（行业映射 {len(ind)} 只）")
        print(f"{'变体':<18}{'回合':>7}{'做T胜率':>9}{'做T贡献%':>11}"
              f"{'纯持有%':>11}{'组合%':>11}{'跑赢':>6}")
        for name, p, im in variants:
            res = simulate_twin_t(raw=raw, params=p, industry_map=im)
            _print_row(name, res)

    print("\n纯研究产物，不参与交易。对比问题：不融券前提下，双向做T 是否给持有组合加分。"
          "（做T贡献>0 即跑赢纯持有）")


if __name__ == "__main__":
    main()
