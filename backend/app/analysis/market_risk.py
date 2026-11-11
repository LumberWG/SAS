"""统一大盘宏观风险层（Layer2：含外围，驱动总仓位上限）。

把「国内大盘 + 外围前一/当日」综合成一个 market risk score，映射到
calm / weak / shock / crisis 四档，供仓位上限与买入拦截使用。

- 国内：上证 / 深成 / 创业板 / 沪深300（本地 ``idx_*.parquet``），用「收盘 vs MA 的下方偏离度」衡量弱势
  （站在 MA 上不计入弱势，只有跌破才计入）。
- 外围：
    * 美股（标普500 / 纳斯达克 / 道琼斯）：取 akshare ``index_global_hist_em`` 末行 = 前一日收盘，
      用「前一日收盘相对其 MA 的下方偏离度」衡量（美股在 A 股开盘前已收盘，天然是「前一日」）。
    * 亚太（日经225 / 恒生 / 韩国KOSPI）：它们比 A 股早开盘，取末行 = 当日盘中最新价，
      用「当日涨跌幅」衡量（当日已发生的冲击会传导到 A 股）。
- 综合：domestic 0.6 + overseas 0.4 加权 → score（0~1 标度，越大越弱）；按阈值映射档位。
- 外围取数失败（网络/超时）→ 失败开放：剔除该成分并标注，不影响其余与国内判定。
- 外围数据本地缓存（``<DATA_DIR>/market_risk_overseas.json``），30 分钟 TTL，减少网络抖动。

所有函数只读、不抛异常、不 500；任一成分缺失都降级而非中断。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import DATA_DIR, get_settings

# 外围成分：name=展示名，symbol=akshare 查询名，kind=us_prev(前一日) / asia_same(当日盘中)
OVERSEAS_UNIVERSE: list[dict] = [
    {"name": "标普500", "symbol": "标普500", "kind": "us_prev"},
    {"name": "纳斯达克", "symbol": "纳斯达克", "kind": "us_prev"},
    {"name": "道琼斯", "symbol": "道琼斯", "kind": "us_prev"},
    {"name": "日经225", "symbol": "日经225", "kind": "asia_same"},
    {"name": "恒生指数", "symbol": "恒生指数", "kind": "asia_same"},
    {"name": "韩国KOSPI", "symbol": "韩国KOSPI", "kind": "asia_same"},
]

_OVERSEAS_CACHE_TTL = 30 * 60  # 30 分钟
_overseas_cache: dict | None = None
_overseas_cache_at: float = 0.0


def _overseas_cache_path() -> str:
    return os.path.join(str(DATA_DIR), "market_risk_overseas.json")


def _fetch_overseas() -> dict:
    """从 akshare 取各外围成分的最新一行，返回 {name: {weakness, value, note, as_of}}。失败整体降级。"""
    try:
        import akshare as ak
    except Exception:
        return {}
    out: dict = {}
    for item in OVERSEAS_UNIVERSE:
        try:
            df = ak.index_global_hist_em(symbol=item["symbol"])
            if df is None or df.empty:
                continue
            df = df.sort_values("日期") if "日期" in df.columns else df
            last = df.iloc[-1]
            if item["kind"] == "us_prev":
                # 末行=前一日收盘；用其相对 MA60 的下方偏离度
                closes = pd.to_numeric(df["最新价"], errors="coerce").dropna().astype(float).to_numpy()
                ma = closes[-60:].mean() if len(closes) >= 60 else closes.mean()
                cur = float(last["最新价"])
                weakness = max(0.0, (ma - cur) / ma) if ma > 0 else 0.0
                out[item["name"]] = {
                    "weakness": round(float(weakness), 4),
                    "value": cur, "note": "前一日收盘 vs MA60", "as_of": str(last.get("日期")),
                }
            else:  # asia_same：当日盘中，用 最新价/今开 - 1
                open0 = float(last.get("今开", np.nan))
                cur = float(last["最新价"])
                chg = (cur / open0 - 1.0) if open0 and open0 > 0 else 0.0
                weakness = max(0.0, -chg)  # 当日下跌才计入弱势
                out[item["name"]] = {
                    "weakness": round(float(weakness), 4),
                    "value": cur, "note": f"当日涨跌 {chg:+.2%}", "as_of": str(last.get("日期")),
                }
        except Exception:
            # 单成分失败：跳过，不阻断整体
            continue
    return out


def _get_overseas(force: bool = False) -> dict:
    global _overseas_cache, _overseas_cache_at
    now = time.time()
    if not force and _overseas_cache is not None and (now - _overseas_cache_at) < _OVERSEAS_CACHE_TTL:
        return _overseas_cache
    # 先试本地缓存文件（跨重启/网络不可用）
    path = _overseas_cache_path()
    if not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("_ts") and (now - cached["_ts"]) < _OVERSEAS_CACHE_TTL and cached.get("data"):
                _overseas_cache, _overseas_cache_at = cached["data"], cached["_ts"]
                return cached["data"]
        except Exception:
            pass
    live = _fetch_overseas()
    if live:
        _overseas_cache, _overseas_cache_at = live, now
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"_ts": now, "data": live}, fh, ensure_ascii=False)
        except Exception:
            pass
    elif _overseas_cache is not None:
        # 取数失败但内存有旧值，沿用
        return _overseas_cache
    return live


def _domestic_weakness(kline_dir: str, symbol: str, ma: int) -> float | None:
    f = os.path.join(kline_dir, f"idx_{symbol}.parquet")
    if not os.path.exists(f):
        return None
    try:
        df = pd.read_parquet(f).sort_values("date")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        s = df.set_index("date")["close"].astype(float)
        s.index = pd.to_datetime(s.index)
        if len(s) < ma + 1:
            return None
        last = float(s.iloc[-1])
        m = float(s.rolling(ma).mean().iloc[-1])
        if m <= 0:
            return None
        return max(0.0, (m - last) / m)
    except Exception:
        return None


def get_unified_market_risk(force_overseas: bool = False) -> dict:
    """综合国内大盘 + 外围，返回统一宏观风险档位。

    返回：``{tier, score, cap, state, new_entry_advice, components, as_of, overseas_ok}``。
    ``cap`` 取自本层专属的 ``unified_risk_cap_*``：calm→0(不降仓)、weak→0.30、
    shock→0.20、crisis→0.10（单调递减）。刻意不复用 ``paper_regime_cap_*``：
    后者属机制层 _dynamic_regime() 的「波动率/回撤 + 政策托底」口径，shock 默认
    0.40 比 weak 0.30 更宽松，直接套到本层会出现「市场更弱、上限反而放宽」的反转。
    """
    s = get_settings()
    ma = int(s.unified_risk_ma)
    kdir = str(s.kline_dir)
    components: list[dict] = []

    # ---- 国内成分 ----
    dom_w = []
    for sym in s.unified_risk_domestic:
        w = _domestic_weakness(kdir, sym, ma)
        name = {"sh000001": "上证指数", "sz399001": "深证成指",
                "sz399006": "创业板指", "sh000300": "沪深300"}.get(sym, sym)
        if w is None:
            components.append({"name": name, "kind": "domestic", "weakness": None,
                               "available": False, "note": "无本地指数数据"})
            continue
        dom_w.append(w)
        components.append({"name": name, "kind": "domestic", "weakness": round(w, 4),
                           "available": True})

    # ---- 外围成分 ----
    ov_w = []
    overseas_ok = True
    if getattr(s, "unified_risk_include_overseas", True):
        ov = _get_overseas(force=force_overseas)
        if not ov:
            overseas_ok = False
        for item in OVERSEAS_UNIVERSE:
            rec = ov.get(item["name"])
            if rec is None:
                components.append({"name": item["name"], "kind": "overseas",
                                   "weakness": None, "available": False, "note": "取数失败/缺失"})
                overseas_ok = overseas_ok and False
                continue
            ov_w.append(rec["weakness"])
            components.append({"name": item["name"], "kind": "overseas",
                               "weakness": rec["weakness"], "available": True,
                               "value": rec.get("value"), "note": rec.get("note"),
                               "as_of": rec.get("as_of")})

    # ---- 综合 score ----
    dom_score = float(np.mean(dom_w)) if dom_w else 0.0
    ov_score = float(np.mean(ov_w)) if ov_w else 0.0
    # 国内占 0.6、外围占 0.4；外围缺失则不加权（退化为纯国内）
    if dom_w and ov_w:
        score = 0.6 * dom_score + 0.4 * ov_score
    elif dom_w:
        score = dom_score
    else:
        score = 0.0
    score = round(float(score), 4)

    # ---- 档位映射 ----
    if score >= s.unified_risk_thr_crisis:
        tier, state = "crisis", "declining"
    elif score >= s.unified_risk_thr_shock:
        tier, state = "shock", "declining"
    elif score >= s.unified_risk_thr_weak:
        tier, state = "weak", "choppy"
    else:
        tier, state = "calm", "uptrend"

    # 本层专属上限（与机制层 paper_regime_cap_* 解耦，默认单调递减）。
    # calm 固定 0 = 不降仓（沿用总仓位上限）。
    cap_map = {
        "calm": 0.0,
        "weak": float(getattr(s, "unified_risk_cap_weak", 0.30)),
        "shock": float(getattr(s, "unified_risk_cap_shock", 0.20)),
        "crisis": float(getattr(s, "unified_risk_cap_crisis", 0.10)),
    }
    # 单调保护：要求 weak >= shock >= crisis。即便用户误把高风险档上限调得比
    # 低风险档更松，也收敛到不超过前一档，杜绝「风险升级反而放宽仓位」的反转。
    # calm 的 0 表示「不降仓、沿用总仓位上限」，是语义值而非数值，不参与比较。
    chain = ["weak", "shock", "crisis"]
    for i in range(1, len(chain)):
        prev, cur = chain[i - 1], chain[i]
        if cap_map[cur] > cap_map[prev]:
            cap_map[cur] = cap_map[prev]
    cap = cap_map[tier]
    new_entry_advice = "avoid" if tier in ("shock", "crisis") else ("caution" if tier == "weak" else "ok")

    return {
        "tier": tier, "score": score, "cap": cap, "state": state,
        "new_entry_advice": new_entry_advice,
        "domestic_score": round(dom_score, 4), "overseas_score": round(ov_score, 4),
        "components": components, "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "overseas_ok": overseas_ok,
    }


__all__ = ["get_unified_market_risk", "OVERSEAS_UNIVERSE"]
