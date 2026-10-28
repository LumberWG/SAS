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
    # 用 ValueError 而不是 SystemExit：调度器要能 except Exception 接住（见 scheduler.shadow_eval）
    with pytest.raises(ValueError):
        _check_start({"start": "2026-11-02"}, "2026-11-05", "p")


def test_run_shadow_merges_snapshots_idempotently(tmp_path, monkeypatch):
    """调度器每天调用的入口：同一天重跑覆盖、不重复追加；起点被改则拒绝。"""
    import scripts.shadow as shadow

    monkeypatch.setattr(shadow, "_shadow_path", lambda tag: str(tmp_path / f"shadow_{tag}.json"))
    monkeypatch.setattr(shadow, "_run_sync", lambda params: _result(6.0, 2.0))
    quiet = lambda msg: None  # noqa: E731

    data = shadow.run_shadow(start="2026-11-02", end="2026-11-06", tag="t", log=quiet)
    assert data["start"] == "2026-11-02"
    assert data["universe"] == 300
    assert data["profiles"]["default"]["snapshots"][0]["excess"] == pytest.approx(4.0)

    # 同日重跑：覆盖而非追加（调度器可能因重启重复触发）
    shadow.run_shadow(start="2026-11-02", end="2026-11-06", tag="t", log=quiet)
    saved = shadow._load(shadow._shadow_path("t"))
    assert len(saved["profiles"]["default"]["snapshots"]) == 1

    # 起点冻结：换起点必须报错，否则曲线断裂
    with pytest.raises(ValueError):
        shadow.run_shadow(start="2026-11-05", end="2026-11-06", tag="t", log=quiet)


def test_run_shadow_rejects_unknown_profile(tmp_path, monkeypatch):
    import scripts.shadow as shadow

    monkeypatch.setattr(shadow, "_shadow_path", lambda tag: str(tmp_path / f"shadow_{tag}.json"))
    with pytest.raises(ValueError):
        shadow.run_shadow(start="2026-11-02", tag="t", profiles="no_such", log=lambda m: None)


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
