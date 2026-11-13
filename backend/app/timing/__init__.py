"""指数/ETF 择时层（#5 ETF 轮动 + #6 指数 8 指标择时）。

把创业板板块级择时作为「个股策略（量价共振2560=A）」之上的一层闸门：
- index_timing：广发研报 8 指标（VWAP/DPO/ER/TII/PO/MADisplaced/MAAMT/SuperTrend）
  在创业板指(sz399006) 上的多空投票，产出"牛市/熊市"允许交易日集合。
- etf_rotation：30 日动量 + 60 日均线过滤（robertquant/quant-research 的创业板 ETF 轮动逻辑）
  作用在创业板指上，作为创业板 regime 闸门。

两者都以「允许交易日集合」形式返回，与现有 bottom_trend_filter（MA30 等权）取交集即可叠加。
全部离线（直接读 D: 盘 klines/idx_*.parquet），无网络依赖。
"""
from app.timing.loader import load_index
from app.timing.indicators import (
    vwap_position, dpo_position, er_position, tii_position,
    po_position, madisplaced_position, maamt_position, supertrend_position,
    composite_position, ALL_INDICATORS,
)
from app.timing.index_timing import index_timing_gate
from app.timing.etf_rotation import etf_rotation_gate

__all__ = [
    "load_index",
    "vwap_position", "dpo_position", "er_position", "tii_position",
    "po_position", "madisplaced_position", "maamt_position", "supertrend_position",
    "composite_position", "ALL_INDICATORS",
    "index_timing_gate", "etf_rotation_gate",
]
