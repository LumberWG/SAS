"""回测记录落库测试：参数 / 过程数据 / 结果全部持久化，列表接口不带大字段。

背景：``backtest_runs`` 原来只存 ``strategy/区间/params/metrics/equity_curve``，
逐笔成交、期末持仓、亏损归因都只活在内存任务表里（TTL 1 小时），
过期或重启后无法复盘「这一笔到底是止损价成交还是开盘价成交」。
"""
import sqlite3

import pytest

from app.data.cache import Database


def _run_payload() -> dict:
    return {
        "strategy": "全策略流水线",
        "start_date": "2026-06-01",
        "end_date": "2026-10-20",
        "params": {"stop_loss": 0.08, "take_profit": 0.2, "max_positions": 10},
        "metrics": {"total_return": 12.3, "trade_count": 2, "max_drawdown": -8.1},
        "equity_curve": [
            {"date": "2026-06-01", "equity": 100000, "benchmark": 100000,
             "cash": 100000, "positions": 0},
            {"date": "2026-06-02", "equity": 101000, "benchmark": 100100,
             "cash": 40000, "positions": 2},
        ],
        "positions": {"600519": {"shares": 100, "cost": 1680.0, "score": 42.0}},
        "review": {"summary": "止损为主", "reasons": [], "suggestions": []},
        "job_id": "abc123",
        "elapsed": 42.5,
        "trades": [
            {"code": "600519", "name": "贵州茅台", "buy_date": "2026-06-02",
             "sell_date": "2026-06-10", "buy_price": 1680.0, "sell_price": 1814.4,
             "shares": 100, "amount": 181440.0, "fee": 231.44, "pnl": 13178.56,
             "pnl_pct": 7.84, "reason": "止盈", "score": 42.0},
            {"code": "000001", "name": "平安银行", "buy_date": "2026-06-03",
             "sell_date": "2026-06-12", "buy_price": 11.0, "sell_price": 10.12,
             "shares": 1000, "amount": 10120.0, "fee": 15.12, "pnl": -895.12,
             "pnl_pct": -8.14, "reason": "止损", "score": 28.0},
        ],
    }


@pytest.fixture()
def bt_db(tmp_path):
    return Database(tmp_path / "bt.db")


class TestBacktestPersistence:
    def test_roundtrip_keeps_params_process_and_result(self, bt_db):
        run_id = bt_db.save_backtest(_run_payload())
        got = bt_db.get_backtest(run_id)
        assert got["params"]["take_profit"] == 0.2
        assert got["metrics"]["trade_count"] == 2
        # 过程数据：逐日权益 / 基准 / 现金 / 持仓数
        assert [p["date"] for p in got["equity_curve"]] == ["2026-06-01", "2026-06-02"]
        assert got["equity_curve"][1]["positions"] == 2
        # 结果：期末持仓 / 归因 / 任务追溯
        assert got["positions"]["600519"]["score"] == 42.0
        assert got["review"]["summary"] == "止损为主"
        assert got["job_id"] == "abc123"
        assert got["elapsed"] == 42.5

    def test_trades_are_queryable_rows(self, bt_db):
        """逐笔明细落在 backtest_trades，可直接按卖出原因做 SQL 聚合。"""
        run_id = bt_db.save_backtest(_run_payload())
        got = bt_db.get_backtest(run_id)
        assert [t["reason"] for t in got["trades"]] == ["止盈", "止损"]
        assert got["trades"][1]["pnl_pct"] == -8.14

        with sqlite3.connect(bt_db.path) as conn:
            rows = conn.execute(
                "SELECT reason, COUNT(*) FROM backtest_trades"
                " WHERE run_id=? GROUP BY reason", (run_id,)
            ).fetchall()
        assert dict(rows) == {"止盈": 1, "止损": 1}

    def test_list_returns_summary_only(self, bt_db):
        run_id = bt_db.save_backtest(_run_payload())
        runs = bt_db.list_backtests()
        assert len(runs) == 1
        item = runs[0]
        assert item["id"] == run_id
        assert item["trade_count"] == 2
        assert item["metrics"]["total_return"] == 12.3
        # 大字段不随列表返回（50 条 × 上万字符会凭空多出几百 KB）
        for heavy in ("equity_curve", "positions", "review", "trades"):
            assert heavy not in item

    def test_run_without_trades_still_saved(self, bt_db):
        payload = _run_payload()
        payload["trades"] = []
        run_id = bt_db.save_backtest(payload)
        got = bt_db.get_backtest(run_id)
        assert got["trades"] == []
        assert bt_db.list_backtests()[0]["trade_count"] == 0


class TestLegacyMigration:
    def test_old_table_gets_new_columns(self, tmp_path):
        """老库（只有 7 列）启动后应自动补 positions/review/job_id/elapsed。"""
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE backtest_runs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL,"
                " start_date TEXT, end_date TEXT, params TEXT, metrics TEXT,"
                " equity_curve TEXT, created_at TEXT NOT NULL);"
                "INSERT INTO backtest_runs(strategy, start_date, end_date, params,"
                " metrics, equity_curve, created_at) VALUES"
                "('全策略流水线','2026-01-01','2026-02-01','{}','{}','[]',"
                "'2026-02-01 00:00:00');"
            )

        db = Database(path)
        with sqlite3.connect(path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
        assert {"positions", "review", "job_id", "elapsed"} <= cols

        # 历史记录这几列为 NULL：按空值降级，不回填、不报错
        old = db.get_backtest(1)
        assert old is not None
        assert old["trades"] == []
        assert old["positions"] == {}
        assert old["review"] == {}
