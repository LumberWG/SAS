"""回测引擎：策略信号回放与绩效评估。

注意：此处只导出类，不导出 engine 单例——
避免单例名与子模块名 engine 相同造成命名遮蔽
（会影响 monkeypatch 等按路径引用的场景）。
"""

from app.backtest.engine import BacktestEngine, BacktestParams

__all__ = ["BacktestEngine", "BacktestParams"]
