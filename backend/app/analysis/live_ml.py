"""线上 ML 打分（default_ml 实时版）编排层。

对外只暴露 :func:`get_live_ml_scores`：给定一批股票代码，返回
``{code: 当日ML截面分位(0~100)}``，供 ``recommender`` 替换 14 因子分。

设计要点
--------
- 仅在 ``settings.live_ml_scoring=True`` 时返回非空（否则返回 {}，recommender 走原 14 因子）。
- **按 as_of（最新交易日）缓存**：训练 + LightGBM 推理较重，但一天内 as_of 不变，
  盘中多次分析（每分钟一轮）复用同一份结果，不会反复训练。
- 训练/推理全部复用 ``app.backtest.ml_scorer.build_live_ml_predictions``，
  与回测 ``default_ml`` 同模型、同行业中性、同 OOS-IC 回退纪律。
- 无信号（样本不足 / OOS IC≤0 / 训练失败）返回 {}，recommender 自动回退 14 因子，不注入噪声。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.backtest.ml_scorer import build_live_ml_predictions
from app.config import get_settings
from app.data.service import get_kline_cached

logger = logging.getLogger("sas.live_ml")

#: as_of(最新交易日) -> {code: 截面分位(0~100)}
_LIVE_CACHE: dict[str, dict[str, float]] = {}
_LIVE_IC: dict[str, float] = {}
#: as_of -> 本进程最近一次「ML 到底跑没跑成」的判定，供状态接口判读。
#: 没有它就无法区分「回退 14 因子」和「服务刚重启、还没跑过一轮」——
#: 两者缓存都是空的，界面却一个该报警一个不该。
_LIVE_ATTEMPT: dict[str, dict] = {}

_HORIZON = 15  # 与 default_ml / 策略中位持仓对齐


def _record_attempt(as_of: str, outcome: str, ic: float | None = None) -> None:
    """记录判定结果（纯内存，不落库、不影响打分）。"""
    if not as_of:
        return
    _LIVE_ATTEMPT[as_of] = {
        "outcome": outcome,
        "ic": ic,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def live_ml_status() -> dict:
    """只读状态：线上 ML 当前是「生效 / 回退 / 还没跑 / 没开」。

    绝不触发训练或取数，供顶栏状态灯与运维审计页 30s 轮询。
    ``effective`` 只在本进程确实构建出当日预测时为 true——开关打开不等于生效。
    """
    enabled = bool(getattr(get_settings(), "live_ml_scoring", False))
    as_of = max(_LIVE_ATTEMPT) if _LIVE_ATTEMPT else (max(_LIVE_CACHE) if _LIVE_CACHE else None)
    key = as_of or ""
    attempt = _LIVE_ATTEMPT.get(key, {})
    outcome = attempt.get("outcome") or ("ok" if key in _LIVE_CACHE else "pending")
    state_map = {
        "ok": "effective", "pending": "pending",
        "no_signal": "fallback", "build_error": "fallback", "insufficient": "fallback",
    }
    return {
        "enabled": enabled,
        "effective": bool(enabled and outcome == "ok" and _LIVE_CACHE.get(key)),
        "state": "off" if not enabled else state_map.get(outcome, "pending"),
        "outcome": outcome,
        "as_of": as_of or None,
        "codes": len(_LIVE_CACHE.get(key, {})),
        "oos_rank_ic": _LIVE_IC.get(key, attempt.get("ic")),
        "at": attempt.get("at"),
        "cache_days": len(_LIVE_CACHE),
    }


def _normalize(d) -> str:
    sd = str(d)
    if " " in sd:
        sd = sd.split(" ")[0]
    return sd


def get_live_ml_scores(codes: list[str]) -> dict[str, float]:
    """返回 {code: 当日ML截面分位(0~100)}；未启用 / 无信号 / 失败返回 {}。

    Args:
        codes: 候选股票代码列表（通常为自选股 + 已持仓）。
    """
    s = get_settings()
    if not getattr(s, "live_ml_scoring", False):
        return {}
    if not codes:
        return {}

    # 加载 klines（复用 recommender 的缓存读取），并推导 as_of
    klines: dict[str, object] = {}
    as_of = ""
    for c in codes:
        try:
            df = get_kline_cached(c)
        except Exception as exc:  # noqa: BLE001
            logger.debug("live ML 取K线失败 %s: %s", c, exc)
            continue
        if df is None or len(df) < _HORIZON + 20:
            continue
        klines[c] = df
        for d in df["date"].values:
            sd = _normalize(d)
            if sd > as_of:
                as_of = sd
    if len(klines) < 5:
        # 样本太少（如全部自选股都缺数据）不值得建模，回退 14 因子
        _record_attempt(as_of, "insufficient")
        return {}

    # 同日命中缓存：直接返回（避免反复训练）
    if as_of in _LIVE_CACHE:
        return _LIVE_CACHE[as_of]

    try:
        ml_pred, ic = build_live_ml_predictions(
            klines,  # type: ignore[arg-type]
            horizon=_HORIZON,
            model="lightgbm",
            industry_neutral=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("live ML 构建失败，回退 14 因子: %s", exc)
        _record_attempt(as_of, "build_error")
        return {}

    if not ml_pred:
        logger.info("live ML 无信号（OOS IC=%.4f），本日回退 14 因子", ic)
        _record_attempt(as_of, "no_signal", ic)
        return {}

    out = {c: p for (c, _d), p in ml_pred.items()}
    _LIVE_CACHE[as_of] = out
    _LIVE_IC[as_of] = ic
    _record_attempt(as_of, "ok", ic)
    logger.info(
        "live ML 构建完成: as_of=%s, 覆盖 %d 只, OOS IC=%.4f",
        as_of, len(out), ic,
    )
    return out


def clear_cache() -> None:
    """清空缓存（测试 / 强制重建用）。"""
    _LIVE_CACHE.clear()
    _LIVE_IC.clear()
    _LIVE_ATTEMPT.clear()
