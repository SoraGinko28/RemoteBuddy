"""页面（`frontend/workspace.js`）在真浏览器里的行为：无头 Chrome + CDP。

只查一件事：上边栏那条错误提示、以及阶段那几个字，跟着会议状态走 —— **人在对话里选了一段
文字看的时候也照走**。以前整页只要有一个选区，`renderHeader` 就整段让路，于是会那边已经
又跑起来了，上边栏还挂着上一条错误提示（阶段、状态都停在出错那一刻）。

跑法：`python -m pytest tests/test_workspace_page_ui.py`（本机没有 Chrome 就跳过）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import signal
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
class _PageTestCase(unittest.TestCase):
    """起一个真服务、一个无头 Chrome、一条 CDP 连接；具体查什么由子类说。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = tempfile.mkdtemp(prefix="page-ui-chrome-")
        cls.debug_port = _free_port()
        cls.chrome = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                f"--remote-debugging-port={cls.debug_port}",
                f"--user-data-dir={cls.profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--remote-allow-origins=*",
                "--window-size=1280,900",
                "about:blank",
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.page = _Page(cls._target_url())
        cls.page.call("Page.enable")
        cls._previous_signal_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
        }
        for signum in cls._previous_signal_handlers:
            signal.signal(signum, cls._handle_interrupt)

    @classmethod
    def _handle_interrupt(cls, signum: int, _frame: Any) -> None:
        cls.tearDownClass()
        os._exit(128 + signum)

    @classmethod
    def tearDownClass(cls) -> None:
        page = getattr(cls, "page", None)
        if page is not None:
            page.close()
        chrome = getattr(cls, "chrome", None)
        if chrome is not None and chrome.poll() is None:
            try:
                os.killpg(chrome.pid, signal.SIGTERM)
                chrome.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(chrome.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        shutil.rmtree(getattr(cls, "profile", ""), ignore_errors=True)
        for signum, handler in getattr(cls, "_previous_signal_handlers", {}).items():
            try:
                signal.signal(signum, handler)
            except ValueError:
                pass

    def setUp(self) -> None:
        import web_v2
        from meeting_v2 import MeetingStore

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.test_project_root = Path(self.temporary.name) / "project"
        shutil.copytree(REPO_ROOT / "frontend", self.test_project_root / "frontend")
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

        self.service = web_v2.MeetingWebService(
            workspace=self.workspace, project_root=self.test_project_root
        )
        self.port = _free_port()
        self.server = web_v2.build_server("127.0.0.1", self.port, service=self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        def cleanup_tmux() -> None:
            try:
                self.service._transport().kill_session(self._store().meeting.session_name)
            except Exception:  # noqa: BLE001 - the meeting may already be deleted
                pass
        self.addCleanup(cleanup_tmux)

        self.page = type(self).page
        self.page.call("Page.navigate", url=f"http://127.0.0.1:{self.port}/")

    @classmethod
    def _target_url(cls) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{cls.debug_port}/json/list", timeout=5
                ) as response:
                    targets: List[Dict[str, Any]] = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page":
                        return str(target["webSocketDebuggerUrl"])
            except Exception:  # noqa: BLE001 - Chrome 还没起来
                pass
            time.sleep(0.3)
        self.fail("Chrome 没起来（拿不到调试端口）")
        raise AssertionError  # pragma: no cover - self.fail 已经抛了

    def _store(self):
        return self.service.load_store(self.meeting_id)


class HeaderNoticeTest(_PageTestCase):
    """上边栏的提示跟着会议状态走，哪怕页面上正有一个选区。"""

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

    def test_appending_events_does_not_leave_a_toast(self):
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30,
            what="页面画出这场会",
        )
        self.page.wait_for(
            "document.querySelector('#toast').textContent === ''", 10,
            what="追加事件后无错误提示",
        )

    def test_repeated_identical_toasts_eventually_hide(self):
        self.page.wait_for(
            "typeof globalThis.workspaceToast === 'function'",
            30,
            what="toast 函数加载完成",
        )
        result = self.page.js(
            """(async () => {
                const repeat = setInterval(() => window.workspaceToast('重复错误'), 700);
                window.workspaceToast('重复错误');
                await new Promise((resolve) => setTimeout(resolve, 4200));
                clearInterval(repeat);
                await new Promise((resolve) => setTimeout(resolve, 1200));
                return document.querySelector('#toast').hidden;
            })()"""
        )
        self.assertTrue(result)


class ComposerTest(_PageTestCase):
    """发不出去的时候，输入框里留什么。

    被明确拒绝（4xx）就把话还给人家 —— 一个字都没丢；服务端出错（5xx）可能是"话已经收下了
    才出错"，那就别塞回去，否则人会以为没发出去、再发一遍（真会里重复发了三条）。
    """

    def _send(self, status: int) -> None:
        """把下一次发消息的响应换掉：用 CDP 在页面里替一层 fetch。"""
        self.page.js(
            "(() => {"
            f"  const failing = () => Promise.resolve(new Response("
            f"    JSON.stringify({{error: 'server said {status}'}}),"
            f"    {{status: {status}, headers: {{'Content-Type': 'application/json'}}}}));"
            "  const original = window.fetch;"
            "  window.fetch = (input, init) => {"
            "    const url = typeof input === 'string' ? input : (input && input.url) || '';"
            "    if (String(url).includes('/messages')) { window.fetch = original; return failing(); }"
            "    return original(input, init);"
            "  };"
            "  return 'stubbed';"
            "})()"
        )
        self.page.js(
            "(() => {"
            f"  const box = document.querySelector('#message-input');"
            f"  box.value = '这一句得有个着落 {status}';"
            "  box.dispatchEvent(new Event('input', {bubbles: true}));"
            "  document.querySelector('#message-form').requestSubmit();"
            "  return 'sent';"
            "})()"
        )
        self.page.wait_for(
            "document.querySelector('#toast') && !document.querySelector('#toast').hidden",
            10, what="出错提示出现",
        )

    def _wait_for_composer(self) -> None:
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30, what="页面画出这场会"
        )
        self.page.wait_for(
            "!document.querySelector('#message-input').disabled", 10, what="输入框可以打字"
        )

    def test_a_refused_message_comes_back_to_the_box(self):
        self._wait_for_composer()
        self._send(400)
        self.assertEqual(
            "这一句得有个着落 400", self.page.js("document.querySelector('#message-input').value")
        )

    def test_a_server_error_does_not_put_the_message_back(self):
        self._wait_for_composer()
        self._send(503)
        self.assertEqual("", self.page.js("document.querySelector('#message-input').value"))


# 会话行上那颗「···」：先在行里找它，点开菜单，「重命名」才会出现。
OPEN_MEETING_MENU = """
(() => {
  if (document.querySelector('#meeting-menu') && !document.querySelector('#meeting-menu').hidden) return 'already-open';
  const row = document.querySelector('.meeting-row');
  if (!row) return 'no-row';
  const nodes = [...row.querySelectorAll('button, [role="button"]')];
  const trigger = nodes.find((n) =>
      /menu|more|更多|菜单/i.test((n.getAttribute('aria-label') || '') + n.className))
    || nodes.find((n) => /^[.\u00b7\u22ef\u2026]+$/.test((n.textContent || '').trim()))
    || nodes[nodes.length - 1];
  if (!trigger) return 'no-trigger';
  trigger.click();
  return 'clicked:' + (trigger.getAttribute('aria-label') || trigger.className || '');
})()
"""


class RenameDialogTest(_PageTestCase):
    """改名走页面里的 modal，不走浏览器原生 prompt（原生框跟暗色界面不是一套）。"""

    def test_the_rename_dialog_replaces_the_native_prompt(self):
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30, what="页面画出这场会"
        )
        # 原生 prompt 一旦被调用就炸：这条用例就是查"没走原生框"。
        self.page.js(
            "window.prompt = () => { throw new Error('native prompt was used'); }; 'stubbed'"
        )
        self.page.js(OPEN_MEETING_MENU)
        self.page.wait_for(
            "(() => { const menu = document.querySelector('#meeting-menu');"
            " return !!menu && !menu.hidden"
            " && !!menu.querySelector('[data-meeting-action=\"rename\"]'); })()",
            10, what="菜单里有「重命名」",
        )
        self.page.js(
            "document.querySelector('[data-meeting-action=\"rename\"]').click(); 'ok'"
        )
        self.page.wait_for(
            "document.querySelector('#rename-dialog')?.open === true", 10, what="弹框打开"
        )
        self.assertEqual(
            "重命名会话",
            self.page.js("document.querySelector('#rename-dialog h2').textContent.trim()"),
        )
        # 不预填：没改过名时那一行的 label 就是首消息全文，预填等于把整段请求塞进框里。
        self.assertEqual("", self.page.js("document.querySelector('#rename-input').value"))
        self.assertTrue(
            self.page.js(
                "document.activeElement && document.activeElement.id === 'rename-input'"
            ),
            "光标该落在输入框里",
        )
        self.page.js(
            "(() => { const box = document.querySelector('#rename-input');"
            " box.value = '改过的会话名';"
            " box.dispatchEvent(new Event('input', {bubbles: true}));"
            " document.querySelector('#rename-submit').click(); return 'submitted'; })()"
        )
        self.page.wait_for(
            "document.querySelector('#rename-dialog')?.open !== true", 10, what="提交后弹框关掉"
        )
        self.page.wait_for(
            "document.body.innerText.includes('改过的会话名')", 10, what="列表里出现新名字"
        )
        self.assertEqual("改过的会话名", self._store().meeting.name)


class DeleteDialogTest(_PageTestCase):
    """删会话也走页面里的 modal，并且点下去真的删掉。"""

    def _open_delete_dialog(self) -> None:
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30, what="页面画出这场会"
        )
        self.page.js(OPEN_MEETING_MENU)
        self.page.wait_for(
            "(() => { const menu = document.querySelector('#meeting-menu');"
            " return !!menu && !menu.hidden"
            " && !!menu.querySelector('[data-meeting-action=\"delete\"]'); })()",
            10, what="菜单里有「删除会话」",
        )
        self.page.js(
            "document.querySelector('[data-meeting-action=\"delete\"]').click(); 'ok'"
        )
        self.page.wait_for(
            "document.querySelector('#delete-dialog')?.open === true", 10, what="删除确认框打开"
        )

    def test_the_delete_dialog_cancels_without_deleting(self):
        self.page.js(
            "window.confirm = () => { throw new Error('native confirm was used'); }; 'stubbed'"
        )
        self._open_delete_dialog()
        self.assertIn(
            FIRST_MESSAGE,
            self.page.js("document.querySelector('#delete-message').textContent"),
        )
        # 框里就一个中文标题：不要再顶一行英文小字（"DELETE SESSION" 那种）。
        self.assertEqual(
            "删除会话",
            self.page.js("document.querySelector('#delete-dialog h2').textContent.trim()"),
        )
        self.assertIsNone(self.page.js("document.querySelector('#delete-dialog .pane-kicker')"))
        self.page.js("document.querySelector('#cancel-delete').click(); 'ok'")
        self.page.wait_for(
            "document.querySelector('#delete-dialog')?.open !== true", 10, what="取消后关掉"
        )
        self.assertIsNotNone(self._store().meeting.meeting_id)

    def test_confirming_the_dialog_deletes_the_meeting(self):
        self.page.js(
            "window.confirm = () => { throw new Error('native confirm was used'); }; 'stubbed'"
        )
        self._open_delete_dialog()
        self.page.js("document.querySelector('#confirm-delete').click(); 'ok'")
        self.page.wait_for(
            "document.querySelector('#delete-dialog')?.open !== true", 10, what="删除后关掉"
        )
        self.page.wait_for(
            f"!document.body.innerText.includes({FIRST_MESSAGE!r})", 10, what="列表里那条没了"
        )
        self.assertEqual(
            [], [m for m in self.service.list_meetings()["meetings"]
                 if m["meeting_id"] == self.meeting_id]
        )


class AssetReloadTest(_PageTestCase):
    """页面已打开时，磁盘上的 CSS 变化会自动落到现有 DOM。"""

    def test_open_page_applies_css_changed_on_disk_without_reload(self):
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 30,
            what="页面画出这场会",
        )
        baseline = self.page.js(
            "getComputedStyle(document.querySelector('#chat-list')).marginBottom"
        )
        repository_css = REPO_ROOT / "frontend" / "workspace.css"
        repository_before = repository_css.read_bytes()
        css_path = self.service.project_root / "frontend" / "workspace.css"
        original = css_path.read_text(encoding="utf-8")
        self.addCleanup(css_path.write_text, original, encoding="utf-8")
        css_path.write_text(
            original + "\n#chat-list { margin-bottom: 13.5px !important; }\n",
            encoding="utf-8",
        )
        self.page.wait_for(
            "getComputedStyle(document.querySelector('#chat-list')).marginBottom === '13.5px'",
            40,
            what="页面自动换上磁盘里的新 CSS",
        )
        self.assertNotEqual(baseline, "13.5px")
        self.assertEqual(repository_before, repository_css.read_bytes())


class OrderOfImagesAndQuotesTest(_PageTestCase):
    """引用和图片放在文字前面 —— 聊天区和输入框都是。"""

    def _first_event_id(self) -> str:
        store = self._store()
        return next(
            event["event_id"] for event in store.events()
            if event.get("kind") == "chat" and event.get("actor") == "human"
        )

    def test_the_composer_puts_the_strips_above_the_text(self):
        self.page.wait_for("!!document.querySelector('#message-input')", 30)
        for selector in ("#attachment-strip", "#reference-card"):
            before = self.page.js(
                "(() => {"
                f" const strip = document.querySelector('{selector}');"
                " const box = document.querySelector('#message-input');"
                " if (!strip || !box) return null;"
                " const order = strip.compareDocumentPosition(box);"
                " return Boolean(order & Node.DOCUMENT_POSITION_FOLLOWING);"
                "})()"
            )
            self.assertTrue(before, f"{selector} 该在输入框前面")

    def test_a_message_shows_its_quote_and_image_before_the_text(self):
        import base64

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        attachment = self.service.save_attachment(self.meeting_id, "image/png", png)
        source = self._first_event_id()
        # 直接往 journal 里写这一条：走 message() 会把 Agent 叫起来，而这场会没有可用的模型配置，
        # 一叫就进 api_error、输入框被禁用，后面那条输入框的用例就量不到东西了。
        self._store().append_chat(
            "human",
            "带引用和图片的一条",
            "request",
            references=[
                {"kind": "chat", "event_id": source, "region": {"start": 0, "end": 4},
                 "excerpt": "带引用和图片的一条"}
            ],
            attachments=[{"name": attachment["name"]}],
        )
        self._store().append_chat(
            "human",
            "",
            "request",
            attachments=[{"name": attachment["name"]}],
        )
        self.page.wait_for(
            "document.body.innerText.includes('带引用和图片的一条')", 20, what="这条消息画出来"
        )
        self.page.wait_for(
            "document.querySelectorAll('#chat-list article .message-images').length >= 2",
            20,
            what="两条图片消息画出来",
        )
        order = self.page.js("""
(() => {
  const article = [...document.querySelectorAll('#chat-list article')]
    .find((one) => one.innerText.includes('带引用和图片的一条'));
  if (!article) return null;
  const text = article.querySelector('.message-content');
  const quote = article.querySelector('.message-reference');
  const images = article.querySelector('.message-images');
  if (!text || !quote || !images) return null;
  const before = (node) => Boolean(node.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING);
  const rect = (node) => node.getBoundingClientRect();
  const gap = (upper, lower) => rect(lower).top - rect(upper).bottom;
  const head = article.querySelector('.message-head');
  const image = images.querySelector('.message-image');
  const imageOnly = [...document.querySelectorAll('#chat-list article .message-images')]
    .map((one) => one.closest('article'))
    .find((one) => one && one !== article && !(one.querySelector('.message-content')?.textContent || '').trim());
  const imageOnlyHead = imageOnly?.querySelector('.message-head');
  const imageOnlyImages = imageOnly?.querySelector('.message-images');
  if (!head || !image || !imageOnlyHead || !imageOnlyImages) return null;
  return {
    quote: before(quote),
    images: before(images),
    quoteFits: quote.scrollWidth <= quote.clientWidth,
    articleFits: article.scrollWidth <= article.clientWidth,
    nameToQuote: gap(head, quote),
    quoteToImage: gap(quote, images),
    imageToText: gap(images, text),
    imageOnlyNameToImage: gap(imageOnlyHead, imageOnlyImages),
  };
})()
""")
        self.assertIsNotNone(order, "这条消息该带引用、图片和正文")
        self.assertTrue(order["quote"], "引用该在正文前面")
        self.assertTrue(order["quoteFits"], "引用不得横向溢出")
        self.assertTrue(order["articleFits"], "消息不得被引用撑宽")
        self.assertTrue(order["images"], "图片该在正文前面")
        self.assertEqual(8, order["nameToQuote"], "名字到引用应为 8px")
        self.assertEqual(8, order["quoteToImage"], "引用到图片应为 8px")
        self.assertEqual(8, order["imageToText"], "图片到正文应为 8px")
        self.assertEqual(8, order["imageOnlyNameToImage"], "只有图片时名字到图片应为 8px")

    def test_completion_proposals_show_whether_they_have_evidence(self):
        self.page.wait_for(
            f"document.body.innerText.includes({FIRST_MESSAGE!r})", 20,
            what="页面画出这场会",
        )
        store = self._store()
        agent = next(iter(store.meeting.agent_names()))
        store.append_chat(agent, "裸完成提议", "final")
        store.append_event(
            "state", "harness", "completion_proposed",
            data={"reason": "agent_done", "evidence": False, "evidence_type": None, "reference_count": 0},
        )
        store.append_chat(
            agent, "有证据完成提议", "final",
            references=[{"kind": "screen", "seq": 1}],
        )
        store.append_event(
            "state", "harness", "completion_proposed",
            data={"reason": "agent_done", "evidence": True, "evidence_type": "command", "reference_count": 1},
        )
        store.update(phase="completion_proposed", control_owner="none")

        self.page.wait_for(
            "document.body.innerText.includes('裸完成提议') && document.body.innerText.includes('有证据完成提议')",
            20,
            what="两条完成提议画出来",
        )
        badges = self.page.js("""
(() => [...document.querySelectorAll('.completion-evidence, .completion-no-evidence')]
  .map((node) => ({className: node.className, text: node.textContent})))()
""")
        self.assertEqual(
            [
                {"className": "completion-no-evidence", "text": "完成提议 · 无证据"},
                {"className": "completion-evidence", "text": "完成提议 · 有证据"},
            ],
            badges[-2:],
        )
        self.assertEqual("completion_proposed", store.meeting.phase)

    def test_chat_never_overflows_horizontally_for_adversarial_content(self):
        self.page.call("Emulation.setDeviceMetricsOverride", width=420, height=900, deviceScaleFactor=1, mobile=False)
        self.page.js("""
(() => {
  const list = document.querySelector('#chat-list');
  const make = (text, quote, imageWidth) => {
    const article = document.createElement('article');
    article.className = 'message human';
    article.innerHTML = `<div class="message-head"><strong>human</strong><time>now</time></div>
      <div class="message-reference">${quote}</div>
      <div class="message-images"><img class="message-image" alt="wide" src="data:image/svg+xml,
        <svg xmlns='http://www.w3.org/2000/svg' width='${imageWidth}' height='40'></svg>"></div>
      <p class="message-content">${text}</p>`;
    list.append(article);
  };
  make('X'.repeat(300), 'reference-' + 'R'.repeat(600), 1600);
  make('正文-' + 'Y'.repeat(800), 'quote-' + 'Q'.repeat(500), 1400);
  return 'injected';
})()
""")
        self.page.wait_for("document.querySelectorAll('#chat-list article').length >= 2", 5)
        measurements = self.page.js("""
(() => {
  const list = document.querySelector('#chat-list');
  const nodes = [list, ...list.querySelectorAll('*')];
  return {
    viewport: window.innerWidth,
    client: list.clientWidth,
    scroll: list.scrollWidth,
    overflowing: nodes.filter((node) => node.scrollWidth > node.clientWidth).map((node) => ({
      tag: node.tagName, className: node.className, client: node.clientWidth, scroll: node.scrollWidth,
    })),
  };
})()
""")
        self.assertLessEqual(measurements["scroll"], measurements["client"], measurements)
        self.assertEqual([], measurements["overflowing"], measurements)


class TerminalTakeoverTest(_PageTestCase):
    """真 tmux pane 的长行不能在真浏览器的终端里静默消失。"""

    def test_opening_page_alone_reveals_populated_terminal_without_resize(self):
        started = time.monotonic()
        self.page.wait_for(
            """(() => {
                const viewport = document.querySelector('#terminal-screen');
                const terminal = viewport?.querySelector('.xterm');
                const rows = viewport?.querySelector('.xterm-rows');
                return viewport?.dataset.geometryReady === 'true'
                    && terminal
                    && getComputedStyle(terminal).visibility === 'visible'
                    && rows
                    && rows.textContent.trim().length > 0;
            })()""",
            3,
            what="只打开页面后终端在三秒内可见且有内容",
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self.assertLessEqual(elapsed_ms, 3000)

    def test_long_tmux_line_keeps_every_character_and_tail_after_layout_changes(self):
        store = self._store()
        store.update(phase="agent_thinking")
        pane_id = self.service._transport().create_workspace_session(
            store.meeting.session_name, store.workspace_path
        )
        store.set_session(pane_id)
        self.service.acquire(self.meeting_id)

        body = "A" * 140 + "，" + "B" * 140 + "、TAIL-9f3c"
        command = f"printf '%s\\n' '{body}'"
        self.service.terminal_input(
            self.meeting_id,
            {"bytes": (command + "\r").encode("utf-8").hex()},
        )

        self.page.wait_for(
            "document.querySelector('#terminal-screen .xterm') && window.__takeoverReady === true",
            20,
            what="页面接上真 tmux takeover",
        )
        self.page.wait_for(
            "document.querySelector('#terminal-screen').innerText.includes('TAIL-9f3c')",
            20,
            what="页面看得到长行尾巴",
        )
        self._assert_long_line(body)

        # Resize 对准属于观看态；先结束 takeover，避免流式终端与观看态对准互相抢尺寸。
        self.page.js("stopTerminalTakeover(false); 'released'")
        self.page.wait_for("window.__takeoverReady === false", 10, what="退出 takeover 回到观看态")
        self.page.call(
            "Emulation.setDeviceMetricsOverride",
            width=420,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        self.page.wait_for(
            """(() => {
                const viewport = document.querySelector('#terminal-screen');
                const terminal = viewport?.querySelector('.xterm');
                if (!viewport || !terminal) return false;
                const rows = viewport.querySelector('.xterm-rows');
                return rows && terminal.scrollWidth <= terminal.clientWidth + 1;
            })()""",
            20,
            what="resize 应答后立即按 pane 尺寸重画",
        )
        immediate = self.page.js(
            """(() => {
                const viewport = document.querySelector('#terminal-screen');
                const terminal = viewport.querySelector('.xterm');
                const rows = viewport.querySelector('.xterm-rows');
                return {
                    paneWidth: terminal?.clientWidth || 0,
                    renderedWidth: rows?.getBoundingClientRect().width || 0,
                    scrollWidth: terminal?.scrollWidth || 0,
                    clientWidth: terminal?.clientWidth || 0,
                };
            })()"""
        )
        self.assertLessEqual(immediate["scrollWidth"], immediate["clientWidth"] + 1, immediate)
        self.page.js("document.querySelector('.agent-state')?.click(); 'opened'")
        self.page.js("document.querySelector('#member-panel-close')?.click(); 'closed'")
        self.page.call(
            "Emulation.setDeviceMetricsOverride",
            width=1280,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        self.page.wait_for(
            "document.querySelector('#terminal-screen').innerText.includes('TAIL-9f3c')",
            20,
            what="resize 和成员面板开合后尾巴仍在",
        )
        self._assert_long_line(body)

    def _assert_long_line(self, body: str) -> None:
        result = self.page.js(
            """(() => {
                const viewport = document.querySelector('#terminal-screen');
                const terminal = viewport.querySelector('.xterm');
                const rows = [...viewport.querySelectorAll('.xterm-rows > div')];
                const tail = rows.find((row) => row.innerText.includes('TAIL-9f3c'));
                const textRight = (row) => {
                    const kids = row.children;
                    const last = kids[kids.length - 1];
                    return (last || row).getBoundingClientRect().right;
                };
                const contentRight = rows.reduce((right, row) =>
                    Math.max(right, textRight(row)), 0);
                const tailRect = tail?.getBoundingClientRect();
                const box = viewport.getBoundingClientRect();
                const overflowX = terminal ? getComputedStyle(terminal).overflowX : '';
                const tailRight = tailRect?.right ?? 0;
                const boxRight = box.right;
                const contentVisibleWithoutScroll = contentRight <= boxRight + 1;
                const maxScrollLeft = terminal
                    ? terminal.scrollWidth - terminal.clientWidth : 0;
                const previousScrollLeft = terminal?.scrollLeft || 0;
                if (terminal && maxScrollLeft > 0) terminal.scrollLeft = maxScrollLeft;
                const scrolledContentRight = rows.reduce((right, row) =>
                    Math.max(right, textRight(row)), 0);
                const contentVisibleAfterScroll = scrolledContentRight <= boxRight + 1;
                if (terminal) terminal.scrollLeft = previousScrollLeft;
                return {
                    viewportClient: viewport.clientWidth,
                    viewportScroll: viewport.scrollWidth,
                    xtermClient: terminal?.clientWidth || 0,
                    xtermScroll: terminal?.scrollWidth || 0,
                    overflowX,
                    maxScrollLeft,
                    contentRight,
                    scrolledContentRight,
                    tailRight,
                    boxRight,
                    contentVisibleWithoutScroll,
                    contentVisibleAfterScroll,
                    tailText: tail?.innerText || '',
                };
            })()"""
        )
        self.assertIn("TAIL-9f3c", result["tailText"])
        self.assertNotIn(result["overflowX"], {"auto", "scroll"}, result)
        self.assertLessEqual(
            result["xtermScroll"] - result["xtermClient"],
            7.225,
            result,
        )
        self.assertTrue(result["contentVisibleWithoutScroll"], result)
        self.assertTrue(result["tailRight"] <= result["boxRight"] + 1, result)


if __name__ == "__main__":
    unittest.main()
