"""用本地 free-stockdb 库抓全市场 5 分钟 K 线，写入独立目录（不覆盖 Baostock 数据）。

数据源：D:\\Backup\\SAS\\fsdb\\stockdb（本地 leveldb，127.0.0.1:7899）
输出：  D:\\Backup\\SAS\\data\\minute5_fsdb\\<code>.parquet
       列：date(str 'YYYY-MM-DD HH:MM:SS'), open, high, low, close, volume, amount

特点：断点续传（已有且行数达标则跳过）、可随时 Ctrl+C、无网络封禁风险。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# stockdb 查询 90% 时间花在 leveldb 解码，多路并发可接近线性加速
_LOCK = threading.Lock()
_SERVER_LOCK = threading.Lock()

FSDB_ROOT = Path(os.getenv("SAS_FSDB_DIR", r"D:\Backup\SAS\fsdb\stockdb"))
DATA_ROOT = Path(os.getenv("SAS_DATA_DIR", r"D:\Backup\SAS\data"))
OUT_DIR = DATA_ROOT / "minute5_fsdb"
KLINE_DIR = DATA_ROOT / "klines"

sys.path.insert(0, str(FSDB_ROOT / "pybao"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from stock_sdk import rd  # noqa: E402
except Exception as _e:  # noqa: BLE001
    print(f"[FATAL] stock_sdk 导入失败: {type(_e).__name__}: {_e}")
    print("  请确认 fsdb/stockdb/stockdb.exe 已启动（127.0.0.1:7899 在监听）")
    sys.exit(2)

# 修 SDK 聚合 bug：停牌分钟的 high/low 为 None 会抛 TypeError，导致整只取不到数据
try:
    import fsdb_safe_merge  # noqa: E402

    fsdb_safe_merge.apply()
except Exception as _e:  # noqa: BLE001
    print(f"[WARN] 聚合补丁未生效（个别停牌股可能取不到）: {type(_e).__name__}: {_e}")

# 与 app.config.SHORT_TERM_RESEARCH_START 同源：短线定位下近两年分钟线足够，
# 不需要更久远的历史；本脚本不 import app，改起点时两处一起改。
START = "20250101"


def latest_trading_day(now: datetime | None = None) -> str:
    """最近一个交易日（YYYYMMDD）。

    与 ``app/market/clock.is_trading_day`` 同口径：只按周一至周五，不含法定节假日。
    节假日 fsdb 没有数据，``to_frame`` 返回空 → 增量分支当作「无新增」跳过，不影响已有数据。
    """
    d = now or datetime.now()
    while d.weekday() >= 5:  # 周六/周日回退到周五
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


#: 默认导出到「最近交易日」。可用环境变量 SAS_MINUTES_END 或命令行 --end 覆盖。
#: 2026-10-31 之前这里写死 ``"20261029"``，导致分钟库永远停在 10-29：
#: 个股详情的分时图比日 K 落后一天（页面标注「本地历史 · 10-29」，而报价是 10-30 收盘）。
END = os.getenv("SAS_MINUTES_END") or latest_trading_day()
FREQ = "5m"
FIELDS = ["date", "open", "high", "low", "close", "volume", "amount"]

#: 「这只票在目标日期上问到过、但数据库没有数据」的标记，写成 ``NODATA-YYYYMMDD``。
#: 没有它的话，退市/北交所那 100 多只无数据代码会在每次日更里被反复重试
#: （每次 1~2 分钟纯超时），而它们本来就不可能有数据。
NODATA_PREFIX = "NODATA-"
# 行数低于此值视为不完整（一个完整年度约 48 根/天 × 240 天）
MIN_ROWS = 2000


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    with _LOCK:
        try:
            sys.stdout.buffer.write(line.encode("utf-8", "replace"))
            sys.stdout.buffer.flush()
        except Exception:  # noqa: BLE001
            pass


def port_open(timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", 7899)) == 0
    finally:
        s.close()


def _start_via_service() -> bool:
    """装了 Windows 服务就优先走服务（服务存在时独立进程会被拒绝启动）。"""
    try:
        r = subprocess.run(["net", "start", "stockdb"], capture_output=True, timeout=180)
        if r.returncode == 0:
            log("已通过服务 net start stockdb 启动")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _start_via_popen() -> bool:
    """回退：直接拉起（必须显式传 conf，否则静默退出、不监听端口）。"""
    try:
        subprocess.Popen(
            [str(FSDB_ROOT / "stockdb.exe"), str(FSDB_ROOT / "stockdb.conf")],
            cwd=str(FSDB_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        log("已独立进程启动 stockdb.exe（conf=stockdb.conf）")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"!! 启动失败 {type(e).__name__}: {e}")
        return False


def ensure_server(wait_s: int = 120) -> bool:
    if port_open():
        return True
    log("!! 数据库 7899 未监听，正在启动 stockdb ...")
    _start_via_service()
    if not port_open():
        _start_via_popen()
    for i in range(wait_s):
        time.sleep(1)
        if port_open():
            log(f"数据库已就绪（等待 {i + 1}s）")
            time.sleep(3)
            return True
    log("!! 等待超时，数据库仍未就绪")
    return False


def to_frame(code: str, freq: str = FREQ, start: str = "", end: str = "") -> pd.DataFrame:
    rows = rd.get_data(code, start=start or START, end=end or END, frequency=freq, fq=None)
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame(columns=FIELDS)
    df = pd.DataFrame(rows)
    keep = [c for c in FIELDS if c in df.columns]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(
        df["date"].astype("int64").astype(str), format="%Y%m%d%H%M%S"
    ).dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).drop_duplicates(subset=["date"]).sort_values("date")
    return df.reset_index(drop=True)


def existing_rows(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return len(pd.read_parquet(path, columns=["date"]))
    except Exception:  # noqa: BLE001
        return 0


def existing_last_date(path: Path) -> str:
    """已导出文件的最后一根 K 线日期（YYYYMMDD），没有/读不出返回空串。

    「是否已最新」必须看这个而不是行数：分钟线每天稳定增加 242 根，
    只看行数会把「停在三天前」的文件误判为已完成（10-30 那次就是因此没补上）。
    """
    if not path.exists():
        return ""
    try:
        s = pd.read_parquet(path, columns=["date"])["date"]
        if s.empty:
            return ""
        return str(s.iloc[-1])[:10].replace("-", "")
    except Exception:  # noqa: BLE001
        return ""


def done_file(out_dir: Path, freq: str) -> Path:
    """已完成清单：避免每次重启都去读几千个 parquet 判断进度。"""
    return out_dir / f"_done_{freq}.txt"


def load_done(out_dir: Path, freq: str) -> dict[str, str]:
    """已完成清单：``{code: 最后一根日期 YYYYMMDD}``。

    行格式 ``code|YYYYMMDD``；旧版本只写 ``code`` 的行仍能读，日期视为空（会被当成待检查）。
    """
    p = done_file(out_dir, freq)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            code, _, last = line.partition("|")
            code = code.strip()
            if code:
                out[code] = last.strip()  # 后写覆盖先写，取最新日期
    except Exception:  # noqa: BLE001
        return {}
    return out


def mark_done(out_dir: Path, freq: str, code: str, last_date: str = "") -> None:
    with _LOCK:
        try:
            with open(done_file(out_dir, freq), "a", encoding="utf-8") as f:
                f.write(f"{code}|{last_date}\n")
        except Exception:  # noqa: BLE001
            pass


def ensure_server_ts() -> bool:
    """多线程自愈：只让一个线程去拉起 stockdb，其余等待。"""
    if port_open():
        return True
    with _SERVER_LOCK:
        if port_open():
            return True
        return ensure_server()


def process_one(
    code: str, out: Path, freq: str, out_dir: Path, force: bool,
    start: str = "", end: str = "", min_rows: int = MIN_ROWS,
):
    """处理单只股票，返回 (status, detail)。status ∈ {ok, skip, fail}。

    ``start`` / ``end`` 为空时沿用模块级 ``START`` / ``END``。
    「是否已最新」按**文件最后一根的日期**判断（不再只看行数）；落后的文件走**增量补拉**
    （``start=最后一根日期``，重叠一根用于覆盖当日不完整的尾根）后与原文件合并去重，
    不整段重抓。``min_rows`` 仅为兼容旧调用保留，已不参与跳过判断。
    """
    end_compact = (end or END).replace("-", "")
    last = existing_last_date(out)

    if not force:
        if last and last >= end_compact:
            mark_done(out_dir, freq, code, last)
            return ("skip", None)
        if last and existing_rows(out) == 0:  # 文件读得出日期却取不到行 → 当损坏重建
            last = ""

    incremental = bool(last) and not force
    fetch_start = last if incremental else start
    t0 = time.time()
    try:
        df = to_frame(code, freq, start=fetch_start, end=end)
    except Exception as e:  # noqa: BLE001
        net_like = isinstance(e, (TimeoutError, ConnectionError, OSError)) or any(
            k in str(e).lower() for k in ("connect", "timeout", "refused", "reset")
        )
        if net_like and ensure_server_ts():
            try:
                df = to_frame(code, freq, start=fetch_start, end=end)
            except Exception as e2:  # noqa: BLE001
                return ("fail", f"重试异常 {type(e2).__name__}: {str(e2)[:70]}")
        else:
            return ("fail", f"{type(e).__name__}: {str(e)[:70]}")

    if df.empty:
        # 增量模式下拉不到新数据 = 该股当天停牌/无成交，属正常，不能记成失败
        # 无论哪种，都把「这个日期问过、没有数据」记进清单，避免同一天被反复重试
        mark_done(out_dir, freq, code, NODATA_PREFIX + end_compact)
        return ("skip", "无新增") if incremental else ("fail", "无数据")

    if incremental:
        try:
            old = pd.read_parquet(out)
            df = (
                pd.concat([old, df], ignore_index=True)
                .drop_duplicates(subset=["date"], keep="last")  # 重叠的那根以新抓到的为准
                .sort_values("date")
                .reset_index(drop=True)
            )
        except Exception as e:  # noqa: BLE001
            # 旧文件读坏（少见）：退回「只写新抓到的这一段」，不阻断整只票
            log(f"!! {code} 旧文件合并失败，改为只写增量段: {type(e).__name__}: {e}")

    df.to_parquet(out, index=False)
    mark_done(out_dir, freq, code, str(df["date"].iloc[-1])[:10].replace("-", ""))
    return ("ok", (len(df), time.time() - t0))


def main() -> None:
    ap = argparse.ArgumentParser(description="本地库抓分钟 K 线")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--codes", default="", help="逗号分隔的指定代码")
    ap.add_argument("--sleep", type=float, default=0.0, help="每只间隔秒数")
    ap.add_argument("--force", action="store_true", help="已有文件也重抓")
    ap.add_argument("--freq", default=FREQ, choices=["1m", "5m", "15m", "30m", "60m"], help="K 线周期")
    ap.add_argument("--out", default="", help="输出目录（默认按周期自动选）")
    ap.add_argument("--workers", type=int, default=8, help="并发线程数（5m 建议 12~16，1m 建议 4）")
    ap.add_argument(
        "--start", default="",
        help=f"起始日期 YYYYMMDD（默认模块常量 {START}）。导早期历史时用它，"
             "配合 --out 指到独立目录，避免覆盖现有导出",
    )
    ap.add_argument(
        "--end", default="",
        help=f"结束日期 YYYYMMDD（默认最近交易日 {END}）；只补某一天就传那一天",
    )
    ap.add_argument(
        "--min-rows", type=int, default=MIN_ROWS,
        help="[已废弃] 旧版按行数判完整的阈值，现按文件最后一根日期判断，保留只为兼容",
    )
    args = ap.parse_args()

    freq = args.freq
    out_dir = Path(args.out) if args.out else (
        DATA_ROOT / "minute1_fsdb" if freq == "1m" else OUT_DIR
    )

    start_date = (args.start or START).replace("-", "")
    end_date = (args.end or END).replace("-", "")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not ensure_server():
        log("[FATAL] 数据库未就绪，退出。请手动双击 fsdb\\stockdb\\stockdb.exe")
        sys.exit(2)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = sorted(p.stem for p in KLINE_DIR.glob("*.parquet"))
    if args.limit:
        codes = codes[: args.limit]

    total = len(codes)
    ok = skip = fail = 0
    failed: list[tuple[str, str]] = []
    t_start = time.time()
    done_map = load_done(out_dir, freq)
    if done_map:
        log(f"已完成清单载入 {len(done_map)} 只（含最后日期）")

    pending = []
    for code in codes:
        # 跳过条件（旧的「清单里有这个 code」不再算数，它说明不了导到哪天）：
        #   ① 最后日期 >= 目标日期：已最新；
        #   ② 清单记着 NODATA-目标日期：这一天问过、确实没有数据（退市/北交所），当天不重试。
        # 旧清单（无日期）会被重新检查一次，之后 mark_done 会把日期写进去，后续就是纯查表。
        done = done_map.get(code, "")
        if not args.force:
            if done == NODATA_PREFIX + end_date:
                skip += 1
                continue
            if done and not done.startswith(NODATA_PREFIX) and done >= end_date:
                skip += 1
                continue
        pending.append(code)

    log(f"任务：{total} 只 · {freq} · {start_date}~{end_date} · 输出 {out_dir}")
    log(f"已完成 {skip} 只，待处理 {len(pending)} 只，并发 {max(1, args.workers)}")

    if pending:
        workers = max(1, args.workers)
        finished = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futmap = {
                ex.submit(
                    process_one, c, out_dir / f"{c}.parquet", freq, out_dir,
                    args.force, start_date, end_date, args.min_rows,
                ): c
                for c in pending
            }
            for fut in as_completed(futmap):
                code = futmap[fut]
                try:
                    status, detail = fut.result()
                except Exception as e:  # noqa: BLE001
                    status, detail = "fail", f"{type(e).__name__}: {str(e)[:70]}"

                finished += 1
                if status == "ok":
                    ok += 1
                elif status == "skip":
                    skip += 1
                else:
                    fail += 1
                    failed.append((code, detail))
                    log(f"X {code} {detail}")

                if finished % 20 == 0 or finished == len(pending):
                    elapsed = time.time() - t_start
                    tps = finished / elapsed if elapsed > 0 else 0
                    eta_min = (len(pending) - finished) / tps / 60 if tps > 0 else 0
                    log(
                        f"进度 {finished}/{len(pending)} ok={ok} skip={skip} fail={fail} "
                        f"| {tps:.2f} 只/秒 | 剩余约 {eta_min:.1f} 分钟"
                    )

                if args.sleep:
                    time.sleep(args.sleep)

    log(f"完成 总{total} ok={ok} skip={skip} fail={fail} 用时 {(time.time()-t_start)/60:.1f}min")
    if failed:
        log(f"失败清单（前 20）：{failed[:20]}")


if __name__ == "__main__":
    main()
