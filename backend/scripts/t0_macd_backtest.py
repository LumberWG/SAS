# -*- coding: utf-8 -*-
"""5 分钟 MACD(6,38,6) T+0 做T策略回测（学习研究用途，不接实盘）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\t0_macd_backtest.py
    .venv\\Scripts\\python.exe scripts\\t0_macd_backtest.py 2025-01-02 2026-11-09 100

默认回测全市场 5 分钟本地数据（data/minute5_fsdb）前 N 只，输出十六组对照：
- 完整纪律(A) / 关止损(B) / 仅正T(C) / 0轴下方金叉(D) / 强趋势+高振幅(E) /
  波动自适应+支撑(F) / 柱拐头+量能确认(G) / 支撑阻力均值回归(H) / MACD柱背离(H2) /
  盈亏比优化(I) / 解封赢家(J) / 60分钟方向偏置(K) / 60分钟偏置(H2基准, K2) /
  偏置+解封赢家(K3) / 120分钟偏置(K4) / 120分钟偏置+解封(K5)；
  逐步验证「执行力」「选票 regime」「入场质量」「信号源本身」「非顺势信号」
  「payoff 结构」「更高周期方向过滤」「更高周期强度」是否真的影响盈亏；
- 每只标的的回合数与平均每笔净收益，按贡献排序。

所有数据走本地 parquet，免联网、可复现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.minute import MINUTE5_DIR  # noqa: E402
from app.strategies.t0_macd import T0Params, simulate_universe  # noqa: E402


def _list_codes(universe: int) -> list[str]:
    if not MINUTE5_DIR.exists():
        return []
    codes = sorted(p.stem for p in MINUTE5_DIR.glob("*.parquet"))
    return codes[:universe]


def _print_row(name: str, m: dict) -> None:
    # 盈亏平衡所需赢亏比 = (1-win_rate)/win_rate
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


def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-11-09"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    codes = _list_codes(universe)
    if not codes:
        print("未找到本地 5 分钟数据，请先预下载 data/minute5_fsdb。")
        return
    print(f"区间 {start} ~ {end}，标的 Top{universe}，本地 5 分钟覆盖 {len(codes)} 只\n")
    print(
        f"{'变体':<22}{'回合':>7}{'胜率':>9}{'均净%':>11}"
        f"{'赢亏比':>8}{'需':>8}{'最大回撤':>11}"
    )

    # A：完整纪律（板哥原文设定）
    a = simulate_universe(
        codes, start, end, T0Params(),
        max_stocks=None,
    )
    _print_row("A 完整纪律", a["aggregate"])

    # B：关掉破位/时间止损（纪律缺失对照组）
    b_params = T0Params(stop_pct=0.0, max_hold_bars=10 ** 9)
    b = simulate_universe(codes, start, end, b_params, max_stocks=None)
    _print_row("B 无止损(纪律缺失)", b["aggregate"])

    # C：仅正T（关闭反T）
    c_params = T0Params(allow_reverse=False)
    c = simulate_universe(codes, start, end, c_params, max_stocks=None)
    _print_row("C 仅正T(关反T)", c["aggregate"])

    # D：0 轴下方金叉才买（板哥强调的"低位金叉更可靠"）
    d_params = T0Params(zero_axis_filter="below")
    d = simulate_universe(codes, start, end, d_params, max_stocks=None)
    _print_row("D 0轴下方金叉", d["aggregate"])

    # E：强趋势/高振幅/流动性 硬过滤（板哥原文"只在日线趋势向上+振幅≥2.5%+流动性好才做T"）
    e_params = T0Params(trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8)
    e = simulate_universe(codes, start, end, e_params, max_stocks=None)
    _print_row("E 强趋势+高振幅", e["aggregate"])

    # F：在 E 基础上叠加 波动率自适应仓位 + 支撑位附近金叉（看能否推过盈亏平衡）
    f_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
    )
    f = simulate_universe(codes, start, end, f_params, max_stocks=None)
    _print_row("F 波动自适应+支撑", f["aggregate"])

    # G：信号源替换（MACD柱拐头 + 量能确认），在 F 基础上叠加，验证能否推过盈亏平衡
    g_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="hist_turn", vol_confirm=True,
    )
    g = simulate_universe(codes, start, end, g_params, max_stocks=None)
    _print_row("G 柱拐头+量能确认", g["aggregate"])

    # H：非顺势信号源——支撑/阻力均值回归（买支撑卖阻力，而非顺势追），在 G 基础上叠加
    h_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="reversal", vol_confirm=True,
    )
    h = simulate_universe(codes, start, end, h_params, max_stocks=None)
    _print_row("H 支撑阻力均值回归", h["aggregate"])

    # H2：非顺势信号源——MACD 柱背离（价格新低柱不新低作买、价格新高柱不新高作卖），抓反转
    h2_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
    )
    h2 = simulate_universe(codes, start, end, h2_params, max_stocks=None)
    _print_row("H2 MACD柱背离", h2["aggregate"])

    # I：在 H2（非顺势背离，最强信号）基础上改 payoff 结构——
    # 追踪止盈(让赢家跑、封回吐) + 更紧的 ATR 硬止损封顶亏损 + 固定止盈锁定盈利，
    # 攻击「31.9% 胜率下需赢亏比≈2.13 才够盈亏平衡」的缺口
    i_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
    )
    i = simulate_universe(codes, start, end, i_params, max_stocks=None)
    _print_row("I 盈亏比优化", i["aggregate"])

    # J：在 I 基础上「解封赢家」——去掉固定止盈(让赢家自然跑到反转)，放宽追踪回撤，
    # 同时保留更紧的 ATR 硬止损封顶亏损。直接检验「赢家被过早封顶」的诊断假设。
    j_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.0, stop_pct=0.008,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.010,
        take_profit_pct=0.0,
    )
    j = simulate_universe(codes, start, end, j_params, max_stocks=None)
    _print_row("J 解封赢家", j["aggregate"])

    # K：在 I（非顺势背离+追踪止盈+紧止损+固定止盈，全样本最强）基础上，叠加
    # 60 分钟方向偏置过滤——只做与更高周期方向一致的做T（正T需 60min 偏多、反T需偏空），
    # 把「在小周期反转里找大周期顺势的回调买点」落地，检验能否推过盈亏平衡
    k_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
        bias_filter=True, bias_tf=60, bias_mode="macd",
    )
    k = simulate_universe(codes, start, end, k_params, max_stocks=None)
    _print_row("K 60min偏置(I)", k["aggregate"])

    # K2：在 H2（非顺势背离，无 payoff 优化）基础上叠加 60 分钟偏置，独立验证偏置过滤本身的价值
    k2_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        bias_filter=True, bias_tf=60, bias_mode="macd",
    )
    k2 = simulate_universe(codes, start, end, k2_params, max_stocks=None)
    _print_row("K2 60min偏置(H2)", k2["aggregate"])

    # K3：在 K（60min偏置 + I全样本最强配置）基础上「解封赢家」——去掉固定止盈、
    # 放宽追踪回撤、更紧的 ATR 硬止损封顶亏损。直接检验 #145 提出的第一刀：
    # 「偏置下再放大赢家」（更高周期已筛掉逆势笔，留下的顺势回调里让赢家自然跑到反转）。
    k3_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.0, stop_pct=0.008,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.010,
        take_profit_pct=0.0,
        bias_filter=True, bias_tf=60, bias_mode="macd",
    )
    k3 = simulate_universe(codes, start, end, k3_params, max_stocks=None)
    _print_row("K3 偏置+解封赢家", k3["aggregate"])

    # K4：在 K（I 全样本最强配置 + 60min 偏置）基础上，把更高周期从 60 分钟升到
    # 120 分钟（更强趋势过滤），直接检验 #145 提出的第二刀「换更强更高周期」。
    k4_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.2, stop_pct=0.010,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.005,
        take_profit_pct=0.015,
        bias_filter=True, bias_tf=120, bias_mode="macd",
    )
    k4 = simulate_universe(codes, start, end, k4_params, max_stocks=None)
    _print_row("K4 120min偏置", k4["aggregate"])

    # K5：120 分钟偏置 + 解封赢家（K3 与 K4 的交叠），同时测试「更强更高周期」
    # 与「偏置下再放大赢家」两个杠杆的合力。
    k5_params = T0Params(
        trend_filter=True, min_daily_amp_pct=2.5, min_daily_amount=1e8,
        vol_adaptive=True, support_entry=True,
        signal_mode="divergence", vol_confirm=True,
        atr_stop_mult=1.0, stop_pct=0.008,
        trailing_tp=True, trail_activate_pct=0.004, trail_pct=0.010,
        take_profit_pct=0.0,
        bias_filter=True, bias_tf=120, bias_mode="macd",
    )
    k5 = simulate_universe(codes, start, end, k5_params, max_stocks=None)
    _print_row("K5 120min偏置+解封", k5["aggregate"])

    # 贡献榜（前 10）
    top = a["per_stock"][:10]
    if top:
        print("\n贡献榜 Top10（完整纪律，按累计净%排序）:")
        print(f"{'代码':<10}{'回合':>7}{'胜率':>8}{'均净%':>10}{'Σ净%':>11}{'天数':>6}")
        for s in top:
            print(
                f"{s['code']:<10}{s['rounds']:>7}{s['win_rate']:>7.1f}%"
                f"{s['avg_net_pct']:>9.4f}%{s['total_net_pct']:>10.2f}%"
                f"{s['days_with_trade']:>6}"
            )

    agg = a["aggregate"]
    print(
        f"\n扫描 {a['scanned']} 只；合计 T 回合 {agg['rounds']} 次，"
        f"有交易天数 {agg['days_with_trade']} 天，胜率 {agg['win_rate']}%，"
        f"平均每笔净 {agg['avg_net_pct']}%（成本已扣）。纯研究产物，不参与交易。"
    )


if __name__ == "__main__":
    main()
