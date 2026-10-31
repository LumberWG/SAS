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

from app.config import get_settings

#: 回测参数键 → `Settings` 字段。只有「键名语义一致」的项才在这里。
BACKTEST_TO_SETTING: dict[str, str] = {
    # 基础
    "max_positions": "paper_max_positions",
    "universe_size": "paper_pool_top_n",
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
    # 五段式③④⑤：队列容量与切入点
    "queue_top_m": "paper_trade_queue_top_m",
    "entry_max_chase": "paper_entry_max_chase",
    "entry_pullback": "paper_entry_pullback",
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
}

#: 明确不落盘的项 + 原因（前端在落盘结果里逐条展示，别让用户以为「都同步了」）。
SKIP_REASONS: dict[str, str] = {
    # ---- 账户级 ----
    "initial_capital": "初始资金只在开户时生效，已有模拟账户不会被重置",
    # ---- 选股侧（回测专属） ----
    "board_prefixes": "实盘只支持「排除北交所」开关（paper_exclude_bj），不支持任意板块组合",
    "watch_cap": "实盘自选没有上限/FIFO 淘汰，无对应配置",
    "add_per_day": "实盘自选由「扫描 → 分析」链路写入，没有每日配额",
    "rebalance_days": "实盘按每日调仓，没有调仓周期开关",
    "buy_top_pct": "实盘选股没有分位门槛，只有评分分档（full_score/half_score）",
    "max_open_gain": "与「切入点 · 不追高」同口径，落盘请用 entry_max_chase",
    "max_gain_5d": "实盘选股没有近 5 日涨幅过滤",
    "min_volume_ratio": "实盘选股没有量比下限过滤",
    "min_turnover_rate": "实盘选股没有换手率下限过滤",
    "invert_score": "反转选股只在回测里验证，实盘仍按 14 因子正向排序",
    # ---- 卖出侧（回测专属） ----
    "max_hold_days": "实盘没有时间止损（不按持仓天数强制卖出）",
    "stop_atr_min": "实盘 ATR 止损的下限就是固定止损（paper_stop_loss），没有独立下限",
    "stop_confirm_close": "实盘逐 tick 撮合，没有「收盘确认」这一档",
    "partial_tp_at": "实盘没有分段止盈",
    "partial_tp_ratio": "实盘没有分段止盈",
    # ---- 择时/仓位分档 ----
    "regime_fast_ma": "实盘择时只用单条均线（paper_regime_ma）",
    "weak_exposure": "实盘弱市只暂停买入（paper_regime_weak_pause），不按档位降仓",
    "regime_flat_below": "实盘没有分档清仓（同 weak_exposure）",
    "regime_ramp_days": "实盘没有「弱转强爬坡」（BACKTEST_LOG #64 未采纳，回测默认 0）",
    "t_minute_path": "实盘 t_tick 本身就是逐 tick 推进（等价于 True），无需落盘",
    "t_minute_period": "实盘用实时报价，无「分钟周期」概念；回测专用的采样口径（记录 #66）",
    "exec_fill": "实盘开盘窗口本来就按实时价成交，不存在「开盘价 vs VWAP」的度量选择（记录 #67）",
    "exec_fill_minutes": "同 exec_fill：回测度量口径，实盘无对应配置",
    # ---- 切入点 ----
    "entry_window_minutes": "实盘是「开盘后 N 分钟内成交」（paper_buy_open_window_minutes），不是等回踩窗口",
    # ---- 打分层（BACKTEST_LOG 记录 #74~#78）----
    "ml_model": "实盘 live_ml 固定走 lightgbm + 行业中性（与 default_ml 同口径）；换模型只在回测里比较",
    "ml_label_horizon": "实盘标签前瞻固定 15 个交易日（与策略中位持仓对齐）",
    "ml_blend": "实盘只用纯 ML 分位排序，没有「与 14 因子加权融合」这一档",
    "ml_require_ic": "实盘 OOS IC≤0 时自动回退 14 因子，门槛不可调",
    "industry_neutral": "实盘固定做行业中性（live_ml 内写死），回测中心无法关闭",
    "disable_limit_mask": "仅用于量化涨跌停 mask 的贡献（记录 #73），线上永远开启约束",
}

#: ``scoring_mode`` 的取值 → 实盘开关 ``live_ml_scoring``。
#: 回测的三种口径里只有「是否用 ML 排序」能落盘，融合比例与模型选择都是回测专属。
ML_SCORING_MODES: frozenset[str] = frozenset({"ml", "ml_blend"})


def split_changes(changes: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把一批回测参数拆成「可落盘的 Settings 变更」+「跳过项与原因」。"""
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
