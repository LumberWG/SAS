"""回测 ↔ 实盘 参数对齐契约（单一来源 + 可执行漂移自检）。

用户约定（2026-11-12）：**后续所有更改，回测部分和实盘部分都同步，不能有丝毫不一样。**

光靠「写 `_defaults_payload()` 时记得读 `get_settings()`」是守不住这条约定的——以后
给 `Settings` 新加一个参数、给 `PipelineParams` 新加一个字段、或改了落盘映射却忘了
同步说明，都会悄悄产生偏差，而且页面上看不出来。本模块把约定变成**可验证的契约**：

1. :data:`BINDINGS` —— 「回测默认键 ← 实盘 Settings 字段」的显式登记。每一项都由
   :func:`audit_alignment` 用**运行时探针**验证：临时把该 setting 换成两个不同的值，
   重算回测默认值，若回测键纹丝不动，就说明同步断了。探针走只读代理 + 临时替换
   `get_settings` 返回值，**绝不修改 live 单例**，可安全在线上调用。
2. :data:`AUTO_FALLBACK` / :data:`RESEARCH_ONLY` —— 允许「defaults 不登记」的字段白名单。
   `PipelineParams` 里任何既不在 defaults、也不在白名单的新字段都会直接报漂移，
   逼迫新增参数时必须先想清楚「实盘有没有对应物、要不要同步」。
3. 反向一致性 —— `BACKTEST_TO_SETTING`（回测落盘 → 实盘）里的每一项，其落点字段必须
   与 :data:`BINDINGS` 的来源字段一致，否则会形成「默认从 A 读、落盘写到 B」的永久漂移。
4. 说明同步 —— `SKIP_REASONS` 里若出现「其实有 live 来源」的键（除显式豁免），说明
   文案已经过时，同样报漂移（历史教训：`watch_cap` / `add_per_day` 曾写着"实盘无对应
   配置"，而实盘早就有 `watch_auto_max` / `scan_auto_watch_limit`）。
5. 股票池口径 —— :func:`audit_scope_parity` 用合成票做行为探针，验证回测建池与实盘
   `scope.is_st_name` 同口径剔 ST，且 `_prepare` 真的把名称快照接了进去。参数登记
   再全也挡不住「代码里少了一层过滤」这类偏差（回测 #44 的 ST南都 -57%）。
6. 止损口径 —— :func:`audit_stop_parity` 验证回测与实盘物理共用
   `app.risk.stops.resolve_stop_price`，且止损判定不带任何大盘强弱门控。回测 #45
   曾用「弱市才防守」跳过 250 天里的 134 天止损，参数契约完全测不出来。

维护规则（改任何一侧参数时必做）：
- 实盘新增/改名 `Settings.paper_*` → 同步改 `api/backtest.py:_defaults_payload()`，并在
  :data:`BINDINGS` 登记；若该参数能由回测落盘，还要同步 :data:`apply.BACKTEST_TO_SETTING`。
- 回测新增 `PipelineParams` 字段 → 要么进 defaults + BINDINGS，要么进本文件白名单并写明理由。
- 改实盘可交易范围（ST / 板块 / 巨无霸）判定 → 回测 `_build_universe_by_date` 必须同步，
  共用 `scope` 里的判定函数，别各写一份正则。
- 改止损/止盈判定 → 两侧都必须走 `app.risk.stops.resolve_stop_price`，不得在任一侧
  另加 regime / 大盘强弱 / 机制层门控；`audit_stop_parity` 会抓回归。
- 改完跑 `scripts/check_backtest_alignment.py`（或 `GET /api/backtest/alignment`），必须零漂移。
"""
from __future__ import annotations

import dataclasses
from typing import Any
from unittest import mock

from app.backtest.apply import BACKTEST_TO_SETTING, SKIP_REASONS
from app.backtest.pipeline import PipelineParams
from app.config import get_settings


@dataclasses.dataclass(frozen=True)
class Binding:
    """一条「回测默认键跟随实盘字段」的契约。

    ``probe_a`` / ``probe_b`` 是两个不同的哨兵值：自检时分别注入 ``setting``，
    回测默认键必须给出两个不同结果，才算真的跟随。用双值而非「当前值 vs 哨兵」，
    可避免哨兵恰好等于线上现值造成的假阴性。
    """

    setting: str
    probe_a: Any
    probe_b: Any
    preconditions: dict[str, Any] = dataclasses.field(default_factory=dict)
    note: str = ""


#: 回测参数键 → 实盘来源契约。覆盖 `_defaults_payload()` 里**所有**读 Settings 的项。
#: 新增实盘参数时，这里必须同步登记，否则自检直接报「未登记」。
BINDINGS: dict[str, Binding] = {
    # ---- 基础 / 池子 / 板块 ----
    "initial_capital": Binding("paper_initial_capital", 111111.0, 222222.0),
    "max_positions": Binding("paper_max_positions", 7, 13),
    "universe_size": Binding("paper_pool_top_n", 137, 246, note="0=不限制，回测折算 6000 并 clamp"),
    "board_prefixes": Binding("twin_t_boards", "创业板", "科创板", note="统一板块，折算成板块键数组"),
    # 策略集：实盘是 .env 只读的 A-Only 护栏，回测默认必须 1:1 跟随。
    # 以前放在 RESEARCH_ONLY（"默认 None=全部策略"）→ 回测默认 14 策略全开、
    # 与实盘 A-Only 完全不同，却因豁免而零漂移（回测 #44 暴露的口径问题）。
    "strategies": Binding(
        "auto_scan_strategies", ["量价共振2560"], ["海龟突破", "均线放量"],
        note="跟随 .env 的 AUTO_SCAN_STRATEGIES；空数组=全部策略，与实盘同义",
    ),
    "watch_cap": Binding("watch_auto_max", 137, 246, note="clamp 到 BacktestRequest le=500"),
    "add_per_day": Binding("scan_auto_watch_limit", 37, 46, note="clamp 到 BacktestRequest le=200"),
    # ---- 选股 / 买入 ----
    "buy_threshold": Binding("factor_buy_threshold", 11.0, 22.0),
    "queue_top_m": Binding("paper_trade_queue_top_m", 7, 13),
    "entry_max_chase": Binding("paper_entry_max_chase", 0.11, 0.22),
    "entry_pullback": Binding("paper_entry_pullback", 0.11, 0.22),
    "entry_window_minutes": Binding("paper_entry_timeout_minutes", 7, 13),
    # ---- 止损 / 止盈 ----
    "stop_loss": Binding("paper_stop_loss", 0.11, 0.22),
    "take_profit": Binding("paper_take_profit", 0.31, 0.42),
    "stop_atr_mult": Binding("paper_atr_stop_mult", 1.1, 2.2),
    "stop_atr_min": Binding("paper_stop_loss", 0.11, 0.22, note="实盘下限=固定止损，与 stop_loss 同源"),
    "stop_atr_max": Binding("paper_atr_stop_max", 0.31, 0.42),
    "breakeven_trigger": Binding("paper_breakeven_pct", 0.11, 0.22),
    "trail_pct": Binding("paper_trail_pct", 0.11, 0.22),
    "trail_arm": Binding("paper_trail_arm", 0.11, 0.22),
    # ---- 大盘择时（两项都受 weak_pause 开关门控，探针需先打开它）----
    "regime_ma": Binding("paper_regime_ma", 37, 73, {"paper_regime_weak_pause": True}),
    "regime_index": Binding("paper_regime_index", "sh000985", "sh000001"),
    "weak_exposure": Binding("paper_weak_exposure", 0.11, 0.22, {"paper_regime_weak_pause": True}),
    # ---- 仓位 / 评分 / 轮动 ----
    "max_total_exposure": Binding("paper_max_total_exposure", 0.41, 0.62),
    "single_position_ratio": Binding("paper_single_position_max", 0.11, 0.22),
    "full_score": Binding("paper_full_score", 31.0, 42.0),
    "half_score": Binding("paper_half_score", 11.0, 22.0),
    "rotation_enabled": Binding("paper_rotation_enabled", False, True),
    "rotation_gap": Binding("paper_rotation_gap", 11.0, 22.0),
    "rotation_min_score": Binding("paper_rotation_min_score", 31.0, 42.0),
    "rotation_max_per_day": Binding("paper_rotation_max_per_day", 3, 7),
    # ---- 做 T ----
    "t_active": Binding("paper_t_active", False, True),
    "t_size": Binding("paper_t_size", 0.11, 0.22),
    "t_up_sell": Binding("paper_t_up_sell", 0.11, 0.22),
    "t_up_buyback": Binding("paper_t_up_buyback", 0.11, 0.22),
    "t_down_add": Binding("paper_t_down_add", 0.11, 0.22),
    "t_down_full": Binding("paper_t_down_full", 0.11, 0.22),
    "t_rebound_sell": Binding("paper_t_rebound_sell", 0.11, 0.22),
    "t_limit_sell_half": Binding("paper_t_limit_sell_half", False, True),
    "t_crash_guard": Binding("paper_t_crash_guard", 0.11, 0.22),
    # ---- 入场冷静期 / 打分层 ----
    "entry_open_delay_minutes": Binding("paper_open_cooldown_minutes", 7, 13),
    "scoring_mode": Binding("live_ml_scoring", False, True, note="bool 开关折算成 ml/factors"),
    "industry_neutral": Binding("live_ml_industry_neutral", False, True, note="回测默认与线上 ML 行业中性同源"),
    # 短线持有上限：线上自然日 → 回测交易日（5/7 换算），探针 7/21 会分别得到 5/15。
    "max_hold_days": Binding(
        "paper_max_hold_days", 7, 21,
        note="两周=14自然日≈10交易日；单位不同只单向跟随，不做落盘",
    ),
    # ---- P1 上下文风险闸：四项共用一份函数，6 个参数逐项跟随实盘 ----
    # 行为口径另由 audit_context_parity() 验证（参数对齐测不出「少一层逻辑」）。
    "context_enabled": Binding("paper_ctx_enabled", False, True, note="四项总开关"),
    "context_max_industry_weight": Binding("paper_ctx_max_industry_weight", 0.11, 0.22,
                                          note="0=关闭行业集中度子闸"),
    "context_max_corr": Binding("paper_ctx_max_corr", 0.11, 0.22,
                                note="0=关闭相关性子闸"),
    "context_hot_exclude_pct": Binding("paper_ctx_hot_exclude_pct", 0.11, 0.22,
                                       note="0=关闭板块过热剔除子闸"),
    "context_event_cooldown_days": Binding("paper_ctx_event_cooldown_days", 2, 9,
                                           note="0=关闭事件日历子闸"),
    "context_news_block": Binding("paper_ctx_news_block", 0.11, 0.22,
                                  note="0=关闭舆情新闻子闸"),
}

#: 由 pipeline 在运行时自动读 `Settings.paper_*` 兜底的字段（defaults 不传即同步）。
#: 见 `pipeline._simulate`：`params.X if X is not None else settings.paper_X`。
AUTO_FALLBACK: dict[str, str] = {
    "board_trend_gate": "pipeline 以 settings.paper_board_trend_gate 兜底",
    "unified_risk_cap_enabled": "pipeline 以 settings.paper_unified_risk_cap_enabled 兜底",
}

#: 回测专属研究旋钮 / 中性默认：实盘没有对应实现，默认值 None/False/0 恰好等于
#: 「实盘不启用此逻辑」。任何新增字段都必须在此登记理由，否则自检报漂移。
RESEARCH_ONLY: dict[str, str] = {
    "bear_half_score": "分档仓位实验，默认 None=未启用",
    "dd_pause_at": "回撤暂停实验(E49)，默认 None=未启用",
    "dd_pause_days": "由 dd_pause_at=None 门控，不生效",
    "entry_confirm_minutes": "回测专属确认档，默认 None",
    "entry_confirm_mode": "回测专属确认档，默认 None",
    "factor_weights": "自定义因子权重实验，默认 None",
    "fill_noise_bps": "成交价扰动实验，默认 0=不扰动",
    "fill_noise_seed": "由 fill_noise_bps=0 门控，不生效",
    "intraday_entry": "盘中分批入场实验，默认 False",
    "intraday_entry_times": "由 intraday_entry=False 门控，不生效",
    "intraday_entry_top_n": "由 intraday_entry=False 门控，不生效",
    "intraday_minutes": "分钟级采样实验，默认 False",
    "max_pos_bear": "分档仓位实验，默认 None",
    "max_pos_bull": "分档仓位实验，默认 None",
    "open_fill_minutes": "开盘成交窗口实验，默认 0",
    "regime_tiers": "多档择时实验，默认 None",
    "slippage": "滑点实验，默认 0=无滑点",
    # regime-aware 打分路由实验（研究专用，验证结论：反向、不优于纯因子，未进实盘）
    "ml_regime_aware": "regime-aware 路由实验，默认 False=不启用，实盘走纯因子",
    "ml_regime_vol_cap": "由 ml_regime_aware=False 门控，不生效",
    "ml_regime_ma_gap": "由 ml_regime_aware=False 门控，不生效",
    # IC 门控 ML 叠加实验（第三条路 v2，研究专用，默认 False=不启用，实盘走纯因子）
    "ml_ic_gate": "IC 门控 ML 叠加实验，默认 False=不启用，实盘走纯因子",
    "ml_ic_gate_window": "由 ml_ic_gate=False 门控，不生效",
    "ml_ic_gate_thresh": "由 ml_ic_gate=False 门控，不生效",
    "ml_ic_gate_min_samples": "由 ml_ic_gate=False 门控，不生效",
    "ml_ic_gate_segment": "IC 门控段级 OOS 模式，默认 False=不启用，实盘走纯因子",
    "ml_seed": "多 seed 严格验收专用，默认 42；线上固定同一 seed，不进页面",
    "ml_strict_lookahead": "严格零前视口径参数，默认 True；线上与回测同实现，不进页面",
}

#: 非参数字段（回测时间段由请求传入，不属于对齐契约）
NON_PARAM_FIELDS: frozenset[str] = frozenset({"start_date", "end_date"})

#: 允许「有 live 来源、但不落盘」的键（两类，必须写明理由，否则自检报错）：
#: 1. 同源参数由另一个键负责落盘，避免重复写同一字段；
#: 2. 实盘有配置、但改它不会改变既有账户行为（落盘无意义）。
SKIP_ALLOWED_SAME_SOURCE: dict[str, str] = {
    "stop_atr_min": "与 stop_loss 同源(paper_stop_loss)，由 stop_loss 落盘",
}
SKIP_ALLOWED_NO_DIRECT_MAPPING: dict[str, str] = {
    "initial_capital": "实盘 paper_initial_capital 只在开户/重置时生效，落盘不会重置已有模拟账户",
    "max_hold_days": (
        "实盘按自然日、回测按交易日，落盘反向换算会漂移；默认值只由 "
        "_defaults_payload 按 5/7 单向跟随（见 SKIP_REASONS）"
    ),
    "strategies": (
        "实盘 auto_scan_strategies 属 READONLY_KEYS（.env 只读的 A-Only 护栏），"
        "刻意不允许页面一键改成 14 策略全开；改 .env 重启后回测默认值会自动跟随"
    ),
}


class _SettingsProxy:
    """只读代理：把少量字段替换成哨兵值，其余全部委托给真实 Settings。

    这样自检无需 `setattr` 修改 live 单例，不会污染正在运行的交易服务。
    """

    def __init__(self, base: Any, overrides: dict[str, Any]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name: str) -> Any:
        ov = object.__getattribute__(self, "_overrides")
        if name in ov:
            return ov[name]
        return getattr(object.__getattribute__(self, "_base"), name)


def _defaults_with(overrides: dict[str, Any]) -> dict:
    """在「临时替换 get_settings 返回值」的隔离环境里重算回测默认值。"""
    from app.api.backtest import _defaults_payload  # 延迟导入，避免与 api 层循环依赖

    base = get_settings()
    with mock.patch(
        "app.api.backtest.get_settings",
        return_value=_SettingsProxy(base, overrides),
    ):
        return _defaults_payload()


def _same(a: Any, b: Any) -> bool:
    """比较探针两次结果（列表/元组按序列比较）。"""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def audit_scope_parity() -> list[dict[str, str]]:
    """股票池口径探针：回测必须与实盘同口径剔除 ST / *ST / 退市票。

    参数契约管不到「代码里少了一层过滤」这类偏差——回测 #44 就是这样把
    ST南都(300068) 放进池子、打出 -57% 单笔。这里用两只合成票做行为探针：
    名称带 ST 的必须被 `_build_universe_by_date` 剔掉，正常票必须留下；同时
    检查 `_prepare` 确实把名称快照传进建池函数（否则过滤形同虚设）。
    """
    drifts: list[dict[str, str]] = []
    try:
        import inspect

        import pandas as pd

        from app.backtest import pipeline as _pipeline
        from app.backtest.pipeline import _build_universe_by_date
        from app.data.scope import is_st_name

        day = "2026-01-05"
        raw = {
            "300001": pd.DataFrame({"date": [day], "amount": [2.0e8]}),
            "300068": pd.DataFrame({"date": [day], "amount": [1.0e8]}),
        }
        names = {"300001": "测试A", "300068": "ST测试"}
        if not is_st_name("ST测试"):
            drifts.append({
                "kind": "scope_st_detector_broken", "key": "universe_st_filter",
                "detail": "实盘 scope.is_st_name 已识别不出 ST 名称，ST 判定口径被改坏",
            })
        universe = _build_universe_by_date(raw, [day], 10, None, names=names)[day]
        if "300068" in universe:
            drifts.append({
                "kind": "backtest_universe_missing_st_filter", "key": "universe_st_filter",
                "detail": "回测股票池仍会放进 ST 票，与实盘 scanner / 买入池口径不一致",
            })
        if "300001" not in universe:
            drifts.append({
                "kind": "backtest_universe_over_filtered", "key": "universe_st_filter",
                "detail": "回测股票池把正常票也剔了，ST 过滤范围不对",
            })
        if "names=names" not in inspect.getsource(_pipeline._prepare):
            drifts.append({
                "kind": "backtest_universe_names_not_wired", "key": "universe_st_filter",
                "detail": "_prepare 没有把名称快照传给 _build_universe_by_date，ST 过滤不会生效",
            })
    except Exception as exc:  # noqa: BLE001 - 探针本身跑不起来也算漂移
        drifts.append({
            "kind": "scope_probe_error", "key": "universe_st_filter",
            "detail": f"股票池口径探针执行失败: {exc}",
        })
    return drifts


def audit_stop_parity() -> list[dict[str, str]]:
    """止损口径探针：两侧物理共用一份解析，且止损不受大盘强弱门控。

    回测 #45 的教训：``_simulate`` 曾用「大盘弱于 MA60 才防守」把**固定止损**
    一并门控，250 天里 134 天完全不止损、45 笔止损全挤在弱市日；实盘
    ``check_stop_loss_target`` 是每日无条件检查。参数绑定（BINDINGS）对这类
    「代码行为差异」无能为力，必须做可执行的行为 + 源码探针。
    """
    drifts: list[dict[str, str]] = []
    key = "stop_loss_gate"

    def bad(kind: str, detail: str) -> None:
        drifts.append({"kind": kind, "key": key, "detail": detail})

    try:
        import inspect
        import linecache
        import re

        from app.backtest import pipeline as _pipeline
        from app.paper.engine import PaperEngine
        from app.risk.stops import resolve_stop_price

        # 1) 纯函数行为：固定 / 保本 / 移动三档，以及 trail_arm 必须真的生效
        cost, base = 100.0, 92.0
        cases = [
            ((100.0, 0.0, 0.0, 0.0), 92.0, "止损"),
            ((130.0, 0.1, 0.0, 0.0), 100.0, "保本止损"),
            ((130.0, 0.0, 0.1, 0.0), 117.0, "移动止损"),
            ((130.0, 0.1, 0.1, 0.0), 117.0, "移动止损"),
            ((105.0, 0.0, 0.1, 0.1), 92.0, "止损"),
        ]
        for (peak, be, trail, arm), want_px, want_label in cases:
            px, label = resolve_stop_price(
                cost, base, peak, breakeven_pct=be, trail_pct=trail, trail_arm=arm,
            )
            if abs(px - want_px) > 1e-9 or label != want_label:
                bad(
                    "stop_resolver_behavior_changed",
                    f"peak={peak} be={be} trail={trail} arm={arm} 得到 "
                    f"({px}, {label})，期望 ({want_px}, {want_label})",
                )

        # 2) 签名不得混入任何市场状态，门控只能由调用方显式传参
        forbidden = {"regime", "defensive", "weak", "market", "index", "state"}
        mixed = forbidden & set(inspect.signature(resolve_stop_price).parameters)
        if mixed:
            bad("stop_resolver_has_market_gate", f"止损解析签名混入市场状态参数: {sorted(mixed)}")

        # 3) 两侧必须真的调用同一份函数，而不是各写一份
        sim_src = inspect.getsource(_pipeline._simulate)

        # 行号护栏：inspect.getsource 拿的是「内存中」code 对象的 co_firstlineno，
        # 却去读「磁盘上」的源文件。服务启动后若源文件被改动（增删行），会取到
        # 另一个方法的源码，进而误报「实盘未共用止损」——2026-11-12 就发生过：
        # 服务 21:46 启动、engine.py 22:30 改过，回测页因此显示 1 项假漂移。
        # 这类情况应明确报「代码未同步 / 需重启」，不能伪装成策略口径漂移。
        live_fn = PaperEngine.check_stop_loss_target
        live_file = inspect.getsourcefile(live_fn) or ""
        first_line = live_fn.__code__.co_firstlineno
        if live_file:
            linecache.checkcache(live_file)
        disk_line = linecache.getline(live_file, first_line).strip() if live_file else ""
        if "def check_stop_loss_target" not in disk_line:
            bad(
                "stale_live_module_loaded",
                f"磁盘代码已在服务启动后更新：内存函数首行 {first_line} 对应磁盘内容 "
                f"{disk_line[:48]!r}；请重启后端加载新代码（此项不是策略口径漂移）",
            )
            return drifts
        live_src = inspect.getsource(live_fn)
        if "resolve_stop_price" not in sim_src:
            bad("backtest_stop_not_shared", "回测 _simulate 未调用共用的 resolve_stop_price")
        if "resolve_stop_price" not in live_src:
            bad("live_stop_not_shared", "实盘 check_stop_loss_target 未调用共用的 resolve_stop_price")

        # 4) 门控回归：回测止损段不得再出现强弱门控变量 / 条件赋值
        if "defensive" in sim_src:
            bad("backtest_stop_regime_gate_reintroduced",
                "回测 _simulate 又出现 defensive 门控；止损必须每日无条件生效")
        if re.search(r"stop_check\s*=\s*[^#\n]*(?:regime|weak|defensive)", sim_src):
            bad("backtest_stop_check_gated",
                "回测 stop_check 被大盘强弱条件门控，强市会漏掉止损")
        if "defensive" in live_src or re.search(
            r"stop_price\s*=\s*[^#\n]*_index_is_weak", live_src
        ):
            bad("live_stop_regime_gate_reintroduced",
                "实盘止损被大盘强弱门控，与回测口径不一致")
    except Exception as exc:  # noqa: BLE001 - 探针本身跑不起来也算漂移
        drifts.append({
            "kind": "stop_probe_error", "key": key,
            "detail": f"止损口径探针执行失败: {exc}",
        })
    return drifts


def audit_context_parity() -> list[dict[str, str]]:
    """行为探针 3：P1 四项上下文风险闸的两侧口径。

    参数对齐（BINDINGS）测不出「某一侧少了一层逻辑」或「配置字段映射写错」，
    因此这里做四层可执行检查：
      1) 决策函数签名不得混入 regime / 大盘 / 机制层参数（#45 的教训）；
      2) ``Settings → ContextGateConfig`` 与 ``PipelineParams → ContextGateConfig``
         用同一组哨兵值必须得到**完全相同**的配置，且换哨兵值必须跟着变；
      3) 四项闸门各自用隔离的假数据跑一遍，必须真的产出对应拒单原因；
      4) 回测 ``_simulate``、实盘 ``execute_recommendations``（含手动 ``place_order``）
         的源码里都必须出现 ``audit_buy_candidates``——同一份函数，不是各写一份。
    """
    drifts: list[dict[str, str]] = []

    def bad(kind: str, detail: str, key: str = "context") -> None:
        drifts.append({"kind": kind, "key": key, "detail": detail})

    try:
        import inspect
        import linecache
        from types import SimpleNamespace

        from app.backtest import pipeline as _pipeline
        from app.paper.engine import PaperEngine
        from app.risk.context_gates import (
            CORR_WINDOW, ContextGateConfig, REASONS, audit_buy_candidates,
        )

        # 1) 签名纯净性
        forbidden = {"regime", "defensive", "weak", "market", "index", "state"}
        mixed = forbidden & set(inspect.signature(audit_buy_candidates).parameters)
        if mixed:
            bad("context_gate_has_market_gate",
                f"上下文闸签名混入市场状态参数: {sorted(mixed)}")

        # 2) 双入口配置映射：两组哨兵都要「两侧相等」，且两组结果互不相同
        set_a = {"enabled": True, "max_industry_weight": 0.11, "max_corr": 0.22,
                 "hot_exclude_pct": 0.13, "event_cooldown_days": 2, "news_block": 0.31}
        set_b = {"enabled": False, "max_industry_weight": 0.44, "max_corr": 0.66,
                 "hot_exclude_pct": 0.27, "event_cooldown_days": 9, "news_block": 0.72}
        def _cfg(prefix: str, vals: dict) -> ContextGateConfig:
            obj = SimpleNamespace(**{prefix + k: v for k, v in vals.items()})
            return (ContextGateConfig.from_settings(obj) if prefix == "paper_ctx_"
                    else ContextGateConfig.from_params(obj))
        for label, vals in (("A", set_a), ("B", set_b)):
            if _cfg("paper_ctx_", vals) != _cfg("context_", vals):
                bad("context_config_mapping_split",
                    f"哨兵组 {label}：实盘 Settings 与回测 PipelineParams 映射到不同的闸门配置")
        if _cfg("paper_ctx_", set_a) == _cfg("paper_ctx_", set_b):
            bad("context_config_not_following_live",
                "改实盘 paper_ctx_* 哨兵值，闸门配置没有跟着变")

        # 3) 四项闸门的行为探针（全部用隔离假数据，不碰生产库 / 不碰 app_state）
        class _ProbeStore:
            def __init__(self, *, events=None, news=None, heat=None, rets=None,
                         industries=None, closes=None):
                self.events = events or {}
                self.news = news or {}
                self.heat = heat or {}
                self.rets = rets or {}
                self.industries = industries or {}
                # 注意别叫 self.closes：那会覆盖同名的 closes() 方法
                self.close_map = closes or {}
            def bulk_events(self, codes, as_of, days):
                return {c: v for c, v in self.events.items() if c in codes}
            def bulk_news(self, codes, as_of, days):
                return {c: v for c, v in self.news.items() if c in codes}
            def heat_thresholds(self, as_of):
                return self.heat
            def ret(self, code, as_of, lookback):
                return self.rets.get(code)
            def industry_of(self, code):
                return self.industries.get(code, "默认行业")
            def closes(self, code, as_of, window):
                return self.close_map.get(code, ())
            def coverage(self):
                return {}

        def _cfg_off(**kw) -> ContextGateConfig:
            base = dict(enabled=True, max_industry_weight=0.0, max_corr=0.0,
                        hot_exclude_pct=0.0, event_cooldown_days=0, news_block=0.0)
            base.update(kw)
            return ContextGateConfig(**base)

        day = "2026-01-05"
        cases = [
            ("ctx_event_cooling", _cfg_off(event_cooldown_days=3), ["A"], [],
             _ProbeStore(events={"A": [{"kind": "earnings"}]})),
            ("ctx_negative_news", _cfg_off(news_block=0.6), ["B"], [],
             _ProbeStore(news={"B": -0.9})),
            ("ctx_hot_stock", _cfg_off(hot_exclude_pct=0.2), ["C"], [],
             _ProbeStore(heat={"stock": 0.10, "industry": 0.10,
                               "hot_industries": frozenset()}, rets={"C": 0.5})),
            ("ctx_hot_board", _cfg_off(hot_exclude_pct=0.2), ["D"], [],
             _ProbeStore(heat={"stock": None, "industry": 0.10,
                               "hot_industries": frozenset({"过热行业"})},
                         industries={"D": "过热行业"})),
            ("ctx_industry_weight", _cfg_off(max_industry_weight=0.4), ["F"], ["E"],
             _ProbeStore(industries={"E": "同一行业", "F": "同一行业"})),
        ]
        # 相关性：构造 30 根完全同向的收盘价，|corr| = 1 > 0.85
        wave = tuple(100.0 + i + (i % 3) * 0.5 for i in range(CORR_WINDOW // 2 + 2))
        cases.append((
            "ctx_correlation", _cfg_off(max_corr=0.85), ["I"], ["H"],
            _ProbeStore(closes={"H": wave, "I": wave}),
        ))
        for want, cfg, codes, held, store in cases:
            got = audit_buy_candidates(codes, as_of=day, held_codes=held,
                                       cfg=cfg, store=store)
            if got.get(codes[0]) != want:
                bad("context_gate_behavior_broken",
                    f"子闸 {want} 未生效：探针返回 {got!r}", key=want)
        if set(REASONS) != {c[0] for c in cases}:
            bad("context_reason_set_changed",
                f"REASONS={sorted(REASONS)} 与探针覆盖的四项不一致")
        # 回归护栏：全市场每日公告里八成是普通公告，若把它们列入阻断型事件，
        # 冷却闸会几乎锁死买入。白名单必须存在且不含 other / dividend。
        from app.risk.context_store import BLOCKING_EVENT_KINDS, ContextStore
        if "other" in BLOCKING_EVENT_KINDS or "dividend" in BLOCKING_EVENT_KINDS:
            bad("context_event_kind_too_broad",
                "普通公告/除权被列入阻断型事件，冷却闸会锁死买入")
        if not {"earnings", "unlock", "reduce", "st_change"} <= set(BLOCKING_EVENT_KINDS):
            bad("context_event_kind_incomplete", "重大事件类型未全部进入阻断白名单")
        if "kind IN" not in inspect.getsource(ContextStore.bulk_events):
            bad("context_event_kind_filter_missing",
                "bulk_events 未按阻断型事件白名单过滤 SQL")
        if audit_buy_candidates(["A"], as_of=day, held_codes=[],
                                cfg=_cfg_off(enabled=False),
                                store=_ProbeStore(events={"A": [{"kind": "earnings"}]})):
            bad("context_master_switch_broken", "总开关关闭后仍返回了拒单")
        # 回归：空仓买第一只时行业占比必然 100%，集中度闸不得把首仓误杀
        # （曾因直接判「占比 > 上限」导致一个候选都过不了）。
        first = audit_buy_candidates(
            ["FIRST"], as_of=day, held_codes=[],
            cfg=_cfg_off(max_industry_weight=0.4),
            store=_ProbeStore(industries={"FIRST": "同一行业"}),
        )
        if first:
            bad("context_concentration_blocks_first_position",
                f"空仓首只买入被集中度闸误杀：{first!r}")

        # 4) 两侧必须真的调用同一份函数（含 stale_live_module_loaded 护栏）
        sim_src = inspect.getsource(_pipeline._simulate)
        live_fn = PaperEngine.execute_recommendations
        live_file = inspect.getsourcefile(live_fn) or ""
        first_line = live_fn.__code__.co_firstlineno
        if live_file:
            linecache.checkcache(live_file)
        disk_line = linecache.getline(live_file, first_line).strip() if live_file else ""
        if "def execute_recommendations" not in disk_line:
            bad("stale_live_module_loaded",
                f"磁盘代码已在服务启动后更新：内存函数首行 {first_line} 对应磁盘内容 "
                f"{disk_line[:48]!r}；请重启后端加载新代码（此项不是策略口径漂移）")
            return drifts
        live_src = inspect.getsource(live_fn)
        manual_src = inspect.getsource(PaperEngine.place_order)
        if "audit_buy_candidates" not in sim_src:
            bad("backtest_context_not_shared", "回测 _simulate 未调用共用 audit_buy_candidates")
        if "audit_buy_candidates" not in live_src:
            bad("live_context_not_shared", "实盘 execute_recommendations 未调用共用闸函数")
        if "audit_buy_candidates" not in manual_src:
            bad("live_manual_context_bypassed", "实盘手动下单绕过了上下文风险闸")
    except Exception as exc:  # noqa: BLE001 - 探针本身跑不起来也算漂移
        bad("context_probe_error", f"上下文闸行为探针异常: {type(exc).__name__}: {exc}")
    return drifts


def context_data_coverage() -> dict[str, Any]:
    """只读诊断：四项闸实际读到的本地快照覆盖情况（不参与漂移判定）。"""
    try:
        from app.risk.context_store import ContextStore
        return ContextStore().coverage()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def audit_alignment() -> dict:
    """跑完整对齐自检，返回结构化报告。``ok=True`` 表示零漂移。

    七类检查：跟随性探针、字段覆盖性、落盘反向一致性、跳过说明同步性，
    以及三类行为探针（股票池 ST 过滤、止损门控、P1 上下文风险闸）。
    """
    scope_drifts = audit_scope_parity()
    stop_drifts = audit_stop_parity()
    context_drifts = audit_context_parity()
    drifts: list[dict[str, str]] = (
        list(scope_drifts) + list(stop_drifts) + list(context_drifts)
    )
    defaults = _defaults_with({})
    pp_fields = {f.name for f in dataclasses.fields(PipelineParams)}

    # ---- 1. 跟随性探针：改实盘，回测默认必须跟着变 ----
    for key, binding in sorted(BINDINGS.items()):
        pre = dict(binding.preconditions)
        try:
            a = _defaults_with({**pre, binding.setting: binding.probe_a}).get(key)
            b = _defaults_with({**pre, binding.setting: binding.probe_b}).get(key)
        except Exception as exc:  # noqa: BLE001 - 探针失败也算漂移，需暴露
            drifts.append({
                "kind": "probe_error", "key": key,
                "detail": f"改 {binding.setting} 时回测默认值计算抛错: {exc}",
            })
            continue
        if key not in defaults:
            drifts.append({
                "kind": "missing_default", "key": key,
                "detail": f"契约登记了 {key}，但 _defaults_payload() 没返回它",
            })
        elif _same(a, b):
            drifts.append({
                "kind": "not_following_live", "key": key,
                "detail": (
                    f"把实盘 {binding.setting} 改成 {binding.probe_a!r}/{binding.probe_b!r}，"
                    f"回测默认 {key} 都还是 {a!r} —— 同步已断开"
                ),
            })

    # ---- 2. 覆盖性：PipelineParams 新字段必须登记 ----
    unregistered = sorted(
        pp_fields - set(defaults) - set(AUTO_FALLBACK) - set(RESEARCH_ONLY) - NON_PARAM_FIELDS
    )
    for key in unregistered:
        drifts.append({
            "kind": "unregistered_param", "key": key,
            "detail": (
                "PipelineParams 新增字段未在回测默认值登记，会退回硬编码默认 → 与实盘脱节；"
                "请补 _defaults_payload() + BINDINGS，或在本文件 RESEARCH_ONLY 写明豁免理由"
            ),
        })
    for key in sorted(set(AUTO_FALLBACK) & set(defaults)):
        drifts.append({
            "kind": "redundant_auto_fallback", "key": key,
            "detail": "已进 defaults 就无需 AUTO_FALLBACK 豁免，请清理白名单",
        })

    # ---- 3. 反向一致性：落盘目标字段必须就是默认值来源字段 ----
    for key, target in sorted(BACKTEST_TO_SETTING.items()):
        binding = BINDINGS.get(key)
        if binding is None:
            drifts.append({
                "kind": "persist_without_binding", "key": key,
                "detail": f"落盘会写 {target}，但回测默认值没登记这个来源（BINDINGS 缺失）",
            })
        elif binding.setting != target:
            drifts.append({
                "kind": "persist_target_mismatch", "key": key,
                "detail": (
                    f"回测默认读 {binding.setting}，落盘却写 {target} —— 双向会永久漂移"
                ),
            })
        if key not in defaults:
            drifts.append({
                "kind": "persist_key_missing", "key": key,
                "detail": "可落盘键不在回测默认值里，落盘后默认值不会跟着变",
            })

    # ---- 4. 说明同步：SKIP_REASONS 不能把「有 live 来源」的项说成实盘无配置 ----
    allowed_skips = set(SKIP_ALLOWED_SAME_SOURCE) | set(SKIP_ALLOWED_NO_DIRECT_MAPPING)
    for key, reason in sorted(SKIP_REASONS.items()):
        if key in BINDINGS and key not in allowed_skips:
            drifts.append({
                "kind": "stale_skip_reason", "key": key,
                "detail": (
                    f"回测默认跟随实盘 {BINDINGS[key].setting}，却仍被列为「不落盘」：{reason}；"
                    "请补 BACKTEST_TO_SETTING，或登记到 SKIP_ALLOWED_* 并写明理由"
                ),
            })

    return {
        "ok": not drifts,
        "summary": {
            "bound": len(BINDINGS),
            "defaults_fields": len(defaults),
            "pipeline_fields": len(pp_fields),
            "auto_fallback": len(AUTO_FALLBACK),
            "research_only": len(RESEARCH_ONLY),
            "persist_mapped": len(BACKTEST_TO_SETTING),
            "scope_parity": "ok" if not scope_drifts else "drift",
            "stop_parity": "ok" if not stop_drifts else "drift",
            "context_parity": "ok" if not context_drifts else "drift",
            "context_coverage": context_data_coverage(),
            "drifts": len(drifts),
        },
        "drifts": drifts,
        "bindings": [
            {"key": k, "setting": v.setting, "note": v.note}
            for k, v in sorted(BINDINGS.items())
        ],
        "auto_fallback": dict(AUTO_FALLBACK),
        "research_only": dict(RESEARCH_ONLY),
        "skip_allowed_same_source": dict(SKIP_ALLOWED_SAME_SOURCE),
        "skip_allowed_no_direct_mapping": dict(SKIP_ALLOWED_NO_DIRECT_MAPPING),
    }


def summary() -> dict:
    """给 `/backtest/defaults` 用的轻量摘要（完整报告走 `/backtest/alignment`）。"""
    report = audit_alignment()
    return {
        "ok": report["ok"],
        "bound": report["summary"]["bound"],
        "drifts": report["drifts"],
    }
