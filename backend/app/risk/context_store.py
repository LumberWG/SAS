"""P1 上下文风险闸的数据层：按 ``as_of`` 提供四项判定所需的全部输入。

设计红线（用户 2026-11-12：回测要和实盘严格对齐，不能有丝毫不一样）
------------------------------------------------------------------
历史事故是「实盘读实时接口、回测读历史文件」造成口径分裂。这里因此定死三条：

1. **只有一个类**：回测 ``_simulate`` 与实盘 ``execute_recommendations`` 都构造
   ``ContextStore``，只是回测传循环日期、实盘传今天。两侧不各自实现取数。
2. **所有查询都截断到 as_of**：K 线只用 ``date <= as_of``，事件/新闻 SQL 只用
   ``*_date <= as_of``。绝不读取 as_of 之后才知道的任何信息（零未来函数）。
3. **数据只来自本地快照**：事件日历 / 舆情新闻由 ``scripts/fetch_context_snapshots.py``
   预采集落库；策略代码不调网络。快照为空时两侧**一致地**退化为「该闸门不触发」，
   不会出现「实盘有新数据、回测没有」的半边生效。

可注入 ``klines`` / ``pos_index``：回测把已加载的 prepared 传进来避免重复读 5979 个
parquet；实盘不传，按 code 懒加载当日之前的 K 线。两种路径语义完全相同。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings

#: 采集器写入的全部事件类型
EVENT_KINDS: frozenset[str] = frozenset({
    "earnings",        # 业绩预告 / 财报披露
    "unlock",          # 限售解禁
    "reduce",          # 股东减持 / 质押冻结
    "dividend",        # 除权除息（前复权已处理，不阻断）
    "st_change",       # ST / 退市风险警示变更
    "other",           # 调研、互动问答等普通公告（不阻断）
})

#: **只有这些类型会触发冷却闸**。全市场每日公告里约 80% 是 other / dividend，
#: 若一并拦截会让策略几乎买不到票；这也是两侧共用的规则，不是某一侧的门控。
BLOCKING_EVENT_KINDS: frozenset[str] = frozenset({
    "earnings", "unlock", "reduce", "st_change",
})


def board_of(code: str) -> str:
    """行业映射缺失时的兜底分组：按代码前缀归到宽板块。

    与 ``app.board_rules`` 的板块前缀口径一致，但这里只做「分组键」，不做交易范围判定，
    避免把「只做创业板」的白名单混进集中度统计。
    """
    c = str(code or "")
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("8", "4", "9")):
        return "北交所"
    if c.startswith("6"):
        return "沪主板"
    return "深主板"


class ContextStore:
    """按 as-of 日期读取本地上下文快照（线程安全，连接线程内复用）。

    Parameters
    ----------
    klines / pos_index:
        回测注入 ``prepared`` 里已加载的日线与日期索引；不注入时按 code 读 parquet。
    db_path / kline_dir / industry_map_path:
        默认全部取自 ``Settings``，测试可显式注入临时路径。
    """

    def __init__(
        self,
        *,
        klines: Mapping[str, pd.DataFrame] | None = None,
        pos_index: Mapping[str, Mapping[str, int]] | None = None,
        db_path: Path | str | None = None,
        kline_dir: Path | str | None = None,
        industry_map_path: Path | str | None = None,
    ) -> None:
        s = get_settings()
        self.db_path = Path(db_path) if db_path is not None else Path(s.db_path)
        self.kline_dir = Path(kline_dir) if kline_dir is not None else Path(s.kline_dir)
        self.industry_map_path = (
            Path(industry_map_path) if industry_map_path is not None
            else self.kline_dir.parent / "industry_map.csv"
        )
        self._klines = klines
        self._pos_index = pos_index
        self._series_cache: dict[tuple[str, str, int], tuple[float, ...]] = {}
        self._heat_cache: dict[str, dict[str, Any]] = {}
        self._industry_cache: dict[str, str] = {}
        self._industry_loaded = False
        self._local = threading.local()
        self._db_missing = False

    # ------------------------------------------------------------------ DB
    def _conn(self) -> sqlite3.Connection | None:
        """只读连接；表不存在（老库 / 尚未采集）时返回 None，绝不建表或写库。"""
        if self._db_missing:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(
                    f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=2.0
                )
                conn.row_factory = sqlite3.Row
                self._local.conn = conn
            except (sqlite3.Error, OSError):
                self._db_missing = True
                return None
        return conn

    def _fetchall(self, sql: str, args: Sequence[Any]) -> list[sqlite3.Row]:
        conn = self._conn()
        if conn is None:
            return []
        try:
            return list(conn.execute(sql, tuple(args)).fetchall())
        except sqlite3.Error:
            # 表尚未创建 / schema 旧版本：一致地当作「无数据」
            return []

    # -------------------------------------------------------------- 行业
    def _load_industry_map(self) -> None:
        self._industry_loaded = True
        if not self.industry_map_path.exists():
            return
        try:
            df = pd.read_csv(self.industry_map_path, dtype=str)
            for row in df.itertuples(index=False):
                code = str(getattr(row, "code", "") or "").zfill(6)
                ind = str(getattr(row, "industry", "") or "").strip()
                if code and ind:
                    self._industry_cache[code] = ind
        except (OSError, ValueError, pd.errors.ParserError):
            self._industry_cache.clear()

    def industry_of(self, code: str) -> str:
        """个股分组键：优先东财行业（静态快照），缺失回退宽板块。

        注意：``industry_map.csv`` 是**当前快照**、无历史时间戳，两侧共用同一份，
        因此不构成漂移；但它带行业分类前视偏差，只用于「分组」不用于打分。
        """
        if not self._industry_loaded:
            self._load_industry_map()
        c = str(code or "").zfill(6)
        return self._industry_cache.get(c) or board_of(c)

    # -------------------------------------------------------------- 价量
    def _frame(self, code: str, as_of: str) -> pd.DataFrame | None:
        """返回严格截断到 as_of 的日线 frame（无未来数据）。"""
        c = str(code or "").zfill(6)
        if self._klines is not None:
            df = self._klines.get(c)
            if df is None:
                return None
            idx = (self._pos_index or {}).get(c, {}).get(str(as_of))
            if idx is not None:
                return df.iloc[: int(idx) + 1]
            return df[df["date"] <= str(as_of)]
        path = self.kline_dir / f"{c}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError, ImportError):
            return None
        if df is None or df.empty or "date" not in df or "close" not in df:
            return None
        return df[df["date"] <= str(as_of)]

    def _finish(self, key: tuple[str, str, int], out: tuple[float, ...]) -> tuple[float, ...]:
        if len(self._series_cache) > 20000:
            self._series_cache.clear()
        self._series_cache[key] = out
        return out

    def closes(self, code: str, as_of: str, window: int) -> tuple[float, ...]:
        """截至 as_of 的最近 ``window`` 根收盘价（升序，严格无未来数据）。"""
        key = (str(code), str(as_of), int(window))
        hit = self._series_cache.get(key)
        if hit is not None:
            return hit
        c = str(code or "").zfill(6)
        n = int(window)
        # 快路径：回测注入了 prepared 日线 + pos_index，直接按索引取窗口，
        # 不对整段历史做 iloc[:idx+1] 大切片（250 天 × 30 只会明显拖慢回测）。
        if self._klines is not None:
            full = self._klines.get(c)
            idx = (self._pos_index or {}).get(c, {}).get(str(as_of))
            if full is not None and idx is not None and "close" in full:
                i = int(idx)
                series = full["close"].iloc[max(0, i + 1 - n): i + 1]
                vals = pd.to_numeric(series, errors="coerce").dropna().tolist()
                return self._finish(key, tuple(float(v) for v in vals if float(v) > 0))
        df = self._frame(code, as_of)
        if df is None or df.empty:
            return self._finish(key, ())
        vals = pd.to_numeric(df["close"].tail(n), errors="coerce").dropna().tolist()
        return self._finish(key, tuple(float(v) for v in vals if float(v) > 0))

    def ret(self, code: str, as_of: str, lookback: int) -> float | None:
        """截至 as_of 的 ``lookback`` 根收益率；样本不足返回 None。"""
        vals = self.closes(code, as_of, int(lookback) + 1)
        if len(vals) < int(lookback) + 1 or vals[0] <= 0:
            return None
        return vals[-1] / vals[0] - 1.0

    # -------------------------------------------------------------- 事件
    def bulk_events(
        self, codes: Sequence[str], as_of: str, cooldown_days: int
    ) -> dict[str, list[dict[str, Any]]]:
        """批量取「事件日 ≤ as_of 且落在冷却窗口内」的事件。

        严格只回看过去：财报/解禁/减持这类公告在发生前无法从现有数据确定，
        因此不做前瞻禁止，避免把「当时不可能知道的信息」引入回测。
        """
        codes = [str(c) for c in codes]
        if not codes or int(cooldown_days) <= 0:
            return {}
        from datetime import datetime, timedelta
        try:
            start = (datetime.strptime(str(as_of)[:10], "%Y-%m-%d")
                     - timedelta(days=int(cooldown_days))).strftime("%Y-%m-%d")
        except ValueError:
            start = str(as_of)[:10]
        kinds = sorted(BLOCKING_EVENT_KINDS)
        marks = ",".join("?" for _ in codes)
        kmarks = ",".join("?" for _ in kinds)
        rows = self._fetchall(
            f"SELECT code, event_date, kind, severity FROM context_events "
            f"WHERE code IN ({marks}) AND kind IN ({kmarks}) "
            f"AND event_date <= ? AND event_date >= ?",
            [*codes, *kinds, str(as_of)[:10], start],
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["code"]), []).append(dict(r))
        return out

    # -------------------------------------------------------------- 舆情
    def bulk_news(
        self, codes: Sequence[str], as_of: str, lookback_days: int
    ) -> dict[str, float]:
        """批量取截至 as_of 的加权舆情分（负=利空），按 ``sentiment × heat`` 均值。"""
        codes = [str(c) for c in codes]
        if not codes or int(lookback_days) <= 0:
            return {}
        from datetime import datetime, timedelta
        try:
            start = (datetime.strptime(str(as_of)[:10], "%Y-%m-%d")
                     - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
        except ValueError:
            start = str(as_of)[:10]
        marks = ",".join("?" for _ in codes)
        rows = self._fetchall(
            f"SELECT code, sentiment, heat FROM context_news "
            f"WHERE code IN ({marks}) AND news_date <= ? AND news_date >= ?",
            [*codes, str(as_of)[:10], start],
        )
        acc: dict[str, tuple[float, float]] = {}
        for r in rows:
            c = str(r["code"])
            s = float(r["sentiment"] or 0.0)
            w = max(float(r["heat"] or 1.0), 0.0)
            num, den = acc.get(c, (0.0, 0.0))
            acc[c] = (num + s * w, den + w)
        return {c: (num / den if den > 0 else 0.0) for c, (num, den) in acc.items()}

    # -------------------------------------------------------------- 热度
    def heat_thresholds(self, as_of: str) -> dict[str, Any]:
        """读「截至 as_of 最近一个已构建交易日」的全市场过热分位阈值。

        关键：阈值来自 ``context_heat`` 表（全市场口径、由本地日线预计算），
        两侧共读，**不随各自候选池大小变化**。表为空 / 日期还没构建时返回 {}，
        两侧一致地让过热子闸不触发，不会出现半边生效。
        """
        day = str(as_of)[:10]
        if day in self._heat_cache:
            return self._heat_cache[day]
        rows = self._fetchall(
            "SELECT stock_cut, industry_cut, hot_industries FROM context_heat "
            "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1",
            [day],
        )
        out: dict[str, Any] = {}
        if rows:
            r = rows[0]
            try:
                out = {
                    "stock": float(r["stock_cut"]) if r["stock_cut"] is not None else None,
                    "industry": float(r["industry_cut"]) if r["industry_cut"] is not None else None,
                    "hot_industries": frozenset(json.loads(r["hot_industries"] or "[]")),
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                out = {}
        if len(self._heat_cache) > 3000:
            self._heat_cache.clear()
        self._heat_cache[day] = out
        return out

    # -------------------------------------------------------------- 覆盖
    def coverage(self) -> dict[str, Any]:
        """快照覆盖情况（只读诊断；不参与是否漂移的判定）。"""
        out: dict[str, Any] = {"events": 0, "news": 0, "heat_days": 0,
                               "events_from": None, "events_to": None,
                               "news_from": None, "news_to": None,
                               "heat_from": None, "heat_to": None,
                               "db": str(self.db_path), "tables": False}
        conn = self._conn()
        if conn is None:
            return out
        try:
            out["tables"] = True
            out["events"] = int(conn.execute("SELECT COUNT(*) FROM context_events").fetchone()[0])
            out["news"] = int(conn.execute("SELECT COUNT(*) FROM context_news").fetchone()[0])
            r = conn.execute("SELECT MIN(event_date), MAX(event_date) FROM context_events").fetchone()
            out["events_from"], out["events_to"] = r[0], r[1]
            r = conn.execute("SELECT MIN(news_date), MAX(news_date) FROM context_news").fetchone()
            out["news_from"], out["news_to"] = r[0], r[1]
            r = conn.execute(
                "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM context_heat"
            ).fetchone()
            out["heat_days"], out["heat_from"], out["heat_to"] = int(r[0]), r[1], r[2]
        except sqlite3.Error:
            out["tables"] = False
        return out


_STORE: ContextStore | None = None
_STORE_LOCK = threading.Lock()


def get_context_store() -> ContextStore:
    """实盘进程共用的惰性单例（回测自己按 prepared 构造，不走这里）。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = ContextStore()
    return _STORE


def reset_context_store() -> None:
    """测试数据目录切换 / 采集后强制重建连接（不删任何持久数据）。"""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
