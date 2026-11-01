"""并行模拟账本：同一份信号、不同处置参数各自记账。

为什么需要：验证一组交易参数（止损档 / 仓位档 / 弱市档）过去只能
「改配置 → 跑 21 个交易日 → 看结果 → 再改下一组」，串行试错的代价是一个月
一个方案。这里让 N 个账本**同时**跑在真实 tick 上，各自独立持仓与净值，
一天就能看出分化。

与影子账户（``scripts/shadow.py``）的分工：

- 影子账户：日频，用回测引擎把「起点 ~ 今天」整段重跑，**能**复现选股侧差异
  （ML 打分、评分反转、策略子集），代价是一次跑几分钟且只到收盘；
- 并行账本：tick 级，复用线上撮合引擎，**只能**复现处置侧差异（止损/止盈/
  仓位/持仓数/弱市档），但盘中就能看到净值分化与每笔成交。

因此本模块只接受能换算到 ``paper_*`` 的方案参数；含选股侧参数的方案
（如 ``rev_inv`` / ``default_ml``）会被**明确拒绝**而不是静默降级 ——
降级跑出来的是一条和方案名不符的曲线，比不跑更误导（见 AGENTS.md 口径红线）。
"""
from __future__ import annotations

import json
from typing import Any

#: 回测方案里的参数名 -> 线上撮合引擎真正读取的 ``paper_*`` 字段。
#:
#: 只收「处置侧」参数：这些参数在信号已生成之后才起作用，所以多个账本
#: 可以共用同一份信号入队、各自按自己的规则成交。选股侧参数（阈值/因子/
#: 打分方式/策略子集）不在这里，因为信号是共享的，改不动。
PROFILE_TO_PAPER: dict[str, str] = {
    "stop_loss": "paper_stop_loss",
    "take_profit": "paper_take_profit",
    "max_positions": "paper_max_positions",
    "max_total_exposure": "paper_max_total_exposure",
    "single_position_ratio": "paper_single_position_max",
    "weak_exposure": "paper_weak_exposure",
    "regime_ma": "paper_regime_ma",
    "breakeven_trigger": "paper_breakeven_pct",
    "trail_pct": "paper_trail_pct",
    "trail_arm": "paper_trail_arm",
    "stop_atr_mult": "paper_atr_stop_mult",
    "stop_atr_max": "paper_atr_stop_max",
}

#: 明确不支持并行复现的参数名 -> 为什么（写进 API/日志，避免有人以为它在生效）
UNSUPPORTED_REASONS: dict[str, str] = {
    "invert_score": "评分反转发生在选股阶段，信号共享后改不动",
    "scoring_mode": "ML 打分发生在选股阶段",
    "ml_label_horizon": "ML 打分发生在选股阶段",
    "ml_model": "ML 打分发生在选股阶段",
    "ml_require_ic": "ML 打分发生在选股阶段",
    "strategies": "策略子集决定信号是否产生，信号共享后改不动",
    "factor_weights": "因子权重决定排序，信号共享后改不动",
    "buy_threshold": "买入阈值决定信号是否产生",
    "max_gain_5d": "买入前的涨幅过滤，信号已生成后失效",
    "min_volume_ratio": "买入前的量能过滤，信号已生成后失效",
    "min_turnover_rate": "买入前的换手过滤，信号已生成后失效",
    "max_open_gain": "买点的高开过滤，实盘由切入点规则接管",
    "max_hold_days": "时间止损需逐日盯持仓天数，引擎未实现",
    "regime_fast_ma": "回测四档择时用的快线，线上只有单线弱市判定",
    "regime_tiers": "回测四档仓位系数，线上用 paper_weak_exposure 单档替代",
    "regime_flat_below": "双线之下清仓是回测特有动作",
    "partial_tp_at": "分批止盈引擎未实现",
    "stop_confirm_close": "收盘确认止损引擎未实现",
    "board_prefixes": "股票池范围由 is_tradable 统一口径",
    "exec_jitter": "成交价扰动只用于稳健性检验",
}


class UnsupportedProfileError(ValueError):
    """方案名不存在，或含无法在并行账本里复现的参数。"""


class OverlaySettings:
    """给单个账本套一层参数覆盖：未覆盖的字段回落全局 settings。

    引擎里读参数是 ``self.settings.paper_xxx``，只要把 ``settings`` 换成这个
    代理，51 处调用一行都不用改，也不会漏改某一处导致两本账本串味。
    """

    __slots__ = ("_base", "_overrides")

    def __init__(self, base: Any, overrides: dict | None = None) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides or {}))

    def __getattr__(self, name: str):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # 引擎只会读；万一某处写回，落到全局 settings 而不是静默丢弃
        setattr(object.__getattribute__(self, "_base"), name, value)

    def overrides(self) -> dict:
        """当前覆盖项（落库/接口展示用）。"""
        return dict(object.__getattribute__(self, "_overrides"))


def profile_overrides(name: str) -> tuple[dict, list[str]]:
    """方案名 -> ``paper_*`` 覆盖参数。

    Returns:
        (overrides, 无法复现的参数名列表)。后者非空时调用方应跳过该方案，
        而不是拿半套参数去跑——曲线会与方案名不符。
    """
    from app.backtest.profiles import PROFILES

    if name not in PROFILES:
        raise UnsupportedProfileError(
            f"未知方案 {name}（可选见 app/backtest/profiles.py 的 PROFILES）"
        )
    overrides: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in (PROFILES[name] or {}).items():
        field = PROFILE_TO_PAPER.get(key)
        if field is None:
            skipped.append(key)
            continue
        overrides[field] = value
    return overrides, skipped


def _explain(skipped: list[str]) -> str:
    parts = []
    for key in skipped:
        reason = UNSUPPORTED_REASONS.get(key, "回测特有参数")
        parts.append(f"{key}（{reason}）")
    return "、".join(parts)


def sim_specs(settings: Any) -> tuple[list[dict], list[str]]:
    """由 ``paper_sim_profiles`` 生成账本规格。

    Returns:
        (specs, 被跳过的方案说明)。跳过的原因会进启动日志与 ``/api/paper/sim``，
        让人一眼看出「某个方案为什么没在并行跑」。
    """
    raw = str(getattr(settings, "paper_sim_profiles", "") or "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    capital = float(getattr(settings, "paper_sim_capital", 0) or 0)
    if capital <= 0:
        capital = float(getattr(settings, "paper_initial_capital", 100000.0))
    specs: list[dict] = []
    skipped_profiles: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        try:
            overrides, skipped = profile_overrides(name)
        except UnsupportedProfileError as exc:
            skipped_profiles.append(f"{name}：{exc}")
            continue
        if skipped:
            skipped_profiles.append(
                f"{name}：含无法并行复现的参数 {_explain(skipped)}，已跳过"
                "（用影子账户 scripts/shadow.py 跑这类选股侧差异）"
            )
            continue
        specs.append({
            "tag": name,
            "profile": name,
            "overrides": overrides,
            "initial_capital": capital,
            "note": "",
        })
    return specs, skipped_profiles


def account_overrides(acct: dict) -> dict:
    """把 sim_account.overrides 从 JSON 文本还原成 dict。"""
    raw = acct.get("overrides")
    if isinstance(raw, str):
        try:
            return dict(json.loads(raw or "{}"))
        except json.JSONDecodeError:
            return {}
    return dict(raw or {})


async def settle_ledgers(settings: Any, log: Any = None) -> dict[str, float]:
    """各并行账本分别结算（净值落 sim_equity），返回 tag -> 收益率(%)。

    单账本失败只记一条日志，不影响其它账本 —— 否则一个方案的脏数据会让所有
    对比曲线一起缺那一天。
    """
    from app.data.cache import db, ledger_scope
    from app.paper.engine import PaperEngine

    if not bool(getattr(settings, "paper_sim_enabled", False)):
        return {}
    out: dict[str, float] = {}
    for acct in db.list_sim_accounts(enabled_only=True):
        tag = str(acct.get("tag") or "")
        try:
            with ledger_scope(int(acct["account_id"])):
                detail = await PaperEngine(
                    OverlaySettings(settings, account_overrides(acct))
                ).settle(record_regime=False)
            out[tag] = round(float(detail.get("return_pct") or 0.0), 2)
        except Exception as exc:  # noqa: BLE001 - 单账本失败不影响其它账本
            if log is not None:
                log(f"并行账本 {tag} 结算失败：{type(exc).__name__} {exc}")
            out[tag] = float("nan")
    return out


def ledger_overview() -> list[dict]:
    """各账本概览（初始资金 / 现金 / 最新净值 / 累计收益 / 持仓数 / 参数）。"""
    from app.data.cache import db, ledger_scope

    rows: list[dict] = []
    for acct in db.list_sim_accounts():
        with ledger_scope(int(acct["account_id"])):
            equity = db.list_paper_equity(limit=2000)
            positions = db.list_paper_positions()
        init = float(acct.get("initial_capital") or 0)
        last = equity[-1] if equity else None
        total = float(last["total_asset"]) if last else float(acct.get("cash") or 0)
        rows.append({
            "tag": acct.get("tag") or "",
            "profile": acct.get("profile") or "",
            "enabled": bool(acct.get("enabled")),
            "initial_capital": init,
            "cash": float(acct.get("cash") or 0),
            "total_asset": round(total, 2),
            "return_pct": round((total / init - 1) * 100, 2) if init > 0 else 0.0,
            "positions": len(positions),
            "equity_days": len(equity),
            "as_of": last["date"] if last else None,
            "overrides": account_overrides(acct),
        })
    return rows


def ledger_equity(limit: int = 250) -> dict[str, list[dict]]:
    """各账本净值曲线（前端多曲线对比用），键为 tag。"""
    from app.data.cache import db, ledger_scope

    series: dict[str, list[dict]] = {}
    for acct in db.list_sim_accounts(enabled_only=True):
        with ledger_scope(int(acct["account_id"])):
            rows = db.list_paper_equity(limit=limit)
        series[str(acct.get("tag") or "")] = [
            {"date": r["date"], "total_asset": r["total_asset"]} for r in rows
        ]
    return series
