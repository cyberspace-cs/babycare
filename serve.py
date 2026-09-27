#!/usr/bin/env python3
"""babycare · 一条命令启动演示。

    python serve.py

启动后浏览器打开 http://127.0.0.1:8115/ 即可看到「会吃」六屏演示，
页面上的营养数据由本地 API 实时计算（不再是写死的假数字）。

两种运行模式，自动选择、接口完全一致：
  · FastAPI 模式 —— 装了 fastapi + uvicorn 时优先使用
  · 标准库模式 —— 没装任何第三方包时自动退回（只用 Python 自带的 http.server）

也就是说：**不装任何依赖也能跑通全部前后端功能。**
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from server import api  # noqa: E402

DEFAULT_PORT = 8115
MIME_OVERRIDE = {".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8",
                 ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".ico": "image/x-icon"}


# ------------------------------------------------------------ 标准库模式

class DemoHandler(BaseHTTPRequestHandler):
    server_version = "babycare-demo"

    # BaseHTTPRequestHandler 默认 HTTP/1.0，意味着**每个请求都要新建一条 TCP 连接**。
    # 前端一次打开就要取 eat.html + 11 张素材 + 1 次 API，全走新连接。
    # 改成 1.1 后 nginx 到上游可以复用连接。
    # 前提是每条响应都带准确的 Content-Length —— _send() 已经保证了这点；
    # 若哪天加了流式/分块响应，这里必须同步处理，否则连接会挂住。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点，只报错误
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    # ---- 公共 ----
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _serve_static(self, path: str):
        rel = "eat.html" if path in ("/", "/eat.html") else path.lstrip("/")
        target = (ROOT / rel).resolve()
        # 防目录穿越
        if not str(target).startswith(str(ROOT)) or not target.is_file():
            self._json(404, {"error": "not found", "path": path})
            return
        ctype = MIME_OVERRIDE.get(target.suffix.lower()) or \
            mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # ---- 路由 ----
    def _handle(self, method: str):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            status, payload = api.dispatch(method, parsed.path,
                                           api.parse_query(parsed.query),
                                           self._read_body() if method == "POST" else None)
            self._json(status, payload)
            return
        if method == "OPTIONS":
            self._send(204, b"", "text/plain")
            return
        self._serve_static(parsed.path)

    def do_GET(self): self._handle("GET")
    def do_HEAD(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")


class DemoServer(ThreadingHTTPServer):
    """注意：这个类必须定义在 DemoHandler **之后**。

    插在 DemoHandler 中间会把后面的方法（do_GET 等）全都并进这个类，
    结果是 DemoHandler 里没有 do_GET → 每个请求都回 501 Unsupported method ('GET')，
    而且不报任何导入错误。
    """
    # Windows 上 SO_REUSEADDR 允许「抢占式」绑定已在监听的端口，会让端口冲突静默变成
    # 「两个服务抢一个端口」。宁可这里直接报 EADDRINUSE，也不要静默拿到旧数据。
    # Linux/macOS 上保留它，否则 Ctrl+C 重启会撞 TIME_WAIT。
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def run_stdlib(host: str, port: int):
    httpd = DemoServer((host, port), DemoHandler)
    return httpd


# --------------------------------------------------------------- 启动

def _pick_port(host: str, port: int) -> int:
    """端口被占就往后顺延，最多试 20 个。

    这里**故意不设 SO_REUSEADDR**。在 Windows 上 SO_REUSEADDR 的语义和 Linux 不同：
    它允许绑定一个已经有进程在 LISTEN 的端口。探针一旦带上它，无论端口是否被占都会
    绑定成功，_pick_port 就永远返回原端口，接着 HTTPServer（allow_reuse_address=1）
    也会绑定成功 —— 结果同一个端口上挂了两个服务，请求被谁接到全看运气，
    你会拿到上一个进程的**旧数据**，而且没有任何报错。
    不加 SO_REUSEADDR 时，Windows 和 Linux 都会老老实实抛 EADDRINUSE。
    """
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"端口 {port}-{port + 19} 都被占用了，请用 --port 指定其他端口")


def main():
    parser = argparse.ArgumentParser(description="babycare 用餐分析演示")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args()

    port = _pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    # 先判断依赖是否齐全，再决定用哪个模式启动，避免「导入成功但启动失败」时静默退回
    use_fastapi = False
    if os.getenv("BABYCARE_FORCE_STDLIB") != "1":
        try:
            import uvicorn
            from server.app import app
            use_fastapi = True
        except ImportError:
            use_fastapi = False

    if use_fastapi:
        # flush=True：stdout 被重定向到文件/管道时是块缓冲的，
        # 不 flush 的话启动信息要等缓冲区满才出现，看起来像「卡住了」。
        print(f"\n  babycare · 用餐分析演示\n"
              f"  运行模式：FastAPI + uvicorn\n"
              f"  前端演示：{url}\n"
              f"  接口文档：{url}docs\n"
              f"  API 探活：{url}api/health\n"
              f"\n  按 Ctrl+C 停止\n", flush=True)
        if not args.no_browser:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
        return

    httpd = run_stdlib(args.host, port)
    print(f"\n  babycare · 用餐分析演示\n"
          f"  运行模式：Python 标准库（未检测到 FastAPI，功能完全一致）\n"
          f"  前端演示：{url}\n"
          f"  API 探活：{url}api/health\n"
          f"  场景列表：{url}api/meal/scenarios\n"
          f"\n  按 Ctrl+C 停止\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
