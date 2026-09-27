import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_protocol import AgentProtocolError
from codex_app_server_client import CodexAppServerBackend
from model_api_client import ModelApiConfig


ACK = {
    "type": "chat",
    "message_type": "ack",
    "content": "I will inspect the shared terminal first.",
    "command": "none",
    "path": "",
    "delay_ms": 0,
}
PWD = {
    "type": "command",
    "message_type": "none",
    "content": "",
    "command": "pwd",
    "path": "",
    "delay_ms": 0,
}
DONE = {
    "type": "done",
    "message_type": "final",
    "content": "The shared-terminal task is complete.",
    "command": "none",
    "path": "",
    "delay_ms": 0,
}


FAKE_SERVER = r'''
import json
import sys

audit_path = sys.argv[1]
thread_id = "thread-test"
turn_id = "turn-test"
dynamic = False

ACK = {ack}
PWD = {pwd}
DONE = {done}

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def audit(value):
    with open(audit_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")

def item(action, item_id="message-one"):
    return {{
        "method": "item/completed",
        "params": {{
            "threadId": thread_id,
            "turnId": turn_id,
            "completedAtMs": 1,
            "item": {{
                "type": "agentMessage",
                "id": item_id,
                "text": json.dumps({{"action": action}}, separators=(",", ":")),
            }},
        }},
    }}

for line in sys.stdin:
    value = json.loads(line)
    audit(value)
    method = value.get("method")
    request_id = value.get("id")
    if method == "initialize":
        send({{"id": request_id, "result": {{"codexHome": "/private", "platformFamily": "unix", "platformOs": "macos", "userAgent": "fake"}}}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        dynamic = bool(value["params"].get("dynamicTools"))
        polluted = "POLLUTED" in value["params"].get("developerInstructions", "")
        send({{
            "id": request_id,
            "result": {{
                "thread": {{"id": thread_id}},
                "instructionSources": ["/outside/AGENTS.md"] if polluted else [],
            }},
        }})
    elif method == "turn/start":
        text = value["params"]["input"][0]["text"]
        send({{"id": request_id, "result": {{"turn": {{"id": turn_id, "status": "inProgress", "items": []}}}}}})
        if "WAIT_FOR_INTERRUPT" in text:
            continue
        if "FORBIDDEN_COMMAND" in text:
            send({{
                "method": "item/started",
                "params": {{
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "startedAtMs": 1,
                    "item": {{"type": "commandExecution", "id": "bad-command"}},
                }},
            }})
            continue
        if dynamic:
            send({{
                "id": "server-call-1",
                "method": "item/tool/call",
                "params": {{
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "callId": "call-1",
                    "tool": "remotebuddy_action",
                    "namespace": None,
                    "arguments": {{"action": ACK}},
                }},
            }})
            continue
        # Emit the item before the turn/start response consumer begins waiting.
        send(item(ACK))
        send({{"method": "turn/completed", "params": {{"threadId": thread_id, "turn": {{"id": turn_id, "status": "completed", "items": []}}}}}})
    elif method == "turn/interrupt":
        send({{"id": request_id, "result": {{}}}})
        send({{"method": "turn/completed", "params": {{"threadId": thread_id, "turn": {{"id": turn_id, "status": "interrupted", "items": []}}}}}})
    elif request_id == "server-call-1" and "result" in value:
        send({{
            "id": "server-call-2",
            "method": "item/tool/call",
            "params": {{
                "threadId": thread_id,
                "turnId": turn_id,
                "callId": "call-2",
                "tool": "remotebuddy_action",
                "namespace": None,
                "arguments": {{"action": PWD}},
            }},
        }})
    elif request_id == "server-call-2" and "result" in value:
        send(item(DONE, "message-done"))
        send({{"method": "turn/completed", "params": {{"threadId": thread_id, "turn": {{"id": turn_id, "status": "completed", "items": []}}}}}})
'''.format(
    ack=repr(ACK),
    pwd=repr(PWD),
    done=repr(DONE),
)


class CodexAppServerBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_app_server.py"
        self.audit = self.root / "wire.jsonl"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.telemetry = []

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, dynamic=False):
        return ModelApiConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            request_timeout_seconds=3.0,
            reasoning_effort="low",
            api_key="",
            warnings=(),
            agent_backend="codex-app-server",
            codex_dynamic_tools=dynamic,
        )

    def process_factory(self, _command, cwd):
        return subprocess.Popen(
            [sys.executable, str(self.script), str(self.audit)],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def backend(self, dynamic=False):
        return CodexAppServerBackend(
            self.config(dynamic),
            codex_binary="fake-codex",
            process_factory=self.process_factory,
        )

    def wire(self):
        if not self.audit.is_file():
            return []
        return [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]

    def test_trae_backend_shares_protocol_without_stdio_flag(self):
        # TraeCode CLI speaks the same app-server protocol but treats stdio as
        # the default transport, so the spawn command must omit --stdio.
        from codex_app_server_client import TraeAgentBackend

        commands = []

        def factory(command, cwd):
            commands.append(list(command))
            return self.process_factory(command, cwd)

        backend = TraeAgentBackend(
            self.config(),
            codex_binary="fake-traecli",
            process_factory=factory,
        )
        try:
            session = backend.start_cycle(
                "meeting-one",
                "RemoteBuddy Skill",
                "initial full local context",
                event_callback=self.telemetry.append,
            )
            response = backend.next_action(
                session,
                "later delta",
                allowed_types=("chat",),
                allowed_message_types=("ack", "question"),
            )
        finally:
            backend.close()
        self.assertEqual([["fake-traecli", "app-server"]], commands)
        self.assertEqual("thread-test", session)
        self.assertEqual("ack", response.action.message_type)
        self.assertEqual("trae-agent", backend.backend_name)
        self.assertTrue(any(item.get("backend") == "trae-agent" for item in self.telemetry))

    def test_initialization_ephemeral_thread_and_structured_turn(self):
        backend = self.backend()
        try:
            session = backend.start_cycle(
                "meeting-one",
                "RemoteBuddy Skill",
                "initial full local context",
                event_callback=self.telemetry.append,
            )
            response = backend.next_action(
                session,
                "later delta",
                allowed_types=("chat",),
                allowed_message_types=("ack", "question"),
            )
        finally:
            backend.close()

        self.assertEqual("thread-test", session)
        self.assertEqual("ack", response.action.message_type)
        requests = self.wire()
        initialize = next(value for value in requests if value.get("method") == "initialize")
        self.assertTrue(initialize["params"]["capabilities"]["experimentalApi"])
        thread = next(value for value in requests if value.get("method") == "thread/start")
        self.assertTrue(thread["params"]["ephemeral"])
        self.assertEqual([], thread["params"]["environments"])
        self.assertEqual([], thread["params"]["runtimeWorkspaceRoots"])
        self.assertEqual("never", thread["params"]["approvalPolicy"])
        self.assertEqual("read-only", thread["params"]["sandbox"])
        self.assertNotIn("dynamicTools", thread["params"])
        self.assertEqual("test-model", thread["params"]["model"])
        turn = next(value for value in requests if value.get("method") == "turn/start")
        self.assertEqual("initial full local context", turn["params"]["input"][0]["text"])
        self.assertEqual("test-model", turn["params"]["model"])
        self.assertFalse(turn["params"]["sandboxPolicy"]["networkAccess"])
        self.assertIn("outputSchema", turn["params"])
        self.assertFalse(any("text" in item for item in self.telemetry))

    def test_a_closed_backend_can_start_another_cycle(self):
        """页面上那个"重启"就是在同一个对象上 close 完再 start_cycle 一次。

        close() 会把进程失败标记钉成 "closed"；不清掉它，新 cycle 的第一个
        请求就会立刻报 "Codex App Server process exited"。
        """
        backend = self.backend()
        try:
            first = backend.start_cycle("meeting-one", "Skill", "context")
            backend.close()
            second = backend.start_cycle("meeting-one", "Skill v2", "context again")
            response = backend.next_action(
                second,
                "later delta",
                allowed_types=("chat",),
                allowed_message_types=("ack",),
            )
        finally:
            backend.close()
        self.assertEqual("thread-test", first)
        self.assertEqual("thread-test", second)
        self.assertEqual("ack", response.action.message_type)

    def test_empty_model_defers_to_cli_default(self):
        from dataclasses import replace

        backend = CodexAppServerBackend(
            replace(self.config(), model=""),
            codex_binary="fake-codex",
            process_factory=self.process_factory,
        )
        try:
            session = backend.start_cycle(
                "meeting-one",
                "RemoteBuddy Skill",
                "initial full local context",
                event_callback=self.telemetry.append,
            )
            backend.next_action(
                session,
                "later delta",
                allowed_types=("chat",),
                allowed_message_types=("ack", "question"),
            )
        finally:
            backend.close()
        requests = self.wire()
        thread = next(value for value in requests if value.get("method") == "thread/start")
        self.assertNotIn("model", thread["params"])
        turn = next(value for value in requests if value.get("method") == "turn/start")
        self.assertNotIn("model", turn["params"])

    def test_instruction_sources_and_forbidden_items_are_rejected(self):
        polluted = self.backend()
        with self.assertRaisesRegex(AgentProtocolError, "instructionSources"):
            polluted.start_cycle("meeting", "POLLUTED", "context")

        backend = self.backend()
        try:
            session = backend.start_cycle("meeting", "Skill", "FORBIDDEN_COMMAND")
            with self.assertRaisesRegex(AgentProtocolError, "commandExecution"):
                backend.next_action(session, "ignored")
            methods = [value.get("method") for value in self.wire()]
            self.assertIn("turn/interrupt", methods)
        finally:
            backend.close()

    def test_interrupt_wakes_waiter_without_returning_an_action(self):
        backend = self.backend()
        session = backend.start_cycle("meeting", "Skill", "WAIT_FOR_INTERRUPT")
        result = []

        def run_turn():
            try:
                backend.next_action(session, "ignored")
            except Exception as exc:
                result.append(exc)

        worker = threading.Thread(target=run_turn)
        worker.start()
        deadline = time.time() + 2
        while backend.turn_id is None and time.time() < deadline:
            time.sleep(0.01)
        backend.interrupt()
        worker.join(2)
        backend.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(result))
        self.assertEqual("AgentInterrupted", type(result[0]).__name__)

    def test_dynamic_tool_calls_are_validated_and_handled_in_order(self):
        backend = self.backend(dynamic=True)
        handled = []

        def handler(action, metadata):
            handled.append((action.type, action.command, metadata["call_id"]))
            return {"success": True, "content": "screen updated", "pause": False}

        try:
            session = backend.start_cycle("meeting", "Skill", "dynamic context")
            response = backend.next_action(session, "ignored", action_handler=handler)
        finally:
            backend.close()
        self.assertEqual(
            [("chat", "none", "call-1"), ("command", "pwd", "call-2")],
            handled,
        )
        self.assertEqual("done", response.action.type)
        server_results = [
            value for value in self.wire() if value.get("id") in {"server-call-1", "server-call-2"}
        ]
        self.assertEqual(2, len(server_results))
        self.assertTrue(all(value["result"]["success"] for value in server_results))

    def test_timeout_clears_the_turn_slot(self):
        backend = self.backend()
        try:
            session = backend.start_cycle("meeting", "Skill", "WAIT_FOR_INTERRUPT")
            with self.assertRaisesRegex(Exception, "timed out"):
                backend.next_action(session, "ignored", request_timeout_seconds=0.5)
            # A wedged turn slot would fail every later call with
            # "already has an active turn".
            self.assertIsNone(backend.turn_id)
        finally:
            backend.close()

    def test_dynamic_pause_releases_the_turn_for_the_next_request(self):
        backend = self.backend(dynamic=True)

        def handler(action, metadata):
            del action, metadata
            return {"success": True, "content": "executed", "pause": True}

        try:
            session = backend.start_cycle("meeting", "Skill", "dynamic context")
            response = backend.next_action(session, "ignored", action_handler=handler)
            self.assertTrue(response.executed)
            self.assertEqual("chat", response.action.type)
            self.assertIsNone(backend.turn_id)
        finally:
            backend.close()

    def test_handler_crash_answers_the_rpc_and_fails_the_turn(self):
        backend = self.backend(dynamic=True)

        def handler(action, metadata):
            del action, metadata
            raise OSError("storage failure")

        try:
            session = backend.start_cycle("meeting", "Skill", "dynamic context")
            with self.assertRaisesRegex(AgentProtocolError, "handler failed"):
                backend.next_action(session, "ignored", action_handler=handler)
        finally:
            backend.close()
        # The JSON-RPC request must still be answered so the child is not
        # left waiting on a dangling call.
        answered = [value for value in self.wire() if value.get("id") == "server-call-1"]
        self.assertEqual(1, len(answered))
        self.assertFalse(answered[0]["result"]["success"])


if __name__ == "__main__":
    unittest.main()


class LocalImageInputTest(unittest.TestCase):
    """人类发来的图交给模型自己看：Codex/Trae 的 `localImage`，不是命令行 OCR。"""

    def test_a_stored_picture_becomes_a_local_image_item(self):
        from codex_app_server_client import _local_image_inputs

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            png = root / "image-1.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nbody")
            self.assertEqual(
                [{"type": "localImage", "path": str(png)}],
                _local_image_inputs([{"name": "image-1.png", "media_type": "image/png",
                                      "path": str(png)}]),
            )

    def test_a_picture_that_is_not_there_is_not_attached(self):
        from codex_app_server_client import _local_image_inputs

        self.assertEqual([], _local_image_inputs([{"path": "/nowhere/image-9.png"}]))
        self.assertEqual([], _local_image_inputs(None))
