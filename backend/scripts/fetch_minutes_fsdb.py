"""用本地 free-stockdb 库抓全市场 5 分钟 K 线，写入独立目录（不覆盖 Baostock 数据）。

数据源：D:\\Backup\\SAS\\fsdb\\stockdb（本地 leveldb，127.0.0.1:7899）
输出：  E:\\Study\\SAS\\data\\minute5_fsdb\\<code>.parquet
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

START = "20250101"
END = "20261029"
FREQ = "5m"
FIELDS = ["date", "open", "high", "low", "close", "volume", "amount"]
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


def done_file(out_dir: Path, freq: str) -> Path:
    """已完成清单：避免每次重启都去读几千个 parquet 判断进度。"""
    return out_dir / f"_done_{freq}.txt"


def load_done(out_dir: Path, freq: str) -> set[str]:
    p = done_file(out_dir, freq)
    if not p.exists():
        return set()
    try:
        return {x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}
    except Exception:  # noqa: BLE001
        return set()


def mark_done(out_dir: Path, freq: str, code: str) -> None:
    with _LOCK:
        try:
            with open(done_file(out_dir, freq), "a", encoding="utf-8") as f:
                f.write(code + "\n")
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

    ``start`` / ``end`` 为空时沿用模块级 ``START`` / ``END``（日更与全量导出行为不变）；
    ``min_rows`` 是「已导出多少行算完整」的阈值，导短区间（如只导 2024 一年）时必须调小，
    否则会被误判为不完整而反复重抓。
    """
    if not force:
        if existing_rows(out) >= min_rows:
            mark_done(out_dir, freq, code)
            return ("skip", None)

    t0 = time.time()
    try:
        df = to_frame(code, freq, start=start, end=end)
    except Exception as e:  # noqa: BLE001
        net_like = isinstance(e, (TimeoutError, ConnectionError, OSError)) or any(
            k in str(e).lower() for k in ("connect", "timeout", "refused", "reset")
        )
        if net_like and ensure_server_ts():
            try:
                df = to_frame(code, freq, start=start, end=end)
            except Exception as e2:  # noqa: BLE001
                return ("fail", f"重试异常 {type(e2).__name__}: {str(e2)[:70]}")
        else:
            return ("fail", f"{type(e).__name__}: {str(e)[:70]}")

    if df.empty:
        return ("fail", "无数据")

    df.to_parquet(out, index=False)
    mark_done(out_dir, freq, code)
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
    ap.add_argument("--end", default="", help=f"结束日期 YYYYMMDD（默认 {END}）")
    ap.add_argument(
        "--min-rows", type=int, default=MIN_ROWS,
        help=f"已导出多少行算完整（默认 {MIN_ROWS}）；导短区间时需调小，否则会被误判为不完整",
    )
    args = ap.parse_args()

    freq = args.freq
    out_dir = Path(args.out) if args.out else (
        DATA_ROOT / "minute1_fsdb" if freq == "1m" else OUT_DIR
    )

    start_date = args.start or START
    end_date = args.end or END
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
    done_set = load_done(out_dir, freq)
    if done_set:
        log(f"已完成清单载入 {len(done_set)} 只")

    pending = []
    for code in codes:
        if not args.force and code in done_set:
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
