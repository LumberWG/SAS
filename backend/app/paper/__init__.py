"""模拟交易引擎包（Paper Trading）。

只导出类，不导出 engine 单例——避免单例名与子模块名相同
造成命名遮蔽（影响 monkeypatch 等按路径引用的场景）。
"""

from app.paper.engine import PaperEngine

__all__ = ["PaperEngine"]
