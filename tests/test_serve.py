"""启动层（serve.py）的回归测试。

这里锁住的都是「不报错但行为错」的坑 —— 单测比人工点一遍可靠。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from serve import DemoHandler, DemoServer, _pick_port  # noqa: E402


class TestHandlerWiring(unittest.TestCase):
    """BaseHTTPRequestHandler 靠 do_<METHOD> 分发；方法挂错类不会报错，
    只会让每个请求都回 501 Unsupported method。"""

    def test_http_verbs_live_on_the_handler(self):
        for method in ("do_GET", "do_HEAD", "do_POST", "do_OPTIONS"):
            self.assertTrue(hasattr(DemoHandler, method), method)
            # 必须由 DemoHandler 自己（或它的基类）提供，而不是被挪到 DemoServer 上
            self.assertIn(method, DemoHandler.__dict__, f"{method} 不在 DemoHandler 上")

    def test_internals_live_on_the_handler(self):
        for name in ("_handle", "_serve_static", "_json", "_send", "_read_body"):
            self.assertIn(name, DemoHandler.__dict__, name)

    def test_server_class_does_not_swallow_handler_methods(self):
        """DemoServer 若被写在 DemoHandler 中间，后面的方法会全并进来。"""
        for name in ("do_GET", "_handle", "_serve_static"):
            self.assertNotIn(name, DemoServer.__dict__, f"{name} 被并进了 DemoServer")


class TestPortPicking(unittest.TestCase):
    def test_free_port_is_returned_as_is(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        # probe 已关闭，端口应可用
        self.assertEqual(_pick_port("127.0.0.1", free), free)

    def test_occupied_port_is_skipped(self):
        """关键回归：探针不能设 SO_REUSEADDR。

        Windows 上带 SO_REUSEADDR 的探针可以绑定一个**已有进程在 LISTEN** 的端口，
        于是 _pick_port 永远返回原端口，HTTPServer 也照样绑定成功 ——
        同一个端口上挂两个服务，请求被谁接到看运气，还会静默返回旧数据。
        """
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken = holder.getsockname()[1]
        try:
            picked = _pick_port("127.0.0.1", taken)
            self.assertNotEqual(picked, taken,
                                "被占用的端口没有被跳过 —— 探针很可能又带上了 SO_REUSEADDR")
            self.assertGreater(picked, taken)
        finally:
            holder.close()

    def test_no_reuse_address_on_windows(self):
        """Windows 上必须关掉 allow_reuse_address，否则端口冲突会静默变成抢占。"""
        if os.name == "nt":
            self.assertFalse(DemoServer.allow_reuse_address)
        else:
            self.assertTrue(DemoServer.allow_reuse_address)


class TestStaticServingSafety(unittest.TestCase):
    """真的起一个服务器打请求，而不是 grep 源码 —— grep 只能证明字符串还在，
    证明不了行为。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = DemoServer(("127.0.0.1", 0), DemoHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_root_serves_the_frontend(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"babycare", body)

    def test_api_route_works(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_route_is_404_not_500(self):
        self.assertEqual(self._get("/nope.html")[0], 404)

    def test_directory_traversal_is_rejected(self):
        """http.client 不会规范化 '..'，所以这确实是在考服务器的防护。"""
        for path in ("/../serve.py", "/../../etc/passwd",
                     "/assets/../../serve.py", "/%2e%2e/serve.py"):
            status, body = self._get(path)
            self.assertIn(status, (400, 404), f"{path} 返回了 {status}")
            self.assertNotIn(b"DemoHandler", body, f"{path} 泄露了源码")


if __name__ == "__main__":
    unittest.main(verbosity=2)
