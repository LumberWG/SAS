# SAS frontend server: serve frontend/dist statically + reverse-proxy /api -> backend (8000)
# This lets the public cpolar tunnel (RayHive -> :8080) serve the SPA AND reach the
# backend through the same origin, so the browser's relative baseURL '/api' just works.
# Run: python serve_frontend.py [port]   (default 8080)
import http.server
import socketserver
import urllib.request
import urllib.error
import mimetypes
import os
import sys

DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")
BACKEND = "http://127.0.0.1:8000"


class Handler(http.server.BaseHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        url = BACKEND + self.path
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            # 回测/全市场扫描等长任务可能跑几分钟；超时给足，
            # 避免反代先把连接断掉（前端拿到的是 502 而不是结果）。
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", len(data))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header(
                "Content-Type", e.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # noqa: BLE001
            self.send_error(502, str(e))

    def _static(self):
        path = self.path.split("?")[0]
        if path == "/":
            fpath = os.path.join(DIST, "index.html")
        else:
            fpath = os.path.normpath(os.path.join(DIST, path.lstrip("/")))
            if not os.path.isfile(fpath) and path.startswith("/assets/"):
                # 已删除的旧 hash 资源必须 404：若回落到 index.html（200 + HTML），
                # 浏览器动态 import 会解析失败，页面表现为“点了没反应/白屏”。
                self.send_error(404)
                return
            if not os.path.isfile(fpath):
                # SPA history fallback（仅用于页面路由，不用于静态资源）
                fpath = os.path.join(DIST, "index.html")
        if not os.path.isfile(fpath):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        with open(fpath, "rb") as f:
            data = f.read()
        # index.html 不缓存，避免重新构建后浏览器仍用旧入口；
        # 带内容 hash 的静态资源则长期缓存。
        if os.path.basename(fpath).lower() == "index.html":
            cache = "no-cache, no-store, must-revalidate"
        else:
            cache = "public, max-age=31536000, immutable"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api"):
            self._proxy()
        else:
            self._static()

    def do_POST(self):
        if self.path.startswith("/api"):
            self._proxy()
        else:
            self._static()

    def do_PUT(self):
        if self.path.startswith("/api"):
            self._proxy()
        else:
            self._static()

    def do_DELETE(self):
        if self.path.startswith("/api"):
            self._proxy()
        else:
            self._static()

    def log_message(self, *args):
        pass


def _parse_port(default=8080):
    # nssm may pass "serve_frontend.py 8080" as one arg or split them;
    # just pick the first numeric token, fall back to default.
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return default


if __name__ == "__main__":
    port = _parse_port(8080)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as httpd:
        print(f"SAS frontend: serving {DIST} + /api -> {BACKEND} on :{port}")
        httpd.serve_forever()
