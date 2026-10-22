"""智能建议引擎：自选股实时分析 → 买入 / 卖出 / 持有建议。

流程（与调度器配合，组成自动盯盘体系）:
  - 开盘前(9:20) pre_market : 分析全部自选股 → 今日建议 + 读取昨日「明日关注」
  - 盘中(每5分钟) intraday : 分析全部自选股 → 实时买卖建议
  - 盘后(15:35) post_market: 全市场扫描 → 明日关注 TopN + 自选股分析

分级规则（技术面评分 ±8 + 市场情绪修正）:
  评分 >= +3            → 买入建议
  评分 <= -3            → 卖出建议
  其他                  → 持有观察
  情绪权重 <= -0.5 时，买入建议降级为持有（逆环境谨慎，宁可错过）

止损/目标价沿用 default_stop_target（10 日最低价 / +15%）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.analysis.scoring import score_technical
from app.config import get_settings
from app.data.cache import db
from app.data.service import get_kline_cached
from app.indicators import add_all_indicators
from app.market.sentiment import sentiment_from_snapshot

logger = logging.getLogger("sas.recommender")

#: 最少K线根数（不足则跳过该股）
_MIN_BARS = 30


@dataclass
class Recommendation:
    """一条股票建议。"""

    code: str
    name: str
    action: str               # buy / sell / hold / focus
    score: float              # 技术面评分（未含情绪降级）
    reason: str               # 人类可读理由
    price: float
    stop_loss: float | None
    target_price: float | None
    sentiment: str = ""       # 生成时的市场情绪等级


class Recommender:
    """自选股建议引擎（进程内单例）。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # 单股分析
    # ------------------------------------------------------------------
    async def analyze_stock(self, code: str, name: str) -> Recommendation | None:
        """分析单只自选股，返回建议（数据不足返回 None）。"""
        try:
            df = await asyncio.to_thread(get_kline_cached, code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("建议引擎取K线失败 %s: %s", code, exc)
            return None
        if df is None or df.empty or len(df) < _MIN_BARS:
            return None

        df = await asyncio.to_thread(add_all_indicators, df)
        result = score_technical(df)
        price = float(df["close"].iloc[-1])
        stop = round(float(df["low"].tail(10).min()), 2)
        target = round(price * 1.15, 2)

        # 市场情绪修正：逆环境买入降级为持有
        sentiment_level = ""
        try:
            sentiment = await asyncio.to_thread(
                sentiment_from_snapshot, _safe_snapshot()
            )
            sentiment_level = sentiment.get("level", "")
            weight = float(sentiment.get("weight", 0.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("建议引擎情绪获取失败: %s", exc)
            weight = 0.0

        if result.score >= 3:
            action = "buy"
        elif result.score <= -3:
            action = "sell"
        else:
            action = "hold"
        if weight <= -0.5 and action == "buy":
            action = "hold"

        # 理由：取最强的一正一负信号
        positives = result.reasons(("pass",))
        negatives = result.reasons(("fail",))
        reason_parts = [f"技术面评分 {result.score:+.1f}"]
        if positives:
            reason_parts.append("；".join(positives[:2]))
        if negatives and action in ("sell", "hold"):
            reason_parts.append("；".join(negatives[:2]))
        if sentiment_level:
            reason_parts.append(f"市场情绪{sentiment_level}({weight:+})")
        reason = "，".join(reason_parts)

        return Recommendation(
            code=code, name=name, action=action, score=result.score,
            reason=reason, price=round(price, 2),
            stop_loss=stop, target_price=target, sentiment=sentiment_level,
        )

    # ------------------------------------------------------------------
    # 批量分析（盘中/开盘调用）
    # ------------------------------------------------------------------
    async def analyze_watchlist(self) -> list[Recommendation]:
        """分析全部自选股（并发受信号量限制）。"""
        watchlist = await asyncio.to_thread(db.list_watchlist)
        if not watchlist:
            return []

        semaphore = asyncio.Semaphore(self.settings.scan_concurrency)

        async def one(row) -> Recommendation | None:
            async with semaphore:
                return await self.analyze_stock(row["code"], row["name"])

        results = await asyncio.gather(
            *(one(r) for r in watchlist), return_exceptions=True
        )
        recommendations = [
            r for r in results if isinstance(r, Recommendation)
        ]
        logger.info(
            "自选股分析完成: %d 只 → 买入 %d / 卖出 %d / 持有 %d",
            len(recommendations),
            sum(1 for r in recommendations if r.action == "buy"),
            sum(1 for r in recommendations if r.action == "sell"),
            sum(1 for r in recommendations if r.action == "hold"),
        )
        return recommendations

    # ------------------------------------------------------------------
    # 明日关注（盘后：全市场扫描命中 TopN）
    # ------------------------------------------------------------------
    async def post_market_focus(self, hits: list[dict], top_n: int) -> list[Recommendation]:
        """从收盘扫描命中中挑选明日关注（按信号内评分排序取 TopN）。

        Args:
            hits: 扫描命中列表（signal dict，extra.score 为技术面评分）
            top_n: 关注数量
        """
        scored = []
        for hit in hits:
            score = hit.get("extra", {}).get("score")
            if isinstance(score, (int, float)):
                scored.append((score, hit))
        scored.sort(key=lambda x: x[0], reverse=True)

        focus = []
        for score, hit in scored[:top_n]:
            focus.append(Recommendation(
                code=hit["code"], name=hit.get("name", ""),
                action="focus", score=float(score),
                reason=f"{hit.get('strategy', '')}：{hit.get('reason', '')}",
                price=float(hit.get("price", 0)),
                stop_loss=hit.get("stop_loss"),
                target_price=hit.get("target_price"),
            ))
        return focus


def _safe_snapshot():
    """获取全市场快照（失败时返回空 DataFrame，情绪退化为默认）。"""
    from app.data.snapshot_store import snapshot_store

    try:
        return snapshot_store.get_snapshot()
    except Exception:  # noqa: BLE001
        import pandas as pd

        return pd.DataFrame({"pct_change": []})


# 全局单例
recommender = Recommender()
