"""随机抽一段历史「一个月」，用线上参数按 10 万本金跑一遍，看真实交易序列。

为什么单独写一个脚本而不是复用 ``regression.py``：
``regression.py`` 的窗口是写死的（A~F/G~J），多用于参数对比；
这里要的是**随机抽样 + 逐日明细**，重点是回答「随便挑一个月，这个程序会怎么打」。

不做上帝视角（与 ``pipeline`` 的既有约束一致）：
  - 信号只用**当日收盘**及之前的数据（评分、因子、择时判定都基于 t 日及之前）；
  - 买/卖委托一律在**次日开盘价**成交，不在当日收盘价上「抢跑」；
  - 盘中止损用当日 low 判触发，但成交价取 ``min(开盘价, 止损价)``（跳空低开不会假装卖在止损价）；
  - 移动止损的峰值只用**之前交易日**已知的最高价，不用当日 high。

用法（backend 目录）::

    # 随机抽 3 个月，打印明细
    .venv\\Scripts\\python.exe scripts\\random_month.py --draws 3 --seed 42
    # 换参数口径（默认线上 default）
    .venv\\Scripts\\python.exe scripts\\random_month.py --draws 1 --profile default --universe 300
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.backtest.profiles import PROFILES  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402


def _trading_dates() -> list[str]:
    idx = kline_cache.get("idx_sh000001")
    if idx is None or idx.empty:
        raise SystemExit("缺少上证指数 K 线（idx_sh000001），无法确定交易日历")
    return [str(d)[:10] for d in idx["date"]]


def _pick(dates: list[str], span: int, rng: random.Random) -> tuple[str, str]:
    """随机取一段：起点需留足指标预热（60 根 ≈ 2023-01 之后）与完整跨度。"""
    candidates = [
        i for i in range(len(dates) - span)
        if dates[i] >= "2023-01-01" and dates[i + span] <= dates[-1]
    ]
    if not candidates:
        raise SystemExit("可用区间不足，无法抽取一个月")
    i = rng.choice(candidates)
    return dates[i], dates[i + span]


def _print_result(tag: str, result: dict) -> None:
    m, p = result["metrics"], result["params"]
    print(
        f"\n===== {tag} | {result['start_date']} ~ {result['end_date']}"
        f"（{m['trading_days']} 个交易日）====="
    )
    print(
        f"本金 {m['initial_capital']:,.0f} → {m['final_equity']:,.2f}"
        f" | 收益 {m['total_return']:+.2f}% | 基准 {m['benchmark_return']:+.2f}%"
        f" | 超额 {m['total_return'] - m['benchmark_return']:+.2f}%"
    )
    print(
        f"最大回撤 {m['max_drawdown']:.2f}% | {m['trade_count']} 笔"
        f" | 胜率 {m['win_rate']:.1f}% | 平均每笔 {m['avg_pnl_pct']:+.2f}%"
        f" | 期末持仓 {m['open_positions']} 只"
    )
    print(
        f"参数: 池 Top{p['universe_size']} 自选{p['watch_cap']} 最多{p['max_positions']}只"
        f" 止损{p['stop_loss']:.0%} 止盈{p['take_profit']:.0%} 持有{p['max_hold_days']}日"
        f" 总仓{p['max_total_exposure']:.0%} 单票{p['single_position_ratio']:.0%}"
    )

    print("\n-- 逐日净值 --")
    prev = float(m["initial_capital"])
    for point in result["equity_curve"]:
        eq = float(point["equity"])
        day_pct = (eq / prev - 1) * 100 if prev else 0.0
        prev = eq
        print(
            f"  {point['date']}  权益 {eq:>10,.2f}  当日 {day_pct:+6.2f}%"
            f"  现金 {point['cash']:>9,.2f}  持仓 {point['positions']:>2}"
        )

    if result["trades"]:
        print("\n-- 成交明细 --")
        for t in result["trades"]:
            print(
                f"  {t.get('buy_date', '')} → {t.get('sell_date', '')}"
                f"  {t.get('code')} {str(t.get('name') or '')[:6]:<6}"
                f"  {t.get('shares')} 股  成本 {t.get('buy_price', 0):.2f}"
                f" → 卖 {t.get('sell_price', 0):.2f}"
                f"  {t.get('pnl_pct', 0):+6.2f}%  {t.get('reason', '')}"
            )
    else:
        print("\n（区间内未产生任何成交）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draws", type=int, default=3, help="随机抽几个月")
    parser.add_argument("--span", type=int, default=21, help="每段交易日数（21≈一个月）")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN 股票池")
    parser.add_argument("--profile", default="default", help="方案名（见 profiles.py）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument(
        "--quiet", action="store_true",
        help="只打汇总（多段抽样时用，避免逐日明细刷屏）",
    )
    args = parser.parse_args()

    if args.profile not in PROFILES:
        raise SystemExit(f"未知方案 {args.profile}，可选 {list(PROFILES)}")
    seed = args.seed if args.seed is not None else random.randrange(10**6)
    rng = random.Random(seed)
    dates = _trading_dates()
    print(f"随机种子 {seed}（复现请加 --seed {seed}）| 方案 {args.profile}"
          f" | 池 Top{args.universe} | 本金 {args.capital:,.0f}")

    returns = []
    for draw in range(1, args.draws + 1):
        start, end = _pick(dates, args.span, rng)
        params = PipelineParams(
            start_date=start, end_date=end,
            universe_size=args.universe, initial_capital=args.capital,
            **PROFILES[args.profile],
        )
        result = _run_sync(params)
        if args.quiet:
            m = result["metrics"]
            print(
                f"  第 {draw:>2} 段 {result['start_date']} ~ {result['end_date']}"
                f" | 收益 {m['total_return']:+7.2f}% | 基准 {m['benchmark_return']:+7.2f}%"
                f" | 超额 {m['total_return'] - m['benchmark_return']:+7.2f}%"
                f" | 回撤 {m['max_drawdown']:6.2f}% | {m['trade_count']:>3} 笔"
                f" | 胜率 {m['win_rate']:5.1f}%",
                flush=True,
            )
        else:
            _print_result(f"第 {draw} 段", result)
        returns.append(result["metrics"]["total_return"])

    if len(returns) > 1:
        avg = sum(returns) / len(returns)
        print(f"\n===== {len(returns)} 段汇总 =====")
        print("  各段收益: " + "  ".join(f"{r:+.2f}%" for r in returns))
        print(f"  平均 {avg:+.2f}% | 最好 {max(returns):+.2f}% | 最差 {min(returns):+.2f}%")


if __name__ == "__main__":
    main()
