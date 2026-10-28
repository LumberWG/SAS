"""补齐退市股历史 K 线（消除回测池的幸存者偏差）。

背景（BACKTEST_LOG 记录 #36 下一步优先级 3）：
本地缓存是「当前在市股票 + 少量退市残片」。2023-2026 全市场退市 150+ 家，
我们只有 21 只残片，而退市股恰恰多在放量下跌 / 被 ST 之后才退市——
缺掉它们，回测池（成交额 TopN）天然避开了最差的那部分 → 绝对收益偏高。

数据源**只用新浪**（``sina_kline`` 的 ``datalen`` ≤ 1023 ≈ 4 年）。实测各源对退市股的覆盖：

| 来源 | 600355（2026-04 退市） | 000005（2024-03 退市） |
| --- | --- | --- |
| 东财 `stock_zh_a_hist` | 300 根，从 2025-01 起 | 300 根，从 2022-12 起 |
| 腾讯 `fqkline` | 800 根，从 2022-12 起 | 800 根，从 2020-11 起 |
| 新浪 `getKLineData` | 1023 根，从 2022-01 起 | 1023 根，从 2019-12 起 |

→ 东财对退市股只给最近 300 根，不够；新浪能覆盖我们 2022-09 起的回测窗口。
代价：新浪不带换手率，这批股票 14 因子里的换手率分项为 0（与记录 #17 同一问题）。

写入走「临时文件 + os.replace」，且**已有行优先**（保留东财口径的换手率）。

用法（backend 目录）::

    .venv\\Scripts\\python.exe scripts/backfill_delisted.py --dry-run      # 只看清单
    .venv\\Scripts\\python.exe scripts/backfill_delisted.py --workers 4     # 正式补齐
    .venv\\Scripts\\python.exe scripts/backfill_delisted.py --limit 20      # 试跑 20 只
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.data.cache import kline_cache  # noqa: E402
from app.data.fallback import code_to_symbol, sina_kline  # noqa: E402

#: 只关心与本地 K 线缓存（2022-08 起）重叠的退市股；更早退市的股票拉不到数据也没用
DEFAULT_SINCE = "2022-09-01"
#: 新浪一次最多约 1023 根（≈4 年），足够覆盖 2022-09 至今
SINA_DATALEN = 1023
#: 退市前常有长期停牌，缓存最后 bar 比退市日早几天属正常 → 算"已补齐"的容差
STOP_TOLERANCE_DAYS = 10
#: 单只重试次数（新浪有风控，失败退避后重试）
RETRIES = 3

_stat = {"ok": 0, "skip": 0, "empty": 0, "fail": 0}
_lock = threading.Lock()


def delisted_list(since: str = DEFAULT_SINCE) -> pd.DataFrame:
    """沪深退市公司清单（code / name / list_date / delist_date），按退市日期过滤。"""
    import akshare as ak

    specs = [
        (
            lambda: ak.stock_info_sh_delist(symbol="全部"),
            {"公司代码": "code", "公司简称": "name",
             "上市日期": "list_date", "暂停上市日期": "delist_date"},
        ),
        (
            lambda: ak.stock_info_sz_delist(symbol="终止上市公司"),
            {"证券代码": "code", "证券简称": "name",
             "上市日期": "list_date", "终止上市日期": "delist_date"},
        ),
    ]
    parts: list[pd.DataFrame] = []
    for fetch, mapping in specs:
        df = fetch()
        if df is None or df.empty:
            continue
        df = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})
        parts.append(df.reindex(columns=["code", "name", "list_date", "delist_date"]))
    if not parts:
        return pd.DataFrame(columns=["code", "name", "list_date", "delist_date"])

    out = pd.concat(parts, ignore_index=True)
    out["code"] = out["code"].astype(str).str.strip().str.zfill(6)
    for col in ("list_date", "delist_date"):
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime("%Y-%m-%d")
    out = out[out["delist_date"].notna() & (out["delist_date"] >= since)]
    return out.drop_duplicates("code").sort_values("delist_date").reset_index(drop=True)


def _merge_atomic(code: str, new_df: pd.DataFrame) -> None:
    """与缓存合并去重后原子写入（已有行优先，保留东财口径的换手率）。"""
    path = kline_cache._path(code)  # noqa: SLF001 - 复用缓存层路径规则
    cached = kline_cache.get(code)
    if cached is not None and not cached.empty:
        new_df = pd.concat([cached, new_df], ignore_index=True)
    new_df = new_df.drop_duplicates(subset=["date"], keep="first")
    new_df = new_df.sort_values("date").reset_index(drop=True)
    tmp = path.with_suffix(".parquet.tmp")
    new_df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _fetch(code: str, datalen: int) -> pd.DataFrame:
    """新浪日线，失败退避重试（新浪风控会偶发空响应）。"""
    last_exc: Exception | None = None
    for i in range(RETRIES):
        try:
            df = sina_kline(code_to_symbol(code), datalen=datalen)
            if df is not None and not df.empty:
                return df
        except Exception as exc:  # noqa: BLE001 - 退避后重试
            last_exc = exc
        time.sleep(1.5 * (i + 1))
    if last_exc is not None:
        raise last_exc
    return pd.DataFrame()


def _one(row: dict, datalen: int, dry_run: bool) -> dict:
    """补一只退市股，返回结果行（status: ok/skip/empty/fail）。"""
    code = row["code"]
    base = {"code": code, "name": row["name"], "delist_date": row["delist_date"]}
    try:
        cached = kline_cache.get(code)
        if cached is not None and not cached.empty:
            last = str(cached["date"].iloc[-1])[:10]
            floor = (
                pd.Timestamp(row["delist_date"]) - pd.Timedelta(days=STOP_TOLERANCE_DAYS)
            ).strftime("%Y-%m-%d")
            if last >= floor:
                return {**base, "status": "skip", "bars": int(len(cached)),
                        "first": str(cached["date"].iloc[0])[:10], "last": last}

        df = _fetch(code, datalen)
        if df is None or df.empty:
            return {**base, "status": "empty", "bars": 0, "first": None, "last": None}

        # 退市日之后的行没有意义（代码不会被复用，但接口偶尔给出脏数据）
        df = df[df["date"].astype(str).str[:10] <= row["delist_date"]]
        if df.empty:
            return {**base, "status": "empty", "bars": 0, "first": None, "last": None}
        if not dry_run:
            _merge_atomic(code, df)
        return {**base, "status": "ok", "bars": int(len(df)),
                "first": str(df["date"].iloc[0])[:10], "last": str(df["date"].iloc[-1])[:10]}
    except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
        print(f"  失败 {code}: {type(exc).__name__} {exc}", flush=True)
        return {**base, "status": "fail", "bars": 0, "first": None, "last": None,
                "error": f"{type(exc).__name__}: {exc}"}


def _log_dir() -> str:
    out = str(get_settings().kline_dir.parent / "logs")
    os.makedirs(out, exist_ok=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=DEFAULT_SINCE, help="只补退市日期不早于此的股票")
    parser.add_argument("--datalen", type=int, default=SINA_DATALEN, help="新浪返回根数（≤1023）")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--codes", default="", help="指定代码（逗号分隔），默认全部退市股")
    parser.add_argument("--dry-run", action="store_true", help="只列清单不写缓存")
    parser.add_argument("--tag", default="", help="结果文件名后缀")
    args = parser.parse_args()

    print(f"拉取沪深退市清单（退市日期 ≥ {args.since}）…", flush=True)
    lst = delisted_list(args.since)
    if args.codes:
        wanted = {c.strip().zfill(6) for c in args.codes.split(",") if c.strip()}
        lst = lst[lst["code"].isin(wanted)]
    if args.limit:
        lst = lst.head(args.limit)
    if lst.empty:
        raise SystemExit("没有匹配的退市股（检查 --since / --codes）")

    by_year = lst["delist_date"].str[:4].value_counts().sort_index()
    print(f"待处理 {len(lst)} 只："
          + "，".join(f"{y} 年 {n} 只" for y, n in by_year.items()), flush=True)

    if args.dry_run:
        print("\n| 代码 | 名称 | 上市 | 退市 | 缓存现状 |")
        print("| --- | --- | --- | --- | --- |")
        for r in lst.head(40).itertuples(index=False):
            cached = kline_cache.get(r.code)
            now = (f"{len(cached)} 根，最后 {str(cached['date'].iloc[-1])[:10]}"
                   if cached is not None and not cached.empty else "无缓存")
            print(f"| {r.code} | {r.name} | {r.list_date} | {r.delist_date} | {now} |")
        if len(lst) > 40:
            print(f"\n（只显示前 40 只，共 {len(lst)} 只）")

    t0 = time.time()
    results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_one, {"code": r.code, "name": r.name, "delist_date": r.delist_date},
                        args.datalen, args.dry_run)
            for r in lst.itertuples(index=False)
        ]
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            with _lock:
                _stat[res["status"]] += 1
                done += 1
                if done % 50 == 0:
                    print(f"  进度 {done}/{len(lst)} | ok {_stat['ok']} skip {_stat['skip']} "
                          f"empty {_stat['empty']} fail {_stat['fail']} | "
                          f"{time.time() - t0:.0f}s", flush=True)

    results.sort(key=lambda r: r["delist_date"])
    print(f"\n完成：ok {_stat['ok']} / skip {_stat['skip']} / empty {_stat['empty']} "
          f"/ fail {_stat['fail']}，用时 {time.time() - t0:.0f}s"
          + ("（dry-run，未写缓存）" if args.dry_run else ""), flush=True)

    log_dir = _log_dir()
    with open(os.path.join(log_dir, "delisted_list.json"), "w", encoding="utf-8") as fh:
        json.dump(lst.to_dict(orient="records"), fh, ensure_ascii=False, indent=1)
    suffix = f"_{args.tag}" if args.tag else ""
    out = os.path.join(log_dir, f"delisted_backfill{suffix}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "since": args.since,
            "datalen": args.datalen,
            "dry_run": args.dry_run,
            "stat": dict(_stat),
            "results": results,
        }, fh, ensure_ascii=False, indent=1)
    print(f"结果已保存：{os.path.abspath(out)}")


if __name__ == "__main__":
    main()
