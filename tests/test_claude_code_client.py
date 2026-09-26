import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from agent_protocol import AgentInterrupted, AgentProtocolError
from claude_code_client import ClaudeCodeBackend, MAX_ACTION_REJECTIONS, TOOL_NAME, resolve_claude_binary
from model_api_client import ModelApiConfig


ACK = {
    "type": "chat",
    "message_type": "ack",
    "content": "I will inspect the workspace.",
    "command": "none",
    "path": "",
    "keys": [],
    "delay_ms": 0,
}


FAKE_CLAUDE = r'''
import json
import signal
import socket
import sys

ACK = {ack!r}
TOOL = {tool!r}

audit_path = sys.argv[1]
command = sys.argv[2:]
session_id = command[command.index("--session-id") + 1]
mcp_path = command[command.index("--mcp-config") + 1]
mcp = json.load(open(mcp_path, encoding="utf-8"))["mcpServers"]["remotebuddy"]
socket_path = mcp["env"]["REMOTEBUDDY_MCP_SOCKET"]
token = mcp["env"]["REMOTEBUDDY_MCP_TOKEN"]
initialized = False
refuse_forever = [False]
prose_forever = [False]

def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

for line in sys.stdin:
    value = json.loads(line)
    with open(audit_path, "a", encoding="utf-8") as audit:
        audit.write(json.dumps(value, ensure_ascii=False) + "\n")
    if not initialized:
        send({{
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "tools": [TOOL],
            "mcp_servers": [{{"name": "remotebuddy", "status": "connected"}}],
            "claude_code_version": "2.1.248",
        }})
        initialized = True
    text = value["message"]["content"][0]["text"]
    if "NO_TOOL_FOREVER" in text:
        refuse_forever[0] = True
    if refuse_forever[0] or "NO_TOOL" in text:
        send({{"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}})
        continue
    if "PROSE_ONLY_FOREVER" in text:
        prose_forever[0] = True
    if prose_forever[0] or "PROSE_ONLY" in text:
        send({{
            "type": "assistant",
            "session_id": session_id,
            "message": {{"content": [{{"type": "text", "text": "just prose answer"}}]}},
        }})
        send({{"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}})
        continue
    if "HANG" in text:
        signal.pause()
        continue
    call_id = "call-" + str(len(open(audit_path, encoding="utf-8").readlines()))
    send({{
        "type": "assistant",
        "session_id": session_id,
        "message": {{"content": [{{"type": "tool_use", "id": call_id, "name": TOOL, "input": {{"action": ACK}}}}]}},
    }})
    request = {{"token": token, "request_id": call_id, "action": {{"action": ACK}}}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path)
        connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
        response = b""
        while not response.endswith(b"\n"):
            response += connection.recv(65536)
    result = json.loads(response)
    send({{
        "type": "result",
        "subtype": "success" if result.get("success") else "error_during_execution",
        "is_error": not result.get("success"),
        "session_id": session_id,
    }})
'''.format(ack=ACK, tool=TOOL_NAME)


class ClaudeCodeBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_claude.py"
        self.audit = self.root / "audit.jsonl"
        self.script.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.spawned = []
        self.telemetry = []

    def tearDown(self):
        self.temporary.cleanup()

    def config(self):
        return ModelApiConfig(
            model="deepseek-v4-pro[1m]",
            base_url="https://api.deepseek.com/anthropic",
            request_timeout_seconds=3.0,
            reasoning_effort="max",
            api_key="deepseek-secret",
            warnings=(),
            agent_backend="claude-code",
            agent_provider="deepseek",
            fast_model="deepseek-v4-flash",
            auth_keys={"DEEPSEEK_API_KEY": "deepseek-secret"},
        )

    def process_factory(self, command, cwd, env):
        process = subprocess.Popen(
            [sys.executable, str(self.script), str(self.audit), *command],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self.spawned.append((list(command), dict(env), process))
        return process

    def backend(self):
        return ClaudeCodeBackend(
            self.config(),
            claude_binary="fake-claude",
            process_factory=self.process_factory,
            skip_protocol_check=True,
        )

    def test_effort_level_env_omitted_when_reasoning_is_none(self):
        from dataclasses import replace

        backend = ClaudeCodeBackend(
            replace(self.config(), reasoning_effort="none"),
            claude_binary="fake-claude",
            process_factory=self.process_factory,
            skip_protocol_check=True,
        )
        env = backend._child_environment("deepseek-v4-pro[1m]")
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", env)

        env = self.backend()._child_environment("deepseek-v4-pro[1m]")
        self.assertEqual("max", env["CLAUDE_CODE_EFFORT_LEVEL"])

    def test_a_picture_rides_along_as_an_image_block(self):
        import base64
        import io
        import types

        png = self.root / "image-1.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nbody")
        backend = self.backend()
        backend._session_id = "session-1"
        backend._process = types.SimpleNamespace(stdin=io.StringIO())
        backend._send_user_message("看这张", [{"path": str(png), "media_type": "image/png"}])

        payload = json.loads(backend._process.stdin.getvalue())
        content = payload["message"]["content"]
        self.assertEqual("text", content[0]["type"])
        self.assertEqual("image", content[1]["type"])
        self.assertEqual("image/png", content[1]["source"]["media_type"])
        self.assertEqual(b"\x89PNG\r\n\x1a\nbody",
                         base64.b64decode(content[1]["source"]["data"]))

    def test_a_picture_that_does_not_fit_is_left_to_its_path(self):
        import io
        import types

        huge = self.root / "huge.png"
        huge.write_bytes(b"x" * (2 * 1024 * 1024))
        backend = self.backend()
        backend._session_id = "session-1"
        backend._process = types.SimpleNamespace(stdin=io.StringIO())
        backend._send_user_message("看这张", [{"path": str(huge), "media_type": "image/png"}])

        payload = json.loads(backend._process.stdin.getvalue())
        self.assertEqual([{"type": "text", "text": "看这张"}], payload["message"]["content"])

    def test_isolated_command_environment_and_single_executed_action(self):
        backend = self.backend()
        actions = []
        try:
            session = backend.start_cycle(
                "meeting-one",
                "RemoteBuddy skill",
                "initial durable context",
                event_callback=self.telemetry.append,
            )
            response = backend.next_action(
                session,
                "later delta",
                allowed_types=("chat",),
                allowed_message_types=("ack",),
                action_handler=lambda action, metadata: (
                    actions.append((action, metadata))
                    or {"success": True, "content": "ack saved", "pause": True}
                ),
            )
        finally:
            backend.close()

        self.assertTrue(response.executed)
        self.assertEqual("ack", response.action.message_type)
        self.assertEqual(1, len(actions))
        command, env, process = self.spawned[0]
        self.assertIn("--restricted", command)
        self.assertEqual("", command[command.index("--tools") + 1])
        self.assertEqual(TOOL_NAME, command[command.index("--allowedTools") + 1])
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("deepseek-secret", command)
        self.assertEqual("deepseek-secret", env["ANTHROPIC_AUTH_TOKEN"])
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual("deepseek-v4-flash", env["CLAUDE_CODE_SUBAGENT_MODEL"])
        mcp_path = Path(command[command.index("--mcp-config") + 1])
        # The temporary directory is deleted on close, but its path and argv are
        # still sufficient to prove that no API key crossed the command line.
        self.assertFalse(mcp_path.exists())
        inputs = [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]
        self.assertEqual("initial durable context", inputs[0]["message"]["content"][0]["text"])
        self.assertIsNotNone(process.poll())
        self.assertTrue(any(item.get("event") == "initialized" for item in self.telemetry))

    def test_result_without_action_is_rejected(self):
        backend = self.backend()
        try:
            session = backend.start_cycle("meeting-two", "Skill", "")
            with self.assertRaisesRegex(AgentProtocolError, "no RemoteBuddy action"):
                backend.next_action(
                    session,
                    "NO_TOOL_FOREVER",
                    action_handler=lambda _action, _metadata: {
                        "success": True,
                        "content": "unused",
                        "pause": True,
                    },
                )
        finally:
            backend.close()
        # The stubborn model was nudged twice before the turn failed.
        inputs = [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]
        self.assertEqual(3, len(inputs))
        self.assertIn("remotebuddy_action", inputs[1]["message"]["content"][0]["text"])

    def test_prose_only_turn_is_nudged_and_recovers(self):
        backend = self.backend()
        actions = []
        try:
            session = backend.start_cycle(
                "meeting-nudge", "Skill", "", event_callback=self.telemetry.append
            )
            response = backend.next_action(
                session,
                "NO_TOOL this once",
                action_handler=lambda action, metadata: (
                    actions.append(action)
                    or {"success": True, "content": "accepted", "pause": True}
                ),
            )
        finally:
            backend.close()
        self.assertTrue(response.executed)
        self.assertEqual(1, len(actions))
        inputs = [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]
        self.assertEqual(2, len(inputs))
        self.assertTrue(any(item.get("event") == "turn_nudged" for item in self.telemetry))

    def test_prose_only_after_nudges_becomes_a_question_not_a_failure(self):
        backend = self.backend()
        actions = []
        try:
            session = backend.start_cycle(
                "meeting-prose-forever", "Skill", "", event_callback=self.telemetry.append
            )
            response = backend.next_action(
                session,
                "PROSE_ONLY_FOREVER",
                action_handler=lambda action, metadata: (
                    actions.append(action)
                    or {"success": True, "content": "accepted", "pause": True}
                ),
            )
        finally:
            backend.close()
        self.assertTrue(response.executed)
        self.assertEqual(1, len(actions))
        action = actions[0]
        self.assertEqual("chat", action.type)
        self.assertEqual("question", action.message_type)
        self.assertEqual("just prose answer", action.content)
        inputs = [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]
        self.assertEqual(3, len(inputs))  # initial + two nudges, then salvaged
        self.assertEqual(2, len([item for item in self.telemetry if item.get("event") == "turn_nudged"]))

    def test_result_arriving_during_slow_action_does_not_fail_the_turn(self):
        backend = self.backend()
        release = threading.Event()
        results = []
        try:
            backend.start_cycle("meeting-race", "Skill", "HANG")

            def slow_handler(_action, _metadata):
                release.wait(2)
                return {"success": True, "content": "typed slowly", "pause": True}

            backend._active_handler = slow_handler
            backend._turn_id = "turn-race"
            request = {
                "token": backend._bridge_token,
                "request_id": "slow-call",
                "action": {"action": ACK},
            }
            worker = threading.Thread(
                target=lambda: results.append(backend._handle_bridge_request(request))
            )
            worker.start()
            deadline = time.monotonic() + 2
            while backend._turn_claimed_action is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(backend._turn_claimed_action)
            # Claude's turn result lands while the action is still executing
            # (the real-world MCP tool-call timeout race).
            backend._handle_stream_event(
                {"type": "result", "subtype": "success", "is_error": False}
            )
            self.assertIsNone(backend._turn_protocol_error)
            release.set()
            worker.join(2)
            self.assertTrue(results and results[0]["success"])
            self.assertIsNotNone(backend._turn_action)
            self.assertTrue(backend._turn_result_seen)
        finally:
            release.set()
            backend.close()

    def test_rejection_reason_survives_an_early_turn_result(self):
        backend = self.backend()
        release = threading.Event()
        try:
            backend.start_cycle("meeting-reject", "Skill", "HANG")

            def rejecting_handler(_action, _metadata):
                release.wait(2)
                return {"success": False, "content": "phase mismatch: use vim", "pause": False}

            backend._active_handler = rejecting_handler
            backend._turn_id = "turn-reject"
            request = {
                "token": backend._bridge_token,
                "request_id": "rejected-call",
                "action": {"action": ACK},
            }
            worker = threading.Thread(target=lambda: backend._handle_bridge_request(request))
            worker.start()
            deadline = time.monotonic() + 2
            while backend._turn_claimed_action is None and time.monotonic() < deadline:
                time.sleep(0.01)
            backend._handle_stream_event(
                {"type": "result", "subtype": "success", "is_error": False}
            )
            release.set()
            worker.join(2)
            self.assertIn("phase mismatch: use vim", backend._turn_protocol_error or "")
            self.assertIn("ended the turn", backend._turn_protocol_error or "")
        finally:
            release.set()
            backend.close()

    def test_chat_only_rejections_do_not_trip_the_retry_limit(self):
        # A wrong chat message_type is a correctable mistake whose rejection
        # names the allowed options; it must not burn the retry budget that
        # guards genuinely stuck terminal actions.
        backend = self.backend()
        try:
            backend._bridge_token = "test-token"
            backend._turn_id = "turn-chat-rejects"
            backend._active_handler = lambda _action, _metadata: {
                "success": False,
                "content": "model message type is not allowed in the current phase; "
                "allowed_message_types=none,progress,question,final",
                "pause": False,
            }
            request = {
                "token": "test-token",
                "request_id": "chat-reject",
                "action": {"action": ACK},
            }
            for index in range(MAX_ACTION_REJECTIONS + 2):
                result = backend._handle_bridge_request(dict(request, request_id=f"chat-reject-{index}"))
                self.assertFalse(result["success"])
            self.assertIsNone(backend._turn_protocol_error)
            self.assertEqual(0, backend._rejections)
        finally:
            backend.close()

    def test_terminal_action_rejections_still_trip_the_retry_limit(self):
        backend = self.backend()
        try:
            backend._bridge_token = "test-token"
            backend._turn_id = "turn-terminal-rejects"
            backend._active_handler = lambda _action, _metadata: {
                "success": False,
                "content": "current_command=vim; command is not allowed in this phase",
                "pause": False,
            }
            request = {
                "token": "test-token",
                "request_id": "terminal-reject",
                "action": {
                    "action": {
                        "type": "command",
                        "message_type": "none",
                        "content": "",
                        "command": "pwd",
                        "path": "",
                        "keys": [],
                        "delay_ms": 0,
                    }
                },
            }
            for index in range(MAX_ACTION_REJECTIONS):
                backend._handle_bridge_request(dict(request, request_id=f"terminal-reject-{index}"))
            self.assertIsNotNone(backend._turn_protocol_error)
            self.assertIn(
                "exceeded the rejected-action retry limit", backend._turn_protocol_error or ""
            )
            self.assertIn("current_command=vim", backend._turn_protocol_error or "")
        finally:
            backend.close()

    def test_duplicate_tool_call_is_idempotent_and_first_action_wins(self):
        backend = self.backend()
        actions = []
        try:
            session = backend.start_cycle("meeting-duplicate", "Skill", "")
            # The fake CLI sends one call per input; call the bridge directly
            # twice to model Claude's duplicate same-turn call.
            request = {
                "token": backend._bridge_token,
                "request_id": "first-call",
                "action": {"action": ACK},
            }
            backend._active_handler = lambda action, metadata: actions.append(action) or {
                "success": True,
                "content": "accepted",
                "pause": True,
            }
            backend._turn_id = "turn-test"
            backend._handle_bridge_request(request)
            duplicate = dict(request, request_id="duplicate-call")
            result = backend._handle_bridge_request(duplicate)
            self.assertTrue(result["success"])
            self.assertTrue(result["pause"])
            self.assertEqual(1, len(actions))
        finally:
            backend.close()

    def test_concurrent_duplicate_tool_calls_execute_handler_once(self):
        backend = self.backend()
        actions = []
        entered = threading.Event()
        try:
            backend._bridge_token = "test-token"
            backend._turn_id = "turn-concurrent"

            def handler(action, metadata):
                actions.append(metadata["call_id"])
                entered.set()
                time.sleep(0.05)
                return {"success": True, "content": "accepted", "pause": True}

            backend._active_handler = handler
            request = {
                "token": "test-token",
                "request_id": "concurrent-call",
                "action": {"action": ACK},
            }
            results = []
            workers = [threading.Thread(target=lambda: results.append(backend._handle_bridge_request(request))) for _ in range(2)]
            for worker in workers:
                worker.start()
            self.assertTrue(entered.wait(1))
            for worker in workers:
                worker.join(1)
            self.assertEqual(1, len(actions))
            self.assertEqual(2, len(results))
            self.assertTrue(all(result["success"] for result in results))
        finally:
            backend.close()

    def test_three_consecutive_turns_each_execute_one_action(self):
        backend = self.backend()
        actions = []
        try:
            session = backend.start_cycle("meeting-three-turns", "Skill", "")
            for index in range(3):
                response = backend.next_action(
                    session,
                    f"observation-{index}",
                    allowed_types=("chat",),
                    allowed_message_types=("ack",),
                    action_handler=lambda action, metadata: (
                        actions.append((action, metadata))
                        or {"success": True, "content": "accepted", "pause": True}
                    ),
                )
                self.assertTrue(response.executed)
                self.assertEqual("ack", response.action.message_type)
                self.assertEqual(index + 1, len(actions))
        finally:
            backend.close()

    def test_interrupt_wakes_waiter_and_close_reaps_process(self):
        backend = self.backend()
        session = backend.start_cycle("meeting-three", "Skill", "")
        outcome = []

        def wait_for_action():
            try:
                backend.next_action(
                    session,
                    "HANG",
                    action_handler=lambda _action, _metadata: {
                        "success": True,
                        "content": "unused",
                        "pause": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - assertion captures exact type
                outcome.append(exc)

        worker = threading.Thread(target=wait_for_action)
        worker.start()
        deadline = time.time() + 2
        while time.time() < deadline and not self.audit.exists():
            time.sleep(0.01)
        backend.interrupt()
        worker.join(timeout=2)
        backend.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], AgentInterrupted)
        self.assertIsNotNone(self.spawned[0][2].poll())


class ClaudeBinaryResolutionTest(unittest.TestCase):
    def test_standard_macos_install_is_discovered_when_path_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "Claude" / "claude-code" / "2.1.249" / "claude"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            candidate.chmod(0o700)
            with patch("claude_code_client.shutil.which", return_value=None), patch(
                "claude_code_client._claude_install_candidates", return_value=[candidate]
            ):
                self.assertEqual(str(candidate), resolve_claude_binary())

    def test_explicit_path_wins_over_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "claude"
            candidate.write_text("#!/bin/sh\n", encoding="utf-8")
            candidate.chmod(0o700)
            with patch("claude_code_client.shutil.which") as which, patch(
                "claude_code_client._claude_install_candidates"
            ) as discover:
                self.assertEqual(str(candidate), resolve_claude_binary(str(candidate)))
                which.assert_not_called()
                discover.assert_not_called()


class ClaudeMcpServerTest(unittest.TestCase):
    def test_stdio_server_exposes_only_action_and_forwards_over_authenticated_socket(self):
        from mcp import Client, StdioServerParameters

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                socket_path = str(Path(directory) / "bridge.sock")
                token = "bridge-test-token"
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(socket_path)
                listener.listen(1)
                observed = []

                def bridge():
                    connection, _address = listener.accept()
                    with connection:
                        raw = b""
                        while not raw.endswith(b"\n"):
                            raw += connection.recv(65536)
                        request = json.loads(raw)
                        observed.append(request)
                        response = {
                            "request_id": request["request_id"],
                            "success": True,
                            "content": "action accepted",
                            "pause": True,
                        }
                        connection.sendall((json.dumps(response) + "\n").encode("utf-8"))

                worker = threading.Thread(target=bridge)
                worker.start()
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=[str(Path(__file__).resolve().parents[1] / "claude_mcp_server.py")],
                    env={
                        "REMOTEBUDDY_MCP_SOCKET": socket_path,
                        "REMOTEBUDDY_MCP_TOKEN": token,
                        "REMOTEBUDDY_MCP_TIMEOUT": "3",
                    },
                    cwd=str(Path(__file__).resolve().parents[1]),
                )
                async with Client(parameters) as client:
                    listing = await client.list_tools()
                    self.assertEqual(["remotebuddy_action"], [tool.name for tool in listing.tools])
                    self.assertEqual(["action"], listing.tools[0].input_schema["required"])
                    result = await client.call_tool("remotebuddy_action", {"action": ACK})
                    self.assertFalse(result.is_error)
                    self.assertEqual("action accepted", result.content[0].text)
                worker.join(timeout=2)
                listener.close()
                self.assertFalse(worker.is_alive())
                self.assertEqual(token, observed[0]["token"])
                self.assertEqual(ACK, observed[0]["action"]["action"])

        asyncio.run(exercise())


@unittest.skipUnless(
    os.environ.get("REMOTEBUDDY_DEEPSEEK_INTEGRATION") == "1"
    and bool(os.environ.get("DEEPSEEK_API_KEY"))
    and bool(__import__("shutil").which("claude")),
    "requires explicit opt-in, Claude Code, and DEEPSEEK_API_KEY",
)
class ClaudeDeepSeekIntegrationTest(unittest.TestCase):
    def test_environment_is_available_for_manual_cost_capped_integration(self):
        # The real run is deliberately opt-in. The deterministic backend and MCP
        # protocol tests above are the default CI acceptance boundary.
        self.assertTrue(os.environ["DEEPSEEK_API_KEY"])


if __name__ == "__main__":
    unittest.main()
