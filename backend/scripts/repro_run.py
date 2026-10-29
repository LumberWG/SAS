"""复跑历史上某一次回测：参数与窗口直接从 ``backtest_runs`` 读，保证可复现。

为什么需要它：回测结果落库时同时保存了 ``params``（全部 PipelineParams 覆盖值）
与 ``start_date/end_date``，但事后往往只记得结论、不记得条件（BACKTEST_LOG #48 的教训）。
本脚本把「库里那一行」变成「可重跑的一次实验」，同参数两次独立进程应逐位相同。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\repro_run.py 11
    .venv\\Scripts\\python.exe scripts\\repro_run.py --list      # 列出最近 20 次

参数差异只来自代码状态或数据本身——如果同参数结果不同，先查工作区是否有未提交改动。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest.pipeline import PipelineParams, run_pipeline  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "sas.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"找不到回测数据库：{db_path}")
    return sqlite3.connect(db_path)


def list_runs(db: sqlite3.Connection, limit: int = 20) -> None:
    rows = db.execute(
        "SELECT id, start_date, end_date, metrics, created_at FROM backtest_runs "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    print(f"{'id':>4} | {'created_at':19} | {'window':24} | {'ret':>8} | {'bench':>8}")
    for rid, start, end, metrics, created in rows:
        m = json.loads(metrics or "{}")
        ret = m.get("total_return")
        bench = m.get("benchmark_return")
        print(
            f"{rid:>4} | {str(created)[:19]:19} | {start}~{end} | "
            f"{ret if ret is None else f'{ret:.2f}%':>8} | "
            f"{bench if bench is None else f'{bench:.2f}%':>8}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="按 backtest_runs 保存的参数复跑回测")
    ap.add_argument("run_id", nargs="?", type=int, help="backtest_runs.id")
    ap.add_argument("--list", action="store_true", help="列出最近的回测记录")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    args = ap.parse_args()

    db = _connect(Path(args.db))
    if args.list or args.run_id is None:
        list_runs(db)
        return

    row = db.execute(
        "SELECT start_date, end_date, params FROM backtest_runs WHERE id=?",
        (args.run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"backtest_runs 里没有 id={args.run_id}")
    start_date, end_date, params_json = row

    kwargs = json.loads(params_json or "{}")
    kwargs.setdefault("start_date", start_date)
    kwargs.setdefault("end_date", end_date)
    # 请求体里 board_prefixes 是 list，PipelineParams 要 tuple
    if kwargs.get("board_prefixes") is not None:
        kwargs["board_prefixes"] = tuple(kwargs["board_prefixes"])

    res = asyncio.run(run_pipeline(PipelineParams(**kwargs)))
    m = res["metrics"]
    print(
        f"REPRO id={args.run_id} window={start_date}~{end_date} "
        f"ret={m['total_return']} bench={m['benchmark_return']} "
        f"mdd={m['max_drawdown']} trades={m['trade_count']} win={m['win_rate']}"
    )


if __name__ == "__main__":
    main()
