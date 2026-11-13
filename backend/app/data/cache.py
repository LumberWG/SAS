"""本地数据缓存层。

存储策略：
- 历史 K 线 → Parquet 文件（按股票代码一个文件，列式存储，追加写入快）；
- 结构化小数据（自选股 / 策略信号 / 回测记录）→ SQLite（WAL 模式，
  个人单机使用完全够用，且读写并发不互斥）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings


def _now() -> str:
    """当前时间字符串（本地时区）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 卖出原因结构化编码（#191）：从人类可读 reason 文本推断稳定编码，便于复盘/统计。
# 仅在未显式传入 exit_reason 时作为兜底；买入侧不推断（保持 NULL）。
_EXIT_REASON_RULES: list[tuple[str, str]] = [
    ("时间止损", "TIME_STOP"),
    ("止损触发", "STOP_LOSS"),
    ("止盈触发", "TAKE_PROFIT"),
    ("轮动", "ROTATION"),
    ("清非交易板块", "OFFBOARD"),
    ("收盘减仓", "EOD_TRIM"),
    ("涨停卖半", "T_SELL"),
    ("做T卖出", "T_SELL"),
    ("反弹减仓", "T_SELL"),
    ("手动下单", "MANUAL"),
    ("卖出信号", "SIGNAL"),
]


def _infer_exit_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    for kw, code in _EXIT_REASON_RULES:
        if kw in reason:
            return code
    return "OTHER"


# ----------------------------------------------------------------------
# 账本作用域（线上模拟盘 / 并行模拟账本）
# ----------------------------------------------------------------------
#: 并行模拟账本的独立表：与 paper_* 一比一同构，只多一列 account_id。
#: 线上模拟盘继续用 paper_* 表（单账户），影子账本写 sim_* 表，互不影响。
_SIM_TABLES = {
    "paper_account": "sim_account",
    "paper_positions": "sim_positions",
    "paper_position_lots": "sim_lots",
    "paper_trades": "sim_trades",
    "paper_equity": "sim_equity",
}

#: 当前账本 id；None = 线上主账本（paper_* 表）。
#: 用 ContextVar 而不是给引擎里 20 多处 db 调用加参数：tick 里
#: ``with ledger_scope(id)`` 把一个代码块切到某个影子账本，引擎内的
#: ``db.paper_*`` 会自动落到 sim_* 表 —— 交易规则代码一行都不用改，
#: 也就不会出现「改漏一处导致线上主账本被污染」。
_LEDGER_CTX: ContextVar[int | None] = ContextVar("sas_ledger", default=None)


@contextmanager
def ledger_scope(account_id: int | None):
    """把 with 块内所有 ``db.paper_*`` 调用路由到指定模拟账本。"""
    token = _LEDGER_CTX.set(account_id)
    try:
        yield
    finally:
        _LEDGER_CTX.reset(token)


def current_ledger() -> int | None:
    """当前账本 id（None=线上主账本）。"""
    return _LEDGER_CTX.get()


class _Scope:
    """当前账本的 SQL 片段生成器：表名 / account_id 过滤 / 前缀参数。

    ``where``/``args``/``cols``/``vals`` 必须成对使用，参数顺序才与列顺序一致。
    """

    __slots__ = ("account_id",)

    def __init__(self, account_id: int | None) -> None:
        self.account_id = account_id

    @property
    def sim(self) -> bool:
        return self.account_id is not None

    def t(self, base: str) -> str:
        return _SIM_TABLES[base] if self.sim else base

    def where(self, cond: str = "") -> str:
        if not self.sim:
            return f" WHERE {cond}" if cond else ""
        return f" WHERE account_id=? AND {cond}" if cond else " WHERE account_id=?"

    def args(self, *values: Any) -> tuple:
        return (self.account_id, *values) if self.sim else values

    def cols(self, cols: str) -> str:
        return f"account_id,{cols}" if self.sim else cols

    def vals(self, n: int) -> str:
        return ",".join(["?"] * (n + 1)) if self.sim else ",".join(["?"] * n)

    def conflict(self, key: str) -> str:
        return f"account_id,{key}" if self.sim else key


def _scope() -> _Scope:
    return _Scope(_LEDGER_CTX.get())


# ----------------------------------------------------------------------
# 换手率补齐（成交量 ÷ 流通股本）
# ----------------------------------------------------------------------
#: 反推流通股本时的滚动中位数窗口（按观测数，不是自然日）
_FLOAT_SHARE_WINDOW = 21
#: 反推换手率的合理上限（%）：超过说明流通股本取错（新股/退市残留）
_MAX_TURNOVER = 100.0


def float_shares_series(df: pd.DataFrame) -> pd.Series | None:
    """由「成交量 + 换手率」反推逐日流通股本（股本不变时应近似常数）。

    口径：``换手率(%) = 成交量(手) × 10000 / 流通股本(股)``。
    同一行同时有成交量与换手率时可直接解出流通股本；两位小数的换手率
    （0.14% 时 ±0.005 就是 ±3.6%）靠滚动中位数压掉舍入噪声。

    Returns:
        与 ``df`` 同索引的流通股本（股）；无可用观测（或缺列）时返回 None。
    """
    if "volume" not in df.columns or "turnover_rate" not in df.columns:
        return None
    volume = pd.to_numeric(df["volume"], errors="coerce")
    turnover = pd.to_numeric(df["turnover_rate"], errors="coerce")
    obs = turnover.notna() & (turnover > 0) & volume.notna() & (volume > 0)
    if not bool(obs.any()):
        return None
    shares = volume[obs] * 10000.0 / turnover[obs]
    shares = shares.rolling(_FLOAT_SHARE_WINDOW, min_periods=1, center=True).median()
    return shares.reindex(df.index).ffill().bfill()


def fill_turnover(
    df: pd.DataFrame, fallback_shares: float | None = None
) -> tuple[pd.DataFrame, int, int]:
    """补齐 ``turnover_rate`` 空值（腾讯/新浪 K 线兜底源不返回换手率）。

    流通股本优先用该股自己历史行情的反推值（真实股本变化时缺口里用的是
    「最近的已知值」，而不是拿今天的股本回填三年前）；完全没有观测时用
    ``fallback_shares``（如腾讯实时行情的 ``float_shares``）兜底。

    Args:
        df: 日线，需含 ``volume`` / ``turnover_rate``
        fallback_shares: 无历史观测时的流通股本（股）

    Returns:
        ``(新df, 补齐行数, 越界丢弃行数)``
    """
    if "volume" not in df.columns or "turnover_rate" not in df.columns:
        return df, 0, 0
    volume = pd.to_numeric(df["volume"], errors="coerce")
    turnover = pd.to_numeric(df["turnover_rate"], errors="coerce")
    shares = float_shares_series(df)
    if shares is None:
        if not fallback_shares or fallback_shares <= 0:
            return df, 0, 0
        shares = pd.Series(float(fallback_shares), index=df.index)

    target = turnover.isna() & volume.notna() & (volume > 0) & shares.notna() & (shares > 0)
    if not bool(target.any()):
        return df, 0, 0
    derived = (volume[target] * 10000.0 / shares[target]).round(4)
    good = (derived > 0) & (derived <= _MAX_TURNOVER)
    out = df.copy()
    out.loc[derived[good].index, "turnover_rate"] = derived[good]
    return out, int(good.sum()), int((~good).sum())


class KlineCache:
    """日K线 Parquet 缓存。

    每个股票一个文件：``{kline_dir}/{code}.parquet``。
    通过 ``last_date`` 元数据配合数据源实现增量更新（只拉取新数据）。
    """

    def __init__(self, kline_dir: Path | None = None) -> None:
        settings = get_settings()
        self.dir = kline_dir or settings.kline_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, code: str) -> Path:
        return self.dir / f"{code}.parquet"

    def get(self, code: str) -> pd.DataFrame | None:
        """读取缓存的K线；不存在或损坏返回 None。"""
        path = self._path(code)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            return df if not df.empty else None
        except Exception:
            # 缓存文件损坏时视为未命中，允许重新拉取
            return None

    def last_date(self, code: str) -> str | None:
        """返回缓存中最后一条K线的日期（YYYY-MM-DD），用于增量更新。"""
        df = self.get(code)
        if df is None:
            return None
        return str(df["date"].iloc[-1])

    def save(self, code: str, df: pd.DataFrame) -> None:
        """保存/更新K线（按日期去重合并后原子写入）。

        合并后顺手补齐 ``turnover_rate``：腾讯/新浪兜底 K 线不带换手率，
        不补的话每天新增的行都会是空值（14 因子的换手率分项恒为 0）。
        用该股历史行情的反推值填充，纯本地计算，不额外请求数据源。
        """
        if df is None or df.empty:
            return
        existing = self.get(code)
        if existing is not None:
            # 合并并按日期去重（同一天以新拉取的数据为准）
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date").reset_index(drop=True)
        df, _, _ = fill_turnover(df)
        df.to_parquet(self._path(code), index=False)


# SQLite 建表语句：自选股 / 策略信号 / 回测记录
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    -- 来源：auto=程序自选（扫描/收盘任务写入），manual=手动自选（用户添加）
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    score      REAL                  -- 最近一次写入时的技术面评分（淘汰排序/展示用，可空）
);
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    strategy     TEXT NOT NULL,
    price        REAL,
    reason       TEXT DEFAULT '',
    stop_loss    REAL,
    target_price REAL,
    score        REAL,            -- 技术面评分（第 1 次排序依据）
    rank         INTEGER DEFAULT 0,  -- 本轮扫描名次（1 起，同股并列）
    scan_date    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_code      ON signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_scan_date ON signals(scan_date);
-- 模拟交易（Paper Trading）：单账户 + 持仓 + 交易流水 + 每日净值
CREATE TABLE IF NOT EXISTS paper_account (
    id             INTEGER PRIMARY KEY CHECK (id = 1),  -- 单账户约束
    initial_capital REAL NOT NULL,   -- 初始资金
    cash           REAL NOT NULL,    -- 可用现金
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    code       TEXT PRIMARY KEY,
    name       TEXT,
    shares     INTEGER NOT NULL,     -- 持仓股数（整手100）
    cost_price REAL NOT NULL,        -- 摊薄成本价（含买入手续费）
    buy_date   TEXT NOT NULL,        -- 最近一次买入日期（T+1 可卖判断）
    strategy   TEXT,                 -- 买入来源（策略/建议原因）
    score      REAL DEFAULT 0,       -- 建仓时的建议评分（轮动替换的比较基准）
    stop_price REAL,                 -- 止损线（买入时按 ATR/固定比例算好，NULL=按固定 8% 现算）
    peak_price REAL,                 -- 持仓期最高价（移动/保本止损跟踪）
    sellable_shares INTEGER DEFAULT 0,  -- 可卖股数（T+1：buy_date<今日 的批次合计）
    t_t_sold   INTEGER DEFAULT 0,    -- 做T：已卖出待买回股数（倒T/涨停卖半）
    t_sold_date TEXT,                -- 做T：上述待买回的卖出日（<今日=跨日待买回，次日开盘恢复底仓）
    t_down_armed INTEGER DEFAULT 0,  -- 做T：下跌加仓已触发（防每tick重复）
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_position_lots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,       -- 持仓批次（做T/严格 T+1：每批带 buy_date 算可卖）
    shares      INTEGER NOT NULL,
    cost_price  REAL NOT NULL,
    buy_date    TEXT NOT NULL,       -- 该批次买入日（T+1 可卖判断）
    tag         TEXT DEFAULT 'base', -- base=底仓 / t_add=做T加仓 / t_buyback=做T买回
    provenance  TEXT,                -- #115 建仓溯源 JSON（触发信号 + regime + 决策理由）
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lots_code ON paper_position_lots(code);
CREATE TABLE IF NOT EXISTS paper_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    name       TEXT,
    side       TEXT NOT NULL,        -- buy / sell
    price      REAL NOT NULL,        -- 成交价
    shares     INTEGER NOT NULL,     -- 成交股数
    amount     REAL NOT NULL,        -- 成交金额（不含费用）
    fee        REAL NOT NULL,        -- 费用合计（佣金+印花税+过户费）
    reason     TEXT,                 -- 交易原因（人类可读文本）
    trade_date TEXT NOT NULL,        -- 信号/交易日期
    exit_reason TEXT,                -- 卖出原因结构化编码（#191）：STOP_LOSS / TAKE_PROFIT / TIME_STOP / SIGNAL / ROTATION / EOD_TRIM / OFFBOARD / MANUAL / T_SELL ...
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_date ON paper_trades(trade_date);
-- 每日净值快照（模拟盘净值曲线）
CREATE TABLE IF NOT EXISTS paper_equity (
    date          TEXT PRIMARY KEY,
    total_asset   REAL NOT NULL,     -- 总资产 = 现金 + 持仓市值
    cash          REAL NOT NULL,
    market_value  REAL NOT NULL,
    created_at    TEXT NOT NULL
);
-- 实时撮合队列持久化（pending/backlog 镜像，重启后回填；不含影子账本队列）
CREATE TABLE IF NOT EXISTS paper_pending (
    code            TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- 'pending' | 'backlog'
    name            TEXT,
    action          TEXT,            -- buy / sell
    reason          TEXT,
    created_at      TEXT NOT NULL,   -- ISO 时间
    expire_at       TEXT NOT NULL,   -- ISO 时间
    price_at_signal REAL,
    score           REAL DEFAULT 0,
    scan            TEXT,           -- JSON: {"task_id":..,"strategy":..}（选股扫描命中来源，重启后回填徽标用）
    PRIMARY KEY (code, kind)
);
-- 并行模拟账本（同一份信号 + 不同处置参数各自记账；与 paper_* 完全隔离，
-- 主键带 account_id，因此同一代码在不同账本可以各自持仓、各有一条净值）
CREATE TABLE IF NOT EXISTS sim_account (
    account_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tag             TEXT NOT NULL UNIQUE,        -- 方案标签（前端/接口用它定位账本）
    profile         TEXT NOT NULL DEFAULT '',    -- 来源方案名（app/backtest/profiles.py）
    overrides       TEXT NOT NULL DEFAULT '{}',  -- 已换算成 paper_* 的覆盖参数（JSON）
    initial_capital REAL NOT NULL,
    cash            REAL NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_positions (
    account_id INTEGER NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    shares     INTEGER NOT NULL,
    cost_price REAL NOT NULL,
    buy_date   TEXT NOT NULL,
    strategy   TEXT,
    score      REAL DEFAULT 0,
    stop_price REAL,
    peak_price REAL,
    sellable_shares INTEGER DEFAULT 0,
    t_t_sold   INTEGER DEFAULT 0,
    t_sold_date TEXT,
    t_down_armed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, code)
);
CREATE TABLE IF NOT EXISTS sim_lots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    code       TEXT NOT NULL,
    shares     INTEGER NOT NULL,
    cost_price REAL NOT NULL,
    buy_date   TEXT NOT NULL,
    tag        TEXT DEFAULT 'base',
    provenance TEXT,                 -- 必须与 paper_position_lots 同构：add_paper_lot 统一写这列
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_lots_acct ON sim_lots(account_id, code);
CREATE TABLE IF NOT EXISTS sim_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    side       TEXT NOT NULL,
    price      REAL NOT NULL,
    shares     INTEGER NOT NULL,
    amount     REAL NOT NULL,
    fee        REAL NOT NULL,
    reason     TEXT,
    trade_date TEXT NOT NULL,
    exit_reason TEXT,                -- 与 paper_trades 同构：结构化卖出原因编码
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_trades_acct ON sim_trades(account_id, trade_date);
CREATE TABLE IF NOT EXISTS sim_equity (
    account_id   INTEGER NOT NULL,
    date         TEXT NOT NULL,
    total_asset  REAL NOT NULL,
    cash         REAL NOT NULL,
    market_value REAL NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, date)
);
-- 智能建议（盘中买卖建议 / 盘后明日关注，按批次存储）
CREATE TABLE IF NOT EXISTS recommendations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT NOT NULL,
    name         TEXT,
    action       TEXT NOT NULL,   -- buy / sell / hold / focus
    score        REAL,            -- 技术面评分
    reason       TEXT,
    price        REAL,
    stop_loss    REAL,
    target_price REAL,
    sentiment    TEXT,            -- 生成时的市场情绪等级
    rank         INTEGER DEFAULT 0,  -- 本轮自选股排名（1 起，0=未参与排序）
    detail       TEXT DEFAULT '[]',  -- 因子分项明细（JSON）
    batch        TEXT NOT NULL,   -- 批次：intraday_/premarket_/focus_ + 日期
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reco_batch ON recommendations(batch);
-- 自定义策略（用户在前端改参数另存，启动时自动注册进注册表）
CREATE TABLE IF NOT EXISTS custom_strategies (
    name          TEXT PRIMARY KEY,
    base_strategy TEXT NOT NULL,   -- 基础内置策略名（提供 evaluate 逻辑）
    description   TEXT,
    category      TEXT,
    params        TEXT NOT NULL,   -- JSON：用户参数 {key: value}
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy     TEXT NOT NULL,
    start_date   TEXT,
    end_date     TEXT,
    params       TEXT,   -- JSON：本次回测的全部参数（默认值 + 覆盖项）
    metrics      TEXT,   -- JSON：收益率/胜率/最大回撤/夏普等
    equity_curve TEXT,   -- JSON：[{date, equity, benchmark, cash, positions}] 逐日过程数据
    positions    TEXT,   -- JSON：期末持仓快照
    review       TEXT,   -- JSON：亏损归因 + 调参建议
    job_id       TEXT,   -- 产出该记录的后台任务 id（与 backend.log 对齐）
    elapsed      REAL,   -- 回测耗时（秒）
    created_at   TEXT NOT NULL
);
-- 逐笔成交明细：单独成表而不是塞进 backtest_runs 的 JSON 列，是为了能直接做
-- 「按卖出原因 / 按股票 / 按建仓评分」的 SQL 聚合（复盘与参数对比都靠它）。
CREATE TABLE IF NOT EXISTS backtest_trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    buy_date   TEXT,
    sell_date  TEXT,
    buy_price  REAL,
    sell_price REAL,
    shares     INTEGER,
    amount     REAL,
    fee        REAL,
    pnl        REAL,
    pnl_pct    REAL,
    reason     TEXT,
    score      REAL
);
CREATE INDEX IF NOT EXISTS idx_bt_trades_run ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_trades_code ON backtest_trades(code);
-- 运行时状态（交易模式等少量键值，重启后保留）
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 六阶段生命周期事件（#191）：全局事件表，贯穿 初选→自选→队列→入场→持有→离场，
-- 用 code + created_at 串联任意一只票的完整旅程，支持复盘「哪个信号导致哪笔成交」。
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,            -- 股票代码（跨阶段追踪主体）
    name       TEXT,
    stage      INTEGER NOT NULL,         -- 阶段序号：1 初选 / 2 入自选 / 3 进队列 / 4 入场 / 5 持有 / 6 离场
    stage_name TEXT NOT NULL,            -- 阶段中文名
    event      TEXT NOT NULL,            -- 事件类型：scan_hit / signal_in / watch_in / queued / filled / hold / exited ...
    detail     TEXT,                     -- JSON 或自由文本
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_code ON lifecycle_events(code);
CREATE INDEX IF NOT EXISTS idx_lifecycle_stage ON lifecycle_events(stage);
-- P1 上下文风险闸：按 as-of 读取的本地快照（策略代码绝不直接调实时接口）
-- 事件日历：*_date <= as_of 才可查，禁止未来函数；采集器写入，策略只读。
CREATE TABLE IF NOT EXISTS context_events (
    code        TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    severity    REAL NOT NULL DEFAULT 1.0,
    title       TEXT,
    source      TEXT,
    collected_at TEXT,
    PRIMARY KEY (code, event_date, kind)
);
CREATE INDEX IF NOT EXISTS idx_ctx_events_code_date ON context_events(code, event_date);
CREATE INDEX IF NOT EXISTS idx_ctx_events_date ON context_events(event_date);
-- 舆情新闻：sentiment ∈ [-1, 1]，负=利空；heat 为权重。
CREATE TABLE IF NOT EXISTS context_news (
    code        TEXT NOT NULL,
    news_date   TEXT NOT NULL,
    sentiment   REAL NOT NULL,
    heat        REAL NOT NULL DEFAULT 1.0,
    title       TEXT NOT NULL DEFAULT '',
    source      TEXT,
    collected_at TEXT,
    PRIMARY KEY (code, news_date, title)
);
CREATE INDEX IF NOT EXISTS idx_ctx_news_code_date ON context_news(code, news_date);
-- 板块/个股热度分位：由本地日线按交易日预计算，只用当日及之前 close。
-- 两侧共读这张表，保证「最热 N%」阈值不随各自候选池大小变化。
CREATE TABLE IF NOT EXISTS context_heat (
    trade_date      TEXT PRIMARY KEY,
    stock_cut       REAL,
    industry_cut    REAL,
    hot_industries  TEXT NOT NULL DEFAULT '[]',
    sample_count    INTEGER NOT NULL DEFAULT 0,
    built_at        TEXT
);
-- 影子研究专用：凌晨生成的「隔夜市场环境」日频快照（2026-11-13 用户确认先做影子研究）。
-- ⚠ 决策边界：禁止任何选股/买入/仓位/止损链路读取本表。若将来要接入，
--    必须回测↔实盘 1:1 同步（BINDINGS + 行为探针）并重建前向验证基线。
-- 口径：trade_date = A 股交易日；us_session_date 严格 < trade_date
--    （美股 D 日收盘发生在北京时间 D+1 凌晨，早于 A 股 T 日 09:30，零未来函数）。
-- policy_score 为 NULL 表示「历史不可得/无数据」，不是中性 0 分。
CREATE TABLE IF NOT EXISTS market_env (
    trade_date       TEXT PRIMARY KEY,
    us_session_date  TEXT,
    spx_ret          REAL,
    ndx_ret          REAL,
    dji_ret          REAL,
    us_risk_score    REAL,
    policy_count     INTEGER NOT NULL DEFAULT 0,
    policy_score     REAL,
    env_score        REAL,
    env_label        TEXT,
    source           TEXT,
    built_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_env_date ON market_env(trade_date);
"""


class Database:
    """SQLite 元数据库（线程安全，WAL 模式）。

    注意：实时快照（盘中每 30 秒 × 5000 只）不落库，
    只放内存并按需聚合，避免数据库文件无限膨胀。
    """

    def __init__(self, db_path: Path | None = None) -> None:
        settings = get_settings()
        self.path = db_path or settings.db_path
        self._lock = threading.Lock()
        # 每线程复用一个连接。旧实现每次 ``_connect()`` 都新建连接：``with conn``
        # 只提交、不关闭，连接全靠 GC 回收，一个自选/建议类请求会连建十几次，
        # 每次还要跑一遍 ``PRAGMA journal_mode``。按线程缓存后稳态不再建连。
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """取当前线程的 SQLite 连接（首次调用时建立，之后复用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        # check_same_thread=False：连接只在本线程使用，保留该选项以免
        # 将来跨线程归还连接时报 ProgrammingError。
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # 多写者（服务 + 备份/复盘脚本）并发落库时，默认会立刻抛 SQLITE_BUSY；
        # 等 5 秒再放弃，配合 WAL 基本不再丢写。
        conn.execute("PRAGMA busy_timeout=5000")
        self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._migrate_watchlist_source(conn)
            self._migrate_watchlist_score(conn)
            self._migrate_recommendation_rank(conn)
            self._migrate_signal_rank(conn)
            self._migrate_position_score(conn)
            self._migrate_position_stop(conn)
            self._migrate_position_peak(conn)
            self._migrate_position_lots(conn)
            self._migrate_lots_tag(conn)
            self._migrate_lots_provenance(conn)
            self._migrate_pending_scan(conn)
            self._migrate_backtest_extra(conn)
            self._migrate_paper_trades_exit_reason(conn)
            self._dedupe_signals(conn)

    @staticmethod
    def _migrate_backtest_extra(conn: sqlite3.Connection) -> None:
        """老库给 backtest_runs 补列：期末持仓 / 归因 / 任务 id / 耗时。

        ``_SCHEMA_SQL`` 里的 ``CREATE TABLE IF NOT EXISTS`` 只对新建库生效，
        已有历史记录的库必须靠 ALTER 补。历史记录这几列为 NULL，
        读取侧按空值降级，不回填——它们是「当时没存」，不是「存了空」。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(backtest_runs)")}
        for name, ddl in (
            ("positions", "ALTER TABLE backtest_runs ADD COLUMN positions TEXT"),
            ("review", "ALTER TABLE backtest_runs ADD COLUMN review TEXT"),
            ("job_id", "ALTER TABLE backtest_runs ADD COLUMN job_id TEXT"),
            ("elapsed", "ALTER TABLE backtest_runs ADD COLUMN elapsed REAL"),
        ):
            if name not in cols:
                conn.execute(ddl)

    @staticmethod
    def _dedupe_signals(conn: sqlite3.Connection) -> None:
        """清理信号表里的重复行（同一股票+策略+交易日只保留最新一条）。

        每天多轮扫描会重复命中同一只股票，历史信号流水会被重复行刷屏。
        写入侧已在 save_signals 里改为「同日覆盖」，这里只清理历史残留。
        不建唯一索引：滚动升级期间旧进程仍可能重复插入，建索引会让其写入报错。
        """
        conn.execute(
            "DELETE FROM signals WHERE id NOT IN ("
            "  SELECT MAX(id) FROM signals GROUP BY code, strategy, scan_date)"
        )

    @staticmethod
    def _migrate_watchlist_source(conn: sqlite3.Connection) -> None:
        """老库补 source 列：历史备注含“自动加入”的视为程序自选。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(watchlist)")}
        if "source" in cols:
            return
        conn.execute(
            "ALTER TABLE watchlist ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
        )
        conn.execute(
            "UPDATE watchlist SET source='auto' WHERE note LIKE '%自动加入%'"
        )

    @staticmethod
    def _migrate_position_score(conn: sqlite3.Connection) -> None:
        """老库补 score 列：持仓的建仓评分（轮动替换的比较基准）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_positions)")}
        if "score" not in cols:
            conn.execute(
                "ALTER TABLE paper_positions ADD COLUMN score REAL DEFAULT 0"
            )

    @staticmethod
    def _migrate_position_stop(conn: sqlite3.Connection) -> None:
        """老库补 stop_price 列：建仓时按波动率算好的止损线。

        以前止损线是「成本 × (1-8%)」实时现算，高波动票会被噪声打掉；
        现在建仓时就按 max(8%, N×ATR%) 定下来，落库便于复盘「当时定的止损是多少」。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_positions)")}
        if "stop_price" not in cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN stop_price REAL")

    @staticmethod
    def _migrate_position_peak(conn: sqlite3.Connection) -> None:
        """老库补 peak_price 列：持仓期最高价（移动/保本止损跟踪用）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_positions)")}
        if "peak_price" not in cols:
            conn.execute("ALTER TABLE paper_positions ADD COLUMN peak_price REAL")

    @staticmethod
    def _migrate_position_lots(conn: sqlite3.Connection) -> None:
        """老库补 sellable_shares 列（可卖股数=T+1 判定用）；批次表已在 schema 建好。

        历史持仓不在此处灌入批次——由 engine 启动时的 ensure_lots_migrated()
        在切换到批次模型后幂等补建，避免「旧引擎已灌 lots 但还没切批次逻辑」的窗口不一致。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_positions)")}
        if "sellable_shares" not in cols:
            conn.execute(
                "ALTER TABLE paper_positions ADD COLUMN sellable_shares INTEGER DEFAULT 0"
            )
        if "t_t_sold" not in cols:
            conn.execute(
                "ALTER TABLE paper_positions ADD COLUMN t_t_sold INTEGER DEFAULT 0"
            )
        if "t_down_armed" not in cols:
            conn.execute(
                "ALTER TABLE paper_positions ADD COLUMN t_down_armed INTEGER DEFAULT 0"
            )
        if "t_sold_date" not in cols:
            # 做T待买回的卖出日：跨日后仍可识别「往日待买回」，次日开盘恢复底仓
            conn.execute(
                "ALTER TABLE paper_positions ADD COLUMN t_sold_date TEXT"
            )

    @staticmethod
    def _migrate_lots_tag(conn: sqlite3.Connection) -> None:
        """老库补 lots 表的 tag 列（区分底仓/做T加仓/做T买回）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_position_lots)")}
        if "tag" not in cols:
            conn.execute("ALTER TABLE paper_position_lots ADD COLUMN tag TEXT DEFAULT 'base'")

    @staticmethod
    def _migrate_lots_provenance(conn: sqlite3.Connection) -> None:
        """老库补 provenance 列：每个批次挂「触发信号 + regime 快照 + 决策理由」溯源。

        建仓时一次性写入 JSON；历史批次该列为 NULL，读取侧按空值降级
        （「当时没存」而非「存了空」）。见 BACKTEST_LOG.md #115。

        **两张 lots 表都要补**：`ledger_scope` 会把并行账本的 `add_paper_lot` 路由到
        `sim_lots`。#115 只补了主账本，结果每个并行账本一建仓就抛
        `no column named provenance`，被 `_tick_sim` 的兜底吞成一条 warning——
        净值还在按价格刷新，看着完全正常，实际一本都没在买。
        """
        for table in ("paper_position_lots", "sim_lots"):
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            # 表不存在时 PRAGMA 返回空（未启用并行账本的老库），跳过即可
            if cols and "provenance" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN provenance TEXT")

    @staticmethod
    def _migrate_pending_scan(conn: sqlite3.Connection) -> None:
        """老库给 paper_pending 补 scan 列：记录信号是否来自选股扫描命中。

        重启后（每日 SASRestart / 部署重启）从库回填撮合队列时，若没有该列，
        选股来源的「选股扫描」徽标会在重启后消失。见 #137 续。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(paper_pending)")}
        if "scan" not in cols:
            conn.execute("ALTER TABLE paper_pending ADD COLUMN scan TEXT")

    @staticmethod
    def _migrate_signal_rank(conn: sqlite3.Connection) -> None:
        """老库补 score / rank 列（第 1 次排序：扫描命中评分与名次）。"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}
        if "score" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN score REAL")
        if "rank" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN rank INTEGER DEFAULT 0")

    @staticmethod
    def _migrate_paper_trades_exit_reason(conn: sqlite3.Connection) -> None:
        """老库给 paper_trades / sim_trades 补 exit_reason 列（#191 结构化卖出原因编码）。

        **两张 trades 表都要补**：`ledger_scope` 会把并行账本的 `insert_paper_trade`
        路由到 `sim_trades`，只补主表会让并行账本每笔成交都抛
        `no column named exit_reason`（同 #115 provenance 的坑）。
        """
        for table in ("paper_trades", "sim_trades"):
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            # 表不存在时 PRAGMA 返回空（未启用并行账本的老库），跳过即可
            if cols and "exit_reason" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN exit_reason TEXT")

    @staticmethod
    def _migrate_recommendation_rank(conn: sqlite3.Connection) -> None:
        """老库补 rank / detail 列（第 2 次排序的名次与因子分项）。"""
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(recommendations)")
        }
        if "rank" not in cols:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN rank INTEGER DEFAULT 0"
            )
        if "detail" not in cols:
            conn.execute(
                "ALTER TABLE recommendations ADD COLUMN detail TEXT DEFAULT '[]'"
            )

    # ---------------- 运行时状态 ----------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """读取运行时状态（少量键值对，重启后保留）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def delete_state(self, key: str) -> None:
        """删除运行时状态（系统设置页「还原为 .env 值」用）。"""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM app_state WHERE key=?", (key,))

    def set_state(self, key: str, value: str) -> None:
        """写入运行时状态。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state(key, value, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, str(value), _now()),
            )

    # ---------------- P1 上下文快照（只由采集脚本写，策略侧只读） ----------------

    def upsert_context_events(self, rows: list[tuple]) -> int:
        """批量写事件日历。rows = (code, event_date, kind, severity, title, source)。"""
        if not rows:
            return 0
        payload = [
            (str(c), str(d)[:10], str(k), float(sev), str(title or ""), str(src or ""), _now())
            for c, d, k, sev, title, src in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO context_events(code,event_date,kind,severity,title,source,collected_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code,event_date,kind) DO UPDATE SET "
                "severity=excluded.severity, title=excluded.title, "
                "source=excluded.source, collected_at=excluded.collected_at",
                payload,
            )
        return len(payload)

    def upsert_context_news(self, rows: list[tuple]) -> int:
        """批量写舆情新闻。rows = (code, news_date, sentiment, heat, title, source)。"""
        if not rows:
            return 0
        payload = [
            (str(c), str(d)[:10], float(s), float(h), str(t or ""), str(src or ""), _now())
            for c, d, s, h, t, src in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO context_news(code,news_date,sentiment,heat,title,source,collected_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(code,news_date,title) DO UPDATE SET "
                "sentiment=excluded.sentiment, heat=excluded.heat, "
                "source=excluded.source, collected_at=excluded.collected_at",
                payload,
            )
        return len(payload)

    def normalize_context_event_kinds(self) -> int:
        """删掉「全市场公告被误归 earnings」的行（采集规则收紧后的历史清洗）。

        这些是调研/说明会等普通公告；同票同日往往已有 ``other`` 行，直接 UPDATE
        会撞主键唯一约束。真实业绩预告由 ``yjyg_em`` 单独落库，不受影响。
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM context_events "
                "WHERE source='notice_report' AND kind='earnings'"
            )
            return cur.rowcount

    def upsert_context_heat(self, rows: list[tuple]) -> int:
        """批量写每日热度分位。rows = (trade_date, stock_cut, industry_cut, hot_industries_json, n)。"""
        if not rows:
            return 0
        payload = [
            (str(d)[:10], sc, ic, str(hi or "[]"), int(n), _now())
            for d, sc, ic, hi, n in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO context_heat(trade_date,stock_cut,industry_cut,hot_industries,"
                "sample_count,built_at) VALUES(?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "stock_cut=excluded.stock_cut, industry_cut=excluded.industry_cut, "
                "hot_industries=excluded.hot_industries, sample_count=excluded.sample_count, "
                "built_at=excluded.built_at",
                payload,
            )
        return len(payload)

    # ---------------- 隔夜市场环境快照（影子研究专用，不进决策） ----------------

    def upsert_market_env(self, rows: list[dict]) -> int:
        """批量写 market_env（按 trade_date 幂等）。policy_score=None 表示历史不可得。"""
        if not rows:
            return 0
        payload = [
            (str(r["trade_date"])[:10],
             r.get("us_session_date"), r.get("spx_ret"), r.get("ndx_ret"),
             r.get("dji_ret"), r.get("us_risk_score"),
             int(r.get("policy_count") or 0), r.get("policy_score"),
             r.get("env_score"), r.get("env_label"),
             str(r.get("source") or "unknown"), _now())
            for r in rows
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO market_env(trade_date,us_session_date,spx_ret,ndx_ret,dji_ret,"
                "us_risk_score,policy_count,policy_score,env_score,env_label,source,built_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "us_session_date=excluded.us_session_date, spx_ret=excluded.spx_ret, "
                "ndx_ret=excluded.ndx_ret, dji_ret=excluded.dji_ret, "
                "us_risk_score=excluded.us_risk_score, policy_count=excluded.policy_count, "
                "policy_score=excluded.policy_score, env_score=excluded.env_score, "
                "env_label=excluded.env_label, source=excluded.source, built_at=excluded.built_at",
                payload,
            )
        return len(payload)

    def market_env_rows(self, start: str, end: str) -> list[dict]:
        """按 trade_date 区间读快照（只读；影子研究评估用）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM market_env WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (str(start)[:10], str(end)[:10]),
            ).fetchall()
        return [dict(r) for r in rows]

    def context_trade_dates(self, start: str, end: str) -> list[str]:
        """读两侧共用的 A 股交易日列表（context_heat 的预计算日期）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_date FROM context_heat WHERE trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (str(start)[:10], str(end)[:10]),
            ).fetchall()
        return [str(r[0])[:10] for r in rows]

    # ---------------- 自选股 ----------------

    def add_watch(
        self, code: str, name: str, note: str = "", source: str = "manual"
    ) -> None:
        """添加/更新自选股（按 code 主键幂等）。

        来源优先级：已存在于「手动自选」的记录不会被程序自选覆盖，
        避免扫描把用户主动关注的股票降级为程序自选。
        """
        from app.data.scope import clean_name

        name = clean_name(code, name)      # 入库即清洗，避免 GBK 乱码写进库
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO watchlist(code, name, note, source, created_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name, "
                "source=CASE WHEN watchlist.source='manual' THEN 'manual' "
                "ELSE excluded.source END",
                (code, name, note, source, _now()),
            )

    def add_watch_many(
        self, items: list[tuple], source: str = "auto", note: str = ""
    ) -> int:
        """批量加入自选股，返回真正新增的条数（已存在的手动自选不计入）。

        items 元素为 ``(code, name)`` 或 ``(code, name, score)``；缺省 score 记为 0。
        """
        if not items:
            return 0
        from app.data.scope import clean_name

        added = 0
        with self._lock, self._connect() as conn:
            for item in items:
                if len(item) == 3:
                    code, name, score = item
                else:
                    code, name = item
                    score = 0.0
                name = clean_name(code, name)   # 入库即清洗乱码名称
                exists = conn.execute(
                    "SELECT source FROM watchlist WHERE code=?", (code,)
                ).fetchone()
                if exists is not None and exists["source"] == "manual":
                    continue
                if exists is None:
                    added += 1
                conn.execute(
                    "INSERT INTO watchlist(code, name, note, source, created_at, score) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
                    "source=excluded.source, note=excluded.note, score=excluded.score",
                    (code, name, note, source, _now(), score),
                )
        return added

    def prune_watch_auto(self, keep_codes: set[str]) -> int:
        """删除「程序自选」中不在 keep_codes 的记录（统一评分后淘汰低分用）。不动 manual。"""
        with self._lock, self._connect() as conn:
            if not keep_codes:
                cur = conn.execute("DELETE FROM watchlist WHERE source='auto'")
            else:
                placeholders = ",".join("?" for _ in keep_codes)
                cur = conn.execute(
                    f"DELETE FROM watchlist WHERE source='auto' AND code NOT IN ({placeholders})",
                    tuple(keep_codes),
                )
            return cur.rowcount or 0

    @staticmethod
    def _migrate_watchlist_score(conn: sqlite3.Connection) -> None:
        """老库给 watchlist 补 score 列（新建库已在 _SCHEMA_SQL 内）。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(watchlist)")}
        if "score" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN score REAL")

    def set_watch_source(self, codes: list[str], source: str) -> int:
        """批量修改自选股来源（程序自选 → 手动自选），返回实际更新条数。"""
        if not codes:
            return 0
        placeholders = ",".join("?" for _ in codes)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE watchlist SET source=? WHERE code IN ({placeholders})",
                (source, *codes),
            )
            return cur.rowcount or 0

    def remove_watch(self, code: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE code=?", (code,))

    def list_watchlist(self, source: str | None = None) -> list[dict[str, Any]]:
        """自选股列表；source 为空时返回全部（auto/manual）。"""
        with self._lock, self._connect() as conn:
            if source:
                rows = conn.execute(
                    "SELECT code, name, note, source, created_at FROM watchlist "
                    "WHERE source=? ORDER BY created_at",
                    (source,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT code, name, note, source, created_at FROM watchlist "
                    "ORDER BY created_at"
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 策略信号 ----------------

    def save_signals(self, signals: list[dict[str, Any]], scan_date: str) -> None:
        """批量保存一轮扫描的命中信号（同日同股同策略覆盖，写入最新触发数据）。"""
        if not signals:
            return
        with self._lock, self._connect() as conn:
            for s in signals:
                # 先删同日同股同策略的旧记录再插入：等价于覆盖，
                # 无需唯一索引（滚动升级期间旧进程写入不会因约束报错）
                conn.execute(
                    "DELETE FROM signals WHERE code=? AND strategy=? AND scan_date=?",
                    (s["code"], s["strategy"], scan_date),
                )
                extra = s.get("extra") or {}
                score = s.get("score", extra.get("score"))
                conn.execute(
                    "INSERT INTO signals(code, name, strategy, price, reason,"
                    " stop_loss, target_price, score, rank, scan_date, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        s["code"], s.get("name", ""), s["strategy"], s.get("price"),
                        s.get("reason", ""), s.get("stop_loss"), s.get("target_price"),
                        score, int(s.get("rank") or 0), scan_date, _now(),
                    ),
                )

    def list_signals(
        self, code: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """查询历史信号（可按股票过滤，按时间倒序）。"""
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE code=? ORDER BY id DESC LIMIT ?",
                    (code, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def signal_stats_by_date(self, scan_date: str) -> dict[str, int]:
        """统计某日各策略的命中数量（选股中心策略徽标用）。

        Returns:
            {策略名: 命中数}
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT strategy, COUNT(*) AS cnt FROM signals"
                " WHERE scan_date=? GROUP BY strategy",
                (scan_date,),
            ).fetchall()
        return {r["strategy"]: r["cnt"] for r in rows}

    # ---------------- 模拟交易 ----------------

    def get_paper_account(self) -> dict[str, Any] | None:
        """模拟账户（不存在返回 None）。账本作用域内返回该影子账本的账户行。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {led.t('paper_account')}"
                f"{led.where('' if led.sim else 'id=1')}",
                led.args(),
            ).fetchone()
        return dict(row) if row else None

    def init_paper_account(self, initial_capital: float) -> dict[str, Any]:
        """初始化模拟账户（幂等：已存在则直接返回，绝不覆盖已有现金余额）。

        关键修复：早期实现用裸 INSERT，若在「账户已存在但本调用瞬间没查到」的竞态
        （如 09:30 服务重启、`ensure_account` 与写入并发）下执行，会把现金重置成
        初始资金、丢失历史成交的现金效应，导致总资产虚增。改为 ``ON CONFLICT DO NOTHING``
        后，任何重复初始化都不会改动已有的 ``cash`` / ``initial_capital``。
        """
        existing = self.get_paper_account()
        if existing:
            return existing
        led = _scope()
        if led.sim:
            # 影子账本由 ensure_sim_accounts() 预建；走到这里说明账本行已被删
            raise RuntimeError(f"影子账本 {led.account_id} 不存在，请重启服务重建")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_account(id, initial_capital, cash, created_at)"
                " VALUES(1,?,?,?)"
                " ON CONFLICT(id) DO NOTHING",
                (initial_capital, initial_capital, _now()),
            )
        return self.get_paper_account() or {
            "initial_capital": initial_capital, "cash": initial_capital
        }

    def update_paper_cash(self, cash: float) -> None:
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE {led.t('paper_account')} SET cash=?"
                f"{led.where('' if led.sim else 'id=1')}",
                (cash, *led.args()),
            )

    def audit_paper_cash(self, repair: bool = True) -> dict[str, Any]:
        """以成交台账为唯一真相源核对（并可原子修正）模拟账户现金。

        背景：2026-11-12 两次出现 ``paper_account.cash`` 被写成初始资金 10 万，
        持仓市值被当成白赚，页面显示 **+37.67%** 的假收益，而同一时刻按台账
        重算与净值快照都是 **−1.27%**。根因不是策略，而是账户查询完全信任
        ``cash`` 字段、没有任何一致性校验；上次只修数据没修信任链，故复发。

        这里把「读账户 → 按台账重放 → 必要时写回」放进**同一把锁 + 同一连接**，
        避免与撮合的 ``update_paper_cash`` 交错。两条护栏：

        * 台账为空时**绝不**把现金重放成初始资金（防止修复逻辑自己制造污染：
          ``cash = initial`` 在无成交时会把 10 万写回去，正是最可疑的写入路径）；
        * 只在偏差 > 0.01 元时才写，正常撮合不产生额外写入。

        账本作用域（影子账户）同样适用：所有表名 / WHERE 都经 ``led`` 折算。
        """
        led = _scope()
        where = led.where("" if led.sim else "id=1")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {led.t('paper_account')}{where}", led.args(),
            ).fetchone()
            if row is None:
                return {"ok": False, "reason": "account_missing", "cash": 0.0,
                        "repaired": False, "trades": 0}
            stored = float(row["cash"])
            initial = float(row["initial_capital"])
            trades = conn.execute(
                f"SELECT side, amount, fee FROM {led.t('paper_trades')}{led.where()}"
                " ORDER BY id",
                led.args(),
            ).fetchall()
            if not trades:
                # 新账户或台账读取异常：保持原值，绝不擅自重置成 initial
                return {"ok": True, "cash": stored, "stored": stored,
                        "replayed": None, "repaired": False, "trades": 0,
                        "note": "no_trades_skip"}
            cash = initial
            for t in trades:
                amt = float(t["amount"]); fee = float(t["fee"])
                cash += -(amt + fee) if t["side"] == "buy" else (amt - fee)
            replayed = round(cash, 2)
            repaired = False
            if repair and abs(replayed - stored) > 0.01:
                conn.execute(
                    f"UPDATE {led.t('paper_account')} SET cash=?{where}",
                    (replayed, *led.args()),
                )
                repaired = True
            return {
                "ok": True,
                "cash": replayed if repaired else stored,
                "stored": stored,
                "replayed": replayed,
                "repaired": repaired,
                "trades": len(trades),
            }

    def list_paper_positions(self) -> list[dict[str, Any]]:
        led = _scope()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {led.t('paper_positions')}{led.where()}"
                " ORDER BY created_at",
                led.args(),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_paper_pending_snapshot(self, items: list[dict]) -> None:
        """覆盖式保存 pending/backlog 快照（items 含 kind 字段），供重启恢复。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(f"DELETE FROM {led.t('paper_pending')}")
            for it in items:
                conn.execute(
                    f"INSERT OR REPLACE INTO {led.t('paper_pending')}"
                    "(code,kind,name,action,reason,created_at,expire_at,price_at_signal,score,scan)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        it["code"], it["kind"], it.get("name"), it.get("action"),
                        it.get("reason"), it.get("created_at"), it.get("expire_at"),
                        it.get("price_at_signal"), it.get("score"), it.get("scan"),
                    ),
                )

    def list_paper_pending(self) -> list[dict[str, Any]]:
        """读取持久化的撮合队列（重启恢复用）。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {led.t('paper_pending')}").fetchall()
        return [dict(r) for r in rows]

    def get_paper_position(self, code: str) -> dict[str, Any] | None:
        led = _scope()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {led.t('paper_positions')}{led.where('code=?')}",
                led.args(code),
            ).fetchone()
        return dict(row) if row else None

    def upsert_paper_position(
        self, code: str, name: str, shares: int, cost_price: float,
        buy_date: str, strategy: str, score: float = 0.0,
        stop_price: float | None = None, peak_price: float | None = None,
    ) -> None:
        """新增/覆盖持仓（同代码重复买入时摊薄成本由调用方算好传入）。"""
        led = _scope()
        cols = (
            "code, name, shares, cost_price, buy_date, strategy, score,"
            " stop_price, peak_price, created_at"
        )
        params = (
            code, name, shares, cost_price, buy_date, strategy, score,
            stop_price, peak_price, _now(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {led.t('paper_positions')}({led.cols(cols)})"
                f" VALUES({led.vals(10)})"
                f" ON CONFLICT({led.conflict('code')}) DO UPDATE SET name=excluded.name,"
                " shares=excluded.shares, cost_price=excluded.cost_price,"
                " buy_date=excluded.buy_date, strategy=excluded.strategy,"
                " score=excluded.score, stop_price=excluded.stop_price,"
                " peak_price=excluded.peak_price",
                led.args(*params),
            )

    def update_paper_peak(self, code: str, peak_price: float) -> None:
        """更新持仓期最高价（移动/保本止损跟踪用）。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE {led.t('paper_positions')} SET peak_price=?"
                f"{led.where('code=?')}",
                (peak_price, *led.args(code)),
            )

    def count_paper_trades(
        self, reason_prefix: str, trade_date: str, side: str = "sell"
    ) -> int:
        """统计某日某类交易笔数（轮动替换的每日次数限制用）。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {led.t('paper_trades')}"
                f"{led.where('trade_date=? AND side=? AND reason LIKE ?')}",
                led.args(trade_date, side, f"{reason_prefix}%"),
            ).fetchone()
        return int(row["n"] if row else 0)

    def remove_paper_position(self, code: str) -> None:
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"DELETE FROM {led.t('paper_positions')}{led.where('code=?')}",
                led.args(code),
            )

    # ---------------- 持仓批次（做T / 严格 T+1：每批带 buy_date 算可卖） ----------------
    def _aggregate_paper_summary(self, conn, code: str) -> bool:
        """从 lots 聚合汇总行的 shares/cost_price/buy_date；无批次则删汇总行。返回是否仍有持仓。"""
        led = _scope()
        lots = conn.execute(
            f"SELECT shares, cost_price, buy_date FROM {led.t('paper_position_lots')}"
            f"{led.where('code=?')}",
            led.args(code),
        ).fetchall()
        if not lots:
            conn.execute(
                f"DELETE FROM {led.t('paper_positions')}{led.where('code=?')}",
                led.args(code),
            )
            return False
        total_shares = sum(int(r["shares"]) for r in lots)
        total_cost = sum(int(r["shares"]) * float(r["cost_price"]) for r in lots)
        agg_cost = (total_cost / total_shares) if total_shares else 0.0
        max_buy = max(str(r["buy_date"]) for r in lots)
        conn.execute(
            f"UPDATE {led.t('paper_positions')} SET shares=?, cost_price=?, buy_date=?"
            f"{led.where('code=?')}",
            (total_shares, round(agg_cost, 4), max_buy, *led.args(code)),
        )
        return True

    def _refresh_paper_sellable(self, conn, code: str, today: str) -> int:
        led = _scope()
        row = conn.execute(
            f"SELECT COALESCE(SUM(shares),0) AS s FROM {led.t('paper_position_lots')}"
            f"{led.where('code=? AND buy_date < ?')}",
            led.args(code, today),
        ).fetchone()
        sellable = int(row["s"]) if row else 0
        conn.execute(
            f"UPDATE {led.t('paper_positions')} SET sellable_shares=?"
            f"{led.where('code=?')}",
            (sellable, *led.args(code)),
        )
        return sellable

    def add_paper_lot(
        self, code: str, name: str, shares: int, cost_price: float,
        buy_date: str, strategy: str, score: float = 0.0,
        stop_price: float | None = None, peak_price: float | None = None,
        today: str | None = None, tag: str = "base",
        provenance: str | None = None,
    ) -> None:
        """新增一个买入批次（开仓 / 做T加仓 / 做T买回都走这里；buy_date=当日→次日可卖）。

        ``provenance``：建仓决策溯源 JSON（触发信号 + regime 快照 + 决策理由），
        由 engine 在买入时构造并传入；历史/迁移补建的批次为 None。
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {led.t('paper_position_lots')}"
                f"({led.cols('code, shares, cost_price, buy_date, tag, provenance, created_at')})"
                f" VALUES({led.vals(7)})",
                led.args(code, shares, cost_price, buy_date, tag, provenance, _now()),
            )
            exists = conn.execute(
                f"SELECT 1 FROM {led.t('paper_positions')}{led.where('code=?')}",
                led.args(code),
            ).fetchone()
            if exists:
                self._aggregate_paper_summary(conn, code)
            else:
                cols = (
                    "code,name,shares,cost_price,buy_date,strategy,score,"
                    "stop_price,peak_price,sellable_shares,created_at"
                )
                conn.execute(
                    f"INSERT INTO {led.t('paper_positions')}({led.cols(cols)})"
                    f" VALUES({led.vals(11)})",
                    led.args(code, name, shares, round(cost_price, 4), buy_date,
                             strategy, score, stop_price, peak_price, 0, _now()),
                )
            # **加仓不得覆盖底仓的建仓属性**（2026-11-02）：
            # 做T加仓/买回走同一个 add_paper_lot，但它们的 score 默认 0、stop_price/peak_price 为 None。
            # 无脑覆盖会把「建仓评分 100 的票」写成 0 分（轮动时被当成最弱换掉）、清空建仓时定死的
            # 止损线、并把移动止损的高水位重置 —— 三处都静默失效。
            # 规则：传了有效值才覆盖（score>0 / strategy 非空 / 价格非 None），否则保留原值。
            conn.execute(
                f"UPDATE {led.t('paper_positions')} SET name=?, "
                f"score=CASE WHEN ? > 0 THEN ? ELSE score END, "
                f"strategy=CASE WHEN ? <> '' THEN ? ELSE strategy END, "
                f"stop_price=COALESCE(?, stop_price), "
                f"peak_price=COALESCE(?, peak_price)"
                f"{led.where('code=?')}",
                (name, score, score, strategy, strategy, stop_price, peak_price,
                 *led.args(code)),
            )
            self._refresh_paper_sellable(conn, code, today)

    def reduce_paper_lots_by_tag(
        self, code: str, tag: str, want_shares: int, today: str
    ) -> int:
        """从指定 tag 的可卖批次（buy_date<today，FIFO）减仓，返回实际卖出股数。

        用于做T反弹减仓：只卖「做T加仓」批次（t_add），不误伤底仓。
        """
        if want_shares <= 0:
            return 0
        led = _scope()
        with self._lock, self._connect() as conn:
            lots = conn.execute(
                f"SELECT id, shares FROM {led.t('paper_position_lots')}"
                f"{led.where('code=? AND tag=? AND buy_date < ?')} ORDER BY buy_date, id",
                led.args(code, tag, today),
            ).fetchall()
            remaining = int(want_shares)
            sold = 0
            for lot in lots:
                if remaining <= 0:
                    break
                take = min(int(lot["shares"]), remaining)
                new_shares = int(lot["shares"]) - take
                if new_shares <= 0:
                    conn.execute(
                        f"DELETE FROM {led.t('paper_position_lots')} WHERE id=?",
                        (lot["id"],))
                else:
                    conn.execute(
                        f"UPDATE {led.t('paper_position_lots')} SET shares=? WHERE id=?",
                        (new_shares, lot["id"]),
                    )
                remaining -= take
                sold += take
            self._aggregate_paper_summary(conn, code)
            self._refresh_paper_sellable(conn, code, today)
            return sold

    def reduce_paper_sellable(self, code: str, want_shares: int, today: str) -> int:
        """从可卖批次（buy_date<today，FIFO 最老先卖）减仓，返回实际卖出股数；刷新汇总与可卖。"""
        if want_shares <= 0:
            return 0
        led = _scope()
        with self._lock, self._connect() as conn:
            lots = conn.execute(
                f"SELECT id, shares FROM {led.t('paper_position_lots')}"
                f"{led.where('code=? AND buy_date < ?')} ORDER BY buy_date, id",
                led.args(code, today),
            ).fetchall()
            remaining = int(want_shares)
            sold = 0
            for lot in lots:
                if remaining <= 0:
                    break
                take = min(int(lot["shares"]), remaining)
                new_shares = int(lot["shares"]) - take
                if new_shares <= 0:
                    conn.execute(
                        f"DELETE FROM {led.t('paper_position_lots')} WHERE id=?",
                        (lot["id"],))
                else:
                    conn.execute(
                        f"UPDATE {led.t('paper_position_lots')} SET shares=? WHERE id=?",
                        (new_shares, lot["id"]),
                    )
                remaining -= take
                sold += take
            self._aggregate_paper_summary(conn, code)
            self._refresh_paper_sellable(conn, code, today)
            return sold

    def list_paper_lots(self, code: str | None = None) -> list[dict[str, Any]]:
        led = _scope()
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    f"SELECT * FROM {led.t('paper_position_lots')}"
                    f"{led.where('code=?')} ORDER BY id",
                    led.args(code),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {led.t('paper_position_lots')}{led.where()}"
                    " ORDER BY code, id",
                    led.args(),
                ).fetchall()
        return [dict(r) for r in rows]

    def ensure_lots_migrated(self, today: str | None = None) -> None:
        """批次模型兼容：把尚无批次的现有持仓补建为一个批次（买入日沿用）。幂等。

        在 engine 切换到批次卖出逻辑后、首次启动时调用一次，避免历史持仓因无 lot 而无法卖出。
        """
        today = today or datetime.now().strftime("%Y-%m-%d")
        led = _scope()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT code, shares, cost_price, buy_date FROM "
                f"{led.t('paper_positions')}{led.where()}",
                led.args(),
            ).fetchall()
            for r in rows:
                code = r["code"]
                has = conn.execute(
                    f"SELECT 1 FROM {led.t('paper_position_lots')}{led.where('code=?')}",
                    led.args(code),
                ).fetchone()
                if has:
                    self._refresh_paper_sellable(conn, code, today)
                    continue
                conn.execute(
                    f"INSERT INTO {led.t('paper_position_lots')}"
                    f"({led.cols('code, shares, cost_price, buy_date, created_at')})"
                    f" VALUES({led.vals(5)})",
                    led.args(code, int(r["shares"]), float(r["cost_price"]),
                             str(r["buy_date"]), _now()),
                )
                self._refresh_paper_sellable(conn, code, today)

    def update_paper_t_state(
        self, code: str,
        t_t_sold: int | None = None,
        t_down_armed: int | None = None,
        t_sold_date: str | None = None,
    ) -> None:
        """更新做T状态字段（倒T/涨停卖半待买回股数、卖出日、下跌加仓触发标记）。

        ``t_sold_date`` 传空串表示清空（买回完成后不再挂「待买回」状态）。
        """
        sets: list[str] = []
        vals: list[Any] = []
        if t_t_sold is not None:
            sets.append("t_t_sold=?")
            vals.append(int(t_t_sold))
        if t_down_armed is not None:
            sets.append("t_down_armed=?")
            vals.append(int(t_down_armed))
        if t_sold_date is not None:
            sets.append("t_sold_date=?")
            vals.append(t_sold_date or None)
        if not sets:
            return
        led = _scope()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE {led.t('paper_positions')} SET " + ",".join(sets)
                + led.where("code=?"),
                (*vals, *led.args(code)),
            )

    def insert_paper_trade(
        self, code: str, name: str, side: str, price: float, shares: int,
        amount: float, fee: float, reason: str, trade_date: str,
        exit_reason: str | None = None,
    ) -> None:
        led = _scope()
        cols = (
            "code, name, side, price, shares, amount, fee, reason,"
            " trade_date, exit_reason, created_at"
        )
        with self._lock, self._connect() as conn:
            if exit_reason is None and side == "sell":
                exit_reason = _infer_exit_reason(reason)
            conn.execute(
                f"INSERT INTO {led.t('paper_trades')}({led.cols(cols)})"
                f" VALUES({led.vals(11)})",
                led.args(code, name, side, price, shares, amount, fee, reason,
                         trade_date, exit_reason, _now()),
            )
        # 六阶段生命周期：所有卖出即「离场」（#191），由唯一写入点统一发射，
        # 覆盖止损/止盈/时间止损/信号/轮动/收盘减仓/清非交易板块/手动/做T 全部路径。
        # **必须在上面的 with 块之外发射**：insert_lifecycle_event 自己会
        # ``with self._lock``，而 Database._lock 是不可重入的 threading.Lock；
        # 放在锁内会在第一笔卖出成交时永久阻塞，导致整个服务自此无法再卖出。
        # 2026-11-12「卖出出口巡检」在隔离副本上复现并修复。
        if side == "sell":
            self.insert_lifecycle_event(
                code, name, 6, "离场",
                exit_reason or _infer_exit_reason(reason) or "OTHER",
                detail=reason,
            )

    # ---- 六阶段生命周期事件（#191）：全局表，不属于 paper/sim 账本，直接写 lifecycle_events ----
    def insert_lifecycle_event(
        self, code: str, name: str, stage: int, stage_name: str,
        event: str, detail: str | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO lifecycle_events(code, name, stage, stage_name, event, detail, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (str(code or ""), name, int(stage), stage_name, event, detail, _now()),
            )

    def list_lifecycle_events(
        self, code: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    "SELECT * FROM lifecycle_events WHERE code=? ORDER BY id DESC LIMIT ?",
                    (str(code), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM lifecycle_events ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_lifecycle_events_by_stage(self) -> list[dict[str, Any]]:
        """按阶段聚合事件数，供前端六阶段概览卡使用。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT stage, stage_name, COUNT(*) AS cnt FROM lifecycle_events"
                " GROUP BY stage, stage_name ORDER BY stage"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_paper_trades(
        self, limit: int = 100, code: str | None = None
    ) -> list[dict[str, Any]]:
        """交易流水（按时间倒序）。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            if code:
                rows = conn.execute(
                    f"SELECT * FROM {led.t('paper_trades')}{led.where('code=?')}"
                    " ORDER BY id DESC LIMIT ?",
                    led.args(code, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {led.t('paper_trades')}{led.where()}"
                    " ORDER BY id DESC LIMIT ?",
                    led.args(limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def save_paper_equity(
        self, date: str, total_asset: float, cash: float, market_value: float
    ) -> None:
        """保存每日净值快照（同日覆盖）。"""
        led = _scope()
        cols = "date, total_asset, cash, market_value, created_at"
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {led.t('paper_equity')}({led.cols(cols)})"
                f" VALUES({led.vals(5)})"
                f" ON CONFLICT({led.conflict('date')}) DO UPDATE SET"
                " total_asset=excluded.total_asset, cash=excluded.cash,"
                " market_value=excluded.market_value",
                led.args(date, total_asset, cash, market_value, _now()),
            )

    def list_paper_equity(self, limit: int = 250) -> list[dict[str, Any]]:
        """净值曲线（按日期升序，取**最近** limit 个交易日）。

        必须取「最近」而不是「最早」：`ORDER BY date LIMIT n` 在快照超过 n 行后
        会把最新的点全部截掉，曲线停在最早那一段（2026-11-05 发现——当天行明明
        已经随 tick 刷新，接口却查不到，因为 limit 把老数据截在了前面）。
        """
        led = _scope()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {led.t('paper_equity')}{led.where()}"
                " ORDER BY date DESC LIMIT ?",
                led.args(limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def reset_paper_account(self, initial_capital: float) -> None:
        """重置模拟账户：清空持仓/批次/流水/净值，资金恢复初始值。"""
        led = _scope()
        with self._lock, self._connect() as conn:
            for base in (
                "paper_positions", "paper_position_lots", "paper_trades", "paper_equity",
            ):
                conn.execute(f"DELETE FROM {led.t(base)}{led.where()}", led.args())
            if led.sim:
                conn.execute(
                    f"UPDATE {led.t('paper_account')} SET initial_capital=?, cash=?"
                    f"{led.where()}",
                    (initial_capital, initial_capital, *led.args()),
                )
                return
            conn.execute(
                "INSERT INTO paper_account(id, initial_capital, cash, created_at)"
                " VALUES(1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET initial_capital=excluded.initial_capital,"
                " cash=excluded.cash",
                (initial_capital, initial_capital, _now()),
            )

    # ---------------- 并行模拟账本（sim_*，与 paper_* 隔离） ----------------

    def list_sim_accounts(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """并行模拟账本清单（含现金/初始资金；enabled_only 只返回启用的）。"""
        sql = "SELECT * FROM sim_account"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY account_id"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_sim_account(self, tag: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sim_account WHERE tag=?", (tag,)
            ).fetchone()
        return dict(row) if row else None

    def ensure_sim_accounts(self, specs: list[dict]) -> list[dict[str, Any]]:
        """按配置同步账本：新建缺失的、刷新 profile/overrides、停用不在清单里的。

        账本一旦建立就保留自己的现金与持仓（只在首次创建时写 initial_capital），
        改配置不会把已有净值曲线推倒重来；从清单里移除只是置 ``enabled=0``，
        数据留着便于事后回看，重新加回来即可继续。
        """
        with self._lock, self._connect() as conn:
            existing = {
                r["tag"]: dict(r)
                for r in conn.execute("SELECT * FROM sim_account").fetchall()
            }
            keep = {s["tag"] for s in specs}
            for s in specs:
                overrides = json.dumps(s.get("overrides") or {}, ensure_ascii=False)
                note = str(s.get("note") or "")
                if s["tag"] in existing:
                    conn.execute(
                        "UPDATE sim_account SET profile=?, overrides=?, enabled=1, note=?"
                        " WHERE tag=?",
                        (s["profile"], overrides, note, s["tag"]),
                    )
                else:
                    cap = float(s.get("initial_capital") or 0)
                    conn.execute(
                        "INSERT INTO sim_account(tag, profile, overrides, initial_capital,"
                        " cash, enabled, note, created_at) VALUES(?,?,?,?,?,1,?,?)",
                        (s["tag"], s["profile"], overrides, cap, cap, note, _now()),
                    )
            for tag in existing:
                if tag not in keep:
                    conn.execute("UPDATE sim_account SET enabled=0 WHERE tag=?", (tag,))
        return self.list_sim_accounts()

    def reset_sim_account(self, tag: str, initial_capital: float) -> None:
        """按 tag 重置某个并行账本（只清它自己的持仓/流水/净值）。"""
        acct = self.get_sim_account(tag)
        if not acct:
            raise KeyError(f"未知账本: {tag}")
        with ledger_scope(int(acct["account_id"])):
            self.reset_paper_account(initial_capital)

    # ---------------- 智能建议 ----------------

    def save_recommendations(
        self, recommendations: list[dict], batch: str
    ) -> int:
        """保存一批建议（先清除同批次旧记录，实现同批次只保留最新）。

        Args:
            recommendations: 建议 dict 列表（code/name/action/score/reason/
                price/stop_loss/target_price/sentiment）
            batch: 批次标识，如 intraday_2026-10-22 / focus_2026-10-21

        Returns:
            保存条数
        """
        if not recommendations:
            return 0
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM recommendations WHERE batch=?", (batch,))
            conn.executemany(
                "INSERT INTO recommendations(code, name, action, score, reason,"
                " price, stop_loss, target_price, sentiment, rank, detail,"
                " batch, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["code"], r.get("name", ""), r["action"], r.get("score"),
                        r.get("reason", ""), r.get("price"), r.get("stop_loss"),
                        r.get("target_price"), r.get("sentiment", ""),
                        int(r.get("rank") or 0), r.get("detail") or "[]",
                        batch, _now(),
                    )
                    for r in recommendations
                ],
            )
        return len(recommendations)

    def list_recommendations(
        self, batch_prefix: str, action: str | None = None,
        *, dedupe_by_code: bool = False,
    ) -> list[dict[str, Any]]:
        """查询某类批次的最新一批建议（按评分排序）。

        Args:
            batch_prefix: 批次前缀，如 intraday / focus / premarket
            action: 可选过滤 buy / sell / hold / focus
            dedupe_by_code: 同一 code 只保留排序后第一条。用于兼容历史
                ``focus_`` 批次——生成逻辑修好前，同股多策略会存成多条记录
                （见 #119），列表与角标计数都需按股票去重。
        """
        with self._lock, self._connect() as conn:
            # 取该前缀下最新的批次（批次名含日期，字符串最大即最新）
            row = conn.execute(
                "SELECT MAX(batch) AS latest FROM recommendations WHERE batch LIKE ?",
                (f"{batch_prefix}%",),
            ).fetchone()
            if not row or not row["latest"]:
                return []
            # 优先按第 2 次排序的名次（rank>0），未排名的（如明日关注）退回评分排序
            order = " ORDER BY CASE WHEN rank > 0 THEN rank ELSE 9999 END, score DESC"
            if action:
                rows = conn.execute(
                    "SELECT * FROM recommendations WHERE batch=? AND action=?" + order,
                    (row["latest"], action),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM recommendations WHERE batch=?" + order,
                    (row["latest"],),
                ).fetchall()
        items = [dict(r) for r in rows]
        if dedupe_by_code:
            seen: set[str] = set()
            unique: list[dict[str, Any]] = []
            for item in items:
                code = str(item.get("code") or "")
                if code in seen:
                    continue
                seen.add(code)
                unique.append(item)
            return unique
        return items

    def latest_batch(self, batch_prefix: str) -> str | None:
        """某类批次的最新批次名（含日期）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(batch) AS latest FROM recommendations WHERE batch LIKE ?",
                (f"{batch_prefix}%",),
            ).fetchone()
        return row["latest"] if row and row["latest"] else None

    # ---------------- 自定义策略 ----------------

    def save_custom_strategy(
        self, name: str, base_strategy: str, params: dict,
        description: str = "", category: str = "自定义",
    ) -> None:
        """保存自定义策略（按 name 主键幂等，更新即覆盖）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO custom_strategies(name, base_strategy, description,"
                " category, params, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET base_strategy=excluded.base_strategy,"
                " description=excluded.description, category=excluded.category,"
                " params=excluded.params",
                (name, base_strategy, description, category,
                 json.dumps(params, ensure_ascii=False), _now()),
            )

    def list_custom_strategies(self) -> list[dict[str, Any]]:
        """全部自定义策略（params 已解析为 dict）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT name, base_strategy, description, category, params, created_at"
                " FROM custom_strategies ORDER BY created_at"
            ).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["params"] = json.loads(item["params"] or "{}")
            result.append(item)
        return result

    def delete_custom_strategy(self, name: str) -> bool:
        """删除自定义策略，返回是否删除成功。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM custom_strategies WHERE name=?", (name,)
            )
        return cur.rowcount > 0

    # ---------------- 回测记录 ----------------

    #: 逐笔成交表的列（写入与读取共用一份，避免两处各写一遍字段名）
    _BT_TRADE_COLS: tuple[str, ...] = (
        "code", "name", "buy_date", "sell_date", "buy_price", "sell_price",
        "shares", "amount", "fee", "pnl", "pnl_pct", "reason", "score",
    )

    def save_backtest(self, run: dict[str, Any]) -> int:
        """保存一次回测结果（参数 + 过程数据 + 结果），返回自增 id。

        落库范围：
        - ``backtest_runs``：参数全量、metrics、逐日权益曲线（过程数据）、
          期末持仓、亏损归因与建议、任务 id 与耗时；
        - ``backtest_trades``：逐笔成交明细，单独成表以便直接做
          「按卖出原因 / 按股票 / 按建仓评分」的 SQL 聚合。

        两者在**同一事务**内写入（同一次 ``with conn``），不会出现
        「有 run 没明细」或「有明细没 run」的半截记录。
        """
        trades = list(run.get("trades") or [])
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO backtest_runs(strategy, start_date, end_date, params,"
                " metrics, equity_curve, positions, review, job_id, elapsed, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run["strategy"], run.get("start_date"), run.get("end_date"),
                    json.dumps(run.get("params", {}), ensure_ascii=False),
                    json.dumps(run.get("metrics", {}), ensure_ascii=False),
                    json.dumps(run.get("equity_curve", []), ensure_ascii=False),
                    json.dumps(run.get("positions", {}), ensure_ascii=False),
                    json.dumps(run.get("review", {}), ensure_ascii=False),
                    run.get("job_id"),
                    run.get("elapsed"),
                    _now(),
                ),
            )
            run_id = int(cur.lastrowid or 0)
            if trades:
                conn.executemany(
                    "INSERT INTO backtest_trades(run_id, code, name, buy_date,"
                    " sell_date, buy_price, sell_price, shares, amount, fee, pnl,"
                    " pnl_pct, reason, score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (run_id, *[t.get(col) for col in self._BT_TRADE_COLS])
                        for t in trades
                    ],
                )
            return run_id

    def get_backtest(self, run_id: int) -> dict[str, Any] | None:
        """取单条回测记录（含逐笔成交、期末持仓与归因）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backtest_runs WHERE id=?", (run_id,)
            ).fetchone()
            trade_rows = conn.execute(
                "SELECT * FROM backtest_trades WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        if row is None:
            return None
        result = dict(row)
        result["params"] = json.loads(result.get("params") or "{}")
        result["metrics"] = json.loads(result.get("metrics") or "{}")
        result["equity_curve"] = json.loads(result.get("equity_curve") or "[]")
        result["positions"] = json.loads(result.get("positions") or "{}")
        result["review"] = json.loads(result.get("review") or "{}")
        # 明细来自 backtest_trades；本功能上线前的历史记录没有明细 → 空列表
        result["trades"] = [
            {col: r[col] for col in self._BT_TRADE_COLS} for r in trade_rows
        ]
        return result

    def list_backtests(self, limit: int = 50) -> list[dict[str, Any]]:
        """历史回测列表（摘要）。

        故意**不**取 ``equity_curve`` / ``positions`` / ``review``：50 条 × 上万字符
        会让列表接口凭空多出几百 KB，而列表只需要「什么时候跑的、什么参数、
        结果如何」。逐日曲线与逐笔明细走 :meth:`get_backtest` 单条查。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, strategy, start_date, end_date, params, metrics,"
                " job_id, elapsed, created_at FROM backtest_runs"
                " ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            counts = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT run_id, COUNT(*) FROM backtest_trades GROUP BY run_id"
                )
            }
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.get("params") or "{}")
            item["metrics"] = json.loads(item.get("metrics") or "{}")
            item["trade_count"] = counts.get(item["id"], 0)
            out.append(item)
        return out


# 全局单例
kline_cache = KlineCache()
db = Database()
