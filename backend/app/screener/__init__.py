"""选股扫描器：全市场并发扫描 + 任务管理。

注意：此处只导出类，不导出 scanner 单例——
避免单例名与子模块名 scanner 相同造成命名遮蔽。
"""

from app.screener.scanner import Scanner

__all__ = ["Scanner"]
