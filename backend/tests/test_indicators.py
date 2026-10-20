"""指标计算单元测试（合成数据，验证公式正确性）。"""
import numpy as np
import pandas as pd
import pytest

from app.indicators.technical import (
    add_all_indicators, atr, bias, boll, ema, kdj, macd, ma, rsi, volume_ratio,
)


def make_df(closes, highs=None, lows=None, opens=None, volumes=None) -> pd.DataFrame:
    """由收盘价序列构造行情 DataFrame。"""
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": opens if opens is not None else closes,
        "close": closes,
        "high": highs if highs is not None else closes,
        "low": lows if lows is not None else closes,
        "volume": volumes if volumes is not None else [1000.0] * n,
    })


class TestMA:
    def test_ma_simple_average(self):
        # MA5 = 最近5日收盘均值
        df = make_df([1, 2, 3, 4, 5, 6])
        df = ma(df)
        assert df["ma5"].iloc[-1] == pytest.approx((2 + 3 + 4 + 5 + 6) / 5)
        # 第4日（index=3）不足5日窗口，MA5 为 NaN
        assert bool(pd.isna(df["ma5"].iloc[3]))

    def test_ema_recursion(self):
        # EMA: EMA_t = 2/(N+1)*C_t + (N-1)/(N+1)*EMA_{t-1}
        s = pd.Series([10.0, 11.0, 12.0])
        result = ema(s, 2)
        assert result.iloc[0] == pytest.approx(10.0)
        # α = 2/3: EMA_2 = 2/3*11 + 1/3*10 = 10.6667
        assert result.iloc[1] == pytest.approx(10 + 2 / 3 * 1)


class TestMACD:
    def test_dif_equals_ema_diff(self):
        closes = list(np.linspace(10, 20, 60))
        df = make_df(closes)
        df = macd(df)
        expected_dif = ema(df["close"], 12) - ema(df["close"], 26)
        assert np.allclose(df["dif"], expected_dif)
        # MACD柱 = 2*(DIF-DEA)
        assert np.allclose(df["macd_hist"], 2 * (df["dif"] - df["dea"]))


class TestKDJ:
    def test_kdj_range(self):
        rng = np.random.default_rng(42)
        closes = 10 + rng.normal(0, 1, 100).cumsum()
        df = make_df(closes, highs=closes + 1, lows=closes - 1)
        df = kdj(df)
        assert df["kdj_k"].between(0, 100).all()
        assert df["kdj_d"].between(0, 100).all()
        # J = 3K - 2D
        assert np.allclose(df["kdj_j"], 3 * df["kdj_k"] - 2 * df["kdj_d"])

    def test_kdj_flat_line_handles_division(self):
        # 一字板（最高=最低）不应产生 NaN
        df = make_df([10.0] * 30)
        df = kdj(df)
        assert df["kdj_k"].notna().all()


class TestRSI:
    def test_rsi_all_up_is_100(self):
        # 单边上涨：没有下跌，RSI 应为 100
        df = make_df(list(range(1, 51)))
        df = rsi(df)
        assert df["rsi6"].iloc[-1] == pytest.approx(100.0)

    def test_rsi_range(self):
        rng = np.random.default_rng(0)
        closes = 10 + rng.normal(0, 1, 100).cumsum()
        df = make_df(closes)
        df = rsi(df)
        assert df["rsi12"].dropna().between(0, 100).all()


class TestBOLL:
    def test_boll_bands_order(self):
        rng = np.random.default_rng(1)
        closes = 10 + rng.normal(0, 1, 100).cumsum()
        df = make_df(closes)
        df = boll(df)
        # 上轨 > 中轨 > 下轨（剔除前19日窗口未填满的 NaN）
        assert (df["boll_up"].dropna() > df["boll_mid"].dropna()).all()
        assert (df["boll_mid"].dropna() > df["boll_dn"].dropna()).all()
        # 中轨 = MA20
        assert np.allclose(df["boll_mid"].dropna(),
                           df["close"].rolling(20).mean().dropna())


class TestATR:
    def test_atr_positive(self):
        rng = np.random.default_rng(2)
        closes = 10 + rng.normal(0, 1, 60).cumsum()
        df = make_df(closes, highs=closes + 0.5, lows=closes - 0.5)
        df = atr(df)
        assert (df["atr"].dropna() > 0).all()


class TestVolumeRatio:
    def test_volume_ratio(self):
        df = make_df([10.0] * 10, volumes=[100] * 9 + [300])
        df = volume_ratio(df)
        # 最后一日量比 = 当日量 / 近5日均量 = 300 / ((100×4+300)/5)
        assert df["vol_ratio"].iloc[-1] == pytest.approx(300 / 140)


class TestBias:
    def test_bias_above_ma_positive(self):
        # 价格高于均线 → 正乖离
        df = make_df([10, 10, 10, 10, 10, 11])
        df = bias(df)
        assert df["bias6"].iloc[-1] > 0


class TestAddAll:
    def test_all_columns_present(self):
        df = make_df(list(np.linspace(10, 15, 80)))
        df = add_all_indicators(df)
        for col in ("ma5", "ma20", "dif", "dea", "macd_hist", "kdj_k",
                    "boll_up", "rsi6", "bias6", "atr", "vol_ratio"):
            assert col in df.columns
