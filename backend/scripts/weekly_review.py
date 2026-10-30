"""周度复盘：把「这周为什么没交易 / 为什么亏」直接算出来，不用再翻日志。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe scripts\\weekly_review.py
    .venv\\Scripts\\python.exe scripts\\weekly_review.py --days 14
    .venv\\Scripts\\python.exe scripts\\weekly_review.py --start 2026-10-26 --end 2026-10-30 --md

数据来源（全部**只读**）：
  - ``paper_equity``   每日净值快照 → 区间收益、最大回撤
  - ``paper_trades``   成交流水 → 笔数 / 成交额 / 费用 / 最大亏损笔
  - ``paper_positions``当前持仓 → 浮盈与集中度
  - ``app_state``      ``paper_regime_paused:YYYY-MM-DD`` → 弱市闸门每日拦截次数
                       （由 ``PaperEngine`` 每 60 次 tick 落库一次，重启不丢）

输出末尾给一条「诊断」：无成交时区分「被弱市闸门拦住」和「本来就没信号」，
这正是 2026-10-30 那天需要人工翻 1999 行日志才能得到的结论。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

REGIME_KEY_PREFIX = "paper_regime_paused:"
#: 收盘结算写入的当日择时快照（成功/弱市/仓位），比拦截计数更适合判读
REGIME_STATE_PREFIX = "paper_regime_state:"


@dataclass
class DayStats:
    """单个交易日的成交与拦截汇总。"""

    day: str
    buys: int = 0
    sells: int = 0
    amount: float = 0.0
    fee: float = 0.0
    regime_paused: int = 0
    #: 当日是否弱势（收盘快照，``None`` = 该日没有快照，无法判定）
    weak: bool | None = None
    #: 当日收盘总仓位比例（0.3 = 三成仓）
    exposure: float | None = None
    trades: list[dict] = field(default_factory=list)


def _connect(db_path: Path) -> sqlite3.Connection:
    """只读连接（``mode=ro``）：复盘脚本永远不该改动账户数据。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _load_equity(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    if not _table_exists(conn, "paper_equity"):
        return []
    return conn.execute(
        "SELECT date, total_asset, cash, market_value FROM paper_equity "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    ).fetchall()


def _load_trades(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    if not _table_exists(conn, "paper_trades"):
        return []
    return conn.execute(
        "SELECT code, name, side, price, shares, amount, fee, reason, trade_date, created_at "
        "FROM paper_trades WHERE trade_date BETWEEN ? AND ? ORDER BY id",
        (start, end),
    ).fetchall()


def _load_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(conn, "paper_positions"):
        return []
    return conn.execute(
        "SELECT code, name, shares, cost_price, buy_date, score FROM paper_positions "
        "ORDER BY shares * cost_price DESC"
    ).fetchall()


def _load_regime(conn: sqlite3.Connection, start: str, end: str) -> dict[str, int]:
    """弱市闸门每日拦截次数（引擎每 60 次 tick 落一次库，取到的值是最近的快照）。"""
    if not _table_exists(conn, "app_state"):
        return {}
    rows = conn.execute(
        "SELECT key, value FROM app_state WHERE key LIKE ?", (REGIME_KEY_PREFIX + "%",)
    ).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        day = str(row["key"])[len(REGIME_KEY_PREFIX):]
        if start <= day <= end:
            try:
                out[day] = int(float(row["value"]))
            except (TypeError, ValueError):
                continue
    return out


def _load_regime_states(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict]:
    """当日择时快照（``paper_regime_state:{date}`` → dict）。

    与 ``paper_regime_paused:`` 的区别：后者只在**发生拦截**时才有值，
    「弱市但当天没有买入信号」不会留下任何痕迹。收盘快照是无条件写的，
    所以能回答「这天到底是不是弱势」——对账要看的就是这个（BACKTEST_LOG #70 §8）。
    """
    if not _table_exists(conn, "app_state"):
        return {}
    rows = conn.execute(
        "SELECT key, value FROM app_state WHERE key LIKE ?", (REGIME_STATE_PREFIX + "%",)
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        day = str(row["key"])[len(REGIME_STATE_PREFIX):]
        if not (start <= day <= end):
            continue
        try:
            payload = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            out[day] = payload
    return out


def _max_drawdown(values: list[float]) -> float:
    """区间最大回撤（正数，0.12 = 12%）。"""
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1 - value / peak)
    return worst


def build_report(start: str, end: str) -> dict:
    """汇总区间数据，返回可直接打印/序列化的结构。"""
    settings = get_settings()
    db_path = settings.db_path
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")

    conn = _connect(db_path)
    try:
        equity = _load_equity(conn, start, end)
        trades = _load_trades(conn, start, end)
        positions = _load_positions(conn)
        regime = _load_regime(conn, start, end)
        states = _load_regime_states(conn, start, end)
    finally:
        conn.close()

    days: dict[str, DayStats] = {}
    for row in trades:
        day = str(row["trade_date"])
        stat = days.setdefault(day, DayStats(day=day))
        if str(row["side"]) == "buy":
            stat.buys += 1
        else:
            stat.sells += 1
        stat.amount += float(row["amount"] or 0)
        stat.fee += float(row["fee"] or 0)
        stat.trades.append(dict(row))
    for day, count in regime.items():
        days.setdefault(day, DayStats(day=day)).regime_paused = count
    for day, state in states.items():
        stat = days.setdefault(day, DayStats(day=day))
        if isinstance(state.get("weak"), bool):
            stat.weak = state["weak"]
        exposure = state.get("exposure")
        if isinstance(exposure, (int, float)):
            stat.exposure = float(exposure)
        paused = state.get("paused_count")
        if not stat.regime_paused and isinstance(paused, int):
            stat.regime_paused = paused

    assets = [float(r["total_asset"]) for r in equity]
    equity_rows = [dict(r) for r in equity]

    sells = [t for t in trades if str(t["side"]) == "sell"]
    worst_trade = None
    if sells:
        worst_trade = min(
            sells,
            key=lambda t: float(t["price"] or 0) * float(t["shares"] or 0),
        )

    return {
        "range": {"start": start, "end": end},
        "equity": {
            "rows": equity_rows,
            "first": assets[0] if assets else None,
            "last": assets[-1] if assets else None,
            "change_pct": ((assets[-1] / assets[0] - 1) * 100) if len(assets) >= 2 and assets[0] else None,
            "max_drawdown_pct": _max_drawdown(assets) * 100 if assets else None,
        },
        "trades": {
            "count": len(trades),
            "buys": sum(1 for t in trades if str(t["side"]) == "buy"),
            "sells": len(sells),
            "amount": sum(float(t["amount"] or 0) for t in trades),
            "fee": sum(float(t["fee"] or 0) for t in trades),
            "worst_sell": worst_trade,
        },
        "positions": [dict(p) for p in positions],
        "days": [days[d] for d in sorted(days)],
        "regime_total": sum(regime.values()),
    }


def diagnose(report: dict) -> str:
    """给一句人话结论：这周没交易/亏钱，是闸门拦的，还是本来就没信号。"""
    trades = report["trades"]
    regime_total = report["regime_total"]
    weak_days = [d.day for d in report["days"] if d.weak]
    if trades["count"] == 0:
        if regime_total > 0:
            return (f"本周零成交：弱市闸门累计拦截 {regime_total} 次（大盘在 MA 下方），"
                    "自动买入被整体暂停——不是系统故障，可在设置页关闭闸门或调「弱市保留仓位」。")
        if weak_days:
            return (f"本周零成交，其中 {len(weak_days)} 个交易日处于弱市（指数在 MA 下）"
                    "却没有拦截记录——说明当天本来就没有买入信号，不是闸门拦的。"
                    "若「弱市保留仓位」>0，还需确认不是被买入名额/仓位上限挤掉的。")
        return ("本周零成交，且没有弱市拦截记录。可能原因：① 本来就没有买入信号；"
                "② 信号都在开盘窗口外（买单顺延到下一个交易日开盘）；"
                "③ 拦截计数未落库——该计数自 2026-10-30 起才写 app_state，"
                "旧版本或重启前的日子在这里是空的（那时只能查 data/logs 里的「大盘弱势」）。"
                "先看「选股/自选股」有没有买入建议，再看 pending 队列。")
    if trades["sells"] == 0:
        return f"本周只买不卖（{trades['buys']} 笔买入），尚未兑现，看下周表现。"
    fee_ratio = trades["fee"] / trades["amount"] * 100 if trades["amount"] else 0.0
    tail = f"摩擦占比 {fee_ratio:.2f}%。"
    if regime_total > 0:
        return f"本周 {trades['buys']} 买 / {trades['sells']} 卖，弱市闸门另拦截 {regime_total} 次；{tail}"
    return f"本周 {trades['buys']} 买 / {trades['sells']} 卖，闸门未触发；{tail}"


def render_text(report: dict) -> str:
    """终端表格输出。"""
    lines: list[str] = []
    rng = report["range"]
    lines.append(f"复盘区间 {rng['start']} ~ {rng['end']}")
    lines.append("")

    eq = report["equity"]
    if eq["rows"]:
        change = "—" if eq["change_pct"] is None else f"{eq['change_pct']:+.2f}%"
        dd = "—" if eq["max_drawdown_pct"] is None else f"{eq['max_drawdown_pct']:.2f}%"
        lines.append(f"净值：{len(eq['rows'])} 个快照 · 期初 {eq['first']:,.2f} → 期末 {eq['last']:,.2f}"
                     f" · 区间 {change} · 最大回撤 {dd}")
    else:
        lines.append("净值：区间内没有 paper_equity 快照（结算任务未跑或账户刚重置）")

    tr = report["trades"]
    lines.append(f"成交：{tr['count']} 笔（买 {tr['buys']} / 卖 {tr['sells']}）"
                 f" · 成交额 {tr['amount']:,.0f} 元 · 费用 {tr['fee']:,.2f} 元")

    if report["days"]:
        lines.append("")
        lines.append("按日：")
        lines.append(f"  {'日期':<12}{'买':>4}{'卖':>4}{'成交额':>12}"
                     f"{'弱市拦截':>10}{'收盘仓位':>10}{'弱势':>6}")
        for stat in report["days"]:
            exposure = f"{stat.exposure * 100:.0f}%" if stat.exposure is not None else "—"
            weak = ("是" if stat.weak else "否") if stat.weak is not None else "—"
            lines.append(f"  {stat.day:<12}{stat.buys:>4}{stat.sells:>4}"
                         f"{stat.amount:>12,.0f}{stat.regime_paused:>10}"
                         f"{exposure:>10}{weak:>6}")
    else:
        lines.append("按日：区间内无成交、无弱市拦截记录")

    if report["positions"]:
        lines.append("")
        lines.append("当前持仓（按市值）：")
        for pos in report["positions"]:
            value = float(pos["shares"]) * float(pos["cost_price"])
            lines.append(f"  {pos['code']} {pos['name'] or '':<6} {pos['shares']:>6} 股"
                         f" · 成本 {float(pos['cost_price']):.2f} · 市值 {value:,.0f}"
                         f" · 建仓 {pos['buy_date']}")

    lines.append("")
    lines.append("诊断：" + diagnose(report))
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    """Markdown 输出，便于贴进 WORKLOG / 周报。"""
    rng = report["range"]
    eq = report["equity"]
    tr = report["trades"]
    change = "—" if eq["change_pct"] is None else f"{eq['change_pct']:+.2f}%"
    dd = "—" if eq["max_drawdown_pct"] is None else f"{eq['max_drawdown_pct']:.2f}%"
    lines = [
        f"### 周度复盘 {rng['start']} ~ {rng['end']}",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 净值 | {eq['first'] if eq['first'] is not None else '—'} → "
        f"{eq['last'] if eq['last'] is not None else '—'} |",
        f"| 区间收益 | {change} |",
        f"| 最大回撤 | {dd} |",
        f"| 成交 | {tr['count']} 笔（买 {tr['buys']} / 卖 {tr['sells']}） |",
        f"| 费用 | {tr['fee']:,.2f} 元 |",
        f"| 弱市拦截 | {report['regime_total']} 次 |",
        "",
        "| 日期 | 买 | 卖 | 成交额 | 弱市拦截 | 收盘仓位 | 弱势 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for stat in report["days"]:
        exposure = f"{stat.exposure * 100:.0f}%" if stat.exposure is not None else "—"
        weak = ("是" if stat.weak else "否") if stat.weak is not None else "—"
        lines.append(f"| {stat.day} | {stat.buys} | {stat.sells} | {stat.amount:,.0f} "
                     f"| {stat.regime_paused} | {exposure} | {weak} |")
    lines += ["", f"> {diagnose(report)}"]
    return "\n".join(lines)


def _parse_day(text: str) -> str:
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"日期格式应为 YYYY-MM-DD：{text}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="周度复盘：成交 / 净值 / 弱市拦截")
    parser.add_argument("--days", type=int, default=7, help="回看天数（默认 7；被 --start 覆盖）")
    parser.add_argument("--start", default="", help="起始日 YYYY-MM-DD（含）")
    parser.add_argument("--end", default="", help="结束日 YYYY-MM-DD（含，默认今天）")
    parser.add_argument("--md", action="store_true", help="输出 Markdown（便于贴进日志）")
    args = parser.parse_args()

    end = _parse_day(args.end) if args.end else date.today().strftime("%Y-%m-%d")
    if args.start:
        start = _parse_day(args.start)
    else:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=max(1, args.days) - 1)).strftime("%Y-%m-%d")

    report = build_report(start, end)
    print(render_markdown(report) if args.md else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
