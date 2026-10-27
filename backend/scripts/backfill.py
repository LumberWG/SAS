"""补齐历史 K 线：把 2023 年起的日线合并进本地 Parquet 缓存（用于 2024 年回测）。

为什么不用主源：东财 `stock_zh_a_hist` 在部分网络下会被拒（RemoteDisconnected），
而腾讯/新浪的接口能一次返回 800~1000 根（够覆盖 2023-07 起）。代价是这两家
**不返回换手率**，2024 段数据的 `turnover_rate` 为空 → 14 因子里的「换手率」因子
恒为 0 分。因此 2024 窗口只能用于「结构稳健性」检查，不能与 2025-2026 的绝对
分数直接比较（见 BACKTEST_LOG 记录 #9）。

写入用「临时文件 + os.replace」，避免和正在运行的服务/扫描抢同一个 parquet 文件。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts/backfill.py --limit 5     # 试跑
    .venv\\Scripts\\python.exe scripts/backfill.py --workers 6   # 全量
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402
from app.data.fallback import code_to_symbol, sina_kline, tencent_kline  # noqa: E402

_lock = threading.Lock()
_stat = {"ok": 0, "skip": 0, "fail": 0}


def _codes() -> list[str]:
    """本地 K 线缓存里的个股代码（排除 idx_ 指数文件）。"""
    d = get_settings().kline_dir
    out = []
    for name in os.listdir(d):
        if name.endswith(".parquet"):
            code = name.split(".")[0]
            if code.isdigit():
                out.append(code)
    return sorted(out)


def _merge_atomic(code: str, new_df: pd.DataFrame) -> None:
    """与缓存合并去重后原子写入（先写 .tmp 再 replace）。"""
    path = kline_cache._path(code)  # noqa: SLF001 - 复用缓存层路径规则
    cached = kline_cache.get(code)
    if cached is not None and not cached.empty:
        # 已有行优先（东财口径带换手率），只补充缺失的日期
        new_df = pd.concat([cached, new_df], ignore_index=True)
    new_df = new_df.drop_duplicates(subset=["date"], keep="first")
    new_df = new_df.sort_values("date").reset_index(drop=True)
    tmp = path.with_suffix(".parquet.tmp")
    new_df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _fetch(code: str, count: int, datalen: int) -> pd.DataFrame:
    """腾讯优先（一次 800 根），失败退新浪（1023 根）。"""
    try:
        df = tencent_kline(code, adjust="qfq", count=count)
        if df is not None and not df.empty:
            return df
    except Exception:  # noqa: BLE001 - 换下一家
        pass
    return sina_kline(code_to_symbol(code), datalen=datalen)


def _backfill_one(code: str, start: str, count: int, datalen: int) -> str:
    try:
        cached = kline_cache.get(code)
        if cached is not None and str(cached["date"].iloc[0]) <= start:
            return "skip"
        df = _fetch(code, count, datalen)
        if df is None or df.empty or "date" not in df:
            return "fail"
        df = df[df["date"] >= start]
        if df.empty:
            return "fail"
        _merge_atomic(code, df)
        return "ok"
    except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
        print(f"  失败 {code}: {type(exc).__name__} {exc}", flush=True)
        return "fail"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-07-01")
    parser.add_argument("--count", type=int, default=800, help="腾讯接口返回根数")
    parser.add_argument("--datalen", type=int, default=1023, help="新浪接口返回根数")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--codes", default="")
    args = parser.parse_args()

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else _codes()
    )
    if args.limit:
        codes = codes[: args.limit]
    print(f"待补 {len(codes)} 只，起点 {args.start}，并发 {args.workers}", flush=True)

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_backfill_one, c, args.start, args.count, args.datalen): c
            for c in codes
        }
        for fut in as_completed(futures):
            status = fut.result()
            with _lock:
                _stat[status] += 1
                done += 1
                if done % 300 == 0:
                    print(
                        f"  进度 {done}/{len(codes)} | ok {_stat['ok']} skip {_stat['skip']} "
                        f"fail {_stat['fail']} | {time.time() - t0:.0f}s",
                        flush=True,
                    )
    print(
        f"完成：ok {_stat['ok']} / skip {_stat['skip']} / fail {_stat['fail']}，"
        f"用时 {time.time() - t0:.0f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
