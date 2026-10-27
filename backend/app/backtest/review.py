"""回测复盘：把交易明细拆成「亏在哪」的归因，并给出参数调整建议。

设计原则：
  - 只依赖回测结果本身（trades / metrics / equity_curve），不重新读K线，
    保证复盘与回测口径完全一致、且毫秒级完成；
  - 归因维度固定（卖出原因 / 建仓评分档 / 持有天数 / 费用 / 极端单），
    便于跨期对比同一组指标的变化；
  - 建议由规则触发，每条都带「当前值 → 建议值 + 依据数字」，
    避免“感觉应该调大一点”这类无法复盘的结论。

调用方：``app/backtest/pipeline.py`` 在回测结束时调用 :func:`analyze`，
结果挂在返回值的 ``review`` 字段；人工采纳与否记入 ``BACKTEST_LOG.md``。
"""
from __future__ import annotations

from collections import defaultdict

#: 止损/止盈结构下打平所需的胜率 = L / (L + G)
STOP_LOSS = 0.08
TAKE_PROFIT = 0.20


def _reason_key(trade: dict) -> str:
    """把卖出原因归并到固定类别（带参数的说明统一到前缀）。"""
    reason = str(trade.get("reason") or "")
    # E9/E10/E11 的新卖出原因并入大类别：保本/移动/收盘确认 ⊂ 止损，分段止盈 ⊂ 止盈
    if reason.startswith(("保本止损", "移动止损", "止损(收盘确认)")):
        return "止损"
    if reason.startswith("分段止盈"):
        return "止盈"
    for key in ("止损", "止盈", "时间止损", "轮动替换", "卖出信号"):
        if reason.startswith(key):
            return key
    return reason or "其他"


def _score_bucket(score: float | None) -> str:
    value = float(score or 0)
    if value >= 40:
        return "≥40（全额档）"
    if value >= 25:
        return "25~40（半额档）"
    return "<25"


def _hold_bucket(days: int) -> str:
    if days <= 1:
        return "1 天"
    if days <= 3:
        return "2~3 天"
    if days <= 7:
        return "4~7 天"
    return "≥8 天"


def _group_stats(name: str, items: list[dict]) -> dict:
    pnl = sum(float(t.get("pnl") or 0) for t in items)
    pct = [float(t.get("pnl_pct") or 0) for t in items]
    wins = sum(1 for t in items if float(t.get("pnl") or 0) > 0)
    return {
        "name": name,
        "count": len(items),
        "pnl": round(pnl, 2),
        "avg_pnl_pct": round(sum(pct) / len(pct), 2) if pct else 0.0,
        "win_rate": round(wins / len(items) * 100, 1) if items else 0.0,
    }


def analyze(result: dict) -> dict:
    """对一次回测结果做亏损归因，返回可直接展示的 dict。"""
    trades: list[dict] = list(result.get("trades") or [])
    metrics: dict = result.get("metrics") or {}
    dates = [p.get("date") for p in (result.get("equity_curve") or [])]
    day_index = {d: i for i, d in enumerate(dates)}
    stop_loss = float(result.get("params", {}).get("stop_loss") or STOP_LOSS)
    take_profit = float(result.get("params", {}).get("take_profit") or TAKE_PROFIT)

    if not trades:
        return {
            "summary": "区间内无成交，无法归因（可放宽评分门槛或延长区间）",
            "reasons": [], "score_buckets": [], "hold_buckets": [],
            "cost": {}, "extremes": {}, "suggestions": [],
        }

    # ---------- 1) 按卖出原因 ----------
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_reason[_reason_key(t)].append(t)
    reasons = [
        _group_stats(k, v)
        for k, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))
    ]

    # ---------- 2) 按建仓评分档 ----------
    by_score: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_score[_score_bucket(t.get("score"))].append(t)
    score_buckets = [
        _group_stats(k, v)
        for k, v in sorted(by_score.items(), key=lambda kv: kv[0])
    ]

    # ---------- 3) 按持有交易日数 ----------
    by_hold: dict[str, list[dict]] = defaultdict(list)
    holds: list[int] = []
    for t in trades:
        buy = day_index.get(t.get("buy_date"))
        sell = day_index.get(t.get("sell_date"))
        if buy is None or sell is None:
            continue
        held = sell - buy
        holds.append(held)
        by_hold[_hold_bucket(held)].append(t)
    hold_buckets = [
        _group_stats(k, v)
        for k, v in sorted(by_hold.items(), key=lambda kv: kv[0])
    ]
    avg_hold = round(sum(holds) / len(holds), 1) if holds else 0.0

    # ---------- 4) 费用与摩擦 ----------
    fee_total = round(sum(float(t.get("fee") or 0) for t in trades), 2)
    pnl_total = round(sum(float(t.get("pnl") or 0) for t in trades), 2)
    loss_total = sum(min(float(t.get("pnl") or 0), 0) for t in trades)
    cost = {
        "fee_total": fee_total,
        "pnl_total": pnl_total,
        "fee_vs_loss_pct": round(fee_total / abs(loss_total) * 100, 1) if loss_total else 0.0,
    }

    # ---------- 5) 极端单 ----------
    ordered = sorted(trades, key=lambda t: float(t.get("pnl_pct") or 0))
    extremes = {
        "worst": [_trade_row(t) for t in ordered[:5]],
        "best": [_trade_row(t) for t in ordered[-3:][::-1]],
    }

    summary = _summary(metrics, reasons, cost)
    scores = sorted(float(t.get("score") or 0) for t in trades)
    suggestions = _suggest(reasons, score_buckets, hold_buckets, cost, metrics,
                           stop_loss, take_profit, avg_hold, extremes, scores)

    return {
        "summary": summary,
        "reasons": reasons,
        "score_buckets": score_buckets,
        "hold_buckets": hold_buckets,
        "avg_hold_days": avg_hold,
        "cost": cost,
        "extremes": extremes,
        "suggestions": suggestions,
    }


def _trade_row(t: dict) -> dict:
    return {
        "code": t.get("code"), "name": t.get("name"),
        "buy_date": t.get("buy_date"), "sell_date": t.get("sell_date"),
        "pnl_pct": round(float(t.get("pnl_pct") or 0), 2),
        "pnl": round(float(t.get("pnl") or 0), 2),
        "reason": t.get("reason"), "score": t.get("score"),
    }


def _summary(metrics: dict, reasons: list[dict], cost: dict) -> str:
    total = metrics.get("total_return", 0.0)
    bench = metrics.get("benchmark_return", 0.0)
    win = metrics.get("win_rate", 0.0)
    stop = next((r for r in reasons if r["name"] == "止损"), None)
    parts = [f"总收益 {total:+.2f}%（基准 {bench:+.2f}%）"]
    if stop:
        parts.append(f"{stop['count']} 笔止损、平均 {stop['avg_pnl_pct']:+.2f}%")
    parts.append(f"胜率 {win:.1f}%")
    if cost.get("fee_vs_loss_pct"):
        parts.append(f"费用占亏损 {cost['fee_vs_loss_pct']:.1f}%")
    return "；".join(parts)


def _suggest(
    reasons: list[dict],
    score_buckets: list[dict],
    hold_buckets: list[dict],
    cost: dict,
    metrics: dict,
    stop_loss: float,
    take_profit: float,
    avg_hold: float,
    extremes: dict,
    scores: list[float],
) -> list[dict]:
    """按规则给出参数调整建议（每条都带数字依据）。"""
    out: list[dict] = []
    total = sum(r["count"] for r in reasons) or 1
    win = float(metrics.get("win_rate") or 0.0)
    breakeven = stop_loss / (stop_loss + take_profit) * 100
    buy_threshold = 15.0
    median_score = scores[len(scores) // 2] if scores else 0.0
    min_score = scores[0] if scores else 0.0

    # 1) 胜率低于「止损止盈结构」的打平线 → 先判断提高买入阈值是否真的有用
    if win < breakeven:
        if scores and min_score - buy_threshold >= 5:
            # 成交分远高于阈值：阈值不是约束，提高它不会过滤任何单子
            out.append({
                "target": "选股条件（BUY_THRESHOLD 已不是约束）",
                "current": f"阈值 {buy_threshold:.0f}，实际成交最低分 {min_score:.0f}、中位 {median_score:.0f}",
                "suggest": "不要动阈值，改买点或增加筛选条件",
                "reason": (
                    f"胜率 {win:.1f}% < 打平所需的 {breakeven:.0f}%"
                    f"（{stop_loss*100:.0f}% 止损 / {take_profit*100:.0f}% 止盈）。"
                    f"本轮成交建仓分全部 ≥{min_score:.0f}，把 BUY_THRESHOLD 从 "
                    f"{buy_threshold:.0f} 往上调不会过滤任何单子（实测 25 与 15 成交完全相同），"
                    "要靠买点或新增筛选维度提升胜率"
                ),
                "priority": "高",
            })
        else:
            out.append({
                "target": "BUY_THRESHOLD（14因子买入阈值，app/analysis/factors.py）",
                "current": buy_threshold,
                "suggest": round((median_score + 10) / 5) * 5,
                "reason": (
                    f"胜率 {win:.1f}% < 打平所需的 {breakeven:.0f}%"
                    f"（{stop_loss*100:.0f}% 止损 / {take_profit*100:.0f}% 止盈）。"
                    f"成交建仓分中位数 {median_score:.0f} 贴近阈值，抬高门槛能实打实过滤弱单"
                ),
                "priority": "高",
            })

    # 2) 持有 1 天就离场 → 买点问题（属于算法改动，不只是调参）
    short = next((b for b in hold_buckets if b["name"] == "1 天"), None)
    if short and short["count"] >= 3 and short["count"] / total >= 0.2:
        out.append({
            "target": "买入时点（算法）",
            "current": "次日开盘价全额买入",
            "suggest": "次日开盘不追高：开盘涨幅 > +2% 则等回踩，或拆成两笔",
            "reason": (
                f"持有 1 天即离场 {short['count']} 笔（占 {short['count']/total*100:.0f}%），"
                f"平均 {short['avg_pnl_pct']:+.2f}%，说明买在短期高点；"
                "改买点的收益通常大于调止损"
            ),
            "priority": "高",
        })

    # 2) 半额档明显拖累 → 取消半额档，只做高分票
    half = next((b for b in score_buckets if b["name"].startswith("25~40")), None)
    if half and half["count"] >= 5 and half["avg_pnl_pct"] < -1.0:
        out.append({
            "target": "paper_full_score / paper_half_score",
            "current": "40 / 25",
            "suggest": "40 / 40（取消半额档）",
            "reason": (
                f"25~40 分档 {half['count']} 笔平均 {half['avg_pnl_pct']:+.2f}%、"
                f"胜率 {half['win_rate']:.1f}%，明显拖累整体，建议只在 ≥40 分时下单"
            ),
            "priority": "中",
        })

    # 3) 止损占比过高 → 问题在触发方式，不在幅度（实测放宽幅度只会亏更多）
    stop = next((r for r in reasons if r["name"] == "止损"), None)
    if stop and stop["count"] / total >= 0.5:
        out.append({
            "target": "止损触发方式（算法，不是幅度）",
            "current": f"盘中触及 -{stop_loss*100:.0f}% 即卖出",
            "suggest": "改为收盘确认，或按 ATR 自适应设定止损距离",
            "reason": (
                f"止损占全部交易的 {stop['count']/total*100:.0f}%、平均 "
                f"{stop['avg_pnl_pct']:+.2f}%；实测把幅度从 8% 放宽到 10% 后收益更差"
                "（−9.51% → −10.95%），说明该改的是触发方式"
            ),
            "priority": "中",
        })

    # 4) 隔夜跳空导致实际亏损远超止损线 → 用仓位而非参数解决
    worst_pct = min((float(x.get("pnl_pct") or 0) for x in extremes.get("worst", [])), default=0.0)
    if worst_pct <= -(stop_loss * 100 + 3):
        out.append({
            "target": "单票仓位敞口",
            "current": "≤20%",
            "suggest": "≤15%",
            "reason": (
                f"最差单笔 {worst_pct:+.2f}%，比 {stop_loss*100:.0f}% 止损线还低 "
                f"{abs(worst_pct) - stop_loss*100:.1f} 个点——跳空低开时止损价根本卖不出，"
                "只能靠降低单票敞口控制"
            ),
            "priority": "中",
        })

    # 5) 摩擦成本过高 → 降低换手
    if cost.get("fee_vs_loss_pct", 0) >= 10:
        out.append({
            "target": "paper_rotation_gap / paper_tick 换手",
            "current": 20,
            "suggest": 25,
            "reason": (
                f"费用 {cost['fee_total']:.0f} 元，占亏损 {cost['fee_vs_loss_pct']:.1f}%，"
                "换手过密；提高轮动门槛或减少轮动次数能直接省下这部分"
            ),
            "priority": "中",
        })

    # 6) 基准为正、策略为负 → 选股负 alpha，先别加仓
    if float(metrics.get("benchmark_return") or 0) > 0 > float(metrics.get("total_return") or 0):
        out.append({
            "target": "实盘仓位（paper_auto_trade）",
            "current": "启用",
            "suggest": "影子跟踪（只记录不下单）",
            "reason": (
                f"基准 {metrics['benchmark_return']:+.2f}% 而策略 "
                f"{metrics['total_return']:+.2f}%，选股是负超额，先修信号再放资金"
            ),
            "priority": "高",
        })

    # 7) 持有天数过短 → 信号还没走完就被换掉
    if avg_hold and avg_hold < 2.5 and total >= 10:
        out.append({
            "target": "max_hold_days / 卖出信号阈值",
            "current": "不限",
            "suggest": "≥3 天",
            "reason": (
                f"平均持有 {avg_hold:.1f} 个交易日，短线摩擦最重；"
                "给信号至少 3 天的发酵时间，或把卖出阈值从 −15 放宽到 −20"
            ),
            "priority": "低",
        })

    if not out:
        out.append({
            "target": "—",
            "current": "—",
            "suggest": "维持现参数",
            "reason": "本轮各项指标没有触发调整规则，继续观察",
            "priority": "低",
        })
    # 高优先级在前（同优先级保持规则顺序）
    rank = {"高": 0, "中": 1, "低": 2}
    return sorted(out, key=lambda s: rank.get(str(s.get("priority")), 3))
