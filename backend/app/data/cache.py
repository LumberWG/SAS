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
-- 模拟交易（Paper Trading）：单账户 + 持仓 + 交易流水 + 每日净值
CREATE TABLE IF NOT EXISTS paper_account (
    id             INTEGER PRIMARY KEY CHECK (id = 1),  -- 单账户约束
    initial_capital REAL NOT NULL,   -- 初始资金
    cash           REAL NOT NULL,    -- 可用现金
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    shares     INTEGER NOT NULL,     -- 持仓股数（整手100）
    cost_price REAL NOT NULL,        -- 摊薄成本价（含买入手续费）
    buy_date   TEXT NOT NULL,        -- 最近一次买入日期（T+1 可卖判断）
    strategy   TEXT,                 -- 买入来源（策略/建议原因）
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    name       TEXT,
    side       TEXT NOT NULL,        -- buy / sell
    price      REAL NOT NULL,        -- 成交价
    shares     INTEGER NOT NULL,     -- 成交股数
    amount     REAL NOT NULL,        -- 成交金额（不含费用）
    fee        REAL NOT NULL,        -- 费用合计（佣金+印花税+过户费）
    reason     TEXT,                 -- 交易原因
    trade_date TEXT NOT NULL,        -- 信号/交易日期
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_date ON paper_trades(trade_date);
-- 每日净值快照（模拟盘净值曲线）
CREATE TABLE IF NOT EXISTS paper_equity (
    date          TEXT PRIMARY KEY,
    total_asset   REAL NOT NULL,     -- 总资产 = 现金 + 持仓市值
    cash          REAL NOT NULL,
    market_value  REAL NOT NULL,
    created_at    TEXT NOT NULL
);
-- 智能建议（盘中买卖建议 / 盘后明日关注，按批次存储）
CREATE TABLE IF NOT EXISTS recommendations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL,
    name         TEXT,
    action       TEXT NOT NULL,   -- buy / sell / hold / focus
    score        REAL,            -- 技术面评分
    reason       TEXT,
    price        REAL,
    stop_loss    REAL,
    target_price REAL,
    sentiment    TEXT,            -- 生成时的市场情绪等级
    batch        TEXT NOT NULL,   -- 批次：intraday_/premarket_/focus_ + 日期
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reco_batch ON recommendations(batch);
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

    # ---------------- 模拟交易 ----------------

    def get_paper_account(self) -> dict[str, Any] | None:
        """模拟账户（不存在返回 None）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_account WHERE id=1"
            ).fetchone()
        return dict(row) if row else None

    def init_paper_account(self, initial_capital: float) -> dict[str, Any]:
        """初始化模拟账户（幂等：已存在则直接返回）。"""
        existing = self.get_paper_account()
        if existing:
            return existing
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_account(id, initial_capital, cash, created_at)"
                " VALUES(1,?,?,?)",
                (initial_capital, initial_capital, _now()),
            )
        return {"initial_capital": initial_capital, "cash": initial_capital}

    def update_paper_cash(self, cash: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE paper_account SET cash=? WHERE id=1", (cash,))

    def list_paper_positions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_paper_position(self, code: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_positions WHERE code=?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_paper_position(
        self, code: str, name: str, shares: int, cost_price: float,
        buy_date: str, strategy: str,
    ) -> None:
        """新增/覆盖持仓（同代码重复买入时摊薄成本由调用方算好传入）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_positions(code, name, shares, cost_price,"
                " buy_date, strategy, created_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
                " shares=excluded.shares, cost_price=excluded.cost_price,"
                " buy_date=excluded.buy_date, strategy=excluded.strategy",
                (code, name, shares, cost_price, buy_date, strategy, _now()),
            )

    def remove_paper_position(self, code: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM paper_positions WHERE code=?", (code,))

    def insert_paper_trade(
        self, code: str, name: str, side: str, price: float, shares: int,
        amount: float, fee: float, reason: str, trade_date: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_trades(code, name, side, price, shares,"
                " amount, fee, reason, trade_date, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, name, side, price, shares, amount, fee, reason,
                 trade_date, _now()),
            )

    def list_paper_trades(
        self, limit: int = 100, code: str | None = None
    ) -> list[dict[str, Any]]:
        """交易流水（按时间倒序）。"""
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE code=? ORDER BY id DESC LIMIT ?",
                    (code, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def save_paper_equity(
        self, date: str, total_asset: float, cash: float, market_value: float
    ) -> None:
        """保存每日净值快照（同日覆盖）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_equity(date, total_asset, cash, market_value,"
                " created_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(date) DO UPDATE SET total_asset=excluded.total_asset,"
                " cash=excluded.cash, market_value=excluded.market_value",
                (date, total_asset, cash, market_value, _now()),
            )

    def list_paper_equity(self, limit: int = 250) -> list[dict[str, Any]]:
        """净值曲线（按日期升序，限制条数）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_equity ORDER BY date LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def reset_paper_account(self, initial_capital: float) -> None:
        """重置模拟账户：清空持仓/流水/净值，资金恢复初始值。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_trades")
            conn.execute("DELETE FROM paper_equity")
            conn.execute(
                "INSERT INTO paper_account(id, initial_capital, cash, created_at)"
                " VALUES(1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET initial_capital=excluded.initial_capital,"
                " cash=excluded.cash",
                (initial_capital, initial_capital, _now()),
            )

    # ---------------- 智能建议 ----------------

    def save_recommendations(
        self, recommendations: list[dict], batch: str
    ) -> int:
        """保存一批建议（先清除同批次旧记录，实现同批次只保留最新）。

        Args:
            recommendations: 建议 dict 列表（code/name/action/score/reason/
                price/stop_loss/target_price/sentiment）
            batch: 批次标识，如 intraday_2026-10-22 / focus_2026-10-21

        Returns:
            保存条数
        """
        if not recommendations:
            return 0
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM recommendations WHERE batch=?", (batch,))
            conn.executemany(
                "INSERT INTO recommendations(code, name, action, score, reason,"
                " price, stop_loss, target_price, sentiment, batch, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["code"], r.get("name", ""), r["action"], r.get("score"),
                        r.get("reason", ""), r.get("price"), r.get("stop_loss"),
                        r.get("target_price"), r.get("sentiment", ""), batch, _now(),
                    )
                    for r in recommendations
                ],
            )
        return len(recommendations)

    def list_recommendations(
        self, batch_prefix: str, action: str | None = None
    ) -> list[dict[str, Any]]:
        """查询某类批次的最新一批建议（按评分排序）。

        Args:
            batch_prefix: 批次前缀，如 intraday / focus / premarket
            action: 可选过滤 buy / sell / hold / focus
        """
        with self._lock, self._connect() as conn:
            # 取该前缀下最新的批次（批次名含日期，字符串最大即最新）
            row = conn.execute(
                "SELECT MAX(batch) AS latest FROM recommendations WHERE batch LIKE ?",
                (f"{batch_prefix}%",),
            ).fetchone()
            if not row or not row["latest"]:
                return []
            if action:
                rows = conn.execute(
                    "SELECT * FROM recommendations WHERE batch=? AND action=?"
                    " ORDER BY score DESC",
                    (row["latest"], action),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recommendations WHERE batch=? ORDER BY score DESC",
                    (row["latest"],),
                ).fetchall()
        return [dict(r) for r in rows]

    def latest_batch(self, batch_prefix: str) -> str | None:
        """某类批次的最新批次名（含日期）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(batch) AS latest FROM recommendations WHERE batch LIKE ?",
                (f"{batch_prefix}%",),
            ).fetchone()
        return row["latest"] if row and row["latest"] else None

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
