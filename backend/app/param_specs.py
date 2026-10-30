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
})

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
            ("请求与直连", "网络层开关与单次请求超时", ("data_direct_only", "request_timeout")),
            (
                "扫描并发",
                "全市场扫描的并发度与等待上限",
                ("scan_concurrency", "scan_wait_timeout", "scan_auto_watch_limit"),
            ),
            ("抓取与缓存", "失败重试与快照缓存时长", ("fetch_retries", "snapshot_ttl")),
            ("K 线预热", "历史 K 线的预热范围与并发", ("kline_warmup_concurrency", "kline_warmup_top_n")),
        ),
    ),
    (
        "选股与自选",
        "评分判级、收盘选股、自选与影子策略",
        (
            ("评分阈值", "决定 buy / sell 判级的分数线", ("factor_buy_threshold", "factor_sell_threshold")),
            (
                "自选与关注",
                "收盘后自动加自选与明日关注的数量",
                ("auto_watch_enabled", "auto_watch_top_n", "focus_top_n"),
            ),
            ("盘后选股", "收盘扫描的股票池范围", ("auto_scan_scope",)),
            (
                "影子策略",
                "只记录不交易的对照实验",
                ("shadow_start", "shadow_tag", "shadow_universe", "shadow_profiles"),
            ),
            ("盘中分析", "盘中分析的触发间隔", ("intraday_analysis_interval",)),
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
                "先剔掉不该买的票，再看信号",
                ("paper_exclude_bj", "paper_exclude_large_cap", "paper_large_cap_yi"),
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
                "用更高分的票替换最弱持仓",
                (
                    "paper_rotation_enabled",
                    "paper_rotation_min_score",
                    "paper_rotation_gap",
                    "paper_rotation_max_per_day",
                ),
            ),
            ("候补队列", "满仓时把高分票先存着", ("paper_backlog_enabled", "paper_backlog_max")),
        ),
    ),
    (
        "风控与止损",
        "切入节奏、固定止损止盈与移动保护",
        (
            (
                "买点切入",
                "不追高、等回踩、超时放弃",
                ("paper_entry_max_chase", "paper_entry_pullback", "paper_entry_timeout_minutes"),
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
            ("大盘择时", "指数在均线下方时收缩买入", ("paper_regime_ma", "paper_regime_weak_pause")),
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
)

#: 分组展示顺序（只列**可编辑**的组；只读的部署项与硬编码常量单独返回，
#: 不混进参数列表——页面上能改的才叫"系统设置"，改不了的塞进去只会误导）
GROUP_ORDER: tuple[str, ...] = tuple(g[0] for g in _LAYOUT)

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
})

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
}

#: 不允许为负的数值项（费率类：负费率会让回测/实盘 PnL 凭空变好）
_NON_NEGATIVE: frozenset[str] = frozenset({
    "trade_fee_commission_rate",
    "trade_fee_commission_min",
    "trade_fee_stamp_rate",
    "trade_fee_transfer_rate",
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
    "kline_warmup_top_n": "预热股票数",
    "request_timeout": "请求超时",
    "snapshot_ttl": "快照缓存时长",
    "scan_wait_timeout": "扫描等待上限",
    "scan_auto_watch_limit": "扫描入池上限",
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
    # ---- 轮动 / 候补 ----
    "paper_rotation_enabled": "轮动替换",
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
    # ---- 股票池过滤 ----
    "paper_pool_top_n": "成交额池 TopN",
    "paper_exclude_bj": "排除北交所",
    "paper_exclude_large_cap": "大盘股过滤",
    "paper_large_cap_yi": "大盘股阈值",
    # ---- 止损 / 止盈 ----
    "paper_stop_loss": "固定止损",
    "paper_take_profit": "固定止盈",
    "paper_single_position_max": "单票上限",
    "paper_atr_stop_mult": "ATR 止损倍数",
    "paper_atr_stop_max": "ATR 止损上限",
    "paper_breakeven_pct": "保本触发",
    "paper_trail_pct": "移动止损回撤",
    "paper_trail_arm": "移动止损启用",
    # ---- 大盘择时 ----
    "paper_regime_ma": "择时均线",
    "paper_regime_weak_pause": "弱市暂停买入",
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
    "intraday_analysis_interval": "盘中分析间隔",
}

#: 「首句截断」用的分隔符（没进 `_SHORT_LABELS` 的键走这条路）
_SHORT_CUT = "：:，,。；;（(【[/、 "

#: 少数字段在 config.py 里没有注释（或只有标签），补一句说明给 tooltip 用
_DESC_OVERRIDES: dict[str, str] = {
    "auto_watch_top_n": "收盘选股后按评分取前 N 只自动加入自选",
    "paper_half_score": "评分介于半额线与全额线之间时，仓位权重打 5 折",
    "paper_scan_autotrade_top_n": "扫描命中后最多自动送多少只进交易队列",
    "paper_rotation_gap": "新票评分需高出最弱持仓的建仓评分多少分才触发轮动",
    "paper_backlog_max": "候补队列最多存多少只高分票（有名额时按评分优先买入）",
    "paper_large_cap_yi": "总市值超过该值（亿元）视为大盘股，默认不进买入队列",
    "paper_atr_stop_max": "ATR 止损放宽的上限（0.20 = 最多放宽到 20%）",
    "paper_t_active": "做T引擎总开关；关闭后下面各项做T参数不生效",
    "llm_base_url": "OpenAI 兼容接口地址（如 https://api.openai.com/v1）",
    "llm_api_key": "接口密钥；未配置时自动降级为纯技术面结论",
    "llm_model": "模型名，如 gpt-4o-mini / qwen-plus",
    "llm_timeout": "单次请求超时（秒）",
    "shadow_tag": "影子策略落库时用的标签，便于区分多组实验",
    "shadow_universe": "影子策略使用的股票池规模（成交额 TopN）",
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
    """页面渲染用的控件类型：``bool`` 开关 / ``number`` 数字框 / ``text`` 文本框 / ``readonly`` 只读。"""
    if key in READONLY_KEYS:
        return "readonly"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, (int, float)):
        return "number"
    return "text"


def _coerce_value(key: str, raw: Any, default: Any) -> Any:
    """把前端/字符串输入转成字段该有的类型（类型不合法直接抛 ValueError）。"""
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
        value = _coerce_value(key, raw, field.default)
        floor = _MIN_EXCLUSIVE.get(key)
        if floor is not None and isinstance(value, (int, float)) and value <= floor:
            raise ValueError(f"{key} 需大于 {floor}")
        if key in _NON_NEGATIVE and isinstance(value, (int, float)) and value < 0:
            raise ValueError(f"{key} 不能为负")
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
    for key, field in fields.items():
        default = field.default
        value = getattr(s, key, default)
        source = "default"
        # 覆盖层的键名有两套：RUNTIME_KEYS 沿用既有无前缀键（engine 直接读它们），
        # 其余走 `cfg:` 前缀，避免与业务自定义状态键撞名
        state_key = key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key
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
            "value": _jsonable(value),
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
        # 只读项（路径/列表类）与硬编码常量直接不进清单：它们改不了
        # （前者要改 .env 重启，后者要改代码），展示了也没用，只会让人以为能调。
        if kind != "readonly":
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
        "hint": "当前值 = app_state 覆盖 > 环境变量/.env > 默认值。"
                "改动即时生效并落库（重启保留）。"
                "路径/列表类（DB 文件、K 线目录、CORS 等）不进本页，请改 .env 后重启。",
    }
