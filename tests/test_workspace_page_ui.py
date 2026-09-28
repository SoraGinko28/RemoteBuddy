"""页面（`frontend/workspace.js`）在真浏览器里的行为：无头 Chrome + CDP。

只查一件事：上边栏那条错误提示、以及阶段那几个字，跟着会议状态走 —— **人在对话里选了一段
文字看的时候也照走**。以前整页只要有一个选区，`renderHeader` 就整段让路，于是会那边已经
又跑起来了，上边栏还挂着上一条错误提示（阶段、状态都停在出错那一刻）。

跑法：`python -m pytest tests/test_workspace_page_ui.py`（本机没有 Chrome 就跳过）。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FAILURE = "Claude Code turn timed out"
FIRST_MESSAGE = "第一条消息"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Page:
    """一条 CDP 连接，够用就好：Runtime.evaluate。"""

    def __init__(self, ws_url: str) -> None:
        import websocket

        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.seq = 0

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.seq:
                if "error" in message:
                    raise AssertionError(f"{method}: {message['error']}")
                return message.get("result", {})

    def js(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if result.get("exceptionDetails"):
            raise AssertionError(json.dumps(result["exceptionDetails"], ensure_ascii=False)[:400])
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, seconds: float = 10.0, what: str = "") -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.js(expression):
                return
            time.sleep(0.2)
        raise AssertionError(f"等不到：{what or expression}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - 收尾不该再抛
            pass


@unittest.skipUnless(Path(CHROME).exists(), "需要本机的 Google Chrome")
class HeaderNoticeTest(unittest.TestCase):
    """上边栏的提示跟着会议状态走，哪怕页面上正有一个选区。"""

    def setUp(self) -> None:
        import web_v2
        from meeting_v2 import MeetingStore

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.meeting_id = "page-notice-0001"
        store = MeetingStore.create(
            self.workspace,
            self.meeting_id,
            FIRST_MESSAGE,
            REPO_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md",
            f"remotebuddy-{self.meeting_id}",
            "test-model",
            workspace=self.workspace,
        )
        # 停在"等人说话"：这一条不查终端，就别让页面去把 tmux 会话开出来。
        store.update(phase="waiting_for_user")

        self.service = web_v2.MeetingWebService(workspace=self.workspace, project_root=REPO_ROOT)
        self.port = _free_port()
        self.server = web_v2.build_server("127.0.0.1", self.port, service=self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.profile = tempfile.mkdtemp(prefix="page-ui-chrome-")
        self.addCleanup(shutil.rmtree, self.profile, ignore_errors=True)
        self.debug_port = _free_port()
        self.chrome = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                f"--remote-debugging-port={self.debug_port}",
                f"--user-data-dir={self.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--remote-allow-origins=*",
                "--window-size=1280,900",
                f"http://127.0.0.1:{self.port}/",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._kill_chrome)
        self.page = _Page(self._target_url())
        self.addCleanup(self.page.close)
        self.page.call("Page.enable")

    def _kill_chrome(self) -> None:
        self.chrome.terminate()
        try:
            self.chrome.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.chrome.kill()

    def _target_url(self) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.debug_port}/json/list", timeout=5
                ) as response:
                    targets: List[Dict[str, Any]] = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page" and str(target.get("url", "")).startswith(
                        f"http://127.0.0.1:{self.port}"
                    ):
                        return str(target["webSocketDebuggerUrl"])
            except Exception:  # noqa: BLE001 - Chrome 还没起来
                pass
            time.sleep(0.3)
        self.fail("Chrome 没起来（拿不到调试端口）")
        raise AssertionError  # pragma: no cover - self.fail 已经抛了

    def _store(self):
        return self.service.load_store(self.meeting_id)

    def test_the_notice_follows_the_state_while_a_selection_is_alive(self):
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30,
            what="页面画出这场会",
        )
        self._store().update(phase="api_error", failure=FAILURE)
        self.page.wait_for(
            "!document.querySelector('#failure-message').hidden", 10, what="错误提示出现"
        )
        self.assertIn(
            FAILURE, self.page.js("document.querySelector('#failure-message').textContent")
        )

        # 人在对话里选一段文字看，选区一直留着（页面为了不把选区弄没，会本 tick 放弃重绘）。
        self.page.js(
            "(() => { const node = document.querySelector('#chat-list');"
            " window.getSelection().selectAllChildren(node); return 'selected'; })()"
        )
        self.assertFalse(self.page.js("window.getSelection().isCollapsed"))

        # 会又开始干活了：提示和阶段都得跟着变，不许停在出错那一刻。
        self._store().update(phase="agent_thinking", failure=None)
        self.page.wait_for(
            "document.querySelector('#failure-message').hidden === true", 10,
            what="选区还在时错误提示消失",
        )
        self.page.wait_for(
            "document.querySelector('#phase-label').textContent.includes('思考')", 10,
            what="选区还在时阶段跟着变",
        )


if __name__ == "__main__":
    unittest.main()
