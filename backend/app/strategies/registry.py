"""策略注册表（工厂模式）。

新增策略两步走：
1. 继承 Strategy 实现 evaluate；
2. 在 builtin.py（或自定义模块）中加 @registry.register 装饰器。

扫描器/API 通过注册表统一获取策略实例，不感知具体策略类。
"""
from __future__ import annotations

from app.strategies.base import Strategy


class StrategyRegistry:
    """策略类注册表。"""

    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}

    def register(self, cls: type[Strategy]) -> type[Strategy]:
        """注册策略类（装饰器用法）。"""
        if not cls.name:
            raise ValueError(f"策略 {cls.__name__} 未定义 name")
        if cls.name in self._strategies:
            raise ValueError(f"策略名重复注册: {cls.name}")
        self._strategies[cls.name] = cls
        return cls

    def create(self, name: str) -> Strategy:
        """按名称创建策略实例。"""
        if name not in self._strategies:
            raise KeyError(f"未注册的策略: {name}")
        return self._strategies[name]()

    def create_many(self, names: list[str]) -> list[Strategy]:
        return [self.create(n) for n in names]

    def list(self) -> list[dict]:
        """所有已注册策略的元信息（API 展示用）。"""
        return [
            {
                "name": cls.name,
                "description": cls.description,
                "category": cls.category,
                "min_bars": cls.min_bars,
            }
            for cls in self._strategies.values()
        ]

    def names(self) -> list[str]:
        return list(self._strategies.keys())


# 全局注册表单例
registry = StrategyRegistry()
