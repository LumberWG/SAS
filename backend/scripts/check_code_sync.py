"""磁盘代码 ↔ 运行中服务 同步检测（只读，不写库、不改配置）。

用法::

    cd E:/study/sas/backend && .venv/Scripts/python.exe scripts/check_code_sync.py

判定基准：Windows 上 NSSM 每次启动会新建 ``sas-service.log``，其**创建时间**
等于服务进程启动时间（2026-11-12 实测与 WMI CreationDate 完全一致）。凡是
mtime 晚于该时刻的 ``backend/app/**/*.py`` 与 ``.env``，运行中的服务都没加载。

为什么需要这个脚本：服务启动后改源码，``inspect.getsource`` 会用内存里的
``co_firstlineno`` 去读磁盘文件，行号错位会取到别的方法，导致对齐自检报出
「实盘未共用止损」这类**假漂移**。有了本脚本可直接看出是「代码没同步」。

退出码：0 = 已同步；1 = 后端有未加载代码（需重启）；2 = 基准日志缺失。
"""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOG_FILE = Path(os.environ.get("SAS_LOG_DIR", r"D:\Backup\SAS\data\logs")) / "sas-service.log"
TOLERANCE = 2.0  # 秒：容忍服务启动瞬间写入的文件


def _fmt(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def _api_token() -> str:
    env = BACKEND / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "API_TOKEN" in line and "=" in line:
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def probe_running_service() -> str | None:
    """探测运行中服务是否认识新端点；返回 None 表示跳过。"""
    token = _api_token()
    if not token:
        return None
    url = "http://127.0.0.1:8000/api/paper/exit-audit"
    try:
        req = urllib.request.Request(url, headers={"X-API-Token": token})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read(400).decode("utf-8", "replace").lstrip()
        return "已加载新代码（端点返回 JSON）" if body.startswith("{") else "仍是旧代码（端点落到前端 HTML）"
    except Exception as exc:  # noqa: BLE001 - 服务不可达只影响附加信息
        return f"服务不可达/跳过: {type(exc).__name__}"


def main() -> int:
    if not LOG_FILE.exists():
        print(f"  ✗ 找不到基准日志 {LOG_FILE}，无法判定服务启动时间")
        return 2
    started = os.path.getctime(LOG_FILE)
    print("=" * 68)
    print("磁盘代码 ↔ 运行中服务 同步检测")
    print("=" * 68)
    print(f"  服务启动时间   : {_fmt(started)}（{LOG_FILE.name} 创建时间）")
    print(f"  当前时间       : {_fmt(time.time())}")

    watched: list[Path] = sorted((BACKEND / "app").rglob("*.py"))
    env = BACKEND / ".env"
    if env.exists():
        watched.append(env)
    stale = [f for f in watched if f.stat().st_mtime > started + TOLERANCE]

    print(f"  后端受监视文件 : {len(watched)} 个（app/**/*.py + .env）")
    print(f"  启动后被修改   : {len(stale)} 个")
    if stale:
        for f in sorted(stale, key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
            print(f"    {_fmt(f.stat().st_mtime)}  {f.relative_to(ROOT)}")
        if len(stale) > 20:
            print(f"    ... 另有 {len(stale) - 20} 个")

    # 前端：源码是否比 dist 新（vite build 产物时间）
    dist = FRONTEND / "dist" / "index.html"
    src_files = [f for ext in ("*.vue", "*.ts") for f in (FRONTEND / "src").rglob(ext)]
    if dist.exists() and src_files:
        built = dist.stat().st_mtime
        newer_src = [f for f in src_files if f.stat().st_mtime > built + TOLERANCE]
        print(f"  前端 dist 构建 : {_fmt(built)}；源码晚于构建 {len(newer_src)} 个")
        for f in sorted(newer_src, key=lambda x: x.stat().st_mtime, reverse=True)[:8]:
            print(f"    {_fmt(f.stat().st_mtime)}  {f.relative_to(ROOT)}")
        fe_stale = bool(newer_src)
    else:
        print("  前端 dist      : 未找到，跳过")
        fe_stale = False

    probe = probe_running_service()
    if probe:
        print(f"  线上端点探测   : {probe}")

    print("-" * 68)
    if stale:
        print("  ⚠ 后端磁盘代码比运行中的服务新 → 信息不同步，需重启：")
        print("    远程触发：PowerShell 执行 Start-ScheduledTask -TaskName 'SASRestart'")
        print("    等价入口：双击 E:\\study\\sas\\scripts\\ops\\restart-backend.vbs（前后端一起重启）")
        print("    重启前：自动对账防护、无行情卖出兜底、卖出出口自检、锁死锁修复均未生效。")
    else:
        print("  ✅ 后端已加载当前磁盘代码。")
    if fe_stale:
        print("  ⚠ 前端源码比 dist 新 → 需 npm run build（或浏览器硬刷新确认）。")
    else:
        print("  ✅ 前端 dist 不旧于源码。")
    print("=" * 68)
    return 1 if (stale or fe_stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
