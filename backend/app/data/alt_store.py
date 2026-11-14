"""另类数据本地快照的只读查询层（影子研究，零生产写入）。

数据由 ``scripts/fetch_alt_data.py`` 落到独立的 ``alt_data.db``，本模块只负责
「按 as_of 截断」的查询，是将来回测与实盘共读同一份数据的唯一入口。

零前视规则见 :data:`ASOF_RULES`。**这些字段目前全是研究资产，不进任何决策链路**；
接入前必须在 ``app/backtest/alignment.py`` 的 BINDINGS 或 RESEARCH_ONLY 登记，
并通过 ``scripts/check_backtest_alignment.py`` 零漂移自检（同 ``ContextStore`` 红线）。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("SAS_DATA_DIR", "D:/Backup/SAS/data"))
DEFAULT_ALT_DB = DATA_DIR / "alt_data.db"

ASOF_RULES = {
    "alt_margin": "trade_date < as_of（T 日盘后由交易所发布，T 收盘做信号时不可得）",
    "alt_lhb": "trade_date < as_of（T 日盘后上榜名单才公布）",
    "alt_gdhs": "ann_date <= as_of（只用公告日期；统计截止日是报告期，用它等于看未来）",
}

FEATURE_NAMES = ("margin_chg5", "margin_chg10", "lhb_count20", "lhb_net20", "gdhs_chg_pct")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alt_margin (
    trade_date      TEXT NOT NULL,
    code            TEXT NOT NULL,
    margin_balance  REAL,
    margin_buy      REAL,
    source          TEXT,
    collected_at    TEXT,
    PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS alt_lhb (
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    net_buy      REAL,
    buy_amt      REAL,
    sell_amt     REAL,
    source       TEXT,
    collected_at TEXT,
    PRIMARY KEY (trade_date, code, reason)
);
CREATE TABLE IF NOT EXISTS alt_gdhs (
    code         TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    ann_date     TEXT,
    holders      REAL,
    prev_holders REAL,
    change_pct   REAL,
    source       TEXT,
    collected_at TEXT,
    PRIMARY KEY (code, end_date, ann_date)
);
CREATE TABLE IF NOT EXISTS _progress (
    part TEXT NOT NULL,
    key  TEXT NOT NULL,
    ok   INTEGER NOT NULL,
    ts   TEXT,
    PRIMARY KEY (part, key)
);
CREATE INDEX IF NOT EXISTS idx_alt_margin_code ON alt_margin(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_alt_lhb_code ON alt_lhb(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_alt_gdhs_code ON alt_gdhs(code, ann_date);
"""


def connect(
    db_path: str | Path = DEFAULT_ALT_DB, *, readonly: bool = True
) -> sqlite3.Connection:
    """打开快照库；readonly 用 SQLite 的 mode=ro，保证探针绝不误写。"""
    p = Path(db_path)
    if readonly:
        if not p.exists():
            raise FileNotFoundError(f"另类数据快照不存在：{p}（先跑 fetch_alt_data.py）")
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(as_of: str, days: int) -> str:
    return (date.fromisoformat(as_of[:10]) - timedelta(days=days)).isoformat()


def margin_features(
    conn: sqlite3.Connection, code: str, as_of: str, lookback: int = 10
) -> dict:
    """融资余额近 5/10 个交易日变化率；严格 ``trade_date < as_of``。"""
    rows = conn.execute(
        "SELECT trade_date, margin_balance FROM alt_margin "
        "WHERE code = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT ?",
        (code, as_of[:10], lookback + 1),
    ).fetchall()
    vals = [float(r["margin_balance"]) for r in reversed(rows) if r["margin_balance"] is not None]
    out: dict = {"margin_chg5": None, "margin_chg10": None, "margin_last": None, "margin_n": len(vals)}
    if not vals:
        return out
    out["margin_last"] = vals[-1]
    if len(vals) >= 6 and vals[-6] > 0:
        out["margin_chg5"] = vals[-1] / vals[-6] - 1
    if len(vals) >= 11 and vals[0] > 0:
        out["margin_chg10"] = vals[-1] / vals[0] - 1
    return out


def lhb_features(
    conn: sqlite3.Connection, code: str, as_of: str, window_days: int = 40
) -> dict:
    """近 ~20 个交易日上榜次数与净买额；同一天多个上榜原因只算一次。"""
    rows = conn.execute(
        "SELECT trade_date, net_buy FROM alt_lhb "
        "WHERE code = ? AND trade_date < ? AND trade_date >= ? ORDER BY trade_date",
        (code, as_of[:10], _cutoff(as_of, window_days)),
    ).fetchall()
    by_day: dict[str, float] = {}
    for r in rows:
        by_day.setdefault(r["trade_date"], float(r["net_buy"] or 0.0))
    return {
        "lhb_count20": len(by_day),
        "lhb_net20": sum(by_day.values()) if by_day else None,
    }


def gdhs_features(conn: sqlite3.Connection, code: str, as_of: str) -> dict:
    """最近一次**已公告**的股东户数环比；``ann_date <= as_of``，公告缺失的行不可用。"""
    row = conn.execute(
        "SELECT end_date, ann_date, holders, change_pct FROM alt_gdhs "
        "WHERE code = ? AND ann_date IS NOT NULL AND ann_date <= ? "
        "ORDER BY ann_date DESC, end_date DESC LIMIT 1",
        (code, as_of[:10]),
    ).fetchone()
    if row is None:
        return {"gdhs_chg_pct": None, "gdhs_ann_date": None, "gdhs_lag_days": None}
    lag = (date.fromisoformat(as_of[:10]) - date.fromisoformat(row["ann_date"])).days
    return {
        "gdhs_chg_pct": row["change_pct"],
        "gdhs_ann_date": row["ann_date"],
        "gdhs_lag_days": lag,
    }


def all_features(conn: sqlite3.Connection, code: str, as_of: str) -> dict:
    """一次性取全部特征，键与 :data:`FEATURE_NAMES` 对齐（缺失为 None）。"""
    out: dict = {}
    out.update(margin_features(conn, code, as_of))
    out.update(lhb_features(conn, code, as_of))
    out.update(gdhs_features(conn, code, as_of))
    return out
