"""运维复盘只读接口测试：路径白名单、优雅降级、线上 ML 状态语义。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from app.analysis import live_ml
from app.api import ops


def test_review_whitelist_and_missing(monkeypatch, tmp_path):
    """只认三个固定文件名；越界名直接 400，文件不存在返回 available=false。"""
    monkeypatch.setattr(ops, "_REVIEW_DIR", tmp_path)
    (tmp_path / "daily_trade_review.md").write_text("# 收盘复盘\n", encoding="utf-8")

    r = ops.review("daily")
    assert r["available"] is True and "收盘复盘" in r["content"]
    assert r["updated_at"]

    with pytest.raises(HTTPException):
        ops.review("../../.env")
    assert ops.review("param")["available"] is False


def test_shadow_rejects_bad_tag_and_missing_file(monkeypatch, tmp_path):
    """tag 不允许路径字符；影子账户还没跑过时不能 500。"""
    with pytest.raises(HTTPException):
        ops.shadow("../secrets")

    monkeypatch.setattr(
        ops, "get_settings",
        lambda: SimpleNamespace(kline_dir=tmp_path / "klines"),
    )
    r = ops.shadow("live")
    assert r["available"] is False and r["profiles"] == []


def test_shadow_ml_pair(monkeypatch, tmp_path):
    """default_ml 在方案集内时给出前向超额配对差，不在时明确提示。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    data = {
        "tag": "live", "start": "2026-11-01", "universe": 300,
        "profiles": {
            "default": {"snapshots": [{"as_of": "2026-11-06", "excess": 1.2, "total_return": 3.0, "trade_count": 5}]},
            "default_ml": {"snapshots": [{"as_of": "2026-11-06", "excess": 4.8, "total_return": 6.6, "trade_count": 7}]},
        },
    }
    (log_dir / "shadow_live.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        ops, "get_settings",
        lambda: SimpleNamespace(kline_dir=tmp_path / "klines"),
    )

    r = ops.shadow("live")
    assert r["available"] is True and len(r["profiles"]) == 2
    assert r["ml_pair"]["diff"] == pytest.approx(3.6)
    assert r["pair_identical"] is False

    # 两方案一模一样（典型：ML 整段回退 14 因子）必须提示「不能当配对结论」
    data["profiles"]["default_ml"] = data["profiles"]["default"]
    (log_dir / "shadow_live.json").write_text(json.dumps(data), encoding="utf-8")
    same = ops.shadow("live")
    assert same["pair_identical"] is True and "回退" in same["note"]

    data["profiles"].pop("default_ml")
    (log_dir / "shadow_live.json").write_text(json.dumps(data), encoding="utf-8")
    r2 = ops.shadow("live")
    assert r2["ml_pair"] is None and "default_ml" in r2["note"]


def test_ml_status_distinguishes_pending_fallback_effective(monkeypatch):
    """开关开 ≠ 生效；「还没跑」和「已回退」必须能区分。"""
    live_ml.clear_cache()
    monkeypatch.setattr(
        live_ml, "get_settings",
        lambda: SimpleNamespace(live_ml_scoring=True),
    )
    try:
        s = ops.ml_status()
        assert s["state"] == "pending" and s["effective"] is False

        live_ml._record_attempt("2026-11-06", "no_signal", 0.0)
        s = ops.ml_status()
        assert s["state"] == "fallback" and s["effective"] is False

        live_ml._LIVE_CACHE["2026-11-06"] = {"600519": 80.0}
        live_ml._LIVE_IC["2026-11-06"] = 0.03
        live_ml._record_attempt("2026-11-06", "ok", 0.03)
        s = ops.ml_status()
        assert s["state"] == "effective" and s["effective"] is True
        assert s["codes"] == 1 and s["oos_rank_ic"] == pytest.approx(0.03)
    finally:
        live_ml.clear_cache()


def test_self_check_reports_all_subsystems(monkeypatch, tmp_path):
    """自检要覆盖库结构/快照/调度/分钟库/影子/ML，且临时干净库判为 ok。"""
    from datetime import datetime

    from app.data import cache as cache_module
    from app.data.cache import Database

    database = Database(tmp_path / "selfcheck.db")
    monkeypatch.setattr(cache_module, "db", database)
    monkeypatch.setattr(
        ops, "_minute_export_status",
        lambda: {"available": True, "latest_date": datetime.now().strftime("%Y%m%d"),
                 "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    )

    r = ops.self_check()
    keys = {c["key"] for c in r["checks"]}
    assert {"db", "snapshot", "scheduler", "minute", "shadow", "ml", "tests"} <= keys
    assert r["level"] in {"ok", "warn", "error"}
    db_check = next(c for c in r["checks"] if c["key"] == "db")
    assert db_check["level"] == "ok" and "provenance" in db_check["detail"]


def test_breadth_missing_is_graceful(monkeypatch):
    from app.data import breadth

    monkeypatch.setattr(
        breadth, "load",
        lambda: pd.DataFrame(columns=breadth.COLUMNS).set_index("date"),
    )
    r = ops.breadth_history(10)
    assert r["available"] is False and r["rows"] == []
