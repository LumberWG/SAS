"""实盘对账脚本：基准口径、区间累乘与判读规则。"""
import pandas as pd
import pytest

from scripts import live_review as lr


def _raw(closes: dict[str, dict[str, float]],
         amounts: dict[str, dict[str, float]]) -> dict[str, pd.DataFrame]:
    """构造 ``live_review._load_raw`` 同形状的 K 线字典。"""
    out = {}
    for code, series in closes.items():
        dates = sorted(series)
        out[code] = pd.DataFrame({
            "date": dates,
            "close": [series[d] for d in dates],
            "amount": [amounts[code][d] for d in dates],
        })
    return out


def test_bench_daily_returns_lags_one_day():
    """基准用**前一交易日**的池子——当日成交额排名收盘后才知道（防未来函数）。"""
    dates = ["2026-11-02", "2026-11-03", "2026-11-04"]
    # 11-03 当日成交额只有 A 排第一，但 11-03 的收益要用 11-02 的池子（A+B）
    universe = {"2026-11-02": ["A", "B"], "2026-11-03": ["A"]}
    close_index = {
        "A": {"2026-11-02": 10.0, "2026-11-03": 11.0, "2026-11-04": 11.0},
        "B": {"2026-11-02": 20.0, "2026-11-03": 20.0, "2026-11-04": 22.0},
    }

    daily = lr.bench_daily_returns(dates, universe, close_index)

    # 11-03：(+10% + 0%)/2 = +5%
    assert daily["2026-11-03"] == pytest.approx(0.05)
    # 11-04：池子换成 11-03 的 [A] → 只有 A 的 0%
    assert daily["2026-11-04"] == pytest.approx(0.0)
    # 第一天没有前一日池子，不应产出
    assert "2026-11-02" not in daily


def test_verdict_follows_frozen_rules():
    """判读区间由 #70 §6 冻结：−1pp / +5pp 上下界，21 天以下不下结论。"""
    assert "样本仅" in lr.verdict(9.0, 5)
    assert "查执行价" in lr.verdict(-2.0, 21)
    assert "运气" in lr.verdict(6.0, 21)
    assert "与回测一致" in lr.verdict(2.0, 21)


def test_reconcile_excess_and_gap_tolerance():
    """两日对账：策略 +1%/日、基准 +5%/日 → 超额 −8.25pp（复利）。"""
    closes = {
        "600000": {"2026-11-02": 10.0, "2026-11-03": 11.0, "2026-11-04": 11.0},
        "600001": {"2026-11-02": 20.0, "2026-11-03": 20.0, "2026-11-04": 22.0},
    }
    amounts = {
        "600000": {"2026-11-02": 1000.0, "2026-11-03": 900.0, "2026-11-04": 900.0},
        "600001": {"2026-11-02": 500.0, "2026-11-03": 800.0, "2026-11-04": 800.0},
    }
    raw = _raw(closes, amounts)
    dates = lr._trading_dates(raw)
    close_index = lr._close_index(raw)
    equity = {"2026-11-02": 100000.0, "2026-11-03": 101000.0, "2026-11-04": 102000.0}

    report = lr.reconcile(equity, dates, (2,), raw, close_index,
                          {"2026-11-03": {"weak": True, "exposure": 0.3}})

    data = report["per_pool"][2]
    assert data["strategy_total"] == pytest.approx(0.02)
    assert data["benchmark_total"] == pytest.approx(0.1025)
    assert data["excess_pp"] == pytest.approx(-8.25, abs=0.01)
    assert data["trading_days"] == 2
    assert "样本仅" in data["verdict"]

    rows = {r["date"]: r for r in report["rows"]}
    assert rows["2026-11-03"]["excess_pp"] == pytest.approx(-4.0, abs=0.01)
    assert rows["2026-11-03"]["state"]["weak"] is True


def test_reconcile_carries_benchmark_across_equity_gap():
    """净值快照断档时，基准要把中间缺失的交易日照样累乘，不能只看首尾两天。"""
    closes = {
        "600000": {"2026-11-02": 10.0, "2026-11-03": 11.0, "2026-11-04": 11.0},
        "600001": {"2026-11-02": 20.0, "2026-11-03": 20.0, "2026-11-04": 22.0},
    }
    amounts = {
        "600000": {"2026-11-02": 1000.0, "2026-11-03": 900.0, "2026-11-04": 900.0},
        "600001": {"2026-11-02": 500.0, "2026-11-03": 800.0, "2026-11-04": 800.0},
    }
    raw = _raw(closes, amounts)
    dates = lr._trading_dates(raw)
    close_index = lr._close_index(raw)
    # 缺 11-03 的快照
    equity = {"2026-11-02": 100000.0, "2026-11-04": 102000.0}

    report = lr.reconcile(equity, dates, (2,), raw, close_index)

    data = report["per_pool"][2]
    # 仍然累乘了 11-03 与 11-04 两个交易日 → 1.05 × 1.05 − 1
    assert data["benchmark_total"] == pytest.approx(0.1025)
    assert data["strategy_total"] == pytest.approx(0.02)
    assert len(report["rows"]) == 1


def test_reconcile_requires_two_snapshots():
    report = lr.reconcile({"2026-11-02": 100000.0}, ["2026-11-02"], (2,), {}, {})
    assert "不足 2 天" in report["error"]
