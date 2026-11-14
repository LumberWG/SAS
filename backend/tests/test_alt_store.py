"""另类数据快照的零前视回归测试（离线，不依赖网络与生产库）。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from app.data.alt_store import (
    ASOF_RULES,
    FEATURE_NAMES,
    SCHEMA_SQL,
    all_features,
    gdhs_features,
    lhb_features,
    margin_features,
)


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "alt.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_margin_strictly_before_as_of(tmp_path):
    conn = _db(tmp_path)
    for day, bal in (("2026-01-05", 100.0), ("2026-01-06", 200.0), ("2026-01-07", 300.0)):
        conn.execute(
            "INSERT INTO alt_margin(trade_date, code, margin_balance) VALUES (?,?,?)",
            (day, "000001", bal),
        )
    conn.commit()
    # T 日盘后才发布：as_of=01-06 只能看到 01-05
    assert margin_features(conn, "000001", "2026-01-06")["margin_last"] == 100.0
    assert margin_features(conn, "000001", "2026-01-06")["margin_n"] == 1
    assert margin_features(conn, "000001", "2026-01-07")["margin_last"] == 200.0
    conn.close()


def test_gdhs_uses_ann_date_not_report_period(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO alt_gdhs(code, end_date, ann_date, change_pct) VALUES (?,?,?,?)",
        ("000001", "2025-12-31", "2026-03-31", -5.0),
    )
    conn.commit()
    # 报告期是 2025-12-31，但公告要到 2026-03-31 才公开；用报告期会前视
    assert gdhs_features(conn, "000001", "2026-01-10")["gdhs_chg_pct"] is None
    row = gdhs_features(conn, "000001", "2026-04-01")
    assert row["gdhs_chg_pct"] == -5.0
    assert row["gdhs_lag_days"] == 1
    conn.close()


def test_gdhs_missing_ann_date_is_unusable(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO alt_gdhs(code, end_date, ann_date, change_pct) VALUES (?,?,?,?)",
        ("000001", "2025-12-31", None, 3.0),
    )
    conn.commit()
    assert gdhs_features(conn, "000001", "2026-06-01")["gdhs_chg_pct"] is None
    conn.close()


def test_lhb_future_excluded_and_same_day_deduped(tmp_path):
    conn = _db(tmp_path)
    conn.executemany(
        "INSERT INTO alt_lhb(trade_date, code, reason, net_buy) VALUES (?,?,?,?)",
        [
            ("2026-01-05", "000001", "日涨幅偏离值达7%", 100.0),
            ("2026-01-05", "000001", "换手率达20%", 100.0),
            ("2026-01-10", "000001", "连续三日涨幅偏离", 999.0),
        ],
    )
    conn.commit()
    row = lhb_features(conn, "000001", "2026-01-10")
    assert row["lhb_count20"] == 1  # 同日两条原因只算一次
    assert row["lhb_net20"] == 100.0  # 未来那笔 999 不可见
    conn.close()


def test_lhb_keep_drops_forward_return_columns():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import fetch_alt_data as fetcher

    assert "代码" in fetcher.LHB_KEEP
    assert "上榜日" in fetcher.LHB_KEEP
    assert not any("上榜后" in k for k in fetcher.LHB_KEEP)
    assert not any("日后" in k for k in fetcher.LHB_KEEP)


def test_all_features_keys_and_asof_rules():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    out = all_features(conn, "000001", "2026-01-10")
    for name in FEATURE_NAMES:
        assert name in out
    assert out["lhb_count20"] == 0  # 没有上榜记录 = 0 次，是有效值而非缺失
    for name in ("margin_chg5", "margin_chg10", "lhb_net20", "gdhs_chg_pct"):
        assert out[name] is None
    assert "trade_date < as_of" in ASOF_RULES["alt_margin"]
    assert "ann_date <= as_of" in ASOF_RULES["alt_gdhs"]
    conn.close()
