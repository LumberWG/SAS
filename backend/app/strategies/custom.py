"""自定义策略加载：启动时把 SQLite 中的自定义策略注册进注册表。

调用时机：应用启动（main.py lifespan），必须在内置策略注册之后。
内置策略通过 `import app.strategies.builtin` 触发 @registry.register。
"""
from __future__ import annotations

import logging

from app.data.cache import db
from app.strategies import builtin, registry  # noqa: F401 - builtin 触发内置注册

logger = logging.getLogger("sas.strategies.custom")


def load_custom_strategies() -> int:
    """从数据库加载全部自定义策略并注册，返回成功加载的数量。

    单条失败（如基础策略已被移除）只记录警告，不影响其他策略加载。
    """
    loaded = 0
    for item in db.list_custom_strategies():
        try:
            registry.register_dynamic(
                name=item["name"],
                base_name=item["base_strategy"],
                params=item["params"],
                description=item.get("description", ""),
                category=item.get("category") or "自定义",
            )
            loaded += 1
        except (ValueError, KeyError) as exc:
            logger.warning("自定义策略 %s 加载失败: %s", item["name"], exc)
    if loaded:
        logger.info("已加载 %d 个自定义策略", loaded)
    return loaded
