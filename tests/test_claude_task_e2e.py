"""End-to-end task simulation over the real Claude Code backend pipeline.

A scripted fake Claude CLI process drives one complete RemoteBuddy task
through the genuine stack: ClaudeCodeBackend stream-json protocol, the
authenticated MCP bridge socket, InteractiveRunner phase validation, and the
fake tmux transport. Only the model and the real CLI are simulated, which is
exactly what a DeepSeek + Claude Code run looks like from RemoteBuddy's side.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_code_client import ClaudeCodeBackend, TOOL_NAME
from interactive_runner import InteractiveRunner
from meeting_v2 import MeetingStore
from model_api_client import ModelApiConfig
from tests.test_interactive_runner import FakeTransport


TASK_ACTIONS = [
    {
        "type": "chat",
        "message_type": "ack",
        "content": "收到，我先用 Vim 创建 notes.md。",
        "command": "none",
        "path": "",
        "keys": [],
        "delay_ms": 0,
        "references": [{"kind": "chat", "seq": 1, "region": None}],
    },
    {
        "type": "command",
        "message_type": "none",
        "content": "",
        "command": "vim",
        "path": "notes.md",
        "keys": [],
        "delay_ms": 0,
    },
    {
        "type": "vim_keys",
        "message_type": "none",
        "content": "",
        "command": "none",
        "path": "",
        "keys": [
            {"kind": "literal", "value": "i"},
            {"kind": "literal", "value": "# RemoteBuddy DeepSeek+CC e2e"},
            {"kind": "key", "value": "Escape"},
            {"kind": "literal", "value": ":wq"},
            {"kind": "key", "value": "Enter"},
        ],
        "delay_ms": 0,
    },
    {
        "type": "command",
        "message_type": "none",
        "content": "",
        "command": "grep -c RemoteBuddy notes.md",
        "path": "",
        "keys": [],
        "delay_ms": 0,
    },
    {
        "type": "done",
        "message_type": "final",
        "content": "notes.md 已创建并验证，PROJECT TESTS PASSED 可见。",
        "command": "none",
        "path": "",
        "keys": [],
        "delay_ms": 0,
        "references": [{"kind": "screen", "seq": 2, "region": None}],
    },
]


SCRIPTED_CLAUDE = r'''
import json
import socket
import sys

TOOL = {tool!r}

actions_path = sys.argv[1]
command = sys.argv[2:]
session_id = command[command.index("--session-id") + 1]
mcp_path = command[command.index("--mcp-config") + 1]
mcp = json.load(open(mcp_path, encoding="utf-8"))["mcpServers"]["remotebuddy"]
socket_path = mcp["env"]["REMOTEBUDDY_MCP_SOCKET"]
token = mcp["env"]["REMOTEBUDDY_MCP_TOKEN"]
actions = json.load(open(actions_path, encoding="utf-8"))
initialized = False
turn = 0

def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)

for line in sys.stdin:
    json.loads(line)
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
    if turn >= len(actions):
        send({{"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}})
        continue
    action = actions[turn]
    turn += 1
    call_id = "call-%d" % turn
    send({{
        "type": "assistant",
        "session_id": session_id,
        "message": {{"content": [{{"type": "tool_use", "id": call_id, "name": TOOL, "input": {{"action": action}}}}]}},
    }})
    request = {{"token": token, "request_id": call_id, "action": {{"action": action}}}}
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
'''.format(tool=TOOL_NAME)


class ClaudeTaskEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "scripted_claude.py"
        self.script.write_text(SCRIPTED_CLAUDE, encoding="utf-8")
        self.actions_path = self.root / "actions.json"
        self.actions_path.write_text(json.dumps(TASK_ACTIONS), encoding="utf-8")
        skill = self.root / "SKILL.md"
        skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\nBe specific.",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "cc-task-demo",
            "请创建 notes.md 并写入一个标题。",
            skill,
            "remotebuddy-cc-task-demo",
            "deepseek-v4-pro[1m]",
            agent_backend="claude-code",
            agent_provider="deepseek",
        )
        self.transport = FakeTransport()

    def tearDown(self):
        self.temporary.cleanup()

    def process_factory(self, command, cwd, env):
        return subprocess.Popen(
            [sys.executable, str(self.script), str(self.actions_path), *command],
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

    def test_full_task_completes_over_the_claude_deepseek_pipeline(self):
        config = ModelApiConfig(
            model="deepseek-v4-pro[1m]",
            base_url="https://api.deepseek.com/anthropic",
            request_timeout_seconds=10.0,
            reasoning_effort="max",
            api_key="deepseek-secret",
            warnings=(),
            agent_backend="claude-code",
            agent_provider="deepseek",
            fast_model="deepseek-v4-flash",
            auth_keys={"DEEPSEEK_API_KEY": "deepseek-secret"},
        )
        backend = ClaudeCodeBackend(
            config,
            claude_binary="fake-claude",
            process_factory=self.process_factory,
            skip_protocol_check=True,
        )
        runner = InteractiveRunner(
            self.store, self.transport, backend, action_settle_seconds=0, typing_interval_seconds=0
        )
        try:
            runner.start()
        finally:
            backend.close()

        loaded = MeetingStore.load(self.root, "cc-task-demo")
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        self.assertIsNone(loaded.meeting.failure)
        chats = loaded.chat_messages()
        self.assertEqual(
            ["request", "ack", "final"], [event["message_type"] for event in chats]
        )
        # The Agent's ack quoted the human request; done quoted the screen.
        ack = chats[1]
        self.assertEqual("请创建 notes.md 并写入一个标题。", ack["data"]["references"][0]["excerpt"])
        final = chats[2]
        self.assertEqual("screen", final["data"]["references"][0]["kind"])
        # Vim edit and the relaxed bash command both reached the shared pane.
        self.assertTrue(any(item[1] == "vim" for item in self.transport.commands))
        typed = [text for _pane, text in self.transport.literal_sends]
        self.assertIn("grep -c RemoteBuddy notes.md", typed)
        statuses = [
            event["data"].get("status")
            for event in loaded.events()
            if event["kind"] == "action"
        ]
        self.assertNotIn("rejected", statuses)
        self.assertGreaterEqual(statuses.count("completed"), 5)


if __name__ == "__main__":
    unittest.main()
