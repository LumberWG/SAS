"""SAS 全局配置模块。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
所有配置项均可通过环境变量覆盖（优先级：环境变量 > .env 文件 > 默认值）。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """系统配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 基础 ----------
    app_name: str = "SAS - A股智能分析系统"
    debug: bool = False

    # ---------- 存储路径 ----------
    # K线 Parquet 缓存目录（按股票代码一个文件，增量追加）
    kline_dir: Path = BASE_DIR / "data" / "klines"
    # SQLite 数据库文件（自选股 / 信号 / 回测记录，个人使用完全够用）
    db_path: Path = BASE_DIR / "data" / "sas.db"

    # ---------- 数据源 / 扫描 ----------
    # 全市场扫描并发上限。akshare 背后的东方财富接口对并发较敏感，
    # 默认 20 是速度与稳定性的平衡点，被限流时可调低。
    scan_concurrency: int = 20
    # 单股数据获取失败重试次数（扫描时坏股票不拖垮整轮）
    fetch_retries: int = 2
    # HTTP 请求超时（秒）
    request_timeout: float = 15.0
    # 实时快照内存缓存 TTL（秒）。全量刷新一次约 90 秒，
    # TTL 内所有接口共享缓存；过期后由单线程刷新，其余并发请求等待。
    snapshot_ttl: float = 30.0
    # 国内数据源直连：系统代理（如 Clash）不可用或拒绝国内站点时，
    # 将数据源域名加入 NO_PROXY 绕过代理（LLM 等海外服务不受影响）
    data_direct_only: bool = True
    data_no_proxy_domains: list[str] = [
        "eastmoney.com", "sina.com.cn", "gtimg.cn",
        "szse.cn", "sse.com.cn", "hexun.com", "10jqka.com.cn",
    ]

    # ---------- LLM（可选模块） ----------
    # 未配置 API Key 时自动降级为纯技术面结论，其余功能完整可用。
    # 支持任意 OpenAI 兼容服务：DeepSeek / 通义 / 智谱 / OpenAI / Gemini 等。
    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout: float = 30.0

    # ---------- 定时任务 / 监控 ----------
    # 调度器总开关（False 时仅提供手动触发能力）
    scheduler_enabled: bool = True
    # cron 时区（A股交易时间按北京时间）
    scheduler_timezone: str = "Asia/Shanghai"
    # 收盘后自动选股使用的策略列表（缺省为全部已注册策略）
    auto_scan_strategies: list[str] = []

    # ---------- 智能建议 / 自动盯盘 ----------
    # 收盘自动选股后，将评分 Top N 的命中自动加入自选股
    auto_watch_enabled: bool = True
    auto_watch_top_n: int = 10
    # 盘中自选股分析间隔（分钟）
    intraday_analysis_interval: int = 5
    # 盘后「明日关注」数量
    focus_top_n: int = 10

    # ---------- 通知管道（可选） ----------
    # Webhook 机器人地址（企业微信/飞书/钉钉均兼容，POST JSON）
    notify_webhook_url: str = ""
    # 邮件通知开关（预留，后续接 SMTP）
    notify_email: bool = False

    # ---------- 前端 ----------
    # 允许跨域访问的前端源（Vite 开发服务器默认端口）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    # ---------- 免责声明 ----------
    # 接口响应中统一注入该字段，前端在页面上显著展示
    disclaimer: str = (
        "本系统仅供学习研究使用，所有数据与建议不构成任何投资建议。股市有风险，投资需谨慎。"
    )


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（带缓存，进程内共享）。"""
    settings = Settings()
    # 确保数据目录存在，避免首次读写失败
    settings.kline_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
