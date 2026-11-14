"""采集「非价量」外部数据到独立的本地快照（影子研究，零生产写入）。

三类数据：融资余额（沪深交易所逐日明细）、股东户数（东财，按**公告日期**对齐）、
龙虎榜（东财逐日明细）。落 ``D:/Backup/SAS/data/alt_data.db``，查询统一走
``app.data.alt_store`` 的 as_of 截断，见 ``ASOF_RULES``。

**防前视三处硬约束**（改动前先读 alt_store.py 的同名说明）：
1. 融资余额 / 龙虎榜是 T 日盘后发布，查询层一律 ``trade_date < as_of``；
2. 股东户数只用「股东户数公告日期」，绝不用「统计截止日」（报告期会看未来）；
3. 东财龙虎榜接口自带「上榜后1/2/5/10日」涨跌幅等**未来收益列**，本脚本用
   显式白名单只保留 6 列，绝不落库，否则探针会不小心吃到未来信息。

用法（需要外网；代理没开时用 ``--proxy none`` 直连，或先启动本机代理）：
    .venv\\Scripts\\python.exe scripts\\fetch_alt_data.py --dry-run
    .venv\\Scripts\\python.exe scripts\\fetch_alt_data.py --from 20250101 --parts margin,lhb
    .venv\\Scripts\\python.exe scripts\\fetch_alt_data.py --parts gdhs --max-codes 300
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.data.alt_store import DATA_DIR, SCHEMA_SQL, connect  # noqa: E402

KLINE_DIR = DATA_DIR / "klines"
CALENDAR_FILE = KLINE_DIR / "idx_sh000001.parquet"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

# 东财龙虎榜接口会返回「上榜后1日 / 2日 / 5日 / 10日」等未来收益，严禁入库
LHB_KEEP = {
    "上榜日": "trade_date",
    "代码": "code",
    "上榜原因": "reason",
    "龙虎榜净买额": "net_buy",
    "龙虎榜买入额": "buy_amt",
    "龙虎榜卖出额": "sell_amt",
}


def _d(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value)[:10].replace("/", "-")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _code(value) -> str | None:
    text = "".join(ch for ch in str(value) if ch.isdigit())[-6:]
    return text if len(text) == 6 else None


def _col(df: pd.DataFrame, *names: str) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _yyyymmdd(day: str) -> str:
    return day.replace("-", "")


def _apply_proxy(mode: str) -> None:
    if mode == "none":
        for key in PROXY_KEYS:
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
    elif mode != "auto":
        os.environ["HTTPS_PROXY"] = mode
        os.environ["HTTP_PROXY"] = mode
        os.environ.pop("NO_PROXY", None)


def _calendar() -> list[str]:
    if CALENDAR_FILE.exists():
        df = pd.read_parquet(CALENDAR_FILE, columns=["date"])
        days = sorted({_d(x) for x in df["date"]} - {None})
        if days:
            return days  # type: ignore[return-value]
    # 退化：工作日近似，仅用于估算，不作为实际可用日依据
    return [d.isoformat() for d in pd.bdate_range("2022-01-01", date.today())]


def _codes_from_args(args) -> list[str]:
    raw: list[str] = []
    if args.codes:
        raw.extend(x.strip() for x in args.codes.split(","))
    if args.codes_file:
        raw.extend(Path(args.codes_file).read_text(encoding="utf-8").splitlines())
    codes = [c for c in (_code(x) for x in raw if x.strip()) if c]
    if codes:
        return list(dict.fromkeys(codes))
    # 未指定票池时，从本地日 K 目录固定随机抽样，仅供覆盖率自检，不代表真实票池
    files = sorted(KLINE_DIR.glob("[0-9]" * 6 + ".parquet"))
    if not files:
        return []
    picked = pd.Series([p.stem for p in files]).sample(
        n=min(args.max_codes, len(files)), random_state=42
    )
    return picked.tolist()


def _progress_done(conn, part: str, key: str) -> bool:
    row = conn.execute(
        "SELECT ok FROM _progress WHERE part = ? AND key = ?", (part, key)
    ).fetchone()
    return bool(row and row["ok"])


def _progress_set(conn, part: str, key: str, ok: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO _progress(part, key, ok, ts) VALUES (?,?,?,?)",
        (part, key, int(ok), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def _fetch_margin_day(ak, conn, day: str, ts: str) -> int:
    n = 0
    for name, fn in (
        ("sse", ak.stock_margin_detail_sse),
        ("szse", ak.stock_margin_detail_szse),
        ("bse", ak.stock_margin_detail_bse),
    ):
        try:
            df = fn(date=_yyyymmdd(day))
        except Exception as exc:  # 单一交易所失败不阻断其它所
            print(f"  ! {day} {name}: {type(exc).__name__}: {str(exc)[:90]}")
            continue
        if df is None or df.empty:
            continue
        c_code = _col(df, "标的证券代码", "证券代码", "代码")
        c_bal = _col(df, "融资余额")
        c_buy = _col(df, "融资买入额")
        if c_code is None or c_bal is None:
            print(f"  ! {day} {name}: 列名不匹配 {list(df.columns)[:8]}")
            continue
        rows = []
        for _, r in df.iterrows():
            code = _code(r[c_code])
            bal = _num(r[c_bal])
            if code and bal is not None:
                rows.append(
                    (day, code, bal, _num(r[c_buy]) if c_buy else None, name, ts)
                )
        conn.executemany(
            "INSERT OR REPLACE INTO alt_margin"
            "(trade_date, code, margin_balance, margin_buy, source, collected_at)"
            " VALUES (?,?,?,?,?,?)",
            rows,
        )
        n += len(rows)
    conn.commit()
    return n


def _fetch_lhb_range(ak, conn, start: str, end: str, ts: str) -> int:
    df = ak.stock_lhb_detail_em(
        start_date=_yyyymmdd(start), end_date=_yyyymmdd(end)
    )
    if df is None or df.empty:
        return 0
    missing = [k for k in LHB_KEEP if k not in df.columns]
    if missing:
        print(f"  ! 龙虎榜缺列 {missing}，跳过该区间")
        return 0
    rows = []
    for _, r in df[list(LHB_KEEP)].iterrows():
        day, code = _d(r["上榜日"]), _code(r["代码"])
        if not day or not code:
            continue
        rows.append(
            (
                day,
                code,
                str(r["上榜原因"] or "")[:60],
                _num(r["龙虎榜净买额"]),
                _num(r["龙虎榜买入额"]),
                _num(r["龙虎榜卖出额"]),
                "em",
                ts,
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO alt_lhb"
        "(trade_date, code, reason, net_buy, buy_amt, sell_amt, source, collected_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _fetch_gdhs_code(ak, conn, code: str, ts: str) -> int:
    df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
    if df is None or df.empty:
        return 0
    c_end = _col(df, "股东户数统计截止日", "统计截止日")
    c_ann = _col(df, "股东户数公告日期", "公告日期")
    c_now = _col(df, "股东户数-本次", "本次")
    c_prev = _col(df, "股东户数-上次", "上次")
    c_chg = _col(df, "股东户数-增减比例", "增减比例")
    if c_end is None or c_ann is None:
        print(f"  ! {code}: 股东户数缺日期列 {list(df.columns)[:8]}")
        return 0
    rows = []
    for _, r in df.iterrows():
        end_day, ann_day = _d(r[c_end]), _d(r[c_ann])
        if not end_day:
            continue
        # 公告日期缺失 -> ann_date 为 NULL，查询层会一致地忽略（零前视优先于覆盖率）
        rows.append(
            (
                code,
                end_day,
                ann_day,
                _num(r[c_now]) if c_now else None,
                _num(r[c_prev]) if c_prev else None,
                _num(r[c_chg]) if c_chg else None,
                "em",
                ts,
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO alt_gdhs"
        "(code, end_date, ann_date, holders, prev_holders, change_pct, source, collected_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _chunks(days: list[str], size: int = 90) -> list[tuple[str, str]]:
    out = []
    for i in range(0, len(days), size):
        part = days[i : i + size]
        out.append((part[0], part[-1]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="采集非价量外部数据到独立本地快照")
    ap.add_argument("--from", dest="start", default=(date.today() - timedelta(days=365)).isoformat())
    ap.add_argument("--to", dest="end", default=date.today().isoformat())
    ap.add_argument("--parts", default="margin,lhb,gdhs")
    ap.add_argument("--codes", default="", help="逗号分隔的股票代码（gdhs 用）")
    ap.add_argument("--codes-file", default="", help="每行一个股票代码（gdhs 用）")
    ap.add_argument("--max-codes", type=int, default=300)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=0, help="每个 part 最多处理 N 个 key（冒烟用）")
    ap.add_argument("--db", default="")
    ap.add_argument("--proxy", default="auto", help="auto | none | 代理 URL")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start, end = _d(args.start), _d(args.end)
    if not start or not end or start > end:
        print("日期参数不合法")
        return 2
    days = [d for d in _calendar() if start <= d <= end]
    parts = [p.strip() for p in args.parts.split(",") if p.strip()]
    codes = _codes_from_args(args) if "gdhs" in parts else []
    ranges = _chunks(days) if "lhb" in parts and days else []
    keys = {
        "margin": days,
        "lhb": [f"{a}~{b}" for a, b in ranges],
        "gdhs": codes,
    }
    requests_est = len(days) * 3 + len(ranges) + len(codes)

    print(f"窗口 {start} ~ {end} | 交易日 {len(days)} | parts {parts}")
    print(f"计划 key 数：margin={len(days)} × 3 所，lhb={len(ranges)} 区间，gdhs={len(codes)} 只")
    print(f"预计请求 ≈ {requests_est} 次，间隔 {args.sleep}s")
    if args.dry_run:
        print("dry-run：未联网、未写库。代理可用后去掉 --dry-run 即可正式采集。")
        return 0

    _apply_proxy(args.proxy)
    try:
        import akshare as ak
    except ImportError:
        print("缺少 akshare，请在 backend/.venv 内运行")
        return 2

    db_path = Path(args.db) if args.db else None
    conn = connect(db_path, readonly=False) if db_path else connect(readonly=False)
    conn.executescript(SCHEMA_SQL)
    ts = datetime.now().isoformat(timespec="seconds")
    stats: dict[str, int] = {}
    fails = 0

    for part in parts:
        todo = keys.get(part, [])
        if args.limit:
            todo = todo[: args.limit]
        written = 0
        print(f"[{part}] 处理 {len(todo)} 个 key")
        for i, key in enumerate(todo, 1):
            if _progress_done(conn, f"{part}_key", key):
                continue
            try:
                if part == "margin":
                    written += _fetch_margin_day(ak, conn, key, ts)
                elif part == "lhb":
                    a, b = key.split("~")
                    written += _fetch_lhb_range(ak, conn, a, b, ts)
                elif part == "gdhs":
                    written += _fetch_gdhs_code(ak, conn, key, ts)
                _progress_set(conn, f"{part}_key", key, True)
                fails = 0
            except Exception as exc:
                fails += 1
                _progress_set(conn, f"{part}_key", key, False)
                print(f"  ! {part} {key}: {type(exc).__name__}: {str(exc)[:110]}")
                if fails >= 5:
                    print("连续失败 5 次，中止。检查本机代理（默认 127.0.0.1:7890）或试 --proxy none")
                    break
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} 已写入 {written} 行")
            time.sleep(args.sleep)
        stats[part] = written

    for table in ("alt_margin", "alt_lhb", "alt_gdhs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: 累计 {n} 行")
    print(f"本轮写入 {stats}；库 {conn.execute('PRAGMA database_list').fetchone()['file']}")
    conn.close()
    return 0 if fails < 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
