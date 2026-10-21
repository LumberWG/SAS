"""市场情绪分级（5 档离散等级 + 权重）。

设计思想（用户框架）:
  市场整体情绪可划分为 5 类，每类有默认权重，
  用于给选股信号与 LLM 分析提供「市场环境」上下文:

    1. 极度亢奋  权重 +1   整体乐观、赚钱效应极致（追高需警惕）
    2. 多头占优  权重 +0.5 顺势环境，趋势/突破策略有效性高
    3. 中性      权重  0   结构性机会，严选个股
    4. 空头占优  权重 -0.5 防守为主，降低盈利预期
    5. 极度悲观  权重 -1   恐慌区域，关注超跌反弹或观望

映射依据: 市场温度计（0-100）= 上涨占比 × 60 + 涨停占比(封顶5%) × 40，
温度越高代表市场越强。5 档阈值 [80, 60, 40, 20] 按等宽区间划分。

调整阈值或权重时需同步更新 README「市场情绪分级」章节与相关测试。
"""
from __future__ import annotations

#: 情绪等级定义: (温度下限, 等级名, 权重, 操作建议)
#: 顺序必须为温度降序，classify_sentiment 按序遍历首个满足项
SENTIMENT_LEVELS: list[tuple[float, str, float, str]] = [
    (80.0, "极度亢奋", 1.0, "赚钱效应极致，注意波动加剧，追高需谨慎"),
    (60.0, "多头占优", 0.5, "顺势环境，趋势突破类策略有效性较高"),
    (40.0, "中性", 0.0, "结构性机会为主，严选个股、控制仓位"),
    (20.0, "空头占优", -0.5, "防守为主，降低盈利预期，回避弱势股"),
    (-1.0, "极度悲观", -1.0, "恐慌区域，可关注超跌反弹机会或观望"),
]


def classify_sentiment(temperature: float) -> dict:
    """按市场温度返回情绪等级。

    Args:
        temperature: 市场温度计（0-100）

    Returns:
        {level, weight, advice, temperature}
    """
    for threshold, level, weight, advice in SENTIMENT_LEVELS:
        if temperature >= threshold:
            return {
                "level": level,
                "weight": weight,
                "advice": advice,
                "temperature": round(temperature, 1),
            }
    # 理论上不可达（最后一级下限为 -1），防御性兜底
    return {
        "level": "极度悲观", "weight": -1.0,
        "advice": "恐慌区域，可关注超跌反弹机会或观望",
        "temperature": round(temperature, 1),
    }


def sentiment_from_snapshot(snapshot) -> dict:
    """从全市场快照 DataFrame 直接计算情绪等级。

    Args:
        snapshot: 含 pct_change 列的股票快照 DataFrame（涨跌幅单位 %）

    涨跌停判定按 ±9.8% 近似（忽略 ST/科创创业/北交所差异）。
    """
    import pandas as pd

    pct = snapshot["pct_change"].dropna()
    total = len(pct)
    if total == 0:
        return classify_sentiment(50.0)

    up = int((pct > 0).sum())
    limit_up = int((pct >= 9.8).sum())
    up_ratio = up / total
    limit_ratio = min(limit_up / total, 0.05) / 0.05
    temperature = up_ratio * 60 + limit_ratio * 40
    return classify_sentiment(temperature)
