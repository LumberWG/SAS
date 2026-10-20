"""策略抽象基类与信号模型。

所有策略（内置与自定义）遵循同一契约：
输入含行情+指标的 DataFrame，输出 Signal 或 None。
选股扫描器与回测引擎都基于此契约复用策略，新增策略只需
继承 Strategy 并实现 evaluate。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Signal:
    """选股命中信号。"""

    code: str                       # 6位股票代码
    name: str                       # 股票名称
    strategy: str                   # 命中的策略名
    price: float                    # 信号触发价（通常为最新收盘价）
    reason: str = ""                # 命中原因（人类可读）
    stop_loss: float | None = None  # 建议止损价
    target_price: float | None = None  # 建议目标价
    date: str = ""                  # 信号日期（K线最后一日）
    extra: dict = field(default_factory=dict)  # 附加指标快照

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "strategy": self.strategy,
            "price": self.price, "reason": self.reason,
            "stop_loss": self.stop_loss, "target_price": self.target_price,
            "date": self.date, "extra": self.extra,
        }


class Strategy(ABC):
    """策略基类。

    子类需定义类属性 name / description，并实现 evaluate。
    evaluate 只基于传入的 df 判断，禁止访问未来数据（避免未来函数）。
    """

    #: 策略唯一名称（注册表 key）
    name: str = ""
    #: 策略说明（前端展示）
    description: str = ""
    #: 策略分类（前端分组展示）
    category: str = "技术形态"
    #: 计算所需最少K线根数（不足则跳过）
    min_bars: int = 60

    @abstractmethod
    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        """评估单只股票，返回命中信号或 None。

        Args:
            df: 含 open/high/low/close/volume 及指标列的 DataFrame，
                已按日期升序，且已通过 add_all_indicators 附加指标。

        Returns:
            命中返回 Signal，未命中返回 None。
        """
        ...

    # ------------------------------------------------------------------
    # 通用工具：默认止损/目标价（子类可覆盖）
    # ------------------------------------------------------------------
    def default_stop_target(
        self, df: pd.DataFrame, lookback: int = 10
    ) -> tuple[float, float]:
        """通用止损/目标价规则。

        止损: 最近 lookback 日最低价（空头防线，跌破说明形态失败）；
        目标: 信号价 × 1.15（15% 涨幅目标，可被策略覆盖）。
        更精细的做法是用 2×ATR 动态止损（df 已含 atr 列）。
        """
        price = float(df["close"].iloc[-1])
        stop = float(df["low"].tail(lookback).min())
        target = round(price * 1.15, 2)
        return stop, target
