"""分钟级切入点回测：开盘后窗口内「等回踩再买」vs 直接开盘价买。

口径说明：
- ``intraday_minutes=True`` + ``entry_window_minutes=N``：扫描买入日前 N 分钟的 5 分钟收盘价，
  ``entry_pullback>0`` 时要求**窗口内曾回踩**到 ``信号价×(1-pullback)``，以首次触及价成交；
  窗口内没等到就不买。不追高仍按 ``entry_max_chase`` 对每一根分钟价生效。
- 没有 5 分钟线缓存的股票自动回落「开盘价成交」，因此结果覆盖度受 ``data/minute5`` 限制
  （输出里打印覆盖率，覆盖率低时结论只能看方向）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\minute_entry_backtest.py
    .venv\\Scripts\\python.exe scripts\\minute_entry_backtest.py 2025-01-02 2026-10-20 500
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.pipeline import PipelineParams, run_pipeline  # noqa: E402
from app.data.minute import MINUTE5_DIR  # noqa: E402

VARIANTS: dict[str, dict] = {
    "A 基线(开盘价成交)": {},
    "B 30min+回踩1%": {
        "intraday_minutes": True, "entry_window_minutes": 30, "entry_pullback": 0.01,
    },
    "C 30min+回踩2%": {
        "intraday_minutes": True, "entry_window_minutes": 30, "entry_pullback": 0.02,
    },
    "D 60min+回踩2%": {
        "intraday_minutes": True, "entry_window_minutes": 60, "entry_pullback": 0.02,
    },
}


async def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-10-20"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    covered = len(list(MINUTE5_DIR.glob("*.parquet"))) if MINUTE5_DIR.exists() else 0
    print(f"窗口 {start} ~ {end}，池 Top{universe}；5分钟线缓存覆盖 {covered} 只")
    print(f"{'变体':<20}{'收益':>9}{'基准':>9}{'回撤':>9}{'交易':>7}{'胜率':>7}")
    base = {"start_date": start, "end_date": end, "universe_size": universe}
    for name, extra in VARIANTS.items():
        result = await run_pipeline(PipelineParams(**base, **extra))
        m = result["metrics"]
        print(
            f"{name:<20}{m['total_return']:>8.2f}%{m['benchmark_return']:>8.2f}%"
            f"{m['max_drawdown']:>8.2f}%{m['trade_count']:>7}{m['win_rate']:>6.1f}%"
        )


if __name__ == "__main__":
    asyncio.run(main())
