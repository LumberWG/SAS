"""GitHub 创业板策略 × 内置底反 回测对照。

把 conversation 里从 GitHub 筛出的 7 个创业板短线策略 + 内置「创业板底部反转」基线，
用同一套 BacktestEngine 跑相同窗口/ universe/ 趋势滤网（= 用户当前 live 设置），
横向比较成交/收益/胜率/回撤。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\bt_github_compare.py            # 全量(抽样300, 3窗)
    SAMPLE=60 QUICK=1 .venv\\Scripts\\python.exe scripts\\bt_github_compare.py   # 快验
环境变量：SAMPLE(抽样只数,默认300)  QUICK(=1 时只跑1窗/抽样60)
"""
from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 离线：快照后台刷新立即失败，复用本地缓存 ----
import app.data.akshare_source as aks
import app.data.snapshot_store as snap


class _FakeDS:
    def get_realtime_snapshot(self, *a, **k):
        raise RuntimeError("offline")

    def get_realtime_indices(self, *a, **k):
        raise RuntimeError("offline")


aks.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]
snap.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]

import app.strategies  # noqa: F401  (触发 __init__ 注册 github_strategies)
from app.backtest.engine import BacktestEngine, BacktestParams  # noqa: E402

SAMPLE = int(os.environ.get("SAMPLE", "300"))
SEED = 20261112
QUICK = os.environ.get("QUICK", "") == "1"

# 三个不同市场阶段的底反窗口（>=5 个月，满足策略 min_bars=130）
WINDOWS = [
    ("2024-02-01", "2024-06-30", "雪球危机后见底反弹+回踩"),
    ("2024-09-01", "2025-01-31", "政策底暴涨+回落"),
    ("2025-04-01", "2025-08-31", "关税底回升"),
]
if QUICK:
    WINDOWS = WINDOWS[:1]

# 策略 -> 该策略回测用的固定持有天数（0=不强制，靠止损/止盈/时间止损离场）
HOLD_DAYS = {
    "创业板涨停后5日": 5,
    "动量T3": 3,
}

STRATEGIES = [
    "创业板底部反转",     # 基线
    "量价共振2560",
    "RSI超跌反弹",
    "均线多空MA2055",
    "MACD_DMA叠加",
    "创业板涨停后5日",
    "底反ADX增强",
    "动量T3",
]


def cyb_universe() -> list[str]:
    from app.data.service import get_stock_pool
    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if SAMPLE and len(codes) > SAMPLE:
        codes = random.Random(SEED).sample(codes, SAMPLE)
    return codes


async def run_one(strategy: str, start: str, end: str, codes: list[str]) -> dict:
    params = BacktestParams(
        strategy=strategy,
        start_date=start,
        end_date=end,
        trend_filter=True,        # 沿用用户当前 live 设置
        trend_ma=30,
        trend_index="",           # 等权合成（用户当前）
        universe_size=len(codes),
        initial_capital=100_000.0,
        codes=codes,
        max_positions=8,
        trailing_pct=0.06,        # 6% 跟踪止损保护利润
        time_stop_days=10,        # 10 日未启动则砍死钱
        max_hold_days=HOLD_DAYS.get(strategy, 0),
        commission=0.0003,
        stamp_tax=0.001,
    )
    return await BacktestEngine().run(params)


def _fmt(m: dict) -> str:
    return (f"成交{m.get('trade_count',0):>4}笔 | 收益{m.get('total_return',0):+8.2f}% "
            f"| 胜率{m.get('win_rate',0):5.1f}% | 回撤{m.get('max_drawdown',0):6.2f}% "
            f"| 夏普{m.get('sharpe',0):5.2f}")


async def main() -> None:
    codes = cyb_universe()
    print(f"策略对比 | 创业板 universe {len(codes)} 只(抽样{SAMPLE}, 种子{SEED}) | 趋势滤网 MA30+等权(live设置)")
    print("统一风控: 最大持仓8 / 跟踪止损6% / 时间止损10日\n")

    for s, e, label in WINDOWS:
        print(f"===== {s} ~ {e}  ({label}) =====")
        for name in STRATEGIES:
            r = await run_one(name, s, e, codes)
            m = r["metrics"]
            hold = HOLD_DAYS.get(name, 0)
            tag = f"[持有{hold}日]" if hold else ""
            print(f"  {name:12}{tag:8}: {_fmt(m)}")
        print()

    print("注：累计收益/胜率为各窗简单相加，未含手续费滑点外的基准；方向性比较(参与度/质量)有效。QUICK 模式仅1窗。")


if __name__ == "__main__":
    asyncio.run(main())
