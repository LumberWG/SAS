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
    # K线预热：后台批量缓存日K到本地Parquet，消除扫描时的实时网络拉取
    kline_warmup_concurrency: int = 10   # 预热并发（低于扫描并发，避免触发数据源限流）
    kline_warmup_top_n: int = 2000       # 开机预热流动性前N只（其余由收盘调度补全）
    # HTTP 请求超时（秒）
    request_timeout: float = 15.0
    # 实时快照内存缓存 TTL（秒）。全量刷新一次约 90 秒，
    # TTL 内所有接口共享缓存；过期后由单线程刷新，其余并发请求等待。
    snapshot_ttl: float = 30.0
    # 国内数据源直连：系统代理（如 Clash）不可用或拒绝国内站点时，
    # 将数据源域名加入 NO_PROXY 绕过代理（LLM 等海外服务不受影响）
    data_direct_only: bool = True
    data_no_proxy_domains: list[str] = [
        "eastmoney.com", "sina.com.cn", "gtimg.cn", "qq.com",
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
    # 收盘/盘后自动扫描范围（all=全市场约 5500 只，冷缓存下需 15 分钟以上；
    # top_amount:N=成交额前 N 只，通常 1~2 分钟出结果）
    auto_scan_scope: str = "all"
    # 等待一轮扫描完成的最长秒数（全市场扫描必须给足，否则盘后分析会拿不到结果）
    scan_wait_timeout: int = 1800

    # ---------- 影子账户（冻结参数的前向验证，见 BACKTEST_LOG #17/#34） ----------
    # 起点为空 = 不启用；设定后每交易日 16:45 自动重跑「起点 ~ 今天」并落快照，
    # 结果在 data/logs/shadow_<tag>.json。只记录、不交易、不碰 paper_* 表。
    # 起点一旦写入即冻结：改它会让曲线断裂、超额不可比（换一个 tag 重开）。
    shadow_start: str = ""
    shadow_tag: str = "live"
    shadow_universe: int = 300
    # 逗号分隔的方案名；空 = 用 scripts/shadow.DEFAULT_PROFILES
    shadow_profiles: str = ""

    # ---------- 智能建议 / 自动盯盘 ----------
    # 收盘自动选股后，将评分 Top N 的命中自动加入自选股
    auto_watch_enabled: bool = True
    auto_watch_top_n: int = 10
    # 五段式②：扫描命中即「股票池」（一选，全部落 signals 表），
    # 这里是从池内按技术面评分取前 N 只加入自选的口径（二选，0 表示关闭）
    scan_auto_watch_limit: int = 10
    # 盘中自选股分析间隔（分钟）。1 分钟一轮：分析本身走本地K线缓存，
    # 单轮耗时约 1~3 秒；「分析 → 成交」由实时撮合（paper_tick）负责，
    # 超过一轮耗时也不会堆积（max_instances=1 自动跳过）。
    intraday_analysis_interval: int = 1
    # 盘后「明日关注」数量
    focus_top_n: int = 10

    # ---------- 模拟交易（验证选股思路） ----------
    # 初始模拟资金
    paper_initial_capital: float = 100000.0
    # 最大持仓只数（仓位按评分加权分配）
    paper_max_positions: int = 10
    # 总仓位上限：持仓市值不超过总资产的比例（1.0=允许满仓，0.8=始终留 20% 现金）
    paper_max_total_exposure: float = 0.80
    # 评分分档：>= 全额分 给足权重；介于半额分与全额分之间权重打 5 折；
    # 低于半额分只记录建议、不下单（避免刚过买入线就占用资金）
    paper_full_score: float = 40.0
    paper_half_score: float = 25.0
    # 是否自动执行系统建议（false 时仅记录信号不成交）
    paper_auto_trade: bool = True
    # 实时撮合开关：True=建议进入队列后按 tick 实时成交；
    # False=退回旧的「分析任务里批量成交」模式
    paper_live_enabled: bool = True
    # 实时撮合间隔（秒）：每 tick 拉一次关注标的实时价并撮合
    paper_tick_interval: int = 5
    # 信号有效期（分钟）：盘中产生的买卖信号超过该时间未成交即丢弃；
    # 非交易时段产生的信号自动顺延到下一个开盘后重新计时
    paper_signal_ttl_minutes: int = 30
    # 选股完成后是否立即对命中 TopN 分析并送入实时交易队列
    # （打通「选股 → 建议 → 成交」链路，无需等下一轮盘中分析）
    paper_scan_autotrade: bool = True
    paper_scan_autotrade_top_n: int = 10
    # 盘后（非交易时段）扫描的命中是否也入队：信号顺延到次日 9:30 开盘后
    # 按实时价成交（仍是实时撮合，不是定时成交价）。设为 false 则只在盘中联动
    paper_scan_autotrade_overnight: bool = True

    # ---------- 满仓后的处理：轮动替换 + 候补队列 ----------
    # 轮动替换：满仓时若出现更高分的新票，卖出最弱持仓换入（T+1 与持有满 1 日保护）
    paper_rotation_enabled: bool = False
    # 触发条件：新票评分 >= min_score，且比「最弱持仓的建仓评分」高出 gap 分
    paper_rotation_min_score: float = 40.0
    paper_rotation_gap: float = 20.0
    # 每日最多轮动次数（一次 = 卖弱 + 买强）
    paper_rotation_max_per_day: int = 1
    # 候补队列：因满仓/资金不足没买成的高分票先存着，有名额时按评分优先买入
    paper_backlog_enabled: bool = True
    paper_backlog_max: int = 30

    # ---------- 五段式③：交易队列容量（topM） ----------
    # 待执行买单最多同时挂 M 条（卖出不受限，释放风险优先）。
    # 为什么需要：自选最多 50 只，一次分析可能把几十条买单塞进队列，
    # 既无法逐只盯，也会在开盘窗口按低分先到先得占掉名额。
    # 0 = 不限制（回到旧行为）。超出的买单不入队，原因记在 /api/paper/live 的 queue_rejected。
    paper_trade_queue_top_m: int = 5

    # ---------- 五段式⑤：切入点（排队不等于成交） ----------
    # 1) 不追高：现价高于「信号价」超过此比例就不买（0.015 = 1.5%）。
    #    盘后产生的信号在次日开盘常跳空，追进去等于用最差价格接盘。
    paper_entry_max_chase: float = 0.015
    # 2) 回踩：要求现价低于信号价此比例才买（0 = 不要求回踩，照价买）。
    #    开盘直接低开够深时买，避免在下跌中继接刀（与「不追高」互补）。
    paper_entry_pullback: float = 0.0
    # 3) 超时：信号超过此分钟数未成交就放弃（0 = 沿用 paper_signal_ttl_minutes）。
    paper_entry_timeout_minutes: int = 0

    # ---------- 执行口径对齐回测（见 BACKTEST_LOG 记录 #19） ----------
    # 为什么需要：回测用「每日成交额 TopN」池，实盘此前是全市场扫描，
    # 于是回测里选不到的票（尤其北交所小票）被真实买入，回测结论无法解释实盘。
    # 只允许买入当日成交额 TopN 内的股票；0=不限制（回到旧的全市场行为）
    paper_pool_top_n: int = 300
    # 排除北交所（920/43/83/87/4/8 开头）：日波动 5~10%，固定 8% 止损≈1×ATR，
    # 会被正常噪声打掉；且 Top300 成交额池里几乎不会出现
    paper_exclude_bj: bool = True
    # 止损距离 = max(固定 8%, N×ATR%)，上限 paper_atr_stop_max。
    # 0=关闭（沿用固定 8%）；高波动票自动放宽，与回测里 e30_a15 的机制一致
    paper_atr_stop_mult: float = 2.0
    paper_atr_stop_max: float = 0.20
    # ---- 模拟盘盈利保护（对齐回测 breakeven/trail） ----
    paper_breakeven_pct: float = 0.10   # 浮盈达此比例后止损上移到成本（保本）
    paper_trail_pct: float = 0.10       # 从持仓期最高价回撤此比例即卖出（移动止损）
    paper_trail_arm: float = 0.0        # 移动止损启用门槛：浮盈达此比例后才跟踪
    # ---- 模拟盘大盘择时买入闸门（弱市暂停自动买入） ----
    paper_regime_ma: int = 60           # 上证指数 MA 周期，<=0 关闭
    paper_regime_weak_pause: bool = True  # 弱市（指数在 MA 下）暂停自动买入
    # ---- 做T引擎（真实 tick：严格 T+1 多批次；默认全部关闭） ----
    paper_t_active: bool = False
    paper_t_size: float = 0.25             # 单次做T比例（相对总仓）
    paper_t_up_sell: float = 0.05          # 开盘涨幅超此先卖锁利
    paper_t_up_buyback: float = 0.03       # 从高点回调超此买回
    # 下跌加仓在单边弱市会放大亏损（回测 E 版验证），默认调到「跌 20% 才加」≈关闭
    paper_t_down_add: float = 0.20         # 开盘跌幅达此加仓（0.20 ≈ 关闭）
    paper_t_down_full: float = 0.20        # 跌幅达此加满仓（0.20 ≈ 关闭）
    paper_t_rebound_sell: float = 0.02     # 反弹至跌幅小于此减仓
    paper_t_limit_sell_half: bool = True   # 涨停弱封板卖 1/2（F 组合开启）
    paper_t_crash_guard: float = 0.02      # 大盘当日跌幅超此暂停做T
    # 买单只允许在开盘后 N 分钟内成交（0=不限）。回测按「次日开盘价」成交，
    # 实盘此前在盘中分析时刻（如 13:01）按实时价一把买入 → 系统性买高
    paper_buy_open_window_minutes: int = 5

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
