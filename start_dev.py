"""启动 SAS 前后端开发服务器（后台运行，验证后保持存活）。"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = str(ROOT / "backend")
FRONTEND = str(ROOT / "frontend")
PYTHON = str(Path(BACKEND) / ".venv" / "Scripts" / "python.exe")


def kill_port(port: int) -> None:
    """杀掉占用指定端口的进程（清理上次残留）。"""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            pid = parts[4]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            print(f"  清理残留进程 PID={pid} (端口 {port})")


def start() -> None:
    for port in (8000, 5173):
        kill_port(port)
    time.sleep(1)

    flags = subprocess.CREATE_NEW_PROCESS_GROUP

    # 后端：FastAPI (uvicorn)。--reload 监听 app/ 目录变化自动重启，
    # 开发期改 .py 保存即生效，无需手动重启（生产模式由 SAS 服务托管，
    # 不需要也不应使用 reload）。
    if not Path(PYTHON).exists():
        sys.exit(f"未找到虚拟环境解释器：{PYTHON}\n请先执行：cd backend && python -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt")

    p1 = subprocess.Popen(
        [PYTHON, "-m", "uvicorn",
         "app.main:app", "--host", "127.0.0.1", "--port", "8000",
         "--reload", "--reload-dir", "app"],
        cwd=BACKEND, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"后端启动中 (PID {p1}) → http://127.0.0.1:8000（代码改动自动重启）")

    # 前端：Vite dev server
    p2 = subprocess.Popen(
        ["cmd", "/c", "npx vite --port 5173"],
        cwd=FRONTEND, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"前端启动中 (PID {p2}) → http://localhost:5173")

    # 等待就绪（后端预热股票池约需 10 秒）
    time.sleep(14)

    def probe(url: str) -> str:
        try:
            r = urllib.request.urlopen(url, timeout=15)
            return f"OK ({r.status})"
        except Exception as exc:  # noqa: BLE001
            return f"FAIL: {exc}"

    print("\n===== 服务验证 =====")
    print("后端 health :", probe("http://127.0.0.1:8000/api/health"))
    print("策略列表    :", probe("http://127.0.0.1:8000/api/screening/strategies"))
    print("前端页面    :", probe("http://127.0.0.1:5173/"))
    print("\n后端 PID=%s  前端 PID=%s（后台运行中）" % (p1.pid, p2.pid))


if __name__ == "__main__":
    start()
