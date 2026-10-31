"""分钟线导出编排：5 分钟线跑满后，自动接着导 1 分钟线。

两个子任务都支持断点续传：**按文件最后一根的日期**判断是否已最新，落后的只补增量
（`fetch_minutes_fsdb.py` 内部以 `start=最后一根日期` 拉取后与旧文件合并去重），
进程被中断后重新运行会跳过已最新的文件。

用法：
    python scripts/run_minutes_all.py                   # 补到最近交易日
    python scripts/run_minutes_all.py --end 20261030    # 只补到指定日期
    python scripts/run_minutes_all.py --freq 1m         # 只跑 1 分钟线
    python scripts/run_minutes_all.py --force           # 忽略已有记录，全部重抓（很慢）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
FETCH = HERE / "fetch_minutes_fsdb.py"
DATA = Path(os.getenv("SAS_DATA_DIR", r"D:\Backup\SAS\data"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def count(dirname: str) -> int:
    d = DATA / dirname
    return len(list(d.glob("*.parquet"))) if d.exists() else 0


# 并发度：5m 输出行数少、内存压力小，可高并发；1m 单只 10 万行，并发过高反而变慢
WORKERS = {"5m": 16, "1m": 4}


def run(freq: str, extra: list[str]) -> int:
    w = WORKERS.get(freq, 8)
    log(f"=== 启动 {freq} 导出（并发 {w}{' · ' + ' '.join(extra) if extra else ''}）===")
    t0 = time.time()
    cmd = [PY, str(FETCH), "--freq", freq, "--workers", str(w), *extra]
    rc = subprocess.run(cmd).returncode
    log(f"=== {freq} 结束，退出码 {rc}，用时 {(time.time() - t0) / 60:.1f} 分钟 ===")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 5m + 1m 分钟线（本地 fsdb）")
    ap.add_argument("--end", default="", help="结束日期 YYYYMMDD（默认最近交易日）")
    ap.add_argument("--force", action="store_true", help="忽略已完成记录，全部重抓（很慢）")
    ap.add_argument("--freq", default="both", choices=["both", "5m", "1m"], help="只跑某一档")
    args = ap.parse_args()

    extra: list[str] = []
    if args.end:
        extra += ["--end", args.end]
    if args.force:
        extra += ["--force"]

    total = count("klines")
    if total == 0:
        log(f"[FATAL] {DATA / 'klines'} 下没有 K 线文件，无法确定代码池")
        return 2

    log(f"代码池 {total} 只 | 已有 5m={count('minute5_fsdb')} 1m={count('minute1_fsdb')}")
    log("目标：" + (args.end or "最近交易日") + "（落后的文件只补增量，不整段重抓）")

    # 阶段一：5 分钟线
    # 注意：不再用「文件数达标就跳过」的旧逻辑——文件数只能说明导过，说明不了导到哪天。
    # 是否已最新由 fetch_minutes_fsdb 按每只文件最后一根的日期判断（清单带日期后是纯查表）。
    if args.freq in ("both", "5m"):
        rc = run("5m", extra)
        if rc != 0:
            log("5m 未正常结束（可能被中断），本轮退出，下次运行会继续")
            return rc

    # 阶段二：1 分钟线
    if args.freq in ("both", "1m"):
        rc = run("1m", extra)
        if rc != 0:
            log("1m 未正常结束（可能被中断），本轮退出，下次运行会继续")
            return rc

    # 阶段三：补齐失败项（SDK 聚合 bug 已修，停牌股可救回）
    if args.freq == "both":
        log("=== 阶段三：补齐失败项 ===")
        try:
            _sp = subprocess
            _sp.run([PY, str(HERE / "repair_failed.py"), "--freq", "both", "--workers", "6"])
        except Exception as e:  # noqa: BLE001
            log(f"补齐阶段异常（不影响已导数据）：{type(e).__name__}: {e}")

    log(f"全部完成：5m={count('minute5_fsdb')} 1m={count('minute1_fsdb')} / 代码池 {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
