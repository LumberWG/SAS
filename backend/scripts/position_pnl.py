"""每只股票从买入到清仓的真实盈亏 + 账户每日盈亏曲线。

为什么需要（2026-11-03 使用者诉求）：
  ``docs/daily_trade_review.md`` 的「今日涨跌」是**当日**涨跌幅、「浮盈亏(vs成本)」是
  **截至今日**的未实现浮盈——两者都不是「这只票从买入到清仓到底赚了多少」。使用者要的是
  单只标的**全生命周期真实盈亏**（已实现 + 当前浮盈），以及一眼看清账户每日盈亏是「当日」
  还是「累计」。

口径（与 ``daily_review.py`` 同源，勿改）：
  - 已实现盈亏 = Σ卖出回收(扣费) − Σ买入成本(含费)，按 ``paper_trades`` 全部记录汇总；
  - 当前浮盈 = 当前持仓市值 − 当前持仓加权成本（``paper_positions.cost_price``）；
  - 总真实盈亏 = 已实现 + 当前浮盈；已全部清仓的票浮盈为 0。
  - 当前价：实时收盘价优先，退化本地 parquet 日线（收盘后一致）。
  - 账户每日盈亏曲线：取 ``paper_equity`` 每日 ``total_asset``，sparkline + 逐日 Δ/收益率。

用法（backend 目录）::
    .venv\\Scripts\\python.exe scripts\\position_pnl.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from scripts.daily_review import _sina_rows, day_kline  # noqa: E402

OUT_MD = ROOT / "docs" / "position_pnl.md"
SPARK = "▁▂▃▄▅▆▇█"


def _spark(series: list[float]) -> str:
    """把数值序列画成迷你曲线（sparkline）。"""
    if len(series) < 2:
        return "(样本不足)"
    lo, hi = min(series), max(series)
    if hi == lo:
        return SPARK[-1] * len(series)
    return "".join(SPARK[min(len(SPARK) - 1, int((v - lo) / (hi - lo) * (len(SPARK) - 1)))]
                   for v in series)


def _bar(v: float, scale: float) -> str:
    """盈亏水平条：正用 █、负用 ░，长度按 |v|/scale 比例（最长 24）。"""
    if scale <= 0:
        return ""
    n = max(1, int(abs(v) / scale * 24))
    n = min(n, 24)
    return ("+" if v >= 0 else "-") + ("█" * n if v >= 0 else "░" * n)


def _fifo(buys: list[tuple[float, float]], sells: list[tuple[float, float]]):
    """按先进先出匹配买卖批次，返回 (已实现盈亏, 剩余持仓成本, 剩余股数)。

    ``buys`` / ``sells`` 元素为 (股数, 金额)；买入金额含费、卖出金额为扣费后回收。
    对「先卖后买回」的票，朴素 Σ卖出−Σ买入会把买回的持仓也算成已实现亏损，
    必须用批次匹配：卖出只与最早的买入批次对冲，买回的部分留在剩余持仓成本里。
    """
    lots = [[sh, cost] for sh, cost in buys]  # [剩余股数, 该批次总成本]
    realized = 0.0
    for sh, proceeds in sells:
        if sh <= 0:
            continue
        unit_p = proceeds / sh
        matched = 0
        cost_basis = 0.0
        while matched < sh and lots:
            lot = lots[0]
            take = min(sh - matched, lot[0])
            unit_c = lot[1] / lot[0] if lot[0] else 0.0
            cost_basis += take * unit_c
            lot[0] -= take
            lot[1] -= take * unit_c
            matched += take
            if lot[0] <= 1e-9:
                lots.pop(0)
        realized += matched * unit_p - cost_basis
    remaining_cost = sum(l[1] for l in lots)
    remaining_shares = sum(l[0] for l in lots)
    return realized, remaining_cost, remaining_shares


def main() -> None:
    s = get_settings()
    con = sqlite3.connect(str(s.db_path))
    con.row_factory = sqlite3.Row
    kd = Path(str(s.kline_dir))
    today = datetime.now().strftime("%Y-%m-%d")

    # ---- 账户每日盈亏曲线 ----
    eq = con.execute(
        "SELECT date, total_asset, cash, market_value FROM paper_equity ORDER BY date"
    ).fetchall()
    eq_lines = ["# 持仓/账户真实盈亏总览（自动生成，请勿手改）", ""]
    eq_lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 数据：实盘模拟账户 "
                    f"`paper_trades` / `paper_positions` / `paper_equity`")
    eq_lines.append("")
    eq_lines.append("## 一、账户每日盈亏曲线")
    eq_lines.append("")
    if eq:
        assets = [float(r["total_asset"]) for r in eq]
        eq_lines.append(f"总资产迷你曲线（{eq[0]['date']} → {eq[-1]['date']}）：")
        eq_lines.append("")
        eq_lines.append("```")
        eq_lines.append(_spark(assets))
        eq_lines.append("```")
        eq_lines.append("")
        eq_lines.append("| 日期 | 总资产 | 当日盈亏(Δ) | 当日收益率 | 累计收益率 |")
        eq_lines.append("| --- | --- | --- | --- | --- |")
        prev = None
        base = float(eq[0]["total_asset"])
        for r in eq:
            a = float(r["total_asset"])
            delta = (a - prev) if prev is not None else 0.0
            dret = (delta / prev * 100) if prev else 0.0
            cret = (a / base - 1) * 100
            eq_lines.append(
                f"| {r['date']} | {a:,.2f} | {delta:+,.2f} | {dret:+.2f}% | {cret:+.2f}% |"
            )
            prev = a
        eq_lines.append("")
        eq_lines.append("> 说明：**当日盈亏** = 当日总资产 − 前一日总资产（即曲线相邻点的落差）；"
                        "**累计收益率** = 总资产相对期初(首条净值)的涨幅。曲线高度代表「累计」，"
                        "相邻落差代表「每日(当日)」。`daily_trade_review.md` 里的「今日涨跌」是单只票"
                        "**当日**涨跌幅、「浮盈亏(vs成本)」是截至今日的**未实现**浮盈——都不是"
                        "「从买入到清仓的真实盈亏」，后者见下表。")
        eq_lines.append("")

    # ---- 每只票生命周期真实盈亏 ----
    trades = con.execute(
        "SELECT code,name,side,price,shares,amount,fee,trade_date "
        "FROM paper_trades ORDER BY trade_date, id"
    ).fetchall()
    positions = {r["code"]: r for r in con.execute(
        "SELECT code,name,shares,cost_price,buy_date FROM paper_positions")}

    by_code: dict[str, dict] = {}
    for t in trades:
        code = t["code"]
        d = by_code.setdefault(code, {"name": t["name"], "invested": 0.0, "proceeds": 0.0,
                                      "buys": [], "sells": [], "buy_dates": [], "sell_dates": []})
        if t["side"] == "buy":
            amt = float(t["amount"]) + float(t["fee"] or 0)
            d["invested"] += amt
            d["buys"].append((float(t["shares"]), amt))
            d["buy_dates"].append(t["trade_date"])
        else:
            rec = float(t["amount"]) - float(t["fee"] or 0)
            d["proceeds"] += rec
            d["sells"].append((float(t["shares"]), rec))
            d["sell_dates"].append(t["trade_date"])

    # 当前价：实时优先，退化 parquet
    codes = list(by_code.keys())
    live: dict[str, dict] = {}
    try:
        from app.data.fallback import code_to_symbol
        rows = _sina_rows([code_to_symbol(c) for c in codes])
        live = {c: rows[c] for c in codes if rows.get(c)}
    except Exception:
        live = {}

    rows_md = []
    for code, d in by_code.items():
        first_buy = d["buy_dates"][0] if d["buy_dates"] else "—"
        last_sell = d["sell_dates"][-1] if d["sell_dates"] else "—"
        realized, remaining_cost, remaining_shares = _fifo(d["buys"], d["sells"])
        kl = day_kline(kd, code, today, live=live.get(code))
        cur_price = float(kl["close"]) if kl else None
        if remaining_shares > 0 and cur_price:
            cur_mv = remaining_shares * cur_price
            unrealized = cur_mv - remaining_cost
            status = "持仓中"
        else:
            cur_mv, unrealized = 0.0, 0.0
            status = "已清仓"
        total = realized + unrealized
        pct_total = total / d["invested"] * 100 if d["invested"] else 0.0
        rows_md.append({
            "code": code, "name": d["name"], "status": status,
            "first_buy": first_buy, "last_sell": last_sell,
            "invested": d["invested"], "proceeds": d["proceeds"],
            "realized": realized, "unrealized": unrealized,
            "total": total, "pct": pct_total, "held": remaining_shares,
        })

    rows_md.sort(key=lambda x: x["total"])
    scale = max((abs(r["total"]) for r in rows_md), default=0.0)

    eq_lines.append("## 二、每只股票从买入到清仓的真实盈亏")
    eq_lines.append("")
    eq_lines.append("> 总真实盈亏 = 已实现盈亏(Σ卖出回收−Σ买入成本，含费) + 当前浮盈(持仓市值−加权成本)。"
                    "已清仓的票浮盈为 0，总盈亏即「从买入到清仓」的真实结果。")
    eq_lines.append("")
    eq_lines.append("| 代码 | 名称 | 状态 | 首买 | 末卖 | 投入(含费) | 卖出回收 | 已实现 | 当前浮盈 | **总真实盈亏** | 收益率 | 可视化 |")
    eq_lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows_md:
        eq_lines.append(
            f"| {r['code']} | {r['name']} | {r['status']} | {r['first_buy']} | {r['last_sell']} "
            f"| {r['invested']:,.2f} | {r['proceeds']:,.2f} | {r['realized']:+,.2f} | "
            f"{r['unrealized']:+,.2f} | **{r['total']:+,.2f}** | {r['pct']:+.2f}% | {_bar(r['total'], scale)} |"
        )
    eq_lines.append("")
    eq_lines.append("---")

    OUT_MD.write_text("\n".join(eq_lines) + "\n", encoding="utf-8")
    print(f"[position_pnl] 已写入 {OUT_MD}（{len(rows_md)} 只标的，账户净值 {len(eq)} 天）")


if __name__ == "__main__":
    main()
