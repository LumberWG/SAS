"""卖出出口自检：单一来源的纯函数。

实盘 ``PaperEngine.audit_exit_paths()`` 与 ``scripts/check_exit_paths.py`` 共用本模块，
避免「接口说有出口、巡检脚本说没有」两套口径。本模块只读、不碰数据库、不下单，
也不参与任何策略规则，因此回测侧无需同步（不属于 1:1 契约的处置参数）。

出口清单按实盘代码逐条对应：

- ``stops``      —— 每 tick 无条件的止损/止盈/时间止损（``check_stop_loss_target``）
- ``signal``     —— 队列里的卖出建议（开盘/盘中 ``analyze_watchlist``，依赖自选）
- ``eod_trim``   —— 收盘前按最弱优先减仓（``eod_trim``，排除手动持仓）
- ``rotation``   —— 轮动换出（``_weakest_rotatable``，排除手动持仓）
- ``offboard``   —— 清非交易板块持仓（``liquidate_offboard_positions``）
- ``manual``     —— 人工下单（``POST /api/paper/order``；无行情时按成本价兜底）

真正的僵尸仓只有一种：可卖批次已经到期（T+1 已过）却卖不掉，或所有出口都被关掉。
当日买入 ``sellable_shares=0`` 是正常的 T+1，次日由 ``ensure_lots_migrated`` 自动恢复，
不算无出口。
"""
from __future__ import annotations

from datetime import date
from typing import Any


def _days_since(day: str, today: str) -> int:
    try:
        return max(0, (date.fromisoformat(today) - date.fromisoformat(day)).days)
    except (TypeError, ValueError):
        return 0


def audit_positions(
    *,
    positions: list[dict[str, Any]],
    lots_by_code: dict[str, list[dict[str, Any]]],
    watch_codes: set[str],
    prices: dict[str, float | None],
    today: str,
    board_prefixes: tuple[str, ...],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """逐只持仓核对卖出出口；返回可直接序列化的报告。

    ``cfg`` 键：``auto_trade`` / ``eod_trim`` / ``rotation`` /
    ``liquidate_offboard`` / ``max_hold_days`` / ``stop_loss`` / ``take_profit``。
    """
    auto_ok = bool(cfg.get("auto_trade", True))
    prefixes = tuple(board_prefixes or ())
    items: list[dict[str, Any]] = []

    for p in positions:
        code = str(p.get("code") or "").zfill(6)
        name = str(p.get("name") or code)
        shares = int(p.get("shares") or 0)
        sellable = int(p.get("sellable_shares") or 0)
        buy_date = str(p.get("buy_date") or "")
        strategy = str(p.get("strategy") or "")
        is_manual = "手动" in strategy
        price = prices.get(code)
        has_price = bool(price and price > 0)
        watched = code in watch_codes
        offboard = bool(prefixes) and not code.startswith(prefixes)
        lots = lots_by_code.get(code) or []
        t1_ready = sellable > 0
        t1_pending = (not t1_ready) and bool(buy_date) and buy_date >= today

        exits = {
            "stops": auto_ok and t1_ready and has_price,
            "signal": auto_ok and t1_ready and has_price and watched,
            "eod_trim": (
                auto_ok and bool(cfg.get("eod_trim")) and t1_ready and has_price
                and not is_manual and bool(buy_date) and buy_date < today
            ),
            "rotation": (
                auto_ok and bool(cfg.get("rotation")) and t1_ready and has_price
                and not is_manual and bool(buy_date) and buy_date < today
            ),
            "offboard": (
                auto_ok and bool(cfg.get("liquidate_offboard")) and offboard
                and t1_ready and has_price
            ),
            # 人工出口：无行情时按成本价兜底，所以只要求 T+1 可卖批次。
            "manual": t1_ready,
        }
        available = [k for k, ok in exits.items() if ok]

        risks: list[str] = []
        if not t1_ready and buy_date and buy_date < today:
            risks.append("T+1 已到期但可卖股数仍为 0（批次缺失或跨日未刷新）")
        if not lots:
            risks.append("无建仓批次记录（可卖股数无法自动恢复）")
        if not has_price:
            risks.append("取不到行情价：自动卖出全部跳过，只剩人工兜底出口")
        if not watched:
            risks.append("不在自选：卖出建议不覆盖该票")
        if not auto_ok:
            risks.append("手动交易模式：每 tick 自动卖出停用，只有人工下单")
        if t1_pending:
            risks.append("T+1 当日买入不可卖（正常，次日自动恢复）")

        if available:
            status = "ok"
        elif t1_pending:
            status = "t1_pending"
        else:
            status = "blocked"

        items.append({
            "code": code,
            "name": name,
            "shares": shares,
            "sellable_shares": sellable,
            "buy_date": buy_date,
            "held_days": _days_since(buy_date, today) if buy_date else 0,
            "price": round(float(price), 4) if has_price else None,
            "in_watchlist": watched,
            "strategy": strategy,
            "offboard": offboard,
            "exits": exits,
            "available_exits": available,
            "status": status,
            "risks": risks,
        })

    blocked = [i["code"] for i in items if i["status"] == "blocked"]
    notes: list[str] = []
    if not auto_ok:
        notes.append("当前为手动交易模式：自动卖出（含止损止盈）不执行，出口只有人工下单。")
    if int(cfg.get("max_hold_days") or 0) <= 0:
        notes.append(
            "时间止损关闭（paper_max_hold_days=0）：横盘票不会按持有天数强制离场，"
            "但止损/止盈/收盘减仓/人工出口仍在，不属于「没有卖出途径」。"
        )
    no_quote = [i["code"] for i in items if i["price"] is None]
    if no_quote:
        notes.append("无行情持仓：" + "、".join(no_quote)
                     + "；可用 POST /api/paper/order 卖出（不传 price 时按成本价兜底）。")

    return {
        "ok": not blocked,
        "date": today,
        "auto_trade": auto_ok,
        "count": len(items),
        "blocked": blocked,
        "no_quote": no_quote,
        "notes": notes,
        "items": items,
    }
