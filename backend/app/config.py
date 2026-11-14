"""SAS 全局配置模块。

使用 pydantic-settings 从环境变量 / .env 文件加载配置，
所有配置项均可通过环境变量覆盖（优先级：环境变量 > .env 文件 > 默认值）。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 数据根目录：全部数据统一放 D 盘备份区，与代码分离，避免误删代码目录时连带数据。
#   D:\Backup\SAS\fsdb   26GB  free-stockdb 原始库（数据源，由「数据更新.exe」维护）
#   D:\Backup\SAS\data   导出数据 + 程序运行数据（K线 parquet / 分钟线 / SQLite / logs）
# 可用环境变量 SAS_DATA_DIR 覆盖（例如临时切回 E:\Study\SAS\data 做对照）。
DATA_DIR = Path(os.getenv("SAS_DATA_DIR", r"D:\Backup\SAS\data"))


class Settings(BaseSettings):
    """系统配置项。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- 基础 ----------
    app_name: str = "SAS - A股智能分析系统"
    debug: bool = False

    # ---------- 评分阈值（14 因子总分，±100） ----------
    # 此前写死在 factors.BUY_THRESHOLD / SELL_THRESHOLD：回测面板有 buy_threshold，
    # 实盘却只能改代码，同一个口径两套入口。挪进 Settings 后页面可改、回测可落盘。
    factor_buy_threshold: float = 15.0    # 买入阈值：总分 ≥ 此值判 buy（须大于卖出阈值）
    factor_sell_threshold: float = -15.0  # 卖出阈值：总分 ≤ 此值判 sell

    # ---------- 交易费率（回测与实盘共用，改一处两边同时生效） ----------
    # 原先 engine 与 pipeline 各写一份费率常量，佣金谈下来后只改了一边，
    # 回测 PnL 与实盘对不上。统一到这里。
    trade_fee_commission_rate: float = 0.00025  # 佣金费率（双向，万2.5）
    trade_fee_commission_min: float = 5.0       # 单笔最低佣金（元，免五则填 0）
    trade_fee_stamp_rate: float = 0.001         # 印花税（仅卖出，千1）
    trade_fee_transfer_rate: float = 1e-05      # 过户费（双向，万0.1）

    # ---------- 存储路径 ----------
    # K线 Parquet 缓存目录（按股票代码一个文件，增量追加）
    kline_dir: Path = DATA_DIR / "klines"
    # SQLite 数据库文件（自选股 / 信号 / 回测记录，个人使用完全够用）
    db_path: Path = DATA_DIR / "sas.db"

    # ---------- 数据源 / 扫描 ----------
    # 全市场扫描并发上限。akshare 背后的东方财富接口对并发较敏感，
    # 默认 20 是速度与稳定性的平衡点，被限流时可调低。
    scan_concurrency: int = 20
    # 单股数据获取失败重试次数（扫描时坏股票不拖垮整轮）
    fetch_retries: int = 2
    # K线预热：后台批量缓存日K到本地Parquet，消除扫描时的实时网络拉取
    kline_warmup_concurrency: int = 10   # 预热并发（低于扫描并发，避免触发数据源限流）
    # 收盘预热（16:00）覆盖的股票数：0 = 全市场。默认全市场——只热前 N 只时，
    # 剩下的股票一旦某天没刷上就永远停在旧日期（增量预热按各自最后日期续拉，
    # 不会主动发现落后），回测/影子账户会静默用旧 bar。开机预热不走这个值，
    # 只补真正落后的股票（见 scheduler._warmup_stale）。
    kline_warmup_top_n: int = 0
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
    # top_amount:N=目标板块内成交额前 N 只，通常 1~2 分钟出结果，贴合交易板块；
    # top_amount_all:N=全市场成交额前 N 只，用于显式全市场扫描）
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

    # ---------- 摆动影子（相对强弱极值共振，只观测不交易；见 BACKTEST_LOG #120） ----------
    # 与影子账户互不相干：那个是「冻结参数逐日重跑整段回测」，这个是「逐日重放 RS 摆动信号」。
    # 产物 data/logs/swing_shadow_<tag>.json，不读写 paper_* 表、不进评分、不触发下单。
    swing_threshold_pct: float = 0.05  # 摆动尺度：相对幅度至少达到此值才算一次转折
    swing_atr_mult: float = 1.0        # 摆动尺度：ATR 倍数折算的相对幅度，与上一项取较大值
    swing_confirm_bars: int = 3        # 右侧确认 K 线数：越大越可靠，也越滞后
    swing_min_gap: int = 5             # 相邻摆动点最小间隔，过滤密集的噪声级极值
    swing_anchor_bars: int = 3         # 基准确认低点后开放的共振时间窗长度
    swing_z_window: int = 60           # 相对强弱偏离度的滚动窗口
    swing_band: float = 1.0            # 跟随带：|Z| 不超过此值算与基准共振
    swing_extreme_z: float = 2.0       # 极端背离：|Z| 超过此值进观察池，剔除不交易
    swing_min_corr: float = 0.2        # 与基准滚动收益相关度下限，低于此值视为不同步
    swing_max_symbols: int = 300       # 影子扫描最多覆盖多少只票，与线上 Top300 口径对齐

    # ---------- 事前预估（只观测：输出转折概率与条件分布，不输出点位/买卖日期） ----------
    # 标签窗口未走完的样本绝不入库；个股历史只做带上限的贝叶斯收缩，见 BACKTEST_LOG #121。
    swing_est_horizon: int = 3         # 预估窗口：未来多少根内出现摆动低点算一次转折
    swing_est_prior_strength: float = 40.0  # 收缩先验强度（等效虚拟样本数），越大越信任全局
    swing_est_symbol_cap: int = 40     # 单只票有效样本上限，校正日线样本的自相关
    swing_est_entry_lift: float = 2.0  # 发观测信号的概率提升倍数（相对当时已入库样本的基础率）
    swing_est_cost_pct: float = 0.3    # 期望净收益里扣掉的双边成本（百分点）
    swing_est_min_exp_net: float = 0.0  # 期望净收益下限，低于此不发观测信号
    # 逗号分隔的因果特征（z 必选）：z/streak/since/vol/regime/bench_mom/lead/bench_z
    swing_est_features: str = "z,streak,since,vol,regime"  # 预估分箱特征集，#122 网格可热更切换
    swing_est_calibration: str = "none"  # 概率校准：platt（在线 2 参数重标定）/ none（原始分箱概率）
    swing_est_calib_min_samples: int = 200  # 少于这个已入库样本数就不启用校准，直接用原始概率
    swing_est_calib_refit_every: int = 20   # 每多少根 bar 重拟合一次校准器（只用当时可见样本）
    swing_est_calib_l2: float = 1.0         # 校准器向恒等映射 (a=1,b=0) 收缩的强度，防止少样本发散

    # ---------- 出场规则实验（只研究：比较固定持有与跟踪止盈，不接交易） ----------
    # BACKTEST_LOG #124：确认后剩余波段只有 3.5%~6.0%，概率层面补不上，只能从出场找。
    # 默认仍是 fixed（3 根），与 #120~#123 的既有口径一致；改这几项只影响报告对照。
    swing_exit_mode: str = "fixed"     # fixed / atr_trail / swing_trail
    swing_exit_hold_bars: int = 3      # fixed 的持有根数；跟踪模式下的最长持有上限
    swing_exit_atr_k: float = 1.8      # 跟踪回撤宽度 = k × 入场时 ATR 占价格比例
    swing_exit_floor_pct: float = 2.5  # 回撤宽度下限，ATR 很小时避免被噪声扫掉
    swing_exit_take_profit_pct: float = 0.0  # >0 时触及该涨幅直接止盈，0 = 不封顶

    # ---------- 并行模拟账本（同一份信号、不同处置参数各自记账） ----------
    # 与影子账户的区别：影子账户是「日频、重跑整段、能复现选股侧差异」，
    # 并行账本是「tick 级、共享信号、只能复现处置侧差异」，两者互补。
    # 方案名取自 app/backtest/profiles.py 的 PROFILES，只接受能换算到 paper_*
    # 的参数（止损/止盈/仓位/持仓数/弱市档）；含选股侧参数（ML 打分、评分反转、
    # 策略子集）的方案会被跳过，原因在 /api/paper/sim 的 skipped 字段里。
    paper_sim_enabled: bool = False
    paper_sim_profiles: str = ""
    # 每个账本的初始资金；0 = 跟随 paper_initial_capital
    paper_sim_capital: float = 0.0

    # ---------- 智能建议 / 自动盯盘 ----------
    # 收盘自动选股后，将评分 Top N 的命中自动加入自选股
    auto_watch_enabled: bool = True
    auto_watch_top_n: int = 10
    # 五段式②：扫描命中即「股票池」（一选，全部落 signals 表），
    # 这里是从池内按技术面评分取前 N 只加入自选的口径（二选，0 表示关闭）
    scan_auto_watch_limit: int = 10
    # 程序自选容量上限：每轮把「本轮命中 Top limit」与「现有 auto」统一按技术面评分排序，
    # 保留高分前 watch_auto_max 只，其余 auto 淘汰（只删 auto，不动 manual）。
    # 默认 scan_auto_watch_limit×3 = 30；设为 0 则回退 ×3。
    watch_auto_max: int = 30
    # 盘中自选股分析间隔（分钟）。1 分钟一轮：分析本身走本地K线缓存，
    # 单轮耗时约 1~3 秒；「分析 → 成交」由实时撮合（paper_tick）负责，
    # 超过一轮耗时也不会堆积（max_instances=1 自动跳过）。
    intraday_analysis_interval: int = 1
    # 盘后「明日关注」数量
    focus_top_n: int = 10

    # ---------- 模拟交易（验证选股思路） ----------
    # 初始模拟资金
    paper_initial_capital: float = 100000.0
    # 最大持仓**只数上限**（不是门槛）：可以少于这个数，不能超过
    paper_max_positions: int = 10
    # **收盘目标**总仓位：收盘前尽量压到这个比例（留 20% 现金给次日做T）。
    # 盘中不设硬上限——盘中允许满仓（受现金与单票上限约束），由 eod_trim 在收盘前
    # paper_eod_trim_minutes 分钟内按「最弱优先」卖出超出部分，卖不掉不强求。
    paper_max_total_exposure: float = 0.80
    # 收盘减仓：开启后收盘前 N 分钟把超出收盘目标的仓位卖掉（今仓 T+1 卖不掉就跳过）
    paper_eod_trim_enabled: bool = True
    paper_eod_trim_minutes: int = 10
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
    # 轮动替换：满仓时若出现更高分的新票，卖出最弱持仓换入。
    # **默认开启**（2026-11-02 使用者决策：这是模拟盘，先把机制跑起来；见 BACKTEST_LOG #92）。
    # 证据边界：回测（#30）显示 Top300 成交额池下几乎不触发，所以开着的**预期影响很小**——
    # 它不是被证明有效的 alpha，而是「模拟盘把规则跑全、用真实成交数据观察」的选择。
    # 换池子规模（Top300 → 200/500）时必须重跑 #30；若实盘出现连续无谓换手/摩擦侵蚀，把
    # 它改回 False 即可（一处配置，不需要改代码）。
    # 换入票必须**真能买进**（交易池闸门 + 切入点闸门 + 卖出所得够一手），否则不卖，
    # 避免「只卖不买」把仓位凭空降下来；手动建仓与当日买入的持仓不参与选弱（T+1）。
    paper_rotation_enabled: bool = True
    # 触发条件：新票评分 >= min_score（且 >= half_score），且比「最弱持仓的建仓评分」高出 gap 分
    paper_rotation_min_score: float = 40.0
    paper_rotation_gap: float = 20.0
    # 每日最多轮动**只数**（一次 = 卖一只弱票换一只强票）；0 = 当日不轮动。
    # 默认 10 = 与持仓上限同阶，即「只要明显更好就可以一直换」，包括把持仓全换掉。
    paper_rotation_max_per_day: int = 10
    # 候补队列：因满仓/资金不足没买成的高分票先存着，有名额时按评分优先买入
    paper_backlog_enabled: bool = True
    paper_backlog_max: int = 30

    # ---------- 五段式③：交易队列容量（topM） ----------
    # 待执行买单最多同时挂 M 条（卖出不受限，释放风险优先）。
    # 为什么需要：自选最多 50 只，一次分析可能把几十条买单塞进队列，
    # 既无法逐只盯，也会在开盘窗口按低分先到先得占掉名额。
    # 0 = 不限制（回到旧行为）。超出的买单不入队，原因记在 /api/paper/live 的 queue_rejected。
    paper_trade_queue_top_m: int = 5

    # ---------- P2：盘内错峰（每 tick 买入节奏） ----------
    # 单 tick 最多成交几只买入（其余留后续 tick，自然错峰）。解决「积压信号在 5 分钟
    # 买入窗口内被一秒全打出去」的单点择时风险。0 = 不限制（回到旧行为）。
    # 依赖 live 的 retry 保留机制：被节流的买入返回「买入节奏」skip，下一 tick 重试。
    # 注：这是「盘内错峰」，积压信号仍在买入窗口内分批；若要「跨时段分散」需进一步拆窗口。
    paper_buys_per_tick: int = 2  # 盘内错峰：单 tick 最多成交几只买入（默认 2，避免一秒买全 10 只）

    # ---------- 五段式⑤：切入点（排队不等于成交） ----------
    # 1) 不追高：现价高于「信号价」超过此比例就不买（0 = 不限，照价买）。
    #    2026-11-01 由 0.015 改为 0（关闭），依据 BACKTEST_LOG 记录 #89：6 段配对
    #    「中位 −4.54pp / 正占比 2/6 / 最差段 −22.46pp」三条线全不过；且成交笔数
    #    没有减少（95→96），是**买差**而非少买 —— 涨市里强势票高开 1.5% 被拦，
    #    资金转去买相对弱的票，次日集中回吐。参数保留，便于复跑与实盘对照。
    paper_entry_max_chase: float = 0.0
    # 2) 回踩：要求现价低于信号价此比例才买（0 = 不要求回踩，照价买）。
    #    开盘直接低开够深时买，避免在下跌中继接刀（与「不追高」互补）。
    paper_entry_pullback: float = 0.0
    # 3) 超时：信号超过此分钟数未成交就放弃（0 = 沿用 paper_signal_ttl_minutes）。
    paper_entry_timeout_minutes: int = 0
    # 4) 涨停禁买（#181）：模拟盘此前会在涨停价虚假成交——实盘封板根本买不到。
    #    按板块真实涨跌幅判定（app.board_rules，含 0.5pp 容差）：主板±10%、
    #    创业板/科创板±20%、北交所±30%、ST±5%。昨收取进程内内存快照的
    #    prev_close（毫秒级、不联网）；取不到昨收时失败开放、不误杀。
    #    只拦「新开仓/轮动换入」的买入，不拦做 T 买回、卖出、止盈止损。
    paper_block_limit_up_buy: bool = True

    # ---------- 执行口径对齐回测（见 BACKTEST_LOG 记录 #19） ----------
    # 为什么需要：回测用「每日成交额 TopN」池，实盘此前是全市场扫描，
    # 于是回测里选不到的票（尤其北交所小票）被真实买入，回测结论无法解释实盘。
    # 只允许买入当日成交额 TopN 内的股票；0=不限制（回到旧的全市场行为）
    paper_pool_top_n: int = 300
    # 【选股 + 交易 统一板块】单一板块白名单，同时驱动「选股扫描范围」与「交易买入池」。
    # 勾选哪些板块，就只从这些板块选股、也只买这些板块——两者不再各管各的，
    # 因此不再出现「选股板块含主板但交易未勾选 → 候选被买入侧拒绝」的板块不一致告警。
    # 默认仅创业板（用户买卖集中于创业板，2026-11-10 起改默认）；在「系统设置」以一组
    # 板块芯片统一勾选即可。留空=不限制（退回主板+创业板）。引擎买入池、日更做T清单、
    # 选股扫描范围、非板块持仓清仓都按此过滤。
    # #181 起同时接受中文名（主板/创业板/科创板/北交所）与英文键
    # （sh_main/sz_main/cyb/star/bse）；前缀表统一在 app/board_rules.py。
    # 科创板(200股起/1股递增)、北交所(100股起/1股递增)的申报规则与 ±20%/±30% 涨跌停
    # 已由 board_rules 统一支持。
    # ⚠️ 注意：选股板块已统一跟随本项（见 screener/api 的 board 取数），勿再单独改 bottom_boards。
    twin_t_boards: str = "创业板"
    # 板块交易规则回退开关：True=回到旧的全市场 ±9.5% 涨跌停 / 100 股整手口径，
    # 用于复现 #178 之前的历史回测基线（+8.49% 等）。默认 False = 按各板块真实规则
    # （主板±10%、创业板/科创板±20%、北交所±30%、ST±5%；科创200股起、北交所1股递增）。
    # ⚠️ 默认 False 会改变创业板既有回测/模拟盘行为：旧口径把 +9.5%~+20% 的正常上涨
    # 误判为「涨停买不到」。如需严格对比历史结果，临时置 True。
    board_rules_legacy_limit: bool = False
    # 自动清掉"非交易板块"持仓：开启后，引擎在每日开盘前与收盘结算时，把不属于
    # `twin_t_boards` 白名单板块的持仓全部市价卖出（只清板块不符的，ST/市值等维度不动）。
    # 用途：切换只做创业板后，把历史遗留的主板/科创板/北交所持仓自动出清。默认关闭——
    # 开启即开始清仓，请确认后再开。也可调 POST /api/paper/liquidate-offboard 立即清一次。
    paper_liquidate_offboard: bool = False
    # 大盘股过滤（「③ 三选 · 交易队列」页有开关）：总市值 > paper_large_cap_yi（亿元）
    # 的票默认不进买入队列——银行/石油/白酒这类巨无霸短线弹性差，也吃不动评分仓位的弹性。
    # 页面上的开关与数值写进 app_state 覆盖这里的默认值（不用改 .env，重启后仍保留）。
    paper_exclude_large_cap: bool = True
    paper_large_cap_yi: float = 100.0
    # ---- 固定止损/止盈/单票上限（与回测 stop_loss / take_profit / single_position_ratio 同口径） ----
    # 此前这三项硬编码在 paper/engine.py，回测中心「将规则落盘保存到数据库」无法写入，
    # 于是「回测调好的规则」和「实盘在用的规则」永远差一截。改成 Settings 字段后可落盘。
    paper_stop_loss: float = 0.08      # 固定止损：成本价跌此比例卖出（回测 stop_loss）
    paper_take_profit: float = 0.20    # 固定止盈：成本价涨此比例卖出（回测 take_profit）
    paper_single_position_max: float = 0.20  # 单票市值占总资产上限（回测 single_position_ratio）
    # 止损距离 = max(固定止损, N×ATR%)，上限 paper_atr_stop_max。
    # 0=关闭（沿用固定止损）；高波动票自动放宽，与回测里 e30_a15 的机制一致
    paper_atr_stop_mult: float = 2.0
    paper_atr_stop_max: float = 0.20
    # ---- 模拟盘盈利保护（对齐回测 breakeven/trail） ----
    paper_breakeven_pct: float = 0.10   # 浮盈达此比例后止损上移到成本（保本）
    paper_trail_pct: float = 0.10       # 从持仓期最高价回撤此比例即卖出（移动止损）
    paper_trail_arm: float = 0.0        # 移动止损启用门槛：浮盈达此比例后才跟踪
    # 短线时间止损（#191）：持仓满 N 个自然日（首个建仓批次的 buy_date 起算，含当日）
    # 仍未离场则强制卖出，避免「无限期持有」——线上此前只有价格触发才会卖。
    # 0=关闭（沿用旧行为）；≤5 日短线交易者建议设 5。与回测 engine.py 自然日口径一致。
    paper_max_hold_days: int = 0
    # 板块趋势闸门（#191）：开启后，候选所属板块指数处于下降通道（downtrend）时拒绝买入，
    # 即 Layer1 板块感知趋势真正接入入场闸。默认关闭=零风险（保持既有行为）；
    # 多板块交易时可开启，让「创业板指/上证主板指…在 MA 下」的票不买。
    paper_board_trend_gate: bool = False
    # Layer2 统一宏观风险四档 cap 接入 effective_exposure（#191 / 原 #127）：开启后，
    # weak/shock/crisis 档把总仓位上限压到 unified_risk_cap_*（只降不升）。默认关闭=零风险，
    # 保持「此前只有 crisis 二值硬拦、weak/shock 不压仓」的既有行为；开启即把展示层已有的
    # 四档 cap 真正落到下单路径（与 #126 入场护栏、#191 板块闸门同属「择时降仓」开关组）。
    paper_unified_risk_cap_enabled: bool = False
    # 机制层止损收紧：shock/crisis 态新建仓用更紧的止损距离（距离 × 此系数），
    # 只限新建仓（已有持仓止损在建仓时定死，不回溯改）。0=关闭（原行为，保守默认）。
    # 风险态下宁可早点砍、少亏；指数数据缺失 → regime=normal → 不收紧，零风险。
    paper_regime_stop_tighten: float = 0.0
    paper_regime_stop_min: float = 0.03   # 收紧后止损距离下限（防止过度收紧成噪声止损）
    # ---- 模拟盘大盘择时买入闸门（弱市暂停自动买入） ----
    paper_regime_ma: int = 60           # 弱市判定均线周期（作用于 paper_regime_index 指定的宽基指数），<=0 关闭
    paper_regime_index: str = "sh000985"  # 弱市判定所用宽基指数：sh000985=中证全指（覆盖全A，比上证更代表系统性风险）；
                                         #   可改 sh000001=上证指数 / sz399006=创业板指。与 bottom_trend_index 默认全市场口径统一。
    paper_regime_weak_pause: bool = True  # 弱市（指数在 MA 下）暂停自动买入
    #: 弱市**保留仓位比例**（0=全暂停，即上面的开关注释行为；>0 时弱市不暂停买入，
    #: 而是把「总仓位上限」压到该比例继续参与）。回测记录 #59/#60：0.3~0.5 在
    #: 急跌月小亏换反弹时的收益，代价是涨市初期踏空。默认 0 保持既有行为不变。
    paper_weak_exposure: float = 0.0
    # ---- 入场护栏硬屏蔽（#126 续 / #191：把影子层提示升级为可开关的硬拦截） ----
    # 开启后：上证在 MA20 下方（declining）时，硬屏蔽所有**新开仓买入建议**——
    # 既不在「智能建议 / 买入」列表展示，也不送进自动交易引擎成交；卖出与做T买回不受影响。
    # 与上方 paper_regime_weak_pause（MA60 弱市暂停）并存且独立：本开关更紧（MA20）。
    # 自 #191 起**默认开启**——诊断表明用户实盘买点主要败在「下降市开新仓」而非个股尖，
    # 默认拦截可在不改动选股/评分/下单逻辑的前提下直接修复该痛点。可在设置页随时关闭。
    # 诊断依据：4 年回测 41657 次动量信号，上证 >MA20 时 20 日超额 +0.74%、<MA20 时 −1.22%。
    entry_guard_block_declining_market: bool = True
    # ---- 入场护栏提示阈值（仅「慎开新仓 / 勿追当根尖峰」提示，不拦截下单） ----
    # 此前硬编码在 app/analysis/entry_guard.py 的 GAP_THRESHOLD / SPIKE_THRESHOLD。
    entry_guard_gap_threshold: float = 0.03     # 高开阈值：开盘价较昨收涨幅超此值即提示
    entry_guard_spike_threshold: float = 0.05   # 盘中冲高阈值：最高价较开盘涨幅超此值即提示
    # ---- 动态机制降暴露（机制检测层；默认关闭，零风险） ----
    # 开启后由「波动率/回撤/趋势」把市场分成 calm/weak/shock/crisis 四态，
    # 动态压低总仓位上限（只降不升），替代/增强上面的静态弱市档位。与回测
    # pipeline 的 regime_tiers 同思路，但作用在实时引擎。改动见 BACKTEST_LOG #84。
    paper_dynamic_exposure: bool = False
    paper_regime_cap_calm: float = 0.0    # 平稳仓上限：0 = 不降仓（用总仓位上限）
    paper_regime_cap_weak: float = 0.30   # 弱势仓上限
    paper_regime_cap_shock: float = 0.40  # 震荡仓上限
    paper_regime_cap_crisis: float = 0.15  # 危机仓上限（同时触发全暂停买入）
    # ---- 动态机制检测阈值（与上面的档位配套；此前硬编码在 engine 的 detect_regime 调用处） ----
    # 这些阈值决定「何时进入 shock / crisis 态」，和「进入后压到哪一档」分开：
    # 全部改为设置项后，机制灵敏度与档位都能在「系统设置 → 做T 与择时」里调，无需改代码。
    paper_regime_vol_win: int = 20        # 近期波动率窗口（交易日）
    paper_regime_vol_base: int = 60       # 基线波动率窗口（交易日）
    paper_regime_dd_win: int = 120        # 回撤计算窗口（交易日）
    paper_regime_crisis_vol: float = 2.0  # 波动率比 ≥ 此值 → crisis
    paper_regime_crisis_dd: float = 0.12  # 最大回撤 ≥ 此值 → crisis
    paper_regime_shock_vol: float = 1.4   # 波动率比 ≥ 此值 → shock
    paper_regime_shock_dd: float = 0.06   # 最大回撤 ≥ 此值 → shock
    # ---- 政策托底强度检测（把"看不见的底"变可见；默认关闭，零风险） ----
    # 开启后，在 weak/shock 态叠加托底强度：强度高→升级为"托底型阴跌"（缩仓收割反转），
    # 强度极低且高波动→升级为危机防御（自由落体型）。与动态机制层配合，实现
    # "两种下行分治"。直接信号（ETF放量/期指升贴水/官媒情绪）当前部署缺失时，
    # 自动降级为指数底部特征代理，绝不假装拥有没有的数据。改动见 BACKTEST_LOG #85。
    paper_policy_floor_enabled: bool = False  # 托底强度检测：需与 dynamic_exposure 同开，单开不生效
    paper_policy_floor_min_score: float = 0.50   # ≥此强度且处于 weak/shock → 托底型阴跌（可收割）
    paper_policy_floor_low_escalate: float = 0.20  # ≤此强度且处于 shock → 升级危机防御（自由落体）
    paper_policy_floor_harvest_cap: float = 0.30  # 托底型阴跌的总仓位上限（收割但不裸抄）
    # ---- 硬性回撤熔断（最后一道防线，凌驾于任何信号/机制层） ----
    # 组合回撤触及 trigger 即锁仓/压仓到 target，回落到 ≤ release 才解除（滞回）。
    # 峰值与触发状态落库，跨重启保留——重启不会因"忘了刚才跌"就立刻满仓接飞刀。
    # 改动见 BACKTEST_LOG #85。
    paper_circuit_breaker_enabled: bool = False
    paper_circuit_breaker_dd: float = 0.08        # 触发阈值：回撤 ≥8% 锁仓
    paper_circuit_breaker_release: float = 0.04   # 解除阈值：回撤回落到 ≤4% 才恢复
    paper_circuit_breaker_target: float = 0.0     # 触发后目标总仓位（0=清到空仓/只留现金）
    # ---- 做T引擎（真实 tick：严格 T+1 多批次） ----
    # **默认开启**（2026-11-02 使用者决策：模拟盘先把机制跑起来；见 BACKTEST_LOG #92）。
    # 证据边界：#65/#66 已证明做T 对滑点极度敏感（每 0.1% ≈ −0.93pp，≥0.10% 收益即归零），
    # 开着的代价是成交笔数翻倍、与基础策略混在一起难以归因；关掉只改这一处配置。
    # 下面的数值参数默认偏保守（t_down_add/down_full=0.20 ≈ 关闭下跌加仓）。
    paper_t_active: bool = True
    paper_t_size: float = 0.25             # 单次做T比例（相对总仓）

    # ---------- 线上 ML 打分（default_ml 实时版） ----------
    # True 时 recommender 用 LightGBM 截面分位(0~100) 替换 14 因子分做排序与仓位定价；
    # 买入触发仍保留 14 因子信号作安全网（ML 只能降权/跳过，不能凭空制造买入）。
    # 2026-11-01 起线上 True（`.env` + app_state 覆盖层双写）。
    # 此前关闭依据的两条都站不住：① #79 的 `pred_pairs=0` 是**12 个交易日的短区间回测**
    # ——截面太少估不出 rank-IC，属 #76 设计内的纪律性回退，不是实盘路径；② 实测 152 只
    # 自选股 20.7s 全量出分，且按 as_of 缓存、一天只训练一次，见 BACKTEST_LOG #88。
    # 直接证据是 #82：`ml_lgb` 18 段配对中位 +4.55pp（>3.5pp 线）、最差段 −11.44%→−5.07%。
    # 设置页改这一项**立即生效**（就地改 Settings 单例），不需要重启。
    live_ml_scoring: bool = False
    # ML 行业中性化：与回测 industry_neutral 双向绑定，避免两端口径漂移。
    # 默认 True（沿用此前线上固定 True 的行为）；关闭则按全市场截面排序。
    live_ml_industry_neutral: bool = True
    ml_regime_aware: bool = False        # 趋势市用 ML、震荡市回退 14 因子
    ml_regime_vol_cap: float = 0.25     # 趋势市判定：近20日年化波动率上限（超过=震荡市）
    ml_regime_ma_gap: float = 0.0       # 趋势市判定：MA20 须高于 MA60 的最小比例（0=MA20>MA60）
    paper_t_up_sell: float = 0.05          # 开盘涨幅超此先卖锁利
    paper_t_up_buyback: float = 0.03       # 从高点回调超此买回
    # 下跌加仓在单边弱市会放大亏损（回测 E 版验证），默认调到「跌 20% 才加」≈关闭
    paper_t_down_add: float = 0.20         # 开盘跌幅达此加仓（0.20 ≈ 关闭）
    paper_t_down_full: float = 0.20        # 跌幅达此加满仓（0.20 ≈ 关闭）
    paper_t_rebound_sell: float = 0.02     # 反弹至跌幅小于此减仓
    paper_t_limit_sell_half: bool = True   # 涨停弱封板卖 1/2（F 组合开启）
    paper_t_crash_guard: float = 0.02      # 大盘当日跌幅超此暂停做T
    # 买单成交时段：0=全时段可成交（仅受开盘冷静期约束）；>0=只在开盘后 N 分钟内可成交。
    # 旧设计把成交收敛到开盘那一刻以对齐回测「次日开盘价」口径，但实盘会系统性买高、
    # 且造成「积压到开盘窗口一次性爆发」的批量成交。按用户 2026-11-04 要求改为全时段可成交，
    # 配合 paper_open_cooldown_minutes 只拦开盘前 N 分钟（见本轮"9:35-上午收盘/13:05-下午收盘随时可成交"）。
    paper_buy_open_window_minutes: int = 0
    # 开盘后 N 分钟内**禁止自动买入**（含轮动换入、做T买回/加仓），让隔夜外围利空导致的
    # 开盘跳空先消化；0=不延迟（默认，维持现状）。只拦买入、不拦卖出。与「跳空护栏」
    # （paper_t_crash_guard 扩展到买入端）共同构成开盘保护。见 sas_formulas §开盘保护。
    paper_buy_open_delay_minutes: int = 0
    # 开盘冷静期：开盘后 N 分钟内**无条件禁止自动买入**（含轮动换入、做T买回/加仓），
    # 让开盘瞬间波动/跳空先过去；0=不限制。仅拦买入、不拦卖出。对 09:30 与 13:00 两个时段分别生效。
    # 配合 paper_buy_open_window_minutes=0 → 全时段可成交、只拦开盘前 N 分钟（本轮用户要求）。
    # 区别于 paper_buy_open_delay_minutes（仅大盘向下跳空时才延迟）。
    paper_open_cooldown_minutes: int = 5

    # ---------- 通知管道（可选） ----------
    # Webhook 机器人地址（企业微信/飞书/钉钉均兼容，POST JSON）
    notify_webhook_url: str = ""
    # 邮件通知开关（预留，后续接 SMTP）
    notify_email: bool = False

    # ---------- 创业板「从底部起来」选股口径（面板 / 策略 / 回测 同口径） ----------
    # 候选 = 低位启动 + 相对20日高点跌够 + 成交活跃 + 非僵尸 +（可选）创业板上升通道。
    # 详见 app/screener/bottom_reversal.py 的 compute_all/scan_candidates 与
    # app/strategies/builtin.py 的 BottomReversal.evaluate（两者逐字同口径）。
    # 改动：2026-11-11 用户要求收紧——原「近20日涨幅15%~60%」过松会乱选，
    # 改为「相对20日高点下跌≥15%（低位）+ 换手率≥5% + 20日均成交额≥下限（成交不能太低）」。
    bottom_start_pos_max: float = 0.40        # 起点位置上限：20天前收盘价在近120日区间的相对位置 <= 此值（低位启动）
    bottom_dd20_min: float = 0.15             # 相对20日收盘高点最小回撤：>= 此值才算"跌够、处在低位"（如 0.15=跌15%）
    bottom_turnover_min: float = 5.0          # 当日换手率下限（百分制，与数据源 turnover_rate 同单位，5.0=5%）：成交活跃，过滤冷门僵尸股
    bottom_min_avg_amount: float = 50_000_000.0  # 20日均成交额下限（>=5000万）：成交量不能太低
    # 14因子分二次过滤（软闸）：候选需满足 因子分 >= 此值才保留。
    # 实测：底部反转（均值回归）策略里，因子分与 5 日收益相关性≈0.06（噪声级），
    # 加此闸反而拖累收益（对照 -25.7% → 加闸 -33.7%），故默认关闭（-100）。
    # 因子分仍作为面板"质量参考列"展示；如想实验可在设置页开启（建议 ≤ -50 的软闸，勿设正）。
    bottom_min_factor_score: float = -100.0    # 14因子分软闸下限（100分制；-100=关闭，因子分仅作展示）
    bottom_trend_filter: bool = True          # 板块/全市场趋势过滤：仅在所选趋势指数处于上升通道（> MA）的交易日才出候选/开仓
    bottom_trend_ma: int = 60                 # 趋势过滤所用均线周期（日）
    bottom_trend_index: str = "sh000985"      # 趋势过滤所用指数（全市场口径）：sh000985=中证全指（覆盖全A，最宽的全市场基准），分析趋势时全市场一起考虑；
                                              #   填 sz399006=官方创业板指（仅看创业板）；留空=300/301等权合成指数（回测基线兼容，+9.94%）。
    bottom_time_stop_days: int = 0            # 主动离场-时间止损：持仓达此天数仍未启动跟踪止损(即未盈利)则离场(0=关闭；回测显示对均值回归策略有害，默认关)
    bottom_trailing_pct: float = 0.0          # 主动离场-跟踪止损：涨幅达此比例启动跟踪，自峰值回撤此比例即离场(0=关闭；宽幅如0.15近中性，默认关)
    # 选股扫描板块（已统一跟随 twin_t_boards，勿在此单独改）。保留字段仅为向后兼容：
    # screener / api 现统一以 twin_t_boards 作为「选股 + 交易」的唯一板块口径，两者始终一致，
    # 因此 /api/scope/stats 的 board_consistency 始终 ok，不再弹出「板块不一致」告警。
    bottom_boards: str = "创业板"
    # #181 多板块交易策略适配：策略参数（target_pct 止盈幅度）原先按创业板 ±20% 波动标定，
    # 直接套到主板 ±10%（+25% 止盈几乎打不到）或北交所 ±30%（一个板就穿透）会失真。
    # 开启后止盈目标按板块涨跌停幅度等比缩放（基准=创业板 20%，主板×0.5、北交所×1.5），
    # 创业板/科创板保持原值不变。默认关=零行为变更；建议切换到非创业板板块时开启。
    bottom_scale_exit_by_limit: bool = False
    # 底部反转候选自动接入交易链（#191）：收盘刷新候选后自动写入 signals + 自选股，
    # 让底反候选与 scanner 命中走同一套下游（观察/建议）。默认关闭；开启即自动入链，
    # 但不自动送队列（不主动买卖）。如需进一步自动送交易队列，用 API /bridge 显式发起。
    bottom_reversal_autobridge: bool = False

    # ---------- 统一大盘宏观风险层（Layer2：含外围，驱动仓位上限） ----------
    # 把「国内大盘 + 外围前一/当日」综合成 market risk score → calm/weak/shock/crisis 四档，
    # 映射到本层专属的 unified_risk_cap_* 仓位上限。与 Layer1 板块感知趋势过滤相互独立：
    # Layer1 决定「某板块的票能不能选」，Layer2 决定「整体能开多高仓位」。
    # 注意：这里**刻意不复用** paper_regime_cap_*。后者属于机制层 _dynamic_regime()
    # （波动率/回撤触发，shock 默认 0.40 是「高波动+政策托底可收割」的历史回测口径），
    # 语义与本层「多板块趋势 + 外围宏观风险」不同；直接套用会出现「市场更弱、上限反而
    # 从 30% 放宽到 40%」的反直觉结果。本层四档默认单调递减，且不动既有回测参数。
    # 详见 app/analysis/market_risk.py。
    unified_risk_ma: int = 60                 # 国内/美股成分计算弱势度所用的均线周期（日）
    unified_risk_domestic: list[str] = [      # 国内大盘成分（akshare 代码，需本地有 idx_<code>.parquet）
        "sh000001", "sz399001", "sz399006", "sh000300",
    ]
    unified_risk_include_overseas: bool = True   # 是否纳入外围（美股前一日 / 亚太当日）
    # score → 档位阈值（弱势度加权平均，0~1 标度；越大越弱）
    unified_risk_thr_weak: float = 0.02       # < 此值 = calm（不降仓）
    unified_risk_thr_shock: float = 0.06      # weak 上界
    unified_risk_thr_crisis: float = 0.12     # shock 上界；> 此值 = crisis（最严上限 + 全暂停买入）
    # 四档仓位上限（calm 固定 0 = 不降仓，沿用总仓位上限；其余随风险单调递减）
    unified_risk_cap_weak: float = 0.30       # 弱势：压到 30%（与用户手设的弱市上限一致）
    unified_risk_cap_shock: float = 0.20      # 震荡：进一步压到 20%（比 weak 更严）
    unified_risk_cap_crisis: float = 0.10     # 危机：压到 10%，且全暂停新开仓买入

    # ---------- P1 上下文风险闸（回测/实盘共用 app.risk.context_gates，改动必须两侧同步） ----------
    # 四项：组合集中度/相关性、事件日历、舆情新闻、板块过热剔除。0 = 关闭该子闸。
    # 数据一律来自按 as-of 截断的本地快照（context_events / context_news / context_heat），
    # 策略代码不调实时接口；快照缺失时两侧一致地不触发，避免半边生效。
    paper_ctx_enabled: bool = True               # 四项总开关；False = 完全回到 P1 之前的行为
    paper_ctx_max_industry_weight: float = 0.40  # 买入后同一行业只数/组合总只数上限；0=关闭
    paper_ctx_max_corr: float = 0.85            # 与持仓组合 60 日收益平均 |corr| 上限；0=关闭
    paper_ctx_hot_exclude_pct: float = 0.20     # 剔除个股/行业热度最热 20%；0=关闭
    paper_ctx_event_cooldown_days: int = 3      # 事件日及之后 N 天禁买；0=关闭
    paper_ctx_news_block: float = 0.60          # 加权舆情分 <= -0.60 禁买；0=关闭

    # ---------- 前端 ----------
    # 允许跨域访问的前端源（Vite 开发服务器默认端口）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    # ---------- 访问控制 ----------
    # 公开部署（如 cpolar 隧道、云主机）时设置：所有 /api/* 请求需带 `X-API-Token`
    # 请求头（或 `Authorization: Bearer <token>`），否则 401。留空 = 不校验（本机开发）。
    # 该字段在 READONLY_KEYS 里：设置页不展示、不可改，避免令牌本身被接口读走。
    api_token: str = ""


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
