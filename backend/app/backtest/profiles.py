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
from app.backtest.pipeline import MAIN_BOARD_PREFIXES


def _w(**group_mult: float) -> dict[str, float]:
    """按「因子分组」倍数生成 14 因子权重（组内维持原比例）。

    例：``_w(趋势=1.5)`` = 趋势组权重 ×1.5，其余组不变，
    总和在 ``factors.reweight`` 里再归一化到 100，保证与买入阈值同分制。
    """
    return {
        key: round(weight * group_mult.get(factors.FACTOR_LABELS[key][1], 1.0), 2)
        for key, weight in factors.FACTOR_WEIGHTS.items()
    }


#: E34 的「IC 强度」权重（记录 #22 的 20 日 |IC| 分配，合计 100）。
#: _W_IC1：14 项全保留，实测有 IC 的 8 项加权，其余压到低权重；
#: _W_IC2：只留实测有 IC 的 8 项，未实测因子显式归零（reweight 不会自动清空）。
_W_IC1 = {
    "turnover": 15.0, "long_position": 12.0, "bias": 11.0, "gain20": 10.0,
    "rsi": 9.0, "atr": 8.0, "macd": 7.0, "vol_ratio": 5.0,
    "vol_trend": 5.0, "ma_trend": 5.0, "ma_slope": 5.0,
    "drawdown": 4.0, "breakout": 2.0, "boll": 2.0,
}
_W_IC2 = {
    "turnover": 20.0, "long_position": 16.0, "bias": 14.0, "gain20": 13.0,
    "rsi": 11.0, "atr": 10.0, "macd": 9.0, "vol_ratio": 7.0,
    "vol_trend": 0.0, "ma_trend": 0.0, "ma_slope": 0.0,
    "drawdown": 0.0, "breakout": 0.0, "boll": 0.0,
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
    # ---- E22 推荐组合（记录 #11：4 项改动 + 单票保持线上 20%） ----
    # 止损 6% + 止盈 30% + 量比≥1 + 弱市 MA60/0.6；跨池 18 格全正的唯一组合
    "e22": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    # ---- E27：E22 推荐组合 × E26 动量加权（一个改善均值、一个改善最差，看能否叠加） ----
    "e27": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
        "factor_weights": _w(动量=1.4),
    },
    # ---- E24：换手率下限（加在 E22 组合上单测，值敏感性） ----
    "e22_tr1": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
        "min_turnover_rate": 1.0,
    },
    "e22_tr3": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
        "min_turnover_rate": 3.0,
    },
    # ---- E28：e22 邻域敏感性（每侧各 2 点，确认不是孤峰；其余 3 项固定） ----
    # 止损曲线补点（4%/8%），用来画 4~8% 的完整形状
    "e22_sl4": {
        "stop_loss": 0.04,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_sl8": {
        "stop_loss": 0.08,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_tp25": {
        "stop_loss": 0.06,
        "take_profit": 0.25,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_tp35": {
        "stop_loss": 0.06,
        "take_profit": 0.35,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_sl5": {
        "stop_loss": 0.05,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_sl7": {
        "stop_loss": 0.07,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 60,
        "weak_exposure": 0.60,
    },
    "e22_ma40": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 40,
        "weak_exposure": 0.60,
    },
    "e22_ma80": {
        "stop_loss": 0.06,
        "take_profit": 0.30,
        "min_volume_ratio": 1.0,
        "regime_ma": 80,
        "weak_exposure": 0.60,
    },
    # ---- E33：反转版选股（2×2 设计，用来定位问题出在「打分方向」还是「门槛」） ----
    # 依据记录 #22：9 个量价因子在 1 个月尺度上 IC 全为负。
    #   rev_inv  ：只翻总分号（门槛仍是突破类）
    #   rev_kdj  ：只换反转门槛（KDJ 超卖金叉），打分方向不变
    #   rev_both ：两者都翻
    "rev_inv": {"invert_score": True},
    "rev_kdj": {"strategies": ["KDJ超卖金叉"]},
    "rev_both": {"invert_score": True, "strategies": ["KDJ超卖金叉"]},
    # ---- E32：大盘分档仓位（组合层择时，唯一能改变收益量级的方向） ----
    # 用上证 MA20/MA60 把大盘拆成 4 档，档位决定「总仓位上限」= 线上 80% × 系数：
    #   3=双线之上(满仓) 2=破快线站上慢线 1=破慢线站上快线 0=双线之下
    # 两个方案都是**先验设计**（不是网格搜出来的），只在 A~F 上看方向，G~J 只用于验收。
    "e32_a": {
        "regime_ma": 60,
        "regime_fast_ma": 20,
        "regime_tiers": (0.10, 0.40, 0.70, 1.00),
    },
    # 更激进：双线之下直接清仓（次日开盘卖，收盘确认）
    "e32_b": {
        "regime_ma": 60,
        "regime_fast_ma": 20,
        "regime_tiers": (0.00, 0.30, 0.60, 1.00),
        "regime_flat_below": True,
    },
    # ---- E30：波动率自适应止损（机制参数，直接替代固定的 stop_loss 8%） ----
    # 止损距离 = N × ATR%（前一日），夹在 3%~15% 之间；A 股 ATR% 中位数约 3~4%，
    # 所以 2×ATR 大致等价于原来的固定 8%，但会随个股波动自动伸缩。
    "e30_a15": {"stop_atr_mult": 1.5},
    "e30_a2": {"stop_atr_mult": 2.0},
    "e30_a25": {"stop_atr_mult": 2.5},
    # ---- E26 选股模型实验：改「怎么选」，不改「怎么买卖」 ----
    # 因子权重按分组调整（组内比例不变，总和自动归一化到 100）
    "w_trend": {"factor_weights": _w(趋势=1.5)},
    "w_mom": {"factor_weights": _w(动量=1.4)},
    "w_vol": {"factor_weights": _w(量能=1.75)},
    "w_risk": {"factor_weights": _w(风险=1.67)},
    # 策略集 A/B：只看突破类 vs 只看交叉/超卖类
    "strat_break": {"strategies": ("海龟突破", "平台突破", "动量突破")},
    "strat_cross": {"strategies": ("均线放量", "MACD金叉", "KDJ超卖金叉")},
    # ---- E34：按 IC 强度加权反转（记录 #22/#23 的后续） ----
    # `rev_inv` 是「14 因子整体取负」，等价于假设全部因子反向且等强；
    # 但实测 20 日 IC 从 turnover −0.159 到 vol_ratio −0.051 差 3 倍，故按强度分配权重。
    # rev_ic1：14 项全保留，实测有 IC 的 8 项加权，其余压到低权重。
    "rev_ic1": {
        "invert_score": True,
        "factor_weights": _W_IC1,
    },
    # rev_ic2：只留实测有 IC 的 8 项（按 |IC| 归一化到 100），未实测因子权重归零。
    "rev_ic2": {
        "invert_score": True,
        "factor_weights": _W_IC2,
    },
    # 阈值扫描：反转后总分号变了，15 分档的口径要重新标定（25 = 更挑，只买最弱那批）。
    "rev_ic1_t25": {
        "invert_score": True,
        "buy_threshold": 25,
        "factor_weights": _W_IC1,
    },
    "rev_ic2_t25": {
        "invert_score": True,
        "buy_threshold": 25,
        "factor_weights": _W_IC2,
    },
    # ---- E34b：池子口径对齐（记录 #22 选项 B） ----
    # 只买沪深主板 + 创业板（用户实际交易范围），过滤在排名取 TopN 之前生效。
    "mb": {"board_prefixes": MAIN_BOARD_PREFIXES},
    # 反转 + 板块过滤：看两者是否互相加成，还是只是少买了几只票。
    "rev_ic2_mb": {
        "invert_score": True,
        "board_prefixes": MAIN_BOARD_PREFIXES,
        "factor_weights": _W_IC2,
    },
    # ---- E35：分位数买入门槛（记录 #24 的下一步） ----
    # 当日自选池（≤watch_cap 只）按打分取前 X% 作为买入池，替代绝对分 ±15。
    # 动机：权重一变绝对分的分布就变，跨方案不可比（#24 里 `rev_ic1_t25` 与
    # `rev_ic1` 四窗口结果完全一致，就是绝对阈值失效的直接证据）。
    # 先不带反转跑 pct20/pct10，用来看「线上绝对分档」在同一口径下相当于多少分位。
    "pct20": {"buy_top_pct": 0.20},
    "pct10": {"buy_top_pct": 0.10},
    "pct20_rev": {"invert_score": True, "buy_top_pct": 0.20},
    # 权重方案只有在分位数口径下才可比：IC1/IC2 各一格，另加 IC2 的 10% 敏感性。
    "pct20_ic1": {"invert_score": True, "buy_top_pct": 0.20, "factor_weights": _W_IC1},
    "pct20_ic2": {"invert_score": True, "buy_top_pct": 0.20, "factor_weights": _W_IC2},
    "pct10_ic2": {"invert_score": True, "buy_top_pct": 0.10, "factor_weights": _W_IC2},
    # ---- E39：成交摩擦压力测试（单边滑点 0.1% / 0.3% / 0.5%，其余同线上） ----
    # 目的：检验「多数月份正超额」是不是只是摩擦假设的产物。
    # 0.3% 大致相当于「开盘价成交 + 一档冲击 + 小幅延迟」，偏保守但不算苛刻。
    "slip10": {"slippage": 0.001},
    "slip30": {"slippage": 0.003},
    "slip50": {"slippage": 0.005},
    # ---- E40：关闭满仓轮动（降换手，记录 #29 推断的唯一有证据方向） ----
    # 轮动是强制卖出，换手与摩擦的最大来源；先看它与滑点的 2×2。
    "nrot": {"rotation_enabled": False},
    "nrot_s30": {"rotation_enabled": False, "slippage": 0.003},
    # ---- E41：集中持仓（1~2 只），总仓位保持线上 80% 不变 ----
    # 只改「分散度」：单票比例 × 持仓数 = 80%，所以不是加减杠杆，而是把注压到更少的票上。
    # 动机：记录 #27~#30 显示中位胜率只有 22%、超额集中在少数大赢单 → 集中可能放大右尾，
    # 也可能放大左尾（最差月 −14%~−17%），这是唯一没被覆盖过的结构性变量。
    "conc1": {"max_positions": 1, "single_position_ratio": 0.80},
    "conc2": {"max_positions": 2, "single_position_ratio": 0.40},
    "conc1_s30": {"max_positions": 1, "single_position_ratio": 0.80, "slippage": 0.003},
    "conc2_s30": {"max_positions": 2, "single_position_ratio": 0.40, "slippage": 0.003},
    # ---- E42：降频调仓（周频/双周频），卖出与风控仍每日生效 ----
    # 记录 #29：月换手约 2.2 个资本，0.3% 单边摩擦就把正超额月份比例从 63% 打到 53%。
    # 降频是最直接的减法：只减少"新开仓"，不动止损/止盈/信号卖出。
    "wk": {"rebalance_days": 5},
    "wk_s30": {"rebalance_days": 5, "slippage": 0.003},
    "wk10_s30": {"rebalance_days": 10, "slippage": 0.003},
    # ---- E-intraday：真实 5 分钟线精确止损/止盈（对照 default） ----
    # 开启后对持仓日读本地 data/minute5_fsdb（全市场 5563 只，2025-01-02 起，
    # 纯本地不联网 → 同一参数结果可复现），按时间顺序检测首次触发，成交价取分钟价。
    # default 走日K 边界（low/high 已含盘中极值，但成交价直接取止损价，偏乐观）。
    "intraday5": {"intraday_minutes": True},
    # ---- E45：分钟级「确认强势」买入（与 #54 的「等回踩」方向相反） ----
    # 观察开盘后 30 分钟（前 6 根 5m），站住了才以窗口末价成交，否则当天不买。
    # 必须与 intraday5 对照：止损口径一致，只差入场规则。
    "cf30": {"intraday_minutes": True, "entry_confirm_minutes": 30,
             "entry_confirm_mode": "hold_open"},
    "cfv30": {"intraday_minutes": True, "entry_confirm_minutes": 30,
              "entry_confirm_mode": "above_avg"},
    "cfb30": {"intraday_minutes": True, "entry_confirm_minutes": 30,
              "entry_confirm_mode": "both"},
    # 验收协议要求报两档摩擦：同规则 + 0.3% 单边滑点（记录 #29 的生死线）
    "intraday5_s30": {"intraday_minutes": True, "slippage": 0.003},
    "cf30_s30": {"intraday_minutes": True, "entry_confirm_minutes": 30,
                 "entry_confirm_mode": "hold_open", "slippage": 0.003},
    "cfv30_s30": {"intraday_minutes": True, "entry_confirm_minutes": 30,
                  "entry_confirm_mode": "above_avg", "slippage": 0.003},
    # ---- E-intraday-entry：全时段买入（把「只在开盘 5 分钟成交」放开到盘中多时点） ----
    # 假设：实盘「盘中分析每分钟重算 14 因子」能在开盘之后发现新机会，开盘一次性
    # 成交会漏掉这些机会；代价是成交价从开盘价挪到盘中（平均更贵）。
    # 口径：候选 = 前一交易日收盘已在自选池且判 buy；各时点用「当日截断日线」
    # 重算指标与 14 因子，仍判 buy 才以该时点 5 分钟收盘价成交（无未来函数）。
    "iad": {"intraday_entry": True},
    # 只留开盘后 15 分钟一个时点（近似「开盘后再确认一次」的最小改动）
    "iad_0945": {"intraday_entry": True, "intraday_entry_times": ("09:45",)},
    # 只在下午时段补单（10:30 之后才出现的盘中信号）
    "iad_pm": {"intraday_entry": True,
               "intraday_entry_times": ("10:30", "13:30", "14:30")},
    # 验收协议：同规则 + 0.3% 单边滑点
    "iad_s30": {"intraday_entry": True, "slippage": 0.003},
    # ---- E-open-window：只把「开盘那一刻」放宽成「开盘后 N 分钟内可成交」 ----
    # 与 iad 的区别：信号来源不变（仍是前一日收盘的自选池），只补「开盘一字板后开板」
    # 这一类机会，等价于线上窗口内多次撮合。用来回答「5 分钟窗口是不是太窄」。
    "win30": {"open_fill_minutes": 30},
    "win30_s30": {"open_fill_minutes": 30, "slippage": 0.003},
    # 完整「全时段」：开盘窗口开板补单 + 盘中各时点重新确认
    "win30_iad": {"open_fill_minutes": 30, "intraday_entry": True},
    # ---- E13b：弱市买入闸门三档（对齐线上 paper_regime_weak_pause / paper_regime_ma=60） ----
    # 对照基准 default = 不择时（regime_ma=None）。regime_ma=60 与线上一致：
    # 用**前一交易日**上证收盘 vs MA60 判弱市，不留未来函数。
    # 弱市总仓位上限：0.6 / 0.3 是「降仓」，0.0 = 「全暂停买入」（等价线上 weak_pause=True）。
    "reg_red60": {"regime_ma": 60, "weak_exposure": 0.60},
    "reg_red30": {"regime_ma": 60, "weak_exposure": 0.30},
    "reg_pause": {"regime_ma": 60, "weak_exposure": 0.0},
    # 均线敏感性：全暂停档换 MA20 / MA120（判断结论是否只是 60 日的巧合）
    "reg_pause20": {"regime_ma": 20, "weak_exposure": 0.0},
    "reg_pause120": {"regime_ma": 120, "weak_exposure": 0.0},
    # ---- 弱市「保留比例仓位」档位寻优（用户风险偏好：宁可小亏换上涨时的收益） ----
    # 50% / 40% 介于「降仓 60%（几乎无效）」与「降仓 30%（均值最好但涨市踏空）」之间
    "reg_red50": {"regime_ma": 60, "weak_exposure": 0.50},
    "reg_red40": {"regime_ma": 60, "weak_exposure": 0.40},
    # 降仓 30% × 均线敏感性：MA60 在涨市初期滞后，试 MA20（更敏感）与 MA120（更迟钝）
    "reg_red30_ma20": {"regime_ma": 20, "weak_exposure": 0.30},
    "reg_red30_ma120": {"regime_ma": 120, "weak_exposure": 0.30},
    # ---- E44：弱转强爬坡（ramp）——「不猜时点，摊成本」 ----
    # 背景：#59 H 段 11 笔全挤在弱转强切换日、回撤 −16.48%，说明二值闸门「翻上来就满仓」
    # 是损失来源之一。ramp 不动「何时算强」，只动「强了以后几天打满」。
    # 弱市档位固定用线上现状 0.3（#63），基准 reg_red30 = 不爬坡。
    "reg_ramp1": {"regime_ma": 60, "weak_exposure": 0.30, "regime_ramp_days": 1},
    "reg_ramp2": {"regime_ma": 60, "weak_exposure": 0.30, "regime_ramp_days": 2},
    "reg_ramp3": {"regime_ma": 60, "weak_exposure": 0.30, "regime_ramp_days": 3},
    "reg_ramp5": {"regime_ma": 60, "weak_exposure": 0.30, "regime_ramp_days": 5},
    # 对照：弱市全暂停（#59 线上旧档）+ ramp，验证 ramp 对「0 → 满仓」跳变是否也有效
    "reg_pause_ramp3": {"regime_ma": 60, "weak_exposure": 0.0, "regime_ramp_days": 3},
    # ---- E47：开盘段成交价 = 开盘后 30 分钟 1m VWAP（不改任何策略逻辑，只改执行价） ----
    # 目的：把「开盘价一次性成交」换成「开盘窗口的真实均价」，让所有回测更贴近线上。
    # 这不是新策略，是**度量校准**——若结果变差，说明历史收益里有「开盘价精确成交」的红利。
    # 窗口长度敏感度：线上开盘窗口只有几分钟（每 5 秒撮合），
    # 1/5 分钟才是「同一时点的更真实成交价」，30 分钟等于把买入延迟半小时（那是策略变化）。
    "vwap1": {"exec_fill": "vwap1m", "exec_fill_minutes": 1},
    "vwap5": {"exec_fill": "vwap1m", "exec_fill_minutes": 5},
    "vwap30": {"exec_fill": "vwap1m", "exec_fill_minutes": 30},
    "vwap30_red30": {"exec_fill": "vwap1m", "exec_fill_minutes": 30,
                     "regime_ma": 60, "weak_exposure": 0.30},
    # ---- E46：做T（首次系统化回归；基准 default = 不做T） ----
    # 参数取线上默认（config.py 的 paper_t_*），必须显式写全：None 表示「该动作关闭」，
    # 而不是「沿用线上值」。t_minute_path 才是与线上 t_tick 同口径的版本（5m 路径）。
    # 线上默认里 down_add/down_full=0.20 ≈ 关闭低吸（A 股跌停 -10%，开盘跌 20% 不可能）。
    "t_on": {
        "t_active": True,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.20, "t_down_full": 0.20, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # 同参数 + 5 分钟真实路径（当日回落即可买回，能区分先高后低/先低后高）
    "t_on_min": {
        "t_active": True, "t_minute_path": True,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.20, "t_down_full": 0.20, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # 低吸真的打开（跌 5% 加 1/4 现金、跌 7% 加满）+ 分钟路径：config 注释里"E 版"的激进参数
    "t_low_min": {
        "t_active": True, "t_minute_path": True,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.05, "t_down_full": 0.07, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # 只做低吸（关掉高抛与涨停卖半），用于分离「哪个动作在赚/亏」
    "t_dip_min": {
        "t_active": True, "t_minute_path": True,
        "t_size": 0.25, "t_up_sell": 0.0, "t_up_buyback": 0.0,
        "t_down_add": 0.05, "t_down_full": 0.07, "t_rebound_sell": 0.02,
        "t_limit_sell_half": False, "t_crash_guard": 0.02,
    },
    # 采样频率敏感度：同样参数，路径改用 1 分钟线（每天 240 个价格点 vs 5m 的 48 个）。
    # 若 5m 与 1m 差很多，说明做T结论由采样精度主导，历史回测不足以定论（记录 #66）。
    "t_low_1m": {
        "t_active": True, "t_minute_path": True, "t_minute_period": "1",
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.05, "t_down_full": 0.07, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # 1m 路径 + 0.3% 单边滑点（与 5m 版同一条验收线）
    "t_low_1m_s30": {
        "t_active": True, "t_minute_path": True, "t_minute_period": "1", "slippage": 0.003,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.05, "t_down_full": 0.07, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # 验收协议第 3 条：同规则 + 0.3% 单边滑点。做T 笔数是基准的 2 倍以上，
    # 摩擦敏感度是这一族实验的决定性检验（记录 #29）。
    "t_on_min_s30": {
        "t_active": True, "t_minute_path": True, "slippage": 0.003,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.20, "t_down_full": 0.20, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    "t_low_min_s30": {
        "t_active": True, "t_minute_path": True, "slippage": 0.003,
        "t_size": 0.25, "t_up_sell": 0.05, "t_up_buyback": 0.03,
        "t_down_add": 0.05, "t_down_full": 0.07, "t_rebound_sell": 0.02,
        "t_limit_sell_half": True, "t_crash_guard": 0.02,
    },
    # ---- 盈亏比结构（记录 #60 §7 方向：小亏换大赚要靠盈亏比，不靠择时） ----
    # 历史上只测过「收紧」方向（tp10 / sl10 都更差，见记录 #30 附近），
    # 本轮补「放宽止盈 / 收紧止损 / 提高单票集中度」这三类未测方向。
    "tp30": {"take_profit": 0.30},
    "tp40": {"take_profit": 0.40},
    "sl6": {"stop_loss": 0.06},
    "sl5": {"stop_loss": 0.05},
    "atr15": {"stop_atr_mult": 1.5},          # 止损距离 2.0→1.5 倍 ATR（收紧高波动票）
    "single25": {"single_position_ratio": 0.25},  # 单票 20%→25%（高分票承担更大权重）
    # 组合：放宽止盈 + 收紧止损（盈亏比从 20:8=2.5:1 提高到 40:5=8:1 / 30:5=6:1）
    # 依据：tp40 在涨市段更好、sl5 在弱市段更好，两者作用于不同市况，理论上互补
    "tp40_sl5": {"take_profit": 0.40, "stop_loss": 0.05},
    "tp30_sl5": {"take_profit": 0.30, "stop_loss": 0.05},
    "tp40_sl6": {"take_profit": 0.40, "stop_loss": 0.06},
    "tp40_sl5_s25": {"take_profit": 0.40, "stop_loss": 0.05,
                     "single_position_ratio": 0.25},
    # ---- 候选池宽度（#61 之后唯一没扫过的参数族：universe / watch_cap / add_per_day / 持仓数） ----
    # 假设：机会多寡受「每日成交额池大小 × 自选池容量」限制，而不是出场规则。
    # 线上默认：universe=300、watch_cap=60、add_per_day=50、max_positions=10。
    "uni500": {"universe_size": 500},
    "uni800": {"universe_size": 800},
    "watch100": {"watch_cap": 100},
    "watch150": {"watch_cap": 150},
    "add30": {"add_per_day": 30},
    "add80": {"add_per_day": 80},
    "maxpos12": {"max_positions": 12},
    # ---- E49：组合级回撤断路器（记录 #69）----
    # 与 #59 的弱市择时不同：那个看指数（外部），这个看策略自己的净值（内部）。
    # 目标不是提中位，而是压尾部（#61 §7 点名的剩余方向）；卖出与止损照常，只停新开仓。
    "ddc10": {"dd_pause_at": 0.10, "dd_pause_days": 10},
    "ddc15": {"dd_pause_at": 0.15, "dd_pause_days": 10},
    "ddc10_20": {"dd_pause_at": 0.10, "dd_pause_days": 20},
    "ddc10_s30": {"dd_pause_at": 0.10, "dd_pause_days": 10, "slippage": 0.003},
}

# ---- E48：成交价随机扰动（稳健性检验，BACKTEST_LOG #67 §8）----
# 固定策略与参数，只对每笔成交价乘 (1 + N(0, σ))，看 18 段「中位超额」的分布宽度。
# σ = 10bp（0.1%）与 20bp（0.2%）各 10 个种子：若 10bp 下中位超额就跨 0，
# 则所有 <2.5pp 的「超额」结论都只是执行价抽样噪声。
for _seed in range(1, 11):
    PROFILES[f"nz10_{_seed}"] = {"fill_noise_bps": 10.0, "fill_noise_seed": _seed}
    PROFILES[f"nz20_{_seed}"] = {"fill_noise_bps": 20.0, "fill_noise_seed": _seed}
del _seed


def list_profiles() -> list[str]:
    return list(PROFILES)
