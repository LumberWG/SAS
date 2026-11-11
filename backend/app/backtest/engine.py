"""轻量向量化回测引擎（日级，信号驱动）。

交易规则（简化但透明，便于学习理解）:
  - 买入: 策略在某日收盘后发出信号，以当日收盘价成交；
    资金等分给最多 max_positions 只股票；A股整手（100股）交易。
  - 卖出: 触及止损价 / 目标价 / 回测结束，以当日收盘价成交。
  - 费用: 佣金万2.5（commission，双边）+ 印花税千0.5（stamp_tax，仅卖出）。

绩效指标（公式见各函数注释）:
  - 总收益率 / 年化收益率 / 最大回撤 / 夏普比率 / 胜率 / 交易明细。

局限: 日级回测不考虑日内滑点与涨跌停无法成交的情况，
信号用收盘价成交已包含一定理想化假设，实盘需更严格建模。
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.service import get_kline_cached
from app.indicators import add_all_indicators
from app.strategies.base import Strategy

logger = logging.getLogger("sas.backtest")

#: 指标预热所需的历史根数（保证回测起点指标已填满）
_WARMUP_BARS = 90


def _series_uptrend(close: pd.Series, ma: int = 60) -> set[str]:
    """单条收盘序列的上升通道判定：返回收盘 > MA(ma) 的日期集合（日期字符串）。

    官方指数（如 399006）或合成指数的最终判定都走这里，口径统一。
    """
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < ma:
        return set()
    ma_series = close.rolling(ma).mean()
    up = (close > ma_series) & close.notna() & ma_series.notna()
    return set(close.index[up].astype(str))


def _median_chain_uptrend(close_matrix: pd.DataFrame, ma: int = 60) -> set[str]:
    """用多只股票收盘价矩阵构建等权（中位）指数，返回处于上升通道（收盘 > MA）的日期集合。

    用途：创业板「趋势过滤」——只在创业板整体处于上升通道时才允许开仓，
    过滤下跌市的死猫反弹。日期以字符串返回，便于与引擎事件日期直接比对。
    """
    if close_matrix.empty:
        return set()
    rets = close_matrix.pct_change()
    daily = rets.median(axis=1, skipna=True)
    idx = (1.0 + daily.fillna(0.0)).cumprod() * 1000.0
    idx = idx.replace(0, np.nan).ffill()
    return _series_uptrend(idx, ma)


def compute_index_uptrend_dates(symbol: str, ma: int = 60, kline_dir: str | None = None) -> set[str]:
    """用官方指数日线（如 sz399006=创业板指）的收盘序列算上升通道日期集合。

    优先走 ``get_index_cached``（含网络刷新，保证最新），与盘后/回测共用同一份数据。
    """
    try:
        from app.data.service import get_index_cached
        df = get_index_cached(symbol)
    except Exception:
        df = None
    if df is None or df.empty or "close" not in df.columns or "date" not in df.columns:
        return set()
    close = (
        df.dropna(subset=["date"])
        .assign(date=lambda d: d["date"].astype(str).str[:10])
        .set_index("date")["close"]
    )
    return _series_uptrend(close, ma)


def compute_cyb_uptrend_dates(kline_dir: str, ma: int = 60,
                              codes: list[str] | None = None,
                              index_symbol: str | None = None) -> set[str]:
    """创业板上升通道日期集合（用于「趋势过滤」）。

    - ``index_symbol`` 给定（如 ``"sz399006"``）：直接用该官方指数日线收盘算，
      更贴近市场共识的「创业板指」，且盘后/回测同一口径；
    - ``index_symbol`` 为空：退回旧的「300/301 等权合成指数」口径（回测基线兼容）。
    """
    if index_symbol:
        return compute_index_uptrend_dates(index_symbol, ma, kline_dir)
    import glob
    import os

    frames: dict[str, pd.Series] = {}
    for f in glob.glob(os.path.join(kline_dir, "*.parquet")):
        c = os.path.splitext(os.path.basename(f))[0]
        if codes is not None and c not in set(codes):
            continue
        if c[:3] not in ("300", "301"):
            continue
        try:
            s = (
                # 列裁剪：合成指数只需 date/close，避免整表读进内存
                # （要遍历上千个 parquet，是全量刷新的主要 IO 开销之一）。
                pd.read_parquet(f, columns=["date", "close"])
                .sort_values("date")
                .set_index("date")["close"]
                .astype(float)
            )
        except Exception:
            continue
        frames[c] = s
    if not frames:
        return set()
    mat = pd.concat(frames, axis=1)
    # 关键：本地 parquet 的 date 为字符串，concat 后索引不保证时序；
    # 必须先转 datetime 并排序，否则后续 rolling(MA) 会按错误顺序计算。
    mat.index = pd.to_datetime(mat.index, errors="coerce")
    mat = mat.sort_index()
    rets = mat.pct_change()
    daily = rets.median(axis=1, skipna=True)
    ser = (1.0 + daily.fillna(0.0)).cumprod() * 1000.0
    ser = ser.replace(0, np.nan).ffill().dropna()
    return _series_uptrend(ser, ma)


#: 盘中实时趋势判定的短缓存（避免每次面板刷新都打网络）
_uptrend_live_cache: dict[tuple, tuple[float, dict]] = {}


def compute_uptrend_live(symbol: str | None, ma: int = 60,
                         kline_dir: str | None = None,
                         cache_ttl: float = 30.0) -> dict:
    """盘中实时判断：当前指数（或创业板等权合成指数）是否处于上升通道（> MA）。

    - ``symbol`` 给定（如 ``"sz399006"``）：用官方指数实时快照替换「今日」值重算；
    - ``symbol`` 为空/None：用创业板等权合成指数——盘中取全市场实时快照，
      按成分股实时价/昨收重建「今日」合成指数值，再与 MA 比对，翻多即解锁候选。
    - 非交易时段：回落日线收盘判定。

    返回 ``{as_of, uptrend(bool|None), live_price, ma_value, source, index_name}``；
    ``source`` 为 ``intraday``（盘中实时）或 ``daily``（日线收盘）。
    """
    from datetime import datetime

    now = datetime.now()
    cache_key = (("OFF:" + (symbol or "")), ma) if symbol else (("SYN", ma, kline_dir))
    cached = _uptrend_live_cache.get(cache_key)
    if cached is not None and (now.timestamp() - cached[0]) < cache_ttl:
        return cached[1]
    if symbol:
        result = _compute_uptrend_live(symbol, ma, now)
    else:
        result = _compute_synthetic_uptrend_live(ma, kline_dir, now)
    _uptrend_live_cache[cache_key] = (now.timestamp(), result)
    return result


def _compute_uptrend_live(symbol: str, ma: int, now) -> dict:
    try:
        from app.data.service import (  # noqa: F401
            get_index_cached, get_data_source, _in_trading_hours,
        )
        df = get_index_cached(symbol)
        in_trading = _in_trading_hours(now)
        ds = get_data_source()
    except Exception:
        return {"as_of": None, "uptrend": None, "live_price": None,
                "ma_value": None, "source": "error"}
    if df is None or df.empty or "close" not in df.columns or "date" not in df.columns:
        return {"as_of": None, "uptrend": None, "live_price": None,
                "ma_value": None, "source": "error"}

    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    s = pd.Series(
        pd.to_numeric(df["close"], errors="coerce").values,
        index=df["date"].values,
    ).astype(float)
    s = s[~s.index.duplicated(keep="last")]
    if len(s) < ma:
        return {"as_of": s.index[-1] if len(s) else None, "uptrend": None,
                "live_price": float(s.iloc[-1]) if len(s) else None,
                "ma_value": None, "source": "insufficient"}
    ma_series = s.rolling(ma).mean()
    last_date = s.index[-1]
    last_close = float(s.iloc[-1])
    last_ma = float(ma_series.iloc[-1]) if pd.notna(ma_series.iloc[-1]) else None

    if in_trading:
        try:
            snap = ds.get_index_snapshot()
            row = snap[snap["em_code"] == symbol[-6:]]
            if not row.empty:
                live = float(row["price"].iloc[0])
                today_str = now.strftime("%Y-%m-%d")
                s2 = s.drop(today_str, errors="ignore")
                s2.loc[today_str] = live
                ma_now = float(s2.rolling(ma).mean().iloc[-1])
                return {"as_of": today_str, "uptrend": bool(live > ma_now),
                        "live_price": live, "ma_value": ma_now,
                        "source": "intraday", "index_name": _index_name(symbol)}
        except Exception:
            pass
    return {"as_of": last_date,
            "uptrend": (None if last_ma is None else bool(last_close > last_ma)),
            "live_price": last_close, "ma_value": last_ma, "source": "daily",
            "index_name": _index_name(symbol)}


def _index_name(symbol: str) -> str:
    """官方指数代码 → 面板展示名。"""
    if symbol == "sz399006":
        return "创业板指399006"
    return symbol or "指数"


#: 创业板等权合成指数序列（日频）的进程内缓存，避免每次面板刷新都重读 ~188 个 parquet
_synthetic_index_cache: dict[str, tuple[float, pd.Series, set[str]]] = {}


def _load_synthetic_index_series(kline_dir: str, ttl: float = 300.0):
    """构建并缓存创业板等权合成指数收盘序列（日频）。

    返回 ``(series, set_of_constituent_codes)``。合成口径与 ``compute_cyb_uptrend_dates``
    的 300/301 等权中位法一致，仅此处保留序列用于盘中 MA 重算。命中缓存（默认 5 分钟）
    时不重读 parquet，控制 IO 开销。
    """
    cached = _synthetic_index_cache.get(kline_dir)
    if cached is not None and (datetime.now().timestamp() - cached[0]) < ttl:
        return cached[1], cached[2]
    import glob
    import os

    frames: dict[str, pd.Series] = {}
    for f in glob.glob(os.path.join(kline_dir, "*.parquet")):
        c = os.path.splitext(os.path.basename(f))[0]
        if c[:3] not in ("300", "301"):
            continue
        try:
            s = (
                # 列裁剪：合成指数只需 date/close，避免整表读进内存
                # （要遍历上千个 parquet，是全量刷新的主要 IO 开销之一）。
                pd.read_parquet(f, columns=["date", "close"])
                .sort_values("date")
                .set_index("date")["close"]
                .astype(float)
            )
        except Exception:
            continue
        frames[c] = s
    if not frames:
        _synthetic_index_cache[kline_dir] = (datetime.now().timestamp(), pd.Series(dtype=float), set())
        return pd.Series(dtype=float), set()
    mat = pd.concat(frames, axis=1)
    # 关键：本地 parquet 的 date 为字符串，concat 后索引不保证时序；
    # 必须先转 datetime 并排序，否则后续 rolling(MA) 会按错误顺序计算。
    mat.index = pd.to_datetime(mat.index, errors="coerce")
    mat = mat.sort_index()
    rets = mat.pct_change()
    daily = rets.median(axis=1, skipna=True)
    ser = (1.0 + daily.fillna(0.0)).cumprod() * 1000.0
    ser = ser.replace(0, np.nan).ffill().dropna()
    codes = set(frames.keys())
    _synthetic_index_cache[kline_dir] = (datetime.now().timestamp(), ser, codes)
    return ser, codes


def _compute_synthetic_uptrend_live(ma: int, kline_dir: str | None, now) -> dict:
    """用全市场实时快照重建创业板等权合成指数的「今日」值，再判上升通道。

    历史合成指数序列按 300/301 个股日线收盘等权中位法构建（与回测/盘后同口径，仅缓存以省 IO）；
    盘中对成分股取实时价/昨收，算中位今日收益率，
    today_value = 昨日合成指数值 × (1 + 中位今日收益率)，再与该序列 MA 比对，翻多即解锁候选。
    """
    from app.config import get_settings
    from app.data.service import _in_trading_hours

    kline_dir = kline_dir or str(get_settings().kline_dir)
    try:
        idx, codes = _load_synthetic_index_series(kline_dir)
    except Exception:
        return {"as_of": None, "uptrend": None, "live_price": None,
                "ma_value": None, "source": "error", "index_name": "创业板等权指数"}
    if idx is None or idx.empty or len(idx) < ma:
        last = idx.index[-1] if (idx is not None and len(idx)) else None
        return {"as_of": last, "uptrend": None, "live_price": None,
                "ma_value": None, "source": "insufficient", "index_name": "创业板等权指数"}
    last_date = idx.index[-1]
    last_val = float(idx.iloc[-1])
    ma_series = idx.rolling(ma).mean()
    last_ma = float(ma_series.iloc[-1]) if pd.notna(ma_series.iloc[-1]) else None
    in_trading = _in_trading_hours(now)
    # 非交易时段 / 实时取数失败时，用最近日线收盘 vs MA
    if not in_trading:
        return {"as_of": last_date, "uptrend": (None if last_ma is None else bool(last_val > last_ma)),
                "live_price": last_val, "ma_value": last_ma,
                "source": "daily", "index_name": "创业板等权指数"}
    try:
        # 用进程内内存快照（tick 引擎每 30s 刷新、落盘持久化），不自行拉全市场网络快照
        # （akshare 全市场快照分页耗时 3 分钟且频繁超时，绝不能放进面板请求路径）。
        from app.data.snapshot_store import snapshot_store
        snap = snapshot_store.peek_snapshot()
        if snap is None or snap.empty or "code" not in snap.columns:
            raise RuntimeError("内存快照不可用")
        snap = snap.copy()
        # 优先用昨收；缺失时用「现价 / (1+涨跌幅)」反推
        if "prev_close" not in snap.columns:
            snap["prev_close"] = snap["price"] / (1.0 + snap["pct_change"].fillna(0.0) / 100.0)
        else:
            miss = snap["prev_close"].isna()
            if miss.any():
                snap.loc[miss, "prev_close"] = snap.loc[miss, "price"] / (
                    1.0 + snap.loc[miss, "pct_change"].fillna(0.0) / 100.0)
        snap = snap[snap["code"].isin(codes)]
        snap = snap[snap["prev_close"].notna() & snap["price"].notna() & (snap["prev_close"] > 0)]
        if snap.empty:
            raise RuntimeError("合成指数成分实时快照缺失")
        today_ret = snap["price"] / snap["prev_close"] - 1.0
        med = float(today_ret.median())
        today_ts = pd.Timestamp(now.date())
        # 基准取「昨日」合成指数值：若 idx 末日已是今天（日线已写入），则取 iloc[-2]，
        # 否则（今日尚未写入）取 iloc[-1]；再乘 (1+今日实时中位收益) 得今日实时值，
        # 避免对今日收益双重计入。
        if len(idx) >= 2 and idx.index[-1] == today_ts:
            base = float(idx.iloc[-2])
        else:
            base = float(idx.iloc[-1])
        today_value = base * (1.0 + med)
        idx2 = idx.drop(today_ts, errors="ignore").copy()
        idx2.loc[today_ts] = today_value
        ma_now = float(idx2.rolling(ma).mean().iloc[-1])
        return {"as_of": today_ts.strftime("%Y-%m-%d"), "uptrend": bool(idx2.iloc[-1] > ma_now),
                "live_price": round(float(idx2.iloc[-1]), 2),
                "ma_value": round(ma_now, 2),
                "source": "intraday", "index_name": "创业板等权指数"}
    except Exception:
        return {"as_of": last_date, "uptrend": (None if last_ma is None else bool(last_val > last_ma)),
                "live_price": last_val, "ma_value": last_ma,
                "source": "daily", "index_name": "创业板等权指数"}


@dataclass
class BacktestParams:
    """回测参数。"""

    strategy: str                       # 策略名
    start_date: str                     # YYYY-MM-DD
    end_date: str                       # YYYY-MM-DD
    initial_capital: float = 1_000_000  # 初始资金（元）
    max_positions: int = 5              # 最大持仓股票数
    commission: float = 0.00025         # 佣金率（双边）
    stamp_tax: float = 0.0005           # 印花税率（仅卖出）
    codes: list[str] | None = None      # 限定股票池；缺省用成交额前 N
    universe_size: int = 300            # 缺省股票池大小（按成交额）
    max_hold_days: int = 0              # 最大持有天数（0=不限，仅到期/止损/止盈退出）
    trend_filter: bool = False          # 创业板趋势过滤：仅指数处于上升通道时开仓
    trend_ma: int = 60                  # 趋势过滤所用均线周期（日）
    trend_index: str | None = None      # 趋势过滤所用指数（如 sz399006=官方创业板指）；None=300/301等权合成
    time_stop_days: int = 0             # 主动离场-时间止损：持仓达此天数仍未启动跟踪止损(即未盈利)则离场(0=关闭)
    trailing_pct: float = 0.0           # 主动离场-跟踪止损：涨幅达此比例启动跟踪，自峰值回撤此比例即离场(0=关闭)


@dataclass
class Position:
    """持仓记录。"""

    code: str
    name: str
    shares: int
    cost: float          # 买入均价（含佣金）
    stop_loss: float
    target_price: float
    buy_date: str
    peak: float = 0.0     # 持仓期间最高价（跟踪止损用）


@dataclass
class Trade:
    """已完成交易（平仓）记录。"""

    code: str
    name: str
    buy_date: str
    sell_date: str
    buy_price: float
    sell_price: float
    shares: int
    pnl: float
    pnl_pct: float
    reason: str          # 平仓原因：止损/止盈/到期


class BacktestEngine:
    """回测引擎。"""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run(self, params: BacktestParams) -> dict:
        """执行回测，返回结果 dict（含净值曲线与绩效指标）。"""
        from app.data.akshare_source import get_data_source
        from app.strategies import registry

        strategy: Strategy = registry.create(params.strategy)
        codes = params.codes or await self._default_universe(params.universe_size)

        # 预热期起点（指标需要额外历史）
        warmup_start = self._shift_date(params.start_date, -_WARMUP_BARS)

        semaphore = asyncio.Semaphore(self.settings.scan_concurrency)
        events: list[dict] = []          # 全部买入信号事件
        klines: dict[str, pd.DataFrame] = {}  # code -> 区间K线（止盈止损判断用）

        async def fetch_one(code: str, name: str) -> None:
            async with semaphore:
                try:
                    df = await asyncio.to_thread(get_kline_cached, code)
                    if df is None or df.empty:
                        return
                    df = df[df["date"] >= warmup_start]
                    df = df[df["date"] <= params.end_date]
                    # 只要求覆盖策略自身 min_bars：预热历史已由 warmup_start 提供。
                    # 原先再加 _WARMUP_BARS//2 的余量会随预热长度线性放大，
                    # 导致短窗口（如三个月）整池被跳过、回测结果为空。
                    if len(df) < strategy.min_bars:
                        return
                    # 重置索引，保证后续 iloc 位置与标签一致
                    df = df.reset_index(drop=True)
                    df = await asyncio.to_thread(add_all_indicators, df)
                    klines[code] = df
                    # 逐日评估：仅对回测区间内的日期发信号
                    start_idx = self._first_index_on_or_after(df, params.start_date)
                    found = await asyncio.to_thread(
                        self._collect_events, strategy, df, start_idx, code, name
                    )
                    events.extend(found)
                except Exception as exc:  # noqa: BLE001 - 单股失败跳过
                    logger.debug("回测取数失败 %s: %s", code, exc)

        pool = await asyncio.to_thread(self._resolve_pool, codes)
        await asyncio.gather(*(fetch_one(r.code, r.name) for r in pool.itertuples()))
        logger.info("回测信号生成完毕: %d 只股票, %d 个信号事件", len(klines), len(events))

        # ---- 板块感知趋势过滤（可选）：每个板块各算一份上升通道集合（Layer1）----
        # 创业板(cyb)沿用等权合成/官方指数口径；其余板块用各自官方指数 parquet。
        board_uptrend: dict[str, set[str] | None] | None = None
        if params.trend_filter:
            from app.analysis.board_index import classify_board, board_uptrend_set
            board_uptrend = {}
            boards_in_univ = {classify_board(ev["code"]) for ev in events}
            for b in boards_in_univ:
                if b == "cyb" and (params.trend_index or ""):
                    board_uptrend[b] = compute_cyb_uptrend_dates(
                        str(self.settings.kline_dir), params.trend_ma,
                        index_symbol=params.trend_index,
                    )
                elif b == "cyb":
                    board_uptrend[b] = compute_cyb_uptrend_dates(
                        str(self.settings.kline_dir), params.trend_ma,
                        index_symbol=None,
                    )
                else:
                    board_uptrend[b] = board_uptrend_set(
                        str(self.settings.kline_dir), b, params.trend_ma,
                    )
            total = sum(len(v) for v in board_uptrend.values() if v)
            logger.info("板块感知趋势过滤启用: 板块数 %d, 上升通道交易日合计 %d (MA=%d)",
                        len(board_uptrend), total, params.trend_ma)

        # ---- 模拟交易（放线程：逐日撮合是 CPU 密集，不能堵住事件循环） ----
        equity_curve, trades = await asyncio.to_thread(
            self._simulate, params, events, klines, board_uptrend
        )

        # ---- 绩效指标 ----
        metrics = await asyncio.to_thread(
            self._compute_metrics, equity_curve, trades, params
        )

        return {
            "strategy": params.strategy,
            "start_date": params.start_date,
            "end_date": params.end_date,
            "params": {
                "initial_capital": params.initial_capital,
                "max_positions": params.max_positions,
                "commission": params.commission,
                "stamp_tax": params.stamp_tax,
                "universe_size": len(pool),
            },
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": trades,
        }

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    async def _default_universe(self, size: int) -> list[str]:
        """缺省股票池：实时快照按成交额取前 N（流动性优先）。"""
        from app.data.snapshot_store import snapshot_store

        snapshot = await asyncio.to_thread(snapshot_store.get_snapshot)
        top = snapshot.nlargest(size, "amount")
        return top["code"].tolist()

    def _resolve_pool(self, codes: list[str]) -> pd.DataFrame:
        """codes → 含名称的 DataFrame。"""
        from app.data.service import get_stock_pool

        pool = get_stock_pool()
        return pool[pool["code"].isin(set(codes))]

    @staticmethod
    def _shift_date(date: str, days: int) -> str:
        """自然日平移（简化：不处理节假日，仅用于截取数据起点）。"""
        return (datetime.strptime(date, "%Y-%m-%d")
                + pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    @staticmethod
    def _first_index_on_or_after(df: pd.DataFrame, date: str) -> int:
        """第一个日期 >= date 的行位置（iloc 位置，无则返回 len(df)）。"""
        mask = (df["date"] >= date).to_numpy()
        if not mask.any():
            return len(df)
        return int(mask.argmax())  # argmax 返回位置而非索引标签

    @staticmethod
    def _collect_events(
        strategy: Strategy,
        df: pd.DataFrame,
        start_idx: int,
        code: str,
        name: str,
    ) -> list[dict]:
        """逐日滚动评估单只股票（CPU 密集，由上层丢进线程池执行）。

        只喂 ``df.iloc[: i + 1]`` 前缀切片，保证指标取值与当日实时值一致。
        """
        out: list[dict] = []
        for i in range(start_idx, len(df)):
            part = df.iloc[: i + 1]
            part.attrs["code"] = code  # #181：供策略按板块缩放止盈目标
            signal = strategy.evaluate(part)
            if signal is None:
                continue
            out.append({
                "code": code, "name": name,
                "date": signal.date,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "target_price": signal.target_price,
            })
        return out

    # ------------------------------------------------------------------
    # 交易模拟
    # ------------------------------------------------------------------
    def _simulate(
        self,
        params: BacktestParams,
        events: list[dict],
        klines: dict[str, pd.DataFrame],
        board_uptrend: dict[str, set[str] | None] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """按日期推进模拟交易，返回 (净值曲线, 交易明细)。

        每日流程:
          1. 用当日收盘价检查持仓是否触发止损/止盈/到期，触发则卖出；
          2. 处理当日到期的买入信号（资金等分、整手交易）；
          3. 记录当日总资产（现金 + 持仓市值）。
        """
        all_dates = sorted({d for df in klines.values() for d in df["date"]})
        events_by_date: dict[str, list[dict]] = {}
        for ev in events:
            events_by_date.setdefault(ev["date"], []).append(ev)

        # 收盘价速查: close[(code, date)]
        close: dict[tuple[str, str], float] = {}
        for code, df in klines.items():
            for r in df.itertuples():
                close[(code, r.date)] = float(r.close)

        cash = params.initial_capital
        positions: dict[str, Position] = {}
        trades: list[Trade] = []
        equity_curve: list[dict] = []
        last_date = all_dates[-1] if all_dates else params.end_date

        for date in all_dates:
            # ---- 1) 卖出检查 ----
            for code in list(positions):
                pos = positions[code]
                price = close.get((code, date))
                if price is None:
                    continue  # 当日停牌无价格，顺延
                reason = None
                # 更新持仓峰值（跟踪止损基线）
                if price > pos.peak:
                    pos.peak = price
                # ---- 主动离场：跟踪止损（涨后动态上移止损，回撤即走）----
                eff_stop = pos.stop_loss
                stop_label = "止损"
                trailing_active = False
                if params.trailing_pct > 0:
                    activate = pos.cost * (1.0 + params.trailing_pct)
                    if pos.peak >= activate:
                        trailing_active = True
                        tstop = max(pos.cost, pos.peak * (1.0 - params.trailing_pct))
                        if tstop > eff_stop:
                            eff_stop = tstop
                            stop_label = "跟踪止损"
                if price <= eff_stop:
                    reason = stop_label
                elif price >= pos.target_price:
                    reason = "止盈"
                elif (params.time_stop_days > 0
                      and not trailing_active
                      and self._shift_date(pos.buy_date, params.time_stop_days) <= date):
                    reason = "时间止损"   # 持有N日仍未盈利(未启动跟踪) -> 主动砍掉死钱
                elif params.max_hold_days and self._shift_date(
                        pos.buy_date, params.max_hold_days) <= date:
                    reason = "持有到期"
                elif date == last_date:
                    reason = "到期"
                if reason is not None:
                    proceeds = pos.shares * price * (1 - params.stamp_tax
                                                     - params.commission)
                    cash += proceeds
                    pnl = proceeds - pos.shares * pos.cost
                    trades.append(Trade(
                        code=code, name=pos.name,
                        buy_date=pos.buy_date, sell_date=date,
                        buy_price=pos.cost, sell_price=price,
                        shares=pos.shares, pnl=round(pnl, 2),
                        pnl_pct=round(pnl / (pos.shares * pos.cost) * 100, 2),
                        reason=reason,
                    ))
                    del positions[code]

            # ---- 2) 买入检查 ----
            for ev in events_by_date.get(date, []):
                if params.trend_filter and board_uptrend is not None:
                    from app.analysis.board_index import classify_board
                    b = classify_board(ev["code"])
                    ups = board_uptrend.get(b)
                    if ups is not None and ev["date"] not in ups:
                        continue  # 该板块处于下降通道：不开新仓
                    # ups is None = 该板块无指数数据，不阻断
                if len(positions) >= params.max_positions:
                    break
                if ev["code"] in positions:
                    continue
                price = close.get((ev["code"], date), ev["price"])
                slots = params.max_positions - len(positions)
                alloc = cash / slots                     # 等分剩余资金
                shares = math.floor(alloc / price / 100) * 100  # 整手
                if shares <= 0:
                    continue
                cost = shares * price * (1 + params.commission)
                if cost > cash:
                    continue
                cash -= cost
                positions[ev["code"]] = Position(
                    code=ev["code"], name=ev.get("name", ""),
                    shares=shares,
                    cost=round(price * (1 + params.commission), 4),
                    stop_loss=ev.get("stop_loss") or price * 0.9,
                    target_price=ev.get("target_price") or price * 1.15,
                    buy_date=date,
                    peak=price,
                )

            # ---- 3) 记录净值 ----
            market_value = sum(
                p.shares * close.get((code, date), p.cost)
                for code, p in positions.items()
            )
            equity_curve.append({
                "date": date,
                "equity": round(cash + market_value, 2),
            })

        return equity_curve, [t.__dict__ for t in trades]

    # ------------------------------------------------------------------
    # 绩效指标
    # ------------------------------------------------------------------
    def _compute_metrics(
        self,
        equity_curve: list[dict],
        trades: list[dict],
        params: BacktestParams,
    ) -> dict:
        """计算绩效指标。

        公式:
          总收益率   = 期末净值 / 初始资金 - 1
          年化收益率 = (1 + 总收益率) ** (252 / 交易日数) - 1
          最大回撤   = min(净值_t / 历史最高净值 - 1)
          夏普比率   = 日收益率均值 / 日收益率标准差 × sqrt(252)
                      （无风险利率取 0，年化按 252 个交易日）
          胜率       = 盈利交易数 / 总交易数
        """
        if not equity_curve:
            return {}
        equity = np.array([p["equity"] for p in equity_curve], dtype=float)
        initial = params.initial_capital
        n_days = max(len(equity) - 1, 1)

        total_return = equity[-1] / initial - 1
        annualized = (1 + total_return) ** (252 / n_days) - 1

        # 最大回撤
        cummax = np.maximum.accumulate(equity)
        drawdowns = equity / cummax - 1
        max_drawdown = float(drawdowns.min())

        # 夏普（日收益序列）
        daily_returns = np.diff(equity) / equity[:-1]
        if daily_returns.size > 1 and daily_returns.std() > 0:
            sharpe = float(daily_returns.mean() / daily_returns.std()
                           * math.sqrt(252))
        else:
            sharpe = 0.0

        # 胜率
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / len(trades) * 100 if trades else 0.0

        # 全部转成原生 float：np.float64 无法被 json.dumps / 前端 JSON 序列化
        return {
            "total_return": round(float(total_return) * 100, 2),       # %
            "annualized_return": round(float(annualized) * 100, 2),    # %
            "max_drawdown": round(max_drawdown * 100, 2),              # %
            "sharpe": round(float(sharpe), 2),
            "trade_count": len(trades),
            "win_rate": round(float(win_rate), 1),                     # %
            "final_equity": round(float(equity[-1]), 2),
            "trading_days": n_days,
        }


# 全局单例
engine = BacktestEngine()
