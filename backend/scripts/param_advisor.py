"""每日参数体检 / 调参候选清单（自动生成，不自动改值）。

为什么需要（2026-11-03 使用者诉求）：每日复盘只做描述性复盘（超额、单票真实盈亏、
账户曲线），没有「该调哪个参数」的信号。本脚本基于**滚动窗口**（默认 21 交易日，与
``BACKTEST_LOG`` #70 §6 同口径）的实盘指标，输出三级信号 ✅/⚠️/🔴 与「候选参数 + 当前值
+ 方向」，但**不给出具体新数值、不自动改 ``.env`、不基于单日下结论**——任何参数变更
仍须跑配对回测验证（门槛 >3.5pp / 21 天）。防止「今天亏了就改参数」的过拟合。

指标（数据源 ``paper_equity`` / ``paper_trades`` / 当前 ``settings``）：
  1) 滚动最大回撤 / 年化波动 / 窗口累计收益 —— 组合风控层面；
  2) 「做T加仓」标签票的胜率（FIFO 总真实盈亏）—— 候选 ``paper_t_down_add/down_full``；
  3) 「轮动替换」换入票的胜率 —— 候选 ``paper_rotation_min_score/gap``。
  弱市专项（``paper_weak_exposure`` / ``paper_regime_cap_*``）需择时快照表，当前库无该表，
  仅列出当前值、不触发，待补齐数据源。

阈值均为**初版**，写在 ``TH`` 常量里，需后续回测校准（不要当成定论）。

用法（backend 目录）：:
    .venv\\Scripts\\python.exe scripts\\param_advisor.py
（已由 ``daily_review.py`` 在每日复盘末尾调用，自动刷新 ``docs/param_advisor.md``）
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

OUT_MD = ROOT / "docs" / "param_advisor.md"

# ---- 初版阈值（需回测校准，勿当定论）----
WINDOW = 21            # 滚动窗口（交易日），#70 §6 口径
WIN_MIN = 5            # 最少样本，不足则标「样本不足」
DD_WARN, DD_RED = 0.08, 0.15       # 滚动最大回撤
VOL_WARN, VOL_RED = 0.25, 0.35     # 年化波动
RET_WARN = -0.05                  # 窗口累计收益（负且低于此 → ⚠️）
TAG_SAMPLE_MIN = 5                 # 标签胜率最少样本
TAG_WIN_WARN = 0.50                # 标签胜率低于此 → ⚠️

# 参数快照：字段名 -> 展示名
SNAP = [
    ("paper_single_position_max", "单票上限比例"),
    ("paper_rotation_enabled", "轮动开关"),
    ("paper_rotation_min_score", "轮动最低分"),
    ("paper_rotation_gap", "轮动分差"),
    ("paper_rotation_max_per_day", "每日轮动上限"),
    ("paper_regime_ma", "择时MA周期"),
    ("paper_regime_weak_pause", "弱市暂停买入"),
    ("paper_weak_exposure", "弱市仓位"),
    ("paper_dynamic_exposure", "动态仓位"),
    ("paper_regime_cap_calm", "平稳仓上限"),
    ("paper_regime_cap_weak", "弱势仓上限"),
    ("paper_regime_cap_shock", "震荡仓上限"),
    ("paper_regime_cap_crisis", "危机仓上限"),
    ("paper_circuit_breaker_enabled", "熔断开关"),
    ("paper_circuit_breaker_dd", "熔断回撤阈值"),
    ("paper_circuit_breaker_target", "熔断目标仓位"),
    ("paper_t_down_add", "做T浅加仓阈值"),
    ("paper_t_down_full", "做T满加仓阈值"),
]


def _fifo(buys: list[tuple[float, float]], sells: list[tuple[float, float]]):
    """FIFO 匹配，返回 (已实现+未实现总盈亏, 剩余股数, 剩余成本)。buys/sells=(股数,金额含/扣费)。"""
    lots = [[sh, cost] for sh, cost in buys]
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
    rem_cost = sum(l[1] for l in lots)
    rem_shares = sum(l[0] for l in lots)
    return realized + 0.0, rem_shares, rem_cost  # 占位，未实现在调用处补


def _sig(level: int) -> str:
    return {0: "✅", 1: "⚠️", 2: "🔴"}.get(level, "✅")


def main() -> None:
    s = get_settings()
    con = sqlite3.connect(str(s.db_path))
    con.row_factory = sqlite3.Row
    today = datetime.now().strftime("%Y-%m-%d")

    # ---- 组合净值序列 ----
    eq = con.execute(
        "SELECT date, total_asset, cash, market_value FROM paper_equity ORDER BY date"
    ).fetchall()
    assets = [float(r["total_asset"]) for r in eq]
    dates = [r["date"] for r in eq]
    win = assets[-WINDOW:] if len(assets) >= WINDOW else assets
    win_dates = dates[-WINDOW:] if len(dates) >= WINDOW else dates

    metrics: list[dict] = []
    candidates: list[dict] = []

    def add(metric, value, threshold, level, cand):
        metrics.append({"metric": metric, "value": value, "threshold": threshold,
                        "level": level})
        if level >= 1 and cand:
            candidates.append(cand)

    if len(win) < WIN_MIN:
        add("滚动最大回撤", "样本不足", f"需≥{WIN_MIN}日(当前{len(win)})", 0, None)
        add("年化波动", "样本不足", f"需≥{WIN_MIN}日", 0, None)
        add("窗口累计收益", "样本不足", f"需≥{WIN_MIN}日", 0, None)
    else:
        # 最大回撤
        peak = win[0]
        mdd = 0.0
        for a in win:
            peak = max(peak, a)
            mdd = max(mdd, (peak - a) / peak if peak else 0.0)
        lvl = 2 if mdd > DD_RED else (1 if mdd > DD_WARN else 0)
        add("滚动最大回撤", f"{mdd:+.2%}", f"⚠️>{DD_WARN:.0%} 🔴>{DD_RED:.0%}", lvl,
            {"param": "paper_regime_cap_weak / shock / crisis, paper_circuit_breaker_*, paper_weak_exposure",
             "cur": f"weak={s.paper_regime_cap_weak}, crisis={s.paper_regime_cap_crisis}, cb={s.paper_circuit_breaker_enabled}",
             "dir": "收紧总仓位上限 / 开启熔断",
             "note": f"窗口{len(win)}日最大回撤 {mdd:+.2%}，超阈值建议回测风控参数（#70 §6）"})
        # 年化波动
        rets = [win[i] / win[i - 1] - 1 for i in range(1, len(win))]
        vol = (pd.Series(rets).std() * (252 ** 0.5)) if len(rets) > 1 else 0.0
        lvl = 2 if vol > VOL_RED else (1 if vol > VOL_WARN else 0)
        add("滚动年化波动", f"{vol:.2%}", f"⚠️>{VOL_WARN:.0%} 🔴>{VOL_RED:.0%}", lvl,
            {"param": "paper_regime_cap_*, paper_single_position_max",
             "cur": f"single={s.paper_single_position_max}, weak={s.paper_regime_cap_weak}",
             "dir": "降低总仓位/单票上限",
             "note": f"窗口波动 {vol:.2%}，偏高建议回测降仓（#70 §6）"})
        # 窗口累计收益
        cum = win[-1] / win[0] - 1
        lvl = 1 if cum < RET_WARN else 0
        add("窗口累计收益", f"{cum:+.2%}", f"⚠️<{RET_WARN:+.0%}", lvl,
            {"param": "组合层面（综合）— 先看上面回撤/波动定位是风控还是选股",
             "cur": "—",
             "dir": "先定位归因再定参",
             "note": f"窗口{len(win)}日累计 {cum:+.2%}"})

    # ---- 交易标签胜率（FIFO 总真实盈亏）----
    trades = con.execute(
        "SELECT code,name,side,price,shares,amount,fee,reason,trade_date "
        "FROM paper_trades ORDER BY trade_date, id"
    ).fetchall()
    # 轮动「换出」卖出发生的日期（卖盘 reason 带「轮动替换」）；换入买盘 reason 是 ML打分/综合评分，
    # 故用「该日有轮动卖出 + 该票当日首次买入」来识别换入票。
    rotation_sell_dates = {t["trade_date"] for t in trades if "轮动替换" in (t["reason"] or "")}
    first_buy: dict[str, str] = {}
    for t in trades:
        if t["side"] == "buy":
            first_buy.setdefault(t["code"], t["trade_date"])
    by_code: dict[str, dict] = {}
    for t in trades:
        code = t["code"]
        d = by_code.setdefault(code, {"buys": [], "sells": [], "tags": set()})
        reason = t["reason"] or ""
        if t["side"] == "buy":
            amt = float(t["amount"]) + float(t["fee"] or 0)
            d["buys"].append((float(t["shares"]), amt))
            if "做T加仓" in reason:
                d["tags"].add("t_add")
            if t["trade_date"] in rotation_sell_dates and t["trade_date"] == first_buy.get(code):
                d["tags"].add("rotation")
        else:
            rec = float(t["amount"]) - float(t["fee"] or 0)
            d["sells"].append((float(t["shares"]), rec))

    # 当前价（实时优先，退化 parquet）
    codes = list(by_code.keys())
    kd = Path(str(s.kline_dir))
    live: dict = {}
    try:
        from app.data.fallback import code_to_symbol
        from scripts.daily_review import _sina_rows, day_kline
        rows = _sina_rows([code_to_symbol(c) for c in codes])
        live = {c: rows[c] for c in codes if rows.get(c)}
    except Exception:
        from scripts.daily_review import day_kline  # noqa: F811
        live = {}

    def total_pnl(d: dict) -> float:
        realized, rem_shares, rem_cost = _fifo(d["buys"], d["sells"])
        kl = day_kline(kd, d.get("_code", ""), today, live=live.get(d.get("_code", "")))
        if rem_shares > 0 and kl:
            return realized + (rem_shares * float(kl["close"]) - rem_cost)
        return realized

    for code, d in by_code.items():
        d["_code"] = code
    tag_groups = {"t_add": [], "rotation": []}
    for code, d in by_code.items():
        for tag in ("t_add", "rotation"):
            if tag in d["tags"]:
                tag_groups[tag].append(total_pnl(d))

    tag_meta = {
        "t_add": ("「做T加仓」标签票胜率", "paper_t_down_add / paper_t_down_full",
                  f"add={s.paper_t_down_add}, full={s.paper_t_down_full}",
                  "回测收窄或关闭下跌加仓"),
        "rotation": ("「轮动替换」换入票胜率", "paper_rotation_min_score / paper_rotation_gap",
                     f"min_score={s.paper_rotation_min_score}, gap={s.paper_rotation_gap}",
                     "提高轮动门槛（更高分差/最低分）"),
    }
    for tag, (mname, param, cur, direction) in tag_meta.items():
        vals = tag_groups[tag]
        n = len(vals)
        if n == 0:
            add(mname, "无样本", "—", 0, None)
            continue
        wins = sum(1 for v in vals if v > 0)
        wr = wins / n
        if n < TAG_SAMPLE_MIN:
            add(mname, f"{wr:.0%}（{wins}/{n}）", f"需≥{TAG_SAMPLE_MIN}（当前{n}）", 0, None)
        else:
            lvl = 1 if wr < TAG_WIN_WARN else 0
            add(mname, f"{wr:.0%}（{wins}/{n}）", f"⚠️<{TAG_WIN_WARN:.0%}", lvl,
                {"param": param, "cur": cur, "dir": direction,
                 "note": f"{n} 只样本胜率 {wr:.0%}，低于 {TAG_WIN_WARN:.0%} 建议回测（#70 §6）"})

    # ---- 渲染 ----
    lines = ["# 每日参数体检 / 调参候选清单（自动生成，请勿手改）", ""]
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ 窗口 {WINDOW} 交易日（#70 §6 口径）")
    lines.append(">")
    lines.append("> **本清单为诊断信号，不是定值建议**：不自动改 `.env`、不基于单日下结论。"
                 "任何参数变更须跑配对回测验证（门槛 >3.5pp / 21 天）。防止「今天亏了就改参数」的过拟合。")
    lines.append("> 弱市专项（`paper_weak_exposure` / `paper_regime_cap_*`）需择时快照表，当前库无该表，仅列当前值不触发。")
    lines.append("")
    lines.append("## 一、当前参数快照")
    lines.append("")
    lines.append("| 参数 | 展示名 | 当前值 |")
    lines.append("| --- | --- | --- |")
    for attr, label in SNAP:
        v = getattr(s, attr, None)
        lines.append(f"| `{attr}` | {label} | {v} |")
    lines.append("")
    lines.append("## 二、诊断指标")
    lines.append("")
    lines.append("| 指标 | 当前值 | 阈值 | 信号 |")
    lines.append("| --- | --- | --- | --- |")
    for m in metrics:
        lines.append(f"| {m['metric']} | {m['value']} | {m['threshold']} | {_sig(m['level'])} |")
    lines.append("")
    lines.append("## 三、调参候选清单（⚠️/🔴 触发项）")
    lines.append("")
    if not candidates:
        lines.append("> 当前无触发项（全部 ✅ 或样本不足）。维持现状，无需调参。")
    else:
        lines.append("| 候选参数 | 当前值 | 建议方向 | 备注（需配对回测 #70 §6） |")
        lines.append("| --- | --- | --- | --- |")
        for c in candidates:
            lines.append(f"| {c['param']} | {c['cur']} | {c['dir']} | {c['note']} |")
    lines.append("")
    lines.append("---")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_trig = sum(1 for c in candidates)
    print(f"[param_advisor] 已写入 {OUT_MD}（指标 {len(metrics)} 项，触发候选 {n_trig} 项）")


if __name__ == "__main__":
    main()
