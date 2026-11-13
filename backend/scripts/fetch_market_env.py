"""隔夜市场环境快照采集 CLI（影子研究专用，不进任何买卖决策）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts\\fetch_market_env.py --backfill --from 2022-01-01
    .venv\\Scripts\\python.exe scripts\\fetch_market_env.py --daily        # 凌晨任务手工触发
    .venv\\Scripts\\python.exe scripts\\fetch_market_env.py --check
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.market.env_snapshot import backfill, build_daily, coverage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="隔夜市场环境快照（影子研究，禁止进决策）")
    ap.add_argument("--backfill", action="store_true", help="回填美股隔夜收益历史")
    ap.add_argument("--daily", action="store_true", help="生成当日凌晨快照（含政策快讯）")
    ap.add_argument("--check", action="store_true", help="查看覆盖状态")
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default=date.today().isoformat())
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="--daily 指定 A 股交易日（默认今天）")
    args = ap.parse_args()

    if not any((args.backfill, args.daily, args.check)):
        ap.print_help()
        return 0
    if args.backfill:
        print("[backfill]", json.dumps(backfill(args.start, args.end),
                                       ensure_ascii=False), flush=True)
    if args.daily:
        print("[daily]", json.dumps(build_daily(args.as_of),
                                    ensure_ascii=False), flush=True)
    if args.check:
        print("[check]", json.dumps(coverage(args.start, args.end),
                                    ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
