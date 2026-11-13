"""参数单一来源：把 ``Settings`` 里**可编辑**的字段整理成一份可读清单。

背景：同一个参数目前散在四个地方——`.env` / `config.py`、`app_state`（运行时覆盖）、
页面上的局部门开关、以及硬编码常量（黑名单、阈值、费率）。改一处忘一处就会出现
「开关关了没效果」这类 bug（见 WORKLOG 2026-10-30 那条）。

本模块做三件事：
1. :func:`settings_overview` —— 把「当前生效值 + 来源」吐给 `GET /api/settings`
   与「系统设置」页；只列可编辑项，路径/列表类（`READONLY_KEYS`）与硬编码常量
   **不进清单**（改不了就没必要展示，只会让人以为能调）。展示层级由 :data:`_LAYOUT`
   声明：分组 → 组内小节 → 参数（每组 7~13 项，避免一个 tab 里堆 50 项）；
2. :func:`update_settings` / :func:`reset_settings` —— 唯一写入路径：写 `app_state`
   覆盖层 + 就地改单例，重启由 :func:`apply_persisted_overrides` 重放；
3. 集中声明 :data:`RUNTIME_KEYS`（沿用无前缀旧键）与各项校验规则。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.config import BASE_DIR, Settings, get_settings

logger = logging.getLogger("sas.param_specs")

#: 允许运行时改（走 app_state 覆盖层，不用重启）的键。
#: 目前只有大盘股开关与模拟盘模式真正在跑这条路，其余仍是"改 .env + 重启"。
RUNTIME_KEYS: frozenset[str] = frozenset({
    "paper_exclude_large_cap",
    "paper_large_cap_yi",
    "paper_trade_mode",
    # 弱市买入闸门：一天之内可能要多试几次（关掉看是否恢复买入/卖出），
    # 改 .env 重启代价太高，且它是「今天是否开仓」的唯一决定项。
    "paper_regime_weak_pause",
    # 入场护栏硬屏蔽：盘中想立刻开/关「下降市拦截买入」，改 .env 重启代价太高
    "entry_guard_block_declining_market",
    # 清非交易板块持仓：盘中开启即开始清仓（开盘前/收盘结算都会触发），无需重启
    "paper_liquidate_offboard",
    # 涨停禁买：盘中想立刻开/关「封板不买」以排查成交异常，无需重启
    "paper_block_limit_up_buy",
    # 弱市保留仓位：属于风险偏好档位，盘中想从「全暂停」切到「留三成」要能立刻生效
    "paper_weak_exposure",
    # 动态机制降暴露：风险偏好档位，盘中切换要立刻生效（同弱市保留仓位）
    "paper_dynamic_exposure",
    "paper_regime_cap_calm",
    "paper_regime_cap_weak",
    "paper_regime_cap_shock",
    "paper_regime_cap_crisis",
    # 政策托底强度：是否把"看不见的底"纳入分治，盘中切要立刻生效
    "paper_policy_floor_enabled",
    "paper_policy_floor_min_score",
    "paper_policy_floor_low_escalate",
    "paper_policy_floor_harvest_cap",
    # 硬性回撤熔断：最后一道防线，风险偏好档位，盘中切换要立刻生效
    "paper_circuit_breaker_enabled",
    "paper_circuit_breaker_dd",
    "paper_circuit_breaker_release",
    "paper_circuit_breaker_target",
    # 开盘冷静期：无条件禁止开盘后N分钟买入，风险偏好类，盘中切换要立刻生效
    "paper_open_cooldown_minutes",
    # 创业板底部反转选股：阈值/趋势开关盘中切要即时生效（改 .env 重启代价高）
    "bottom_start_pos_max",
    "bottom_dd20_min",
    "bottom_turnover_min",
    "bottom_min_avg_amount",
    "bottom_min_factor_score",
    "bottom_trend_filter",
    "bottom_trend_ma",
    "bottom_trend_index",
    "bottom_time_stop_days",
    "bottom_trailing_pct",
    # 注：板块白名单已统一为 twin_t_boards（选股+交易共用），bottom_boards 不再单独可调
    # 统一大盘宏观风险层（含外围）阈值：驱动总仓位上限
    "unified_risk_ma",
    "unified_risk_include_overseas",
    "unified_risk_thr_weak",
    "unified_risk_thr_shock",
    "unified_risk_thr_crisis",
    # 四档仓位上限（Layer2 专属，与机制层 paper_regime_cap_* 解耦，默认单调递减）
    "unified_risk_cap_weak",
    "unified_risk_cap_shock",
    "unified_risk_cap_crisis",
    # #181 板块交易规则：涨跌停/申报数量按板块区分的回退开关 + 止盈按板块缩放
    "board_rules_legacy_limit",
    "bottom_scale_exit_by_limit",
    # 开盘成交窗口：0=全时段可成交（仅受冷静期约束）；>0=只限开盘后N分钟。配合冷静期用。
    "paper_buy_open_window_minutes",
    # P2 盘内错峰：每 tick 最多成交几只买入，积压信号错峰（0=不限制，回到旧行为）
    "paper_buys_per_tick",
    # 机制层止损收紧：shock/crisis 态新建仓收紧止损距离（仅新建仓），风险偏好类，盘中切换即时生效
    "paper_regime_stop_tighten",
    "paper_regime_stop_min",
    # 入场护栏提示阈值：高开/盘中冲高提示的灵敏度，提示类，盘中切即时生效
    "entry_guard_gap_threshold",
    "entry_guard_spike_threshold",
    # 动态机制检测阈值：决定何时进入 shock/crisis 态，风控灵敏度，盘中切即时生效
    "paper_regime_vol_win",
    "paper_regime_vol_base",
    "paper_regime_dd_win",
    "paper_regime_crisis_vol",
    "paper_regime_crisis_dd",
    "paper_regime_shock_vol",
    "paper_regime_shock_dd",
})

#: 多选控件（checkbox 组）参数：键 → 可选项列表（顺序即展示顺序与规范化顺序）。
#: 值存为逗号分隔字符串（与 config.py 字段类型 ``str`` 一致），提交时按此列表规范化、
#: 丢弃未知项、按声明顺序重排；前端据此渲染成一组可勾选的板块芯片。
MULTISELECT_KEYS: dict[str, tuple[str, ...]] = {
    "twin_t_boards": ("主板", "创业板", "科创板", "北交所"),
}

#: 设置页布局：**分组（左侧导航）→ 子分组（组内小节）→ 参数键**。
#:
#: 为什么不再用前缀规则：`paper_` 一条前缀就把 50 项塞进同一个 tab，而「基础与存储」
#: 只有 1 项 —— 页面一边要翻三屏，一边是一片空白。这里按**业务语义**显式排布：
#: 每组 7~13 项，组内再切 2~5 个语义小节，左右视觉重量接近、找东西靠语义不靠翻。
#:
#: 新增字段不必改这里：没收录的键走 `_FALLBACK_GROUP_RULES` 前缀兜底（落到对应组的
#: 「其他」小节）。想调位置就在下面挪键名即可 —— 布局只影响展示，不影响写模型与校验。
_LAYOUT: tuple[tuple[str, str, tuple[tuple[str, str, tuple[str, ...]], ...]], ...] = (
    (
        "连接与扫描",
        "行情抓取、并发与缓存；改完下一轮扫描即生效",
        (
            ("请求与直连", "网络层开关与单次请求超时", ("data_direct_only",)),
            (
                "扫描并发",
                "全市场扫描的并发度与等待上限",
                ("scan_concurrency", "scan_wait_timeout", "scan_auto_watch_limit", "watch_auto_max"),
            ),
            ("抓取与缓存", "失败重试与快照缓存时长", ("fetch_retries", "snapshot_ttl")),
            ("K 线预热", "历史 K 线的预热范围与并发", ("kline_warmup_concurrency", "kline_warmup_top_n")),
        ),
    ),
    (
        "账户与费率",
        "模拟盘本金、持仓上限与交易成本",
        (
            (
                "账户与仓位",
                "本金规模、最大持仓数与总仓位上限",
                ("paper_initial_capital", "paper_max_positions", "paper_max_total_exposure"),
            ),
            (
                "交易费率",
                "回测与实盘共用的一套费率",
                (
                    "trade_fee_commission_rate",
                    "trade_fee_commission_min",
                    "trade_fee_stamp_rate",
                    "trade_fee_transfer_rate",
                ),
            ),
        ),
    ),
    (
        "交易执行",
        "自动下单、信号入队与股票池过滤",
        (
            (
                "自动交易",
                "是否自动把信号变成委托，以及撮合节奏",
                (
                    "paper_auto_trade",
                    "paper_live_enabled",
                    "paper_tick_interval",
                    "paper_signal_ttl_minutes",
                    "paper_scan_autotrade",
                    "paper_scan_autotrade_top_n",
                ),
            ),
            (
                "信号与队列",
                "过夜信号、买入队列长度与成交窗口",
                (
                    "paper_scan_autotrade_overnight",
                    "paper_trade_queue_top_m",
                    "paper_buy_open_window_minutes",
                    "paper_pool_top_n",
                ),
            ),
            (
                "股票池过滤",
                "先剔掉不该买的票，再看信号；交易板块跟随「选股策略」页里的选股板块白名单",
                ("paper_exclude_large_cap", "paper_large_cap_yi",
                 "paper_liquidate_offboard", "board_rules_legacy_limit"),
            ),
        ),
    ),
    (
        "仓位与轮动",
        "评分档位、轮动替换与候补队列",
        (
            ("仓位评分", "评分与仓位权重的对应关系", ("paper_full_score", "paper_half_score")),
            (
                "轮动替换",
                "不满仓也换：有更明显的强票就把最弱持仓换掉（可换多只）",
                (
                    "paper_rotation_enabled",
                    "paper_rotation_min_score",
                    "paper_rotation_gap",
                    "paper_rotation_max_per_day",
                ),
            ),
            ("候补队列", "满仓时把高分票先存着", ("paper_backlog_enabled", "paper_backlog_max")),
            (
                "买入节奏",
                "每 tick 最多成交几只买入，积压信号盘内错峰（0=不限制）",
                ("paper_buys_per_tick",),
            ),
            (
                "收盘减仓",
                "80% 是收盘目标（盘中允许满仓），收盘前把超出部分按最弱优先卖掉",
                ("paper_eod_trim_enabled", "paper_eod_trim_minutes"),
            ),
        ),
    ),
    (
        "风控与止损",
        "切入节奏、固定止损止盈与移动保护",
        (
            (
                "买点切入",
                "不追高、等回踩、超时放弃、涨停禁买（按板块幅度）",
                ("paper_entry_max_chase", "paper_entry_pullback", "paper_entry_timeout_minutes",
                 "paper_block_limit_up_buy"),
            ),
            (
                "固定止损止盈",
                "最外层的那道硬风控",
                ("paper_stop_loss", "paper_take_profit", "paper_single_position_max"),
            ),
            (
                "ATR 与移动保护",
                "波动自适应止损、保本与移动止盈",
                (
                    "paper_atr_stop_mult",
                    "paper_atr_stop_max",
                    "paper_breakeven_pct",
                    "paper_trail_pct",
                    "paper_trail_arm",
                    "paper_max_hold_days",
                ),
            ),

        ),
    ),
    (
        "做T 与择时",
        "日内做T 引擎与大盘择时",
        (
            ("做T 开关", "总开关与做T 仓位比例", ("paper_t_active", "paper_t_size")),
            (
                "做T 买卖点",
                "高开锁利、回调买回、低开加仓",
                (
                    "paper_t_up_sell",
                    "paper_t_up_buyback",
                    "paper_t_down_add",
                    "paper_t_down_full",
                    "paper_t_rebound_sell",
                ),
            ),
            ("做T 保护", "涨停卖半与做T 熔断", ("paper_t_limit_sell_half", "paper_t_crash_guard")),
            (
                "大盘择时",
                "指数在均线下方时收缩买入；统一宏观风险（含外围）四档压仓也可调",
                ("paper_regime_ma", "paper_regime_weak_pause", "paper_weak_exposure",
                 "entry_guard_block_declining_market", "entry_guard_gap_threshold",
                 "entry_guard_spike_threshold", "paper_board_trend_gate",
                 "paper_unified_risk_cap_enabled",
                 "paper_regime_vol_win", "paper_regime_vol_base",
                 "paper_regime_dd_win", "paper_regime_crisis_vol", "paper_regime_crisis_dd",
                 "paper_regime_shock_vol", "paper_regime_shock_dd",
                 "unified_risk_ma", "unified_risk_include_overseas",
                 "unified_risk_thr_weak", "unified_risk_thr_shock", "unified_risk_thr_crisis",
                 "unified_risk_cap_weak", "unified_risk_cap_shock", "unified_risk_cap_crisis"),
            ),
        ),
    ),
    (
        "调度与 LLM",
        "定时任务、推送渠道与大模型接入",
        (
            ("调度", "定时任务的开关与时区", ("scheduler_enabled", "scheduler_timezone")),
            ("推送通知", "外部渠道的推送地址", ("notify_webhook_url",)),
            (
                "LLM 接入",
                "任意 OpenAI 兼容接口，未配置时自动降级为纯技术面",
                ("llm_enabled", "llm_base_url", "llm_api_key", "llm_model", "llm_timeout"),
            ),
        ),
    ),
    (
        "高级 / 研究参数",
        "研究观测、影子策略与默认关闭的高级风控；日常交易一般不用改",
        (
            (
                "研究观测（摆动影子）",
                "相对强弱极值共振，只观测不交易，不参与下单",
                ("swing_threshold_pct", "swing_atr_mult", "swing_confirm_bars", "swing_min_gap",
                 "swing_anchor_bars", "swing_z_window", "swing_band", "swing_extreme_z",
                 "swing_min_corr", "swing_max_symbols", "swing_est_horizon", "swing_est_prior_strength",
                 "swing_est_symbol_cap", "swing_est_entry_lift", "swing_est_cost_pct",
                 "swing_est_min_exp_net", "swing_est_features", "swing_est_calibration",
                 "swing_est_calib_min_samples", "swing_est_calib_refit_every", "swing_est_calib_l2",
                 "swing_exit_mode", "swing_exit_hold_bars", "swing_exit_atr_k",
                 "swing_exit_floor_pct", "swing_exit_take_profit_pct"),
            ),
            (
                "影子策略",
                "冻结参数前向验证，只记录不交易（起点写入即冻结）",
                ("shadow_start", "shadow_tag", "shadow_universe", "shadow_profiles"),
            ),
            (
                "高级风控（默认关闭）",
                "动态降暴露 / 托底 / 熔断 / 并行账本 / 机制层止损收紧；默认全关",
                ("paper_dynamic_exposure", "paper_regime_cap_calm", "paper_regime_cap_weak",
                 "paper_regime_cap_shock", "paper_regime_cap_crisis", "paper_policy_floor_enabled",
                 "paper_policy_floor_min_score", "paper_policy_floor_low_escalate",
                 "paper_policy_floor_harvest_cap", "paper_circuit_breaker_enabled",
                 "paper_circuit_breaker_dd", "paper_circuit_breaker_release", "paper_circuit_breaker_target",
                 "paper_sim_enabled", "paper_sim_profiles", "paper_sim_capital",
                 "paper_regime_stop_tighten", "paper_regime_stop_min"),
            ),
            (
                "已废弃（统一到 twin_t_boards）",
                "bottom_boards 已并入「选股+交易板块」(twin_t_boards)，保留字段仅为向后兼容；页面不再展示，仅 API 可写以同步",
                ("bottom_boards",),
            ),
            (
                "网络",
                "请求超时（当前未被任何代码读取，仅保留以防后续启用）",
                ("request_timeout",),
            ),
        ),
    ),
)

#: 分组展示顺序（只列**可编辑**的组；只读的部署项与硬编码常量单独返回，
#: 不混进参数列表——页面上能改的才叫"系统设置"，改不了的塞进去只会误导）
GROUP_ORDER: tuple[str, ...] = tuple(g[0] for g in _LAYOUT)

#: 从「系统设置」页**完全移除**、但仍可经 `update_settings` / 选股策略面板编辑的键。
#:
#: 语义：这些参数是「选股策略」的一部分（选股板块、评分阈值、盘后自选、底部反转选股口径），
#: 用户要求只在「选股策略」(全市场选股) 页里改，不在「系统设置」里重复出现。它们仍是
#: 合法可编辑项（`update_settings` 照常写 `app_state` 覆盖层 + 就地改单例），只是不在
#: 系统设置总览里渲染——否则会落进 `_FALLBACK_GROUP_RULES` 的兜底「其他」分组，
#: 造成"明明移走了还在"的假象。选股策略页专用总览见 :func:`selection_settings_overview`。
SETTINGS_SYSTEM_HIDDEN: frozenset[str] = frozenset({
    # 选股 + 交易统一板块（原在「交易执行 → 股票池过滤」）
    "twin_t_boards",
    # 选股与自选组
    "factor_buy_threshold", "factor_sell_threshold", "live_ml_scoring",
    "auto_watch_enabled", "auto_watch_top_n", "focus_top_n",
    "auto_scan_scope", "intraday_analysis_interval",
    # 创业板底部反转选股口径（不含 unified_risk_* 风险压仓项，后者仍留在系统设置）
    "bottom_start_pos_max", "bottom_dd20_min", "bottom_turnover_min",
    "bottom_min_avg_amount", "bottom_min_factor_score",
    "bottom_trend_filter", "bottom_trend_ma", "bottom_trend_index",
    "bottom_scale_exit_by_limit", "bottom_reversal_autobridge",
    "bottom_time_stop_days", "bottom_trailing_pct",
})

#: 页面隐藏分组：这些分组里的参数**仍保留在 `Settings` 字段与业务代码读取中**
#: （回测 / 影子策略 / 引擎风控仍在用），只是不在设置页展示——避免把基本不需要
#: 调整的内部参数摊给用户。需要恢复展示时，从本集合删掉对应分组名即可（无副作用）。
_HIDDEN_GROUPS: frozenset[str] = frozenset({"高级 / 研究参数"})

#: 分组一句话说明（左栏导航与右栏标题下都用它）
GROUP_DESCS: dict[str, str] = {g[0]: g[1] for g in _LAYOUT}

#: 键 → (分组, 子分组)；由 `_LAYOUT` 反查，避免两处手工维护
_PLACEMENT: dict[str, tuple[str, str]] = {
    key: (group, sub)
    for group, _gdesc, subs in _LAYOUT
    for sub, _sdesc, keys in subs
    for key in keys
}

#: 子分组一句话说明
SUBGROUP_DESCS: dict[tuple[str, str], str] = {
    (group, sub): sdesc
    for group, _gdesc, subs in _LAYOUT
    for sub, sdesc, _keys in subs
}

#: 未收录进 `_LAYOUT` 的新字段按前缀兜底（落到该组的「其他」小节）。
#: 顺序敏感：`paper_t_` / `paper_regime_` 必须先于通用的 `paper_`。
_FALLBACK_GROUP_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("paper_t_", "paper_regime_"), "做T 与择时"),
    (("paper_", "trade_fee_"), "交易执行"),
    (("scan_", "fetch_", "kline_", "request_", "snapshot_", "proxy", "http_", "minute", "data_"), "连接与扫描"),
    (
        ("auto_watch_", "factor_", "analysis_", "watch_", "focus_", "auto_scan_", "shadow_", "intraday_"),
        "选股与自选",
    ),
    (("scheduler_", "notify_", "telegram_", "alert_", "monitor_", "llm_", "app_", "debug", "base_", "log_"), "调度与 LLM"),
)

#: 页面不可编辑、也不展示的键。
#:
#: 两类：① 路径 / 列表类——进程启动时就被各处捕获（静态挂载、CORS、缓存目录）；
#: ② 仅启动时读取的一次性配置——`app_name` 只用于 `FastAPI(title=...)`，
#: 改它连前端页面标题都不会变（标题写死在 index.html）。运行中改这些只会造成
#: 「显示改了、实际没变」的错觉，干脆不进清单。
READONLY_KEYS: frozenset[str] = frozenset({
    "kline_dir",
    "db_path",
    "cors_origins",
    "data_no_proxy_domains",
    "auto_scan_strategies",
    "app_name",
    # 历史遗留字段：全仓库没有任何代码读它（不是"启动时读一次"，是根本没用）。
    # 做成可调开关只会让人以为开了有区别。
    "debug",
    # 文案常量：接口 JSON 里统一注入的免责声明，改它等于改 UI 文案，不是配置
    "disclaimer",
    # 预留渠道：EmailNotifier 目前只打一行日志并返回 False，打开它反而会让每次
    # 推送多记一条「渠道失败」，属于"看着能开、开了更糟"的开关
    "notify_email",
    # 访问令牌：只从 .env 读，绝不能出现在 GET /api/settings 的返回里
    # （否则等于把钥匙挂在门上）
    "api_token",
    # 国内大盘成分（列表字段，UI 不可编辑，改 .env 后重启）
    "unified_risk_domestic",
})

#: 需要脱敏回显的项：接口只返回掩码（`sk-****abcd`），前端原样回传视为「未修改」。
#: 这类值要么是密钥、要么是带 token 的机器人地址，明文回显等于公开。
SECRET_KEYS: frozenset[str] = frozenset({"llm_api_key", "notify_webhook_url"})

#: 掩码标记
MASK_MARK = "****"


def mask_secret(value: Any) -> str:
    """密钥/地址脱敏：保留头 3 位与尾 4 位，中间打码。

    空值返回空串（前端据空串判断「未配置」）；太短的值整体打码，避免拼出原文。
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 10:
        return MASK_MARK
    return f"{text[:3]}{MASK_MARK}{text[-4:]}"

#: 条件可见：子键 → 父键。
#:
#: 语义只有两条，够用且不用写表达式：
#:   • 父键是开关（bool）→ 父为 true 时子项才显示；
#:   • 父键是文本/数字 → 父为空（空串 / 0 / false）时子项隐藏。
#:
#: 为什么交给前端算：显隐要跟着**草稿**即时变化（打开「做T 总开关」立刻看到下面
#: 八个参数），后端只声明关系，不参与渲染决策；`depends_on` 也可以套娃
#: （`paper_scan_autotrade` 依赖 `paper_auto_trade`，它自己的两个子项再依赖它）。
_DEPENDS_ON: dict[str, str] = {
    # 做T 引擎：总开关关着时，下面 8 个参数改了也没有任何效果
    "paper_t_size": "paper_t_active",
    "paper_t_up_sell": "paper_t_active",
    "paper_t_up_buyback": "paper_t_active",
    "paper_t_down_add": "paper_t_active",
    "paper_t_down_full": "paper_t_active",
    "paper_t_rebound_sell": "paper_t_active",
    "paper_t_limit_sell_half": "paper_t_active",
    "paper_t_crash_guard": "paper_t_active",
    # LLM：未启用时模型名/密钥/超时都无从生效（启用后自动降级与否由它自己判断）
    "llm_base_url": "llm_enabled",
    "llm_api_key": "llm_enabled",
    "llm_model": "llm_enabled",
    "llm_timeout": "llm_enabled",
    # 影子账户：起点为空 = 不启用，其余三项只是它的作用域
    "shadow_tag": "shadow_start",
    "shadow_universe": "shadow_start",
    "shadow_profiles": "shadow_start",
    # 并行模拟账本：关掉总开关后，方案清单与本金都无从生效
    "paper_sim_profiles": "paper_sim_enabled",
    "paper_sim_capital": "paper_sim_enabled",
    # 各种「总开关 + 细项」：关掉后细项无意义
    "paper_rotation_min_score": "paper_rotation_enabled",
    "paper_rotation_gap": "paper_rotation_enabled",
    "paper_rotation_max_per_day": "paper_rotation_enabled",
    "paper_backlog_max": "paper_backlog_enabled",
    "paper_large_cap_yi": "paper_exclude_large_cap",
    # 弱市保留仓位只在「弱市暂停买入」开启时有意义（关掉闸门就谈不上弱市档位）
    "paper_weak_exposure": "paper_regime_weak_pause",
    "auto_watch_top_n": "auto_watch_enabled",
    "scheduler_timezone": "scheduler_enabled",
    # 自动交易关掉 = 不产生委托，撮合方式（tick / 批量）与收盘自动下单都无从谈起
    "paper_live_enabled": "paper_auto_trade",
    "paper_tick_interval": "paper_auto_trade",
    "paper_scan_autotrade": "paper_auto_trade",
    "paper_scan_autotrade_top_n": "paper_scan_autotrade",
    "paper_scan_autotrade_overnight": "paper_scan_autotrade",
}

#: 运行时覆盖在 app_state 里的前缀（`RUNTIME_KEYS` 那几项用无前缀的既有键）
OVERRIDE_PREFIX = "cfg:"

#: 必须严格大于该值的数值项（集中校验，避免每个入口各写一份）
_MIN_EXCLUSIVE: dict[str, float] = {
    "paper_large_cap_yi": 0.0,
    "paper_max_positions": 0.0,
    "paper_max_total_exposure": 0.0,
    # 风控三项必须为正：0 会让「止损线=成本价」直接清仓，属于误操作而非关闭
    "paper_stop_loss": 0.0,
    "paper_take_profit": 0.0,
    "paper_single_position_max": 0.0,
    # 出场规则：持有上限 0 根没有意义（等于入场即卖），ATR 倍数 0 会让跟踪退化成死止损
    "swing_exit_hold_bars": 0.0,
    "swing_exit_atr_k": 0.0,
    # 机制检测窗口：0 个交易日没有意义（取不到足够的波动率/回撤样本）
    "paper_regime_vol_win": 0.0,
    "paper_regime_vol_base": 0.0,
    "paper_regime_dd_win": 0.0,
    # 创业板底部反转选股阈值：必须为正的合理性下限
    "bottom_trend_ma": 0.0,
    "bottom_min_avg_amount": 0.0,
    "bottom_dd20_min": 0.0,
    "bottom_turnover_min": 0.0,
    "bottom_start_pos_max": 0.0,
}

#: 不允许为负的数值项（费率类：负费率会让回测/实盘 PnL 凭空变好）
_NON_NEGATIVE: frozenset[str] = frozenset({
    "trade_fee_commission_rate",
    "trade_fee_commission_min",
    "trade_fee_stamp_rate",
    "trade_fee_transfer_rate",
    # 弱市保留仓位：0 是合法档位（=全暂停买入），所以不能进 _MIN_EXCLUSIVE
    "paper_weak_exposure",
    # 动态机制档位：比例项，0 合法（calm=不降仓），不能进 _MIN_EXCLUSIVE
    "paper_regime_cap_calm",
    "paper_regime_cap_weak",
    "paper_regime_cap_shock",
    "paper_regime_cap_crisis",
    "paper_policy_floor_min_score",
    "paper_policy_floor_low_escalate",
    "paper_policy_floor_harvest_cap",
    "paper_circuit_breaker_dd",
    "paper_circuit_breaker_release",
    "paper_circuit_breaker_target",
    # 入场护栏提示阈值：比例项，负阈值会让「下跌也提示追高」，无意义
    "entry_guard_gap_threshold",
    "entry_guard_spike_threshold",
    # 机制检测阈值：波动率比/回撤比例项，负值无意义
    "paper_regime_crisis_vol",
    "paper_regime_crisis_dd",
    "paper_regime_shock_vol",
    "paper_regime_shock_dd",
})

#: 必须 ≤1 的比例项（>1 等于加杠杆，属于误操作）
_MAX_ONE: frozenset[str] = frozenset({
    "paper_weak_exposure",
    "paper_regime_cap_calm",
    "paper_regime_cap_weak",
    "paper_regime_cap_shock",
    "paper_regime_cap_crisis",
    "paper_policy_floor_min_score",
    "paper_policy_floor_low_escalate",
    "paper_policy_floor_harvest_cap",
    "paper_circuit_breaker_dd",
    "paper_circuit_breaker_release",
    "paper_circuit_breaker_target",
    "bottom_start_pos_max",
})

#: 跨字段约束：单键下限表达不了「A 必须大于 B」，同批提交时合并校验
_CROSS_RULES: tuple[tuple[tuple[str, str], str], ...] = (
    (("factor_buy_threshold", "factor_sell_threshold"), "买入阈值必须大于卖出阈值（否则同一只票同时满足买卖）"),
)

#: 设置页的补充说明（同一参数在别的页面也有快捷入口时提示一下）
_NOTE_OVERRIDES: dict[str, str] = {
    "paper_exclude_large_cap": "也可在「③ 三选 · 交易队列」页顶部快速切换",
    "paper_large_cap_yi": "也可在「③ 三选 · 交易队列」页顶部调整",
}

_FIELD_RE = re.compile(
    r"^    ([a-z_][a-z0-9_]*)\s*:\s*[^=]+=\s*(.+?)(?:\s{2,}#\s*(.+))?$"
)
_COMMENT_RE = re.compile(r"^\s{4}#\s?(.+?)\s*$")

#: 页面**短名**（标准叫法，2~6 字）。写死一份是有意的：
#: config.py 的注释是给人读的整句（"待执行买单最多同时挂 M 条（卖出不受限…）"），
#: 直接把首句当标签，卡片里必然截断成一串省略号。整句仍有用——放到 tooltip 的
#: 「说明」里（见 :func:`_comment_texts`）。没列到的键回退到「首句截断」。
_SHORT_LABELS: dict[str, str] = {
    # ---- 连接与扫描（`app_name` / `debug` 只读，不进清单但仍留短名）----
    "data_direct_only": "数据源直连",
    "app_name": "应用名称",
    "debug": "调试模式",
    # ---- 连接与扫描 ----
    "scan_concurrency": "扫描并发数",
    "fetch_retries": "抓取重试次数",
    "kline_warmup_concurrency": "预热并发数",
    "kline_warmup_top_n": "收盘预热只数",
    "request_timeout": "请求超时",
    "snapshot_ttl": "快照缓存时长",
    "scan_wait_timeout": "扫描等待上限",
    "scan_auto_watch_limit": "扫描入池上限",
    "watch_auto_max": "程序自选容量上限",
    "paper_liquidate_offboard": "清非交易板块持仓",
    # ---- 选股与自选 ----
    "factor_buy_threshold": "买入阈值",
    "factor_sell_threshold": "卖出阈值",
    "auto_watch_enabled": "盘后自动加自选",
    "auto_watch_top_n": "加自选数量",
    "focus_top_n": "明日关注数量",
    "auto_scan_scope": "盘后选股范围",
    "auto_scan_strategies": "盘后选股策略",
    # ---- 费率 ----
    "trade_fee_commission_rate": "佣金费率",
    "trade_fee_commission_min": "最低佣金",
    "trade_fee_stamp_rate": "印花税",
    "trade_fee_transfer_rate": "过户费",
    # ---- 仓位 / 撮合 ----
    "paper_initial_capital": "初始资金",
    "paper_max_positions": "最大持仓数",
    "paper_max_total_exposure": "总仓位上限",
    "paper_full_score": "全额权重分",
    "paper_half_score": "半额权重分",
    "paper_auto_trade": "自动交易",
    "paper_live_enabled": "实时撮合",
    "paper_tick_interval": "撮合间隔",
    "paper_signal_ttl_minutes": "信号有效期",
    "paper_scan_autotrade": "扫描后自动交易",
    "paper_scan_autotrade_top_n": "自动交易上限",
    "paper_scan_autotrade_overnight": "盘后信号入队",
    "paper_buy_open_window_minutes": "开盘成交窗口",
    # ---- 机制层：政策托底 / 熔断 ----
    "paper_policy_floor_enabled": "托底强度检测",
    "paper_policy_floor_min_score": "托底收割阈值",
    "paper_policy_floor_low_escalate": "自由落体阈值",
    "paper_policy_floor_harvest_cap": "托底收割仓位",
    "paper_circuit_breaker_enabled": "硬性回撤熔断",
    "paper_circuit_breaker_dd": "熔断触发回撤",
    "paper_circuit_breaker_release": "熔断解除回撤",
    "paper_circuit_breaker_target": "熔断后仓位",
    # ---- 轮动 / 候补 ----
    "paper_rotation_enabled": "轮动替换",
    "paper_eod_trim_enabled": "收盘减仓",
    "paper_eod_trim_minutes": "收盘减仓窗口(分钟)",
    "paper_rotation_min_score": "轮动评分下限",
    "paper_rotation_gap": "轮动评分差",
    "paper_rotation_max_per_day": "每日轮动上限",
    "paper_backlog_enabled": "候补队列",
    "paper_backlog_max": "候补上限",
    # ---- 五段式③⑤：队列与切入点 ----
    "paper_trade_queue_top_m": "买单队列上限",
    "paper_entry_max_chase": "不追高幅度",
    "paper_entry_pullback": "回踩幅度",
    "paper_entry_timeout_minutes": "切入超时",
    "paper_block_limit_up_buy": "涨停禁买",
    # ---- 股票池过滤 ----
    "paper_pool_top_n": "成交额池 TopN",
    "paper_exclude_large_cap": "大盘股过滤",
    "paper_large_cap_yi": "大盘股阈值",
    "twin_t_boards": "选股+交易板块",
    "board_rules_legacy_limit": "板块规则回退",
    "bottom_scale_exit_by_limit": "止盈按板块缩放",
    "bottom_reversal_autobridge": "底反自动入链",
    # ---- 止损 / 止盈 ----
    "paper_stop_loss": "固定止损",
    "paper_take_profit": "固定止盈",
    "paper_single_position_max": "单票上限",
    "paper_atr_stop_mult": "ATR 止损倍数",
    "paper_atr_stop_max": "ATR 止损上限",
    "paper_breakeven_pct": "保本触发",
    "paper_trail_pct": "移动止损回撤",
    "paper_trail_arm": "移动止损启用",
    "paper_max_hold_days": "短线时间止损(日)",
    "paper_regime_stop_tighten": "机制层止损收紧",
    "paper_regime_stop_min": "收紧后下限",
    # ---- 大盘择时 ----
    "paper_regime_ma": "择时均线",
    "paper_regime_weak_pause": "弱市暂停买入",
    "entry_guard_block_declining_market": "入场护栏硬屏蔽",
    "paper_board_trend_gate": "板块趋势闸门",
    "paper_unified_risk_cap_enabled": "统一风险四档压仓",
    "paper_weak_exposure": "弱市保留仓位",
    "paper_dynamic_exposure": "动态机制降暴露",
    "paper_regime_cap_calm": "平稳仓上限",
    "paper_regime_cap_weak": "弱势仓上限",
    "paper_regime_cap_shock": "震荡仓上限",
    "paper_regime_cap_crisis": "危机仓上限",
    # ---- 做T ----
    "paper_t_active": "做T开关",
    "paper_t_size": "做T仓位比例",
    "paper_t_up_sell": "高开锁利",
    "paper_t_up_buyback": "回调买回",
    "paper_t_down_add": "低开加仓",
    "paper_t_down_full": "跌幅加满",
    "paper_t_rebound_sell": "反弹减仓",
    "paper_t_limit_sell_half": "涨停卖半",
    "paper_t_crash_guard": "做T熔断",
    # ---- 调度 / 通知 ----
    "scheduler_enabled": "调度总开关",
    "scheduler_timezone": "调度时区",
    "notify_webhook_url": "Webhook 地址",
    # ---- LLM ----
    "llm_enabled": "LLM 开关",
    "llm_base_url": "接口地址",
    "llm_api_key": "API Key",
    "llm_model": "模型名称",
    "llm_timeout": "LLM 超时",
    # ---- 其他 ----
    "shadow_start": "影子策略起点",
    "shadow_tag": "影子策略标签",
    "shadow_universe": "影子池规模",
    "shadow_profiles": "影子策略方案",
    # ---- 摆动影子（#120，只观测不交易）----
    "swing_threshold_pct": "摆动幅度",
    "swing_atr_mult": "ATR倍数",
    "swing_confirm_bars": "确认K线",
    "swing_min_gap": "极值间隔",
    "swing_anchor_bars": "共振窗口",
    "swing_z_window": "偏离窗口",
    "swing_band": "跟随带",
    "swing_extreme_z": "背离阈值",
    "swing_min_corr": "同步下限",
    "swing_max_symbols": "影子覆盖",
    "swing_est_horizon": "预估窗口",
    "swing_est_prior_strength": "先验强度",
    "swing_est_symbol_cap": "个股样本",
    "swing_est_entry_lift": "发信号倍数",
    "swing_est_cost_pct": "双边成本",
    "swing_est_min_exp_net": "净收益下限",
    "swing_est_features": "分箱特征集",
    "swing_est_calibration": "校准方式",
    "swing_est_calib_min_samples": "校准最小样本",
    "swing_est_calib_refit_every": "重拟合间隔",
    "swing_est_calib_l2": "校准收缩强度",
    "swing_exit_mode": "出场模式",
    "swing_exit_hold_bars": "持有上限",
    "swing_exit_atr_k": "跟踪ATR倍数",
    "swing_exit_floor_pct": "回撤下限",
    "swing_exit_take_profit_pct": "止盈上限",
    "paper_sim_enabled": "并行账本",
    "paper_sim_profiles": "账本方案",
    "paper_sim_capital": "账本本金",
    "intraday_analysis_interval": "盘中分析间隔",
    "live_ml_scoring": "线上ML打分",
    # ---- 入场护栏提示阈值 ----
    "entry_guard_gap_threshold": "高开提示阈值",
    "entry_guard_spike_threshold": "冲高提示阈值",
    # ---- 动态机制检测阈值 ----
    "paper_regime_vol_win": "波动窗口",
    "paper_regime_vol_base": "波动基线窗",
    "paper_regime_dd_win": "回撤窗口",
    "paper_regime_crisis_vol": "危机波动比",
    "paper_regime_crisis_dd": "危机回撤",
    "paper_regime_shock_vol": "震荡波动比",
    "paper_regime_shock_dd": "震荡回撤",
    # ---- 底部反转选股口径（config 首句注释过长，卡片里会截断）----
    "bottom_dd20_min": "20日回撤下限",
    # ---- 多板块扫描 / 统一宏观风险 ----
    "bottom_boards": "多板块白名单",
    "unified_risk_ma": "宏观均线周期",
    "unified_risk_include_overseas": "纳入外围指数",
    "unified_risk_thr_weak": "风险弱档阈值",
    "unified_risk_thr_shock": "风险震荡档阈值",
    "unified_risk_thr_crisis": "风险危机档阈值",
    "unified_risk_cap_weak": "弱势档仓位上限",
    "unified_risk_cap_shock": "震荡档仓位上限",
    "unified_risk_cap_crisis": "危机档仓位上限",
    "unified_risk_domestic": "国内大盘成分",
}

#: 「首句截断」用的分隔符（没进 `_SHORT_LABELS` 的键走这条路）
_SHORT_CUT = "：:，,。；;（(【[/、 "

#: 少数字段在 config.py 里没有注释（或只有标签），补一句说明给 tooltip 用
_DESC_OVERRIDES: dict[str, str] = {
    "auto_watch_top_n": "收盘选股后按评分取前 N 只自动加入自选",
    "paper_liquidate_offboard": (
        "开启后，引擎在每日开盘前与收盘结算时，把不属于'交易板块'白名单的持仓全部市价卖出"
        "（只清板块不符的，ST/市值维度不动）。用于切换只做创业板后清掉历史遗留的主板票。"
        "默认关闭——开启即开始清仓，请确认后再开；也可调 POST /api/paper/liquidate-offboard 立即清一次"
    ),
    "paper_half_score": "评分介于半额线与全额线之间时，仓位权重打 5 折",
    "paper_scan_autotrade_top_n": "扫描命中后最多自动送多少只进交易队列",
    "paper_rotation_enabled": (
        "满仓时用更高分且真能买进的新票替换最弱持仓；默认开启（模拟盘先把规则跑全），"
        "Top300 池下几乎不触发，出现无谓换手可关掉"
    ),
    "paper_rotation_min_score": "换入票的最低评分（同时要求 ≥ 半额分），低于此分不因轮动卖出任何持仓",
    "paper_rotation_gap": "新票评分需高出最弱持仓的建仓评分多少分才触发轮动",
    "paper_rotation_max_per_day": (
        "每日最多轮动**只数**（一次 = 卖一只弱票换一只强票；默认 10 等于不限，0 = 当日不轮动）"
    ),
    "paper_eod_trim_enabled": (
        "收盘前把总仓位压回「收盘目标」（总仓位上限）；今仓 T+1 卖不掉就跳过，不强求"
    ),
    "paper_eod_trim_minutes": "收盘减仓窗口：收盘前多少分钟内执行（默认 10，即 14:50 起）",
    "paper_backlog_max": "候补队列最多存多少只高分票（有名额时按评分优先买入）",
    "paper_large_cap_yi": "总市值超过该值（亿元）视为大盘股，默认不进买入队列",
    "twin_t_boards": (
        "【选股 + 交易 统一板块】单一板块白名单，同时驱动「选股扫描范围」与「交易买入池」。"
        "可勾中文名（主板/创业板/科创板/北交所）或英文键（sh_main/sz_main/cyb/star/bse），"
        "留空=退回主板+创业板。引擎买入池、日更做T清单、选股扫描范围、非板块持仓清仓都按此过滤。"
        "选股与交易共用同一设置，两者不再各管各的，因此不再出现「板块不一致」告警。"
    ),
    "board_rules_legacy_limit": (
        "板块交易规则回退开关：默认关=按各板块真实规则（主板±10%、创业板/科创板±20%、"
        "北交所±30%、ST±5% 涨跌停；主板/创业板 100 股整手、科创板 200 股起 1 股递增、"
        "北交所 100 股起 1 股递增）。置 True 回到旧的全市场 ±9.5% / 100 股整手口径，"
        "仅用于复现 #178 之前的历史回测基线（+8.49% 等）；改后需重启服务生效"
    ),
    "bottom_scale_exit_by_limit": (
        "多板块交易策略适配：底反策略的止盈幅度原先按创业板 ±20% 波动标定（+25%），"
        "直接套到主板 ±10% 几乎打不到、北交所 ±30% 一个板就穿透。开启后按板块涨跌停等比缩放"
        "（基准创业板 20%：主板×0.5→+12.5%、创业板/科创板×1.0 不变、北交所×1.5→+37.5%）。"
        "默认关=零行为变更；切换到非创业板板块时建议开启，并重新回测验证"
    ),
    "paper_atr_stop_max": "ATR 止损放宽的上限（0.20 = 最多放宽到 20%）",
    "paper_t_active": "做T引擎总开关（默认开启）；关闭后下面各项做T参数不生效",
    "llm_base_url": "OpenAI 兼容接口地址（如 https://api.openai.com/v1）",
    "llm_api_key": "接口密钥；未配置时自动降级为纯技术面结论",
    "llm_model": "模型名，如 gpt-4o-mini / qwen-plus",
    "llm_timeout": "单次请求超时（秒）",
    "shadow_tag": "影子策略落库时用的标签，便于区分多组实验",
    "shadow_universe": "影子策略使用的股票池规模（成交额 TopN）",
    "paper_sim_enabled": "同一份信号按不同处置参数并行记账，盘中实时对比各账本净值",
    "paper_sim_profiles": "逗号分隔的方案名，取自回测方案库，只支持止损/仓位/择时这类处置参数",
    "paper_sim_capital": "每个并行账本的初始资金；0 = 跟随模拟盘本金",
    "paper_open_cooldown_minutes": "开盘冷静期(分钟)：开盘后N分钟内禁止自动买入(含轮动/做T买回)，仅拦买入不拦卖出；0=不限制",
    "bottom_start_pos_max": (
        "近20天前收盘价在近120日价格区间的相对位置上限（0=区间最低、1=最高）；"
        "越小越说明从底部启动。默认0.40：起点落在下半区才入选"
    ),
    "bottom_dd20_min": (
        "相对近20日收盘高点的回撤下限（比值）。默认0.15=较20日高点已跌≥15%，"
        "代表处在低位、已从近期高位回落够深"
    ),
    "bottom_turnover_min": (
        "当日换手率下限（百分制，与数据源 turnover_rate 同单位）。默认5.0=换手率≥5%："
        "成交活跃，过滤冷门僵尸股"
    ),
    "bottom_min_avg_amount": (
        "近20日平均成交额下限（元）。默认5000万：成交量不能太低，过滤流动性差的票"
    ),
    "bottom_min_factor_score": (
        "14因子分二次过滤（软闸）：候选需满足 因子分 ≥ 此值才保留（100分制）。"
        "底反票因子分天然偏低（实测均值≈-30），此为「剔除仍在深度恶化的票」的软闸，而非「要求因子转多」；"
        "切勿设为正值（如15），否则候选几乎清空。设为 -100 表示关闭该过滤"
    ),
    "bottom_trend_filter": (
        "创业板趋势过滤：仅当创业板指（默认399006）站在其 MA 之上的上升通道日，才给出底部反转候选/开仓；"
        "下降通道日不出候选（避免下跌市接飞刀）。关闭则无论涨跌都出候选"
    ),
    "bottom_trend_ma": "趋势过滤所用均线周期（交易日）；默认60日",
    "bottom_trend_index": (
        "趋势过滤所用的指数代码（新浪/东财格式，如 sz399006=官方创业板指）；"
        "留空=退回「300/301 等权合成指数」口径（回测验证更优：+9.94% vs 399006 的 -28.86%）。"
        "填 sz399006 才支持盘中按实时指数动态重算（翻多即解锁候选），但回测表现较差，谨慎开启"
    ),
    "bottom_time_stop_days": (
        "主动离场-时间止损：持仓达到该天数（自然日）仍未启动跟踪止损（即从未盈利到 +trailing_pct），"
        "则主动平仓，避免震荡死钱长期占用资金。0=关闭（仅靠硬持有上限与止损/止盈离场）"
    ),
    "bottom_trailing_pct": (
        "主动离场-跟踪止损：买入后涨幅达到该比例即启动跟踪止损，此后止损线上移至「峰值×(1−该比例)」，"
        "价格自峰值回撤该比例即离场，用来锁定利润、不让盈利变亏损。0=关闭"
    ),
    # ---- 创业板底部反转选股口径 ----
    "bottom_start_pos_max": "起点位置上限",
    "bottom_dd20_min": "20日高点回撤下限",
    "bottom_turnover_min": "换手率下限(%)",
    "bottom_min_avg_amount": "20日均额下限",
    "bottom_min_factor_score": "14因子分软闸",
    "bottom_trend_filter": "趋势过滤开关",
    "bottom_trend_ma": "趋势均线周期",
    "bottom_trend_index": "趋势指数代码",
    "bottom_time_stop_days": "时间止损天数",
    "bottom_trailing_pct": "跟踪止损比例",
    # ---- 多板块扫描 / 统一宏观风险 ----
    "bottom_boards": (
        "多板块扫描白名单（逗号分隔的板块键）：cyb=创业板、sh_main=上证主板、sz_main=深证主板、"
        "star=科创板、bse=北交所。每只票按所属板块的指数做趋势过滤，不混为一谈。"
        "默认 cyb,sh_main,sz_main,star；北交所(bse)本地缺指数 parquet，默认不扫"
    ),
    "unified_risk_ma": (
        "统一宏观风险层计算「指数相对 MA 的下方偏离度」所用的均线周期（交易日）；默认60"
    ),
    "unified_risk_include_overseas": (
        "是否在统一宏观风险中纳入外围指数：美股标普500/纳指/道琼斯取前一日收盘；"
        "日经225/恒生/韩国KOSPI取当日盘中（因它们比 A 股早开盘）。关闭则只看国内大盘"
    ),
    "unified_risk_thr_weak": (
        "综合风险分 ≥ 此值进入 weak（弱势）档，仓位上限取 unified_risk_cap_weak（默认0.30）；低于此值为 calm 不降仓"
    ),
    "unified_risk_thr_shock": (
        "综合风险分 ≥ 此值进入 shock（震荡）档，仓位上限取 unified_risk_cap_shock（默认0.20）；只压仓位、买入照常，另给「慎开新仓」软提示"
    ),
    "unified_risk_thr_crisis": (
        "综合风险分 ≥ 此值进入 crisis（危机）档，仓位上限取 unified_risk_cap_crisis（默认0.10），且全暂停新开仓买入；卖出与做T买回不受影响"
    ),
    "unified_risk_cap_weak": (
        "weak 弱势档总仓位上限（0~1）；默认0.30。系统强制 weak≥shock≥crisis 单调，误设成反向会被自动收敛"
    ),
    "unified_risk_cap_shock": (
        "shock 震荡档总仓位上限（0~1）；默认0.20，必须不高于 weak 档。此档只压仓位、不全停买入"
    ),
    "unified_risk_cap_crisis": (
        "crisis 危机档总仓位上限（0~1）；默认0.10，为最严档。此档同时触发全暂停新开仓买入"
    ),
}


def _comment_texts() -> dict[str, str]:
    """从 ``config.py`` 源码里抽出「字段 → 注释全文」，作为设置页 tooltip 的说明。

    注释本来就是写给人看的一句话（甚至一段），比另维护一份描述表更不容易过期。
    取整个注释块（多行直接拼接），分节分隔线（``# ---------- 基础 ----------``）跳过。
    行尾注释优先——config.py 里做T/移动止损那批都是行尾一行说明。
    """
    texts: dict[str, str] = {}
    try:
        lines = (BASE_DIR / "backend" / "app" / "config.py").read_text(encoding="utf-8").splitlines()
    except OSError:
        return texts
    buf: list[str] = []
    for line in lines:
        comment = _COMMENT_RE.match(line)
        if comment:
            text = comment.group(1).strip()
            if text.startswith("-") or not set(text) - set("-= "):
                buf = []          # 分节分隔线，清空缓冲
                continue
            buf.append(text)
            continue
        field = _FIELD_RE.match(line)
        if field:
            key = field.group(1)
            inline = (field.group(3) or "").strip()
            text = inline or "".join(buf)
            if text:
                texts[key] = text
            buf = []
            continue
        if line.strip():
            buf = []
    return texts


def _short_label(key: str, desc: str) -> str:
    """短标签：先查手工表，没收录就截到第一个标点（完整句子在 tooltip 里）。"""
    if key in _SHORT_LABELS:
        return _SHORT_LABELS[key]
    cut = min([i for i in (desc.find(c) for c in _SHORT_CUT) if i > 0] or [len(desc)])
    return desc[:cut].strip() or key


def _placement_of(key: str) -> tuple[str, str]:
    """键落在哪个分组的哪个小节：先查 `_LAYOUT` 显式排布，未收录的按前缀兜底。"""
    if key in _PLACEMENT:
        return _PLACEMENT[key]
    for prefixes, group in _FALLBACK_GROUP_RULES:
        if key.startswith(prefixes):
            return group, "其他"
    return "其他", "其他"


def _env_keys() -> set[str]:
    """环境变量里显式设置过的大写键（含 `.env` 文件里的，pydantic 会读它）。"""
    keys = {k.upper() for k in os.environ}
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            keys.add(text.split("=", 1)[0].strip().upper())
    except OSError:
        pass
    return keys


def _jsonable(value: Any) -> Any:
    from pathlib import Path

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def kind_of(key: str, default: Any) -> str:
    """页面渲染用的控件类型：``bool`` 开关 / ``number`` 数字框 / ``text`` 文本框 / ``multiselect`` 多选 / ``readonly`` 只读。"""
    if key in READONLY_KEYS:
        return "readonly"
    if key in MULTISELECT_KEYS:
        return "multiselect"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, (int, float)):
        return "number"
    return "text"


def _coerce_value(key: str, raw: Any, default: Any) -> Any:
    """把前端/字符串输入转成字段该有的类型（类型不合法直接抛 ValueError）。"""
    if key in MULTISELECT_KEYS:
        # 多选值存为逗号分隔字符串；按声明顺序规范化、丢弃未知项（避免脏值污染白名单）
        raw_str = "" if raw is None else str(raw)
        parts = {p.strip() for p in raw_str.split(",")}
        return ",".join(p for p in MULTISELECT_KEYS[key] if p in parts)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on", "开", "是"):
            return True
        if text in ("0", "false", "no", "off", "关", "否", ""):
            return False
        raise ValueError(f"{key} 需要布尔值，收到 {raw!r}")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要整数，收到 {raw!r}") from exc
    if isinstance(default, float):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要数字，收到 {raw!r}") from exc
        if value != value:      # NaN
            raise ValueError(f"{key} 不合法（NaN）")
        return value
    return "" if raw is None else str(raw)


def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """批量修改配置：写 app_state 覆盖层 + **就地**改 ``get_settings()`` 单例。

    就地改单例是关键：几乎所有代码都持有 ``self.settings`` 这个对象引用，
    替换对象无效；改属性才能让「下一个 tick / 下一轮扫描」立刻用上新值。
    覆盖同时落库，重启后由 :func:`apply_persisted_overrides` 重新套用。

    抛 ``ValueError`` 表示该键不可改或值类型不合法，由 API 层转 400。
    """
    from app.data.cache import db

    s = get_settings()

    # 第一阶段：全部转换 + 校验。校验不过就整批拒绝，避免"第 N 项非法、前 N-1 项已落库"
    pending: dict[str, Any] = {}
    for key, raw in changes.items():
        field = Settings.model_fields.get(key)
        if field is None:
            raise ValueError(f"未知参数: {key}")
        if key in READONLY_KEYS:
            raise ValueError(f"{key} 不支持页面修改（路径/列表类，请改 .env 后重启）")
        # 密钥类：前端拿到的是掩码，原样回传说明用户没改，跳过（写库会毁掉真值）
        if key in SECRET_KEYS and isinstance(raw, str) and MASK_MARK in raw:
            continue
        value = _coerce_value(key, raw, field.default)
        floor = _MIN_EXCLUSIVE.get(key)
        if floor is not None and isinstance(value, (int, float)) and value <= floor:
            raise ValueError(f"{key} 需大于 {floor}")
        if key in _NON_NEGATIVE and isinstance(value, (int, float)) and value < 0:
            raise ValueError(f"{key} 不能为负")
        if key in _MAX_ONE and isinstance(value, (int, float)) and value > 1:
            raise ValueError(f"{key} 需 ≤ 1（比例，0 表示弱市全暂停买入）")
        pending[key] = value

    # 第二阶段：跨字段校验（用「现值 + 本批新值」合并后判断）
    for (high, low), msg in _CROSS_RULES:
        if high not in pending and low not in pending:
            continue
        hi = pending.get(high, getattr(s, high, None))
        lo = pending.get(low, getattr(s, low, None))
        if hi is not None and lo is not None and float(hi) <= float(lo):
            raise ValueError(msg)

    # 第三阶段：写覆盖层 + 就地改单例（到这里不会再失败）
    applied: dict[str, Any] = {}
    for key, value in pending.items():
        target = key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key
        db.set_state(target, "1" if value is True else "0" if value is False else str(value))
        setattr(s, key, value)
        applied[key] = value
    if applied:
        logger.info("系统设置已更新: %s", ", ".join(sorted(applied)))
    return applied


def reset_settings(keys: list[str] | None = None) -> list[str]:
    """还原为 `.env` / 环境变量的值：删覆盖 + 用新建的 ``Settings()`` 回填。

    ``keys`` 为空表示还原全部可编辑项。
    """
    from app.data.cache import db

    s = get_settings()
    fresh = Settings()
    targets = list(keys) if keys else [
        k for k in Settings.model_fields if k not in READONLY_KEYS
    ]
    done: list[str] = []
    for key in targets:
        if key not in Settings.model_fields or key in READONLY_KEYS:
            continue
        try:
            db.delete_state(key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("删除覆盖失败 %s: %s", key, exc)
        setattr(s, key, getattr(fresh, key))
        done.append(key)
    if done:
        logger.info("系统设置已还原 %d 项", len(done))
    return done


def apply_persisted_overrides() -> int:
    """启动时把 app_state 里的覆盖重新套到 Settings 单例上（否则重启即丢失）。"""
    from app.data.cache import db

    s = get_settings()
    applied = 0
    for key, field in Settings.model_fields.items():
        if key in READONLY_KEYS:
            continue
        try:
            raw = db.get_state(key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取覆盖失败 %s: %s", key, exc)
            continue
        if raw is None or raw == "":
            continue
        try:
            setattr(s, key, _coerce_value(key, raw, field.default))
            applied += 1
        except ValueError:
            logger.warning("忽略非法配置覆盖 %s=%r", key, raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("应用配置覆盖失败 %s: %s", key, exc)
    if applied:
        logger.info("已应用 %d 项运行时配置覆盖（来自 app_state）", applied)
    return applied


def _item_for(key: str, texts: dict, env_keys: set) -> dict | None:
    """构造单个参数在设置页渲染所需的元数据 dict。

    系统设置页（`settings_overview`）与选股策略面板（`selection_settings_overview`）
    共用同一套字段读取逻辑：当前生效值 → 来源判定（app_state > env > default）
    → 控件类型 → 短名/说明/可选项。任何一处改了字段处理，两边一起生效。
    """
    field = Settings.model_fields.get(key)
    if field is None:
        return None
    s = get_settings()
    default = field.default
    value = getattr(s, key, default)
    source = "default"
    # 覆盖层的键名有两套：RUNTIME_KEYS 沿用既有无前缀键（engine 直接读它们），
    # 其余走 `cfg:` 前缀，避免与业务自定义状态键撞名
    state_key = key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key
    from app.data.cache import db

    try:
        raw = db.get_state(state_key)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is not None and raw != "":
        try:
            value = _coerce_value(key, raw, default)
            source = "app_state"
        except ValueError:
            logger.warning("配置项 %s 的覆盖值非法: %r", key, raw)
    if source == "default" and key.upper() in env_keys:
        source = "env"
    runtime = key in RUNTIME_KEYS
    kind = kind_of(key, default)
    item = {
        "key": key,
        # 短名进卡片，整句说明进 tooltip（前端 `tipOf`）
        "label": _short_label(key, texts.get(key, "")),
        "desc": texts.get(key) or _DESC_OVERRIDES.get(key, ""),
        # 密钥/机器人地址只回显掩码；前端原样回传掩码时 `update_settings` 视为未改动
        "value": mask_secret(value) if key in SECRET_KEYS else _jsonable(value),
        "type": "bool" if isinstance(default, bool)
        else "int" if isinstance(default, int)
        else "float" if isinstance(default, float)
        else "str",
        "source": source,
        # 能落到这里的都可编辑：写 app_state 覆盖层 + 就地生效，重启由启动流程重放
        "restart_required": False,
        "writable": True,
        "kind": kind,
        "runtime": runtime,
        "note": _NOTE_OVERRIDES.get(key, ""),
    }
    if key in SECRET_KEYS:
        # 前端据此渲染「已设置（掩码）」提示与 password 输入框
        item["secret"] = True
    if key in _DEPENDS_ON:
        # 条件可见：父开关（或空的文本父项）不满足时，前端把这一行藏起来
        item["depends_on"] = _DEPENDS_ON[key]
    if key in MULTISELECT_KEYS:
        # 多选控件的可选项列表（前端据此渲染 chip），顺序同 MULTISELECT_KEYS 声明
        item["options"] = list(MULTISELECT_KEYS[key])
    return item


def selection_settings_overview() -> dict:
    """「选股策略」页（全市场选股）专用的参数总览：只返回与**选股**相关的参数，
    分三块（选股板块 / 选股与自选 / 底部反转选股）。这些参数已从系统设置页隐藏，
    但仍可经 `update_settings` 正常读写。结构与 `settings_overview` 一致，前端可直接复用渲染。

    注：`unified_risk_*`（统一宏观风险 + 四档压仓）属风控层、非选股口径，仍留在系统设置
    （「风控与止损 → 大盘择时」），不在此出现。
    """
    texts = _comment_texts()
    env_keys = _env_keys()
    sections = (
        (
            "选股板块",
            "选股与交易共用的统一板块白名单（一处改，两边生效）",
            "板块白名单",
            "可勾选主板/创业板/科创板/北交所；留空退回主板+创业板",
            ("twin_t_boards",),
        ),
        (
            "选股与自选",
            "评分判级、盘后自选与扫描范围",
            "选股与自选",
            "评分阈值、打分引擎、盘后自选/关注、盘后选股范围、盘中分析间隔",
            (
                "factor_buy_threshold", "factor_sell_threshold", "live_ml_scoring",
                "auto_watch_enabled", "auto_watch_top_n", "focus_top_n",
                "auto_scan_scope", "intraday_analysis_interval",
            ),
        ),
        (
            "底部反转选股",
            "实盘底部反转选股面板（与回测同口径）：低位 + 回撤 + 活跃成交 + 趋势过滤",
            "底部反转",
            "选股口径、趋势过滤、多板块缩放、入链、主动离场",
            (
                "bottom_start_pos_max", "bottom_dd20_min", "bottom_turnover_min",
                "bottom_min_avg_amount", "bottom_min_factor_score",
                "bottom_trend_filter", "bottom_trend_ma", "bottom_trend_index",
                "bottom_scale_exit_by_limit", "bottom_reversal_autobridge",
                "bottom_time_stop_days", "bottom_trailing_pct",
            ),
        ),
    )
    groups: list[dict] = []
    total = 0
    runtime_count = 0
    for gname, gdesc, subname, subdesc, keys in sections:
        items: list[dict] = []
        for k in keys:
            it = _item_for(k, texts, env_keys)
            if it is None or it["kind"] == "readonly":
                continue
            items.append(it)
            if it["source"] == "app_state":
                runtime_count += 1
        if not items:
            continue
        total += len(items)
        groups.append({
            "name": gname,
            "desc": gdesc,
            "items": items,
            "subgroups": [{"name": subname, "desc": subdesc, "items": items}],
        })
    return {
        "groups": groups,
        "total": total,
        "runtime_overrides": runtime_count,
        "hint": "以下参数属于选股策略：改动即时落库生效（重启保留）；风险压仓类仍在系统设置。",
    }


def settings_overview() -> dict:
    """当前**生效值**总览：分组 → 组内小节 → 全部参数（含来源与生效方式）。

    返回结构（前端左栏导航 + 右栏小节卡片直接消费）::

        {"groups": [{"name", "desc", "items", "subgroups": [{"name", "desc", "items"}]}],
         "total", "runtime_overrides", "hint"}

    `items` 是该组全部参数的扁平串（按小节顺序拼接），保留给搜索与计数；
    `subgroups` 才是渲染用的语义切分。
    """
    s = get_settings()
    texts = _comment_texts()
    env_keys = _env_keys()
    fields = Settings.model_fields
    from app.data.cache import db

    # 分组 → 小节 → 参数
    buckets: dict[str, dict[str, list[dict]]] = {}
    for key in fields:
        # 选股策略相关参数已从系统设置页隐藏（仍可在选股策略面板编辑）
        if key in SETTINGS_SYSTEM_HIDDEN:
            continue
        item = _item_for(key, texts, env_keys)
        if item is None:
            continue
        # 只读项（路径/列表类）与硬编码常量直接不进清单：它们改不了
        # （前者要改 .env 重启，后者要改代码），展示了也没用，只会让人以为能调。
        if item["kind"] != "readonly":
            group, sub = _placement_of(key)
            buckets.setdefault(group, {}).setdefault(sub, []).append(item)

    # 组顺序 = `_LAYOUT` 声明顺序；未声明的分组（新字段兜底产生的「其他」）排最后
    group_order = [n for n in GROUP_ORDER if n in buckets]
    group_order += [n for n in buckets if n not in GROUP_ORDER]
    # 小节顺序同理：`_LAYOUT` 里该组的声明顺序优先，兜底的「其他」排到末尾
    layout_subs = {
        g[0]: [sub[0] for sub in g[2]] for g in _LAYOUT
    }

    ordered = []
    for name in group_order:
        if name in _HIDDEN_GROUPS:
            # 整组不在设置页渲染（字段本身保留，业务逻辑照常读取）
            continue
        subs = buckets[name]
        sub_order = [sub for sub in layout_subs.get(name, []) if sub in subs]
        sub_order += [sub for sub in subs if sub not in sub_order]
        subgroups = [
            {
                "name": sub,
                "desc": SUBGROUP_DESCS.get((name, sub), ""),
                "items": subs[sub],
            }
            for sub in sub_order
        ]
        ordered.append({
            "name": name,
            "desc": GROUP_DESCS.get(name, ""),
            # 扁平串保留给搜索 / 计数（按小节顺序拼）
            "items": [it for sg in subgroups for it in sg["items"]],
            "subgroups": subgroups,
        })
    total = sum(len(g["items"]) for g in ordered)
    runtime_count = sum(
        1 for g in ordered for i in g["items"] if i["source"] == "app_state"
    )
    return {
        "groups": ordered,
        "total": total,
        "runtime_overrides": runtime_count,
        # 这行要**尽量排成一行**（前端给了 ~860px 宽度）：超过就换行，所以文案控制在
        # 60 个汉字以内，别往里加新句子，长说明放 tooltip 或文档。
        "hint": "当前值 = app_state 覆盖 > .env > 默认值；改动即时落库生效（重启保留）；"
                "路径/列表类（DB/K线/CORS）需改 .env 后重启。",
    }
