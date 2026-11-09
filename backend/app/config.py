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

    # ---------- 执行口径对齐回测（见 BACKTEST_LOG 记录 #19） ----------
    # 为什么需要：回测用「每日成交额 TopN」池，实盘此前是全市场扫描，
    # 于是回测里选不到的票（尤其北交所小票）被真实买入，回测结论无法解释实盘。
    # 只允许买入当日成交额 TopN 内的股票；0=不限制（回到旧的全市场行为）
    paper_pool_top_n: int = 300
    # 排除北交所（920/43/83/87/4/8 开头）：日波动 5~10%，固定 8% 止损≈1×ATR，
    # 会被正常噪声打掉；且 Top300 成交额池里几乎不会出现
    paper_exclude_bj: bool = True
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
    # 机制层止损收紧：shock/crisis 态新建仓用更紧的止损距离（距离 × 此系数），
    # 只限新建仓（已有持仓止损在建仓时定死，不回溯改）。0=关闭（原行为，保守默认）。
    # 风险态下宁可早点砍、少亏；指数数据缺失 → regime=normal → 不收紧，零风险。
    paper_regime_stop_tighten: float = 0.0
    paper_regime_stop_min: float = 0.03   # 收紧后止损距离下限（防止过度收紧成噪声止损）
    # ---- 模拟盘大盘择时买入闸门（弱市暂停自动买入） ----
    paper_regime_ma: int = 60           # 上证指数 MA 周期，<=0 关闭
    paper_regime_weak_pause: bool = True  # 弱市（指数在 MA 下）暂停自动买入
    #: 弱市**保留仓位比例**（0=全暂停，即上面的开关注释行为；>0 时弱市不暂停买入，
    #: 而是把「总仓位上限」压到该比例继续参与）。回测记录 #59/#60：0.3~0.5 在
    #: 急跌月小亏换反弹时的收益，代价是涨市初期踏空。默认 0 保持既有行为不变。
    paper_weak_exposure: float = 0.0
    # ---- 动态机制降暴露（机制检测层；默认关闭，零风险） ----
    # 开启后由「波动率/回撤/趋势」把市场分成 calm/weak/shock/crisis 四态，
    # 动态压低总仓位上限（只降不升），替代/增强上面的静态弱市档位。与回测
    # pipeline 的 regime_tiers 同思路，但作用在实时引擎。改动见 BACKTEST_LOG #84。
    paper_dynamic_exposure: bool = False
    paper_regime_cap_calm: float = 0.0    # 平稳仓上限：0 = 不降仓（用总仓位上限）
    paper_regime_cap_weak: float = 0.30   # 弱势仓上限
    paper_regime_cap_shock: float = 0.40  # 震荡仓上限
    paper_regime_cap_crisis: float = 0.15  # 危机仓上限（同时触发全暂停买入）
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

    # ---------- 访客访问记录（区分真实用户 / 开发者调试） ----------
    # 开发者自测用的公网 IP（逗号分隔）：这些 IP 的访问会被标记为调试流量，
    # 默认不计入「真实访客」。本机 localhost / 局域网地址无需列出，已自动排除。
    # 例：DEV_IPS=203.0.113.7,198.51.100.2
    dev_ips: list[str] = []
    # 开发者标记密钥：开发者在请求头带 `X-Dev-Key: <密钥>` 即标记为调试流量，
    # 便于「用同一公网 IP 自测」时也能把那次访问排除。留空 = 不启用（仅靠 IP 区分）。
    dev_key: str = ""

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
