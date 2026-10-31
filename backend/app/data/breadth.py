"""全市场涨跌家数（广度）的日序列：本地日线回算 + parquet 缓存。

**为什么要单独做一份**
`api/market.py` 的 `breadth` 是在**实时快照**上算的「当刻」家数，没有历史。而机制层要判的是
「过去几天广度怎么走」——「普跌但跌家数在收敛」与「每天 3000 只跌、越跌越广」是两种完全不同的
环境，只看指数收盘/波动率分不出来。历史广度只能从本地日线回算。

**口径**（与 `market_overview` 的实时口径一致）
`pct > 0` 涨、`pct < 0` 跌、`pct == 0` 平；停牌/无价的行不参与统计。
缓存里的日线是**前复权**序列，所以 `close.pct_change()` 在除权日不会出现假跌——这也是不回退到
「读 `pct_change` 字段」的原因（本地 parquet 本来就没有那一列）。

**成本与调用方式**
全市场 5734 只 × 4 年实测约 10 秒（抽样 400 只 0.7s）。所以：
- 刷新走调度器（每日 16:00 K线预热之后）或手动 `python -m app.data.breadth`；
- **tick 路径只 `load()` 读 parquet（毫秒级）**，绝不现场回算。

用法::

    python -m app.data.breadth              # 回算并刷新缓存
    python -m app.data.breadth --since 2024-01-01
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

logger = logging.getLogger("sas.data.breadth")

#: 缓存文件名（与 `klines` 同级，即 `<data_dir>/breadth_daily.parquet`）
CACHE_NAME = "breadth_daily.parquet"
#: 输出列顺序
COLUMNS = ("date", "up", "down", "flat", "total", "up_ratio")


def _klines_dir(klines_dir: str | os.PathLike | None = None) -> Path:
    if klines_dir is not None:
        return Path(klines_dir)
    from app.config import get_settings

    return Path(get_settings().kline_dir)


def cache_path(klines_dir: str | os.PathLike | None = None) -> Path:
    """缓存落点：`<kline_dir 的父目录>/breadth_daily.parquet`。"""
    return _klines_dir(klines_dir).parent / CACHE_NAME


def build(
    klines_dir: str | os.PathLike | None = None,
    *,
    since: str | None = None,
) -> pd.DataFrame:
    """遍历本地日线回算每日涨跌家数。

    Args:
        klines_dir: 日线 parquet 目录；None 取 ``settings.kline_dir``。
        since: 只保留 >= 该日期的行（"YYYY-MM-DD"）；None 保留全部历史。

    Returns:
        DataFrame，列见 :data:`COLUMNS`，按日期升序、``date`` 为索引。
        无数据时返回空表（列齐但 0 行），调用方不需要额外判空分支。
    """
    kdir = _klines_dir(klines_dir)
    files = [p for p in kdir.glob("*.parquet") if not p.name.startswith("idx_")]
    if not files:
        logger.warning("广度回算：%s 下没有个股日线", kdir)
        return pd.DataFrame(columns=COLUMNS).set_index("date")

    up = pd.Series(dtype="int64")
    down = pd.Series(dtype="int64")
    flat = pd.Series(dtype="int64")
    skipped = 0
    for path in files:
        try:
            df = pd.read_parquet(path, columns=["date", "close"])
        except Exception:  # noqa: BLE001 - 单只坏文件不影响整体广度
            skipped += 1
            continue
        if df is None or len(df) < 2:
            skipped += 1
            continue
        df = df.dropna(subset=["close"])
        if len(df) < 2:
            skipped += 1
            continue
        close = df["close"].astype(float)
        pct = close.pct_change()
        # date 可能是 datetime64 或 str，统一截成 "YYYY-MM-DD"
        day = df["date"].astype(str).str.slice(0, 10)
        frame = pd.DataFrame({"d": day.to_numpy(), "p": pct.to_numpy()}).dropna()
        if frame.empty:
            continue
        up = up.add(frame.loc[frame["p"] > 0].groupby("d").size(), fill_value=0)
        down = down.add(frame.loc[frame["p"] < 0].groupby("d").size(), fill_value=0)
        flat = flat.add(frame.loc[frame["p"] == 0].groupby("d").size(), fill_value=0)

    out = pd.concat(
        [up.rename("up"), down.rename("down"), flat.rename("flat")], axis=1
    ).fillna(0).astype("int64")
    out.index.name = "date"
    out = out.sort_index()
    if since:
        out = out[out.index >= str(since)[:10]]
    out["total"] = out[["up", "down", "flat"]].sum(axis=1)
    #: 上涨家数占比：机制层用的是它（比值不受上市家数逐年变化影响）
    out["up_ratio"] = (out["up"] / out["total"].where(out["total"] > 0)).round(4)
    out = out[list(COLUMNS[1:])]
    if skipped:
        logger.info("广度回算：%d 只跳过（无数据/坏文件）", skipped)
    return out


def refresh(
    klines_dir: str | os.PathLike | None = None,
    *,
    since: str | None = None,
    path: str | os.PathLike | None = None,
) -> pd.DataFrame:
    """回算并覆盖写缓存，返回写出的表（失败不抛，返回空表）。"""
    out = build(klines_dir, since=since)
    if out.empty:
        return out
    target = Path(path) if path is not None else cache_path(klines_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(target)
        logger.info(
            "广度缓存已刷新：%s（%d 个交易日，%s ~ %s）",
            target, len(out), out.index[0], out.index[-1],
        )
    except Exception as exc:  # noqa: BLE001 - 缓存写失败只影响机制层降级，不能拖垮调用方
        logger.warning("广度缓存写入失败（机制层将退化为不使用广度）: %s", exc)
    return out


def load(
    klines_dir: str | os.PathLike | None = None,
    *,
    path: str | os.PathLike | None = None,
) -> pd.DataFrame:
    """读缓存；缺失/损坏返回空表（调用方据此退化为不使用广度）。"""
    target = Path(path) if path is not None else cache_path(klines_dir)
    try:
        if not target.is_file():
            return pd.DataFrame(columns=COLUMNS).set_index("date")
        df = pd.read_parquet(target)
        if "date" in df.columns:
            df = df.set_index("date")
        return df.sort_index()
    except Exception as exc:  # noqa: BLE001
        logger.debug("广度缓存读取失败: %s", exc)
        return pd.DataFrame(columns=COLUMNS).set_index("date")


def recent_up_ratio(df: pd.DataFrame, as_of: str, n: int = 5) -> list[float]:
    """取 ``<= as_of`` 的最近 n 个交易日 ``up_ratio``（升序）。

    少于 n 个时返回已有的全部；空表返回空列表。**只用 as_of 及之前**，不含未来。
    """
    if df is None or df.empty or "up_ratio" not in df.columns:
        return []
    head = df[df.index <= str(as_of)[:10]]
    if head.empty:
        return []
    return [round(float(x), 4) for x in head["up_ratio"].tail(n).tolist()]


def main(argv: list[str] | None = None) -> int:
    """CLI：``python -m app.data.breadth [--since YYYY-MM-DD]``。"""
    import argparse

    ap = argparse.ArgumentParser(description="回算并刷新全市场涨跌家数缓存")
    ap.add_argument("--since", default=None, help="只保留该日期之后（默认全部历史）")
    args = ap.parse_args(argv)

    out = refresh(since=args.since)
    if out.empty:
        print("[error] 没有回算出任何数据，检查 kline_dir")
        return 1
    print(f"[ok] {len(out)} 个交易日  {out.index[0]} ~ {out.index[-1]}")
    tail = out.tail(5)
    for day, row in tail.iterrows():
        print(
            f"  {day}  up={int(row['up']):4d} down={int(row['down']):4d} "
            f"flat={int(row['flat']):3d}  up_ratio={row['up_ratio']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
