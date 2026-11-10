# -*- coding: utf-8 -*-
"""#154 同行业（sector-matched）观察池对照回测（学习研究用途，不接实盘）。

核心问题（承接 #153 F 段）：持仓股完全不在观察池里时，分位标尺由「另一批股票」定义，
边缘明显衰减（q=0.01 全负）。本脚本检验：把观察池换成「与持仓同行业」的同质标尺，
能否恢复并改善边缘。

用法（backend 目录）::
    .venv\\Scripts\\python.exe scripts\\twin_t_sector_backtest.py
    .venv\\Scripts\\python.exe scripts\\twin_t_sector_backtest.py 2025-01-02 2026-11-09 0.0015 500

对三类持仓设定各跑两组对照（同一持仓、同一日期范围、quantile q=0.01）：
- naive-prefix：观察池 = 全市场按代码前 N（与 #153 一致，可能不含持仓行业）
- sector-matched：观察池 = 持仓各行业全部成分（按全市场顺序截到 N，保证含持仓自身）
做T贡献%>0 即跑赢纯持有；两池并列比较即见同质性标尺的净效果。

所有数据走本地 parquet，免联网、可复现。
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
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))


def _print_row(name: str, r: dict) -> None:
    a = r["aggregate"]
    print(
        f"{name:<30}"
        f"{a['rounds']:>7}"
        f"{a['win_rate']:>8.1f}%"
        f"{a['avg_net_pct']:>11.4f}%"
        f"{r['t_pnl_pct'] * 100:>12.3f}%"
        f"{r['hold_return'] * 100:>11.2f}%"
        f"{r['combined'] * 100:>11.2f}%"
        f"{'YES' if r['beats_hold'] else 'no':>6}"
    )


def _scenario(label: str, hold_codes, ind_map, all_codes, cost, observe_n):
    raw_hold, _ = _build_panel(hold_codes, START, END)
    hold_codes = [c for c in hold_codes if c in raw_hold]
    if not hold_codes:
        print(f"{label}: 持仓无本地数据，跳过")
        return
    raw_naive, _ = _build_panel(all_codes[:observe_n], START, END)
    sector_codes = build_sector_observe_codes(hold_codes, ind_map, all_codes, cap=observe_n)
    raw_sector, _ = _build_panel(sector_codes, START, END)
    p = DecoupledParams(mode="both", select_mode="quantile",
                        quantile=0.01, n_select=3, cost_per_round_pct=cost)
    rn = simulate_decoupled(raw_hold={c: raw_hold[c] for c in hold_codes},
                            raw_observe=raw_naive, params=p)
    rs = simulate_decoupled(raw_hold={c: raw_hold[c] for c in hold_codes},
                            raw_observe=raw_sector, params=p)
    in_naive = len(set(hold_codes) & set(raw_naive))
    in_sector = len(set(hold_codes) & set(raw_sector))
    print(f"\n=== {label}（持仓 {len(hold_codes)} 只，naive池含 {in_naive} / "
          f"sector池含 {in_sector}，sector池 {len(raw_sector)} 只）===")
    print(f"{'变体':<30}{'回合':>7}{'胜率':>9}{'每笔净%':>12}"
          f"{'做T贡献%':>13}{'纯持有%':>12}{'组合%':>12}{'跑赢':>6}")
    _print_row("naive-prefix 观察池", rn)
    _print_row("sector-matched 观察池", rs)
    if rn["aggregate"]["rounds"] and rs["aggregate"]["rounds"]:
        delta = (rs["t_pnl_pct"] - rn["t_pnl_pct"]) * 100
        print(f"    sector − naive 做T贡献差：{delta:+.3f}%（>0 即同质性标尺更优）")


def main() -> None:
    global START, END
    START = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    END = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    cost = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0015
    observe_n = int(sys.argv[4]) if len(sys.argv) > 4 else 500

    all_codes = _all_codes()
    if not all_codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return
    ind_map = load_industry_map()
    print(f"区间 {START} ~ {END}，成本 {cost * 100:.2f}%，观察池上限 {observe_n}，"
          f"行业映射 {len(ind_map)} 只\n")

    # 情景 1：分散持仓（全市场前 30，落在 prefix 池内）
    _scenario("分散持仓 前30（在 prefix 内）", all_codes[:30],
              ind_map, all_codes, cost, observe_n)

    # 情景 2：持仓在 prefix 池之外（代码 500~529），naive 池完全不含持仓
    _scenario("持仓在 prefix 外（代码500-529）", all_codes[500:530],
              ind_map, all_codes, cost, observe_n)

    # 情景 3：单一行业集中持仓（取成分≥40 的最大行业前 30）
    cnt = Counter(ind_map.get(c, "未知") for c in all_codes if ind_map.get(c))
    big = sorted(((k, v) for k, v in cnt.items() if v >= 40),
                 key=lambda x: -x[1])
    if big:
        ind, _ = big[0]
        same = [c for c in all_codes if ind_map.get(c) == ind][:30]
        _scenario(f"单一行业集中持仓（{ind} 前30）", same,
                  ind_map, all_codes, cost, observe_n)

    print("\n纯研究产物，不参与交易。检验假设：同行业标尺能恢复持仓不在观察池时衰减的边缘。")


if __name__ == "__main__":
    main()
