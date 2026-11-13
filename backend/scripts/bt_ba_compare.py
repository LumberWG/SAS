"""B+A 回测对照：创业板底部反转策略，趋势滤网「MA60+等权合成(旧默认)」 vs 「MA20+官方399006(当前设置)」，
并拆出 B-only(MA60+官方) / A-only(MA20+等权) 的边际贡献。

4 个组合：
  old = MA60 + 等权合成(None)        —— 改动前默认
  B   = MA60 + 官方399006             —— 只换指数
  A   = MA20 + 等权合成(None)         —— 只降 MA
  BA  = MA20 + 官方399006             —— 当前实盘设置（B+A）

用 BacktestEngine 真实回测引擎。离线：快照后台刷新立即失败，复用本地缓存。
trend_index="" 与 None 等价（引擎里 `params.trend_index or ""` 为假→等权合成）。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\bt_ba_compare.py
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
from app.data.cache import kline_cache  # noqa: E402

SAMPLE = 400       # 抽样加速；趋势闸门对全样本一致，抽样不影响方向性对比
SEED = 20261112
STRATEGY = "创业板底部反转"

# 三个不同市场阶段的底反窗口（均 >=5 个月，满足策略 min_bars=130，避免被引擎整池跳过）
WINDOWS = [
    ("2024-02-01", "2024-06-30", "雪球危机后见底反弹+回踩"),
    ("2024-09-01", "2025-01-31", "政策底暴涨+回落"),
    ("2025-04-01", "2025-08-31", "关税底回升"),
]

COMBOS = [
    ("old", 60, ""),           # 改动前默认
    ("B",   60, "sz399006"),   # 只换指数
    ("A",   20, ""),           # 只降 MA
    ("BA",  20, "sz399006"),   # 当前实盘设置
]


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
        trend_index=index,        # ""/None = 等权合成；"sz399006" = 官方创业板指
        universe_size=len(codes),
        initial_capital=100_000.0,
        codes=codes,
    )
    return await BacktestEngine().run(params)


def _fmt(m: dict) -> str:
    return (f"成交{m.get('trade_count',0):>3}笔 | 收益{m.get('total_return',0):+7.2f}% "
            f"| 胜率{m.get('win_rate',0):5.1f}% | 回撤{m.get('max_drawdown',0):6.2f}% "
            f"| 夏普{m.get('sharpe',0):4.2f} | 期末{m.get('final_equity',0):>11,.0f}")


async def main() -> None:
    codes = cyb_universe()
    print(f"策略 {STRATEGY} | 趋势滤网开 | 创业板 universe {len(codes)} 只(抽样{SAMPLE}, 种子{SEED})")
    print("组合: old=MA60+等权  B=MA60+官方399006  A=MA20+等权  BA=MA20+官方399006(当前设置)\n")

    agg = {n: {"t": 0, "ret": 0.0, "win_sum": 0.0} for n, _, _ in COMBOS}
    for s, e, label in WINDOWS:
        print(f"===== {s} ~ {e}  ({label}) =====")
        res = {}
        for name, ma, idx in COMBOS:
            r = await run_one(ma, idx, s, e, codes)
            m = r["metrics"]
            res[name] = r
            print(f"  {name:>3}: {_fmt(m)}")
            agg[name]["t"] += m.get("trade_count", 0)
            agg[name]["ret"] += m.get("total_return", 0.0)
            agg[name]["win_sum"] += m.get("win_rate", 0.0) * m.get("trade_count", 0)
        old_codes = {t["code"] for t in res["old"].get("trades", [])}
        ba_codes = {t["code"] for t in res["BA"].get("trades", [])}
        only_ba = sorted(ba_codes - old_codes)
        print(f"  >> BA 比 old 多买到的票({len(only_ba)}): {only_ba if only_ba else '—'}")
        print()

    print("===== 三窗口汇总 =====")
    for n, _, _ in COMBOS:
        a = agg[n]
        avg_win = (a["win_sum"] / a["t"]) if a["t"] else 0.0
        print(f"  {n:>3}: 总成交 {a['t']} 笔 | 累计收益 {a['ret']:+8.2f}% | 加权平均胜率 {avg_win:5.1f}%")
    print("\n注：累计收益为各窗口收益简单相加（非复利），仅用于横向比较参与度/方向性。")


if __name__ == "__main__":
    asyncio.run(main())
