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
from pathlib import Path

from agent_protocol import AgentInterrupted, AgentProtocolError
from claude_code_client import ClaudeCodeBackend, TOOL_NAME
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
    if "NO_TOOL" in text:
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
                    "NO_TOOL",
                    action_handler=lambda _action, _metadata: {
                        "success": True,
                        "content": "unused",
                        "pause": True,
                    },
                )
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
