"""本地数据缓存层。

存储策略：
- 历史 K 线 → Parquet 文件（按股票代码一个文件，列式存储，追加写入快）；
- 结构化小数据（自选股 / 策略信号 / 回测记录）→ SQLite（WAL 模式，
  个人单机使用完全够用，且读写并发不互斥）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings


def _now() -> str:
    """当前时间字符串（本地时区）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class KlineCache:
    """日K线 Parquet 缓存。

    每个股票一个文件：``{kline_dir}/{code}.parquet``。
    通过 ``last_date`` 元数据配合数据源实现增量更新（只拉取新数据）。
    """

    def __init__(self, kline_dir: Path | None = None) -> None:
        settings = get_settings()
        self.dir = kline_dir or settings.kline_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, code: str) -> Path:
        return self.dir / f"{code}.parquet"

    def get(self, code: str) -> pd.DataFrame | None:
        """读取缓存的K线；不存在或损坏返回 None。"""
        path = self._path(code)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            return df if not df.empty else None
        except Exception:
            # 缓存文件损坏时视为未命中，允许重新拉取
            return None

    def last_date(self, code: str) -> str | None:
        """返回缓存中最后一条K线的日期（YYYY-MM-DD），用于增量更新。"""
        df = self.get(code)
        if df is None:
            return None
        return str(df["date"].iloc[-1])

    def save(self, code: str, df: pd.DataFrame) -> None:
        """保存/更新K线（按日期去重合并后原子写入）。"""
        if df is None or df.empty:
            return
        existing = self.get(code)
        if existing is not None:
            # 合并并按日期去重（同一天以新拉取的数据为准）
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)
        df.to_parquet(self._path(code), index=False)


# SQLite 建表语句：自选股 / 策略信号 / 回测记录
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    strategy     TEXT NOT NULL,
    price        REAL,
    reason       TEXT DEFAULT '',
    stop_loss    REAL,
    target_price REAL,
    scan_date    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_code      ON signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_scan_date ON signals(scan_date);
-- 自定义策略（用户在前端改参数另存，启动时自动注册进注册表）
CREATE TABLE IF NOT EXISTS custom_strategies (
    name          TEXT PRIMARY KEY,
    base_strategy TEXT NOT NULL,   -- 基础内置策略名（提供 evaluate 逻辑）
    description   TEXT,
    category      TEXT,
    params        TEXT NOT NULL,   -- JSON：用户参数 {key: value}
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy     TEXT NOT NULL,
    start_date   TEXT,
    end_date     TEXT,
    params       TEXT,   -- JSON：初始资金/持仓数/手续费等
    metrics      TEXT,   -- JSON：收益率/胜率/最大回撤/夏普等
    equity_curve TEXT,   -- JSON：[{date, equity}, ...]
    created_at   TEXT NOT NULL
);
"""


class Database:
    """SQLite 元数据库（线程安全，WAL 模式）。

    注意：实时快照（盘中每 30 秒 × 5000 只）不落库，
    只放内存并按需聚合，避免数据库文件无限膨胀。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        settings = get_settings()
        self.path = db_path or settings.db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: FastAPI 多线程共享连接，由 self._lock 保护
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    # ---------------- 自选股 ----------------

    def add_watch(self, code: str, name: str, note: str = "") -> None:
        """添加/更新自选股（按 code 主键幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO watchlist(code, name, note, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, note=excluded.note",
                (code, name, note, _now()),
            )

    def remove_watch(self, code: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE code=?", (code,))

    def list_watchlist(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT code, name, note, created_at FROM watchlist ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 策略信号 ----------------

    def save_signals(self, signals: list[dict[str, Any]], scan_date: str) -> None:
        """批量保存一轮扫描的命中信号。"""
        if not signals:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO signals(code, name, strategy, price, reason, stop_loss,"
                " target_price, scan_date, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        s["code"], s.get("name", ""), s["strategy"], s.get("price"),
                        s.get("reason", ""), s.get("stop_loss"), s.get("target_price"),
                        scan_date, _now(),
                    )
                    for s in signals
                ],
            )

    def list_signals(
        self, code: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """查询历史信号（可按股票过滤，按时间倒序）。"""
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE code=? ORDER BY id DESC LIMIT ?",
                    (code, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def signal_stats_by_date(self, scan_date: str) -> dict[str, int]:
        """统计某日各策略的命中数量（选股中心策略徽标用）。

        Returns:
            {策略名: 命中数}
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT strategy, COUNT(*) AS cnt FROM signals"
                " WHERE scan_date=? GROUP BY strategy",
                (scan_date,),
            ).fetchall()
        return {r["strategy"]: r["cnt"] for r in rows}

    # ---------------- 自定义策略 ----------------

    def save_custom_strategy(
        self, name: str, base_strategy: str, params: dict,
        description: str = "", category: str = "自定义",
    ) -> None:
        """保存自定义策略（按 name 主键幂等，更新即覆盖）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO custom_strategies(name, base_strategy, description,"
                " category, params, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET base_strategy=excluded.base_strategy,"
                " description=excluded.description, category=excluded.category,"
                " params=excluded.params",
                (name, base_strategy, description, category,
                 json.dumps(params, ensure_ascii=False), _now()),
            )

    def list_custom_strategies(self) -> list[dict[str, Any]]:
        """全部自定义策略（params 已解析为 dict）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT name, base_strategy, description, category, params, created_at"
                " FROM custom_strategies ORDER BY created_at"
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["params"] = json.loads(item["params"] or "{}")
            result.append(item)
        return result

    def delete_custom_strategy(self, name: str) -> bool:
        """删除自定义策略，返回是否删除成功。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM custom_strategies WHERE name=?", (name,)
            )
        return cur.rowcount > 0

    # ---------------- 回测记录 ----------------

    def save_backtest(self, run: dict[str, Any]) -> int:
        """保存一次回测结果，返回自增 id。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO backtest_runs(strategy, start_date, end_date, params,"
                " metrics, equity_curve, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    run["strategy"], run.get("start_date"), run.get("end_date"),
                    json.dumps(run.get("params", {}), ensure_ascii=False),
                    json.dumps(run.get("metrics", {}), ensure_ascii=False),
                    json.dumps(run.get("equity_curve", []), ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_backtest(self, run_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backtest_runs WHERE id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["params"] = json.loads(result["params"] or "{}")
        result["metrics"] = json.loads(result["metrics"] or "{}")
        result["equity_curve"] = json.loads(result["equity_curve"] or "[]")
        return result

    def list_backtests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# 全局单例
kline_cache = KlineCache()
db = Database()
