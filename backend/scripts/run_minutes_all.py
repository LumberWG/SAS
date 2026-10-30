"""分钟线导出编排：5 分钟线跑满后，自动接着导 1 分钟线。

两个子任务都支持断点续传，进程被中断后重新运行会跳过已完成文件。

用法：
    python scripts/run_minutes_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
FETCH = HERE / "fetch_minutes_fsdb.py"
DATA = Path(os.getenv("SAS_DATA_DIR", r"E:\Study\SAS\data"))

# 退市/停牌等无数据代码占比估计，达到该比例视为该周期已跑完
DONE_RATIO = 0.95


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def count(dirname: str) -> int:
    d = DATA / dirname
    return len(list(d.glob("*.parquet"))) if d.exists() else 0


# 并发度：5m 输出行数少、内存压力小，可高并发；1m 单只 10 万行，并发过高反而变慢
WORKERS = {"5m": 16, "1m": 4}


def run(freq: str) -> int:
    w = WORKERS.get(freq, 8)
    log(f"=== 启动 {freq} 导出（并发 {w}）===")
    t0 = time.time()
    rc = subprocess.run([PY, str(FETCH), "--freq", freq, "--workers", str(w)]).returncode
    log(f"=== {freq} 结束，退出码 {rc}，用时 {(time.time() - t0) / 60:.1f} 分钟 ===")
    return rc


def main() -> int:
    total = count("klines")
    if total == 0:
        log(f"[FATAL] {DATA / 'klines'} 下没有 K 线文件，无法确定代码池")
        return 2

    m5 = count("minute5_fsdb")
    m1 = count("minute1_fsdb")
    log(f"代码池 {total} 只 | 已有 5m={m5} 1m={m1}")

    # 阶段一：5 分钟线
    if m5 < total * DONE_RATIO:
        rc = run("5m")
        if rc != 0:
            log("5m 未正常结束（可能被中断），本轮退出，下次运行会继续")
            return rc
    else:
        log("5m 已达标，跳过")

    m5 = count("minute5_fsdb")
    # 注意：退市/B股/无数据代码本来就导不出来，文件数天然达不到 100%。
    # 只要 5m 阶段是「正常跑完」（rc==0，即所有待处理代码都过了），就可以进 1m，
    # 不再用文件数硬卡，否则会被几十只无数据代码卡住。
    if m5 < total * DONE_RATIO:
        log(f"5m 仅 {m5}/{total}，但已跑完待处理队列，继续进入 1m 阶段")

    # 阶段二：1 分钟线
    m1 = count("minute1_fsdb")
    if m1 < total * DONE_RATIO:
        rc = run("1m")
        if rc != 0:
            log("1m 未正常结束（可能被中断），本轮退出，下次运行会继续")
            return rc
    else:
        log("1m 已达标，跳过")

    # 阶段三：补齐失败项（SDK 聚合 bug 已修，停牌股可救回）
    log("=== 阶段三：补齐失败项 ===")
    try:
        import subprocess as _sp

        _sp.run([PY, str(HERE / "repair_failed.py"), "--freq", "both", "--workers", "6"])
    except Exception as e:  # noqa: BLE001
        log(f"补齐阶段异常（不影响已导数据）：{type(e).__name__}: {e}")

    log(f"全部完成：5m={count('minute5_fsdb')} 1m={count('minute1_fsdb')} / 代码池 {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
