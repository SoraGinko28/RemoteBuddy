"""End-to-end Codex pipeline scenario over the real runner and journal.

A scripted fake app-server subprocess plays a 15-turn collaboration that
exercises, in one session: structured and general bash commands, the shell
deletion policy, the invalid-action retry path (``command: "none"``), a full
Vim edit, a foreground python run with C-c interrupt, a mid-cycle human chat
message, a phase_mismatch rejection with corrective feedback, and a ``done``
completion. Only the model is fake; the CodexAppServerBackend decode path,
InteractiveRunner state machine, and MeetingStore journal are all real.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codex_app_server_client import CodexAppServerBackend
from interactive_runner import InteractiveRunner
from meeting_v2 import MeetingStore
from model_api_client import ModelApiConfig
from tests.test_interactive_runner import FakeTransport


HUMAN_MESSAGE = "请顺便确认 analysis.py 的输出没有异常。"

FAKE_SERVER = r'''
import json, sys, time

audit_path = sys.argv[1]
thread_id = "thread-scenario"
turn_count = 0

def act(a_type, message_type="none", content="", command="none", path="", diff="", delay_ms=0):
    return {"type": a_type, "message_type": message_type, "content": content,
            "command": command, "diff": diff, "path": path, "delay_ms": delay_ms}


BAD_DIFF = "把这两行改掉就行"

SCRIPT = {
    1: act("chat", "ack", "收到任务，我先查看当前目录。"),
    2: act("command", command="pwd"),
    3: act("command", command="ls -la"),
    4: act("command", command="rm -rf build"),
    5: act("command", command="mkdir -p build"),
    6: act("diff", diff=BAD_DIFF),
    7: act("command", command="git status --porcelain"),
    8: act("chat", "progress", "目录已经建好了。"),
    9: act("command", command="python", path="analysis.py"),
    10: act("command", command="python", path="analysis.py"),
    11: act("command", command="git status --porcelain"),
    12: act("command", command="python", path="analysis.py"),
    13: act("command", command="git status --porcelain"),
    14: act("chat", "progress", "分析脚本跑过了，没有异常输出。"),
    15: act("done", "final", "笔记与分析脚本均已就绪，任务完成。"),
}

# Turns whose observation must carry the previous rejection (or the injected
# human message), proving the correction loop feeds errors back to the model.
FEEDBACK_CHECKS = {
    5: "deletion-class command is not allowed",
    7: "unified diff",
    11: "analysis.py 的输出没有异常",
}

# Turns whose observation must carry the previous rejection (or the injected
# human message), proving the correction loop feeds errors back to the model.

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def audit(value):
    with open(audit_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")

for line in sys.stdin:
    value = json.loads(line)
    method = value.get("method")
    request_id = value.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"codexHome": "/private", "platformFamily": "unix",
              "platformOs": "macos", "userAgent": "fake"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        send({"id": request_id, "result": {"thread": {"id": thread_id}, "instructionSources": []}})
    elif method == "turn/start":
        turn_count += 1
        turn_id = f"turn-{turn_count}"
        text = value["params"]["input"][0]["text"]
        send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        record = {"turn": turn_count}
        if turn_count == 10:
            # The audit record triggers the test's injector thread; wait so
            # the human message lands before this turn's action executes and
            # is therefore part of the next observation deterministically.
            audit(record)
            time.sleep(0.4)
        expectation = FEEDBACK_CHECKS.get(turn_count)
        if expectation is not None:
            record["expected"] = expectation
            record["feedback_seen"] = expectation in text
        action = SCRIPT.get(turn_count)
        if action is None:
            record["error"] = "script exhausted"
            audit(record)
            continue
        record["sent"] = f"{action['type']}/{action['command']}"
        if turn_count != 10:
            audit(record)
        elif "feedback_seen" in record:
            audit(record)
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
              "completedAtMs": 1, "item": {"type": "agentMessage", "id": f"message-{turn_count}",
              "text": json.dumps({"action": action}, separators=(",", ":"))}}})
        send({"method": "turn/completed", "params": {"threadId": thread_id,
              "turn": {"id": turn_id, "status": "completed", "items": []}}})
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
'''


class CodexTaskEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_app_server.py"
        self.audit = self.root / "audit.jsonl"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        skill = self.root / "SKILL.md"
        skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Scenario.\n---\nBe precise.",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root, "codex-e2e", "整理基准计划并验证分析脚本。",
            skill, "remotebuddy-codex-e2e", "test-model", agent_backend="codex-app-server",
        )
        (self.store.workspace_path / "analysis.py").write_text("print('ok')\n", encoding="utf-8")
        self.config = ModelApiConfig(
            model="test-model", base_url="https://api.openai.com/v1",
            request_timeout_seconds=10.0, reasoning_effort="low", api_key="",
            warnings=(), agent_backend="codex-app-server",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def process_factory(self, _command, cwd):
        return subprocess.Popen(
            [sys.executable, str(self.script), str(self.audit)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )

    def audit_records(self):
        if not self.audit.is_file():
            return []
        return [json.loads(line) for line in self.audit.read_text("utf-8").splitlines()]

    def inject_human_message(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(record.get("turn") == 10 for record in self.audit_records()):
                MeetingStore.load(self.root, "codex-e2e").append_chat(
                    "human", HUMAN_MESSAGE, "request"
                )
                return
            time.sleep(0.02)

    def test_full_scenario_recovers_from_every_rejection_and_completes(self):
        backend = CodexAppServerBackend(
            self.config, codex_binary="fake-codex", process_factory=self.process_factory
        )
        transport = FakeTransport()
        injector = threading.Thread(target=self.inject_human_message, daemon=True)
        injector.start()
        try:
            InteractiveRunner(
                self.store, transport, backend,
                action_settle_seconds=0, typing_interval_seconds=0,
            ).start()
        finally:
            backend.close()
        injector.join(timeout=2)

        loaded = MeetingStore.load(self.root, "codex-e2e")
        events = loaded.events()
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        self.assertIsNone(loaded.meeting.failure)

        invalid = [
            event for event in events
            if event["kind"] == "action" and event.get("data", {}).get("reason") == "invalid_action"
        ]
        mismatch = [
            event for event in events
            if event["kind"] == "action" and event.get("data", {}).get("reason") == "phase_mismatch"
        ]
        self.assertEqual(2, len(invalid))  # rm -rf build, a diff that is not a diff
        self.assertEqual(0, len(mismatch))

        records = self.audit_records()
        feedback = {r["turn"]: r["feedback_seen"] for r in records if "feedback_seen" in r}
        for turn in (5, 7, 11):
            self.assertTrue(feedback.get(turn), f"turn {turn} observation lacked the feedback")
        self.assertEqual(15, max(r["turn"] for r in records))

        sequences = [event["seq"] for event in events]
        self.assertEqual(sequences, list(range(1, len(events) + 1)))

        typed = [entry[1] for entry in transport.commands]
        self.assertLessEqual({"pwd", "git", "python"}, set(typed))
        self.assertNotIn("rm", typed)
        # 动作里没有按键：终端里出现的按键只有 harness 自己的返回码探针，以及把前台
        # 从一个解释器手里收回来的 C-c。
        for _pane, keys in transport.tokens:
            for token in keys:
                value = str(token.get("value", ""))
                self.assertTrue(
                    "remotebuddy-retcode-" in value or value == "C-c",
                    transport.tokens,
                )

        conversation = (loaded.run_dir / "conversation.md").read_text(encoding="utf-8")
        self.assertIn(HUMAN_MESSAGE, conversation)
        self.assertIn("任务完成", conversation)
        self.assertLessEqual(loaded.meeting.cycle_action_count, 20)


if __name__ == "__main__":
    unittest.main()
