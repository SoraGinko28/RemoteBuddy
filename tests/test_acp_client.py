"""Trae-over-ACP 后端：一个假的 ACP server 走完一轮，看动作怎么回来。

真 CLI 不参与：假的进程按 ACP 的报文回答 initialize / session/new / session/prompt，
并且在收到 prompt 之后**自己扮演那个 MCP server**（用 session/new 里内联的 socket 和
token 连回 harness），把一条动作送进来。这条路径和真会议里一模一样，只是换掉了模型。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from acp_client import AcpBackend
from agent_protocol import AgentBackendError
from model_api_client import ModelApiConfig

FAKE_ACP = r'''
import json
import socket
import sys
import threading
import time

audit = []
settings = json.loads(sys.argv[1])
action = settings["action"]
permission_kinds = settings["permission_kinds"]
submit = settings.get("submit", True)
say = settings.get("say", "看着呢")


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def record(value):
    print(json.dumps({"audit": value}, ensure_ascii=False), flush=True)


def ask_permission(kind):
    send({"jsonrpc": "2.0", "id": "perm-" + kind, "method": "session/request_permission",
          "params": {"sessionId": session_id,
                     "toolCall": {"toolCallId": "call-" + kind, "kind": kind,
                                  "title": "probe " + kind, "status": "pending"},
                     "options": [{"optionId": "allow_once", "name": "Allow"},
                                 {"optionId": "reject_once", "name": "Reject"}]}})


def submit_action(env):
    path = next(item["value"] for item in env if item["name"] == "REMOTEBUDDY_MCP_SOCKET")
    token = next(item["value"] for item in env if item["name"] == "REMOTEBUDDY_MCP_TOKEN")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall((json.dumps({"token": token, "request_id": "mcp-1",
                                        "action": {"action": action}}) + "\n").encode("utf-8"))
        raw = b""
        while not raw.endswith(b"\n"):
            raw += connection.recv(65536)
    record({"tool_result": json.loads(raw.decode("utf-8"))})


session_id = "session-fake"
servers = []

for line in sys.stdin:
    message = json.loads(line)
    if "method" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        servers.extend(message["params"]["mcpServers"])
        record({"servers": servers})
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": session_id}})
    elif method == "session/prompt":
        if say:
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": session_id,
                             "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"type": "text", "text": say}}}})
        for kind in permission_kinds:
            ask_permission(kind)
        time.sleep(0.2)
        if submit:
            threading.Thread(target=submit_action, args=(servers[0]["env"],), daemon=True).start()
        time.sleep(1.2)
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        record({"cancelled": True})
    elif "id" in message:
        record({"answered": {"id": message["id"], "result": message.get("result")}})
'''


class AcpBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_acp.py"
        self.script.write_text(FAKE_ACP, encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "merge.py").write_text("def merge_sort(items):\n    return items\n",
                                                 encoding="utf-8")
        self.messages = []
        self.action = {
            "type": "command", "message_type": "none", "content": "",
            "command": "touch ran.marker", "path": "", "keys": [], "delay_ms": 0,
        }
        self.permission_kinds = ["read", "edit"]
        self.submit = True
        self.say = "看着呢"

    def config(self):
        return ModelApiConfig(
            model="gpt-5.6-luna",
            base_url="https://api.openai.com/v1",
            request_timeout_seconds=10.0,
            reasoning_effort="low",
            api_key="",
            warnings=(),
            agent_backend="trae-acp",
            agent_provider="openai",
        )

    def process_factory(self, _command, cwd):
        settings = json.dumps({"action": self.action, "permission_kinds": self.permission_kinds,
                               "submit": self.submit, "say": self.say})
        return subprocess.Popen(
            [sys.executable, str(self.script), settings],
            cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )

    def backend(self):
        return AcpBackend(self.config(), acp_binary="fake-traecli",
                          process_factory=self.process_factory)

    def test_one_action_comes_back_through_the_mcp_bridge(self):
        backend = self.backend()
        handled = []
        try:
            session = backend.start_cycle(
                "meeting-acp", "RemoteBuddy skill", "读一下 merge.py",
                workspace=str(self.workspace),
            )
            response = backend.next_action(
                session, "读它并回话",
                action_handler=lambda action, metadata: (
                    handled.append((action, metadata))
                    or {"success": True, "content": "跑了", "pause": True}
                ),
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        self.assertEqual("session-fake", session)
        self.assertEqual("command", response.action.type)
        self.assertEqual("touch ran.marker", response.action.command)
        self.assertEqual(1, len(handled))
        self.assertTrue(response.executed)

    def test_the_action_tool_is_inlined_as_a_stdio_mcp_server(self):
        backend = self.backend()
        try:
            backend.start_cycle("meeting-acp", "skill", "context", workspace=str(self.workspace))
            backend.next_action(
                "session-fake", "go",
                action_handler=lambda action, metadata: {"success": True, "content": "ok", "pause": True},
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        spec = backend._mcp_server_spec()
        self.assertEqual("remotebuddy", spec["name"])
        self.assertTrue(spec["command"])
        self.assertTrue(spec["args"][0].endswith("claude_mcp_server.py"))
        # ACP 的 env 必须是 [{name, value}]：写成对象会被它拒。
        names = {item["name"] for item in spec["env"]}
        self.assertEqual({"REMOTEBUDDY_MCP_SOCKET", "REMOTEBUDDY_MCP_TOKEN", "REMOTEBUDDY_MCP_TIMEOUT"}, names)
        self.assertTrue(all(set(item) == {"name", "value"} for item in spec["env"]))

    def test_our_own_tool_is_approved_when_only_the_text_names_it(self):
        """它批准 MCP 工具调用时，title 只写 "Approve MCP tool call"，名字在 content 里。"""
        backend = self.backend()
        sent = []
        backend._send = lambda payload: sent.append(payload)
        request = {
            "method": "session/request_permission",
            "params": {
                "toolCall": {
                    "toolCallId": "call-1", "status": "pending", "title": "Approve MCP tool call",
                    "content": [{"type": "content", "content": {"type": "text",
                        "text": 'Allow the remotebuddy MCP server to run tool "remotebuddy_action"?'}}],
                },
                "options": [
                    {"optionId": "approved", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
                ],
            },
        }
        backend._answer_request(request)
        self.assertEqual(
            {"outcome": {"outcome": "selected", "optionId": "approved"}},
            sent[0]["result"],
        )

    def test_the_investigation_turn_returns_only_its_text(self):
        """调查步：这一次请求不交动作，它写的字就是结论。"""
        self.submit = False
        self.permission_kinds = []
        backend = self.backend()
        try:
            session = backend.start_cycle(
                "meeting-acp", "RemoteBuddy skill", "context", workspace=str(self.workspace)
            )
            findings = backend.investigate(
                session, "看看 merge.py",
                action_handler=lambda action, metadata: {"success": True, "content": "ok", "pause": True},
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        self.assertEqual("看着呢", findings)

    def test_a_silent_investigation_comes_back_empty(self):
        """没什么可查的它一句话都不说：这一枪照样正常收尾，结论就是空串。"""
        self.submit = False
        self.permission_kinds = []
        self.say = ""
        backend = self.backend()
        try:
            session = backend.start_cycle(
                "meeting-acp", "RemoteBuddy skill", "context", workspace=str(self.workspace)
            )
            findings = backend.investigate(
                session, "看看 merge.py",
                action_handler=lambda action, metadata: {"success": True, "content": "ok", "pause": True},
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        self.assertEqual("", findings)

    def test_an_action_claimed_in_the_investigation_turn_is_answered_and_ignored(self):
        """调查步里它去调动作工具：退回（is_error）还是会答，这一次请求仍然只收文字。"""
        handed = []
        backend = self.backend()
        try:
            session = backend.start_cycle(
                "meeting-acp", "RemoteBuddy skill", "context", workspace=str(self.workspace)
            )
            findings = backend.investigate(
                session, "看看 merge.py",
                action_handler=lambda action, metadata: (
                    handed.append(action)
                    or {"success": False, "content": "终端关着", "pause": False}
                ),
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        self.assertEqual(["command"], [action.type for action in handed])
        self.assertEqual("看着呢", findings)

    def test_permissions_are_answered_by_kind(self):
        """读放行、写拒绝：写只能走共享终端，读走它自己的只读工具。"""
        backend = self.backend()
        answers = []
        original = backend._answer_request

        def spy(message):
            answers.append((str(((message.get("params") or {}).get("toolCall") or {}).get("kind")),
                            original(message)))

        backend._answer_request = spy
        try:
            backend.start_cycle("meeting-acp", "skill", "context", workspace=str(self.workspace))
            backend.next_action(
                "session-fake", "go",
                action_handler=lambda action, metadata: {"success": True, "content": "ok", "pause": True},
                request_timeout_seconds=8.0,
            )
        finally:
            backend.close()
        self.assertEqual([("read", "allow_once"), ("edit", "reject_once")], answers)


if __name__ == "__main__":
    unittest.main()
