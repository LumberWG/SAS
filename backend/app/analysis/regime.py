"""机制检测层：把市场按「波动率 / 回撤 / 趋势」分成四态，输出动态总仓位上限。

设计目标（对齐用户提出的鲁棒性主线）：
- 不预测点位，只判"现在处于哪种环境"——calm / weak / shock / crisis；
- 输出一个**只降不升**的仓位上限（exposure_cap ≤ 总仓位上限），交给引擎做预算缩放；
- 纯函数、无 IO、无外部依赖，便于单测；指数 parquet 读取由调用方负责。

状态机：
    crisis : 最大回撤 ≥ 阈值 或 波动率比 ≥ 阈值        → 危机（默认全暂停买入）
    shock  : 波动率比 / 回撤 达到次级阈值               → 震荡（压到 shock 档）
    weak   : 趋势走弱（前收 < MA）                       → 弱势（压到 weak 档）
    calm   : 其余                                        → 平稳（不降仓）
所有阈值与档位均可被 Settings 覆盖，默认取值见引擎侧调用。
"""
from __future__ import annotations

import pandas as pd


def detect_regime(
    close: pd.Series,
    *,
    ma_n: int = 60,
    vol_win: int = 20,
    vol_base: int = 60,
    dd_win: int = 120,
    crisis_vol: float = 2.0,
    crisis_dd: float = 0.12,
    shock_vol: float = 1.4,
    shock_dd: float = 0.06,
    cap_calm: float = 1.0,
    cap_weak: float = 0.30,
    cap_shock: float = 0.40,
    cap_crisis: float = 0.15,
) -> dict:
    """输入指数收盘价序列（调用方须已剔除未来函数，仅含截至前一交易日的数据）。

    返回 ``{"state", "exposure_cap", "pause_all", "reason", "metrics"}``。
    任何异常或数据不足都退化为 ``calm / 不降仓``，保证调用方零风险。
    """
    out: dict = {
        "state": "calm",
        "exposure_cap": float(cap_calm),
        "pause_all": False,
        "reason": "",
        "metrics": {},
    }
    try:
        s = close.astype(float).dropna()
        need = max(vol_base, ma_n, dd_win) + 2
        if len(s) < need:
            out["reason"] = f"指数数据不足(需≥{need}根)"
            return out

        ret = s.pct_change().dropna()
        if ret.empty:
            out["reason"] = "无收益率数据"
            return out

        # 近期波动率 vs 基线波动率（比值>1 表示波动放大）
        recent_vol = float(ret.tail(vol_win).std())
        base_vol = float(ret.tail(vol_base).std())
        vol_ratio = (recent_vol / base_vol) if (base_vol and base_vol == base_vol and base_vol > 0) else 1.0

        # 自近期高点的回撤深度
        window = min(dd_win, len(s))
        runmax = s.rolling(window, min_periods=1).max()
        dd = (s - runmax) / runmax
        max_dd = float(-dd.tail(dd_win).min()) if dd.tail(dd_win).notna().any() else 0.0

        # 趋势：最新收盘 < MA(N) 视为走弱（与线上弱市闸门同口径）
        ma = float(s.rolling(ma_n, min_periods=ma_n).mean().iloc[-1])
        trend_weak = bool(s.iloc[-1] < ma)

        out["metrics"] = {
            "vol_ratio": round(vol_ratio, 2),
            "max_drawdown": round(max_dd, 4),
            "trend_weak": trend_weak,
        }

        if max_dd >= crisis_dd or vol_ratio >= crisis_vol:
            out.update(state="crisis", exposure_cap=float(cap_crisis), pause_all=True)
        elif vol_ratio >= shock_vol or max_dd >= shock_dd:
            out.update(state="shock", exposure_cap=float(cap_shock), pause_all=False)
        elif trend_weak:
            out.update(state="weak", exposure_cap=float(cap_weak), pause_all=False)
        else:
            out.update(state="calm", exposure_cap=float(cap_calm), pause_all=False)
        return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"机制检测失败: {exc}"
        return out
