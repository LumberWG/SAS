"""P1 四项上下文风险闸：组合集中度/相关性、事件日历、舆情新闻、板块过热。

**这是回测与实盘物理共用的唯一决策入口**（对齐范式照抄 ``app.risk.stops``）。
``app.backtest.pipeline._simulate`` 与 ``app.paper.engine.PaperEngine`` 都只调用
:func:`audit_buy_candidates`，传入「按分数降序的候选代码 + 当前持仓代码 + as_of 日期」，
拿到 ``{code: 拒买原因}`` 后各自过滤自己的候选结构。函数因此完全不认识两侧的
Recommendation / dict 数据结构，也不接受任何 regime / 大盘强弱 / 机制层参数——
历史事故（#45）就是某一侧多了一层门控，参数对齐测不出来。

四项判定（顺序固定，按候选顺序模拟「若买入」的持仓）：
1. ``ctx_event_cooling``   事件日历：as_of 当日及之前的 N 天内发生过财报/解禁/减持/除权/ST变更
2. ``ctx_negative_news``   舆情新闻：as_of 当日及之前窗口内加权舆情分 ≤ -阈值
3. ``ctx_hot_stock``       板块过热：个股 20 日涨幅越过全市场当日 80% 分位
   ``ctx_hot_board``       所在行业热度越过全行业当日 80% 分位（整组剔除）
4. ``ctx_industry_weight`` 组合集中度：买入后同一行业只数占组合比例超上限
   ``ctx_correlation``     与现有持仓平均 |相关系数| 超上限

阈值口径：过热分位从 ``context_heat`` 表按 as_of 读取（由
``scripts/fetch_context_snapshots.py --build-heat`` 从本地日线预计算，只用当日及之前
数据）。两侧读同一张表，因此候选池大小不会影响结果——这一点很关键，实盘队列只有
几只、回测候选几十只，若各自算截面分位就会形成"规则相同、行为不同"的隐性漂移。
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.risk.context_store import ContextStore

# ---- 两侧共用的硬编码常量（放在同一份文件里，天然 1:1，不进参数契约）----
CORR_WINDOW: int = 60          # 相关性回看交易日
HOT_LOOKBACK: int = 20         # 过热判定回看交易日（与 hot20_exclude_probe 一致）
NEWS_LOOKBACK_DAYS: int = 5    # 舆情聚合窗口（自然日）
MIN_CORR_SAMPLES: int = 20     # 收益序列少于此数不算相关性

REASONS: frozenset[str] = frozenset({
    "ctx_event_cooling", "ctx_negative_news", "ctx_hot_stock", "ctx_hot_board",
    "ctx_industry_weight", "ctx_correlation",
})

#: 拒单原因的中文说明，两侧共用（TradeResult / 回测诊断都读这里）
REASON_LABELS: dict[str, str] = {
    "ctx_event_cooling": "上下文闸：近期有财报/解禁/减持等事件，处于冷却期",
    "ctx_negative_news": "上下文闸：近期舆情明显偏空",
    "ctx_hot_stock": "上下文闸：20 日涨幅处于全市场最热区间",
    "ctx_hot_board": "上下文闸：所属行业整体过热，整组剔除",
    "ctx_industry_weight": "上下文闸：买入后同一行业持仓过于集中",
    "ctx_correlation": "上下文闸：与现有持仓走势相关性过高",
}

#: 字段名映射的**唯一来源**：(配置名, 类型)。Settings 用 ``paper_ctx_<name>``，
#: PipelineParams 用 ``context_<name>``，两个 from_* 入口都遍历这张表。
_FIELDS: tuple[tuple[str, str, Any], ...] = (
    ("enabled", "bool", True),
    ("max_industry_weight", "float", 0.40),
    ("max_corr", "float", 0.85),
    ("hot_exclude_pct", "float", 0.20),
    ("event_cooldown_days", "int", 3),
    ("news_block", "float", 0.60),
)


def _coerce(kind: str, value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        if kind == "bool":
            return bool(value) if not isinstance(value, str) else value.strip().lower() in (
                "1", "true", "yes", "on")
        if kind == "int":
            return int(float(value))
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ContextGateConfig:
    """四项闸门的开关与阈值。**0 = 关闭该子闸**（与项目其它 0 语义一致）。"""

    enabled: bool = True
    max_industry_weight: float = 0.40   # 同一行业只数 / 组合总只数上限
    max_corr: float = 0.85              # 与持仓组合平均 |corr| 上限
    hot_exclude_pct: float = 0.20       # 剔除最热的比例（0.20 = 80% 分位）
    event_cooldown_days: int = 3        # 事件后禁买自然日
    news_block: float = 0.60            # 加权舆情分 ≤ -此值禁买

    @classmethod
    def from_settings(cls, s: Any) -> ContextGateConfig:
        """实盘入口：从 ``Settings`` 读（含 app_state 覆盖层）。"""
        return cls(**{
            name: _coerce(kind, getattr(s, "paper_ctx_" + name, None), default)
            for name, kind, default in _FIELDS
        })

    @classmethod
    def from_params(cls, p: Any) -> ContextGateConfig:
        """回测入口：从 ``PipelineParams`` 读。与 from_settings 共用同一张映射表。"""
        return cls(**{
            name: _coerce(kind, getattr(p, "context_" + name, None), default)
            for name, kind, default in _FIELDS
        })

    def active_reasons(self) -> list[str]:
        out = []
        if self.event_cooldown_days > 0:
            out.append("ctx_event_cooling")
        if self.news_block > 0:
            out.append("ctx_negative_news")
        if self.hot_exclude_pct > 0:
            out += ["ctx_hot_stock", "ctx_hot_board"]
        if self.max_industry_weight > 0:
            out.append("ctx_industry_weight")
        if self.max_corr > 0:
            out.append("ctx_correlation")
        return out


def _returns(closes: Sequence[float]) -> tuple[float, ...]:
    if len(closes) < 2:
        return ()
    return tuple(
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    )


def _pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < MIN_CORR_SAMPLES:
        return None
    x, y = list(a[-n:]), list(b[-n:])
    mx, my = statistics.fmean(x), statistics.fmean(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    den = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if den <= 1e-12:
        return None
    return sum(u * v for u, v in zip(dx, dy)) / den


def _mean_abs_corr(
    target: Sequence[float], others: Iterable[Sequence[float]]
) -> float | None:
    vals = [abs(c) for c in (_pearson(target, o) for o in others) if c is not None]
    return statistics.fmean(vals) if vals else None


def audit_buy_candidates(
    codes: Sequence[str],
    *,
    as_of: str,
    held_codes: Sequence[str],
    cfg: ContextGateConfig,
    store: ContextStore,
) -> dict[str, str]:
    """对一批按分数降序的买入候选做上下文风险预筛，返回 ``{code: 拒买原因}``。

    按候选顺序**模拟逐一买入**：先通过的票会进入虚拟持仓，参与后续候选的集中度与
    相关性判定，与实盘逐票下单、回测逐票建仓的顺序一致。
    """
    if not cfg.enabled:
        return {}
    ordered = list(dict.fromkeys(str(c) for c in codes))
    if not ordered:
        return {}
    held = list(dict.fromkeys(str(c) for c in held_codes))
    all_codes = list(dict.fromkeys(ordered + held))
    rejected: dict[str, str] = {}

    # ---- 批量预取：每侧每天只有 2 次 SQL + 1 次热度查表 ----
    events = (
        store.bulk_events(ordered, as_of, cfg.event_cooldown_days)
        if cfg.event_cooldown_days > 0 else {}
    )
    news = (
        store.bulk_news(ordered, as_of, NEWS_LOOKBACK_DAYS)
        if cfg.news_block > 0 else {}
    )
    heat = store.heat_thresholds(as_of) if cfg.hot_exclude_pct > 0 else {}
    stock_cut = heat.get("stock")
    hot_boards = frozenset(heat.get("hot_industries") or ())

    series: dict[str, tuple[float, ...]] = {}
    if cfg.max_corr > 0:
        series = {
            c: _returns(store.closes(c, as_of, CORR_WINDOW + 1))
            for c in all_codes
        }
    industries: dict[str, str] = {}
    if cfg.max_industry_weight > 0 or hot_boards:
        industries = {c: store.industry_of(c) for c in all_codes}

    virtual = list(held)
    for code in ordered:
        # 1) 事件日历：只看 as_of 及之前，零未来函数
        if events.get(code):
            rejected[code] = "ctx_event_cooling"
            continue
        # 2) 舆情新闻：缺快照时 bulk_news 返回空 → 与回测一致地不触发
        if cfg.news_block > 0 and news.get(code, 0.0) <= -abs(cfg.news_block):
            rejected[code] = "ctx_negative_news"
            continue
        # 3) 板块过热：阈值来自两侧共读的 context_heat 表，不依赖候选池大小
        if cfg.hot_exclude_pct > 0:
            ind = industries.get(code) or store.industry_of(code)
            if ind and ind in hot_boards:
                rejected[code] = "ctx_hot_board"
                continue
            if stock_cut is not None:
                r = store.ret(code, as_of, HOT_LOOKBACK)
                if r is not None and r > stock_cut:
                    rejected[code] = "ctx_hot_stock"
                    continue
        # 4) 组合集中度（等权按只数，避免两侧估值价不同造成偏差）。
        #    注意不能用「买入后行业占比 > 上限」：空仓买第一只时占比必然 100%，
        #    会把所有候选全拒。改为「同行业只数 ≤ ceil(权重 × 组合规模)，至少 1 只」。
        if cfg.max_industry_weight > 0:
            trial = virtual + [code]
            ind = industries.get(code) or store.industry_of(code)
            same = sum(
                1 for c in trial
                if (industries.get(c) or store.industry_of(c)) == ind
            )
            limit = max(1, math.ceil(abs(cfg.max_industry_weight) * len(trial)))
            if same > limit:
                rejected[code] = "ctx_industry_weight"
                continue
        # 5) 与持仓组合的平均绝对相关
        if cfg.max_corr > 0 and virtual:
            corr = _mean_abs_corr(series.get(code, ()), (series.get(c, ()) for c in virtual))
            if corr is not None and corr > abs(cfg.max_corr):
                rejected[code] = "ctx_correlation"
                continue
        # 全部通过 → 进入虚拟持仓，参与后续候选的组合判定
        virtual.append(code)
    return rejected
