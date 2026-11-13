"""回测任务进度上报（**纯观测层**）。

设计约束（改动前请先读完）：

* 本模块**不参与任何回测/实盘规则计算**：不改 ``PipelineParams``、不改 ``_defaults_payload``、
  不进出 ``alignment.BINDINGS``，只把「当前跑到哪一步」写进内存供 ``/jobs`` 查询。
  因此回测与实盘仍然严格 1:1 对齐，``check_backtest_alignment.py`` 应保持 0 漂移。
* 后端同一时刻只允许一个 running 的回测任务（见 ``api/backtest.py`` 的复用逻辑），
  所以进度用单例状态即可，无需按任务建表。
* 影子账户、复盘脚本、CLI 也会调用 ``run_pipeline``，它们没有 job_id，绝不能污染
  正在跑的回测进度。这里用 ``contextvars`` 绑定 job_id：``asyncio.to_thread`` 会把
  当前 context 复制进工作线程，只有从 ``_execute`` 派生出去的那条计算链能命中
  ``report()``；其他调用链的 context 里没有 job_id，直接静默返回。
* 所有写入都加锁（工作线程写、HTTP 线程读），快照返回副本，避免字典被边遍历边改。
"""
from __future__ import annotations

import contextvars
import threading
import time
from typing import Any

#: 当前 context 绑定的回测任务 id；``asyncio.to_thread`` 会把它带进工作线程
_current_job: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sas_backtest_job", default=""
)

# phase -> (中文阶段名, 起始百分比, 本阶段占比)
# 进度条按「阶段基线 + 阶段内完成比」推进；命中 prepared 缓存跳过 prepare/signals 时，
# 百分比只会向前跳，不会倒退。ML 训练无法细分内部进度，故单独占一段并给出 note。
_STAGES: dict[str, tuple[str, float, float]] = {
    "queued": ("排队中", 0.0, 0.0),
    "load": ("读取本地K线", 0.0, 5.0),
    "indicators": ("计算技术指标", 5.0, 15.0),
    "ml": ("训练ML排序层", 20.0, 10.0),
    "signals": ("逐日扫描选股", 30.0, 35.0),
    "simulate": ("逐日交易模拟", 65.0, 33.0),
    "analyze": ("归因与结果整理", 98.0, 1.5),
    "save": ("写入回测记录", 99.5, 0.5),
    "done": ("已完成", 100.0, 0.0),
    "error": ("执行失败", 0.0, 0.0),
}

_lock = threading.Lock()
_state: dict[str, Any] = {}


def _blank(job_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "job_id": job_id,
        "phase": "queued",
        "phase_label": _STAGES["queued"][0],
        "done": 0,
        "total": 0,
        "percent": 0.0,
        "step": "",
        "note": "",
        "elapsed": 0.0,
        "started_at": now,
        "updated_at": now,
    }


def bind(job_id: str, note: str = "") -> Any:
    """在提交任务的协程里绑定 job_id 并重置进度状态，返回 contextvars token。"""
    with _lock:
        _state.clear()
        _state.update(_blank(job_id))
        if note:
            _state["note"] = note
    return _current_job.set(job_id)


def unbind(token: Any) -> None:
    _current_job.reset(token)


def report(
    phase: str,
    *,
    done: int | None = None,
    total: int | None = None,
    step: str = "",
    note: str = "",
) -> None:
    """上报一次进度；未绑定 job_id（影子任务 / CLI / 复盘脚本）时静默忽略。"""
    jid = _current_job.get()
    if not jid:
        return
    label, base, weight = _STAGES.get(phase, (phase, 0.0, 0.0))
    with _lock:
        if _state.get("job_id") != jid:
            return
        changed_phase = _state["phase"] != phase
        if changed_phase:
            # 换阶段必须重置阶段内计数，否则上一天的 N/250 会串到下一段
            d, t = int(done or 0), int(total or 0)
        else:
            d = _state["done"] if done is None else int(done)
            t = _state["total"] if total is None else int(total)
        frac = (d / t) if t > 0 else 0.0
        pct = base + weight * min(1.0, max(0.0, frac))
        _state.update({
            "phase": phase,
            "phase_label": label,
            "done": d,
            "total": t,
            "percent": round(min(100.0, pct), 1),
            "step": step,
            "note": note,
            "elapsed": round(time.time() - _state["started_at"], 1),
            "updated_at": time.time(),
        })


def mark(job_id: str, ok: bool, note: str = "") -> None:
    """任务结束（成功 / 失败）打终态，供最后一帧轮询与排障读取。"""
    with _lock:
        if _state.get("job_id") != job_id:
            return
        phase = "done" if ok else "error"
        _state.update({
            "phase": phase,
            "phase_label": _STAGES[phase][0],
            "percent": 100.0 if ok else _state["percent"],
            "note": note,
            "elapsed": round(time.time() - _state["started_at"], 1),
            "updated_at": time.time(),
        })


def snapshot() -> dict[str, Any]:
    """给 ``GET /backtest/jobs/{id}`` 用的只读快照（不暴露内部时间戳）。"""
    with _lock:
        if not _state:
            return {}
        data = {k: v for k, v in _state.items() if k not in ("started_at", "updated_at")}
        if data["phase"] not in ("done", "error"):
            data["elapsed"] = round(time.time() - _state["started_at"], 1)
        return data
