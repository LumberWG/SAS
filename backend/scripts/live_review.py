"""实盘对账：把账本净值换算成「相对等权基准的超额」，直接对照回测预期。

为什么需要（BACKTEST_LOG 记录 #70 §6）：
  ``#68`` 冻结的口径是「21 天超额 ≈ +2.1pp」，而 ``scripts/weekly_review.py``
  只输出绝对收益。账户 10 万本金的 +1.5% 与「跑赢 Top300 等权 1.5pp」是两件
  完全不同的事——缺了基准，``#70`` 那条判读规则根本执行不了。

基准口径（与回测 ``app/backtest/pipeline.py`` 完全一致，勿改）::

    对交易日 t：取**前一交易日**收盘时的「当日成交额 TopN」股票池，
    等权持有到今天收盘，日收益 = 成分股 (close_t / close_{t-1} - 1) 的算术平均。

    滞后一天不是保守，是防未来函数：当日成交额排名要收盘后才知道，
    用「当日池」算「当日收益」等于拿成交额（与当日涨跌强相关）挑赢家。

为什么同时算两个池：回测 ``PipelineParams.universe_size`` 默认 500，而线上
``paper_pool_top_n`` 默认 300（``config.py``）。冻结的 +2.1pp 是相对 500 的，
实盘实际只买 300 内的票——两个数都给出来，差异本身就是一条信息。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/live_review.py
    .venv\\Scripts\\python.exe scripts/live_review.py --since 2026-11-02 --until 2026-11-27
    .venv\\Scripts\\python.exe scripts/live_review.py --since 2026-11-02 --md

数据来源（全部只读）：``paper_equity``（每日净值）、``app_state`` 的
``paper_regime_state:YYYY-MM-DD``（当日是否弱势/总仓位，由 ``PaperEngine.settle``
写入）、以及 ``kline_dir`` 下的日 K 缓存（基准成分股）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.backtest.pipeline import _build_universe_by_date  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.weekly_review import _connect, _load_equity, _table_exists  # noqa: E402

REGIME_STATE_PREFIX = "paper_regime_state:"

#: ``#70 §6`` 冻结的判读区间（pp）：低于下界先查执行价，高于上界视为运气。
VERDICT_LOW = -1.0
VERDICT_HIGH = 5.0
#: 少于 21 个交易日不给结论（``#68``：单窗口噪声极差 3.44pp，方向都看不清）。
MIN_TRADING_DAYS = 21


# ----------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------
def _load_regime_states(conn, start: str, end: str) -> dict[str, dict]:
    """当日择时快照（``paper_regime_state:{date}``，JSON 字符串）。"""
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
            out[day] = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
    return out


def _load_raw(kline_dir: Path, start: str, end: str) -> dict[str, pd.DataFrame]:
    """读取区间内的日 K（只留 date/close/amount），与回测同源同口径。

    跳过 ``idx_*``：指数与个股共用同一目录，但指数成交额是全市场合计，
    混进「按成交额 TopN」会把指数排到第一位（``pipeline._load_klines`` 同款处理）。
    """
    raw: dict[str, pd.DataFrame] = {}
    if not os.path.isdir(kline_dir):
        return raw
    for name in os.listdir(kline_dir):
        if not name.endswith(".parquet"):
            continue
        code = name.split(".")[0]
        if not code.isdigit():
            continue
        path = os.path.join(kline_dir, name)
        try:
            df = pd.read_parquet(path, columns=["date", "close", "amount"])
        except Exception:  # noqa: BLE001 - 列缺失/坏文件：回退全读
            try:
                df = pd.read_parquet(path)
            except Exception:  # noqa: BLE001
                continue
        if df is None or df.empty or "date" not in df.columns or "amount" not in df.columns:
            continue
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        if df.empty or "close" not in df.columns:
            continue
        raw[code] = df.reset_index(drop=True)
    return raw


def _trading_dates(raw: dict[str, pd.DataFrame]) -> list[str]:
    """区间内的交易日序列（全市场并集，升序）。"""
    return sorted({d for df in raw.values() for d in df["date"]})


def _close_index(raw: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    """code -> {date: close}，供基准逐日取价。"""
    out: dict[str, dict[str, float]] = {}
    for code, df in raw.items():
        out[code] = dict(zip(df["date"], df["close"].astype(float)))
    return out


# ----------------------------------------------------------------------
# 基准与对账（纯计算，便于单测）
# ----------------------------------------------------------------------
def bench_daily_returns(
    dates: list[str],
    universe: dict[str, list[str]],
    close_index: dict[str, dict[str, float]],
) -> dict[str, float]:
    """每个交易日的等权基准收益（**滞后一天**，见模块 docstring）。"""
    out: dict[str, float] = {}
    for i in range(1, len(dates)):
        prev, day = dates[i - 1], dates[i]
        pcts: list[float] = []
        for code in universe.get(prev) or []:
            series = close_index.get(code)
            if not series:
                continue
            p0 = series.get(prev)
            p1 = series.get(day)
            if not p0 or p1 is None or p0 <= 0:
                continue
            pcts.append(p1 / p0 - 1)
        if pcts:
            out[day] = sum(pcts) / len(pcts)
    return out


def _segment_return(daily: dict[str, float], dates: list[str],
                    prev_day: str, day: str) -> tuple[float, int]:
    """``prev_day`` 之后到 ``day``（含）的基准区间收益；返回 (收益, 缺失天数)。

    按交易日逐日累乘（而不是只看首尾两天）：净值快照可能断档，
    断档期间基准仍在波动，只取首尾会把这段行情漏掉。
    """
    value = 1.0
    missing = 0
    for d in dates:
        if prev_day < d <= day:
            if d in daily:
                value *= 1 + daily[d]
            else:
                missing += 1
    return value - 1, missing


def verdict(excess_pp: float, trading_days: int) -> str:
    """按 ``#70 §6`` 冻结的规则给结论（先写死，避免事后解释）。"""
    if trading_days < MIN_TRADING_DAYS:
        return (f"样本仅 {trading_days} 个交易日（<{MIN_TRADING_DAYS}），"
                "只看方向，不下结论")
    if excess_pp < VERDICT_LOW:
        return ("低于 −1pp：先查执行价与成交可行性（#67 已量化 1.4~2.5pp），"
                "不要调策略参数")
    if excess_pp > VERDICT_HIGH:
        return "高于 +5pp：视为短期运气（#68 噪声极差 3.44pp），不加仓、不调参"
    return "落在 −1 ~ +5pp：与回测一致"


def reconcile(
    equity: dict[str, float],
    dates: list[str],
    pools: tuple[int, ...],
    raw: dict[str, pd.DataFrame],
    close_index: dict[str, dict[str, float]],
    states: dict[str, dict] | None = None,
) -> dict:
    """把净值与各池基准对齐，返回逐日明细与区间汇总。"""
    states = states or {}
    snap_days = [d for d in sorted(equity) if d in dates]
    if len(snap_days) < 2:
        return {"error": "区间内净值快照不足 2 天（结算任务未跑或账户刚重置）",
                "snap_days": snap_days}

    per_pool: dict[int, dict] = {}
    for pool in pools:
        universe = _build_universe_by_date(raw, dates, pool, None)
        daily = bench_daily_returns(dates, universe, close_index)
        rows: list[dict] = []
        missing_total = 0
        prev_day = snap_days[0]
        prev_asset = equity[prev_day]
        for day in snap_days[1:]:
            seg, missing = _segment_return(daily, dates, prev_day, day)
            missing_total += missing
            strat = equity[day] / prev_asset - 1 if prev_asset else 0.0
            rows.append({
                "date": day,
                "strategy": strat,
                "benchmark": seg,
                "excess_pp": (strat - seg) * 100,
                "state": states.get(day) or {},
            })
            prev_day, prev_asset = day, equity[day]

        start_day, end_day = snap_days[0], snap_days[-1]
        bench_total, _ = _segment_return(daily, dates, start_day, end_day)
        strat_total = equity[end_day] / equity[start_day] - 1 if equity[start_day] else 0.0
        trading_days = sum(1 for d in dates if start_day < d <= end_day)
        excess_pp = (strat_total - bench_total) * 100
        per_pool[pool] = {
            "rows": rows,
            "benchmark_total": bench_total,
            "strategy_total": strat_total,
            "excess_pp": excess_pp,
            "trading_days": trading_days,
            "missing_days": missing_total,
            "verdict": verdict(excess_pp, trading_days),
        }

    # 逐日表用最小的池（线上实际执行池 300）展示，其余只在汇总里出现
    primary = per_pool[min(pools)]
    return {
        "range": {"start": snap_days[0], "end": snap_days[-1]},
        "equity": {"first": equity[snap_days[0]], "last": equity[snap_days[-1]]},
        "pools": sorted(pools),
        "per_pool": per_pool,
        "rows": primary["rows"],
        "primary_pool": min(pools),
    }


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------
def _pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def render_text(report: dict) -> str:
    if report.get("error"):
        return f"无法对账：{report['error']}"

    rng = report["range"]
    lines = [f"实盘对账 {rng['start']} ~ {rng['end']}", ""]
    eq = report["equity"]
    lines.append(f"账本：{eq['first']:,.2f} → {eq['last']:,.2f}"
                 f"（{_pct(eq['last'] / eq['first'] - 1) if eq['first'] else '—'}）")
    lines.append("")
    lines.append("| 基准池 | 交易日 | 策略 | 基准 | 超额 | 判读 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for pool in report["pools"]:
        data = report["per_pool"][pool]
        lines.append(
            f"| Top{pool} 等权 | {data['trading_days']} | {_pct(data['strategy_total'])} "
            f"| {_pct(data['benchmark_total'])} | {data['excess_pp']:+.2f}pp "
            f"| {data['verdict']} |"
        )

    rows = report["rows"]
    if rows:
        lines.append("")
        lines.append(f"按日（超额 vs Top{report['primary_pool']}）：")
        lines.append(f"  {'日期':<12}{'策略':>9}{'基准':>9}{'超额':>9}{'仓位':>8}{'弱势':>6}")
        for row in rows:
            state = row["state"] or {}
            exposure = state.get("exposure")
            weak = state.get("weak")
            lines.append(
                f"  {row['date']:<12}{_pct(row['strategy']):>9}"
                f"{_pct(row['benchmark']):>9}{row['excess_pp']:>+8.2f}pp"
                f"{(f'{exposure * 100:.0f}%' if isinstance(exposure, (int, float)) else '—'):>8}"
                f"{('是' if weak else '否') if isinstance(weak, bool) else '—':>6}"
            )

    missing = max(d["missing_days"] for d in report["per_pool"].values())
    if missing:
        lines.append("")
        lines.append(f"提示：有 {missing} 个交易日算不出基准收益（成分股无 K 线），已按 0 计。")
    lines.append("")
    lines.append("口径：基准 = 前一交易日成交额 TopN 等权持有到当日收盘（与回测 pipeline 一致）；"
                 "回测预期 ≈ +2.1pp / 21 天（#68）。")
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    if report.get("error"):
        return f"> 无法对账：{report['error']}"
    rng = report["range"]
    lines = [
        f"### 实盘对账 {rng['start']} ~ {rng['end']}",
        "",
        "| 基准池 | 交易日 | 策略 | 基准 | 超额 | 判读 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for pool in report["pools"]:
        data = report["per_pool"][pool]
        lines.append(
            f"| Top{pool} 等权 | {data['trading_days']} | {_pct(data['strategy_total'])} "
            f"| {_pct(data['benchmark_total'])} | {data['excess_pp']:+.2f}pp | {data['verdict']} |"
        )
    lines += [
        "",
        "| 日期 | 策略 | 基准 | 超额 | 仓位 | 弱势 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        state = row["state"] or {}
        exposure = state.get("exposure")
        weak = state.get("weak")
        lines.append(
            f"| {row['date']} | {_pct(row['strategy'])} | {_pct(row['benchmark'])} "
            f"| {row['excess_pp']:+.2f}pp "
            f"| {f'{exposure * 100:.0f}%' if isinstance(exposure, (int, float)) else '—'} "
            f"| {('是' if weak else '否') if isinstance(weak, bool) else '—'} |"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def build_report(since: str = "", until: str = "",
                 pools: tuple[int, ...] = (500, 300),
                 lookback_days: int = 90) -> dict:
    """读库 + 读 K 线 + 对账，返回可打印结构。"""
    settings = get_settings()
    db_path = settings.db_path
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")

    end = until or datetime.now().strftime("%Y-%m-%d")
    conn = _connect(db_path)
    try:
        equity_rows = _load_equity(conn, since or "0000", end)
        states = _load_regime_states(conn, since or "0000", end)
    finally:
        conn.close()

    equity = {str(r["date"]): float(r["total_asset"]) for r in equity_rows}
    if len(equity) < 2:
        return {"error": f"区间内净值快照仅 {len(equity)} 天"
                         "（paper_equity 由每日收盘结算写入）"}

    first = since or min(equity)
    load_from = (datetime.strptime(first, "%Y-%m-%d")
                 - timedelta(days=max(1, lookback_days))).strftime("%Y-%m-%d")
    raw = _load_raw(settings.kline_dir, load_from, end)
    if not raw:
        return {"error": f"K 线缓存为空：{settings.kline_dir}"}
    dates = _trading_dates(raw)
    return reconcile(equity, dates, pools, raw, _close_index(raw), states)


def main() -> int:
    parser = argparse.ArgumentParser(description="实盘对账：净值 vs 等权基准")
    parser.add_argument("--since", default="", help="起始日 YYYY-MM-DD（含，默认取最早快照）")
    parser.add_argument("--until", default="", help="结束日 YYYY-MM-DD（含，默认今天）")
    parser.add_argument("--pools", default="500,300",
                        help="基准池大小（逗号分隔，默认 500,300）")
    parser.add_argument("--lookback-days", type=int, default=90,
                        help="起始日之前多读多少天 K 线（默认 90）")
    parser.add_argument("--md", action="store_true", help="输出 Markdown")
    args = parser.parse_args()

    try:
        pools = tuple(sorted({int(x) for x in args.pools.split(",") if x.strip()}))
    except ValueError as exc:
        raise SystemExit(f"--pools 应为逗号分隔的整数：{args.pools}") from exc

    report = build_report(args.since, args.until, pools, args.lookback_days)
    print(render_markdown(report) if args.md else render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
