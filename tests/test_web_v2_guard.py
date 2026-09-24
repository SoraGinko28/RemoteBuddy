import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from web_v2 import MeetingWebService, build_server


class WebV2GuardTest(unittest.TestCase):
    """The loopback service must reject rebound hosts and foreign origins."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        model_api = root / "model_api"
        model_api.mkdir()
        (model_api / "config.toml").write_text(
            'model="gpt-5.6-luna"\n'
            'base_url="https://api.openai.com/v1"\n'
            'request_timeout_seconds=60\n'
            'reasoning_effort="low"\n',
            encoding="utf-8",
        )
        (model_api / "auth.json").write_text('{"OPENAI_API_KEY":"secret"}\n', encoding="utf-8")
        self.service = MeetingWebService(
            project_root=root,
            runner_factory=lambda *args, **kwargs: None,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.server = build_server("127.0.0.1", self.port, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path, headers=None, method="GET", body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_loopback_host_and_origin_are_accepted(self):
        status, _payload = self.request("/api/health")
        self.assertEqual(200, status)
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", "localhost"):
            status, _payload = self.request("/api/health", headers={"Host": host})
            self.assertEqual(200, status, host)
        status, _payload = self.request(
            "/api/health", headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(200, status)

    def test_rebound_host_header_is_rejected(self):
        for host in (f"evil.test:{self.port}", "evil.test", f"127.0.0.1.evil.test:{self.port}"):
            status, payload = self.request("/api/health", headers={"Host": host})
            self.assertEqual(403, status, host)
            self.assertIn("host", payload["error"])

    def test_foreign_origin_is_rejected_even_with_loopback_host(self):
        for origin in ("https://evil.test", "http://evil.test", "null"):
            status, payload = self.request("/api/health", headers={"Origin": origin})
            self.assertEqual(403, status, origin)
            self.assertIn("cross-origin", payload["error"])

    def test_guard_runs_before_csrf_on_posts(self):
        status, _payload = self.request(
            "/api/meetings",
            method="POST",
            headers={"Host": "evil.test", "Content-Type": "application/json"},
            body="{}",
        )
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
