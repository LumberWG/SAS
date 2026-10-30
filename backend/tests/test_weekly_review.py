"""周度复盘脚本：只读汇总 + 诊断分支。"""
import sqlite3

import pytest

from scripts import weekly_review as wr


@pytest.fixture()
def fake_db(tmp_path, monkeypatch):
    """建一个最小 schema 的临时库，并把 settings.db_path 指过去。"""
    path = tmp_path / "review.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE paper_equity (
            date TEXT PRIMARY KEY, total_asset REAL, cash REAL,
            market_value REAL, created_at TEXT);
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT, side TEXT,
            price REAL, shares INTEGER, amount REAL, fee REAL, reason TEXT,
            trade_date TEXT, created_at TEXT);
        CREATE TABLE paper_positions (
            code TEXT PRIMARY KEY, name TEXT, shares INTEGER, cost_price REAL,
            buy_date TEXT, score REAL);
        CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        """
    )
    conn.commit()
    conn.close()

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "db_path", path, raising=False)
    return path


def _exec(path, sql, rows=()):
    conn = sqlite3.connect(path)
    conn.executemany(sql, rows) if rows else conn.execute(sql)
    conn.commit()
    conn.close()


def test_report_sums_trades_and_regime(fake_db):
    _exec(fake_db, "INSERT INTO paper_equity VALUES (?,?,?,?,?)", [
        ("2026-10-28", 100000.0, 100000.0, 0.0, "2026-10-28 15:40:00"),
        ("2026-10-30", 98000.0, 48000.0, 50000.0, "2026-10-30 15:40:00"),
    ])
    _exec(fake_db, "INSERT INTO paper_trades (code,name,side,price,shares,amount,fee,"
                   "reason,trade_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("600000", "测试票", "buy", 10.0, 5000, 50000.0, 5.0, "突破", "2026-10-29", "x"),
        ("600000", "测试票", "sell", 9.5, 5000, 47500.0, 52.5, "止损", "2026-10-30", "x"),
    ])
    _exec(fake_db, "INSERT INTO app_state VALUES (?,?,?)",
          [("paper_regime_paused:2026-10-29", "120", "x")])

    report = wr.build_report("2026-10-28", "2026-10-30")

    assert report["trades"]["count"] == 2
    assert report["trades"]["buys"] == 1
    assert report["trades"]["sells"] == 1
    assert report["equity"]["change_pct"] == pytest.approx(-2.0)
    assert report["regime_total"] == 120
    assert {d.day: d.regime_paused for d in report["days"]}["2026-10-29"] == 120

    text = wr.render_text(report)
    assert "复盘区间 2026-10-28 ~ 2026-10-30" in text
    assert "弱市闸门另拦截 120 次" in wr.diagnose(report)


def test_diagnose_zero_trade_with_regime(fake_db):
    _exec(fake_db, "INSERT INTO app_state VALUES (?,?,?)",
          [("paper_regime_paused:2026-10-30", "1999", "x")])

    report = wr.build_report("2026-10-30", "2026-10-30")

    assert report["trades"]["count"] == 0
    assert "弱市闸门累计拦截 1999 次" in wr.diagnose(report)


def test_diagnose_zero_trade_without_regime(fake_db):
    report = wr.build_report("2026-10-30", "2026-10-30")

    msg = wr.diagnose(report)
    assert "没有弱市拦截记录" in msg
    assert "2026-10-30 起才写 app_state" in msg
