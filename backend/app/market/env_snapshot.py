"""隔夜市场环境日频快照（影子研究阶段，只采集不决策）。

⚠ 决策边界（2026-11-13 用户确认「先做影子研究」）
    本模块只负责采集与生成快照，产出写入 ``market_env`` 表。
    **禁止**在选股 / 买入 / 仓位 / 止损等任何决策链路读取本表。
    若将来要接入，必须回测 ↔ 实盘 1:1 同步（BINDINGS + 行为探针 +
    重跑前向验证基线），否则就是「实盘有、回测没有」的半边生效。

零未来函数口径
    trade_date = A 股交易日 T；隔夜美股取 ``us_session_date < T`` 的最近一根
    收盘（美股 D 日收盘发生在北京时间 D+1 凌晨 04:00~05:00，早于 A 股 T 日
    09:30 开盘）；政策窗口为 T-1 15:00 ~ T 05:30。任何字段都不得使用
    T 日 09:30 之后才产生的信息。评估脚本用 T 日收盘收益作**被解释变量**，
    属于预测力检验，不构成前视。

数据可得性（是数据边界，不是规则漂移）
    美股三大指数历史日线可回放（akshare ``index_us_stock_sina``，覆盖 2003 起）；
    国内盘后重大政策 / 快讯的历史客观不可得，只能采当日凌晨快照，历史段
    ``policy_score`` 留 NULL，两侧一致地不参与评分。
"""
from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

import pandas as pd

from app.config import get_settings
from app.data.cache import db

logger = logging.getLogger("sas.market_env")

#: 美股三大指数：akshare 的 symbol → 落库列名
US_SYMBOLS = ((".INX", "spx"), (".IXIC", "ndx"), (".DJI", "dji"))

#: 判定「宏观/政策类」快讯的关键词（只筛标题；普通个股公告不进环境分）
POLICY_WORDS = (
    "国务院", "央行", "人民银行", "证监会", "财政部", "发改委", "政治局", "国常会",
    "降准", "降息", "加息", "LPR", "MLF", "逆回购", "印花税", "减税", "降费",
    "美联储", "议息", "非农", "CPI", "PPI", "关税", "制裁", "出口管制",
    "稳增长", "刺激", "专项债", "注册制", "IPO", "再融资", "减持新规",
    "资本市场", "货币政策", "财政政策", "监管", "风险化解", "房地产政策",
)
POSITIVE_WORDS = (
    "降准", "降息", "宽松", "减税", "降费", "支持", "刺激", "补贴", "放松",
    "利好", "超预期", "回暖", "企稳", "回升", "增持", "回购", "获批",
)
NEGATIVE_WORDS = (
    "加息", "收紧", "监管趋严", "处罚", "罚款", "关税", "制裁", "限制",
    "下滑", "衰退", "风险", "收紧流动性", "暴跌", "违约", "立案", "调查",
)


def _pick(row: pd.Series, *names: str, default=""):
    for n in names:
        if n in row.index and pd.notna(row[n]):
            return row[n]
    return default


def _fetch_us_returns() -> pd.DataFrame:
    """拉三大指数历史日线，返回 index=美东交易日、列=当日涨跌% 的 DataFrame。

    休市日（某指数无 bar）填 0，表示「无变化」而不是沿用前一日涨跌；
    首根 pct_change 为 NaN 也填 0。全部只用历史值，零未来函数。
    """
    import akshare as ak

    cols: dict[str, pd.Series] = {}
    for sym, key in US_SYMBOLS:
        try:
            df = ak.index_us_stock_sina(symbol=sym)
        except Exception as exc:  # noqa: BLE001 - 单个指数失败不拖垮其余
            logger.warning("美股指数 %s 拉取失败: %s", sym, exc)
            continue
        if df is None or df.empty or "close" not in df:
            continue
        d = df[["date", "close"]].copy()
        d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d = (d.dropna().sort_values("date")
              .drop_duplicates("date", keep="last").set_index("date"))
        s = d["close"].pct_change() * 100.0
        s.name = key + "_ret"       # 必须显式命名：否则 concat 三列都叫 close
        cols[key + "_ret"] = s
    if not cols:
        raise RuntimeError("三大美股指数全部拉取失败")
    out = pd.concat(cols.values(), axis=1).sort_index()
    for key in ("spx_ret", "ndx_ret", "dji_ret"):
        if key not in out.columns:
            out[key] = 0.0
    return out[["spx_ret", "ndx_ret", "dji_ret"]].fillna(0.0)


def _us_for(trade_date: str, us: pd.DataFrame):
    """取严格早于 A 股交易日 T 的最近一根美股收盘。"""
    idx = us.index[us.index < str(trade_date)[:10]]
    if len(idx) == 0:
        return None, None
    d = str(idx[-1])[:10]
    return d, us.loc[d]


def _us_risk(row) -> float:
    """三大指数平均涨跌 → [-1, 1] 风险偏好分（±2.5% 约 ±0.76）。"""
    avg = sum(float(row.get(k) or 0.0)
              for k in ("spx_ret", "ndx_ret", "dji_ret")) / 3.0
    return math.tanh(avg / 2.5)


def _label(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 0.25:
        return "risk_on"
    if score <= -0.25:
        return "risk_off"
    return "neutral"


def _fallback_trade_dates(start: str, end: str) -> list[str]:
    """context_heat 未覆盖时，从本地中证全指日线取 A 股交易日。"""
    p = Path(get_settings().kline_dir) / "idx_sh000985.parquet"
    df = pd.read_parquet(p, columns=["date"])
    ds = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna()
    return sorted(d for d in ds if str(start)[:10] <= d <= str(end)[:10])


def _policy_snapshot() -> tuple[int, float | None, str]:
    """当日凌晨的宏观政策/快讯：条数、规则情感分、样例标题。

    历史不可得，因此只在 ``--daily`` / 凌晨任务里调用；历史回填不写政策分。
    """
    import akshare as ak

    titles: list[str] = []
    for name, kw in (("stock_info_global_em", {}),
                     ("stock_info_global_cls", {"symbol": "全部"})):
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            df = fn(**kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("政策快讯 %s 拉取失败: %s", name, exc)
            continue
        if df is None or df.empty:
            continue
        for _, r in df.head(80).iterrows():
            t = str(_pick(r, "标题", "内容", "新闻标题", "摘要", "title", default="") or "").strip()
            if t:
                titles.append(t)

    hits = [t for t in dict.fromkeys(titles) if any(w in t for w in POLICY_WORDS)]
    scores: list[float] = []
    for t in hits:
        pos = sum(1 for w in POSITIVE_WORDS if w in t)
        neg = sum(1 for w in NEGATIVE_WORDS if w in t)
        if pos or neg:
            scores.append(max(-1.0, min(1.0, (pos - neg) / 2.0)))
    # hits 非空但无情感词 = 中性政策消息（0.0）；hits 为空 = 无数据（NULL）
    score = (round(sum(scores) / len(scores), 4) if scores
             else (0.0 if hits else None))
    return len(hits), score, " | ".join(hits[:5])[:300]


def _record(trade_date: str, sess, row, policy_count: int,
            policy_score: float | None, source: str) -> dict:
    us_risk = round(_us_risk(row), 4) if row is not None else None
    if us_risk is None:
        env = policy_score
    elif policy_score is None:
        env = us_risk
    else:
        env = round(0.75 * us_risk + 0.25 * policy_score, 4)
    return {
        "trade_date": str(trade_date)[:10],
        "us_session_date": sess,
        "spx_ret": round(float(row.get("spx_ret") or 0), 4) if row is not None else None,
        "ndx_ret": round(float(row.get("ndx_ret") or 0), 4) if row is not None else None,
        "dji_ret": round(float(row.get("dji_ret") or 0), 4) if row is not None else None,
        "us_risk_score": us_risk,
        "policy_count": int(policy_count or 0),
        "policy_score": policy_score,
        "env_score": round(env, 4) if env is not None else None,
        "env_label": _label(env),
        "source": source,
    }


def backfill(start: str, end: str) -> dict:
    """回放历史：只写美股隔夜收益，政策分留 NULL（历史不可得，一致退化）。"""
    us = _fetch_us_returns()
    dates = db.context_trade_dates(start, end) or _fallback_trade_dates(start, end)
    rows = []
    for t in dates:
        sess, row = _us_for(t, us)
        if row is None:
            continue
        rows.append(_record(t, sess, row, 0, None, "us_history"))
    written = db.upsert_market_env(rows)
    return {"written": written, "start": rows[0]["trade_date"] if rows else None,
            "end": rows[-1]["trade_date"] if rows else None,
            "policy": "历史不可得，policy_score=NULL（一致退化）"}


def build_daily(as_of: str | None = None) -> dict:
    """凌晨任务：生成今日（as_of）的隔夜环境快照，只写库、不进任何决策。"""
    t = str(as_of or date.today().isoformat())[:10]
    us = _fetch_us_returns()
    sess, row = _us_for(t, us)
    try:
        pc, ps, sample = _policy_snapshot()
    except Exception as exc:  # noqa: BLE001 - 网络失败不得影响调度器
        logger.warning("政策快讯采集失败（保留美股部分）: %s", exc)
        pc, ps, sample = 0, None, ""
    rec = _record(t, sess, row, pc, ps, "daily")
    db.upsert_market_env([rec])
    if sample:
        logger.info("凌晨政策快讯样例: %s", sample)
    return rec


def coverage(start: str = "2022-01-01", end: str | None = None) -> dict:
    rows = db.market_env_rows(start, str(end or date.today().isoformat())[:10])
    from collections import Counter
    return {
        "rows": len(rows),
        "from": rows[0]["trade_date"] if rows else None,
        "to": rows[-1]["trade_date"] if rows else None,
        "with_us": sum(1 for r in rows if r["us_session_date"]),
        "with_policy": sum(1 for r in rows if r["policy_score"] is not None),
        "labels": dict(Counter(r["env_label"] for r in rows)),
        "sources": dict(Counter(r["source"] for r in rows)),
    }
