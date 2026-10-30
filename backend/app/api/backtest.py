"""回测 API：提交「全流水线回测」（异步任务 + 轮询）、查询进度与历史记录。

回测固定复刻线上模拟盘的完整链路（全部策略扫描 → 加自选 → 14因子排序
→ 评分加权下单），不再单独选策略，只由用户指定时间段。

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

from app.analysis.factors import BUY_THRESHOLD
from app.api.utils import with_disclaimer
from app.backtest.apply import persist_map, split_changes
from app.backtest.pipeline import PipelineParams, run_pipeline
from app.config import get_settings
from app.data.cache import db
from app.param_specs import update_settings

logger = logging.getLogger("sas.api.backtest")
router = APIRouter(prefix="/backtest", tags=["回测"])

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
    universe_size: int = Field(500, ge=10, le=6000, description="每日成交额 TopN 股票池")
    board_prefixes: list[str] | None = Field(None, description="只在这些代码前缀里选股；None=全市场")
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
    regime_ma: int | None = Field(None, ge=2, le=250, description="大盘择时均线周期")
    regime_fast_ma: int | None = Field(None, ge=2, le=250, description="择时快线周期")
    weak_exposure: float | None = Field(None, gt=0, le=1, description="弱市总仓位上限（需 regime_ma）")
    regime_flat_below: bool | None = Field(None, description="最弱档清仓")
    # ---- 仓位 / 评分 / 轮动 ----
    max_total_exposure: float | None = Field(None, gt=0, le=1, description="总仓位上限")
    single_position_ratio: float | None = Field(None, gt=0, le=1, description="单票仓位上限")
    full_score: float | None = Field(None, description="全额评分档")
    half_score: float | None = Field(None, description="半额评分档")
    rotation_enabled: bool | None = Field(None, description="满仓轮动开关")
    rotation_gap: float | None = Field(None, description="轮动分差")
    rotation_min_score: float | None = Field(None, description="轮动最低分")
    # ---- 做T引擎（参数化主动交易） ----
    t_active: bool | None = Field(None, description="启用次日做T/分级网格")
    t_size: float | None = Field(None, gt=0, le=1, description="单次做T比例（相对总仓）")
    t_up_sell: float | None = Field(None, gt=0, description="开盘涨幅超此先卖1/4锁利")
    t_up_buyback: float | None = Field(None, gt=0, description="从高点回调超此买回")
    t_down_add: float | None = Field(None, gt=0, description="开盘跌幅达此加1/4仓")
    t_down_full: float | None = Field(None, gt=0, description="跌幅达此加满仓")
    t_rebound_sell: float | None = Field(None, gt=0, description="反弹至跌幅小于此减1/4仓")
    t_limit_sell_half: bool | None = Field(None, description="涨停弱封板卖1/2")
    t_crash_guard: float | None = Field(None, gt=0, description="大盘当日跌幅超此暂停做T")


def _prune() -> None:
    """清理过期任务，避免内存表无限增长。"""
    now = time.time()
    for job_id in [k for k, v in _JOBS.items() if now - v["created"] > _JOB_TTL]:
        _JOBS.pop(job_id, None)


async def _execute(job_id: str, params: PipelineParams) -> None:
    """后台执行回测，完成后写入任务表与数据库。"""
    job = _JOBS[job_id]
    try:
        result = await run_pipeline(params)
        result["run_id"] = await asyncio.to_thread(db.save_backtest, result)
        job.update(
            status="done",
            result=result,
            elapsed=round(time.time() - job["created"], 1),
        )
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


def _defaults_payload() -> dict:
    """回测参数与实盘对齐的默认值（两侧共用同一份计算，避免各自漂移）。"""
    s = get_settings()
    return {
        # 基础
        "initial_capital": float(s.paper_initial_capital),
        "max_positions": int(s.paper_max_positions),
        "universe_size": 500,
        "board_prefixes": [],
        "watch_cap": 60,
        "add_per_day": 50,
        # 选股 / 买入（0 = 不启用该过滤）
        "rebalance_days": 1,
        "buy_threshold": float(BUY_THRESHOLD),
        "buy_top_pct": 0.0,
        "max_open_gain": 0.0,
        "max_gain_5d": 0.0,
        "min_volume_ratio": 0.0,
        "min_turnover_rate": 0.0,
        "invert_score": False,
        # 五段式③⑤（0 = 不启用）
        "queue_top_m": 0,
        "entry_max_chase": 0.0,
        "entry_pullback": 0.0,
        "entry_window_minutes": 0,
        # 止损 / 止盈（实盘这三项已挪进 Settings，落盘后默认值同步变化）
        "stop_loss": float(s.paper_stop_loss),
        "take_profit": float(s.paper_take_profit),
        "max_hold_days": 0,
        "stop_atr_mult": 0.0,
        "stop_atr_min": 0.03,
        "stop_atr_max": 0.15,
        "stop_confirm_close": False,
        "partial_tp_at": 0.0,
        "partial_tp_ratio": 0.5,
        # 移动 / 保本
        "breakeven_trigger": 0.0,
        "trail_pct": 0.0,
        "trail_arm": 0.0,
        # 大盘择时
        "regime_ma": 0,
        "regime_fast_ma": 20,
        "weak_exposure": 0.0,
        "regime_flat_below": False,
        # 仓位 / 评分 / 轮动
        "max_total_exposure": float(s.paper_max_total_exposure),
        "single_position_ratio": float(s.paper_single_position_max),
        "full_score": float(s.paper_full_score),
        "half_score": float(s.paper_half_score),
        "rotation_enabled": bool(s.paper_rotation_enabled),
        "rotation_gap": float(s.paper_rotation_gap),
        "rotation_min_score": float(s.paper_rotation_min_score),
        # 做T（0 / False = 关闭）
        "t_active": False,
        "t_size": 0.0,
        "t_up_sell": 0.0,
        "t_up_buyback": 0.0,
        "t_down_add": 0.0,
        "t_down_full": 0.0,
        "t_rebound_sell": 0.0,
        "t_limit_sell_half": False,
        "t_crash_guard": 0.0,
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
    return with_disclaimer({"defaults": _defaults_payload()})


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


@router.post("/run")
async def run_backtest(req: BacktestRequest) -> dict:
    """提交全流水线回测任务，立即返回 job_id（不阻塞 HTTP 连接）。"""
    if req.start_date >= req.end_date:
        raise HTTPException(status_code=400, detail="start_date 必须早于 end_date")

    _prune()
    # 同一时刻只跑一个回测（避免重复点击/多标签页把 CPU 打满）；
    # 已有任务在跑时直接复用，前端继续轮询同一个 job。
    for job_id, job in _JOBS.items():
        if job["status"] == "running":
            return {"job_id": job_id, "status": "running", "reused": True}

    # 所有非 None 字段透传给 PipelineParams（None=沿用线上配置）；
    # board_prefixes 在请求体里是 list，PipelineParams 要 tuple。
    kwargs = req.model_dump(exclude_none=True)
    bp = kwargs.pop("board_prefixes", None)
    if bp is not None:
        kwargs["board_prefixes"] = tuple(bp)
    params = PipelineParams(**kwargs)
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
        raise HTTPException(status_code=404, detail="回测任务不存在或已过期")
    payload: dict = {
        "job_id": job_id,
        "status": job["status"],
        "elapsed": job["elapsed"],
        "error": job["error"],
    }
    if job["status"] == "done" and job["result"] is not None:
        payload["result"] = with_disclaimer(job["result"])
    return payload


@router.get("/{run_id}")
def get_backtest(run_id: int) -> dict:
    """查询历史回测结果。"""
    result = db.get_backtest(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return with_disclaimer(result)


@router.get("")
def list_backtests(limit: int = Query(50, ge=1, le=200)) -> dict:
    """历史回测记录列表。"""
    return with_disclaimer({"runs": db.list_backtests(limit=limit)})
