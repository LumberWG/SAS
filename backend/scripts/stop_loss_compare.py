"""三档止损架构对比：固定 8% vs 当前 max(8%,2ATR) vs 激进 ATR 权重。

直击「量化共识 8% 止损踩踏」担忧：调小固定止损只是平移簇集点，
真正抗踩踏的是 ATR 自适应（让止损价随波动率分散）。跑多时段对比
最大回撤 / 胜率 / 交易次数 / 收益，给数据支撑。

用法（backend 目录，后台运行）:
    .venv\\Scripts\\python.exe scripts\\stop_loss_compare.py
结果写 logs/stop_loss_compare.json，并每段打印一行进度。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.pipeline import PipelineParams, _run_sync  # noqa: E402
from app.config import get_settings  # noqa: E402

# 三档架构（stop_loss 仅作为 ATR 缺失时的 fallback）
CONFIGS = {
    "A_fixed_8pct": dict(stop_atr_mult=None, stop_loss=0.08,
                         stop_atr_min=0.03, stop_atr_max=0.15),
    "B_max8_2atr": dict(stop_atr_mult=2.0, stop_loss=0.08,
                        stop_atr_min=0.08, stop_atr_max=0.20),   # 当前实盘口径
    "C_aggr_atr": dict(stop_atr_mult=3.0, stop_loss=0.08,
                       stop_atr_min=0.06, stop_atr_max=0.25),    # 更激进 ATR 权重
}
PERIODS = [
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
]


def main() -> None:
    s = get_settings()
    log_dir = str(s.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    out = os.path.join(log_dir, "stop_loss_compare.json")
    summary: list[dict] = []
    for name, cfg in CONFIGS.items():
        for (start, end) in PERIODS:
            t0 = time.time()
            r = _run_sync(PipelineParams(
                start_date=start, end_date=end, universe_size=500, **cfg))
            m = dict(r["metrics"])
            m.update(dict(config=name, start=start, end=end,
                          seconds=round(time.time() - t0, 1)))
            summary.append(m)
            keys = ["total_return", "max_drawdown", "win_rate",
                    "trade_count", "sharpe", "avg_hold_days"]
            row = {k: m.get(k) for k in keys}
            print(f"[done] {name} {start}~{end} {row}  ({m['seconds']}s)",
                  flush=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=float)
    print(f"SAVED {out}", flush=True)


if __name__ == "__main__":
    main()
