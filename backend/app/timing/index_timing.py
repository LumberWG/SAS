"""#6 指数择时层：广发研报 8 指标在创业板指(sz399006) 上的多数投票闸门。

产出"允许交易日集合"（看多=放行）。可选 require_hs300 要求沪深300 同时看多作为确认。
"""
from __future__ import annotations

from app.timing.indicators import composite_position
from app.timing.loader import load_index


def index_timing_gate(
    symbol: str = "sz399006",
    require_hs300: bool = False,
    kline_dir: str | None = None,
) -> set[str]:
    """返回看多（允许交易）的日期字符串集合 {'YYYY-MM-DD', ...}。

    - symbol: 主指数，默认创业板指 sz399006（与本系统 300/301 个股策略最相关）。
    - require_hs300: 同时要求沪深300(sh000300) 看多，作为宽基确认（更保守）。
    """
    df = load_index(symbol, kline_dir)
    comp = composite_position(df)
    allowed = set(df["date"][comp == 1].tolist())

    if require_hs300:
        df2 = load_index("sh000300", kline_dir)
        comp2 = composite_position(df2)
        allowed &= set(df2["date"][comp2 == 1].tolist())

    return allowed
