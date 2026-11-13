"""对比：创业板底部反转策略，趋势滤网 MA60 vs MA20，随机抽若干段（各约一个月）离线回测。

复现用户疑问：创业板刚启动（指数还在 MA60 下方）时，底反候选被趋势滤网清空导致买不上。
本脚本用 BacktestEngine（带 trend_filter / trend_ma 的真实回测引擎）做对照，
看把 MA 从 60 降到 20 后，能否在板块刚启动时解锁更多开仓。

重要修正：K 线 parquet 的 turnover_rate 本就是「小数百分比」（0.90 = 0.90%），
与策略阈值 turnover_min=5.0（=5%）口径一致，**不存在单位 bug**。
故本脚本不做任何 turnover 缩放 monkey-patch，真实反映策略过滤效果。

指数口径默认「300/301 等权合成指数」（与实盘/回测默认一致；trend_index 留空）。

用法（backend 目录）：
    .venv\\Scripts\\python.exe scripts\\bt_cyb_ma_compare.py
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

import app.strategies.builtin  # noqa: E402,F401 - 触发 @registry.register 注册底反策略
from app.backtest.engine import BacktestEngine, BacktestParams  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402
from app.data.snapshot_store import snapshot_store  # noqa: E402

SPAN = 100         # 约 5 个月交易日（策略 min_bars=130，窗口过短会被引擎整池跳过）
DRAWS = 1          # 随机抽几段（降低单段偶然性）
SEED = 20261112
SAMPLE = 300       # 抽样加速（趋势闸门对全样本一致，抽样不影响 MA20/MA60 对比方向）
STRATEGY = "创业板底部反转"


def cyb_universe(sample: int = 0) -> list[str]:
    """创业板（300/301 开头）选股池。

    关键修正：不能用「快照成交额前 N」——那批是创业板大票（宁德/中际旭创等），
    换手率仅 0.9%~5.5%，过不了底反策略 5% 换手门槛，导致 0 信号。
    底反真正要抓的是小盘创业板（换手常 >5%）。故直接用股票池全部 300/301 代码，
    与实盘 screener 全量扫描口径一致。

    sample>0 时随机抽 N 只（加速用，趋势闸门对全样本一致，抽样不影响 MA20/MA60 对比方向）。
    """
    from app.data.service import get_stock_pool
    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if sample and len(codes) > sample:
        codes = random.Random(SEED).sample(codes, sample)
    return codes


def trading_dates() -> list[str]:
    idx = kline_cache.get("idx_sh000001")
    if idx is None or getattr(idx, "empty", True):
        raise SystemExit("缺少上证指数 K 线（idx_sh000001），无法确定交易日历")
    return [str(d)[:10] for d in idx["date"]]


def pick(dates: list[str], span: int, rng: random.Random) -> tuple[str, str]:
    cands = [
        i for i in range(len(dates) - span)
        if dates[i] >= "2023-01-01" and dates[i + span] <= dates[-1]
    ]
    if not cands:
        raise SystemExit("可用区间不足，无法抽取一个月")
    i = rng.choice(cands)
    return dates[i], dates[i + span]


async def run_one(ma: int, start: str, end: str, codes: list[str]) -> dict:
    params = BacktestParams(
        strategy=STRATEGY,
        start_date=start,
        end_date=end,
        trend_filter=True,
        trend_ma=ma,
        trend_index="",          # 空 = 300/301 等权合成指数（与实盘/回测默认一致）
        universe_size=300,
        initial_capital=100_000.0,
        codes=codes,
    )
    return await BacktestEngine().run(params)


def _fmt(m: dict) -> str:
    return (
        f"成交 {m.get('trade_count', 0):>3} 笔 | 收益 {m.get('total_return', 0):+7.2f}% "
        f"| 基准 {m.get('benchmark_return', 0):+7.2f}% | 胜率 {m.get('win_rate', 0):5.1f}% "
        f"| 回撤 {m.get('max_drawdown', 0):6.2f}% | 期末 {m.get('final_equity', 0):>12,.0f}"
    )


async def main() -> None:
    dates = trading_dates()
    rng = random.Random(SEED)
    codes = cyb_universe(SAMPLE)
    print(f"种子 {SEED} | 策略 {STRATEGY} | 趋势滤网开 | 创业板 universe {len(codes)} 只(抽样{SAMPLE})")
    print(f"指数口径: 300/301 等权合成（与实盘默认一致）| 随机抽 {DRAWS} 段（各 {SPAN} 交易日）\n")

    tot_t60 = tot_t20 = 0
    for d in range(1, DRAWS + 1):
        s, e = pick(dates, SPAN, rng)
        r60 = await run_one(60, s, e, codes)
        r20 = await run_one(20, s, e, codes)
        m60, m20 = r60["metrics"], r20["metrics"]
        tot_t60 += m60.get("trade_count", 0)
        tot_t20 += m20.get("trade_count", 0)
        print(f"第 {d} 段  {s} ~ {e}:")
        print(f"  MA60: {_fmt(m60)}")
        print(f"  MA20: {_fmt(m20)}")
        t60 = {t["code"] for t in r60.get("trades", [])}
        t20 = {t["code"] for t in r20.get("trades", [])}
        only20 = sorted(t20 - t60)
        print(f"  MA20 解锁(MA60 没买到的票): {only20 if only20 else '—'}")
        print()

    print(f">>> 随机段合计: MA60 成交 {tot_t60} 笔 | MA20 成交 {tot_t20} 笔 "
          f"(MA20 多 {tot_t20 - tot_t60} 笔)\n")

    # 典型底反月对照：创业板明确从底部启动的时段，用于验证 MA20 是否真能解锁买入
    FIXED = [
        ("2024-02-01", "2024-06-30"),   # 雪球敲入流动性危机后见底反弹
        ("2024-09-01", "2024-12-31"),   # 政策底，创业板单周暴涨
        ("2025-04-01", "2025-07-31"),   # 关税冲击后见底回升
    ]
    print("===== 典型底反月对照（非随机，用于验证 MA20 解锁效果）=====\n")
    for s, e in FIXED:
        r60 = await run_one(60, s, e, codes)
        r20 = await run_one(20, s, e, codes)
        m60, m20 = r60["metrics"], r20["metrics"]
        print(f"{s} ~ {e}:")
        print(f"  MA60: {_fmt(m60)}")
        print(f"  MA20: {_fmt(m20)}")
        t60 = {t["code"] for t in r60.get("trades", [])}
        t20 = {t["code"] for t in r20.get("trades", [])}
        only20 = sorted(t20 - t60)
        print(f"  MA20 比 MA60 多买到的票: {only20 if only20 else '—'}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
