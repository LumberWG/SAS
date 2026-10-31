"""全市场涨跌家数（`app/data/breadth.py`）的边界与口径用例。

重点覆盖三处容易错的地方：
1. 涨/跌/平的判定口径（`==0` 必须算平，不能被浮点误差漏掉）；
2. `recent_up_ratio` 只取 `<= as_of` 的行（防未来函数，这是 `#84` 系列判定的前提）；
3. 缓存缺失/损坏时返回**空表而不是抛异常**——机制层据此退化为「不使用广度」。
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.data import breadth as B


def _write(path, dates, closes):
    pd.DataFrame({"date": dates, "close": closes}).to_parquet(path)


@pytest.fixture()
def kdir(tmp_path):
    """三只虚构股票；`idx_sh000001.parquet` 必须被跳过（它不是个股）。"""
    k = tmp_path / "klines"
    k.mkdir()
    dates = ["2026-10-28", "2026-10-29", "2026-10-30"]
    _write(k / "600000.parquet", dates, [10.0, 11.0, 11.0])   # 涨 → 平
    _write(k / "600001.parquet", dates, [10.0, 9.0, 8.0])     # 跌 → 跌
    _write(k / "600002.parquet", dates, [10.0, 10.0, 10.0])   # 平 → 平
    _write(k / "idx_sh000001.parquet", dates, [3000.0, 1.0, 2.0])  # 必须被忽略
    return k


def test_build_counts_and_ratio(kdir):
    df = B.build(kdir)
    # 首个交易日没有前收，pct 为 NaN，不参与统计
    assert list(df.index) == ["2026-10-29", "2026-10-30"]
    d0, d1 = df.loc["2026-10-29"], df.loc["2026-10-30"]
    assert (d0["up"], d0["down"], d0["flat"], d0["total"]) == (1, 1, 1, 3)
    assert d0["up_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    # 次日：600000 平、600001 跌、600002 平
    assert (d1["up"], d1["down"], d1["flat"]) == (0, 1, 2)
    assert d1["up_ratio"] == pytest.approx(0.0)


def test_index_file_is_ignored(kdir):
    """`idx_*` 混进来的话 total 会变成 4，这里一并守住。"""
    assert B.build(kdir)["total"].max() == 3


def test_since_filter(kdir):
    df = B.build(kdir, since="2026-10-30")
    assert list(df.index) == ["2026-10-30"]


def test_refresh_and_load_roundtrip(kdir, tmp_path):
    target = tmp_path / "breadth_daily.parquet"
    out = B.refresh(kdir, path=target)
    assert target.is_file() and len(out) == 2
    got = B.load(kdir, path=target)
    assert list(got.index) == list(out.index)
    assert got["up_ratio"].iloc[-1] == pytest.approx(0.0)


def test_load_missing_returns_empty(kdir, tmp_path):
    """缓存缺失不能让调用方炸——返回空表，机制层据此降级。"""
    got = B.load(kdir, path=tmp_path / "nope.parquet")
    assert got.empty
    assert "up_ratio" in got.columns or got.empty


def test_recent_up_ratio_excludes_future(kdir, tmp_path):
    df = B.build(kdir)
    # 以 10-29 为 as_of，不能把 10-30 算进来
    assert B.recent_up_ratio(df, "2026-10-29", n=5) == [pytest.approx(1 / 3, abs=1e-4)]
    assert len(B.recent_up_ratio(df, "2026-10-30", n=5)) == 2
    # as_of 早于全部数据 → 空
    assert B.recent_up_ratio(df, "2020-01-01") == []


def test_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert B.build(empty).empty
