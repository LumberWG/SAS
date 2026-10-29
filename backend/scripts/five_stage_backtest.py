"""五段式执行规则统一回测：同一窗口 / 同一股票池下对比队列 topM 与切入点的影响。

各变体共享同一份信号缓存（同窗口 + 同池 + 同评分模型），因此第一个变体做完预处理后，
其余变体只需重跑撮合，几秒即可出结果。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\five_stage_backtest.py
    .venv\\Scripts\\python.exe scripts\\five_stage_backtest.py 2025-01-02 2026-10-20 500
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.pipeline import PipelineParams, run_pipeline  # noqa: E402

#: 变体定义：条目名 → PipelineParams 覆盖字段
VARIANTS: dict[str, dict] = {
    "A 基线(线上旧行为)": {},
    "B 不追高1.5%": {"entry_max_chase": 0.015},
    "C 回踩2%": {"entry_pullback": 0.02},
    "D 队列topM=5": {"queue_top_m": 5},
    "E 五段式全开": {"entry_max_chase": 0.015, "entry_pullback": 0.02, "queue_top_m": 5},
}


async def main() -> None:
    start = sys.argv[1] if len(sys.argv) > 1 else "2025-01-02"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-10-20"
    universe = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    base = {"start_date": start, "end_date": end, "universe_size": universe}
    print(f"窗口 {start} ~ {end}，池 Top{universe}（线上默认参数，仅逐一打开五段式规则）")
    print(f"{'变体':<18}{'收益':>9}{'基准':>9}{'回撤':>9}{'交易':>7}{'胜率':>7}")
    for name, extra in VARIANTS.items():
        result = await run_pipeline(PipelineParams(**base, **extra))
        m = result["metrics"]
        print(
            f"{name:<18}{m['total_return']:>8.2f}%{m['benchmark_return']:>8.2f}%"
            f"{m['max_drawdown']:>8.2f}%{m['trade_count']:>7}{m['win_rate']:>6.1f}%"
        )


if __name__ == "__main__":
    asyncio.run(main())
