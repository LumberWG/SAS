"""回测参数方案（profile）：只用于回归测试，**不改变线上配置**。

用法：``scripts/regression.py --profiles default,th55`` 跑矩阵对比。

约定：
  - ``default`` 必须始终是线上参数（不写任何 overrides），作为对照基准；
  - 每个新增方案都要在 ``BACKTEST_LOG.md`` 有对应记录（假设、结果、是否采纳）；
  - 这里的值一旦被线上采纳，要同步改 ``app/config.py`` / ``app/analysis/factors.py``，
    并在 ``WORKLOG.md`` 变更记录里登记。
  - 未列出的键沿用 ``PipelineParams`` 默认值（即线上配置）。
"""
from app.analysis import factors


def _w(**group_mult: float) -> dict[str, float]:
    """按「因子分组」倍数生成 14 因子权重（组内维持原比例）。

    例：``_w(趋势=1.5)`` = 趋势组权重 ×1.5，其余组不变，
    总和在 ``factors.reweight`` 里再归一化到 100，保证与买入阈值同分制。
    """
    return {
        key: round(weight * group_mult.get(factors.FACTOR_LABELS[key][1], 1.0), 2)
        for key, weight in factors.FACTOR_WEIGHTS.items()
    }


#: 方案名 -> PipelineParams 字段覆盖
PROFILES: dict[str, dict] = {
    # 线上默认：阈值 15、止损 8%、满仓 10 只、单票 20%、总仓位 80%
    "default": {},
    # 只买建仓分上半区（本轮成交分中位数约 55）
    "th55": {"buy_threshold": 55},
    # 放宽止损（E3 实验，已知短窗口更差，保留用于复核）
    "sl10": {"stop_loss": 0.10},
    # 阈值 + 止损组合，验证是否互相抵消
    "th55_sl10": {"buy_threshold": 55, "stop_loss": 0.10},
    # 时间止损：持有 3 个交易日后次日开盘离场
    "hold3": {"max_hold_days": 3},
    # 集中持仓 + 降敞口：5 只、单票 20%、总仓位 60%
    "conc5": {"max_positions": 5, "max_total_exposure": 0.60},
    # 低敞口：总仓位 60%、单票 15%，用于观察回撤
    "low_exp": {"max_total_exposure": 0.60, "single_position_ratio": 0.15},
    # ---- E2/E5：改买点与筛选维度（非阈值调参） ----
    # E2 高开 >2% 不追（原假设值）
    "e2_2": {"max_open_gain": 0.02},
    # E2 高开 >4% 不追（更宽松，保留更多成交）
    "e2_4": {"max_open_gain": 0.04},
    # E5 只买有量的：量比 ≥1、换手率 ≥2%
    "e5_liq": {"min_volume_ratio": 1.0, "min_turnover_rate": 2.0},
    # E5 不追已涨：近 5 日涨幅 >15% 不进买入池
    "e5_g5": {"max_gain_5d": 0.15},
    # E5 量能阈值敏感性：宽松（量比 0.8 / 换手 1.5%）
    "e5_liq_lo": {"min_volume_ratio": 0.8, "min_turnover_rate": 1.5},
    # E5 量能阈值敏感性：严格（量比 1.5 / 换手 3%）
    "e5_liq_hi": {"min_volume_ratio": 1.5, "min_turnover_rate": 3.0},
    # E2 + E5 组合（两条过滤同时生效）
    "e2e5": {
        "max_open_gain": 0.02,
        "min_volume_ratio": 1.0,
        "min_turnover_rate": 2.0,
        "max_gain_5d": 0.15,
    },
    # ---- E9：卖出侧加固（浮盈回吐是主要漏损点，见 BACKTEST_LOG 记录 #5） ----
    # 保本止损：曾浮盈 ≥5% 后止损上移到成本价
    "be5": {"breakeven_trigger": 0.05},
    # 保本止损（更晚触发，5% → 8%）
    "be8": {"breakeven_trigger": 0.08},
    # 移动止损：曾浮盈 ≥5% 后，从最高价回撤 6% 卖出
    "trail6": {"trail_pct": 0.06, "trail_arm": 0.05},
    # 对照：止盈目标 20% → 10%（看降低目标能否提高达成率）
    "tp10": {"take_profit": 0.10},
    # 条件化防守：只有上证指数跌破 MA20 时才启用保本止损，走强时让利润跑
    "be8_reg20": {"breakeven_trigger": 0.08, "regime_ma": 20},
    "be5_reg20": {"breakeven_trigger": 0.05, "regime_ma": 20},
    # ---- E10 分段止盈 / E11 止损收盘确认 ----
    # +10% 卖一半，剩余继续按 +20% 跑
    "ptp10": {"partial_tp_at": 0.10},
    # +8% 卖一半（更早落袋）
    "ptp8": {"partial_tp_at": 0.08},
    # 分段止盈 + 余仓移动止损（+5% 后从峰值回撤 6% 出）
    "ptp10_trail6": {"partial_tp_at": 0.10, "trail_pct": 0.06, "trail_arm": 0.05},
    # 止损只认收盘价（盘中插针不砍）
    "stop_close": {"stop_confirm_close": True},
    # 分段止盈 + 收盘确认止损
    "ptp10_close": {"partial_tp_at": 0.10, "stop_confirm_close": True},
    # ---- E13 组合层面择时：上证跌破 MA20 时把总仓位降到 40% / 20% ----
    "reg40": {"regime_ma": 20, "weak_exposure": 0.40},
    "reg20": {"regime_ma": 20, "weak_exposure": 0.20},
    # 弱市降仓 + 保本止损（组合防守 + 个股防守）
    "reg40_be8": {"regime_ma": 20, "weak_exposure": 0.40, "breakeven_trigger": 0.08},
    # 敏感性：换均线周期 / 换弱市仓位
    "reg40_ma10": {"regime_ma": 10, "weak_exposure": 0.40},
    "reg40_ma60": {"regime_ma": 60, "weak_exposure": 0.40},
    "reg30": {"regime_ma": 20, "weak_exposure": 0.30},
    "reg50": {"regime_ma": 20, "weak_exposure": 0.50},
    # ---- 自动搜索产物（scripts/search.py s1→s2→s3 的结果，见 BACKTEST_LOG 记录 #7） ----
    "auto_v1": {
        "buy_threshold": 35,
        "stop_loss": 0.08,
        "take_profit": 0.30,
        "max_positions": 5,
        "single_position_ratio": 0.25,
        "max_total_exposure": 0.95,
        "max_hold_days": 10,
        "breakeven_trigger": 0.08,
        "partial_tp_at": 0.10,
        "min_volume_ratio": 1.0,
        "regime_ma": 20,
        "weak_exposure": 0.40,
    },
    # 稳健性优先的候选（4 窗口 × Top300/200 中 7/8 格正超额，最差 −6.6%）
    "auto_v2": {
        "buy_threshold": 25,
        "stop_loss": 0.06,
        "take_profit": 0.20,
        "max_positions": 5,
        "single_position_ratio": 0.20,
        "max_total_exposure": 0.95,
        "max_hold_days": 5,
        "min_volume_ratio": 1.0,
        "regime_ma": 20,
        "weak_exposure": 0.40,
    },
    # 平均超额最高的候选（+12.19%，但最差 −8.2%，6/8 格为正）
    "auto_v3": {
        "buy_threshold": 25,
        "stop_loss": 0.05,
        "take_profit": 0.20,
        "max_positions": 10,
        "single_position_ratio": 0.15,
        "max_total_exposure": 0.95,
        "max_hold_days": 0,
        "breakeven_trigger": 0.12,
        "partial_tp_at": 0.10,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    # 防守型候选（低止盈 + 保本 + 分段止盈 + 择时）
    "auto_v4": {
        "stop_loss": 0.10,
        "take_profit": 0.12,
        "max_positions": 8,
        "single_position_ratio": 0.20,
        "max_total_exposure": 0.80,
        "max_hold_days": 0,
        "breakeven_trigger": 0.12,
        "partial_tp_at": 0.10,
        "regime_ma": 20,
        "weak_exposure": 0.40,
    },
    # ---- E26 选股模型实验：改「怎么选」，不改「怎么买卖」 ----
    # 因子权重按分组调整（组内比例不变，总和自动归一化到 100）
    "w_trend": {"factor_weights": _w(趋势=1.5)},
    "w_mom": {"factor_weights": _w(动量=1.4)},
    "w_vol": {"factor_weights": _w(量能=1.75)},
    "w_risk": {"factor_weights": _w(风险=1.67)},
    # 策略集 A/B：只看突破类 vs 只看交叉/超卖类
    "strat_break": {"strategies": ("海龟突破", "平台突破", "动量突破")},
    "strat_cross": {"strategies": ("均线放量", "MACD金叉", "KDJ超卖金叉")},
}


def list_profiles() -> list[str]:
    return list(PROFILES)
