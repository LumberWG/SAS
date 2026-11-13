"""入场护栏（仅影子层 / 提示，不改动选股、评分、下单逻辑）。

两个相互独立、都不预测转折点的护栏：

1. **市场状态滤网（market-regime guard）**
   现为两层，互不混淆：
   - **Layer2 统一大盘宏观风险**：国内四大盘（上证/深成/创业板/沪深300）+ 外围
     （美股前一日 / 亚太当日）综合成 risk score → calm/weak/shock/crisis 四档，
     驱动总仓位上限；仅 crisis 硬暂停新开仓。见 ``app.analysis.market_risk``。
   - **Layer1 板块感知趋势**：个股按代码前缀归到自己的板块，用该板块指数做趋势
     过滤（创业板→创业板指，上证主板→上证指数…），不再一律用上证。
     见 ``app.analysis.board_index`` / ``board_regime_for_code``。
   历史回测（4 年、4 万+ 次动量信号：站上 MA20 时 20 日超额 +0.74%，跌破 MA20 时
   −1.22%）是原先「上证单一基准」的依据；新模型保留其「下降市慎开新仓」的软提示
   思路，但把基准拆成板块各自的指数 + 统一宏观层，避免上证系统性低估创业板脆弱度。
   它只是状态开关，不预测拐点。

2. **日内追高护栏（intraday-spike guard）**
   当某信号触发日个股高开（开盘价 vs 前收盘 > 阈值）或盘中冲高（最高价 vs 开盘价
   > 阈值）明显时，提示用限价单在尾盘或回踩至均线附近买入，避免买在当根尖峰。
   纯阈值规则，无预测成分。

所有函数只读数据，遇缺失文件返回 ``available=false``，不抛异常、不 500。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from app.config import DATA_DIR, get_settings

#: 高开阈值（缺省回退值）：开盘价较前一交易日收盘涨幅超过该值即触发追高提示。
#: 实际取值优先用 ``Settings.entry_guard_gap_threshold``（系统设置可改）。
GAP_THRESHOLD = 0.03
#: 盘中冲高阈值（缺省回退值）：当日最高价较开盘价涨幅超过该值即触发追高提示。
#: 实际取值优先用 ``Settings.entry_guard_spike_threshold``（系统设置可改）。
SPIKE_THRESHOLD = 0.05

#: 弱市判定所用宽基指数文件名（idx_ 前缀），由 ``settings.paper_regime_index`` 指定。
#: 默认 sh000985=中证全指（覆盖全A，比上证更能代表系统性风险）；可改 sh000001=上证 / sz399006=创业板指。
_INDEX_NAME_MAP = {
    "sh000985": "中证全指", "sh000001": "上证指数", "sz399006": "创业板指",
    "sz399001": "深证成指", "sh000300": "沪深300",
}


def _regime_index_file() -> str:
    from app.config import get_settings
    return "idx_" + str(get_settings().paper_regime_index or "sh000985")


def _regime_index_name() -> str:
    from app.config import get_settings
    code = str(get_settings().paper_regime_index or "sh000985")
    return _INDEX_NAME_MAP.get(code, code)


def _read_parquet(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "date" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    return df


def market_regime() -> dict:
    """判定当前市场状态（统一大盘宏观风险层，Layer2）。

    用国内大盘 + 外围（美股前一日 / 亚太当日）综合成 risk score →
    calm / weak / shock / crisis 四档（见 ``app.analysis.market_risk``）。
    同时回填旧字段（close/ma20/ma60 取上证指数）以兼容既有 advisory 展示。

    返回：``state``（uptrend/choppy/declining 兼容旧语义）、``tier``、``score``、
    ``cap``（对应档位的总仓位上限）、``new_entry_advice``（avoid/caution/ok）、
    ``components``、以及旧版 close/ma20/ma60。
    """
    from app.analysis.market_risk import get_unified_market_risk

    ur = get_unified_market_risk()
    s = get_settings()
    legacy = {}
    df = _read_parquet(s.kline_dir / _regime_index_file())
    if df is not None and len(df) >= 60:
        df = df.sort_values("date")
        close = float(df["close"].iloc[-1])
        ma20 = float(df["close"].rolling(20).mean().iloc[-1])
        ma60 = float(df["close"].rolling(60).mean().iloc[-1])
        legacy = {
            "close": close, "ma20": ma20, "ma60": ma60,
            "above_ma20": bool(close > ma20), "above_ma60": bool(close > ma60),
            "as_of": str(df["date"].iloc[-1].date()),
            "index_name": _regime_index_name(),
        }
    return {
        "state": ur["state"],
        "tier": ur["tier"],
        "score": ur["score"],
        "cap": ur["cap"],
        "new_entry_advice": ur["new_entry_advice"],
        "components": ur["components"],
        "overseas_ok": ur["overseas_ok"],
        # 统一风险层的时间戳单独命名，避免与下面 legacy 的上证 K 线 as_of 撞名。
        "risk_as_of": ur["as_of"],
        "domestic_score": ur["domestic_score"],
        "overseas_score": ur["overseas_score"],
        **legacy,
    }


def intraday_spike(code: str, scan_date: str) -> dict:
    """判定某信号触发日是否出现明显的日内追高。

    读取 ``minute5_fsdb/{code}.parquet`` 当日 5 分钟 bar：
    - ``gap_up_pct`` = 开盘价 / 前一日收盘 − 1（前收盘取自日线）；
    - ``intraday_high_pct`` = 当日最高 / 开盘价 − 1。
    任一超过阈值即 ``triggered``，并给出「勿追当根尖峰」的提示文案。
    数据缺失（无分钟文件 / 无日线前收盘）返回 ``available=false``。
    """
    s = get_settings()
    gap_threshold = float(getattr(s, "entry_guard_gap_threshold", GAP_THRESHOLD) or GAP_THRESHOLD)
    spike_threshold = float(getattr(s, "entry_guard_spike_threshold", SPIKE_THRESHOLD) or SPIKE_THRESHOLD)
    code6 = str(code).zfill(6)
    f = DATA_DIR / "minute5_fsdb" / f"{code6}.parquet"
    df = _read_parquet(f)
    if df is None:
        return {"available": False, "triggered": False,
                "gap_up_pct": None, "intraday_high_pct": None,
                "open": None, "high": None, "note": ""}
    day = pd.to_datetime(scan_date).date()
    sub = df[df["date"].dt.date == day]
    if sub.empty:
        return {"available": False, "triggered": False,
                "gap_up_pct": None, "intraday_high_pct": None,
                "open": None, "high": None, "note": ""}
    open0 = float(sub["open"].iloc[0])
    high = float(sub["high"].max())

    prev_close = None
    kl = s.kline_dir / f"{code6}.parquet"
    k = _read_parquet(kl)
    if k is not None:
        k = k[k["date"] < pd.to_datetime(scan_date)]
        if not k.empty:
            prev_close = float(k["close"].iloc[-1])

    gap = (open0 / prev_close - 1.0) if prev_close else float("nan")
    spike = (high / open0 - 1.0) if open0 else float("nan")
    triggered = (
        (not np.isnan(gap) and gap > gap_threshold)
        or (not np.isnan(spike) and spike > spike_threshold)
    )
    note = (
        "信号日高开/盘中冲高明显，建议用限价单在尾盘或回踩至均线附近买入，勿追当根尖峰"
        if triggered else ""
    )
    return {
        "available": True,
        "triggered": bool(triggered),
        "gap_up_pct": None if np.isnan(gap) else float(gap),
        "intraday_high_pct": None if np.isnan(spike) else float(spike),
        "open": open0,
        "high": high,
        "note": note,
    }


def evaluate_entry_guards(limit: int = 20, signal_table: str = "signals") -> dict:
    """对最近一批信号叠加两个护栏，返回可直接展示的结构。

    不修改任何信号 / 选股逻辑，只读 ``signals`` 表与行情数据。
    """
    regime = market_regime()
    s = get_settings()
    import sqlite3

    con = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT code, name, strategy, price, scan_date, reason, score, rank "
            f"FROM {signal_table} ORDER BY scan_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()

    items = []
    for r in rows:
        rec = {k: r[k] for k in r.keys()}
        sp = intraday_spike(rec["code"], rec["scan_date"])
        rec["regime_state"] = regime["state"]
        rec["regime_advice"] = regime["new_entry_advice"]
        rec["intraday"] = sp
        # 综合提示：下降市 或 日内追高触发
        flags = []
        if regime["new_entry_advice"] == "avoid":
            flags.append("下降市慎开新仓")
        if sp.get("triggered"):
            flags.append("日内追高")
        rec["flags"] = flags
        items.append(rec)

    return {
        "regime": regime,
        "guards": {
            "gap_threshold": float(getattr(s, "entry_guard_gap_threshold", GAP_THRESHOLD) or GAP_THRESHOLD),
            "spike_threshold": float(getattr(s, "entry_guard_spike_threshold", SPIKE_THRESHOLD) or SPIKE_THRESHOLD),
        },
        "signals": items,
    }


def should_block_buy() -> tuple[bool, dict]:
    """是否应因入场护栏硬屏蔽新开仓买入。

    仅当设置 ``entry_guard_block_declining_market=True`` 且统一大盘宏观风险处于
    **``crisis``** 档时才返回 ``(True, regime)``；其余一律 ``(False, regime)``。
    纯只读、不抛异常、不 500。

    分级原则（与「弱市只压仓位、卖出与止盈止损照常」一致）：
    - ``calm`` / ``weak`` / ``shock``：只通过 cap 压总仓位上限，**买入不被全停**；
      ``shock`` 另给软提示 ``new_entry_advice=avoid``（慎开新仓），但不硬拦。
    - ``crisis``：压到最严上限并全暂停新开仓买入（卖出、做T买回不受影响）。

    这是把影子层「提示」升级为「硬屏蔽」的唯一开关：开启后由调用方（买入建议
    展示 + 自动交易引擎）据此拦截新开仓买入。
    """
    s = get_settings()
    if not getattr(s, "entry_guard_block_declining_market", False):
        return (False, {})
    regime = market_regime()
    if regime.get("tier") == "crisis":
        return (True, regime)
    return (False, regime)


def board_regime_for_code(code: str, ma: int | None = None) -> dict:
    """个股所属板块的当前趋势状态（板块感知参考，Layer1）。

    选股/入场时按「个股自己的板块指数」判断上升通道，而非统一用上证。
    返回 ``app.analysis.board_index.board_regime`` 的结构，附 ``code`` / ``board``。
    """
    from app.analysis.board_index import classify_board, board_regime

    b = classify_board(code)
    rg = board_regime(b, ma=ma or get_settings().bottom_trend_ma)
    rg["code"] = str(code)
    rg["board"] = b
    return rg
