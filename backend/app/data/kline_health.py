"""日K缓存健康度：扫描本地 Parquet 的最后一根 bar，找出落后的股票。

为什么需要
----------
``kline_warmup_top_n`` 只覆盖流动性前 N 只，其余股票一旦某天没刷上
（数据源限流、后端当天没开机、单股请求超时），就会一直停在旧日期，
而增量预热又是按"每只自己的最后日期"续拉的——不主动扫一遍根本发现不了。

这里提供两个纯本地函数（只读 parquet 的 ``date`` 列，不联网）：

``scan_last_dates``    → ``{code: 最后交易日}``，``""`` 表示文件损坏
``find_laggards``      → 相对目标交易日落后的代码，并剔除停牌/退市

调用方：``scripts/backfill_klines.py``（手动补刷）、
``monitor/scheduler.py``（开机只补落后的，避免每次重启全市场重跑）。
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# 停牌/退市判定：最后 bar 早于「目标日 - N 天」就认为不会再有了。
# 30 天足够覆盖长假（春节/国庆连休最多 10 天上下）而不误伤正常股票。
DEFAULT_DEAD_DAYS = 30


def _one_last_date(path: str) -> tuple[str, str]:
    code = os.path.basename(path)[:-8] if path.endswith(".parquet") else os.path.basename(path)
    try:
        df = pd.read_parquet(path, columns=["date"])
        if df.empty:
            return code, ""
        return code, str(pd.to_datetime(df["date"].iloc[-1]).date())
    except Exception:  # noqa: BLE001 - 损坏文件当"需要重建"处理
        logger.debug("K线缓存读取失败 %s", path, exc_info=True)
        return code, ""


def scan_last_dates(kline_dir: Path | str, workers: int = 16) -> dict[str, str]:
    """并发扫描目录下所有 ``*.parquet``，返回 ``{code: 最后交易日}``。

    跳过 ``idx_*``：指数与个股共用这个目录（``api/market.py`` 的 ``idx_``
    前缀），但指数要走 ``get_index_cached`` 而不是按个股代码回源——把它当
    股票代码传下去会被 ``code_to_symbol`` 拼成 ``szidx_sz399006`` 这种无效
    symbol，拉到脏数据还覆盖原文件。
    """
    d = Path(kline_dir)
    if not d.is_dir():
        return {}
    files = [str(d / f) for f in os.listdir(d)
             if f.endswith(".parquet") and not f.startswith("idx_")]
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, last in ex.map(_one_last_date, files):
            out[code] = last
    return out


def latest_trade_date(last_dates: dict[str, str]) -> str:
    """全市场最普遍的最后交易日 = 大家基本上都已经走到的日期。

    用众数而不是最大值：个别股票拿到脏的未来日期（数据源错发）不会把
    目标日整体带偏。
    """
    from collections import Counter

    c = Counter(v for v in last_dates.values() if v)
    return c.most_common(1)[0][0] if c else ""


def find_laggards(
    last_dates: dict[str, str],
    target: str = "",
    dead_days: int = DEFAULT_DEAD_DAYS,
    include_dead: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
    """挑出落后于 ``target`` 的股票。

    Returns:
        ``(todo, dead, target)``
        - ``todo``: 需要补的 ``[(code, 最后日期)]``，含损坏文件（日期为 ``""``）
        - ``dead``: 判为停牌/退市的 ``[(code, 最后日期)]``
        - ``target``: 实际使用的目标交易日（未传入时自动取众数）
    """
    target = target or latest_trade_date(last_dates)
    if not target:
        return [], [], ""
    dead_line = (datetime.strptime(target, "%Y-%m-%d").date()
                 - timedelta(days=dead_days)).isoformat()

    todo: list[tuple[str, str]] = []
    dead: list[tuple[str, str]] = []
    for code, d in last_dates.items():
        # 空串 = 文件损坏/空表，必须重拉；d >= target 则已最新
        if d and d >= target:
            continue
        (dead if d and d < dead_line else todo).append((code, d))
    if include_dead:
        todo.extend(dead)
        dead = []
    return todo, dead, target
