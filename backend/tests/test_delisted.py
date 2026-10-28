"""退市股历史补齐脚本的纯逻辑测试（不联网）。"""
import pandas as pd

from app.data.cache import KlineCache


def _df(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": dates,
        "open": 10.0, "close": 10.0, "high": 10.1, "low": 9.9,
        "volume": 1e6, "amount": 1e8,
    })


def test_skips_when_cache_reaches_delist_date(monkeypatch, tmp_path):
    """缓存已覆盖到退市日附近（容差内）→ 不回源，避免每天重复抓。"""
    from scripts import backfill_delisted as bd

    cache = KlineCache(tmp_path)
    cache.save("600355", _df(["2026-03-02", "2026-04-03"]))
    monkeypatch.setattr(bd, "kline_cache", cache)

    def boom(*args, **kwargs):
        raise AssertionError("已覆盖的股票不该回源")

    monkeypatch.setattr(bd, "_fetch", boom)
    res = bd._one({"code": "600355", "name": "x", "delist_date": "2026-04-03"}, 1023, False)
    assert res["status"] == "skip"
    assert res["last"] == "2026-04-03"


def test_backfills_and_drops_bars_after_delist(monkeypatch, tmp_path):
    """未覆盖 → 回源写入，且丢掉退市日之后的行（接口偶发脏数据）。"""
    from scripts import backfill_delisted as bd

    cache = KlineCache(tmp_path)
    monkeypatch.setattr(bd, "kline_cache", cache)
    monkeypatch.setattr(
        bd, "_fetch",
        lambda code, datalen: _df(["2023-01-03", "2026-04-03", "2026-04-06"]),
    )

    res = bd._one({"code": "600355", "name": "x", "delist_date": "2026-04-03"}, 1023, False)
    assert res["status"] == "ok"
    assert res["bars"] == 2
    saved = cache.get("600355")
    assert list(saved["date"]) == ["2023-01-03", "2026-04-03"]


def test_dry_run_does_not_write(monkeypatch, tmp_path):
    """--dry-run 只统计，不落盘。"""
    from scripts import backfill_delisted as bd

    cache = KlineCache(tmp_path)
    monkeypatch.setattr(bd, "kline_cache", cache)
    monkeypatch.setattr(bd, "_fetch", lambda code, datalen: _df(["2026-04-03"]))

    res = bd._one({"code": "600355", "name": "x", "delist_date": "2026-04-03"}, 1023, True)
    assert res["status"] == "ok"
    assert cache.get("600355") is None


def test_empty_when_all_bars_after_delist(monkeypatch, tmp_path):
    """回源结果全在退市日之后 → 判 empty 而不是写入脏数据。"""
    from scripts import backfill_delisted as bd

    cache = KlineCache(tmp_path)
    monkeypatch.setattr(bd, "kline_cache", cache)
    monkeypatch.setattr(bd, "_fetch", lambda code, datalen: _df(["2026-05-06"]))

    res = bd._one({"code": "600355", "name": "x", "delist_date": "2026-04-03"}, 1023, False)
    assert res["status"] == "empty"
    assert cache.get("600355") is None
