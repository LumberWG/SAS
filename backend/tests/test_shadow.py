# -*- coding: utf-8 -*-
"""影子账户脚本的纯函数测试（不起回测、不联网）。

影子账户是「冻结参数 + 逐日重跑」的前向验证工具（见 BACKTEST_LOG 记录 #17），
这里锁住三件事：快照字段抽取、同日幂等、起点冻结。
"""
import pytest

from scripts.shadow import _check_start, _snapshot, report


def _result(total=5.0, bench=1.0):
    return {
        "metrics": {
            "total_return": total,
            "benchmark_return": bench,
            "max_drawdown": -8.5,
            "trade_count": 12,
            "win_rate": 33.3,
            "final_equity": 105000.0,
            "open_positions": 2,
        },
        "equity_curve": [{"date": "2026-10-27"}, {"date": "2026-10-28"}],
        "positions": {
            "600621": {"name": "华鑫股份", "shares": 600, "cost": 14.9, "stop": 13.11},
        },
    }


def test_snapshot_fields():
    snap = _snapshot("default", _result(total=5.32, bench=1.1), "2026-10-28")
    assert snap["as_of"] == "2026-10-28"
    assert snap["excess"] == pytest.approx(4.22)
    assert snap["open_positions"] == 2
    assert snap["positions"][0]["code"] == "600621"
    assert snap["positions"][0]["stop"] == pytest.approx(13.11)


def test_start_is_frozen():
    _check_start({"start": "2026-11-02"}, "2026-11-02", "p")  # 同起点：放行
    _check_start({}, "2026-11-02", "p")                        # 新开：放行
    with pytest.raises(SystemExit):
        _check_start({"start": "2026-11-02"}, "2026-11-05", "p")


def test_report_compares_against_default(capsys):
    data = {
        "tag": "t",
        "start": "2026-11-02",
        "profiles": {
            "default": {"snapshots": [_snapshot("default", _result(5.0, 1.0), "d")]},
            "e30_a15": {"snapshots": [_snapshot("e30_a15", _result(9.0, 1.0), "d")]},
        },
    }
    report(data)
    out = capsys.readouterr().out
    assert "影子账户 t" in out
    assert "e30_a15" in out and "default" in out
    # 9.0-1.0=8.0 对 5.0-1.0=4.0，差值 +4 个点
    assert "+4.00 个点" in out


def test_report_empty(capsys):
    report({"tag": "t", "profiles": {}})
    assert "还没有快照" in capsys.readouterr().out
