"""回测 API：提交「全流水线回测」（异步任务 + 轮询）、查询进度与历史记录。

回测固定复刻线上模拟盘的完整链路（策略扫描 → 加自选 → 14因子排序
→ 评分加权下单）。默认策略集跟随实盘 ``AUTO_SCAN_STRATEGIES``（A-Only
时只有「量价共振2560」），不再是「14 个策略全开」；用户只指定时间段。

回测要逐只股票逐日滚动评估策略，几百只 × 一年量级往往需要 1~3 分钟，
同步接口必然撞上前端/代理超时，因此改为「提交任务 → 轮询状态」。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.utils import with_disclaimer
from app.backtest import progress as bt_progress
from app.backtest.alignment import audit_alignment, summary as alignment_summary
from app.backtest.apply import persist_map, split_changes
from app.backtest.pipeline import PipelineParams, run_pipeline
from app.board_rules import parse_trade_boards
from app.config import get_settings
from app.data.cache import db
from app.param_specs import update_settings

logger = logging.getLogger("sas.api.backtest")
router = APIRouter(prefix="/backtest", tags=["回测"])

#: 这些参数在 :func:`_defaults_payload` 里用 **0** 表示「不启用」，而 ``PipelineParams``
#: 用 **None** 表示「不启用」。以 defaults 为基底合并后若不转换，0 会被 pipeline 当成
#: 「上限 0」这类极端约束——实测 ``entry_max_chase=0`` 等价于「开盘价只要高于信号价就放弃」，
#: 把 121 个交易日的成交从 91 笔压到 5 笔，看起来像「策略几乎不交易」。
#: 注意 ``weak_exposure=0`` 不在名单里：配合 ``regime_ma>0`` 时它就是「弱市全暂停」
#: （``#63`` 的 0.0 档），语义必须保留。
_ZERO_MEANS_OFF: tuple[str, ...] = (
    "entry_max_chase", "entry_pullback", "entry_window_minutes",
    "entry_confirm_minutes", "max_open_gain", "min_volume_ratio",
    "min_turnover_rate", "breakeven_trigger", "trail_pct", "partial_tp_at",
    "max_gain_5d", "stop_atr_mult", "queue_top_m", "t_crash_guard",
    # 0 分钟冷静期 = 仍用「次日开盘价」的历史口径；传 0 必须还原成 None，
    # 不能让 _open_delay_price(..., 0) 参与判定（虽然它也返回 None，但语义要统一）
    "entry_open_delay_minutes",
)

#: 内存任务表：job_id -> {status, created, elapsed, result, error}
#: 结果同时落库（db.save_backtest），这里只负责「进行中」的进度查询
_JOBS: dict[str, dict] = {}
#: 任务结果在内存里保留的秒数（超过后只能按 run_id 查历史）
_JOB_TTL = 3600.0


class BacktestRequest(BaseModel):
    """回测请求体：时间段必填；其余风控参数默认 None = 沿用线上配置。

    字段名与 :class:`app.backtest.pipeline.PipelineParams` 对齐——传入即覆盖
    本次回测（线上参数不受影响），用于参数实验与回归测试。

    同一份请求体也是 ``POST /backtest/persist`` 的入参：那个接口才把参数写成
    实盘配置（只取 :data:`app.backtest.apply.BACKTEST_TO_SETTING` 里映射得到的子集）。
    """

    start_date: str = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    # ---- 基础 ----
    initial_capital: float | None = Field(None, gt=0, description="初始资金，缺省取模拟盘配置")
    max_positions: int | None = Field(None, ge=1, le=50, description="最大持仓数，缺省取模拟盘配置")
    # 默认必须是 None：写死 500 会让 model_dump(exclude_none=True) 永远带上 500，
    # 把 /defaults 算出的线上池（paper_pool_top_n）覆盖掉——「未提交 = 沿用线上」就不成立了
    universe_size: int | None = Field(None, ge=10, le=6000, description="每日成交额 TopN 股票池")
    board_prefixes: list[str] | None = Field(None, description="只在这些代码前缀里选股；None=全市场")
    #: 策略集：默认（不传）由 /run 以 /defaults 为基底填入实盘 AUTO_SCAN_STRATEGIES。
    #: 显式传值只影响本次回测（策略子集实验）；空数组 = 全部已注册策略，与实盘空配置同义。
    strategies: list[str] | None = Field(
        None, description="参与扫描的策略名；None=跟随实盘 AUTO_SCAN_STRATEGIES，[]=全部注册策略"
    )
    watch_cap: int | None = Field(None, ge=10, le=500, description="自选股上限（FIFO）")
    add_per_day: int | None = Field(None, ge=1, le=200, description="每日扫描后加自选上限")
    # ---- 选股 / 买入 ----
    rebalance_days: int | None = Field(None, ge=1, le=20, description="调仓周期（交易日），1=每日")
    buy_threshold: float | None = Field(None, description="14因子买入阈值")
    buy_top_pct: float | None = Field(None, ge=0, le=1, description="买入分位门槛（前 X，替代绝对阈值）")
    max_open_gain: float | None = Field(None, description="次日开盘涨幅上限（不追高）")
    max_gain_5d: float | None = Field(None, description="近5日涨幅上限（不追已涨）")
    min_volume_ratio: float | None = Field(None, description="量比下限")
    min_turnover_rate: float | None = Field(None, description="换手率下限（%）")
    # ---- 五段式③④⑤：队列容量与切入点（None=不启用） ----
    queue_top_m: int | None = Field(None, ge=0, le=50, description="交易队列买入容量 topM")
    entry_max_chase: float | None = Field(None, description="不追高：次日开盘涨幅上限")
    entry_pullback: float | None = Field(None, description="回踩：要求低开此比例才买（0=不限）")
    entry_window_minutes: int | None = Field(
        None, ge=5, le=240, description="分钟级切入点窗口（需 intraday_minutes；窗口内等回踩）")
    invert_score: bool | None = Field(None, description="反转选股：分数取反")
    # ---- 止损 / 止盈 ----
    stop_loss: float | None = Field(None, gt=0, le=0.5, description="止损比例（相对成本）")
    take_profit: float | None = Field(None, gt=0, description="止盈比例（相对成本）")
    max_hold_days: int | None = Field(None, ge=0, le=250, description="时间止损（0=不限制）")
    stop_atr_mult: float | None = Field(None, gt=0, description="ATR 止损倍数（None=用固定 stop_loss）")
    stop_atr_min: float | None = Field(None, gt=0, description="ATR 止损距离下限")
    stop_atr_max: float | None = Field(None, gt=0, description="ATR 止损距离上限")
    stop_confirm_close: bool | None = Field(None, description="止损收盘确认（次日开盘卖）")
    partial_tp_at: float | None = Field(None, gt=0, description="分段止盈触发涨幅")
    partial_tp_ratio: float | None = Field(None, gt=0, le=1, description="分段止盈卖出比例")
    # ---- 移动 / 保本止损 ----
    breakeven_trigger: float | None = Field(None, gt=0, description="浮盈达此比例上移止损到成本（保本）")
    trail_pct: float | None = Field(None, gt=0, description="从高点回撤此比例即卖出（移动止损）")
    trail_arm: float | None = Field(None, ge=0, description="移动止损启用门槛（浮盈曾达）")
    # ---- 大盘择时 ----
    # ge 放到 0：0 = 关闭择时（pipeline 按 regime_on 判定）。原先 ge=2 会让
    # 「关掉择时」这个合法诉求只能靠不传字段表达，而 /run 现在以 defaults 为基底，
    # 不传就等于开——页面上就再也关不掉了。
    regime_ma: int | None = Field(None, ge=0, le=250, description="大盘择时均线周期（0=关闭）")
    regime_index: str | None = Field(None, description="弱市判定宽基指数代码（如 sh000985=中证全指）；None=跟随实盘 paper_regime_index")
    regime_fast_ma: int | None = Field(None, ge=2, le=250, description="择时快线周期")
    weak_exposure: float | None = Field(
        None, ge=0, le=1, description="弱市总仓位上限（需 regime_ma>0；0=弱市全暂停）"
    )
    regime_flat_below: bool | None = Field(None, description="最弱档清仓")
    regime_ramp_days: int | None = Field(None, ge=0, le=20, description="弱转强后爬坡天数（0=关闭）")
    dd_pause_at: float | None = Field(
        None, gt=0, le=1, description="组合回撤断路器阈值（净值回撤达到即暂停买入）"
    )
    dd_pause_days: int | None = Field(None, ge=1, le=120, description="暂停买入的交易日数")
    # ---- 仓位 / 评分 / 轮动 ----
    max_total_exposure: float | None = Field(None, gt=0, le=1, description="总仓位上限")
    single_position_ratio: float | None = Field(None, gt=0, le=1, description="单票仓位上限")
    full_score: float | None = Field(None, description="全额评分档")
    half_score: float | None = Field(None, description="半额评分档")
    rotation_enabled: bool | None = Field(None, description="满仓轮动开关")
    rotation_gap: float | None = Field(None, description="轮动分差")
    rotation_min_score: float | None = Field(None, description="轮动最低分")
    rotation_max_per_day: int | None = Field(
        None, ge=0, le=20, description="每日轮动次数上限（0 = 当日不轮动，可作对照组）"
    )
    # ---- 做T引擎（参数化主动交易） ----
    t_active: bool | None = Field(None, description="启用次日做T/分级网格")
    t_minute_path: bool | None = Field(
        None, description="做T 按当日 5m 序列推进（与线上同序，需分钟数据）"
    )
    t_minute_period: str | None = Field(None, description="做T 路径的分钟周期（5 / 1）")
    # ---- 开盘段成交价口径（E47：度量校准，不改策略） ----
    exec_fill: str | None = Field(
        None, description="开盘段成交价：open（开盘价）/ vwap1m（开盘后 N 分钟 1m 均价）"
    )
    exec_fill_minutes: int | None = Field(None, ge=1, le=240, description="VWAP 窗口分钟数")
    #: #108：实盘有「开盘冷静期」，回测原先只能靠 profiles 里的 cur10 写死 5，
    #: /run 与 /defaults 都表达不了这个口径 → 页面拿「次日开盘价」的结果评价实盘。
    #: 0 = 关闭（仍用开盘价，历史口径）；>0 = 取开盘后第 (N-1)//5 根 5m bar 收盘价。
    entry_open_delay_minutes: int | None = Field(
        None, ge=0, le=60,
        description="开盘冷静期(分钟)：>0 时入场价改为开盘后 N 分钟，与实盘 paper_open_cooldown_minutes 同口径；0=用开盘价",
    )
    # ---- 稳健性检验（E48）----
    fill_noise_bps: float | None = Field(
        None, ge=0, le=200, description="成交价随机扰动 σ（bp），0=关闭；仅回测用"
    )
    fill_noise_seed: int | None = Field(None, ge=0, description="扰动随机种子")
    t_size: float | None = Field(None, gt=0, le=1, description="单次做T比例（相对总仓）")
    t_up_sell: float | None = Field(None, gt=0, description="开盘涨幅超此先卖1/4锁利")
    t_up_buyback: float | None = Field(None, gt=0, description="从高点回调超此买回")
    t_down_add: float | None = Field(None, gt=0, description="开盘跌幅达此加1/4仓")
    t_down_full: float | None = Field(None, gt=0, description="跌幅达此加满仓")
    t_rebound_sell: float | None = Field(None, gt=0, description="反弹至跌幅小于此减1/4仓")
    t_limit_sell_half: bool | None = Field(None, description="涨停弱封板卖1/2")
    t_crash_guard: float | None = Field(None, gt=0, description="大盘当日跌幅超此暂停做T")
    # ---- 打分层（路线 A：ML 截面排序，记录 #74~#78） ----
    scoring_mode: str | None = Field(
        None, description="打分口径：factors=14因子 / ml=ML截面分位 / ml_blend=与14因子融合"
    )
    ml_model: str | None = Field(
        None, description="ML 模型：lightgbm（缺依赖自动回退 ridge）/ ridge"
    )
    ml_label_horizon: int | None = Field(None, ge=1, le=60, description="标签前瞻交易日数")
    ml_blend: float | None = Field(None, ge=0, le=1, description="ml_blend 下 ML 分位权重")
    ml_require_ic: float | None = Field(
        None, description="OOS rank-IC 门槛，低于则整段回退 14 因子（默认 0）"
    )
    industry_neutral: bool | None = Field(None, description="ML 分位做行业内中性化")
    # ---- 可交易性约束（记录 #73） ----
    disable_limit_mask: bool | None = Field(
        None, description="关闭涨跌停不可成交约束（仅用于量化 mask 贡献，线上永不开）"
    )
    # ---- P1 上下文风险闸（四项；0=关闭子闸，与实盘 Settings.paper_ctx_* 双向绑定）----
    context_enabled: bool | None = Field(None, description="四项上下文风险闸总开关")
    context_max_industry_weight: float | None = Field(
        None, ge=0, le=1, description="买入后同一行业只数/组合总只数上限；0=关闭")
    context_max_corr: float | None = Field(
        None, ge=0, le=1, description="与持仓组合 60 日收益平均绝对相关上限；0=关闭")
    context_hot_exclude_pct: float | None = Field(
        None, ge=0, le=0.5, description="剔除个股/行业热度最热比例；0=关闭")
    context_event_cooldown_days: int | None = Field(
        None, ge=0, le=30, description="事件日及之后 N 天禁买；0=关闭")
    context_news_block: float | None = Field(
        None, ge=0, le=1, description="加权舆情分 <= -此值禁买；0=关闭")


def _prune() -> None:
    """清理过期的**已结束**任务，避免内存表无限增长。

    running 的任务绝不清理：全池长区间回测实测要 65 分钟，原先按 3600s TTL
    一刀切会在下一次提交时把仍在跑的任务删掉，前端轮询直接 404、白等一场。
    """
    now = time.time()
    for job_id in [k for k, v in _JOBS.items()
                   if v["status"] != "running" and now - v["created"] > _JOB_TTL]:
        _JOBS.pop(job_id, None)


async def _execute(job_id: str, params: PipelineParams) -> None:
    """后台执行回测，完成后写入任务表与数据库。"""
    job = _JOBS[job_id]
    # 绑定进度 context：asyncio.to_thread 会把它复制进计算线程，pipeline 里的
    # progress.report 才知道自己属于哪个任务；影子账户等无 job 的调用链不受影响。
    token = bt_progress.bind(job_id, note=f"{params.start_date} ~ {params.end_date}")
    try:
        result = await run_pipeline(params)
        bt_progress.report("save", note="写入 sas.db 回测记录")
        elapsed = round(time.time() - job["created"], 1)
        # job_id / elapsed 一并写进结果再落库：事后拿日志里的 job_id 能直接定位到
        # 这条记录，不用靠时间戳猜；elapsed 也是「过程数据」的一部分。
        result["job_id"] = job_id
        result["elapsed"] = elapsed
        result["run_id"] = await asyncio.to_thread(db.save_backtest, result)
        job.update(status="done", result=result, elapsed=elapsed)
        bt_progress.mark(job_id, ok=True,
                         note=f"{len(result.get('trades', []))} 笔交易 / 耗时 {elapsed:.1f}s")
        logger.info(
            "流水线回测完成 %s（%s~%s）：%d 笔交易，耗时 %.1fs",
            job_id, params.start_date, params.end_date,
            len(result.get("trades", [])), job["elapsed"],
        )
    except Exception as exc:  # noqa: BLE001 - 失败原因回传给前端
        logger.exception("流水线回测执行失败 %s", job_id)
        job.update(
            status="error",
            error=str(exc),
            elapsed=round(time.time() - job["created"], 1),
        )
        bt_progress.mark(job_id, ok=False, note=str(exc))
    finally:
        bt_progress.unbind(token)


def _defaults_payload() -> dict:
    """回测参数与实盘对齐的默认值（两侧共用同一份计算，避免各自漂移）。

    关键约束：回测默认必须是实盘配置的 1:1 克隆——打开回测中心、不改动任何项的
    情况下点「开始回测」，模拟的就该是实盘正在跑的那套规则。板块 / 池子 / 自选容量 /
    做T日增上限 / ATR 止损下限一律从这里派生，绝不写死占位值（历史教训：写死的
    ``board_prefixes=[]`` 让回测默认全市场，而实盘只做创业板，结论根本无法解释实盘）。
    """
    s = get_settings()
    # 板块：严格跟随「选股+交易」统一板块 twin_t_boards，返回板块键（cyb/sh_main…），
    # 前端多选下拉与引擎 _expand_board_prefixes 都认；空=全市场（与 live 留空语义一致）。
    _board_keys = list(parse_trade_boards(s.twin_t_boards))
    # 成交额 TopN 池：跟随 paper_pool_top_n；0=实盘不限制→回测也应全市场，不能用 300 兜底，
    # 否则 live=全市场 时回测却只取 300，两边池子口径不一致。
    # 6000 ≥ 全A股约 5500 只，取满即「不限制」；也是 BacktestRequest.universe_size 的
    # le=6000 / 前端 max=6000 上限，写更大的数会让提交直接 400，故做 clamp。
    _pool_top_n = int(getattr(s, "paper_pool_top_n", 0) or 0)
    _universe_size = min(_pool_top_n, 6000) if _pool_top_n > 0 else 6000
    # 自选容量 / 每日加自选上限：严格跟随实盘 watch_auto_max / scan_auto_watch_limit，
    # 并 clamp 到 BacktestRequest 的 le=500 / le=200，避免实盘调大后回测提交 400。
    _watch_cap = min(int(getattr(s, "watch_auto_max", 0) or 0), 500)
    _add_per_day = min(int(getattr(s, "scan_auto_watch_limit", 0) or 0), 200)
    return {
        # 基础
        "initial_capital": float(s.paper_initial_capital),
        "max_positions": int(s.paper_max_positions),
        # 成交额 TopN 池：严格跟随实盘 paper_pool_top_n（0=不限制→全市场，见上方 _universe_size）
        "universe_size": _universe_size,
        # 板块：严格跟随「选股+交易」统一板块 twin_t_boards（见上方 _board_keys）
        "board_prefixes": _board_keys,
        # 策略集：跟随实盘 AUTO_SCAN_STRATEGIES（.env 只读项，当前 A-Only=量价共振2560）。
        # 实盘 scheduler 用「auto_scan_strategies or registry.names()」，即空数组=全部策略；
        # 回测 _pick_strategies 对空 wanted 同样返回全部策略，两侧语义 1:1。
        # 以前 defaults 不含此键 → PipelineParams 落回 None（14 策略全开），回测 #44
        # 因此跑成了「全池 + 14 策略」的研究性实验，根本不能代表 A-Only 实盘。
        "strategies": list(getattr(s, "auto_scan_strategies", None) or []),
        # 自选股容量：跟随实盘 watch_auto_max（程序自动自选上限，见上方 _watch_cap）
        "watch_cap": _watch_cap,
        # 每日加自选上限：跟随实盘 scan_auto_watch_limit（见上方 _add_per_day）
        "add_per_day": _add_per_day,
        # 选股 / 买入（0 = 不启用该过滤）
        "rebalance_days": 1,
        "buy_threshold": float(s.factor_buy_threshold),
        "buy_top_pct": 0.0,
        "max_open_gain": 0.0,
        "max_gain_5d": 0.0,
        "min_volume_ratio": 0.0,
        "min_turnover_rate": 0.0,
        "invert_score": False,
        # 五段式③⑤（0 = 不启用）
        "queue_top_m": int(s.paper_trade_queue_top_m),
        # 五段式⑤切入点：跟随实盘 paper_entry_*（之前写死 0，将来实盘改了回测默认自动同步）
        "entry_max_chase": float(s.paper_entry_max_chase),
        "entry_pullback": float(s.paper_entry_pullback),
        "entry_window_minutes": int(s.paper_entry_timeout_minutes),
        # 止损 / 止盈（实盘这三项已挪进 Settings，落盘后默认值同步变化）
        "stop_loss": float(s.paper_stop_loss),
        "take_profit": float(s.paper_take_profit),
        "max_hold_days": 0,
        "stop_atr_mult": float(s.paper_atr_stop_mult),
        # ATR 止损距离下限：实盘 _stop_distance 是 max(paper_stop_loss, min(dist, atr_max))，
        # 下限就是固定止损；回测公式 max(stop_atr_min, min(stop_atr_max, dist)) 结构相同，
        # 故 stop_atr_min 必须 = paper_stop_loss（原写死 0.03，与实盘 0.08 差一截）。
        # 注意别用 paper_regime_stop_min：那是机制层收紧下限，仅 shock/crisis 生效，回测未实现。
        "stop_atr_min": float(s.paper_stop_loss),
        "stop_atr_max": float(s.paper_atr_stop_max),
        "stop_confirm_close": False,
        "partial_tp_at": 0.0,
        "partial_tp_ratio": 0.5,
        # 移动 / 保本（跟随实盘 paper_*，不再硬编码 0）
        "breakeven_trigger": float(s.paper_breakeven_pct),
        "trail_pct": float(s.paper_trail_pct),
        "trail_arm": float(s.paper_trail_arm),
        # 大盘择时：跟随线上闸门。线上是 paper_regime_weak_pause=True + ma=60 + weak_exposure=0.3，
        # 这里写死 0 会让「默认回测」变成一套完全不做择时、始终顶到 80% 仓位的策略——
        # 与实盘跑的根本不是同一套规则，用户拿它判断实盘会得出反向结论（#79 §7）。
        "regime_ma": int(s.paper_regime_ma) if bool(s.paper_regime_weak_pause) else 0,
        "regime_index": str(s.paper_regime_index),
        "regime_fast_ma": 20,
        "weak_exposure": (
            float(s.paper_weak_exposure) if bool(s.paper_regime_weak_pause) else 0.0
        ),
        "regime_flat_below": False,
        "regime_ramp_days": 0,
        # 仓位 / 评分 / 轮动
        "max_total_exposure": float(s.paper_max_total_exposure),
        "single_position_ratio": float(s.paper_single_position_max),
        "full_score": float(s.paper_full_score),
        "half_score": float(s.paper_half_score),
        "rotation_enabled": bool(s.paper_rotation_enabled),
        "rotation_gap": float(s.paper_rotation_gap),
        "rotation_min_score": float(s.paper_rotation_min_score),
        "rotation_max_per_day": int(s.paper_rotation_max_per_day),
        # 做T：全部跟随实盘 paper_t_*（之前硬编码 False/0 会让回测默认与实盘脱节）
        "t_active": bool(s.paper_t_active),
        "t_minute_path": False,
        "t_minute_period": "5",
        "exec_fill": "open",
        "exec_fill_minutes": 30,
        # #108：回测入场价跟随实盘「开盘冷静期」，否则默认仍是次日开盘价、
        # 与线上「开盘+5min 才允许买」不是一次可比实验（实盘窗口=0 的偏离另见结果页口径提示）
        "entry_open_delay_minutes": int(getattr(s, "paper_open_cooldown_minutes", 0) or 0),
        "t_size": float(s.paper_t_size),
        "t_up_sell": float(s.paper_t_up_sell),
        "t_up_buyback": float(s.paper_t_up_buyback),
        "t_down_add": float(s.paper_t_down_add),
        "t_down_full": float(s.paper_t_down_full),
        "t_rebound_sell": float(s.paper_t_rebound_sell),
        "t_limit_sell_half": bool(s.paper_t_limit_sell_half),
        "t_crash_guard": float(s.paper_t_crash_guard),
        # ---- 打分层：默认跟随实盘开关，避免「回测中心」与实盘两处各说一套 ----
        # live_ml_scoring 打开时线上用 ML 分位排序，回测默认就该同口径；
        # 想跑 14 因子基线对照，把这个下拉改回 factors 即可（其余参数不动）。
        "scoring_mode": "ml" if bool(getattr(s, "live_ml_scoring", False)) else "factors",
        "ml_model": "lightgbm",
        "ml_label_horizon": 15,
        "ml_blend": 0.5,
        "ml_require_ic": 0.0,
        "industry_neutral": bool(getattr(s, "live_ml_industry_neutral", True)),
        "disable_limit_mask": False,
        # ---- P1 上下文风险闸：逐项 1:1 跟随实盘 Settings.paper_ctx_* ----
        # 绝不写死：用户改系统设置里任一项（或回测页落盘改回来），回测默认必须同步。
        # 四项的行为由 app.risk.context_gates 一份函数保证，参数由 BINDINGS 逐项探针保证。
        "context_enabled": bool(getattr(s, "paper_ctx_enabled", True)),
        "context_max_industry_weight": float(getattr(s, "paper_ctx_max_industry_weight", 0.40)),
        "context_max_corr": float(getattr(s, "paper_ctx_max_corr", 0.85)),
        "context_hot_exclude_pct": float(getattr(s, "paper_ctx_hot_exclude_pct", 0.20)),
        "context_event_cooldown_days": int(getattr(s, "paper_ctx_event_cooldown_days", 3)),
        "context_news_block": float(getattr(s, "paper_ctx_news_block", 0.60)),
    }


@router.get("/defaults")
def backtest_defaults() -> dict:
    """回测参数的**线上默认值**（前端表单直接用具体数字展示，不再显示「默认」占位）。

    ``PipelineParams`` 里为 ``None`` 的字段表示「沿用线上配置」，真正的数值分散在
    ``Settings.paper_*`` 与几个硬编码常量里（见 ``pipeline._simulate`` 开头的解析）。
    这里统一算一次吐给前端，避免前端再抄一份默认值导致漂移。

    约定：默认「关闭/不限」的参数返回 ``0``，前端在值为 0 时**不提交**该字段
    （与 ``PipelineParams`` 的 None 语义等价）。``bool`` 参数返回 ``False``。
    """
    from app.strategies import registry  # pipeline 导入时已触发 builtin 注册
    return with_disclaimer({
        "defaults": _defaults_payload(),
        # 策略集只读展示用：让页面能一眼看到「默认跟随实盘 A-Only」而不是 14 策略全开。
        "strategy_options": registry.names(),
        # 每次打开回测页都顺手验一次对齐契约：漂移会直接显示在页面顶部，
        # 不用等有人手动跑脚本才发现「回测和实盘已经不是一套规则」。
        "alignment": alignment_summary(),
    })


@router.get("/alignment")
def backtest_alignment() -> dict:
    """回测 ↔ 实盘 对齐自检报告（零漂移时 ``ok=True``）。

    检查「改实盘→回测默认是否跟着变」「新增参数是否登记」「落盘方向是否同源」
    「不落盘说明是否过时」四类问题。注意本路由必须排在 ``/{run_id}`` 之前，
    否则会被动态路由当作 run_id 解析成 422。
    """
    return with_disclaimer(audit_alignment())


@router.get("/persist-map")
def backtest_persist_map() -> dict:
    """哪些回测参数能落盘成实盘配置（以及当前实盘生效值 / 不能落盘的原因）。"""
    return with_disclaimer(persist_map())


@router.post("/persist")
def persist_backtest_rules(req: BacktestRequest) -> dict:
    """把回测参数落盘为实盘配置：写覆盖层 + 就地生效，重启保留（即新的默认值）。

    只写 ``apply.BACKTEST_TO_SETTING`` 里映射得到的字段；实盘没有对应实现的项
    （选股分位、调仓周期、分段止盈、分钟级切入点等）在 ``skipped`` 里说明原因，
    绝不硬塞进不相干的字段——否则就是「页面显示改了、实盘没变」。
    """
    changes = req.model_dump(exclude_none=True)
    # 只落盘调用方**显式提交**的项：未提交的沿用实盘现值，不因 Pydantic 默认值（如
    # universe_size=500）被动改掉实盘配置
    explicit = set(req.model_fields_set)
    changes = {k: v for k, v in changes.items() if k in explicit}
    changes.pop("start_date", None)
    changes.pop("end_date", None)
    applied, skipped = split_changes(changes)
    if not applied:
        raise HTTPException(status_code=400, detail="当前参数里没有可落盘到实盘的项")

    s = get_settings()
    before = {key: getattr(s, key, None) for key in applied}
    try:
        done = update_settings(applied)
    except ValueError as exc:  # 类型/下限不合法 → 400，前端原样展示
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    changed = [
        {"setting": key, "old": before.get(key), "new": value}
        for key, value in sorted(done.items())
        if before.get(key) != value
    ]
    logger.info("回测规则已落盘：变更 %d 项，跳过 %d 项", len(changed), len(skipped))
    return with_disclaimer({
        "applied": done,
        "changed": changed,
        "skipped": skipped,
        "defaults": _defaults_payload(),
    })


def build_params(req: BacktestRequest) -> PipelineParams:
    """「线上默认值为基底 + 请求显式覆盖」→ :class:`PipelineParams` 的**唯一入口**。

    ``/run`` 与本地严格对齐脚本都调这里：合并逻辑、0→None 还原、list→tuple 折算
    只写一份，避免「系统里点开始回测」和「脚本复跑」各抄一遍而产生口径漂移。
    """
    kwargs = {**_defaults_payload(), **req.model_dump(exclude_none=True)}
    # defaults 里的 0 = 「不启用」，这里换成 None 才是 PipelineParams 认的「不启用」
    for _k in _ZERO_MEANS_OFF:
        _v = kwargs.get(_k)
        if _v is not None and not isinstance(_v, bool) and float(_v) == 0.0:
            kwargs[_k] = None
    bp = kwargs.pop("board_prefixes", None)
    if bp is not None:
        kwargs["board_prefixes"] = tuple(bp)
    # 策略集同理：请求体/defaults 是 list，PipelineParams 声明 tuple。
    # 空数组保持 ()，_pick_strategies 会按「全部注册策略」处理（与实盘空配置同义）。
    sg = kwargs.get("strategies")
    if isinstance(sg, list):
        kwargs["strategies"] = tuple(sg)
    return PipelineParams(**kwargs)


@router.post("/run")
async def run_backtest(req: BacktestRequest) -> dict:
    """提交全流水线回测任务，立即返回 job_id（不阻塞 HTTP 连接）。"""
    if req.start_date >= req.end_date:
        raise HTTPException(status_code=400, detail="start_date 必须早于 end_date")
    # 策略名提前校验：_pick_strategies 默认会抛 RuntimeError，变成「任务失败」，
    # 不如在提交时给出 400 + 可选列表（pipeline 导入时已触发 builtin 注册）。
    if req.strategies:
        from app.strategies import registry
        known = registry.names()
        unknown = [n for n in req.strategies if n not in known]
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"未知策略 {unknown}，可选：{known}"
            )

    _prune()
    # 同一时刻只跑一个回测（避免重复点击/多标签页把 CPU 打满）；
    # 已有任务在跑时直接复用，前端继续轮询同一个 job。
    for job_id, job in _JOBS.items():
        if job["status"] == "running":
            return {"job_id": job_id, "status": "running", "reused": True}

    # 只用 req.model_dump(exclude_none=True) 的话，没传的字段会落到 PipelineParams 的
    # dataclass 默认（scoring_mode=None→factors、regime_ma=0→完全不择时），
    # 与前端表单（由 /defaults 填满后整体提交）跑出来的根本不是同一套参数。
    # 合并/折算统一走 build_params，脚本复跑也是同一入口。
    params = build_params(req)
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {
        "status": "running",
        "created": time.time(),
        "elapsed": 0.0,
        "result": None,
        "error": "",
    }
    asyncio.create_task(_execute(job_id, params))
    return {"job_id": job_id, "status": "running"}


@router.get("/jobs/{job_id}")
def get_backtest_job(job_id: str) -> dict:
    """查询回测任务状态；done 时带上完整结果。"""
    job = _JOBS.get(job_id)
    if job is None:
        # 任务表在内存里：服务重启即清空，正在跑的回测线程也随之终止。
        # 说清原因，避免前端把 404 当成「参数/接口坏了」或显示成天书。
        raise HTTPException(
            status_code=404,
            detail="回测任务不存在或已过期（服务重启会清空内存任务表，正在运行的回测不会跨重启保留，请重新运行；已完成的回测仍在「历史回测」中）",
        )
    # running 时 job["elapsed"] 一直是 0（只有结束才写），前端以前只能自己掐秒表，
    # 刷新页面就归零；改成按 created 实时算，与页面是否刷新无关。
    elapsed = (job["elapsed"] if job["status"] != "running"
               else round(time.time() - job["created"], 1))
    payload: dict = {
        "job_id": job_id,
        "status": job["status"],
        "elapsed": elapsed,
        "error": job["error"],
    }
    prog = bt_progress.snapshot()
    if prog.get("job_id") == job_id:
        payload["progress"] = prog
    if job["status"] == "done" and job["result"] is not None:
        payload["result"] = with_disclaimer(job["result"])
    return payload


@router.get("/{run_id}")
def get_backtest(run_id: int) -> dict:
    """查询历史回测结果。

    ``cache.get_backtest()`` 对「归因功能上线前」的老记录会把空列解成 ``{}``。
    空对象在 JS 里是 truthy，前端 ``v-if="result.review"`` 会进入并访问
    ``review.cost.fee_total.toFixed(0)`` → 渲染抛异常 → 整页白屏（实测 43 条里
    38 条命中）。这里在 API 边界把空/残缺 review 归一成 ``None``，数据库层
    （含 ``test_backtest_store`` 对老库 ``review == {}`` 的断言）保持不变。
    """
    result = db.get_backtest(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    review = result.get("review")
    if (not isinstance(review, dict) or not review
            or not isinstance(review.get("cost"), dict)
            or not isinstance(review.get("reasons"), list)
            or not isinstance(review.get("hold_buckets"), list)):
        result["review"] = None
    return with_disclaimer(result)


@router.get("")
def list_backtests(limit: int = Query(50, ge=1, le=200)) -> dict:
    """历史回测记录列表。"""
    return with_disclaimer({"runs": db.list_backtests(limit=limit)})
