"""技术面共享评分模块（LLM 决策仪表盘与智能建议引擎共用）。

评分体系（满分 ±8，与 app/llm/analyzer.py 的规则降级结论一致）:
  均线多头排列(MA5>MA10>MA20) +2 / 空头 -2
  MACD 金叉且柱>0 +1.5 / 死叉且柱<0 -1.5
  KDJ 超卖(J<20) +1.5 / 超买(J>80) -1.5
  量比>1.5 放量 +1 / <0.7 缩量 -1
  乖离 BIAS6<-6% 超跌 +1 / >6% 超涨 -1

调整评分规则需同步 tests/test_scoring.py 与 README「智能建议」章节。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class ScoreResult:
    """评分结果。"""

    score: float
    #: 检查清单 [(label, status, detail)]，status ∈ pass/warn/fail
    checklist: list[tuple[str, str, str]]

    def reasons(self, statuses: tuple[str, ...] = ("pass", "fail")) -> list[str]:
        """提取指定状态的检查项文本（用于生成建议理由）。"""
        return [
            f"{label}: {detail}"
            for label, status, detail in self.checklist
            if status in statuses
        ]


def score_technical(df: pd.DataFrame) -> ScoreResult:
    """对含指标的K线 DataFrame 做技术面评分。

    Args:
        df: 已按日期升序、已通过 add_all_indicators 附加指标列的 DataFrame

    Returns:
        ScoreResult（score + 六维检查清单）
    """
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    checklist: list[tuple[str, str, str]] = []
    score = 0.0

    # 1. 均线排列
    if last["ma5"] > last["ma10"] > last["ma20"]:
        score += 2
        status, detail = "pass", "多头排列"
    elif last["ma5"] < last["ma10"] < last["ma20"]:
        score -= 2
        status, detail = "fail", "空头排列"
    else:
        status, detail = "warn", "交织粘合"
    checklist.append(("均线排列", status, detail))

    # 2. MACD
    golden = prev["dif"] <= prev["dea"] and last["dif"] > last["dea"]
    dead = prev["dif"] >= prev["dea"] and last["dif"] < last["dea"]
    if golden and last["macd_hist"] > 0:
        score += 1.5
        status, detail = "pass", "金叉红柱"
    elif dead and last["macd_hist"] < 0:
        score -= 1.5
        status, detail = "fail", "死叉绿柱"
    else:
        status, detail = "warn", "无明显交叉"
    checklist.append(("MACD动量", status, detail))

    # 3. KDJ
    if last["kdj_j"] < 20:
        score += 1.5
        status, detail = "pass", f"超卖 J={last['kdj_j']:.0f}"
    elif last["kdj_j"] > 80:
        score -= 1.5
        status, detail = "fail", f"超买 J={last['kdj_j']:.0f}"
    else:
        status, detail = "warn", f"中位 J={last['kdj_j']:.0f}"
    checklist.append(("KDJ位置", status, detail))

    # 4. 量能（量比）
    vol_ratio = last.get("vol_ratio")
    if pd.notna(vol_ratio) and vol_ratio > 1.5:
        score += 1
        status, detail = "pass", f"放量 {vol_ratio:.1f}倍"
    elif pd.notna(vol_ratio) and vol_ratio < 0.7:
        score -= 1
        status, detail = "warn", f"缩量 {vol_ratio:.1f}倍"
    else:
        status, detail = "warn", "量能平稳"
    checklist.append(("量能", status, detail))

    # 5. 乖离率
    bias6 = last.get("bias6")
    if pd.notna(bias6) and bias6 < -6:
        score += 1
        status, detail = "pass", f"超跌 {bias6:.1f}%"
    elif pd.notna(bias6) and bias6 > 6:
        score -= 1
        status, detail = "fail", f"超涨 {bias6:.1f}%"
    else:
        status, detail = "warn", "偏离正常"
    checklist.append(("乖离率", status, detail))

    return ScoreResult(score=round(score, 2), checklist=checklist)
