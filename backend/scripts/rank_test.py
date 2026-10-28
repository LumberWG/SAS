"""排序质量直测（Rank Test）：不掺仓位 / 门槛 / 止损，只看「打分排名」有没有选股力。

为什么单独测：BACKTEST_LOG 记录 #25 证明每日可用仓位（`slots`）通常只有 0~2 个，
组合收益里的噪声主要来自仓位、止损与择时，选股模型的差异会被完全盖住
（`pct20` 与 `default` 在三个窗口逐位相同就是证据）。所以这里直接量最底层的问题：

    每日在股票池内按打分排序 → 取 Top-k → 等权持有 h 日 → 统计

口径：
  - 股票池：每日成交额 TopN（默认再用 ``MAIN_BOARD_PREFIXES`` 去掉科创/北交，与
    ``scripts/factor_ic.py`` 一致；``--all-boards`` 关闭过滤）；
  - 截面：每 ``--step`` 个交易日取一次（默认 5），避免 h 日窗口高度重叠；
  - 收益：``close[t+h] / close[t] - 1``（纯排序检验，不模拟次日开盘成交）；
  - 超额：Top-k 的平均收益 − **当日池内等权平均收益**，用来去掉市场 beta；
    否则普涨月的任何方案都会显得"有效"；
  - 胜率：Top-k 中 h 日收益 > 0 的比例。

对比的方案（打分方向见 ``SCHEMES``）：``origin`` 原方向、``rev_inv`` 整体取负、
``rev_ic1``/``rev_ic2`` 按 IC 强度加权后取负（见记录 #22/#24）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\rank_test.py --windows A,B,C,D
    .venv\\Scripts\\python.exe scripts\\rank_test.py --windows G,H --step 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis import factors  # noqa: E402
from app.analysis.factors import compute_factors  # noqa: E402
from app.backtest.pipeline import (  # noqa: E402
    MAIN_BOARD_PREFIXES,
    PipelineParams,
    _prepare,
)
from app.backtest.profiles import _W_IC1, _W_IC2  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.search import resolve_windows  # noqa: E402

#: 方案名 -> (因子权重, 符号)。符号 −1 = 把打分翻过来（反转选股）。
SCHEMES: dict[str, tuple[dict | None, float]] = {
    "origin": (None, 1.0),
    "rev_inv": (None, -1.0),
    "rev_ic1": (_W_IC1, -1.0),
    "rev_ic2": (_W_IC2, -1.0),
}

MIN_BARS = 30  # compute_factors 的最低K线要求


def _score_panel(prepared: dict, step: int) -> dict[str, list[tuple]]:
    """逐截面算好各方案的总分：``{date: [(code, idx, {scheme: score})]}``。

    每个 ``(code, date)`` 只调用一次 ``compute_factors``，其余方案靠 ``reweight``
    从同一个 ``FactorScore`` 派生（只改权重，不重算因子）。
    """
    dates: list[str] = prepared["dates"]
    universe: dict[str, list[str]] = prepared["universe"]
    klines = prepared["klines"]
    pos_index = prepared["pos_index"]

    panel: dict[str, list[tuple]] = {}
    for date in dates[::step]:
        rows: list[tuple] = []
        for code in universe.get(date, []):
            idx = pos_index.get(code, {}).get(date)
            if idx is None or idx + 1 < MIN_BARS:
                continue
            df = klines[code]
            try:
                fs = compute_factors(df.iloc[: idx + 1])
            except Exception:  # noqa: BLE001 - 单只失败跳过
                continue
            scores: dict[str, float] = {}
            for name, (weights, sign) in SCHEMES.items():
                try:
                    total = factors.reweight(fs, weights).total if weights else fs.total
                except Exception:  # noqa: BLE001
                    continue
                scores[name] = sign * float(total)
            if scores:
                rows.append((code, idx, scores))
        if rows:
            panel[date] = rows
    return panel


def _forward(df, idx: int, horizon: int) -> float | None:
    """第 idx 行收盘买入、持有 horizon 个交易日后的收益；越界或坏值返回 None。"""
    j = idx + horizon
    if j >= len(df):
        return None
    try:
        p0 = float(df["close"].iloc[idx])
        p1 = float(df["close"].iloc[j])
    except (TypeError, ValueError):
        return None
    if p0 != p0 or p1 != p1 or p0 <= 0 or p1 <= 0:
        return None
    return p1 / p0 - 1


def _evaluate(
    panel: dict[str, list[tuple]], klines: dict, scheme: str, top: int, horizon: int
) -> dict:
    """单方案 × 单 (top, horizon)：平均收益、池内超额与胜率。"""
    rets: list[float] = []
    excess: list[float] = []
    for rows in panel.values():
        scored = [r for r in rows if scheme in r[2]]
        if not scored:
            continue
        scored.sort(key=lambda r: -r[2][scheme])
        pool = [
            v for v in (_forward(klines[c], i, horizon) for c, i, _ in scored) if v is not None
        ]
        if not pool:
            continue
        pool_mean = sum(pool) / len(pool)
        for code, idx, _ in scored[:top]:
            r = _forward(klines[code], idx, horizon)
            if r is None:
                continue
            rets.append(r)
            excess.append(r - pool_mean)
    if not rets:
        return {"n": 0, "mean_ret": 0.0, "mean_excess": 0.0, "win_rate": 0.0, "median_excess": 0.0}
    return {
        "n": len(rets),
        "mean_ret": round(sum(rets) / len(rets) * 100, 2),
        "mean_excess": round(sum(excess) / len(excess) * 100, 2),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "median_excess": round(statistics.median(excess) * 100, 2),
    }


def _run_window(
    name: str, start: str, end: str, universe: int, board: tuple | None, step: int,
    tops: tuple, horizons: tuple,
) -> dict:
    t0 = time.time()
    prepared = _prepare(PipelineParams(
        start_date=start, end_date=end, universe_size=universe, board_prefixes=board,
    ))
    panel = _score_panel(prepared, step)
    results = {
        f"{s}|{k}|{h}": _evaluate(panel, prepared["klines"], s, k, h)
        for s in SCHEMES
        for k in tops
        for h in horizons
    }
    print(
        f"[{name}] {start}~{end} 截面 {len(panel)} 个，"
        f"日均股票 {sum(len(v) for v in panel.values()) / max(len(panel), 1):.0f} 只，"
        f"耗时 {time.time() - t0:.1f}s",
        flush=True,
    )
    return {"window": name, "start": start, "end": end, "sections": len(panel),
            "results": results}


def _merge(windows: list[dict], tops: tuple, horizons: tuple) -> dict:
    """跨窗口汇总：按样本数加权平均（每格 n 不相同，直接算术平均会偏）。"""
    merged: dict[str, dict] = {}
    for s in SCHEMES:
        for k in tops:
            for h in horizons:
                key = f"{s}|{k}|{h}"
                cells = [w["results"][key] for w in windows if w["results"].get(key, {}).get("n")]
                n = sum(c["n"] for c in cells)
                if not n:
                    merged[key] = {"n": 0, "mean_excess": 0.0, "win_rate": 0.0}
                    continue
                merged[key] = {
                    "n": n,
                    "mean_excess": round(
                        sum(c["mean_excess"] * c["n"] for c in cells) / n, 2),
                    "win_rate": round(
                        sum(c["win_rate"] * c["n"] for c in cells) / n, 1),
                }
    return merged


def _print_table(title: str, data: dict, tops: tuple, horizons: tuple) -> None:
    heads = [f"Top{k}/{h}日" for k in tops for h in horizons]
    print(f"\n### {title}（超额% / 胜率%，括号为样本数）\n")
    print("| 方案 | " + " | ".join(heads) + " |")
    print("| --- | " + " | ".join("---" for _ in heads) + " |")
    for s in SCHEMES:
        cells = []
        for k in tops:
            for h in horizons:
                c = data.get(f"{s}|{k}|{h}", {})
                if not c.get("n"):
                    cells.append("n/a")
                else:
                    cells.append(f"{c['mean_excess']:+.2f} / {c['win_rate']:.1f}（{c['n']}）")
        print(f"| `{s}` | " + " | ".join(cells) + " |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", default="", help="逗号分隔，缺省=全部但排除样本外 G/H")
    parser.add_argument("--universe", type=int, default=300, help="每日成交额 TopN")
    parser.add_argument("--step", type=int, default=5, help="每隔几个交易日取一个截面")
    parser.add_argument("--tops", default="1,3", help="取打分前 k 只")
    parser.add_argument("--horizons", default="5,10,20", help="持有天数")
    parser.add_argument("--all-boards", action="store_true", help="不做板块过滤（含科创/北交）")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    args = parser.parse_args()

    tops = tuple(int(x) for x in args.tops.split(",") if x.strip())
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    board = None if args.all_boards else MAIN_BOARD_PREFIXES
    windows = resolve_windows(args.windows)

    out_windows: list[dict] = []
    for name, (start, end) in windows.items():
        out_windows.append(
            _run_window(name, start, end, args.universe, board, args.step, tops, horizons)
        )

    for w in out_windows:
        _print_table(f"窗口 {w['window']}（{w['start']}~{w['end']}）", w["results"], tops, horizons)
    merged = _merge(out_windows, tops, horizons)
    _print_table("全部窗口汇总（按样本数加权）", merged, tops, horizons)

    settings = get_settings()
    log_dir = str(settings.kline_dir.parent / "logs")
    os.makedirs(log_dir, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"rank_test{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "universe": args.universe,
            "step": args.step,
            "tops": list(tops),
            "horizons": list(horizons),
            "board_filter": bool(board),
            "schemes": {k: (list(v[0]) if v[0] else None, v[1]) for k, v in SCHEMES.items()},
            "windows": out_windows,
            "merged": merged,
        }, fh, ensure_ascii=False, indent=2, default=float)
    print(f"\n结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
