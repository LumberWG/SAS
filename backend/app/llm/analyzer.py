"""LLM 决策仪表盘生成（借鉴 daily_stock_analysis 的结构化设计）。

流程:
  1. 组装技术面快照（指标 + 行情 + K线摘要）;
  2. 调用 LLM 生成严格 JSON 的分析结论;
  3. 解析校验; 若 LLM 未配置或调用失败,
     降级为纯技术面规则评分结论（保证功能始终可用）。

输出结构（决策仪表盘）:
  一句话结论（买入/持有/卖出）+ 入场价/止损价/目标价 + 多维度检查清单。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.indicators import add_all_indicators
from app.llm.client import llm_client

logger = logging.getLogger("sas.llm.analyzer")

#: 系统提示词：约束 LLM 输出严格 JSON，避免自由发挥
SYSTEM_PROMPT = """你是一位资深A股量化分析师，擅长技术面分析。用户会提供一只股票的最新行情与技术指标快照，你需要输出严格 JSON 格式的分析结论，禁止输出 JSON 以外的任何文字。

输出格式：
{
  "conclusion": "买入 | 持有 | 卖出 三选一",
  "summary": "一句话核心结论，30字以内",
  "entry_price": 建议入场价（数字，基于最新价）,
  "stop_loss": 建议止损价（数字，需低于入场价）,
  "target_price": 建议目标价（数字，需高于入场价）,
  "checklist": [
    {"label": "均线排列", "status": "pass | warn | fail 三选一", "detail": "15字以内说明"}
  ]
}

分析维度（checklist 必须逐项覆盖，共6项）：
1. 均线排列：MA5/MA10/MA20 的多空排列关系
2. MACD动量：DIF与DEA位置、金叉死叉、红绿柱
3. KDJ位置：是否处于超买(>80)超卖(<20)区、金叉死叉
4. 量能：量比与换手率反映的资金关注度
5. 乖离率：价格偏离均线程度，偏离过大有回归风险
6. 布林带：价格处于轨道的上/中/下部

纪律要求：只依据给定数据判断，不得编造数据；价格必须给具体数字；结论谨慎，宁可持有。

市场情绪说明（权重 -1 ~ +1）：
  极度亢奋 +1 / 多头占优 +0.5 / 中性 0 / 空头占优 -0.5 / 极度悲观 -1。
权重 > 0 时趋势策略有效性更高；权重 < 0 时需更严格（弱势中的突破信号失败率高）；
权重为负而个股仍出现买入信号时，应在 summary 中提示逆环境风险。"""


@dataclass
class ChecklistItem:
    """检查清单单项。"""

    label: str
    status: str  # pass / warn / fail
    detail: str


@dataclass
class DecisionDashboard:
    """决策仪表盘（LLM 或规则生成）。"""

    code: str
    name: str
    conclusion: str                       # 买入 / 持有 / 卖出
    summary: str
    entry_price: float | None
    stop_loss: float | None
    target_price: float | None
    checklist: list[ChecklistItem] = field(default_factory=list)
    model: str = ""                       # 生成所用的模型（降级时为 "rule-based"）
    degraded: bool = False                # 是否为规则降级结果
    generated_at: str = ""
    sentiment: dict | None = None         # 市场情绪上下文（可选）

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "conclusion": self.conclusion, "summary": self.summary,
            "entry_price": self.entry_price, "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "checklist": [c.__dict__ for c in self.checklist],
            "model": self.model, "degraded": self.degraded,
            "generated_at": self.generated_at,
            "sentiment": self.sentiment,
        }


class StockAnalyzer:
    """个股分析器：LLM 优先，规则兜底。"""

    def __init__(self) -> None:
        # 注意: 不在此处持有 llm_client 引用，analyze 内直接引用模块级
        # 全局，保证测试可通过 monkeypatch 替换。
        pass

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def analyze(
        self,
        code: str,
        name: str,
        kline: pd.DataFrame,
        snapshot: dict | None = None,
        sentiment: dict | None = None,
    ) -> DecisionDashboard:
        """生成个股决策仪表盘。

        Args:
            code: 股票代码
            name: 股票名称
            kline: 日K线 DataFrame（至少60根，无需含指标列，内部计算）
            snapshot: 实时快照字段（可选，补充量比/换手等）
            sentiment: 市场情绪（可选，{level, weight, advice, temperature}），
                       注入 prompt 作为决策上下文；规则降级时一并返回
        """
        if kline is None or kline.empty or len(kline) < 30:
            raise ValueError("K线数据不足，无法分析")

        df = add_all_indicators(kline)
        snapshot_text = self._build_snapshot_text(code, name, df, snapshot, sentiment)

        if llm_client.available:
            try:
                content = await llm_client.chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": snapshot_text},
                ])
                dashboard = self._parse_dashboard(content, code, name, df, sentiment)
                dashboard.model = llm_client.settings.llm_model
                logger.info("LLM 分析完成 %s %s -> %s",
                            code, name, dashboard.conclusion)
                return dashboard
            except Exception as exc:  # noqa: BLE001 - 任何失败都降级
                logger.warning("LLM 分析失败，降级为规则评分: %s", exc)

        return self._rule_based_dashboard(code, name, df, sentiment)

    # ------------------------------------------------------------------
    # 快照文本组装
    # ------------------------------------------------------------------
    def _build_snapshot_text(
        self,
        code: str,
        name: str,
        df: pd.DataFrame,
        snapshot: dict | None,
        sentiment: dict | None = None,
    ) -> str:
        """把行情与指标组装为 LLM 可读的技术面快照。"""
        last = df.iloc[-1]
        snap = snapshot or {}

        def f(value, fmt="{:.2f}"):
            try:
                if value is None or pd.isna(value):
                    return "N/A"
                return fmt.format(float(value))
            except (ValueError, TypeError):
                return "N/A"

        recent = df.tail(10)[["date", "open", "close", "high", "low", "volume"]]
        kline_lines = "\n".join(
            f"  {r.date} 开{r.open:.2f} 收{r.close:.2f} 高{r.high:.2f} "
            f"低{r.low:.2f} 量{r.volume/1e4:.1f}万手"
            for r in recent.itertuples()
        )

        # 市场情绪上下文（有则附加）
        sentiment_text = ""
        if sentiment and sentiment.get("level"):
            sentiment_text = (
                f"【市场环境】当前市场情绪：{sentiment['level']}"
                f"（权重 {sentiment.get('weight', 0):+}，"
                f"温度 {sentiment.get('temperature', '--')}）"
                f"\n  {sentiment.get('advice', '')}\n\n"
            )

        return f"""{sentiment_text}股票：{name}（{code}）
最新价：{f(last['close'])}  日期：{df['date'].iloc[-1]}

【技术指标快照】
均线：MA5={f(last['ma5'])}  MA10={f(last['ma10'])}  MA20={f(last['ma20'])}
MACD：DIF={f(last['dif'], '{:.3f}')}  DEA={f(last['dea'], '{:.3f}')}  柱={f(last['macd_hist'], '{:.3f}')}
KDJ：K={f(last['kdj_k'], '{:.1f}')}  D={f(last['kdj_d'], '{:.1f}')}  J={f(last['kdj_j'], '{:.1f}')}
RSI：RSI6={f(last['rsi6'], '{:.1f}')}  RSI12={f(last['rsi12'], '{:.1f}')}
BOLL：上轨={f(last['boll_up'])}  中轨={f(last['boll_mid'])}  下轨={f(last['boll_dn'])}
乖离率：BIAS6={f(last['bias6'], '{:.2f}')}%  BIAS12={f(last['bias12'], '{:.2f}')}%
ATR(14)={f(last['atr'], '{:.3f}')}
量比(估算)={f(last.get('vol_ratio'), '{:.2f}')}
换手率={f(snap.get('turnover_rate'), '{:.2f}')}%  总市值={f(snap.get('total_mv'), '{:.1f}')}亿

【近10日K线】
{kline_lines}"""

    # ------------------------------------------------------------------
    # LLM 输出解析
    # ------------------------------------------------------------------
    _JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

    def _parse_dashboard(
        self,
        content: str,
        code: str,
        name: str,
        df: pd.DataFrame,
        sentiment: dict | None = None,
    ) -> DecisionDashboard:
        """解析 LLM 输出的 JSON（容忍 ```json 包裹与杂散文本）。"""
        match = self._JSON_RE.search(content)
        if not match:
            raise ValueError("LLM 输出中未找到 JSON")
        data = json.loads(match.group(0))

        conclusion = str(data.get("conclusion", "")).strip()
        if conclusion not in ("买入", "持有", "卖出"):
            raise ValueError(f"非法结论: {conclusion}")

        checklist = []
        for item in data.get("checklist", []):
            status = str(item.get("status", "")).strip()
            if status not in ("pass", "warn", "fail"):
                status = "warn"
            checklist.append(ChecklistItem(
                label=str(item.get("label", "")),
                status=status,
                detail=str(item.get("detail", "")),
            ))

        price = float(df["close"].iloc[-1])
        return DecisionDashboard(
            code=code, name=name, conclusion=conclusion,
            summary=str(data.get("summary", "")),
            entry_price=_num_or_none(data.get("entry_price"), price),
            stop_loss=_num_or_none(data.get("stop_loss")),
            target_price=_num_or_none(data.get("target_price")),
            checklist=checklist,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            sentiment=sentiment,
        )

    # ------------------------------------------------------------------
    # 规则降级（纯技术面评分）
    # ------------------------------------------------------------------
    def _rule_based_dashboard(
        self,
        code: str,
        name: str,
        df: pd.DataFrame,
        sentiment: dict | None = None,
    ) -> DecisionDashboard:
        """无 LLM 时的规则评分仪表盘。

        评分体系（满分 ±8）:
          均线多头排列(MA5>MA10>MA20) +2 / 空头 -2
          MACD 金叉且柱>0 +1.5 / 死叉且柱<0 -1.5
          KDJ 超卖(J<20) +1.5 / 超买(J>80) -1.5
          量比>1.5 放量 +1 / <0.7 缩量 -1
          乖离 BIAS6<-6% 超跌 +1 / >6% 超涨 -1
        结论: >=3 买入, <=-3 卖出, 否则持有。

        市场情绪（权重 -1 ~ +1）:
          仅作为上下文写入结论说明，**不再把「买入」降级为「持有」**：
          弱市常有更好赔率（别人恐惧时贪婪），是否买入交给技术面评分判断。
        """
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        price = float(last["close"])
        checklist: list[ChecklistItem] = []
        score = 0.0

        # 1. 均线排列
        if last["ma5"] > last["ma10"] > last["ma20"]:
            score += 2; status = "pass"; detail = "多头排列"
        elif last["ma5"] < last["ma10"] < last["ma20"]:
            score -= 2; status = "fail"; detail = "空头排列"
        else:
            status = "warn"; detail = "交织粘合"
        checklist.append(ChecklistItem("均线排列", status, detail))

        # 2. MACD
        golden = prev["dif"] <= prev["dea"] and last["dif"] > last["dea"]
        dead = prev["dif"] >= prev["dea"] and last["dif"] < last["dea"]
        if golden and last["macd_hist"] > 0:
            score += 1.5; status = "pass"; detail = "金叉红柱"
        elif dead and last["macd_hist"] < 0:
            score -= 1.5; status = "fail"; detail = "死叉绿柱"
        else:
            status = "warn"; detail = "无明显交叉"
        checklist.append(ChecklistItem("MACD动量", status, detail))

        # 3. KDJ
        if last["kdj_j"] < 20:
            score += 1.5; status = "pass"; detail = f"超卖 J={last['kdj_j']:.0f}"
        elif last["kdj_j"] > 80:
            score -= 1.5; status = "fail"; detail = f"超买 J={last['kdj_j']:.0f}"
        else:
            status = "warn"; detail = f"中位 J={last['kdj_j']:.0f}"
        checklist.append(ChecklistItem("KDJ位置", status, detail))

        # 4. 量能（量比）
        vol_ratio = last.get("vol_ratio")
        if pd.notna(vol_ratio) and vol_ratio > 1.5:
            score += 1; status = "pass"; detail = f"放量 {vol_ratio:.1f}倍"
        elif pd.notna(vol_ratio) and vol_ratio < 0.7:
            score -= 1; status = "warn"; detail = f"缩量 {vol_ratio:.1f}倍"
        else:
            status = "warn"; detail = "量能平稳"
        checklist.append(ChecklistItem("量能", status, detail))

        # 5. 乖离率
        bias6 = last.get("bias6")
        if pd.notna(bias6) and bias6 < -6:
            score += 1; status = "pass"; detail = f"超跌 {bias6:.1f}%"
        elif pd.notna(bias6) and bias6 > 6:
            score -= 1; status = "fail"; detail = f"超涨 {bias6:.1f}%"
        else:
            status = "warn"; detail = "偏离正常"
        checklist.append(ChecklistItem("乖离率", status, detail))

        # 6. 布林带位置
        if price > last["boll_up"]:
            status = "warn"; detail = "触及上轨"
        elif price < last["boll_dn"]:
            status = "warn"; detail = "触及下轨"
        else:
            status = "pass"; detail = "轨道内运行"
        checklist.append(ChecklistItem("布林带位置", status, detail))

        if score >= 3:
            conclusion = "买入"
        elif score <= -3:
            conclusion = "卖出"
        else:
            conclusion = "持有"

        # 市场情绪：只作上下文提示，不否决买入
        sentiment_note = ""
        if sentiment and sentiment.get("level"):
            sentiment_weight = float(sentiment.get("weight", 0.0))
            hint = "弱市博弈，控制单笔仓位" if sentiment_weight < 0 else "环境偏暖"
            sentiment_note = (
                f"；市场情绪{sentiment['level']}（权重{sentiment_weight:+}），{hint}"
            )

        stop = round(float(df["low"].tail(10).min()), 2)
        target = round(price * 1.15, 2)
        return DecisionDashboard(
            code=code, name=name, conclusion=conclusion,
            summary=f"技术面评分 {score:+.1f}，倾向{conclusion}"
                    f"{sentiment_note}（规则降级结论）",
            entry_price=round(price, 2), stop_loss=stop, target_price=target,
            checklist=checklist, model="rule-based", degraded=True,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            sentiment=sentiment,
        )


def _num_or_none(value, default: float | None = None) -> float | None:
    """安全数值转换（None/NaN → default）。"""
    try:
        if value is None:
            return default
        result = float(value)
        return default if pd.isna(result) else result
    except (ValueError, TypeError):
        return default


# 全局单例
analyzer = StockAnalyzer()
