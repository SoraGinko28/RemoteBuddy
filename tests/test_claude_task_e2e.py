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
import tmux_vim
from interactive_runner import InteractiveRunner
from meeting_v2 import MeetingStore
from model_api_client import ModelApiConfig
from tests.test_interactive_runner import FakeTransport


TASK_ACTIONS = [
    {
        "type": "chat",
        "message_type": "ack",
        "content": "收到，我先建目录再验证。",
        "command": "none",
        "diff": "",
        "path": "",
        "delay_ms": 0,
        "references": [{"kind": "chat", "seq": 1, "region": None}],
    },
    {
        "type": "command",
        "message_type": "none",
        "content": "",
        "command": "mkdir -p build",
        "diff": "",
        "path": "",
        "delay_ms": 0,
    },
    {
        # 不是"单纯看一眼"的验证命令：照旧走共享终端。
        "type": "command",
        "message_type": "none",
        "content": "",
        "command": "touch checked.marker",
        "diff": "",
        "path": "",
        "delay_ms": 0,
    },
    {
        "type": "done",
        "message_type": "final",
        "content": "目录已建好，checked.marker 也落下了，PROJECT TESTS PASSED 可见。",
        "command": "none",
        "diff": "",
        "path": "",
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
    message = json.loads(line)
    if not initialized:
        send({{
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "tools": [TOOL, "Read"],
            "mcp_servers": [{{"name": "remotebuddy", "status": "connected"}}],
            "claude_code_version": "2.1.248",
        }})
        initialized = True
    content = message.get("message", {{}}).get("content") if isinstance(message.get("message"), dict) else None
    text = " ".join(part.get("text", "") for part in (content or []) if isinstance(part, dict))
    if text.startswith("Investigation step"):
        # 调查步：真 CLI 在这里用自己的读工具看文件，然后写字回答；这一次请求不交动作。
        send({{
            "type": "assistant",
            "session_id": session_id,
            "message": {{"content": [{{"type": "text", "text": "我看了工作区：notes.md 还不存在。"}}]}},
        }})
        send({{"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}})
        continue
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
    # 工具返回 success=false（比如动作被 harness 拒了）是"这条动作不行"，
    # 不是"这一轮坏了"：真 CLI 也是这么分的，接着让模型换一条。
    send({{"type": "result", "subtype": "success", "is_error": False, "session_id": session_id}})
'''.format(tool=TOOL_NAME)


class ClaudeResumeCommandTest(unittest.TestCase):
    """续会话/退回新建：看真正拼出来的命令行（不跑 CLI）。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.commands = []

    def tearDown(self):
        self.temporary.cleanup()

    def backend(self):
        config = ModelApiConfig(
            model="deepseek-v4-pro[1m]", base_url="https://api.deepseek.com/anthropic",
            request_timeout_seconds=10.0, reasoning_effort="max", api_key="deepseek-secret",
            warnings=(), agent_backend="claude-code", agent_provider="deepseek",
            fast_model="deepseek-v4-flash", auth_keys={"DEEPSEEK_API_KEY": "deepseek-secret"},
        )

        def factory(command, cwd, env):
            self.commands.append(list(command))
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                    cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8")

        return ClaudeCodeBackend(config, claude_binary="fake-claude", process_factory=factory,
                                 skip_protocol_check=True)

    def test_a_stored_session_is_resumed_and_never_persisted_off(self):
        backend = self.backend()
        try:
            backend.start_cycle("m", "skill", "context", workspace=str(self.root),
                                resume_session_id="11111111-2222-3333-4444-555555555555")
        finally:
            backend.close()
        command = self.commands[0]
        self.assertIn("--resume", command)
        self.assertEqual("11111111-2222-3333-4444-555555555555", command[command.index("--resume") + 1])
        self.assertNotIn("--session-id", command)
        # 带上它会话就永远续不了（帮助原文：cannot be resumed）。
        self.assertNotIn("--no-session-persistence", command)

    def test_without_a_stored_session_a_new_one_is_created(self):
        backend = self.backend()
        try:
            session = backend.start_cycle("m", "skill", "context", workspace=str(self.root))
        finally:
            backend.close()
        command = self.commands[0]
        self.assertIn("--session-id", command)
        self.assertEqual(session, command[command.index("--session-id") + 1])
        self.assertNotIn("--resume", command)


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
        # 两条命令都由 harness 在共享终端里敲进去：动作本身不再带按键。
        typed = [text for _pane, text in self.transport.literal_sends]
        # 命令那一行带前置标记，返回码是紧接着的另一行探针（两行同一个 token）。
        self.assertTrue(any(text.endswith("; mkdir -p build") for text in typed), typed)
        self.assertTrue(any(text.endswith("; touch checked.marker") for text in typed), typed)
        self.assertTrue(any(tmux_vim.RETCODE_MARKER in text for text in typed), typed)
        self.assertEqual([], self.transport.tokens)
        statuses = [
            event["data"].get("status")
            for event in loaded.events()
            if event["kind"] == "action"
        ]
        self.assertNotIn("rejected", statuses)
        results = [status for status in statuses if status == "result"]
        self.assertEqual(2, len(results), statuses)


if __name__ == "__main__":
    unittest.main()
