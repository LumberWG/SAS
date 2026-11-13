"""持仓止损价解析：实盘与回测**唯一**共用的一份口径。

历史教训（2026-11-12，回测 #45）
--------------------------------
回测 ``pipeline._simulate`` 曾用 ``defensive``（大盘是否弱于 MA60）把固定止损、
保本止损、移动止损一起门控；实盘 ``PaperEngine.check_stop_loss_target`` 则是
**每个交易日无条件**检查。结果 250 个交易日里有 134 天回测完全不止损，45 笔
止损全部挤在 116 个弱市日，亏损票在强市被一直扛着、跌更深后集中砍，制造了
「移动止损吃掉全部盈利」的假象，也直接违反「回测与实盘严格 1:1」。

因此本模块刻意**不接受任何 regime / 大盘强弱 / 机制层参数**：止损只看持仓自身
的成本、建仓时定死的止损线、峰值与三个风控比例。谁想加门控，先想清楚实盘
有没有同样一层；`app.backtest.alignment.audit_stop_parity()` 会对此做行为探针。
"""
from __future__ import annotations


def resolve_stop_price(
    cost: float,
    base_stop: float,
    peak: float,
    *,
    breakeven_pct: float = 0.0,
    trail_pct: float = 0.0,
    trail_arm: float = 0.0,
) -> tuple[float, str]:
    """返回 ``(当日生效止损价, 标签)``；标签供回测写卖出原因，实盘可忽略。

    三档按顺序取**更紧**（更高）的一条，与实盘原 ``max(...)`` 语义等价：

    1. 建仓时定死的止损线 ``base_stop``（实盘含 ATR 放宽 / 机制层收紧，回测含
       ``stop_distance``）；标签「止损」。
    2. 峰值曾达 ``cost × (1 + breakeven_pct)`` → 上移到成本价，标签「保本止损」。
       ``breakeven_pct <= 0`` 表示关闭。
    3. 峰值曾达 ``cost × (1 + trail_arm)`` → 从峰值回撤 ``trail_pct`` 即卖，
       标签「移动止损」。``trail_pct <= 0`` 表示关闭。

    ``peak`` 必须是**截至当前时点已知**的最高价：实盘用实时价逐 tick 更新，
    回测用之前交易日（含买入日）的 high，不得用当日 high，否则是未来函数。
    """
    px = float(base_stop)
    label = "止损"
    if breakeven_pct > 0 and peak >= cost * (1.0 + breakeven_pct) and cost > px:
        px, label = float(cost), "保本止损"
    if trail_pct > 0 and peak >= cost * (1.0 + trail_arm):
        trail = peak * (1.0 - trail_pct)
        if trail > px:
            px, label = float(trail), "移动止损"
    return px, label
