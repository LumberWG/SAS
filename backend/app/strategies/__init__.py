"""策略引擎：抽象基类、注册表与内置策略。"""

from app.strategies.base import Signal, Strategy
from app.strategies.registry import StrategyRegistry, registry
from app.strategies import builtin  # noqa: F401 - 导入即注册内置策略
from app.strategies import github_strategies  # noqa: F401 - 接入 GitHub 创业板短线策略

__all__ = ["Signal", "Strategy", "StrategyRegistry", "registry"]
