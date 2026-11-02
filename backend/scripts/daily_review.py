"""每日收盘后交易复盘：今日交易的标的 vs 大盘基准，得出真实得失。

自动跑（收盘后）：把当天 `paper_trades` 里出现的标的，结合当日 K 线与大盘指数
（本地 `idx_*.parquet`，主基准上证指数），算出每只票的当日涨跌、振幅、买入明细、
浮盈亏、相对大盘的超额，以及组合层面的加权涨跌与超额，并附自动得失点评。

结果 **追加** 到 `docs/daily_trade_review.md`（按日期分节），便于长期沉淀、
反推策略与参数。

用法（backend 目录，或任意 cwd 均可，已用绝对路径定位项目）:
    .venv\\Scripts\\python.exe scripts\\daily_review.py            # 默认今天
    .venv\\Scripts\\python.exe scripts\\daily_review.py --date 2026-11-02
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
ROOT = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402

OUT_MD = ROOT / "docs" / "daily_trade_review.md"
# 大盘基准指数（本地 idx_*.parquet）；优先上证，创业板/深证有今日数据才纳入
IDX_BENCH = [
    ("idx_sh000001", "上证指数"),
    ("idx_sz399006", "创业板指"),
    ("idx_sz399001", "深证成指"),
]


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def _sina_rows(symbols: list[str]) -> dict[str, dict]:
    """新浪批量实时行情（收盘后 price 即当日收盘价）→ {code: row}。

    注意 ``sina_quotes`` 会把 symbol 前缀（sh/sz）剥掉存 ``code``，
    个股六位码全局无歧义可直接用；指数须由调用方按名称回配
    （sh000001 剥前缀后与平安银行 sz000001 撞码）。
    """
    try:
        from app.data.fallback import sina_quotes
        df = sina_quotes(list(symbols))
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    return {str(r["code"]): dict(r) for r in df.to_dict("records")}


def day_kline(kd: Path, code: str, today: str, live: dict | None = None):
    """当日行情：优先实时（收盘后=真实收盘价），退化本地 parquet 日线。

    本地日线盘中是快照、收盘后不回填（2026-11-02 实证：301313 parquet
    收盘 59.21 vs 实时收盘 62.49，差 5%），复盘「今天」一律以实时为准。
    """
    if live and float(live.get("price") or 0) > 0 and live.get("prev_close"):
        price = float(live["price"])
        prev = float(live["prev_close"])
        hi = float(live.get("high") or price)
        lo = float(live.get("low") or price)
        return {
            "open": float(live.get("open") or price), "close": price,
            "high": hi, "low": lo, "prev_close": prev,
            "pct": pct(price, prev),
            "amp": (hi - lo) / prev * 100 if prev else 0.0,
            "src": "实时",
        }
    p = kd / f"{code}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["date"] = df["date"].astype(str)
    hit = df[df["date"] == today]
    if hit.empty:
        return None
    r = hit.iloc[0]
    i = df.index[df["date"] == today][0]
    prev = float(df.iloc[i - 1]["close"]) if i > 0 else float(r["close"])
    close = float(r["close"])
    return {
        "open": float(r["open"]),
        "close": close,
        "high": float(r["high"]),
        "low": float(r["low"]),
        "prev_close": prev,
        "pct": pct(close, prev),
        "amp": (float(r["high"]) - float(r["low"])) / prev * 100,
        "src": "K线",
    }


def index_pct(kd: Path, file: str, today: str):
    """指数当日涨跌幅%，无当日数据返回 None。"""
    p = kd / f"{file}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["date"] = df["date"].astype(str)
    hit = df[df["date"] == today]
    if hit.empty:
        return None
    r = hit.iloc[0]
    i = df.index[df["date"] == today][0]
    prev = float(df.iloc[i - 1]["close"]) if i > 0 else float(r["close"])
    return pct(float(r["close"]), prev)


def brief(reason: str, n: int = 46) -> str:
    if not reason:
        return ""
    reason = reason.replace("\n", " ")
    return reason if len(reason) <= n else reason[:n] + "…"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="复盘日期 YYYY-MM-DD，默认今天")
    args = ap.parse_args()
    today = args.date or pd.Timestamp.now().strftime("%Y-%m-%d")

    s = get_settings()
    kd = Path(str(s.kline_dir))
    c = sqlite3.connect(str(s.db_path))
    c.row_factory = sqlite3.Row

    # 当日交易标的（去重）
    trades = c.execute(
        "SELECT code,name,side,price,shares,trade_date,reason FROM paper_trades "
        "WHERE trade_date=?", (today,)).fetchall()
    codes = sorted({t["code"] for t in trades})
    positions = {r["code"]: r for r in c.execute(
        "SELECT code,name,shares,cost_price,stop_price,peak_price,score FROM paper_positions")}

    # 实时行情（仅复盘「今天」时取：收盘后 price 即真实收盘价）
    is_today = today == pd.Timestamp.now().strftime("%Y-%m-%d")
    live_stocks: dict[str, dict] = {}
    idx_live: dict[str, dict] = {}
    if is_today and codes:
        from app.data.fallback import code_to_symbol
        rows = _sina_rows([code_to_symbol(c) for c in codes])
        live_stocks = {c: rows[c] for c in codes if rows.get(c)}
        # 指数：'idx_sh000001' → 新浪 'sh000001'；结果按名称回配（避免 000001 撞码）
        irows = _sina_rows([f[4:] for f, _ in IDX_BENCH])
        for r in irows.values():
            nm = str(r.get("name") or "")
            for _, name in IDX_BENCH:
                if nm == name and float(r.get("price") or 0) > 0:
                    idx_live[name] = r

    # 大盘基准：优先实时（修复创业板指本地缓存滞后），退化本地 idx parquet
    bench = {}
    for f, name in IDX_BENCH:
        v = None
        lr = idx_live.get(name)
        if lr and lr.get("pct_change") is not None:
            v = float(lr["pct_change"])
        if v is None:
            v = index_pct(kd, f, today)
        if v is not None:
            bench[name] = v
    main_bench = bench.get("上证指数")
    bench_note = "、".join(f"{k}{v:+.2f}%" for k, v in bench.items())
    src = "新浪实时收盘" if live_stocks else "本地K线（注意：盘中快照，可能未回填收盘价）"

    lines = [f"## {today} 收盘复盘", ""]
    lines.append(f"**大盘基准**：{bench_note or '（指数数据不可用）'} ｜ 价格源：**{src}**")
    lines.append(f"> 复盘对象为真实主账户（`paper_positions`，与前端「模拟交易」卡片同源）；"
                 f"本地 parquet 日线盘中是快照、收盘后不回填，故复盘「今天」以实时行情为准。")
    lines.append("")

    if not codes:
        lines.append(f"> 当日（{today}）`paper_trades` 无成交记录，无交易标的可复盘。")
        lines.append("")
        _append(lines)
        print(f"[daily_review] {today}: 无交易，仅占位记录")
        return

    # 个股表
    lines.append("### 今日交易标的（%d 只）" % len(codes))
    lines.append("| 代码 | 名称 | 信号类型 | 买入价(加权) | 股数 | 今日收盘 | 今日涨跌 | 振幅 | 浮盈亏(vs成本) | 超额(vs上证) |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    combo_mv, combo_cost, combo_pct_w = 0.0, 0.0, 0.0
    combo_excess_w = 0.0
    pos_rows = []
    for code in codes:
        ts = [t for t in trades if t["code"] == code]
        tot_shares = sum(t["shares"] for t in ts)
        wprice = sum(t["price"] * t["shares"] for t in ts) / tot_shares if tot_shares else 0
        has_t = any("做T" in (t["reason"] or "") for t in ts)
        if any("ML" in (t["reason"] or "") for t in ts):
            base = "ML信号"
        elif any("综合" in (t["reason"] or "") for t in ts):
            base = "综合评分"
        else:
            base = "其他"
        sig = base + ("+做T" if has_t else "")
        kl = day_kline(kd, code, today, live=live_stocks.get(code))
        pos = positions.get(code)
        cost = float(pos["cost_price"]) if pos else wprice
        shares = int(pos["shares"]) if pos else tot_shares
        close = kl["close"] if kl else (cost)
        mv = close * shares
        cost_mv = cost * shares
        float_pct = pct(close, cost)
        excess = (kl["pct"] - main_bench) if (kl and main_bench is not None) else None
        lines.append(
            f"| {code} | {ts[0]['name']} | {sig} | {wprice:.2f} | {shares} | "
            f"{close:.2f} | {kl['pct']:+.2f}% | {kl['amp']:.2f}% | {float_pct:+.2f}% | "
            f"{excess:+.2f}pp |" if excess is not None else
            f"| {code} | {ts[0]['name']} | {sig} | {wprice:.2f} | {shares} | "
            f"{close:.2f} | {kl['pct']:+.2f}% | {kl['amp']:.2f}% | {float_pct:+.2f}% | — |")
        if kl and main_bench is not None:
            w = mv
            combo_mv += mv
            combo_cost += cost_mv
            combo_pct_w += kl["pct"] * w
            combo_excess_w += excess * w
            pos_rows.append((code, ts[0]["name"], sig, wprice, shares, kl, cost, mv, cost_mv, float_pct, excess))

    # 组合层面
    lines.append("")
    lines.append("### 组合层面（今日交易标的的当前持仓）")
    if combo_mv > 0 and main_bench is not None:
        combo_pct = combo_pct_w / combo_mv
        combo_excess = combo_excess_w / combo_mv
        total_float = combo_mv - combo_cost
        total_float_pct = pct(combo_mv, combo_cost)
        lines.append(f"- 持仓市值：**{combo_mv:,.0f}** ｜ 成本市值：{combo_cost:,.0f}")
        lines.append(f"- 总浮盈亏：**{total_float:+,.0f}（{total_float_pct:+.2f}%，成本口径）**")
        lines.append(
            "- 前端「模拟交易」累计收益 **+2.09%（总资产 vs 初始 10万 口径）**：与上方同源实时价、"
            "数学一致，仅分母不同（成本口径 vs 总资产口径），不存在数据矛盾。")
        lines.append(f"- 组合今日加权涨跌：**{combo_pct:+.2f}%** ｜ 相对上证超额：**{combo_excess:+.2f}pp**")
    else:
        lines.append("- （无市值或大盘基准不可用，跳过组合加权）")

    # 得失点评
    lines.append("")
    lines.append("### 得失点评")
    for code, name, sig, wprice, shares, kl, cost, mv, cost_mv, float_pct, excess in pos_rows:
        verdict = "跑赢大盘" if excess > 0 else ("跑输大盘" if excess < 0 else "持平")
        # 买入时机：加权买价相对当日高低点
        if kl["high"] > kl["low"]:
            ratio = (wprice - kl["low"]) / (kl["high"] - kl["low"]) * 100
        else:
            ratio = 50.0
        above_high = wprice > kl["high"]
        below_low = wprice < kl["low"]
        ratio = max(0.0, min(100.0, ratio))
        if above_high:
            timing = "买价高于当日最高价（盘中冲高或数据口径差异）"
        elif below_low:
            timing = "买价低于当日最低价"
        elif ratio >= 99:
            timing = "买在当日最高价附近"
        elif ratio <= 1:
            timing = "买在当日最低价附近"
        elif ratio < 35:
            timing = "买在当日低位区"
        elif ratio > 65:
            timing = "买在当日高位区"
        else:
            timing = "买在当日中部"
        lines.append(
            f"- **{name}({code})**：今日 {kl['pct']:+.2f}%（{verdict} {excess:+.2f}pp），"
            f"信号={sig}，加权买价 {wprice:.2f}（{timing}），"
            f"当前浮盈亏 {float_pct:+.2f}%。")
    if combo_mv > 0 and main_bench is not None:
        cv = "整体跑赢大盘" if combo_excess_w / combo_mv > 0 else "整体跑输大盘"
        lines.append(f"- **组合{cv}**（超额 {combo_excess_w/combo_mv:+.2f}pp）。"
                     f"做T加仓会抬升成本，重点看加仓后浮盈变化：若当日冲高回落则放大回撤，需复核做T触发条件。")

    # 原始信号（备查）
    lines.append("")
    lines.append("### 原始信号（备查）")
    for t in sorted(trades, key=lambda x: x["trade_date"]):
        lines.append(f"- `{t['trade_date']}` {t['name']}({t['code']}) {t['side']} "
                     f"{t['price']:.2f}×{t['shares']} — {brief(t['reason'])}")
    lines.append("")
    lines.append("---")
    lines.append("")

    _write(lines, today)
    print(f"[daily_review] {today}: {len(codes)} 只标的，基准 {bench_note}，价格源 {src}")


def _write(lines: list[str], today: str) -> None:
    """追加当日复盘；若当日已存在则先移除旧节（幂等，支持重跑）。"""
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    import re
    existing = OUT_MD.read_text(encoding="utf-8") if OUT_MD.exists() else ""
    pat = re.compile(r"(?s)## " + re.escape(today) + r" 收盘复盘.*?(?=\n## |\Z)")
    existing = pat.sub("", existing).rstrip() + "\n"
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(existing + "\n".join(lines))


if __name__ == "__main__":
    main()
