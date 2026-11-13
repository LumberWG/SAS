"""P1 上下文快照采集器：把四项风险闸需要的数据落成**本地按日期快照**。

为什么必须有这一层
------------------
策略代码（``app.risk.context_store``）只读本地库、按 ``as_of`` 截断，绝不直接调实时
接口。若实盘读实时新闻而回测没有历史新闻，就违反「回测与实盘不能有丝毫不一样」。
因此所有外部信息先经本脚本落库，历史段也要能回算，两侧再共读同一张表。

用法（backend 目录下，用项目 venv）::

    # 1) 必做：从本地日线预计算全市场/行业过热分位（零未来函数，纯本地、可重复跑）
    .venv/Scripts/python.exe scripts/fetch_context_snapshots.py --build-heat \
        --from 2022-01-01 --to 2026-11-12

    # 2) 尽力而为：akshare 抓财报/业绩预告公告日期、市场快讯与个股新闻
    .venv/Scripts/python.exe scripts/fetch_context_snapshots.py --akshare \
        --from 2022-01-01 --to 2026-11-12

    # 3) 外部数据源自建：导入 CSV（列见 IMPORT_SPEC）
    .venv/Scripts/python.exe scripts/fetch_context_snapshots.py --import-events events.csv
    .venv/Scripts/python.exe scripts/fetch_context_snapshots.py --import-news news.csv

    # 4) 随时查看覆盖情况（策略读到的是这些快照，不是实时数据）
    .venv/Scripts/python.exe scripts/fetch_context_snapshots.py --check

IMPORT_SPEC
----------
events.csv: code, event_date, kind, severity, title, source
news.csv   : code, news_date, sentiment, heat, title, source
``sentiment`` ∈ [-1, 1]（负=利空）；``heat`` ≥ 0 为权重。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.data.cache import db  # noqa: E402
from app.risk.context_store import ContextStore  # noqa: E402

warnings.filterwarnings("ignore", category=FutureWarning)

#: 规则法舆情词典（不是模型，也不改打分；只用于「明显利空禁买」的风险闸）
NEGATIVE_WORDS = (
    "立案", "调查", "处罚", "罚款", "退市", "亏损", "预亏", "爆雷", "违规",
    "减持", "质押", "冻结", "诉讼", "造假", "重整", "破产", "问询函", "警示函",
    "业绩下滑", "商誉减值", "跌停", "暂停上市", "终止上市", "被查",
)
POSITIVE_WORDS = (
    "预增", "中标", "增持", "回购", "涨停", "扭亏", "业绩增长", "获批",
    "战略合作", "分红", "超预期", "新高",
)


def _score_text(text: str) -> float:
    """[-1, 1] 规则情感分：命中词数归一，无命中返回 0（中性=不触发闸门）。"""
    t = str(text or "")
    neg = sum(1 for w in NEGATIVE_WORDS if w in t)
    pos = sum(1 for w in POSITIVE_WORDS if w in t)
    if neg == 0 and pos == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / 3.0))


# ---------------------------------------------------------------- 热度历史
def build_heat(start: str, end: str, pct: float = 0.20, batch: int = 400) -> int:
    """按交易日预计算全市场 ret20 分位与行业热度分位，写入 context_heat。

    严格只用到 `close[t]` 与 `close[t-20]`（都在 as_of 当日或之前），零未来函数。
    行业映射是当前静态快照（两侧共用同一份，不构成漂移；有分类前视偏差，只用于分组）。
    """
    s = get_settings()
    store = ContextStore()
    kline_dir = Path(s.kline_dir)
    files = sorted(p for p in kline_dir.glob("*.parquet") if p.stem.isdigit())
    print(f"[heat] 读取 {len(files)} 份日线，区间 {start} ~ {end}", flush=True)

    series_map: dict[str, pd.Series] = {}
    for i, p in enumerate(files):
        try:
            df = pd.read_parquet(p, columns=["date", "close"])
        except Exception:  # noqa: BLE001 - 单份坏文件不应中断整批
            continue
        if df is None or df.empty or "close" not in df:
            continue
        df = df[df["date"] <= end].sort_values("date")
        if len(df) < 21:
            continue
        r = pd.to_numeric(df["close"], errors="coerce")
        # shift(20) 只用过去 20 根，不触达未来
        ret20 = (r / r.shift(20) - 1.0).replace([np.inf, -np.inf], np.nan)
        sub = ret20[(df["date"] >= start).values]
        if sub.notna().any():
            series_map[p.stem] = pd.Series(
                sub.to_numpy(dtype=float), index=df["date"][(df["date"] >= start).values].to_numpy()
            )
        if (i + 1) % 2000 == 0:
            print(f"[heat] 已加载 {i + 1}/{len(files)}", flush=True)
    if not series_map:
        print("[heat] 没有可用日线，未写入任何阈值", flush=True)
        return 0

    mat = pd.DataFrame(series_map)
    codes = list(mat.columns)
    ind = pd.Series({c: store.industry_of(c) for c in codes})
    cutoff = 1.0 - float(pct)
    rows: list[tuple] = []
    for d in sorted(mat.index):
        row = pd.Series(mat.loc[d].to_numpy(dtype=float), index=codes).dropna()
        if len(row) < 30:
            continue
        stock_cut = float(np.quantile(row.to_numpy(), cutoff))
        grouped = row.groupby(ind.reindex(row.index)).median().dropna()
        if len(grouped) >= 5:
            industry_cut = float(np.quantile(grouped.to_numpy(), cutoff))
            hot = sorted(str(k) for k, v in grouped.items() if v > industry_cut)
        else:
            industry_cut, hot = None, []
        rows.append((str(d), stock_cut, industry_cut, json.dumps(hot, ensure_ascii=False), int(len(row))))
        if len(rows) >= batch:
            db.upsert_context_heat(rows)
            rows = []
    if rows:
        db.upsert_context_heat(rows)
    total = len(list(mat.index))
    print(f"[heat] 完成：{total} 个交易日阈值已写入 context_heat（pct={pct:.0%}）", flush=True)
    return total


# ---------------------------------------------------------------- akshare
def _akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        print("[akshare] 未安装，跳过；可用 --import-events / --import-news 导入本地 CSV")
        return None


def _pick(row: pd.Series, *keys: str, default=None):
    for k in keys:
        if k in row.index and pd.notna(row[k]):
            return row[k]
    return default


def _quarter_ends(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    out = []
    y = d0.year
    while y <= d1.year:
        for md in ("0331", "0630", "0930", "1231"):
            s = f"{y}{md}"
            if start.replace("-", "") <= s <= end.replace("-", ""):
                out.append(s)
        y += 1
    return out


#: 全市场公告 → 事件类型（按公告类型/标题关键词）
_NOTICE_KIND_RULES = (
    ("减持", "reduce"), ("解禁", "unlock"), ("质押", "reduce"), ("冻结", "reduce"),
    ("分红", "dividend"), ("除权", "dividend"), ("除息", "dividend"), ("送转", "dividend"),
    ("退市", "st_change"), ("风险警示", "st_change"),
    # 只认「业绩预告/预增/预亏」这类明确的前瞻披露；「年报」「季报」「说明会」
    # 等普通公告不归 earnings，否则冷却闸会被无关公告淹没。
    ("业绩预告", "earnings"), ("业绩预增", "earnings"), ("业绩预亏", "earnings"),
)


def _notice_kind(title: str, atype: str) -> str:
    text = f"{title} {atype}"
    for kw, kind in _NOTICE_KIND_RULES:
        if kw in text:
            return kind
    return "other"


def _quiet_call(fn, **kw):
    """akshare 部分接口内部用 tqdm 打进度条，采集时静音 stderr。"""
    import contextlib
    import io
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(**kw)


def collect_akshare(
    start: str, end: str, notice_days: int = 45, notices_only: bool = False
) -> None:
    """尽力而为：业绩预告（季度历史） + 全市场每日公告（事件与舆情同源、可按日回放）。

    刻意**不用** ``stock_yjbb_em``：它的「最新公告日期」是股票最近一次公告，
    不是财报披露日，按季度给全市场 5900 只票各记一条事件会让冷却闸几乎锁死买入。
    ``stock_news_em`` 在当前 pyarrow 下有正则兼容 bug，改用 ``stock_notice_report``。
    """
    ak = _akshare()
    if ak is None:
        return
    today = date.today()

    # 1) 业绩预告：有明确「公告日期」，按季度可回放历史
    fn = None if notices_only else getattr(ak, "stock_yjyg_em", None)
    if fn is not None:
        for q in _quarter_ends(start, end):
            try:
                df = _quiet_call(fn, date=q)
            except Exception as exc:  # noqa: BLE001
                print(f"[akshare] 业绩预告 {q} 失败：{type(exc).__name__}: {str(exc)[:80]}")
                continue
            if df is None or df.empty:
                continue
            rows = []
            for _, r in df.iterrows():
                code = str(_pick(r, "股票代码", "代码", default="") or "").zfill(6)
                raw = _pick(r, "公告日期", default=None)
                if not code.isdigit() or raw is None:
                    continue
                ds = str(raw)[:10].replace("/", "-")
                try:
                    ds = datetime.strptime(ds, "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                if ds > today.isoformat():
                    continue  # 不写未来事件（策略本来就按 as-of 截断，这里再兜一层）
                rows.append((code, ds, "earnings", 1.0,
                             str(_pick(r, "股票简称", "名称", default="") or ""), "yjyg_em"))
            db.upsert_context_events(rows)

    # 2) 全市场每日公告：事件日历 + 舆情新闻同源，按交易日逐日回放
    notice_fn = getattr(ak, "stock_notice_report", None)
    if notice_fn is None:
        print("[akshare] 无 stock_notice_report，跳过公告采集")
        return
    ev_rows: list[tuple] = []
    news_rows: list[tuple] = []
    n_ev = n_news = 0
    for offset in range(int(notice_days)):
        d = today - timedelta(days=offset)
        if d.strftime("%Y-%m-%d") < start or d.strftime("%Y-%m-%d") > end:
            continue
        try:
            df = _quiet_call(notice_fn, symbol="全部", date=d.strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001 - 非交易日/接口抖动直接跳过
            continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            code = str(_pick(r, "代码", "股票代码", default="") or "").zfill(6)
            title = str(_pick(r, "公告标题", "标题", default="") or "")
            atype = str(_pick(r, "公告类型", default="") or "")
            pub = str(_pick(r, "公告日期", default=d.isoformat()))[:10].replace("/", "-")
            if not code.isdigit() or not title:
                continue
            if pub > today.isoformat():
                continue
            ev_rows.append((code, pub, _notice_kind(title, atype), 1.0, title[:200], "notice_report"))
            score = _score_text(f"{title} {atype}")
            if score != 0.0:
                news_rows.append((code, pub, score, 1.0, title[:200], "notice_report"))
        # 每 10 天落一次库：长任务被中断也不会把已采部分全丢掉
        if (offset + 1) % 10 == 0:
            n_ev += db.upsert_context_events(ev_rows)
            n_news += db.upsert_context_news(news_rows)
            ev_rows, news_rows = [], []
            print(f"[akshare] 公告已扫 {offset + 1}/{notice_days} 天，已落库 事件 {n_ev} / 舆情 {n_news}",
                  flush=True)
    n_ev += db.upsert_context_events(ev_rows)
    n_news += db.upsert_context_news(news_rows)
    print(f"[akshare] 全市场公告 {notice_days} 天 -> 事件 {n_ev} 条 / 舆情 {n_news} 条", flush=True)


# ---------------------------------------------------------------- CSV 导入
def _read_csv(path: str, cols: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str).fillna("")
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"CSV 缺列：{missing}；必需列 {cols}")
    return df


def import_events(path: str) -> int:
    df = _read_csv(path, ["code", "event_date", "kind"])
    rows = []
    for _, r in df.iterrows():
        rows.append((
            str(r["code"]).zfill(6), str(r["event_date"])[:10], str(r["kind"]),
            float(r.get("severity") or 1.0), str(r.get("title") or ""),
            str(r.get("source") or "csv"),
        ))
    n = db.upsert_context_events(rows)
    print(f"[import] 事件 {n} 条 <- {path}")
    return n


def import_news(path: str) -> int:
    df = _read_csv(path, ["code", "news_date", "sentiment"])
    rows = []
    for _, r in df.iterrows():
        rows.append((
            str(r["code"]).zfill(6), str(r["news_date"])[:10], float(r["sentiment"]),
            float(r.get("heat") or 1.0), str(r.get("title") or ""),
            str(r.get("source") or "csv"),
        ))
    n = db.upsert_context_news(rows)
    print(f"[import] 舆情 {n} 条 <- {path}")
    return n


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="P1 上下文风险闸快照采集（本地落库，策略按 as-of 读）")
    ap.add_argument("--build-heat", action="store_true", help="从本地日线预计算每日过热分位")
    ap.add_argument("--akshare", action="store_true", help="尽力而为采集 akshare 事件 + 新闻")
    ap.add_argument("--import-events", metavar="CSV")
    ap.add_argument("--import-news", metavar="CSV")
    ap.add_argument("--check", action="store_true", help="只报告快照覆盖情况")
    ap.add_argument("--from", dest="start", default="2022-01-01")
    ap.add_argument("--to", dest="end", default=date.today().isoformat())
    ap.add_argument("--pct", type=float, default=0.20, help="剔除最热比例（默认 0.20）")
    ap.add_argument("--notice-days", type=int, default=45,
                    help="--akshare 逐日回看全市场公告的自然日数（默认 45）")
    ap.add_argument("--notices-only", action="store_true",
                    help="只采全市场公告，跳过已落库的季度业绩预告")
    ap.add_argument("--clean-events", action="store_true",
                    help="收紧公告分类后清洗历史：删掉误归 earnings 的普通公告行")
    args = ap.parse_args()

    if not any((args.build_heat, args.akshare, args.import_events, args.import_news,
                args.check, args.notices_only, args.clean_events)):
        ap.print_help()
        return 0

    if args.clean_events:
        print(f"[clean] 普通公告归位 {db.normalize_context_event_kinds()} 条", flush=True)

    if args.build_heat:
        build_heat(args.start, args.end, args.pct)
    if args.import_events:
        import_events(args.import_events)
    if args.import_news:
        import_news(args.import_news)
    if args.akshare or args.notices_only:
        collect_akshare(args.start, args.end, args.notice_days, args.notices_only)

    cov = ContextStore().coverage()
    print("-" * 62)
    print("上下文快照覆盖（策略实际读到的数据，不是实时接口）：")
    print(f"  数据表可用      : {cov['tables']}   ({cov['db']})")
    print(f"  事件日历        : {cov['events']} 条  {cov['events_from']} ~ {cov['events_to']}")
    print(f"  舆情新闻        : {cov['news']} 条  {cov['news_from']} ~ {cov['news_to']}")
    print(f"  热度分位交易日  : {cov['heat_days']} 天  {cov['heat_from']} ~ {cov['heat_to']}")
    if not cov["heat_days"]:
        print("  ⚠ 热度分位为空：过热子闸两侧都静默不触发。请先跑 --build-heat。")
    if not cov["events"] or not cov["news"]:
        print("  ⚠ 事件/舆情为空：相应子闸两侧都静默不触发，且**两侧一致**。")
        print("     补齐历史请用 --import-events/--import-news 或 --akshare；")
        print("     严禁只给实盘喂实时数据而不补历史，那会造成 1:1 漂移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
