"""按「底部反转」方式跑一次历史回测（复用 BacktestEngine + BottomReversal 策略）。

与前端「从底部起来」候选面板、scan_candidates 完全同口径：低位启动 + 20日高点回撤够 +
换手活跃 + 成交额不低。#181 起支持多板块：
  - ``--boards`` 指定股票池（与设置 bottom_boards 同款板块键），默认 cyb 以复现历史基线；
  - 趋势过滤按**每只票自己的板块指数**（创业板沿用等权合成/官方 399006，其余用各自官方指数）；
  - 涨跌停与申报数量由 app.board_rules 按板块区分（主板±10%/创业板科创板±20%/北交所±30%）。

  1) 用 BacktestEngine 跑真实组合回测（受 max_positions 约束，含止损/止盈/手续费）；
  2) 向量化计算「每个底部反转信号日后 N 日的前向收益」分布（不受持仓约束，直接看信号质量）。

用法（backend 目录）:
    .venv\\Scripts\\python.exe scripts\\bottom_reversal_backtest.py --start 2025-01-01 --end 2026-11-10
    .venv\\Scripts\\python.exe scripts\\bottom_reversal_backtest.py --start 2025-01-01 --end 2026-11-10 --boards cyb,sh_main,sz_main,star --short
    .venv\\Scripts\\python.exe scripts\\bottom_reversal_backtest.py --start 2026-09-01 --end 2026-11-10 --smoke
结果：markdown 报告打印到控制台 + data/logs/bottom_reversal_backtest.json + trades CSV。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from app.backtest.engine import BacktestEngine, BacktestParams
from app.config import get_settings

# 与 BottomReversal 策略 / scan_candidates 完全一致的阈值（2026-11-11 收紧口径）
START_POS_MAX = 0.40
DD20_MIN = 0.15
TURNOVER_MIN = 5.0  # 百分比单位(turnover_rate 已是百分比数值，如 5.0=5%)
MIN_AVG_AMOUNT = 50_000_000.0


def _data_root() -> str:
    """kline_dir 的父目录（data/），用于落盘结果。"""
    s = get_settings()
    return os.path.dirname(str(s.kline_dir).rstrip("/\\")) or "."


def board_codes(boards_raw: str) -> list[str]:
    """本地有日线缓存、且属于指定板块白名单的代码（避免联网）。

    ``boards_raw`` 与设置 ``bottom_boards`` 同款英文键（逗号分隔）。
    默认 ``cyb`` 用于复现历史基线；传与线上一致的值即可按多板块口径回测。
    """
    from app.analysis.board_index import classify_board

    s = get_settings()
    kdir = str(s.kline_dir)
    boards = {b.strip() for b in str(boards_raw or "").split(",") if b.strip()}
    codes: list[str] = []
    for f in glob.glob(os.path.join(kdir, "*.parquet")):
        c = os.path.splitext(os.path.basename(f))[0]
        if c.startswith("idx_"):
            continue
        if classify_board(c) in boards:
            codes.append(c)
    return codes


# 兼容旧调用名
cyb_codes = lambda: board_codes("cyb")  # noqa: E731


async def run_engine(codes, start, end, max_positions, initial, max_hold_days,
                     trend_filter=False, trend_ma=60, trend_index=None,
                     time_stop_days=0, trailing_pct=0.0, warmup_bars=None):
    """真实组合回测（受持仓上限与最大持有期约束；支持时间止损+跟踪止损主动离场）。

    ``warmup_bars`` 给短窗口回测用：BottomReversal.min_bars=130，默认 90 自然日
    预热只有约 60 根 K 线，窗口起点之前的历史不够，整池无信号。调大预热后，
    净值曲线里的预热段（恒为初始资金）必须剔除再重算绩效，否则夏普/年化被稀释。
    """
    params = BacktestParams(
        strategy="创业板底部反转",
        start_date=start,
        end_date=end,
        initial_capital=initial,
        max_positions=max_positions,
        codes=codes,
        max_hold_days=max_hold_days,
        trend_filter=trend_filter,
        trend_ma=trend_ma,
        trend_index=trend_index,
        time_stop_days=time_stop_days,
        trailing_pct=trailing_pct,
    )
    if warmup_bars:
        import app.backtest.engine as _bte
        _bte._WARMUP_BARS = int(warmup_bars)
    t0 = time.time()
    result = await BacktestEngine().run(params)
    if warmup_bars:
        # 预热段只是给指标/策略凑历史，不应计入净值曲线与绩效
        eq = [p for p in result.get("equity_curve", [])
              if str(p.get("date", ""))[:10] >= start]
        result["equity_curve"] = eq
        result["metrics"] = BacktestEngine()._compute_metrics(
            eq, result.get("trades", []), params)
    result["_elapsed_sec"] = round(time.time() - t0, 1)
    return result


def signal_forward_returns(codes, start, end, trend_filter=False, trend_ma=60,
                           trend_index=None, windows=(5, 10, 20)) -> dict:
    """向量化计算所有底部反转信号日后的前向收益分布（不受持仓约束）。

    与策略 evaluate 同口径（2026-11-11 收紧口径）：
      start_pos[t]  = (close[t-21]-roll_min120)/(roll_max120-roll_min120)  <= START_POS_MAX
      dd20[t]       = 1 - close[t]/roll_max20                            >= DD20_MIN
      turnover[t]   = 当日换手率                                          >= TURNOVER_MIN
      avg_amount20  = 近20日均成交额                                       >= MIN_AVG_AMOUNT
    前向收益 = close[t+N]/close[t]-1（信号日收盘买入、N 日后收盘卖出）。

    趋势过滤：板块感知——每只票只统计「信号日落在自己板块指数上升通道」的样本
    （创业板用等权合成/官方 399006，其余板块用各自官方指数；无指数数据的板块不阻断），
    与组合回测 BacktestEngine 的 board_uptrend 口径一致。

    windows：前向收益观察窗口（日）。短线交易者关注 3/5 日（<=1 周）。

    注意：滚动窗口必须基于「完整历史」计算，再取信号日落在 [start,end] 的部分，
    否则窗口内早期样本因历史不足全为 NaN（首次冒烟即踩此坑）。
    """
    from app.backtest.engine import compute_cyb_uptrend_dates
    from app.analysis.board_index import board_uptrend_set, classify_board

    uptrend_by_board: dict[str, set[str] | None] = {}
    if trend_filter:
        kdir = str(get_settings().kline_dir)
        for b in {classify_board(c) for c in codes}:
            if b == "cyb":
                uptrend_by_board[b] = compute_cyb_uptrend_dates(
                    kdir, trend_ma, index_symbol=trend_index)
            else:
                uptrend_by_board[b] = board_uptrend_set(kdir, b, trend_ma)
    fwd = {n: [] for n in windows}
    n_sig = 0
    for code in codes:
        s = get_settings()
        path = os.path.join(str(s.kline_dir), f"{code}.parquet")
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_parquet(path).sort_values("date").reset_index(drop=True)
        except Exception:
            continue
        if len(df) < 121:
            continue
        close = df["close"].astype(float)
        roll_min = close.rolling(120).min()
        roll_max = close.rolling(120).max()
        start_pos = (close.shift(21) - roll_min) / (roll_max - roll_min)
        roll_max20 = close.rolling(20).max()
        dd20 = 1.0 - close / roll_max20
        turnover = df["turnover_rate"].astype(float)
        avg_amount20 = df["amount"].astype(float).rolling(20).mean()
        sig = (
            (start_pos <= START_POS_MAX)
            & (dd20 >= DD20_MIN)
            & (turnover >= TURNOVER_MIN)
            & (avg_amount20 >= MIN_AVG_AMOUNT)
        )
        sig = sig.fillna(False)
        # 仅统计信号日落在回测窗口内的样本
        win_mask = (df["date"] >= start) & (df["date"] <= end)
        sig_in = sig & win_mask
        if trend_filter:
            ups = uptrend_by_board.get(classify_board(code))
            if ups is not None:  # None = 该板块缺指数数据，不阻断
                sig_in = sig_in & df["date"].isin(ups)
        n_sig += int(sig_in.sum())
        for n in fwd:
            fr = close.shift(-n) / close - 1.0
            vals = fr[sig_in].dropna().to_numpy()
            fwd[n].extend(vals.tolist())
    out = {"signal_days": n_sig, "windows": {}}
    for n, vals in fwd.items():
        vals = [v for v in vals if v is not None]
        arr = pd.Series(vals) if vals else pd.Series([0.0])
        out["windows"][n] = {
            "count": len(vals),
            "median": round(float(arr.median()) * 100, 2),
            "mean": round(float(arr.mean()) * 100, 2),
            "win_rate": round(float((arr > 0).mean()) * 100, 1),
            "p25": round(float(arr.quantile(0.25)) * 100, 2),
            "p75": round(float(arr.quantile(0.75)) * 100, 2),
        }
    return out


def _serialize(result: dict) -> dict:
    """把 Trade/Position 等对象转成可 JSON 序列化的字典。"""
    def conv(o):
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)
    return json.loads(json.dumps(result, default=conv, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-11-10")
    ap.add_argument("--max-positions", type=int, default=15)
    ap.add_argument("--initial", type=float, default=1_000_000)
    ap.add_argument("--max-hold-days", type=int, default=20,
                    help="单笔最大持有天数(0=不限)；建议与信号前向收益窗口对齐")
    ap.add_argument("--trend-filter", action="store_true",
                    help="板块感知趋势过滤：仅个股所属板块指数处于上升通道时开仓")
    ap.add_argument("--trend-ma", type=int, default=60,
                    help="趋势过滤均线周期(日)，默认60")
    ap.add_argument("--trend-index", type=str, default=None,
                    help="趋势过滤所用指数（如 sz399006=官方创业板指）；仅作用于 cyb 板块，缺省=300/301等权合成指数（v8基线）")
    ap.add_argument("--boards", default="cyb",
                    help="回测股票池板块（逗号分隔，与设置 bottom_boards 同款键："
                         "cyb/sh_main/sz_main/star/bse）。默认 cyb=单创业板以复现历史基线；"
                         "要与线上多板块一致请传 --boards cyb,sh_main,sz_main,star")
    ap.add_argument("--time-stop-days", type=int, default=None,
                    help="主动离场-时间止损天数(默认读 config.bottom_time_stop_days)")
    ap.add_argument("--trailing-pct", type=float, default=None,
                    help="主动离场-跟踪止损比例(默认读 config.bottom_trailing_pct)")
    ap.add_argument("--min-factor-score", type=float, default=None,
                    help="14因子分软闸下限(默认读 config.bottom_min_factor_score；-100=关闭)")
    ap.add_argument("--windows", type=int, nargs="+", default=None,
                    help="信号前向收益观察窗口(日)，默认 5 10 20；短线建议 3 5 10")
    ap.add_argument("--short", action="store_true",
                    help="短线模式：max-hold-days=5，前向窗口默认 3 5 10（<=1周持有）")
    ap.add_argument("--smoke", action="store_true", help="仅小窗口冒烟，不跑全量信号分析")
    ap.add_argument("--out", default="bottom_reversal_backtest",
                    help="输出文件基名(不含扩展名)，落盘到 data/logs/")
    ap.add_argument("--offline", action="store_true",
                    help="回测只读本地 parquet，不做联网增量更新（多窗口批量回测必开，"
                         "否则每个进程都会对上千只票逐只回源检查，被数据源限流后极慢）")
    ap.add_argument("--warmup", type=int, default=None,
                    help="预热自然日数（默认 90）。短窗口(<=3个月)必须调到 250 以上："
                         "BottomReversal.min_bars=130，预热不足会让窗口内全程无信号。"
                         "指定后会剔除净值曲线中的预热段并重算绩效，不改变全年基线默认口径")
    args = ap.parse_args()

    if args.offline:
        # get_kline_cached 在每个新进程首次访问某只票时，因 _last_net_fetch 为空
        # 会走一次联网增量检查；回测本就用固定历史数据，不该受数据源抖动影响。
        # 把节流间隔设为无穷大后 `since <= _NET_MIN_INTERVAL` 恒成立，
        # 命中本地缓存即直接返回，完全不回源。
        import app.data.service as _svc
        _svc._NET_MIN_INTERVAL = float("inf")
        print("[*] 离线模式：仅读本地 parquet，跳过联网增量更新")

    # 短线模式：贴合「持有 <=1 周」的打法
    if args.short:
        if args.windows is None:
            args.windows = [3, 5, 10]
        if args.max_hold_days == 20:   # 仅当用户未显式覆盖时才改成 5
            args.max_hold_days = 5
    else:
        if args.windows is None:
            args.windows = [5, 10, 20]

    codes = board_codes(args.boards)
    print(f"[*] 股票池(本地缓存, 板块={args.boards}): {len(codes)} 只")
    if args.boards != "cyb":
        print("[*] 注意：多板块口径的涨跌停/申报数量/趋势指数均已按板块区分，"
              "结果不可与单 cyb 历史基线直接对比")
    print(f"[*] 回测区间: {args.start} ~ {args.end}")
    print(f"[*] 模式: {'短线(<=1周)' if args.short else '常规'} | "
          f"最大持有={args.max_hold_days}日 | 前向窗口={args.windows}日")
    if args.trend_filter:
        print(f"[*] 板块感知趋势过滤: 启用 (各板块指数>{args.trend_ma}日MA 才开仓"
              f"；cyb {'官方指数=' + args.trend_index if args.trend_index else '等权合成指数(基线)'}，其余用各自官方指数)")

    # 主动离场参数（CLI 优先，否则读 config）
    s = get_settings()
    time_stop_days = args.time_stop_days if args.time_stop_days is not None \
        else s.bottom_time_stop_days
    trailing_pct = args.trailing_pct if args.trailing_pct is not None \
        else s.bottom_trailing_pct

    # 14 因子分软闸（CLI 优先，否则读 config）；-100=关闭。直接改策略类默认，
    # 因为 BacktestEngine 内部 registry.create 用策略声明默认值。
    min_factor_score = args.min_factor_score if args.min_factor_score is not None \
        else s.bottom_min_factor_score
    from app.strategies.builtin import BottomReversal
    BottomReversal.parameters["min_factor_score"].default = float(min_factor_score)
    print(f"[*] 14因子分软闸下限: {min_factor_score} (-100=关闭)")

    # 1) 引擎真实组合回测
    result = asyncio.run(run_engine(codes, args.start, args.end,
                                    args.max_positions, args.initial,
                                    args.max_hold_days,
                                    trend_filter=args.trend_filter,
                                    trend_ma=args.trend_ma,
                                    trend_index=args.trend_index,
                                    time_stop_days=time_stop_days,
                                    trailing_pct=trailing_pct,
                                    warmup_bars=args.warmup))

    # 2) 信号前向收益分布（不受持仓约束）
    fwd = None
    if not args.smoke:
        print("[*] 计算信号前向收益分布（向量化）...")
        t0 = time.time()
        fwd = signal_forward_returns(codes, args.start, args.end,
                                    trend_filter=args.trend_filter, trend_ma=args.trend_ma,
                                    trend_index=args.trend_index,
                                    windows=tuple(args.windows))
        print(f"    信号日数={fwd['signal_days']}, 耗时 {time.time()-t0:.1f}s")

    # ---- 指标 ----
    m = result.get("metrics", {})
    eq = result.get("equity_curve", [])
    trades = result.get("trades", [])
    final_eq = eq[-1]["equity"] if eq else args.initial
    # 与 _compute_metrics 同口径：按「历史累积峰值」算回撤。旧写法用全区间最高点
    # 当分母，净值先涨后跌时会把回撤显著放大（例如 +32% 窗口显示 -31.78%，
    # 引擎口径其实只有 -10.37%），两个数字对不上会误导结论。
    eqs = pd.Series([e["equity"] for e in eq], dtype=float)
    mdd = float((eqs / eqs.cummax() - 1).min()) * 100 if len(eqs) else 0.0

    print("\n" + "=" * 56)
    print("  底部反转 · 多板块历史回测结果")
    print("=" * 56)
    print(f"  区间          : {args.start} ~ {args.end}")
    print(f"  股票池        : {result['params']['universe_size']} 只（板块 {args.boards}）")
    print(f"  初始资金      : {args.initial:,.0f} 元")
    print(f"  最大持仓      : {args.max_positions} 只")
    print(f"  总收益率      : {m.get('total_return')} %")
    print(f"  期末净值      : {final_eq:,.0f} 元")
    print(f"  最大回撤      : {round(mdd, 2)} %  (引擎口径: {m.get('max_drawdown')})")
    print(f"  夏普          : {m.get('sharpe')}")
    print(f"  交易次数      : {m.get('trade_count')}")
    print(f"  胜率          : {m.get('win_rate')} %")
    print(f"  回测耗时      : {result.get('_elapsed_sec')} s")

    if fwd:
        print("\n  -- 信号前向收益（不受持仓约束，信号日收盘买、N日后卖）--")
        for n in args.windows:
            w = fwd["windows"][n]
            print(f"   +{n}日: 样本={w['count']} 中位={w['median']}% 均值={w['mean']}% "
                  f"胜率={w['win_rate']}% [P25={w['p25']}%, P75={w['p75']}%]")

    # ---- 落盘 ----
    root = _data_root()
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_json = os.path.join(log_dir, f"{args.out}.json")
    payload = {
        "start": args.start, "end": args.end,
        "max_positions": args.max_positions, "initial": args.initial,
        "short_mode": args.short, "windows": args.windows,
        "boards": args.boards, "trend_filter": bool(args.trend_filter),
        "offline": bool(args.offline), "warmup_bars": args.warmup,
        "engine_result": _serialize(result),
        "signal_forward": fwd,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[*] JSON -> {out_json}")

    # trades CSV
    if trades:
        csv_path = os.path.join(log_dir, f"{args.out}_trades.csv")
        cols = ["code", "name", "buy_date", "sell_date", "buy_price",
                "sell_price", "shares", "pnl", "pnl_pct", "reason"]
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            import csv
            w = csv.writer(f)
            w.writerow(cols)
            for t in trades:
                d = t.__dict__ if hasattr(t, "__dict__") else t
                w.writerow([d.get(c, "") for c in cols])
        print(f"[*] trades CSV -> {csv_path}")


if __name__ == "__main__":
    main()
