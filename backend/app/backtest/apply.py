"""回测参数 → 实盘配置（选股/模拟盘）的落盘映射。

回测中心的两件事要分清楚：

1. 改参数 + 「开始回测」——只影响**本次回测**，不碰实盘（`PipelineParams` 覆盖）；
2. 点「将规则落盘保存到数据库」——把这套规则写进 `Settings` 覆盖层
   （`app_state`，重启保留），下一轮扫描 / 下一个 tick 就生效，并且成为新的默认值。

但两套参数**并非一一对应**：有些是回测专属的实验开关（分位门槛、反转选股、调仓周期、
分段止盈、分钟级切入点），实盘根本没有对应实现；硬把值塞进某个不相干的字段，就会出现
「页面显示改了、实盘没变」的错觉（WORKLOG 2026-10-30 记的就是这类 bug）。
所以这里只映射**实盘真有落点**的子集，其余逐项列出跳过原因，前端原样展示。
"""
from __future__ import annotations

from typing import Any

from app.board_rules import BOARD_ZH
from app.config import get_settings

#: 回测参数键 → `Settings` 字段。只有「键名语义一致」的项才在这里。
#: 维护约定：本表每一项都必须同时登记在 `app.backtest.alignment.BINDINGS` 里
#: （回测默认值从同一个 Settings 字段读），否则会形成「默认从 A 读、落盘写到 B」
#: 的双向漂移；`scripts/check_backtest_alignment.py` 会强制检查这一点。
BACKTEST_TO_SETTING: dict[str, str] = {
    # 基础
    "max_positions": "paper_max_positions",
    "universe_size": "paper_pool_top_n",
    # 板块 / 自选池：与回测默认值同源于 twin_t_boards / watch_auto_max / scan_auto_watch_limit，
    # 落盘时由 split_changes 做值折算（板块键数组 → 中文名逗号串；6000 → 0）
    "board_prefixes": "twin_t_boards",
    "watch_cap": "watch_auto_max",
    "add_per_day": "scan_auto_watch_limit",
    # 单票/总仓位
    "max_total_exposure": "paper_max_total_exposure",
    "single_position_ratio": "paper_single_position_max",
    # 评分阈值与分档
    "buy_threshold": "factor_buy_threshold",
    "full_score": "paper_full_score",
    "half_score": "paper_half_score",
    # 止损 / 止盈
    "stop_loss": "paper_stop_loss",
    "take_profit": "paper_take_profit",
    "stop_atr_mult": "paper_atr_stop_mult",
    "stop_atr_max": "paper_atr_stop_max",
    "breakeven_trigger": "paper_breakeven_pct",
    "trail_pct": "paper_trail_pct",
    "trail_arm": "paper_trail_arm",
    # 大盘择时
    "regime_ma": "paper_regime_ma",
    # 轮动
    "rotation_enabled": "paper_rotation_enabled",
    "rotation_gap": "paper_rotation_gap",
    "rotation_min_score": "paper_rotation_min_score",
    "rotation_max_per_day": "paper_rotation_max_per_day",
    # 五段式③④⑤：队列容量与切入点
    "queue_top_m": "paper_trade_queue_top_m",
    "entry_max_chase": "paper_entry_max_chase",
    "entry_pullback": "paper_entry_pullback",
    "entry_window_minutes": "paper_entry_timeout_minutes",
    # 弱市降仓：实盘 engine 明写「总仓位上限压到 paper_weak_exposure，与回测 day_exposure 同口径」
    "weak_exposure": "paper_weak_exposure",
    # #108：回测「开盘+N 分钟入场价」与实盘「开盘冷静期」是同一口径，改这里才会同步生效
    "entry_open_delay_minutes": "paper_open_cooldown_minutes",
    # 做T引擎
    "t_active": "paper_t_active",
    "t_size": "paper_t_size",
    "t_up_sell": "paper_t_up_sell",
    "t_up_buyback": "paper_t_up_buyback",
    "t_down_add": "paper_t_down_add",
    "t_down_full": "paper_t_down_full",
    "t_rebound_sell": "paper_t_rebound_sell",
    "t_limit_sell_half": "paper_t_limit_sell_half",
    "t_crash_guard": "paper_t_crash_guard",
    # P1 上下文风险闸：6 项都是同语义数值，无需值折算；行为由共用函数保证，
    # 参数在这里双向同步（回测页「落盘」写回实盘并成为新默认值）。
    "context_enabled": "paper_ctx_enabled",
    "context_max_industry_weight": "paper_ctx_max_industry_weight",
    "context_max_corr": "paper_ctx_max_corr",
    "context_hot_exclude_pct": "paper_ctx_hot_exclude_pct",
    "context_event_cooldown_days": "paper_ctx_event_cooldown_days",
    "context_news_block": "paper_ctx_news_block",
    # ML 行业中性化：回测页落盘写回实盘，默认值也由同一字段提供（alignment 探针保证同源）
    "industry_neutral": "live_ml_industry_neutral",
}

#: 明确不落盘的项 + 原因（前端在落盘结果里逐条展示，别让用户以为「都同步了」）。
SKIP_REASONS: dict[str, str] = {
    # ---- 账户级 ----
    "initial_capital": "初始资金只在开户时生效，已有模拟账户不会被重置",
    # ---- 选股侧（回测专属；板块/自选容量/日增上限已改为双向同步，不再列在这里） ----
    # 策略集是「默认严格跟随」但**不落盘**：实盘 auto_scan_strategies 属 READONLY_KEYS，
    # 由 .env 的 AUTO_SCAN_STRATEGIES 决定（当前 A-Only 护栏），刻意不允许页面一键改成
    # 14 策略全开。回测默认值已通过 BINDINGS 逐项探针绑定，改 .env 重启后自动同步。
    "strategies": "实盘策略集由 .env 的 AUTO_SCAN_STRATEGIES 只读（A-Only 护栏），需改 .env 后重启；回测默认值已严格跟随",
    "rebalance_days": "实盘按每日调仓，没有调仓周期开关",
    "buy_top_pct": "实盘选股没有分位门槛，只有评分分档（full_score/half_score）",
    "max_open_gain": "与「切入点 · 不追高」同口径，落盘请用 entry_max_chase",
    "max_gain_5d": "实盘选股没有近 5 日涨幅过滤",
    "min_volume_ratio": "实盘选股没有量比下限过滤",
    "min_turnover_rate": "实盘选股没有换手率下限过滤",
    "invert_score": "反转选股只在回测里验证，实盘仍按 14 因子正向排序",
    # ---- 卖出侧（回测专属） ----
    "max_hold_days": (
        "实盘已实现时间止损（#191 / paper_max_hold_days），但实盘按自然日、回测按交易日，"
        "落盘会双向漂移；回测默认值由 _defaults_payload 按 5/7 单向换算跟随，不做反向落盘"
    ),
    "stop_atr_min": "与 stop_loss 同源（实盘下限即 paper_stop_loss），落盘请用 stop_loss，避免重复写同一字段",
    "stop_confirm_close": "实盘逐 tick 撮合，没有「收盘确认」这一档",
    "partial_tp_at": "实盘没有分段止盈",
    "partial_tp_ratio": "实盘没有分段止盈",
    # ---- 择时/仓位分档（weak_exposure 已双向同步，不再列在这里）----
    "regime_fast_ma": "实盘择时只用单条均线（paper_regime_ma）",
    "regime_flat_below": "实盘没有分档清仓（同 weak_exposure）",
    "regime_ramp_days": "实盘没有「弱转强爬坡」（BACKTEST_LOG #64 未采纳，回测默认 0）",
    "t_minute_path": "实盘 t_tick 本身就是逐 tick 推进（等价于 True），无需落盘",
    "t_minute_period": "实盘用实时报价，无「分钟周期」概念；回测专用的采样口径（记录 #66）",
    "exec_fill": "实盘开盘窗口本来就按实时价成交，不存在「开盘价 vs VWAP」的度量选择（记录 #67）",
    "exec_fill_minutes": "同 exec_fill：回测度量口径，实盘无对应配置",
    # ---- 打分层（BACKTEST_LOG 记录 #74~#78）----
    "ml_model": "实盘 live_ml 固定走 lightgbm；换模型只在回测里比较",
    "ml_label_horizon": "实盘标签前瞻固定 15 个交易日（与策略中位持仓对齐）",
    "ml_blend": "实盘只用纯 ML 分位排序，没有「与 14 因子加权融合」这一档",
    "ml_require_ic": "实盘 OOS IC≤0 时自动回退 14 因子，门槛不可调",
    "disable_limit_mask": "仅用于量化涨跌停 mask 的贡献（记录 #73），线上永远开启约束",
}

#: ``scoring_mode`` 的取值 → 实盘开关 ``live_ml_scoring``。
#: 回测的三种口径里只有「是否用 ML 排序」能落盘，融合比例与模型选择都是回测专属。
ML_SCORING_MODES: frozenset[str] = frozenset({"ml", "ml_blend"})


def _board_keys_to_boards(value: Any) -> str:
    """回测板块键数组 → 实盘 ``twin_t_boards`` 的中文名逗号串。

    空数组（回测「全市场」）折算成四个板块全选——实盘没有「不限板块」这个选项，
    全选即等价全市场，这样双向同步才闭合，不会把实盘板块清空成无票可交易。
    """
    keys = [str(k).strip() for k in value] if isinstance(value, (list, tuple)) else []
    names: list[str] = []
    for key in keys:
        zh = BOARD_ZH.get(key)
        if zh and zh not in names:
            names.append(zh)
    if not names:  # 空数组 / 全市场，或未识别的键
        names = list(dict.fromkeys(BOARD_ZH.values()))
    return ",".join(names)


def split_changes(changes: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把一批回测参数拆成「可落盘的 Settings 变更」+「跳过项与原因」。

    有几项的值形态两侧不同，落盘前必须折算，否则会把数组/哨兵值直接写进设置：
    - ``board_prefixes``：板块键数组 → 中文名逗号串（空=全市场 → 四板块全选）
    - ``universe_size``：6000 是回测「不限池」的合法上限，实盘对应 0=不限制
    - ``scoring_mode``：三选一字符串 → ``live_ml_scoring`` 布尔
    """
    applied: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    for key in sorted(changes):
        # 打分层：回测是三选一的字符串，实盘只有一个布尔开关——按「是否用 ML 排序」折算
        if key == "scoring_mode":
            applied["live_ml_scoring"] = str(changes[key]) in ML_SCORING_MODES
            continue
        target = BACKTEST_TO_SETTING.get(key)
        if target is None:
            skipped.append({"key": key, "reason": SKIP_REASONS.get(key, "实盘没有对应配置项")})
            continue
        if key == "board_prefixes":
            applied[target] = _board_keys_to_boards(changes[key])
            continue
        if key == "universe_size":
            n = int(changes[key] or 0)
            applied[target] = 0 if n >= 6000 else n
            continue
        applied[target] = changes[key]
    return applied, skipped


def persist_map() -> dict[str, Any]:
    """落盘映射 + 当前实盘生效值（前端用它算「会改哪几项」的差异）。

    ``scoring_mode`` 也列进来：它是字符串三选一、落盘时折算成 ``live_ml_scoring``，
    前端要靠这条映射才知道「改打分层会同步到实盘哪一项」（见 ``SELECT_TO_SWITCH``）。
    """
    s = get_settings()
    mapping = {**BACKTEST_TO_SETTING, "scoring_mode": "live_ml_scoring"}
    current = {}
    for target in set(mapping.values()):
        value = getattr(s, target, None)
        current[target] = value if isinstance(value, (bool, int, float, str)) else str(value)
    return {
        "mapping": mapping,
        "skipped": dict(SKIP_REASONS),
        "current": current,
    }
