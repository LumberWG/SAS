"""验证 动量T3 / 底反ADX增强 校准后行为：新闸门相对关闭基线是否按计划过滤。

离线（复用本地缓存），仅统计信号条数，不做完整盈亏回测。
"""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.data.akshare_source as aks
import app.data.snapshot_store as snap
class _FakeDS:
    def get_realtime_snapshot(self, *a, **k): raise RuntimeError("offline")
    def get_realtime_indices(self, *a, **k): raise RuntimeError("offline")
aks.get_data_source = lambda: _FakeDS()
snap.get_data_source = lambda: _FakeDS()

import app.strategies  # noqa: F401
from app.backtest.engine import BacktestEngine, _WARMUP_BARS
from app.data.service import get_kline_cached, get_stock_pool
from app.indicators import add_all_indicators
from app.config import get_settings
from app.strategies import registry

SAMPLE = int(os.environ.get("SAMPLE", "200"))
START = os.environ.get("START", "2022-01-01")
END = os.environ.get("END", "2026-11-12")


async def load(codes, warmup_start, end):
    out = {}
    async def one(c):
        df = await asyncio.to_thread(get_kline_cached, c)
        if df is None or df.empty: return
        df = df[df["date"] >= warmup_start]; df = df[df["date"] <= end]
        if len(df) < 130: return
        df = df.reset_index(drop=True)
        df = await asyncio.to_thread(add_all_indicators, df)
        out[c] = df
    await asyncio.gather(*(one(c) for c in codes))
    return out


def count(strategy, klines, start):
    eng = BacktestEngine()
    n = 0
    for code, df in klines.items():
        s = eng._first_index_on_or_after(df, start)
        n += len(eng._collect_events(strategy, df, s, code, ""))
    return n


async def main():
    settings = get_settings()
    pool = get_stock_pool()
    cyb = pool[pool["code"].astype(str).str.startswith(("300", "301"))]
    codes = cyb["code"].astype(str).tolist()
    if SAMPLE and len(codes) > SAMPLE:
        import random
        codes = random.Random(20261001).sample(codes, SAMPLE)
    warmup_start = (datetime.strptime(START, "%Y-%m-%d") - timedelta(days=_WARMUP_BARS)).strftime("%Y-%m-%d")
    klines = await load(codes, warmup_start, END)
    print(f"载入 {len(klines)} 只创业板，区间 {START}~{END}\n")

    # 动量T3：默认(跳过涨停) vs 关闭跳过
    m_on = registry.create("动量T3")
    m_off = registry.create("动量T3", skip_limit_up=False)
    n_on = count(m_on, klines, START)
    n_off = count(m_off, klines, START)
    print(f"【动量T3】跳过涨停ON={n_on}笔  跳过涨停OFF={n_off}笔  "
          f"(跳过过滤掉 {n_off - n_on} 笔, {(n_off-n_on)/n_off*100:.1f}%)")

    # 底反ADX增强：默认(21<ADX<30+方向+MA20) vs 闸门全开(0<ADX<100, 无方向/MA20)
    b_def = registry.create("底反ADX增强")
    b_open = registry.create("底反ADX增强", adx_lo=0.0, adx_hi=100.0, adx_dir=False if False else None)
    # 闸门全开：用极低下界+极高上界；方向/MA20 仍需在 evaluate 内关掉，这里用参数无法关，
    # 故额外构造一个“无ADX闸门”实例：直接继承覆盖——用 adx_lo=0,adx_hi=999 近似（方向/MA20 仍会挡，仅作量级参考）
    b_open = registry.create("底反ADX增强", adx_lo=0.0, adx_hi=999.0)
    n_def = count(b_def, klines, START)
    n_open = count(b_open, klines, START)
    print(f"【底反ADX增强】ADX(21~30)+方向+MA20 ON={n_def}笔  "
          f"ADX闸门近似全开(0~999)={n_open}笔  (ADX闸门过滤掉 {n_open - n_def} 笔)")

    print("\n校准验证完成（仅信号计数；完整盈亏回测另跑）。")


if __name__ == "__main__":
    asyncio.run(main())
