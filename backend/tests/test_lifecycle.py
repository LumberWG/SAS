"""六阶段生命周期相关改动（flow.md 0.5 P0/P1/P2，归口 WORKLOG #191）的单元测试。

覆盖：
- 卖出原因结构化编码（_infer_exit_reason，P1-3）
- 生命周期事件表 insert/list/count（P2-1）
- 短线时间止损（_held_days / _time_stop_hit，P0-2）
- 板块趋势闸门（_buy_allowed，P0-3）
- 统一宏观风险四档 cap 接入（_unified_risk 按日缓存，P1-1）
- 底部反转候选接入交易链（bridge_to_trade，P0-1）
- 手动单护栏（place_order 经 crisis 硬拦，P0-4）

说明：测试套件共用真实 ``sas.db``（与全量 pytest 一致），本文件对写入的行
统一用专属前缀并在 teardown 删除，不污染其他用例。
"""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.data import scope as scope_mod
from app.data.cache import _infer_exit_reason, db
from app.paper import engine as engine_mod
from app.paper.engine import PaperEngine


# ----------------------------------------------------------------------
# P1-3 卖出原因结构化编码
# ----------------------------------------------------------------------
class TestExitReasonInference:
    def test_time_stop(self):
        assert _infer_exit_reason("时间止损(持有6日，上限5日)") == "TIME_STOP"

    def test_stop_loss(self):
        assert _infer_exit_reason("止损触发 @ 9.80") == "STOP_LOSS"

    def test_take_profit(self):
        assert _infer_exit_reason("止盈触发，落袋为安") == "TAKE_PROFIT"

    def test_rotation(self):
        assert _infer_exit_reason("轮动替换：换入更强标的") == "ROTATION"

    def test_offboard(self):
        assert _infer_exit_reason("清非交易板块持仓") == "OFFBOARD"

    def test_eod_trim(self):
        assert _infer_exit_reason("收盘减仓 50%") == "EOD_TRIM"

    def test_t_sell_group(self):
        assert _infer_exit_reason("涨停卖半") == "T_SELL"
        assert _infer_exit_reason("做T卖出") == "T_SELL"
        assert _infer_exit_reason("反弹减仓") == "T_SELL"

    def test_manual(self):
        assert _infer_exit_reason("手动下单卖出") == "MANUAL"

    def test_signal(self):
        assert _infer_exit_reason("卖出信号：动量转弱") == "SIGNAL"

    def test_other(self):
        assert _infer_exit_reason("普通卖出，无明显标签") == "OTHER"

    def test_empty(self):
        assert _infer_exit_reason("") is None
        assert _infer_exit_reason(None) is None


# ----------------------------------------------------------------------
# P2-1 生命周期事件表
# ----------------------------------------------------------------------
class TestLifecycleEvents:
    PREFIX = "LIFECYTEST"

    def teardown_method(self):
        with db._lock, db._connect() as conn:
            conn.execute("DELETE FROM lifecycle_events WHERE code LIKE ?", (self.PREFIX + "%",))

    def test_insert_and_list_desc(self):
        db.insert_lifecycle_event("LIFECYTEST1", "测试一", 1, "初选", "signal_in", detail="x")
        db.insert_lifecycle_event("LIFECYTEST1", "测试一", 4, "入场", "filled", detail="y")
        evs = db.list_lifecycle_events(code="LIFECYTEST1")
        assert len(evs) == 2
        # 倒序：后插入的「入场」应在前
        assert evs[0]["stage"] == 4
        assert evs[1]["stage"] == 1

    def test_count_by_stage(self):
        db.insert_lifecycle_event("LIFECYTEST2", "测试二", 1, "初选", "signal_in")
        db.insert_lifecycle_event("LIFECYTEST2", "测试二", 1, "初选", "signal_in")
        db.insert_lifecycle_event("LIFECYTEST2", "测试二", 2, "入自选", "watch_in")
        stages = {s["stage"]: s["cnt"] for s in db.count_lifecycle_events_by_stage()}
        assert stages[1] >= 2
        assert stages[2] >= 1

    def test_list_global(self):
        db.insert_lifecycle_event("LIFECYTEST3", "测试三", 3, "进交易队列", "queue_in")
        evs = db.list_lifecycle_events(limit=50)
        assert any(e["code"] == "LIFECYTEST3" and e["stage"] == 3 for e in evs)


# ----------------------------------------------------------------------
# P0-2 短线时间止损
# ----------------------------------------------------------------------
class TestTimeStop:
    def test_held_days_inclusive(self):
        assert PaperEngine._held_days("2026-01-01", "2026-01-01") == 1
        assert PaperEngine._held_days("2026-01-01", "2026-01-05") == 5

    def test_time_stop_hit(self, monkeypatch):
        eng = PaperEngine()
        settings = get_settings()
        monkeypatch.setattr(settings, "paper_max_hold_days", 5)
        monkeypatch.setattr(eng, "_first_buy_date", lambda code: "2026-01-01")
        pos = {"code": "600000"}
        # 持有满 5 日触发
        assert eng._time_stop_hit(pos, "2026-01-05") is not None
        # 超过上限触发
        assert eng._time_stop_hit(pos, "2026-01-06") is not None
        # 未到上限不触发
        assert eng._time_stop_hit(pos, "2026-01-04") is None

    def test_time_stop_disabled(self, monkeypatch):
        eng = PaperEngine()
        settings = get_settings()
        monkeypatch.setattr(settings, "paper_max_hold_days", 0)
        monkeypatch.setattr(eng, "_first_buy_date", lambda code: "2026-01-01")
        assert eng._time_stop_hit({"code": "600000"}, "2026-01-10") is None


# ----------------------------------------------------------------------
# P0-3 板块趋势闸门（_buy_allowed）
# ----------------------------------------------------------------------
class TestBoardTrendGate:
    def _eng_with_isolated_gates(self, monkeypatch, *, gate_on: bool, regime_state: str):
        settings = get_settings()
        monkeypatch.setattr(settings, "paper_board_trend_gate", gate_on)
        eng = PaperEngine()
        # 隔离 _buy_allowed 的其它准入检查，只测板块趋势闸门
        monkeypatch.setattr(scope_mod, "is_tradable", lambda code, name: (True, ""))
        monkeypatch.setattr(engine_mod, "large_cap_rule", lambda: (False, 0.0))
        monkeypatch.setattr(engine_mod, "_name_ok", lambda name, code: True)
        monkeypatch.setattr(eng, "_top_amount_codes", lambda: None)
        monkeypatch.setattr(
            engine_mod, "board_regime_for_code",
            lambda code: {"available": True, "state": regime_state},
        )
        return eng

    def test_blocks_downtrend(self, monkeypatch):
        eng = self._eng_with_isolated_gates(monkeypatch, gate_on=True, regime_state="downtrend")
        ok, why = eng._buy_allowed("600000", "测试")
        assert ok is False
        assert "下降通道" in why

    def test_allows_uptrend(self, monkeypatch):
        eng = self._eng_with_isolated_gates(monkeypatch, gate_on=True, regime_state="uptrend")
        ok, _ = eng._buy_allowed("600000", "测试")
        assert ok is True

    def test_gate_off_passes(self, monkeypatch):
        eng = self._eng_with_isolated_gates(monkeypatch, gate_on=False, regime_state="downtrend")
        ok, _ = eng._buy_allowed("600000", "测试")
        assert ok is True


# ----------------------------------------------------------------------
# P1-1 统一宏观风险四档 cap（_unified_risk 按日缓存）
# ----------------------------------------------------------------------
class TestUnifiedRiskCap:
    def test_unified_risk_caches_per_day(self, monkeypatch):
        eng = PaperEngine()
        calls = {"n": 0}

        def fake() -> dict:
            calls["n"] += 1
            return {"tier": "shock", "cap": 0.2}

        monkeypatch.setattr(engine_mod, "get_unified_market_risk", fake)
        r1 = eng._unified_risk()
        r2 = eng._unified_risk()
        assert r1["tier"] == "shock"
        assert r1["cap"] == 0.2
        # 同日调用应命中缓存：返回同一对象、只算一次
        assert r1 is r2
        assert calls["n"] == 1


# ----------------------------------------------------------------------
# P0-1 底部反转候选接入交易链
# ----------------------------------------------------------------------
class TestBottomReversalBridge:
    PREFIX = "BRTEST"

    def teardown_method(self):
        with db._lock, db._connect() as conn:
            conn.execute("DELETE FROM lifecycle_events WHERE code LIKE ?", (self.PREFIX + "%",))
            conn.execute("DELETE FROM signals WHERE code LIKE ?", (self.PREFIX + "%",))
            conn.execute("DELETE FROM watchlist WHERE code LIKE ?", (self.PREFIX + "%",))

    def test_bridge_top_n_signals_watchlist(self, monkeypatch):
        from app.screener import bottom_reversal as br_mod

        fake = {
            "candidates": [
                {"code": "BRTEST1", "name": "底反一", "factor_score": 9.0, "last": 10.5},
                {"code": "BRTEST2", "name": "底反二", "factor_score": 7.0, "last": 8.2},
                {"code": "BRTEST3", "name": "底反三", "factor_score": 5.0, "last": 6.0},
            ]
        }
        monkeypatch.setattr(br_mod, "read_cache", lambda: fake)
        res = br_mod.bridge_to_trade(top_n=2, watch=True, queue=False)
        # 按 factor_score 降序取 Top2
        assert res["signals"] == 2
        assert res["watched"] == 2
        assert res["queued"] == 0
        # 入链候选应写入初选(1) 与 入自选(2) 生命周期事件
        assert any(e["stage"] == 1 for e in db.list_lifecycle_events(code="BRTEST1"))
        assert any(e["stage"] == 2 for e in db.list_lifecycle_events(code="BRTEST1"))
        # 未入选的 BRTEST3 不应出现
        assert db.list_lifecycle_events(code="BRTEST3") == []


# ----------------------------------------------------------------------
# P0-4 手动单护栏（place_order 经 crisis 硬拦，不再越权直落库）
# ----------------------------------------------------------------------
class TestManualBuyGuardrail:
    def teardown_method(self):
        with db._lock, db._connect() as conn:
            conn.execute("DELETE FROM paper_positions WHERE code=?", ("GUARDT",))
            conn.execute("DELETE FROM paper_position_lots WHERE code=?", ("GUARDT",))
            conn.execute("DELETE FROM paper_trades WHERE code=?", ("GUARDT",))
            conn.execute("DELETE FROM lifecycle_events WHERE code=?", ("GUARDT",))

    def test_crisis_block_skips_and_no_position(self, monkeypatch):
        settings = get_settings()
        eng = PaperEngine()
        eng.ensure_account()
        db.update_paper_cash(100000.0)
        # 模拟统一宏观风险 crisis 档：暂停新开仓
        monkeypatch.setattr(engine_mod, "should_block_buy", lambda: (True, "crisis 测试拦截"))
        res = asyncio.run(
            eng.place_order("GUARDT", "护栏测试", "buy", amount=1000, price=10.0)
        )
        assert res.side == "skip"
        assert "入场护栏" in res.reason
        # 不应产生持仓 / 流水 / 入场事件
        assert db.get_paper_position("GUARDT") is None
        assert db.list_lifecycle_events(code="GUARDT") == []
