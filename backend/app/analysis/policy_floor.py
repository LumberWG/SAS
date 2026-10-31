"""政策托底强度检测 + 硬性回撤熔断的纯逻辑。

设计目标（对齐用户提出的鲁棒性主线 #85）：
- 把"国家不会让股市崩、有个看不见的底"这种**信仰**，转成可被数据观测、也可被
  证伪的信号；
- :func:`detect_policy_floor` 输出 0~1 的托底强度 + 分级，让引擎在
  "政策托底型阴跌"（可缩仓收割反转）与"自由落体型崩盘"（必须撤退+熔断）之间分治；
- :func:`circuit_breaker_state` 是熔断的**纯滞回判定**，单测可覆盖，引擎只负责
  把"当前回撤"喂进来；
- 纯函数、无 IO、无外部依赖，便于单测；指数收盘价由调用方提供。ETF 放量 /
  期指升贴水 / 官媒情绪是**可选**直接信号——当前部署只有指数 K 线，缺失时诚实
  降级为"指数底部特征代理"，并标记 ``available=False``，绝不假装拥有没有的数据。
- 任何异常或数据不足都退化为 score=0 / 未触发，保证调用方零风险。
"""
from __future__ import annotations

import pandas as pd


def detect_policy_floor(
    index_close: pd.Series,
    *,
    etf_volume: "pd.Series | None" = None,
    futures_basis: "pd.Series | None" = None,
    news_sentiment: "float | None" = None,
    dd_win: int = 120,
    drop_thresh: float = 0.05,
    vol_win: int = 10,
    vol_base_win: int = 40,
    min_score_harvest: float = 0.5,
    low_escalate: float = 0.2,
) -> dict:
    """输入指数收盘价序列（截至前一交易日，无未来函数）。

    可选直接信号（缺失则不计权重，``available`` 标记）：
    - ``etf_volume``：宽基 ETF 成交量；异常放量 = 国家队借道买入的经典痕迹；
    - ``futures_basis``：期指升贴水（正=升水，负=贴水）；贴水骤收窄/转升水 = 干预信号；
    - ``news_sentiment``：官媒/政策情绪，已归一化到 [-1, 1]。

    返回 ``{"score","level","components","available","reason","metrics","thresholds"}``。
    任何异常或数据不足都返回 score=0 / level="none"，零风险。
    """
    out: dict = {
        "score": 0.0, "level": "none", "components": {},
        "available": False, "reason": "", "metrics": {},
        "thresholds": {"min_score_harvest": min_score_harvest, "low_escalate": low_escalate},
    }
    try:
        s = index_close.astype(float).dropna()
        need = max(vol_base_win, dd_win) + 2
        if len(s) < need:
            out["reason"] = f"指数数据不足(需≥{need}根)"
            return out

        ret = s.pct_change().dropna()
        if ret.empty:
            out["reason"] = "无收益率数据"
            return out

        # 1) 是否处于下行段（必要条件）：自近期高点回撤
        window = min(dd_win, len(s))
        runmax = s.rolling(window, min_periods=1).max()
        dd = (s - runmax) / runmax
        max_dd = float(-dd.tail(dd_win).min()) if dd.tail(dd_win).notna().any() else 0.0
        out["metrics"]["max_drawdown"] = round(max_dd, 4)
        # 不在下行段就没"底"可谈
        if max_dd < drop_thresh:
            out["reason"] = f"未处于下行段(回撤{max_dd:.1%}<{drop_thresh:.0%})"
            return out

        comp: dict[str, float] = {}

        # 2) 波动压缩：近期波动 ÷ 下跌初期波动；<1 表示卖压衰竭，像底部
        vb = float(ret.tail(vol_base_win).std())
        vr = float(ret.tail(vol_win).std())
        vol_compress = 0.0
        if vb > 0:
            ratio = vr / vb
            vol_compress = float(max(0.0, min(1.0, (1.0 - ratio) / 0.5)))
        comp["vol_compression"] = round(vol_compress, 3)

        # 3) 企稳：近 20 日低点相对前期低点抬升 → 不再创新低
        k = 20
        lows = s.rolling(k, min_periods=5).min()
        recent_low = float(lows.tail(k).iloc[-1])
        prev_low = float(lows.tail(k).iloc[0]) if len(lows) >= k else recent_low
        stabilize = 0.0
        if prev_low > 0:
            lift = (recent_low - prev_low) / prev_low
            stabilize = float(max(0.0, min(1.0, lift / 0.03 + 0.5))) if lift >= 0 else 0.0
        comp["stabilization"] = round(stabilize, 3)

        # 4) 反转日占比：近 10 日收阳比例上升 → 买盘开始承接
        up_ratio = float((ret.tail(vol_win) > 0).mean())
        reversal = float(max(0.0, min(1.0, (up_ratio - 0.4) / 0.3)))
        comp["reversal_days"] = round(reversal, 3)

        # 5) 可选直接信号（缺失则不计权重，available 标记）
        direct = 0.0
        n_direct = 0
        if etf_volume is not None:
            ev = pd.Series(etf_volume).astype(float).dropna()
            if len(ev) >= vol_base_win:
                base = float(ev.tail(vol_base_win).mean())
                recent = float(ev.tail(vol_win).mean())
                if base > 0:
                    etf_spike = float(max(0.0, min(1.0, (recent / base - 1.0) / 1.0)))
                    direct += etf_spike
                    n_direct += 1
                    comp["etf_volume_spike"] = round(etf_spike, 3)
        if futures_basis is not None:
            fb = pd.Series(futures_basis).astype(float).dropna()
            if len(fb) >= 5:
                basis_now = float(fb.tail(5).mean())
                basis_prev = float(fb.tail(20).iloc[0]) if len(fb) >= 20 else basis_now
                shift = float(max(0.0, min(1.0, (basis_now - basis_prev) / 0.02)))
                direct += shift
                n_direct += 1
                comp["futures_basis_shift"] = round(shift, 3)
        if news_sentiment is not None:
            ns = float(news_sentiment)
            direct += float(max(0.0, min(1.0, (ns + 1) / 2)))
            n_direct += 1
            comp["news_sentiment"] = round(ns, 3)

        out["available"] = n_direct > 0

        # 指数代理权重 0.7；有任意直接信号时，直接信号占 0.3（否则全用指数代理）
        idx_score = (
            0.40 * comp["vol_compression"]
            + 0.35 * comp["stabilization"]
            + 0.25 * comp["reversal_days"]
        )
        score = (0.7 * idx_score + 0.3 * (direct / n_direct)) if n_direct > 0 else idx_score
        score = float(max(0.0, min(1.0, score)))
        out["score"] = round(score, 3)
        out["components"] = comp
        out["reason"] = "指数底部特征" + ("+直接信号" if n_direct > 0 else "(无ETF/期指/新闻,仅指数代理)")
        out["level"] = (
            "strong" if score >= min_score_harvest
            else "weak" if score >= low_escalate else "none"
        )
        return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"托底检测失败: {exc}"
        return out


def circuit_breaker_state(
    drawdown: float,
    tripped: bool,
    *,
    trigger: float,
    release: float,
) -> bool:
    """熔断的纯滞回判定（最后一道防线，优先级高于一切信号）。

    - 未触发时，回撤 ≥ ``trigger`` 即触发；
    - 已触发时，回撤须回落到 ≤ ``release`` 才解除（滞回，避免临界值反复抖）。
    ``trigger`` 必须 > ``release``，否则恒为触发（调用方应保证）。
    """
    if tripped:
        return drawdown > release
    return drawdown >= trigger
