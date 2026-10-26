"""CLI：全流水线回测（扫描 → 加自选 → 14因子排序 → 评分加权下单）。

实现在 ``app/backtest/pipeline.py``（与 API / 回测中心共用同一套逻辑），
本脚本只负责命令行参数、控制台打印与 JSON 落盘，方便做参数实验。

防“上帝视角”的约束见 pipeline 模块 docstring。

用法（在 backend 目录）:
    .venv\\Scripts\\python.exe scripts\\pipeline_backtest.py --start 2026-09-28 --end 2026-10-23
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import (  # noqa: E402
    STOP_LOSS_RATIO,
    TAKE_PROFIT_RATIO,
    PipelineParams,
    _run_sync,
)
from app.config import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-09-28")
    parser.add_argument("--end", default="2026-10-23")
    parser.add_argument("--universe", type=int, default=500, help="每日成交额 TopN 股票池")
    parser.add_argument("--capital", type=float, default=None, help="缺省取模拟盘配置")
    parser.add_argument("--max-positions", type=int, default=None, help="缺省取模拟盘配置")
    parser.add_argument("--watch-cap", type=int, default=60, help="自选股上限（FIFO）")
    parser.add_argument("--add-per-day", type=int, default=50, help="每日扫描后加自选上限")
    parser.add_argument("--stop-loss", type=float, default=STOP_LOSS_RATIO,
                        help="止损比例（相对持仓成本），短线常用 0.04~0.05")
    parser.add_argument("--take-profit", type=float, default=TAKE_PROFIT_RATIO,
                        help="止盈比例（相对持仓成本），短线常用 0.08~0.10")
    parser.add_argument("--max-hold-days", type=int, default=0,
                        help="时间止损：持有 N 个交易日后次日开盘卖出（0=不限制）")
    parser.add_argument("--tag", default="", help="结果文件名后缀，便于多组参数对比")
    args = parser.parse_args()

    result = _run_sync(PipelineParams(
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        max_positions=args.max_positions,
        universe_size=args.universe,
        watch_cap=args.watch_cap,
        add_per_day=args.add_per_day,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        max_hold_days=args.max_hold_days,
    ))

    metrics = result["metrics"]
    print("\n===== 回测结果 =====")
    for k, v in metrics.items():
        print(f"{k:>22}: {v}")

    print("\n持仓:")
    for code, p in result["positions"].items():
        print(f"  {code} {p['name']} {p['shares']}股 成本{p['cost']:.2f} 建仓分{p['score']:.0f}")

    print("\n成交明细:")
    for t in result["trades"]:
        print(
            f"  {t['buy_date']}→{t['sell_date']} {t['code']} {t['name']} "
            f"{t['shares']}股 {t['buy_price']}→{t['sell_price']} "
            f"{t['pnl_pct']:+.2f}%  {t['reason']}"
        )

    print("\n净值曲线:")
    for p in result["equity_curve"]:
        print(f"  {p['date']}  {p['equity']:>12,.2f}  仓位{p['positions']}只")

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"pipeline_backtest_{result['end_date']}{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
