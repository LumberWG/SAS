"""策略注册表（工厂模式）。

新增策略两步走：
1. 继承 Strategy 实现 evaluate；
2. 在 builtin.py（或自定义模块）中加 @registry.register 装饰器。

扫描器/API 通过注册表统一获取策略实例，不感知具体策略类。

自定义策略（用户在界面上改参数另存）：
通过 register_dynamic 动态注册——基于内置策略类 + 用户参数生成子类，
下游（扫描器/回测/监控）完全无感，按名称即可创建。
"""
from __future__ import annotations

import logging

from app.strategies.base import Strategy

logger = logging.getLogger("sas.strategies")


class StrategyRegistry:
    """策略类注册表。"""

    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}
        #: 自定义策略元信息（name → {base, params, description, category}）
        self._custom_meta: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, cls: type[Strategy]) -> type[Strategy]:
        """注册策略类（装饰器用法，供内置策略使用）。"""
        if not cls.name:
            raise ValueError(f"策略 {cls.__name__} 未定义 name")
        if cls.name in self._strategies:
            raise ValueError(f"策略名重复注册: {cls.name}")
        self._strategies[cls.name] = cls
        return cls

    def register_dynamic(
        self,
        name: str,
        base_name: str,
        params: dict,
        description: str = "",
        category: str = "自定义",
    ) -> type[Strategy]:
        """动态注册自定义策略（基于内置策略 + 用户参数生成子类）。

        Args:
            name: 自定义策略名（不能与已注册策略重名）
            base_name: 基础策略名（提供 evaluate 逻辑）
            params: 用户参数（覆盖默认值）
            description: 自定义策略说明
            category: 分类（默认"自定义"）
        """
        if name in self._strategies:
            raise ValueError(f"策略名已存在: {name}")
        base_cls = self._strategies.get(base_name)
        if base_cls is None:
            raise KeyError(f"基础策略不存在: {base_name}")

        def __init__(self, **_kwargs) -> None:  # noqa: N807
            # 忽略外部传入参数，固定使用用户保存的参数
            base_cls.__init__(self, **params)

        cls = type(name, (base_cls,), {
            "name": name,
            "description": description or f"{base_name} 的自定义参数版本",
            "category": category,
            "__init__": __init__,
            "__doc__": f"自定义策略：基于「{base_name}」，参数 {params}",
        })
        self._strategies[name] = cls
        self._custom_meta[name] = {
            "base": base_name, "params": params,
            "description": cls.description, "category": category,
        }
        logger.info("注册自定义策略: %s (基于 %s)", name, base_name)
        return cls

    def unregister(self, name: str) -> bool:
        """注销策略（仅限自定义策略；内置策略不可删除）。"""
        if name not in self._custom_meta:
            return False
        self._strategies.pop(name, None)
        self._custom_meta.pop(name, None)
        return True

    # ------------------------------------------------------------------
    # 实例化
    # ------------------------------------------------------------------
    def create(self, name: str, **params) -> Strategy:
        """按名称创建策略实例（可传参数覆盖默认值）。"""
        if name not in self._strategies:
            raise KeyError(f"未注册的策略: {name}")
        return self._strategies[name](**params)

    def create_many(self, names: list[str]) -> list[Strategy]:
        return [self.create(n) for n in names]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_class(self, name: str) -> type[Strategy] | None:
        return self._strategies.get(name)

    def is_custom(self, name: str) -> bool:
        return name in self._custom_meta

    def custom_meta(self, name: str) -> dict | None:
        return self._custom_meta.get(name)

    def list(self) -> list[dict]:
        """所有已注册策略的元信息（API 展示用，含参数 schema）。"""
        return [
            {
                "name": cls.name,
                "description": cls.description,
                "category": cls.category,
                "min_bars": cls.min_bars,
                "custom": cls.name in self._custom_meta,
                "parameters": {
                    key: spec.to_dict()
                    for key, spec in cls.parameters.items()
                },
            }
            for cls in self._strategies.values()
        ]

    def param_defaults(self, name: str) -> dict:
        """某策略的参数默认值（前端编辑表单初始值）。"""
        cls = self._strategies.get(name)
        if cls is None:
            return {}
        return {key: spec.default for key, spec in cls.parameters.items()}

    def names(self) -> list[str]:
        return list(self._strategies.keys())


# 全局注册表单例
registry = StrategyRegistry()
