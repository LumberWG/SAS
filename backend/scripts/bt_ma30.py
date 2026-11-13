"""MA30 专项回测：创业板底部反转策略，趋势滤网 MA30 对照 MA20 / MA60。

两个指数口径都跑，方便和此前结论对齐：
  MA30 + 官方399006   —— 当前实盘设置(MA30+B)
  MA30 + 等权合成("") —— 只降 MA 到 30、指数仍等权
并附 MA20/MA60 的官方指数口径作为参照（值摘自 bt_ba_compare.py 已跑结果，避免重复长跑）。

用 BacktestEngine 真实回测引擎。离线：快照后台刷新立即失败，复用本地缓存。
趋势滤网开；抽样 400 只创业板；3 个 5 月底反窗口（满足策略 min_bars=130）。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\bt_ma30.py
"""
from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 离线：让快照后台刷新立即失败，复用本地缓存（snapshot_latest.parquet）----
import app.data.akshare_source as aks
import app.data.snapshot_store as snap


class _FakeDS:
    def get_realtime_snapshot(self, *a, **k):
        raise RuntimeError("offline")

    def get_realtime_indices(self, *a, **k):
        raise RuntimeError("offline")


aks.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]
snap.get_data_source = lambda: _FakeDS()  # type: ignore[assignment]

import app.strategies.builtin  # noqa: E402,F401
from app.backtest.engine import BacktestEngine, BacktestParams  # noqa: E402

SAMPLE = 400
SEED = 20261112
STRATEGY = "创业板底部反转"

# 三个不同市场阶段的底反窗口（>=5 个月，满足 min_bars=130）
WINDOWS = [
    ("2024-02-01", "2024-06-30", "雪球危机后见底反弹+回踩"),
    ("2024-09-01", "2025-01-31", "政策底暴涨+回落"),
    ("2025-04-01", "2025-08-31", "关税底回升"),
]

# 本次实际跑的 MA30 组合
COMBOS = [
    ("MA30+B", 30, "sz399006"),   # 当前实盘设置
    ("MA30+等权", 30, ""),        # 只降 MA 到 30
]

# 此前 bt_ba_compare.py 已跑结果（官方399006 口径），仅作参照打印，不重跑
REF = {
    "MA20+B (sz399006)": (20, "  总成交 20 笔 | 累计收益 -25.59% | 加权胜率 15.0%"),
    "MA60+B (sz399006)": (60, "  总成交 23 笔 | 累计收益 -22.00% | 加权胜率 21.8%"),
    "MA60+等权 (old)   ": (60, "  总成交  9 笔 | 累计收益  -4.67% | 加权胜率 33.4%"),
}


def cyb_universe() -> list[str]:
    from app.data.service import get_stock_pool
    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if SAMPLE and len(codes) > SAMPLE:
        codes = random.Random(SEED).sample(codes, SAMPLE)
    return codes


async def run_one(ma: int, index: str, start: str, end: str, codes: list[str]) -> dict:
    params = BacktestParams(
        strategy=STRATEGY,
        start_date=start,
        end_date=end,
        trend_filter=True,
        trend_ma=ma,
        trend_index=index,
        universe_size=len(codes),
        initial_capital=100_000.0,
        codes=codes,
    )
    return await BacktestEngine().run(params)


def _fmt(m: dict) -> str:
    return (f"成交{m.get('trade_count',0):>3}笔 | 收益{m.get('total_return',0):+7.2f}% "
            f"| 胜率{m.get('win_rate',0):5.1f}% | 回撤{m.get('max_drawdown',0):6.2f}% "
            f"| 夏普{m.get('sharpe',0):4.2f}")


async def main() -> None:
    codes = cyb_universe()
    print(f"策略 {STRATEGY} | 趋势滤网开 | 创业板 universe {len(codes)} 只(抽样{SAMPLE}, 种子{SEED})")
    print("本次实跑: MA30+B(官方399006) / MA30+等权 ; 参照: 此前已跑的 MA20/MA60 官方指数口径\n")

    agg = {n: {"t": 0, "ret": 0.0, "win_sum": 0.0} for n, _, _ in COMBOS}
    for s, e, label in WINDOWS:
        print(f"===== {s} ~ {e}  ({label}) =====")
        for name, ma, idx in COMBOS:
            r = await run_one(ma, idx, s, e, codes)
            m = r["metrics"]
            print(f"  {name:>10}: {_fmt(m)}")
            agg[name]["t"] += m.get("trade_count", 0)
            agg[name]["ret"] += m.get("total_return", 0.0)
            agg[name]["win_sum"] += m.get("win_rate", 0.0) * m.get("trade_count", 0)
        print()

    print("===== 三窗口汇总（本次实跑）=====")
    for n, _, _ in COMBOS:
        a = agg[n]
        avg_win = (a["win_sum"] / a["t"]) if a["t"] else 0.0
        print(f"  {n:>10}: 总成交 {a['t']} 笔 | 累计收益 {a['ret']:+8.2f}% | 加权胜率 {avg_win:5.1f}%")

    print("\n===== 参照（bt_ba_compare.py 已跑，官方399006 口径，未重跑）=====")
    for k, (_, desc) in REF.items():
        print(f"  {k}: {desc}")
    print("\n注：累计收益为各窗口简单相加（非复利），未含手续费/滑点；方向性比较(参与度/质量)有效，绝对数字供量级参考。")


if __name__ == "__main__":
    asyncio.run(main())
