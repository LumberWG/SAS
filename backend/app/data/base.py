"""数据源抽象层。

借鉴 tick-stock-panel 的「能力路由」设计：上层（扫描器 / 回测 / API）
只依赖本抽象接口，不关心底层是 akshare 还是其他数据源，
未来可平滑替换为通达信 / 同花顺等源而不影响业务代码。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseDataSource(ABC):
    """A股数据源接口。"""

    #: 数据源名称，用于展示与日志
    name: str = "base"

    @abstractmethod
    def get_stock_pool(self) -> pd.DataFrame:
        """获取 A 股股票池。

        Returns:
            至少包含列: ``code``(6位代码), ``name``, ``market``(sh/sz/bj)
        """
        ...

    @abstractmethod
    def get_realtime_snapshot(self) -> pd.DataFrame:
        """获取全市场实时快照。

        Returns:
            列: ``code``, ``name``, ``price``, ``pct_change``, ``volume``,
            ``amount``, ``turnover_rate``, ``volume_ratio``
        """
        ...

    @abstractmethod
    def get_daily_kline(self, code: str, adjust: str = "qfq") -> pd.DataFrame:
        """获取单只股票日线历史。

        Args:
            code: 6位股票代码，如 ``"600000"``
            adjust: 复权方式，``qfq``=前复权，``hfq``=后复权，``""``=不复权

        Returns:
            按日期升序的 DataFrame，列: ``date``, ``open``, ``close``,
            ``high``, ``low``, ``volume``, ``amount``, ``pct_change``,
            ``turnover_rate``
        """
        ...

    @abstractmethod
    def get_index_snapshot(self) -> pd.DataFrame:
        """获取主要指数实时快照（上证/深证/创业板/科创50 等）。

        Returns:
            列: ``symbol``(如 sh000001), ``name``, ``price``, ``pct_change``,
            ``volume``, ``amount``
        """
        ...

    @abstractmethod
    def get_index_daily(self, symbol: str) -> pd.DataFrame:
        """获取指数日线历史，用于迷你走势图。

        Args:
            symbol: 指数代码，如 ``"sh000001"``（上证指数）

        Returns:
            按日期升序的 DataFrame，列: ``date``, ``open``, ``close``,
            ``high``, ``low``, ``volume``, ``amount``
        """
        ...
