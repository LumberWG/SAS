# -*- coding: utf-8 -*-
"""并行分片启动器：把全市场拆成 N 个进程各跑 1/N，互不干扰，大幅提速。

进度显示：每个分片的实时日志会同时镜像到当前控制台（每行带 [S1]/[S2] 标签区分分片），
同时照常写入 backend/logs/minute5_shard_<i>.log（可单独 tail 看某一片）。

卡死自愈（看门狗）：Baostock 走 SOCKS 代理时偶发连接被断开，读取会卡住；
若某分片超过 --stall-timeout 分钟没有任何输出，启动器会自动杀掉并重启该分片
（已落盘的数据会断点续传，不会丢），最多重启 --max-restarts 次。

用法（在你自己的 PowerShell 里执行，这样能稳定跑完多天）：
    cd e:/Study/SAS/backend
    .venv\\Scripts\\python.exe scripts/preload_minutes_parallel.py --shards 4
    # 只先验证前 6 只（2 片各 3 只）：
    .venv\\Scripts\\python.exe scripts/preload_minutes_parallel.py --shards 2 --limit 6
    # 片数可调，并可用 --start/--end 收窄区间；--sleep 调大可更稳（默认 1.5s）

按 Ctrl+C（或任务管理器结束）可一次性终止所有分片；已落盘的数据不会丢，重跑自动续传。
"""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "preload_minutes.py"
LOGDIR = BACKEND / "logs"

_shards = []         # 供信号处理函数清理子进程
_print_lock = threading.Lock()


def _safe_print(msg: str) -> None:
    """线程安全地打印到控制台（Windows 控制台编码兼容）。"""
    with _print_lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def _decode(raw: bytes) -> str:
    """智能解码子进程的一行输出：先按 UTF-8，失败回退 GBK，最后才替换成 ?。

    为什么需要：baostock 库在 socket 出错时会绕过我们的 log() 直接用 print()
    打中文（如 "[WinError 10053] 您的主机中的软件中止了一个已建立的连接"），
    走的是 Windows 默认的 GBK 编码。父进程如果硬按 UTF-8 解码，整行就会变成
    一串方块乱码（"      "），完全看不出是什么错。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _pump(tag: "str | None", proc: "subprocess.Popen", logf: "Path | None", mode: str = "w") -> None:
    """读取子进程 stdout，逐行同时写入日志文件和镜像到控制台（tag=None 则不镜像）。

    日志文件若被上一个进程占用（打不开），降级为只输出到控制台，不让泵线程静默死掉。
    """
    f = None
    try:
        if logf is not None:
            try:
                f = open(logf, mode, encoding="utf-8")
            except Exception as _e:  # noqa: BLE001
                _safe_print(f"[launcher] 警告：日志 {logf.name} 打不开（{_e}），该分片仅输出到控制台")
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = _decode(raw)
            if f is not None:
                f.write(line)
                f.flush()
            if tag:
                _safe_print(f"[{tag}] {line.rstrip()}")
    except Exception:  # noqa: BLE001
        pass
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass


def _spawn(sh: dict, n: int, console: bool) -> None:
    """（重新）启动某个分片子进程，并挂上输出泵线程。"""
    cmd = sh["cmd"]
    # 二进制管道 + 自行解码：子进程输出可能混着 UTF-8（我们的日志）和 GBK（baostock 的 print），
    # 统一按 UTF-8 解码会出乱码，见 _decode()。
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    mode = "w" if sh["restarts"] == 0 else "a"
    tag = None if not console else f"S{sh['i']}"
    t = threading.Thread(target=_pump, args=(tag, p, sh["logf"], mode), daemon=True)
    t.start()
    sh["p"] = p
    sh["t"] = t
    sh["spawned_at"] = time.time()


def _terminate_all() -> None:
    for sh in _shards:
        try:
            sh["p"].terminate()
        except Exception:  # noqa: BLE001
            pass
    for sh in _shards:
        try:
            sh["p"].wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                sh["p"].kill()
            except Exception:  # noqa: BLE001
                pass


def _watchdog(stall_sec: float, max_restarts: int, n: int, console: bool) -> None:
    """监控各分片日志文件的写入时间，超时未输出即判定卡死并重启该分片。"""
    while True:
        time.sleep(20)
        now = time.time()
        for sh in _shards:
            p = sh["p"]
            if p.poll() is not None:
                continue
            try:
                last = sh["logf"].stat().st_mtime
            except OSError:
                last = sh["spawned_at"]
            idle = now - last
            if idle < stall_sec:
                continue
            if sh["restarts"] >= max_restarts:
                if not sh.get("gave_up"):
                    sh["gave_up"] = True
                    _safe_print(f"[launcher] 分片 {sh['i']} 卡死且已达最大重启次数({max_restarts})，不再重启")
                continue
            _safe_print(f"[launcher] 分片 {sh['i']} 已 {idle / 60:.0f} 分钟无输出 -> 判定卡死，重启该分片"
                        f"（第 {sh['restarts'] + 1} 次重启，已抓数据续传）")
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                p.wait(timeout=15)
            except Exception:  # noqa: BLE001
                pass
            sh["restarts"] += 1
            _spawn(sh, n, console)
            _safe_print(f"[launcher] 分片 {sh['i']} 已重启  PID={sh['p'].pid}")


def main() -> None:
    # 让控制台以 UTF-8 输出（避免中文乱码/报错），失败则忽略（用 _safe_print 兜底）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="并行分片预下载 5 分钟线（Baostock）")
    ap.add_argument("--shards", type=int, default=4, help="并行分片数（进程数），默认 4")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 只再分片（0=全市场）")
    ap.add_argument("--period", default="5", help="分钟周期")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="每个子进程的请求间隔（秒）；并行时略大于单进程更稳，默认 1.5")
    ap.add_argument("--start", default=None, help="起始日期（默认 2025-01-01）")
    ap.add_argument("--end", default=None, help="结束日期")
    ap.add_argument("--kline-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--python", default=None, help="venv python 路径（默认自动探测 .venv）")
    ap.add_argument("--no-console", action="store_true",
                    help="不镜像到控制台，只写日志文件（适合后台/计划任务）")
    ap.add_argument("--proxy", action="store_true",
                    help="走 SOCKS5 代理（默认直连 Baostock，实测直连快约 2 倍）")
    ap.add_argument("--stall-timeout", type=float, default=10.0,
                    help="某分片超过该分钟数无任何输出即判定卡死并重启，默认 10 分钟（0=关闭看门狗）")
    ap.add_argument("--max-restarts", type=int, default=20,
                    help="每个分片最多自动重启次数，默认 20")
    args = ap.parse_args()

    py = args.python or str(BACKEND / ".venv" / "Scripts" / "python.exe")
    if not Path(py).exists():
        py = sys.executable
    LOGDIR.mkdir(parents=True, exist_ok=True)

    # 公共参数（传给每个子进程）
    base = [py, str(WORKER)]
    if args.limit:
        base += ["--limit", str(args.limit)]
    if args.start:
        base += ["--start", args.start]
    if args.end:
        base += ["--end", args.end]
    if args.period != "5":
        base += ["--period", args.period]
    base += ["--sleep", str(args.sleep)]
    if args.proxy:
        base.append("--proxy")
    if args.kline_dir:
        base += ["--kline-dir", args.kline_dir]
    if args.out_dir:
        base += ["--out-dir", args.out_dir]

    n = max(1, args.shards)
    console = not args.no_console

    for i in range(1, n + 1):
        sh = {
            "i": i,
            "cmd": base + ["--shard", str(i), "--shards", str(n)],
            "logf": LOGDIR / f"minute5_shard_{i}.log",
            "restarts": 0,
        }
        _shards.append(sh)
        _spawn(sh, n, console)
        _safe_print(f"[launcher] 启动分片 {i}/{n}  PID={sh['p'].pid}  ->  logs/{sh['logf'].name}")

    # 同时处理 SIGTERM（如 timeout / 任务管理器），确保子进程被回收
    try:
        signal.signal(signal.SIGTERM, lambda *a: _terminate_all())
    except (ValueError, OSError):
        pass  # 非主线程/Windows 不支持时忽略

    _safe_print(f"[launcher] 已启动 {n} 个并行分片，进度实时显示在下方（[S1]~[S{n}] 为各分片）。")
    _safe_print("[launcher] 按 Ctrl+C 可终止全部（已抓取数据不会丢失，重跑自动续传）。")
    if args.no_console:
        _safe_print(f"[launcher] 已静默模式，进度仅写入 {LOGDIR}（用 Get-Content -Wait 查看）。")

    if args.stall_timeout and args.stall_timeout > 0:
        threading.Thread(target=_watchdog,
                         args=(args.stall_timeout * 60.0, args.max_restarts, n, console),
                         daemon=True).start()
        _safe_print(f"[launcher] 看门狗已启用：任一分片 {args.stall_timeout:g} 分钟无输出即自动重启该分片。")

    try:
        while True:
            alive = [sh for sh in _shards if sh["p"].poll() is None]
            if not alive:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        _safe_print("\n[launcher] 收到 Ctrl+C，正在终止所有分片 ...")
        _terminate_all()
        _safe_print("[launcher] 已终止全部分片。已落盘数据保留，重跑会续传。")
        return

    for sh in _shards:
        rc = sh["p"].poll()
        extra = f"，自动重启 {sh['restarts']} 次" if sh["restarts"] else ""
        _safe_print(f"[launcher] 分片 {sh['i']} 结束，退出码={rc}{extra}")


if __name__ == "__main__":
    main()
