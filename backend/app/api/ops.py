"""运维/复盘只读接口：线上 ML 生效状态、复盘文档、影子账户、涨跌广度历史。

设计约束（安全）：
- **不接受用户路径**：复盘文档用固定白名单、影子账户 tag 只允许 ``[A-Za-z0-9_-]``，
  服务端拼绝对路径后再校验落在预期目录内；
- 文件缺失/损坏一律返回 ``available=false`` + 说明，不抛异常栈、不 500；
- 只读，不触发训练 / 复盘脚本 / 影子账户重跑；沿用 ``API_TOKEN`` 守卫与 disclaimer。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.api.market import _minute_export_status  # 复用同一份清单解析，避免两处口径分叉
from app.api.utils import with_disclaimer
from app.config import BASE_DIR, get_settings

logger = logging.getLogger("sas.api.ops")
router = APIRouter(prefix="/ops", tags=["运维复盘"])

#: 复盘文档白名单（脚本产出，见 docs/README.md）；value 是固定文件名，不是用户可传路径
_REVIEW_FILES = {
    "daily": "daily_trade_review.md",
    "param": "param_advisor.md",
    "position": "position_pnl.md",
}
_REVIEW_DIR = BASE_DIR / "docs"
_MAX_REVIEW_BYTES = 512 * 1024
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _fmt_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN -> None


def _check(key: str, label: str, level: str, detail: str, hint: str = "") -> dict:
    return {"key": key, "label": label, "level": level, "detail": detail, "hint": hint}


def _date_lag(value: str | None, fmt: str = "%Y%m%d") -> int | None:
    try:
        return (datetime.now().date() - datetime.strptime(str(value), fmt).date()).days
    except (TypeError, ValueError):
        return None


@router.get("/self-check")
def self_check() -> dict:
    """系统自检汇总：库结构/净值、行情快照、调度器、分钟库、影子账户、ML。

    只读、不跑测试、不触发任务。目的是把「页面看着正常但某个环节已经静默坏掉」
    （例如 `sim_lots` 漏列、分钟库断更、快照一直陈旧）变成一屏可见的红黄灯。
    """
    from app.data.cache import db as cache_db
    from app.data.snapshot_store import snapshot_store
    from app.monitor.scheduler import is_trading_day, scheduler

    checks: list[dict] = [_check("api", "接口服务", "ok", "可访问；访问令牌守卫生效")]

    # 数据库：表结构 + 批次溯源列 + 净值最新日期（#115 漏列曾让并行账本静默停止交易）
    try:
        path = str(cache_db.path.as_posix())
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            lot_cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_lots)")}
            latest_equity = conn.execute("SELECT MAX(date) FROM paper_equity").fetchone()[0]
        missing = {"paper_equity", "sim_lots", "paper_trades", "app_state"} - tables
        lag = _date_lag(latest_equity, "%Y-%m-%d") if latest_equity else None
        if missing or "provenance" not in lot_cols:
            checks.append(_check(
                "db", "数据库结构", "error",
                f"缺表 {sorted(missing)}；sim_lots.provenance={'有' if 'provenance' in lot_cols else '无'}",
                "检查 app/data/cache.py 的 DDL 与迁移",
            ))
        else:
            checks.append(_check(
                "db", "数据库结构", "ok",
                f"关键表齐全 · sim_lots.provenance 有 · 净值最新 {latest_equity or '无'}",
            ))
        if lag is not None and lag > 4:
            checks.append(_check(
                "equity", "净值记录", "warn", f"最新净值距今 {lag} 天（{latest_equity}）",
                "确认 15:40 结算与盘中 30s 快照任务",
            ))
        else:
            checks.append(_check("equity", "净值记录", "ok", f"最新 {latest_equity or '无'}"))
    except Exception as exc:  # noqa: BLE001 - 自检本身不能拖垮页面
        checks.append(_check("db", "数据库结构", "error", f"读取失败：{exc}", "检查 DB 路径与权限"))

    # 行情快照
    try:
        st = snapshot_store.status()
        trading = bool(is_trading_day())
        fresh = st["snapshot"]["rows"] > 0 and st["indices"]["rows"] > 0
        stale = trading and (st["snapshot"]["stale"] or st["indices"]["stale"])
        age = st["snapshot"]["age_seconds"]
        checks.append(_check(
            "snapshot", "行情快照",
            "error" if not fresh else "warn" if stale else "ok",
            f"个股 {st['snapshot']['rows']} 只 / 指数 {st['indices']['rows']} 条 · "
            f"快照已过 {age if age is not None else '—'}s",
            "陈旧时检查数据源与 60s 刷新任务" if stale else "",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("snapshot", "行情快照", "error", f"状态读取失败：{exc}"))

    # 进程内调度器
    try:
        sched = scheduler.status()
        running = bool(sched.get("running"))
        checks.append(_check(
            "scheduler", "调度任务", "ok" if running else "warn",
            f"running={running} · {len(sched.get('jobs', []))} 个任务",
            "未运行则检查服务启动日志" if not running else "",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("scheduler", "调度任务", "warn", f"状态读取失败：{exc}"))

    # 本地分钟库导出
    minute = _minute_export_status()
    minute_lag = _date_lag(minute.get("latest_date"))
    checks.append(_check(
        "minute", "分钟线导出",
        "ok" if minute_lag is not None and minute_lag <= 4 else "warn",
        f"最新 {minute.get('latest_date') or '无'} · 导出于 {minute.get('exported_at') or '—'}",
        "每交易日 17:30 的 SASMinutesFsdb" if minute_lag is None or minute_lag > 4 else "",
    ))

    # 影子账户
    log_dir = Path(get_settings().kline_dir).parent / "logs"
    shadow_ok = (log_dir / "shadow_live.json").is_file()
    checks.append(_check(
        "shadow", "影子账户", "ok" if shadow_ok else "warn",
        "shadow_live.json 存在" if shadow_ok else "尚无产物",
        "每交易日 16:45" if not shadow_ok else "",
    ))

    # 线上 ML：待运行/未开启都不算故障，回退才提示
    ml = ml_status()  # 复用 /ml-status 的口径文案，别在自检里再写一份
    checks.append(_check(
        "ml", "线上 ML",
        "warn" if ml["state"] == "fallback" else "ok",
        f"{ml['state']} · {ml['note']}",
        "effective=false 时不能当作 ML 有效的证据" if not ml["effective"] else "",
    ))

    checks.append(_check(
        "tests", "单测基线", "info", "不在接口内执行（约 35s）",
        "命令：.\\.venv\\Scripts\\python.exe -m pytest tests -q",
    ))

    levels = [c["level"] for c in checks]
    level = "error" if "error" in levels else "warn" if "warn" in levels else "ok"
    return with_disclaimer({
        "level": level,
        "checks": checks,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "只读汇总；非交易日的分钟库/净值落后 1~3 天属正常，按 warn 提示而非故障",
    })


@router.get("/ml-status")
def ml_status() -> dict:
    """线上 ML 打分当前是否**真的生效**（不是「开关开没开」）。

    ``effective=false`` 且 ``state=fallback`` 说明本日回退 14 因子，
    此时任何「ML 有效/无效」的结论都不成立（BACKTEST_LOG #82）。
    ``state=pending`` 只表示本进程重启后还没跑过一轮分析，不等于回退。
    """
    from app.analysis.live_ml import live_ml_status

    info = live_ml_status()
    info["note"] = {
        "off": "live_ml_scoring 未开启，打分走 14 因子",
        "effective": "本进程已构建当日 ML 预测，打分使用 ML 截面分位",
        "pending": "开关已开，但本进程重启后尚未跑过一轮 ML 打分；不能据此判断回退",
        "fallback": "本轮 ML 无信号（样本不足 / OOS IC≤0 / 构建失败），已回退 14 因子",
    }.get(info["state"], "")
    return with_disclaimer(info)


@router.get("/review")
def review(name: str = Query("daily", description="daily / param / position")) -> dict:
    """读一份脚本产出的复盘 Markdown（原文，不做渲染，避免新增前端依赖）。"""
    filename = _REVIEW_FILES.get(name)
    if not filename:
        raise HTTPException(status_code=400, detail=f"未知复盘文档: {name}")
    path = (_REVIEW_DIR / filename).resolve()
    if _REVIEW_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="路径越界")
    if not path.is_file():
        return with_disclaimer({
            "name": name, "available": False, "content": "", "updated_at": None,
            "note": f"{filename} 尚未生成（由每日复盘脚本写入）",
        })
    try:
        size = path.stat().st_size
        truncated = size > _MAX_REVIEW_BYTES
        content = path.read_bytes()[:_MAX_REVIEW_BYTES].decode("utf-8", "replace")
    except OSError as exc:
        logger.debug("复盘文档读取失败 %s: %s", filename, exc)
        return with_disclaimer({
            "name": name, "available": False, "content": "", "updated_at": None,
            "note": "读取失败",
        })
    return with_disclaimer({
        "name": name,
        "available": True,
        "content": content,
        "updated_at": _fmt_mtime(path),
        "truncated": truncated,
        "note": "脚本产出，非实时；以文档内日期为准",
    })


@router.get("/shadow")
def shadow(tag: str = Query("live", description="影子账户标签")) -> dict:
    """影子账户各方案最新快照 + default / default_ml 的前向超额对比。"""
    if not _TAG_RE.fullmatch(tag):
        raise HTTPException(status_code=400, detail="非法 tag")
    log_dir = Path(get_settings().kline_dir).parent / "logs"
    path = (log_dir / f"shadow_{tag}.json").resolve()
    if log_dir.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="路径越界")
    if not path.is_file():
        return with_disclaimer({
            "tag": tag, "available": False, "profiles": [], "ml_pair": None,
            "note": "影子账户尚无产物（每交易日 16:45 首次运行后生成）",
        })
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("影子账户读取失败 %s: %s", path, exc)
        return with_disclaimer({
            "tag": tag, "available": False, "profiles": [], "ml_pair": None,
            "note": "影子账户文件损坏",
        })

    profiles = data.get("profiles") or {}
    rows = []
    for name, item in profiles.items():
        snaps = (item or {}).get("snapshots") or []
        last = snaps[-1] if snaps else {}
        diag = last.get("ml_diagnostics") or {}
        rows.append({
            "name": str(name),
            "as_of": last.get("as_of"),
            "total_return": _num(last.get("total_return")),
            "benchmark_return": _num(last.get("benchmark_return")),
            "excess": _num(last.get("excess")),
            "max_drawdown": _num(last.get("max_drawdown")),
            "trade_count": _num(last.get("trade_count")),
            "win_rate": _num(last.get("win_rate")),
            "snapshots": len(snaps),
            # 打分层判定（#109 后影子快照才落这个字段）；旧快照一律 unknown，
            # 「没有字段」不能当成「ML 回退」
            "ml_diagnostics_available": bool(diag),
            "ml_effective": bool(diag.get("effective")) if diag else None,
            "ml_mode": diag.get("mode") if diag else None,
            "ml_oos_rank_ic": _num(diag.get("oos_rank_ic")) if diag else None,
            "ml_pred_pairs": _num(diag.get("pred_pairs")) if diag else None,
        })
    rows.sort(key=lambda r: r["name"])
    base = next((r for r in rows if r["name"] == "default"), None)
    ml = next((r for r in rows if r["name"] == "default_ml"), None)
    ml_pair = None
    if base and ml and base["excess"] is not None and ml["excess"] is not None:
        ml_pair = {
            "base_excess": base["excess"],
            "ml_excess": ml["excess"],
            "diff": round(ml["excess"] - base["excess"], 2),
        }
    # 两方案数值一模一样时不要让人读出「ML 没超额」的结论：可能是 OOS IC≤0 整段回退
    # （#79/#82）、参数没被影子 pipeline 吃到，也可能只是窗口太短、成交路径没分化。
    pair_identical = bool(
        base and ml
        and base["total_return"] == ml["total_return"]
        and base["trade_count"] == ml["trade_count"]
    )
    if ml is None:
        ml_state, ml_note = "absent", "无 default_ml 方案"
    elif not ml["ml_diagnostics_available"]:
        ml_state, ml_note = "unknown", "历史快照无 ML 判定字段"
    elif ml["ml_effective"]:
        ml_state, ml_note = "effective", "ML 生效"
    elif ml["ml_mode"] in ("ml", "ml_blend"):
        ml_state, ml_note = "fallback", "ML 已回退 14 因子"
    else:
        ml_state, ml_note = "factors", "14 因子方案"
    return with_disclaimer({
        "tag": tag,
        "available": True,
        "start": data.get("start"),
        "universe": data.get("universe"),
        "profiles": rows,
        "ml_pair": ml_pair,
        "pair_identical": pair_identical,
        "ml_state": ml_state,
        "ml_note": ml_note,
        "updated_at": _fmt_mtime(path),
        "note": (
            "default_ml 未在当前影子方案集内，无法做 ML 前向配对"
            if ml is None else
            "最新快照没有 ml_diagnostics 字段（改动前的产物），无法区分 ML 回退与生效；下个交易日 16:45 重跑后补齐"
            if ml_state == "unknown" and pair_identical else
            "ML 本段 OOS IC ≤ 0，已整段回退 14 因子，两方案相同不能当作配对结论（#79/#82）"
            if ml_state == "fallback" and pair_identical else
            "ML 已生效但两方案最新快照仍完全相同：窗口内成交路径未分化，样本不足以判读，不能得出「ML 无超额」"
            if ml_state == "effective" and pair_identical else
            "两方案最新快照完全相同（收益/成交笔数一致）：通常说明 ML 本轮回退或参数未生效，不能当作配对结论（#79/#82）"
            if pair_identical else
            "超额为相对基准，配对差 = default_ml − default"
        ),
    })


@router.get("/breadth")
def breadth_history(limit: int = Query(60, ge=1, le=500)) -> dict:
    """涨跌家数广度历史（纯观测，不参与任何交易判定，见 BACKTEST_LOG #86）。"""
    from app.data import breadth

    df = breadth.load()
    if df is None or df.empty:
        return with_disclaimer({
            "available": False, "rows": [],
            "note": "广度缓存尚未生成（每日 16:00 刷新）",
        })
    rows = []
    for day, row in df.tail(limit).iterrows():
        rows.append({
            "date": str(day)[:10],
            "up": _num(row.get("up")),
            "down": _num(row.get("down")),
            "flat": _num(row.get("flat")),
            "total": _num(row.get("total")),
            "up_ratio": _num(row.get("up_ratio")),
        })
    return with_disclaimer({
        "available": True,
        "rows": rows,
        "note": "纯观测：4 年非重叠只有 5 个 shock 样本、方向不稳，不进任何判定（#86）",
    })
