"""换手率补齐测试：腾讯批量行情解析 + 「成交量 ÷ 流通股本」反推。"""
import pandas as pd
import pytest

from app.data import cache as cache_mod
from app.data.cache import KlineCache, fill_turnover, float_shares_series
from app.data.fallback import tencent_quotes


def _tencent_line(
    code: str = "600519", name: str = "贵州茅台", price: str = "1348.00"
) -> str:
    """按腾讯 88 字段格式拼一行（只填用例关心的下标）。"""
    fields = [""] * 88
    values = {
        1: name, 2: code, 3: price, 4: "1349.98", 5: "1343.88", 6: "16961",
        31: "-1.98", 32: "-0.15", 33: "1352.90", 34: "1338.01",
        37: "228608", 38: "0.14", 44: "16868.48", 45: "16868.48",
        72: "1250081601", 73: "1250081601",
    }
    for idx, val in values.items():
        fields[idx] = str(val)
    prefix = "sh" if code.startswith("6") else "sz"
    return f'v_{prefix}{code}="{"~".join(fields)}";'


class TestTencentQuotes:
    def test_parse_fields_and_units(self, monkeypatch):
        # 腾讯原始单位：成交额「万元」、市值「亿元」，返回值统一为「元」
        monkeypatch.setattr(
            "app.data.fallback._get_text",
            lambda url, params=None, timeout=15.0: _tencent_line("600519"),
        )
        df = tencent_quotes(["sh600519"])
        assert len(df) == 1
        row = df.iloc[0]
        assert row["code"] == "600519"
        assert row["price"] == pytest.approx(1348.0)
        assert row["pct_change"] == pytest.approx(-0.15)
        assert row["volume"] == pytest.approx(16961)          # 手
        assert row["amount"] == pytest.approx(228608 * 1e4)   # 万元 → 元
        assert row["total_mv"] == pytest.approx(16868.48 * 1e8)
        assert row["float_mv"] == pytest.approx(16868.48 * 1e8)
        assert row["float_shares"] == pytest.approx(1250081601)

    def test_skip_suspended_and_invalid(self, monkeypatch):
        text = (
            _tencent_line("600519", price="0.00")  # 停牌/未开盘
            + _tencent_line("000002", name="万科A", price="3.38")
            + 'v_pv_none_match="1";'
        )
        monkeypatch.setattr(
            "app.data.fallback._get_text",
            lambda url, params=None, timeout=15.0: text,
        )
        df = tencent_quotes(["sh600519", "sz000002", "sz999999"])
        assert df["code"].tolist() == ["000002"]

    def test_empty_when_all_invalid(self, monkeypatch):
        monkeypatch.setattr(
            "app.data.fallback._get_text",
            lambda url, params=None, timeout=15.0: 'v_pv_none_match="1";',
        )
        df = tencent_quotes(["sz999999"])
        assert df.empty
        assert "turnover_rate" in df.columns


def _kline(rows: list[tuple[str, float, float | None]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["date", "volume", "turnover_rate"])


class TestFillTurnover:
    def test_derive_from_history(self):
        # 流通股本 1 亿股：volume(手) × 10000 / 100000000 = 换手率(%)
        df = _kline([
            ("2026-01-05", 10000.0, 0.10),
            ("2026-01-06", 20000.0, 0.20),
            ("2026-01-07", 30000.0, 0.30),
            ("2026-01-08", 40000.0, None),
            ("2026-01-09", 50000.0, None),
        ])
        out, filled, bad = fill_turnover(df)
        assert filled == 2
        assert bad == 0
        assert out["turnover_rate"].iloc[3] == pytest.approx(0.40, abs=1e-6)
        assert out["turnover_rate"].iloc[4] == pytest.approx(0.50, abs=1e-6)
        # 原始值不被覆盖
        assert out["turnover_rate"].iloc[0] == pytest.approx(0.10)

    def test_suspended_row_left_empty(self):
        df = _kline([
            ("2026-01-05", 10000.0, 0.10),
            ("2026-01-06", 0.0, None),  # 停牌：成交量 0，不该硬算出 0%
        ])
        out, filled, _ = fill_turnover(df)
        assert filled == 0
        assert pd.isna(out["turnover_rate"].iloc[1])

    def test_fallback_shares_when_no_observation(self):
        df = _kline([
            ("2026-01-05", 10000.0, None),
            ("2026-01-06", 20000.0, None),
        ])
        assert float_shares_series(df) is None
        # 10 亿股：10000 手 = 100 万股 → 0.10%
        out, filled, _ = fill_turnover(df, fallback_shares=1e9)
        assert filled == 2
        assert out["turnover_rate"].iloc[0] == pytest.approx(0.10)

    def test_missing_columns_noop(self):
        df = pd.DataFrame({"date": ["2026-01-05"], "close": [10.0]})
        out, filled, bad = fill_turnover(df)
        assert (filled, bad) == (0, 0)
        assert out.equals(df)

    def test_implausible_value_dropped(self):
        # 反推出的换手率 >100% 视为股本取错（新股/退市残留），留空而非写脏值
        df = _kline([
            ("2026-01-05", 100.0, 1.00),   # 股本 100 万股
            ("2026-01-06", 100.0, 1.00),
            ("2026-01-07", 1e9, None),     # 成交量放大 1 万倍 → 反推 1e7%
        ])
        out, filled, bad = fill_turnover(df)
        assert filled == 0
        assert bad == 1
        assert pd.isna(out["turnover_rate"].iloc[2])


class TestKlineCacheSave:
    def test_append_fills_turnover(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache_mod, "get_settings", lambda: type(
            "S", (), {"kline_dir": tmp_path, "db_path": tmp_path / "x.db"}
        )())
        kc = KlineCache(kline_dir=tmp_path)
        kc.save("600519", _kline([
            ("2026-01-05", 10000.0, 0.10),
            ("2026-01-06", 20000.0, 0.20),
        ]))
        # 增量写入的行来自腾讯兜底源（无换手率）
        kc.save("600519", _kline([("2026-01-07", 30000.0, None)]))
        saved = kc.get("600519")
        assert saved["turnover_rate"].iloc[-1] == pytest.approx(0.30, abs=1e-6)
