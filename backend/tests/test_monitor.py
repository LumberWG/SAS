"""监控调度器单元测试：交易时段判断、异动检测、通知管道。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.monitor.scheduler import (
    MarketScheduler,
    is_trading_day,
    is_trading_hours,
)
from app.notify.channels import ConsoleNotifier, NotificationManager


def trading_time(weekday: int, hour: int, minute: int) -> datetime:
    """构造指定星期与时刻（2026-10-19 是周一）。"""
    base = datetime(2026, 10, 19)  # 周一
    day = base + pd.Timedelta(days=weekday - 1)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


class TestTradingHours:
    def test_mid_session(self):
        assert is_trading_hours(trading_time(1, 10, 0)) is True
        assert is_trading_hours(trading_time(1, 14, 30)) is True

    def test_lunch_break(self):
        assert is_trading_hours(trading_time(1, 12, 0)) is False
        assert is_trading_hours(trading_time(1, 9, 0)) is False

    def test_boundaries(self):
        assert is_trading_hours(trading_time(1, 9, 30)) is True
        assert is_trading_hours(trading_time(1, 11, 30)) is True
        assert is_trading_hours(trading_time(1, 15, 0)) is True
        assert is_trading_hours(trading_time(1, 15, 30)) is False

    def test_weekend(self):
        assert is_trading_hours(trading_time(6, 10, 0)) is False
        assert is_trading_hours(trading_time(7, 10, 0)) is False

    def test_trading_day(self):
        assert is_trading_day(trading_time(1, 10, 0)) is True
        assert is_trading_day(trading_time(6, 10, 0)) is False


def make_snapshot() -> pd.DataFrame:
    """构造含各类异动的合成快照。"""
    return pd.DataFrame([
        # 涨停
        {"code": "600001", "name": "涨停股", "price": 12.0, "pct_change": 9.95, "volume_ratio": 2.0},
        # 跌停
        {"code": "600002", "name": "跌停股", "price": 8.0, "pct_change": -9.9, "volume_ratio": 1.0},
        # 放量突破（量比 5 + 涨 6%）
        {"code": "600003", "name": "放量股", "price": 15.0, "pct_change": 6.2, "volume_ratio": 5.1},
        # 急涨（无放量）
        {"code": "600004", "name": "急涨股", "price": 20.0, "pct_change": 7.5, "volume_ratio": 1.2},
        # 普通波动
        {"code": "600005", "name": "普通股", "price": 10.0, "pct_change": 1.5, "volume_ratio": 1.0},
        # NaN 涨跌幅（停牌）
        {"code": "600006", "name": "停牌股", "price": float("nan"), "pct_change": float("nan"), "volume_ratio": float("nan")},
    ])


class FakeNotifier:
    """记录通知内容，避免真实发送。"""

    def __init__(self):
        self.calls = []

    async def notify(self, title: str, content: str) -> None:
        self.calls.append((title, content))


class TestAlertDetection:
    def test_detect_all_types(self, monkeypatch):
        fake_notifier = FakeNotifier()
        monkeypatch.setattr(
            "app.monitor.scheduler.snapshot_store",
            SimpleNamespace(get_snapshot=lambda: make_snapshot()),
        )
        monkeypatch.setattr("app.monitor.scheduler.notifier", fake_notifier)

        sched = MarketScheduler()
        alerts = asyncio.run(sched.detect_alerts())

        types = {a["type"] for a in alerts}
        assert types == {"涨停", "跌停", "放量突破", "急涨"}
        # 通知已发送
        assert len(fake_notifier.calls) == 1

    def test_dedup_same_day(self, monkeypatch):
        monkeypatch.setattr(
            "app.monitor.scheduler.snapshot_store",
            SimpleNamespace(get_snapshot=lambda: make_snapshot()),
        )
        monkeypatch.setattr("app.monitor.scheduler.notifier", FakeNotifier())

        sched = MarketScheduler()
        first = asyncio.run(sched.detect_alerts())
        second = asyncio.run(sched.detect_alerts())
        assert len(first) == 4
        assert len(second) == 0  # 同日同股同类型去重

    def test_alert_priority(self, monkeypatch):
        """涨停股不应同时报「急涨」（优先级取最高）。"""
        monkeypatch.setattr(
            "app.monitor.scheduler.snapshot_store",
            SimpleNamespace(get_snapshot=lambda: make_snapshot()),
        )
        monkeypatch.setattr("app.monitor.scheduler.notifier", FakeNotifier())

        alerts = asyncio.run(MarketScheduler().detect_alerts())
        limit_up = [a for a in alerts if a["code"] == "600001"]
        assert len(limit_up) == 1
        assert limit_up[0]["type"] == "涨停"

    def test_nan_pct_skipped(self, monkeypatch):
        """停牌股（NaN 涨跌幅）不应触发异动。"""
        monkeypatch.setattr(
            "app.monitor.scheduler.snapshot_store",
            SimpleNamespace(get_snapshot=lambda: make_snapshot()),
        )
        monkeypatch.setattr("app.monitor.scheduler.notifier", FakeNotifier())

        alerts = asyncio.run(MarketScheduler().detect_alerts())
        assert all(a["code"] != "600006" for a in alerts)


class TestNotifiers:
    @pytest.mark.asyncio
    async def test_console_notifier(self):
        ok = await ConsoleNotifier().send("测试", "内容")
        assert ok is True

    @pytest.mark.asyncio
    async def test_manager_fans_out(self):
        manager = NotificationManager()
        # 未配置 Webhook 时仅控制台渠道
        assert any(isinstance(c, ConsoleNotifier) for c in manager.channels)
        # 并行发送不应抛异常
        await manager.notify("测试标题", "测试内容")

    @pytest.mark.asyncio
    async def test_webhook_failure_isolated(self, monkeypatch):
        """Webhook 失败不影响其他渠道（管理器内部容错）。"""
        manager = NotificationManager()
        for channel in manager.channels:
            if isinstance(channel, ConsoleNotifier):
                continue
            async def failing_send(*args, **kwargs):
                return False
            monkeypatch.setattr(channel, "send", failing_send)
        await manager.notify("标题", "内容")  # 不抛异常即通过


class TestShadowJob:
    """影子账户任务：只有配置了 `shadow_start` 才注册（避免空跑/无起点报错）。"""

    @staticmethod
    def _job_ids(monkeypatch, shadow_start: str) -> set[str]:
        import app.monitor.scheduler as sched_mod

        settings = SimpleNamespace(
            scheduler_enabled=True,
            scheduler_timezone="Asia/Shanghai",
            intraday_analysis_interval=1,
            paper_tick_interval=5,
            shadow_start=shadow_start,
            shadow_tag="live",
            shadow_universe=300,
            shadow_profiles="",
        )
        monkeypatch.setattr(sched_mod, "get_settings", lambda: settings)

        async def fake_warmup():
            return {}

        async def runner() -> set[str]:
            sched = MarketScheduler()
            sched.warmup_klines = fake_warmup   # 别在测试里真去拉 5500 只 K 线
            sched.start()
            try:
                return {job.id for job in sched.scheduler.get_jobs()}
            finally:
                sched.shutdown()

        return asyncio.run(runner())

    def test_registered_when_start_configured(self, monkeypatch):
        assert "shadow_eval" in self._job_ids(monkeypatch, "2026-11-02")

    def test_not_registered_without_start(self, monkeypatch):
        assert "shadow_eval" not in self._job_ids(monkeypatch, "")
