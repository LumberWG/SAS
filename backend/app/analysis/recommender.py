"""智能建议引擎：自选股实时分析 → 买入 / 卖出 / 持有建议。

流程（与调度器配合，组成自动盯盘体系）:
  - 开盘前(9:20) pre_market : 分析全部自选股 → 今日建议 + 读取昨日「明日关注」
  - 盘中(每5分钟) intraday : 分析全部自选股 → 实时买卖建议
  - 盘后(15:35) post_market: 全市场扫描 → 明日关注 TopN + 自选股分析

分级规则（多因子总分 ±100，见 app/analysis/factors.py）:
  总分 >= +15           → 买入建议
  总分 <= -15           → 卖出建议
  其他                  → 持有观察

市场情绪只作为上下文写入理由，**不再一票否决买入**：
弱市往往伴随更好的赔率（别人恐惧时贪婪），「大落大机会」由因子模型里的
超跌/乖离/KDJ 等分项自行表达，而不是在宏观层面直接禁止买入。

一次分析产出的建议会按总分降序排名（rank 从 1 开始），并把各因子分项写入
detail（JSON 字符串），供前端展示「为什么排这个位置」。

止损/目标价沿用 default_stop_target（10 日最低价 / +15%）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.analysis.factors import compute_factors
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
    score: float              # 多因子总分 [-100, 100]（未含情绪降级）
    reason: str               # 人类可读理由
    price: float
    stop_loss: float | None
    target_price: float | None
    sentiment: str = ""       # 生成时的市场情绪等级
    rank: int = 0             # 本轮自选股排序名次（1 起，0=未排名）
    detail: str = ""          # 因子分项明细（JSON 字符串）


class Recommender:
    """自选股建议引擎（进程内单例）。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # 单股分析
    # ------------------------------------------------------------------
    async def analyze_stock(
        self,
        code: str,
        name: str,
        current_price: float | None = None,
        sentiment: dict | None = None,
        ml_scores: dict | None = None,
    ) -> Recommendation | None:
        """分析单只自选股，返回建议（数据不足返回 None）。

        Args:
            current_price: 当前实时价（来自实时快照）。盘中日K缓存最后一根可能
                还是上一交易日，用它当建议价会记录隔夜价格；缺失时退化为
                日K最后收盘价。
            sentiment: 市场情绪（一轮分析内复用同一份，避免逐股重复读快照）
        """
        try:
            df = await asyncio.to_thread(get_kline_cached, code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("建议引擎取K线失败 %s: %s", code, exc)
            return None
        if df is None or df.empty or len(df) < _MIN_BARS:
            return None

        df = await asyncio.to_thread(add_all_indicators, df)
        result = await asyncio.to_thread(compute_factors, df)
        # 建议价：优先当前实时价，缺失才用K线最后收盘
        price = (
            float(current_price)
            if current_price is not None and current_price > 0
            else float(df["close"].iloc[-1])
        )
        stop = round(float(df["low"].tail(10).min()), 2)
        target = round(price * 1.15, 2)

        # 市场情绪修正：逆环境买入降级为持有
        try:
            data = (
                sentiment
                if sentiment is not None
                else await asyncio.to_thread(market_sentiment)
            )
            sentiment_level = data.get("level", "")
            weight = float(data.get("weight", 0.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("建议引擎情绪获取失败: %s", exc)
            sentiment_level = ""
            weight = 0.0

        # 按 ±15 分阈值给出 buy/sell/hold；情绪不再降级（仅写入理由）
        action = result.action

        # 理由：取贡献最大的正/负因子
        positives = result.top_reasons(positive=True)
        negatives = result.top_reasons(positive=False)
        reason_parts = [f"综合评分 {result.total:+.0f}"]
        if positives:
            reason_parts.append("；".join(positives))
        if negatives and action in ("sell", "hold"):
            reason_parts.append("；".join(negatives))
        if sentiment_level:
            reason_parts.append(f"市场情绪{sentiment_level}({weight:+})")

        # —— 线上 ML 打分覆盖（default_ml 实时版）——
        # 启用且当日有 ML 分位时，用 ML 分位(0~100) 替换 14 因子分做排序/仓位定价；
        # 买入触发保留因子信号作安全网：ML 分位达半额线才判 buy，因子卖出仍优先。
        ml_score = (
            float(ml_scores[code])
            if ml_scores and code in ml_scores and ml_scores[code] is not None
            else None
        )
        if ml_score is not None:
            score = ml_score
            action = (
                "buy" if ml_score >= self.settings.paper_half_score
                else ("sell" if result.action == "sell" else "hold")
            )
            reason_parts[0] = f"ML打分 {ml_score:.0f}（14因子 {result.total:+.0f}）"
        else:
            score = result.total

        reason = "，".join(reason_parts)

        return Recommendation(
            code=code, name=name, action=action, score=score,
            reason=reason, price=round(price, 2),
            stop_loss=stop, target_price=target, sentiment=sentiment_level,
            detail=json.dumps(result.breakdown(), ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # 批量分析（盘中/开盘调用）
    # ------------------------------------------------------------------
    async def analyze_watchlist(self) -> list[Recommendation]:
        """分析全部自选股（并发受信号量限制）。

        范围外的票（科创板 688 / 北交所 / ST / 巨无霸）先剔掉再分析：它们本来就买不了，
        留在分析里只会在「建议买入」和「交易池 · 切入点判定」里长期占位
        （曾出现 54 只 688 常驻面板，且被大盘弱势闸门挡成「暂停买入」，看不出真实原因）。
        已持仓的例外——仍然需要卖出建议。
        """
        from app.data.scope import is_tradable
        from app.paper.engine import large_cap_rule

        watchlist = await asyncio.to_thread(db.list_watchlist)
        if not watchlist:
            return []
        held = {p["code"] for p in await asyncio.to_thread(db.list_paper_positions)}
        large_on, _limit = large_cap_rule()
        before = len(watchlist)
        watchlist = [
            row for row in watchlist
            if str(row["code"]) in held
            or is_tradable(str(row["code"]), str(row.get("name") or ""), exclude_giant=large_on)[0]
        ]
        if len(watchlist) < before:
            logger.info("自选分析跳过 %d 只范围外股票（科创板/北交所/ST/大盘股）", before - len(watchlist))

        # 实时价与市场情绪：一轮只取一次，供全部股票复用
        quotes, sentiment = await asyncio.gather(
            asyncio.to_thread(_quote_prices),
            asyncio.to_thread(market_sentiment),
        )

        # 线上 ML 打分（default_ml 实时版）：整批只构建一次，逐只查表。
        # 仅在 settings.live_ml_scoring 时启用；失败/无信号则回退 14 因子（不影响本轮）。
        ml_scores: dict[str, float] = {}
        if self.settings.live_ml_scoring:
            try:
                from app.analysis.live_ml import get_live_ml_scores

                codes = [str(r["code"]) for r in watchlist]
                ml_scores = await asyncio.to_thread(get_live_ml_scores, codes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("线上 ML 打分构建失败，本批回退 14 因子: %s", exc)
                ml_scores = {}

        semaphore = asyncio.Semaphore(self.settings.scan_concurrency)

        async def one(row) -> Recommendation | None:
            async with semaphore:
                return await self.analyze_stock(
                    row["code"], row["name"],
                    quotes.get(str(row["code"])), sentiment, ml_scores,
                )

        results = await asyncio.gather(
            *(one(r) for r in watchlist), return_exceptions=True
        )
        recommendations = [
            r for r in results if isinstance(r, Recommendation)
        ]
        # 第 2 次排序：按多因子总分降序，名次从 1 开始（rank=0 表示未参与排序）
        recommendations.sort(key=lambda r: r.score, reverse=True)
        for idx, rec in enumerate(recommendations, 1):
            rec.rank = idx

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


def _quote_prices() -> dict[str, float]:
    """当前实时价映射（code → price），快照不可用时返回空 dict。"""
    from app.data.snapshot_store import quote_map

    try:
        return {code: q["price"] for code, q in quote_map().items()}
    except Exception as exc:  # noqa: BLE001 - 快照不可用则退回K线收盘价
        logger.debug("建议引擎实时价获取失败: %s", exc)
        return {}


def market_sentiment() -> dict:
    """当前市场情绪（level/weight），失败时返回中性默认值。

    一轮分析只调用一次：盘中每分钟一轮时，逐股读快照既慢又无意义。
    """
    try:
        return sentiment_from_snapshot(_safe_snapshot())
    except Exception as exc:  # noqa: BLE001
        logger.debug("市场情绪获取失败: %s", exc)
        return {"level": "", "weight": 0.0}


def _safe_snapshot():
    """获取全市场快照（失败时返回空 DataFrame，情绪退化为默认）。

    优先读内存缓存（``peek_snapshot`` 不阻塞、不触发刷新）；仅当完全无缓存
    （冷启动且无持久化文件）时才同步拉取一次。
    """
    from app.data.snapshot_store import snapshot_store

    try:
        snapshot = snapshot_store.peek_snapshot()
        if snapshot is None or snapshot.empty:
            snapshot = snapshot_store.get_snapshot()
        return snapshot
    except Exception:  # noqa: BLE001
        import pandas as pd

        return pd.DataFrame({"pct_change": []})


# 全局单例
recommender = Recommender()
