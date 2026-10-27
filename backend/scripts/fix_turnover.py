"""补齐 K 线缓存里缺失的换手率（用「成交量 ÷ 流通股本」反推）。

背景：东财主源不可达时，日线由腾讯（800 根）/新浪（1023 根）兜底，这两家
**不返回换手率**。实测缓存里 2025-11 之后约 56% 的股票 `turnover_rate` 为空：
14 因子的换手率分项恒为 0 分，E5 的 `min_turnover_rate` 过滤对一半股票不生效
（见 BACKTEST_LOG 记录 #9）。

反推口径：
    换手率(%) = 成交量(手) × 100股 / 流通股本(股) × 100
              = 成交量 × 10000 / 流通股本

流通股本优先用**该股自己历史行情的反推值**（同一行同时有成交量与换手率时
可直接解出流通股本），按 21 个观测取滚动中位数抑制两位小数舍入噪声
（换手率 0.01% 这种小数值反推误差可达数十个百分点），再向前/向后填充到缺口；
完全没有观测的股票，用腾讯批量行情（``tencent_quotes`` 的 ``float_shares``）兜底。
这样做的好处：流通股本发生真实变化（解禁/增发）时，缺口里用的仍是「最近的
已知值」，而不是用今天的股本回填三年前。

写入用「临时文件 + os.replace」，避免和正在运行的服务/扫描抢同一个 parquet 文件。
默认只统计不写盘，加 ``--write`` 才落盘。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe scripts/fix_turnover.py                 # 只看统计
    .venv\\Scripts\\python.exe scripts/fix_turnover.py --write        # 全量补齐
    .venv\\Scripts\\python.exe scripts/fix_turnover.py --write --limit 20
    .venv\\Scripts\\python.exe scripts/fix_turnover.py --validate 60  # 抽样误差检验
    .venv\\Scripts\\python.exe scripts/fix_turnover.py --write --fix-star

科创板附带修复：腾讯 K 线对 688/689 返回的成交量单位是「股」，历史上被当成
「手」落盘（成交量与估算成交额同时放大 100 倍）。``--fix-star`` 会重取一次
K 线，把「缓存/重取 ≈ 100」的行改回「手」，并清空这些行的换手率交给补齐逻辑
重算（它们的官方换手率同样是 100 倍的错误值）。
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.data.cache import (  # noqa: E402
    fill_turnover,
    float_shares_series,
    kline_cache,
)
from app.data.fallback import code_to_symbol, tencent_kline, tencent_quotes  # noqa: E402

#: 重取 K 线的根数（够覆盖缓存历史即可）
_REFETCH_COUNT = 800
#: 「股」当「手」写入的判定区间（缓存 / 重取）。完整历史行比值 ≈100；
#: 当日未收盘的行两边取的时点不同，比值会落在 5~100 之间，故下界放宽。
#: 正常行（同一口径）比值应 ≈1，取 5 不会误伤。
_UNIT_RATIO_LO, _UNIT_RATIO_HI = 5.0, 500.0


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


def _save_atomic(code: str, df: pd.DataFrame) -> None:
    path = kline_cache._path(code)  # noqa: SLF001 - 复用缓存层路径规则
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _refetch(code: str) -> pd.DataFrame:
    """重新拉取日线（腾讯已把科创板成交量统一为「手」）。"""
    return tencent_kline(code, adjust="qfq", count=_REFETCH_COUNT)


def fix_share_unit(df: pd.DataFrame, fresh: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """把「股」当「手」写进缓存的行改回正确量纲（科创板历史遗留）。

    判定：同一日期 ``缓存成交量 / 重取成交量`` 显著大于 1（重取结果已是「手」，
    完整历史行约为 100，当日未收盘行在 5~100 之间）。
    命中行用重取的成交量/成交额覆盖，换手率置空交给 ``fill_turnover`` 重算
    （这些行的官方换手率同样是 100 倍，不能保留）。

    Returns:
        ``(新df, 修正行数)``
    """
    if fresh is None or fresh.empty or df.empty:
        return df, 0
    ref = fresh.set_index("date")
    ref_vol = pd.to_numeric(df["date"].map(ref["volume"]), errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce")
    ratio = vol / ref_vol
    mask = (ratio > _UNIT_RATIO_LO) & (ratio < _UNIT_RATIO_HI)
    if not bool(mask.any()):
        return df, 0
    out = df.copy()
    dates = out.loc[mask, "date"]
    out.loc[mask, "volume"] = ref_vol[mask].to_numpy()
    if "amount" in out.columns and "amount" in ref.columns:
        out.loc[mask, "amount"] = pd.to_numeric(
            dates.map(ref["amount"]), errors="coerce"
        ).to_numpy()
    out.loc[mask, "turnover_rate"] = pd.NA
    return out, int(mask.sum())


def _coverage(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """按年统计换手率覆盖率。"""
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    all_df["year"] = all_df["date"].astype(str).str[:4]
    g = all_df.groupby("year")["turnover_rate"]
    return pd.DataFrame({
        "rows": g.size(),
        "filled": g.apply(lambda s: int(s.notna().sum())),
        "coverage": g.apply(lambda s: round(float(s.notna().mean()), 3)),
    })


def _validate(codes: list[str], sample: int, split: str) -> None:
    """误差检验：只用 split 之前的观测反推，预测之后的换手率并与官方值比对。

    模拟真实场景（历史段有换手率、新段没有），衡量流通股本随时间变化带来的误差。
    """
    step = max(1, len(codes) // sample)
    picked = codes[::step][:sample]
    rows = []
    for code in picked:
        df = kline_cache.get(code)
        if df is None or df.empty or "turnover_rate" not in df.columns:
            continue
        hist = df[df["date"] < split]
        future = df[df["date"] >= split]
        if future["turnover_rate"].notna().sum() < 20:
            continue
        shares = float_shares_series(hist)
        if shares is None or shares.dropna().empty:
            continue
        fs = float(shares.dropna().iloc[-1])
        actual = pd.to_numeric(future["turnover_rate"], errors="coerce")
        vol = pd.to_numeric(future["volume"], errors="coerce")
        mask = actual.notna() & (actual > 0) & vol.notna() & (vol > 0)
        if not bool(mask.any()):
            continue
        pred = vol[mask] * 10000.0 / fs
        rel = ((pred - actual[mask]).abs() / actual[mask])
        rows.append({
            "code": code,
            "n": int(mask.sum()),
            "median_rel_err": float(rel.median()),
            "p90_rel_err": float(rel.quantile(0.9)),
        })
    if not rows:
        print("没有可用于检验的股票（需要 split 之后有官方换手率）", flush=True)
        return
    r = pd.DataFrame(rows)
    print(f"\n误差检验：{len(r)} 只，用 {split} 之前的观测反推之后的换手率", flush=True)
    print(
        f"  相对误差 中位数 {r['median_rel_err'].median():.2%}，"
        f"P90 {r['p90_rel_err'].median():.2%}，"
        f"中位误差<10% 的股票占比 {(r['median_rel_err'] < 0.10).mean():.1%}",
        flush=True,
    )
    worst = r.sort_values("median_rel_err", ascending=False).head(5)
    print("  误差最大的 5 只（多为解禁/增发导致股本变化）:", flush=True)
    print(worst.to_string(index=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="", help="逗号分隔，默认全量")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--write", action="store_true", help="写入 parquet（默认只统计）")
    parser.add_argument("--validate", type=int, default=0, help="抽样检验只数")
    parser.add_argument("--split", default="2025-11-01", help="检验的切分日期")
    parser.add_argument(
        "--fix-star", action="store_true",
        help="顺带修正科创板「股当手」的 100 倍量纲行（需重取 K 线，约 1~2 分钟）",
    )
    args = parser.parse_args()

    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else _codes()
    )
    if args.limit:
        codes = codes[: args.limit]
    print(f"待处理 {len(codes)} 只（{'写入' if args.write else '只统计'}）", flush=True)

    # 检验必须在补齐之前跑：补齐后原始空值会被自己的预测覆盖，无法再衡量误差
    if args.validate:
        _validate(codes, args.validate, args.split)

    before: list[pd.DataFrame] = []
    after: list[pd.DataFrame] = []
    need_fallback: list[str] = []
    filled_total = 0
    bad_total = 0
    touched = 0

    def _apply(
        code: str,
        fallback_shares: float | None = None,
        record_before: bool = True,
        df: pd.DataFrame | None = None,
    ) -> None:
        """补齐单只并（可选）落盘，累加统计。"""
        nonlocal filled_total, bad_total, touched
        if df is None:
            df = kline_cache.get(code)
        if df is None or df.empty or "turnover_rate" not in df.columns:
            return
        if record_before:
            before.append(df[["date", "turnover_rate"]].copy())
        out, filled, bad = fill_turnover(df, fallback_shares)
        after.append(out[["date", "turnover_rate"]].copy())
        filled_total += filled
        bad_total += bad
        if filled:
            touched += 1
            if args.write:
                _save_atomic(code, out)

    star_fixed = 0
    for code in codes:
        df = kline_cache.get(code)
        if df is None or df.empty or "turnover_rate" not in df.columns:
            continue
        if args.fix_star and code.startswith(("688", "689")):
            try:
                df, fixed = fix_share_unit(df, _refetch(code))
            except Exception as exc:  # noqa: BLE001 - 重取失败则按原样补齐
                print(f"  科创板量纲修正失败 {code}: {type(exc).__name__} {exc}", flush=True)
            else:
                star_fixed += fixed
                if fixed and args.write:
                    _save_atomic(code, df)  # 先落盘修正后的量纲，再走通用补齐
        if float_shares_series(df) is None:
            # 留到拿到腾讯兜底股本后再处理（先不做全量常驻内存）
            before.append(df[["date", "turnover_rate"]].copy())
            need_fallback.append(code)
            continue
        _apply(code, df=df)

    if args.fix_star:
        print(f"科创板量纲修正 {star_fixed} 行（股→手）", flush=True)

    # 无历史观测的股票：用腾讯批量行情当日流通股本兜底
    fallback: dict[str, float] = {}
    if need_fallback:
        print(f"其中 {len(need_fallback)} 只无历史换手率观测，改用腾讯行情取流通股本", flush=True)
        symbols = [code_to_symbol(c) for c in need_fallback]
        for i in range(0, len(symbols), 500):
            try:
                quotes = tencent_quotes(symbols[i : i + 500])
            except Exception as exc:  # noqa: BLE001 - 取不到就留空
                print(f"  腾讯行情批次失败: {exc}", flush=True)
                continue
            for row in quotes.itertuples():
                if row.float_shares and row.float_shares > 0:
                    fallback[row.code] = float(row.float_shares)
        print(f"  取到 {len(fallback)} 只的流通股本", flush=True)
        for code in need_fallback:
            _apply(code, fallback.get(code), record_before=False)

    print(f"\n补齐 {filled_total} 行，涉及 {touched} 只；越界丢弃 {bad_total} 行", flush=True)
    b, a = _coverage(before), _coverage(after)
    if not b.empty:
        cmp = b.join(a, rsuffix="_after")
        cmp = cmp[["rows", "filled", "coverage", "filled_after", "coverage_after"]]
        print("\n按年覆盖率（补齐前 → 补齐后）:", flush=True)
        print(cmp.to_string(), flush=True)


if __name__ == "__main__":
    main()
