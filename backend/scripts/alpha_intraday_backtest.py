# -*- coding: utf-8 -*-
"""与 MACD 动量正交的独立日内 alpha 回测（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\alpha_intraday_backtest.py
    .venv\\Scripts\\python.exe scripts\\alpha_intraday_backtest.py 2025-01-02 2026-11-09 80

默认回测本地 5 分钟数据（D:\\Backup\\SAS\\data\\minute5_fsdb）前 N 只：

第一部分（8 组）：四类与 MACD 动量正交的独立 alpha 各跑「原始 / 加 regime 硬闸」两组，
直接对比 K 天花板（每笔 −0.0859%、赢亏比 1.31x、需 1.82x）。

第二部分（组合层，#148）：把正交 alpha 合成，验证能否推过盈亏平衡——
- L  VWAP+ATR 风险层（用 ATR×mult 替代固定止损，压小赢大亏尾）
- M  组合(VWAP+ORB) 多袖求和
- N  组合(VWAP×ORB) 单仓共识
- N2 组合(VWAP×ORB×OBV) 单仓共识
- P  组合(VWAP×ORB×OBV)+ATR 全束+风险层

所有数据走本地 parquet，免联网、可复现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.alpha_intraday import (  # noqa: E402
    AlphaParams,
    ComboParams,
    simulate_universe,
    simulate_universe_combo,
)

AXES = ["vwap", "obv", "volshock", "orb"]


def _list_codes(universe: int) -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    return sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))[:universe]


def _print_row(name: str, m: dict) -> None:
    need = (100.0 - m["win_rate"]) / m["win_rate"] if m["win_rate"] > 0 else float("inf")
    print(
        f"{name:<22}"
        f"{m['rounds']:>7}"
        f"{m['win_rate']:>8.1f}%"
        f"{m['avg_net_pct']:>10.4f}%"
        f"{m['win_loss_ratio']:>7.2f}x"
        f"{need:>7.2f}x"
        f"{m['max_drawdown_pct']:>10.2f}"
    )


def _params(axis: str, gated: bool) -> AlphaParams:
    p = AlphaParams(axis=axis)
    if gated:
        p.trend_filter = True
        p.min_daily_amp_pct = 2.5
        p.min_daily_amount = 1e8
    return p


def _combo(axes, mode="ensemble", **kw) -> ComboParams:
    return ComboParams(axes=tuple(axes), combo_mode=mode, **kw)


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 80

    codes = _list_codes(universe)
    if not codes:
        print("未找到本地 5 分钟数据，请先预下载数据。")
        return
    print(f"区间 {start} ~ {end}，标的 Top{universe}，本地 5 分钟覆盖 {len(codes)} 只\n")

    print("===== 第一部分：单轴正交 alpha（8 组，对比 K 天花板）=====")
    print(
        f"{'变体':<22}{'回合':>7}{'胜率':>9}{'均净%':>11}"
        f"{'赢亏比':>8}{'需':>8}{'最大回撤':>11}"
    )
    print(f"{'K天花板(参照)':<22}{'':>7}{'35.4%':>9}{'-0.0859%':>11}{'1.31x':>8}{'1.82x':>8}{'2.81':>11}")
    for axis in AXES:
        for gated in (False, True):
            tag = "原始" if not gated else "regime闸"
            res = simulate_universe(codes, start, end, _params(axis, gated), max_stocks=None)
            _print_row(f"{axis} {tag}", res["aggregate"])

    print("\n===== 第二部分：组合层 #148（正交合成 + ATR 风险层）=====")
    print(
        f"{'变体':<22}{'回合':>7}{'胜率':>9}{'均净%':>11}"
        f"{'赢亏比':>8}{'需':>8}{'最大回撤':>11}"
    )
    # L：VWAP + ATR 风险层
    l = simulate_universe(codes, start, end,
                          AlphaParams(axis="vwap", atr_stop_mult=1.0), max_stocks=None)
    _print_row("L VWAP+ATR(1x)", l["aggregate"])
    # M：组合(VWAP+ORB) 多袖求和
    m = simulate_universe_combo(codes, start, end,
                                _combo(["vwap", "orb"], "portfolio"), max_stocks=None)
    _print_row("M VWAP+ORB 求和", m["aggregate"])
    # N：组合(VWAP×ORB) 单仓共识
    n = simulate_universe_combo(codes, start, end,
                                _combo(["vwap", "orb"], "ensemble"), max_stocks=None)
    _print_row("N VWAP×ORB 共识", n["aggregate"])
    # N2：组合(VWAP×ORB×OBV) 单仓共识
    n2 = simulate_universe_combo(codes, start, end,
                                 _combo(["vwap", "orb", "obv"], "ensemble"), max_stocks=None)
    _print_row("N2 VWAP×ORB×OBV", n2["aggregate"])
    # P：全束 + ATR 风险层
    p = simulate_universe_combo(codes, start, end,
                                _combo(["vwap", "orb", "obv"], "ensemble", atr_stop_mult=1.0),
                                max_stocks=None)
    _print_row("P 全束+ATR(1x)", p["aggregate"])

    print("\n纯研究产物，不参与交易。对比 K 天花板：每笔 −0.0859% / 赢亏比 1.31x / 需 1.82x。")


if __name__ == "__main__":
    main()
