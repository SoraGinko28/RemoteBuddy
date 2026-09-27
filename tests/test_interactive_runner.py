import shlex
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_protocol import AgentActionError, AgentBackendError
from interactive_runner import (
    MAX_IMAGE_BYTES_PER_TURN,
    InteractiveRunner,
    RunnerError,
    TypingInterrupted,
    resolve_workspace_path,
    send_line_with_captures,
    send_tokens_with_captures,
)
from typing_executor import _send_typing_plan
from meeting_v2 import MeetingStore, utc_now
from agent_protocol import AgentActionError
from model_api_client import AgentAction, AgentProtocolError, AgentResponse, ModelApiError
from typing_policy import plan_input
from agent_protocol import (ACT_STEP_JSON_TEXT, ACT_STEP_OPEN_JSON_TEXT, ACT_STEP_TEXT,
                            INVESTIGATE_STEP_TEXT)


def make_action(action_type="chat", **changes):
    payload = {
        "type": action_type,
        "message_type": "ack" if action_type == "chat" else "none",
        "content": "I will inspect the shared workspace." if action_type == "chat" else "",
        "command": "none",
        "path": "",
        "delay_ms": 0,
    }
    payload.update(changes)
    return AgentAction.from_dict(payload)


class FakeConfig:
    request_timeout_seconds = 60.0


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.conversations = []
        self.config = FakeConfig()
        self.last_images = None
        self.closed = 0

    def close(self):
        self.closed += 1

    def update_instructions(self, instructions):
        """指令是按请求传的那一类后端：换掉就算数。"""
        self.instructions = instructions
        return True

    def create_conversation(self, meeting_id):
        value = f"conv-{len(self.conversations) + 1}"
        self.conversations.append((meeting_id, value))
        return value

    def create_action(self, conversation_id, instructions, observation, **kwargs):
        self.last_images = kwargs.get("images")
        self.calls.append((conversation_id, instructions, observation, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return AgentResponse(f"resp-{len(self.calls)}", outcome)


class BlockingClient(FakeClient):
    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.started = threading.Event()
        self.release = threading.Event()

    def create_action(self, conversation_id, instructions, observation, **kwargs):
        if not self.calls:
            self.started.set()
            self.release.wait(2)
        return super().create_action(conversation_id, instructions, observation, **kwargs)


class PauseOnThirdClient(FakeClient):
    def create_action(self, conversation_id, instructions, observation, **kwargs):
        if len(self.calls) == 2:
            time.sleep(0.08)
        return super().create_action(conversation_id, instructions, observation, **kwargs)


class DynamicBackend:
    backend_name = "codex-app-server"
    stateful = True

    def __init__(self):
        self.config = FakeConfig()
        self.calls = []
        self.protocol_version = "fake-codex"

    def start_cycle(self, meeting_id, instructions, initial_context, **kwargs):
        self.calls.append(("start", meeting_id, instructions, initial_context))
        callback = kwargs.get("event_callback")
        if callback:
            callback({"backend": self.backend_name, "event": "thread_started", "session_id": "thread-1"})
        return "thread-1"

    def next_action(self, session_id, observation, **kwargs):
        self.calls.append(("next", session_id, observation, kwargs))
        handler = kwargs["action_handler"]
        first = handler(make_action(), {"call_id": "call-1", "turn_id": "turn-1"})
        second = handler(
            make_action("command", command="pwd"),
            {"call_id": "call-2", "turn_id": "turn-1"},
        )
        self.calls.append(("results", first, second))
        return AgentResponse(
            "message-final",
            make_action("done", message_type="final", content="I checked the workspace."),
            session_id="thread-1",
            turn_id="turn-1",
        )

    def interrupt(self):
        return None

    def close(self):
        return None


class FakeTransport:
    def __init__(self):
        self.exists = False
        self.pane_id = "%1"
        self.command = "bash"
        self.path = ""
        self.screen_revision = 0
        self.commands = []
        self.tokens = []
        self.literal_sends = []
        self.pending_input = ""
        # 壳里真的发生过什么：harness 跑完一条命令会从整屏缓存里找返回码标记。
        self.console = ""
        self.retcode = "0"

    def create_workspace_session(self, session_name, workdir):
        self.exists = True
        self.command = "bash"          # 新会话起来就是一个干净的壳
        self.pending_input = ""
        self.path = str(workdir.resolve())
        self.commands.append(("create", session_name, self.path))
        return self.pane_id

    def kill_session(self, _session_name):
        self.exists = False
        return True

    def session_exists(self, _session_name):
        return self.exists

    def pane_info(self, pane_id):
        if not self.exists or pane_id != self.pane_id:
            return {
                "dead": "1", "command": "", "path": self.path,
                "cursor_x": "0", "cursor_y": "0", "pane_width": "120", "pane_height": "36",
            }
        return {
            "dead": "0", "command": self.command, "path": self.path,
            "cursor_x": "0", "cursor_y": "0", "pane_width": "120", "pane_height": "36",
        }

    def capture(self, _pane_id):
        return self.capture_visible(_pane_id)

    def capture_visible(self, _pane_id):
        return (
            f"workspace screen {self.screen_revision}\n"
            f"{self.command} {self.path}\n{self.pending_input}\n{self.console}"
            "PROJECT TESTS PASSED"
        )

    def send_literal(self, pane_id, text):
        self.literal_sends.append((pane_id, text))
        if self.command in {"vim", "vi"} or self.command.startswith("python"):
            self.tokens.append((pane_id, [{"kind": "text", "value": text}]))
        self.pending_input += text
        self.screen_revision += 1

    def send_keys(self, pane_id, text):
        self.send_literal(pane_id, text)
        self.send_enter(pane_id)

    def send_special(self, pane_id, key):
        if key == "C-c" and self.command not in {"bash", "zsh", "sh"}:
            # 交互式解释器（python 之类）收到 C-c 就回到 shell：harness 靠它把前台收回来。
            self.tokens.append((pane_id, [{"kind": "ctrl", "value": key}]))
            self.command = "bash"
            self.pending_input = ""
            self.screen_revision += 1
            return
        if key == "C-c" and self.command in {"bash", "zsh", "sh"}:
            self.tokens.append((pane_id, [{"kind": "ctrl", "value": key}]))
            self.pending_input = ""
            self.screen_revision += 1
            return
        if key == "Enter" and self.command in {"bash", "zsh", "sh"}:
            self.send_enter(pane_id)
            return
        self.tokens.append((pane_id, [{"kind": "ctrl", "value": key}]))
        self.screen_revision += 1

    def send_enter(self, pane_id):
        line = self.pending_input
        parts = shlex.split(line)
        if parts:
            executable = parts[0]
            command = "python" if executable == "python3" else executable
            path = parts[-1] if len(parts) > 1 else ""
            self.commands.append((pane_id, command, path))
            if command == "cd":
                self.path = path
            elif command == "vim":
                self.command = "vim"
            elif command == "python":
                self.command = "python3"
            # harness 用一行带随机标记的 echo 读返回码：把那一行也演出来。
            if executable == "echo" and len(parts) > 1 and parts[1].startswith("remotebuddy-retcode-"):
                self.console += f"{parts[1]} {self.retcode}\n"
            else:
                self.console += line + "\n"
        self.pending_input = ""
        self.screen_revision += 1

    def send_workspace_command(self, pane_id, command, path=""):
        self.commands.append((pane_id, command, path))
        self.screen_revision += 1
        if command == "cd":
            self.path = path
        elif command == "vim":
            self.command = "vim"

    def send_tokens(self, pane_id, keys):
        """记账用的假编辑器：只认得出"写盘退出"，好让前台回到 shell。

        普通模式的动作（`1G`、`dd`）发完就完；只有 `:` 开头的 ex 命令会攒到回车。
        """
        self.tokens.append((pane_id, list(keys)))
        for token in keys:
            if self.command not in {"vim", "vi"}:
                continue
            kind = token.get("kind")
            value = str(token.get("value", ""))
            if kind == "ctrl" and value in {"Escape", "C-c"}:
                self.pending_input = ""
            elif kind == "text" and value.startswith(":"):
                self.pending_input = value
            elif kind == "text":
                self.pending_input += value
            elif kind == "ctrl" and value == "Enter":
                if self.pending_input in {":q", ":q!", ":wq", ":wq!", ":x", ":x!"}:
                    self.command = "bash"
                self.pending_input = ""
        self.screen_revision += 1


class JsonActionClient(FakeClient):
    """手里没有工具的 backend（结构化 app-server、Responses）：动作按 schema 交 JSON。"""

    action_submission = "json"


class RecordingCycleClient(FakeClient):
    """记下每一轮 start_cycle 拿到的 resume_session_id 和初始上下文。"""

    # trae-acp：能续会话（claude-code 要求 deepseek provider，这里用默认的 openai）。
    backend_name = "trae-acp"
    stateful = True

    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.cycles = []

    def start_cycle(self, meeting_id, instructions, initial_context, **kwargs):
        self.cycles.append({"resume": kwargs.get("resume_session_id"),
                            "context": initial_context, "instructions": instructions})
        return f"session-{len(self.cycles)}"


class InvestigatingClient(FakeClient):
    """有自己读工具的 backend：每个动作之前，harness 都会先要一次调查。"""

    backend_name = "trae-acp"

    def __init__(self, outcomes, findings="我看了 notes.md：里面还是空的。"):
        super().__init__(outcomes)
        self.findings = findings
        self.investigations = []

    def investigate(self, session_id, observation, **kwargs):
        self.investigations.append(observation)
        return self.findings


class StuckInvestigationClient(InvestigatingClient):
    """调查步超时的 backend：这一步可选，超了也要接着干，不能把整轮判死。"""

    def investigate(self, session_id, observation, **kwargs):
        self.investigations.append(observation)
        raise AgentBackendError("Claude Code investigation turn timed out")


class AskingClient(FakeClient):
    """第一次请求飞着的时候按下页面那个按钮。

    页面上就是"请求在路上、人按下去"这个时序；用真线程去卡这个窗口是碰运气，
    所以让前端自己在下第一次请求时按。
    """

    def __init__(self, outcomes, ask):
        super().__init__(outcomes)
        self.ask = ask
        self.asked = False

    def create_action(self, conversation_id, instructions, observation, **kwargs):
        if not self.asked:
            self.asked = True
            self.ask()
        return super().create_action(conversation_id, instructions, observation, **kwargs)


class BackendRequestTest(unittest.TestCase):
    """页面上那两个按钮：更新（不动进程）与重启（换一个进程）。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\n旧的一句。",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root, "backend-demo", "Create notes.md.", self.skill,
            "remotebuddy-backend-demo", "test-model",
        )
        self.transport = FakeTransport()

    def tearDown(self):
        self.temporary.cleanup()

    def test_skill_reload_swaps_the_text_without_touching_the_process(self):
        holder = {}
        client = AskingClient(
            [make_action("command", command="pwd")],
            lambda: (self.store.runtime_skill_path.write_text(
                "---\nname: workspace-collaborator\n---\n新的一句。", encoding="utf-8"),
                holder["runner"].request_skill_reload()),
        )
        runner = InteractiveRunner(self.store, self.transport, client,
                                   action_settle_seconds=0, typing_interval_seconds=0)
        holder["runner"] = runner
        runner.start()

        self.assertEqual(0, client.closed, "更新不该关掉进程")
        self.assertEqual(1, len(client.conversations), "更新不该重开会话")
        self.assertTrue(client.calls[1][1].endswith("新的一句。"), client.calls[1][1][-40:])
        events = [e for e in MeetingStore.load(self.root, "backend-demo").events()
                  if e.get("phase") == "skill_reload"]
        self.assertTrue(events)
        self.assertEqual("completed", events[0]["data"]["status"])

    def test_backend_restart_closes_the_process_and_starts_a_new_cycle(self):
        holder = {}
        client = AskingClient(
            [make_action("command", command="pwd")],
            lambda: holder["runner"].request_backend_restart(),
        )
        runner = InteractiveRunner(self.store, self.transport, client,
                                   action_settle_seconds=0, typing_interval_seconds=0)
        holder["runner"] = runner
        runner.start()

        self.assertEqual(1, client.closed, "重启要关掉旧进程")
        self.assertEqual(2, len(client.conversations), "重启要重开一个新会话")
        loaded = MeetingStore.load(self.root, "backend-demo")
        restarts = [e for e in loaded.events() if e.get("phase") == "agent_restart"]
        self.assertEqual(["started", "completed"], [e["data"]["status"] for e in restarts])
        session_ids = [e["data"]["session_id"] for e in restarts if e["data"].get("session_id")]
        self.assertEqual(1, len(session_ids))


class InteractiveRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\nBe specific.",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "runner-demo",
            "Create notes.md with a title.",
            self.skill,
            "remotebuddy-runner-demo",
            "test-model",
        )
        self.transport = FakeTransport()

    def tearDown(self):
        self.temporary.cleanup()

    def test_a_command_action_comes_back_with_its_return_code_and_output(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="python3 -m pytest -q"),
                make_action("done", message_type="final", content="Tests printed."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        self.assertEqual(
            "python",
            [item[1] for item in self.transport.commands if item[0] != "create"][0],
        )
        self.assertTrue(
            any("python3 -m pytest -q" in value for _pane, value in self.transport.literal_sends)
        )
        loaded = MeetingStore.load(self.root, "runner-demo")
        results = [
            event for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "result"
        ]
        self.assertEqual(1, len(results))
        result = results[0]["data"]["result"]
        self.assertEqual("cmd", result["kind"])
        self.assertEqual("0", result["retcode"])
        self.assertEqual("python3 -m pytest -q", result["cmd"])

    def test_a_diff_action_goes_through_vim_in_the_shared_pane(self):
        """共享终端是真的在动 vim：这里只验管道——动作交给了落盘那条路，结果记下来了。"""
        workspace = self.store.workspace_path
        subprocess.run(["git", "init", "-q", "."], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "runner@example.com"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "runner"], cwd=workspace, check=True)
        (workspace / "notes.md").write_text("title\nbody\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=workspace, check=True)
        client = FakeClient(
            [
                make_action(),
                make_action(
                    "diff",
                    diff=(
                        "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1,2 @@\n-title\n+TITLE\n body\n"
                        "--- a/extra.md\n+++ b/extra.md\n@@ -0,0 +1,2 @@\n+one\n+two\n"
                    ),
                ),
                make_action("done", message_type="final", content="Notes updated."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        opened = [value for _pane, value in self.transport.literal_sends if "notes.md" in value]
        self.assertTrue(any("vim" in value for value in opened), self.transport.literal_sends)
        loaded = MeetingStore.load(self.root, "runner-demo")
        results = [
            event for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "result"
        ]
        self.assertEqual(1, len(results))
        result = results[0]["data"]["result"]
        self.assertEqual("diff", result["kind"])
        self.assertEqual(["notes.md", "extra.md"], result["files"])
        self.assertTrue(result["rounds"] >= 1)
        # 假终端不会真的改文件，所以落盘一定对不上：比对这条路也在记账。
        self.assertNotEqual("0", result["retcode"])
        self.assertEqual(["notes.md", "extra.md"], result["mismatched_files"])

    def test_an_interactive_command_is_refused_with_something_the_model_can_act_on(self):
        # 模型真的会这么发：客户端在造动作那一步就把它退回去（`vim` 会一直等人敲键盘）。
        client = FakeClient(
            [
                make_action(),
                AgentActionError(
                    "vim is an interactive program: hand over a `diff` or a `command` instead"
                ),
                make_action("command", command="git status"),
                make_action("done", message_type="final", content="Looked instead."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        rejected = [
            event for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "rejected"
        ]
        self.assertTrue(rejected)
        self.assertIn("interactive program", rejected[0]["content"])
        self.assertIn("hand over a `diff`", rejected[0]["content"])
        # 被退回之后它换了条能跑的命令，这一轮没死。
        self.assertTrue(any(item[1] == "git" for item in self.transport.commands))
        self.assertEqual("completion_proposed", loaded.meeting.phase)

    def test_rule_plan_uses_deadlines_and_persists_screen_metadata(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        plan = plan_input(
            [{"kind": "text", "value": "print(alpha + beta)"}],
            "balanced",
            "runner-demo",
            "paced-action",
            "code",
        )
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch("interactive_runner.time.monotonic", side_effect=lambda: clock[0]), mock.patch(
            "interactive_runner.time.sleep", side_effect=sleep
        ):
            _send_typing_plan(
                self.store,
                self.transport,
                pane_id,
                plan,
                "paced-action",
                None,
            )

        self.assertAlmostEqual(plan.planned_duration_ms / 1000.0, clock[0] - 100.0, places=6)
        self.assertEqual(
            "print(alpha + beta)",
            "".join(value for _pane, value in self.transport.literal_sends),
        )
        screens = [event for event in self.store.events() if event["kind"] == "screen"]
        # One frame per step: the plan is one line, so one capture.
        self.assertEqual(len(plan.steps), len(screens))
        self.assertTrue(
            all(event["data"].get("action_id") == "paced-action" for event in screens)
        )
        self.assertTrue(all(event["data"].get("planned_delay_ms") for event in screens))

    def test_rest_step_holds_pacing_without_sending_keys(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        plan = plan_input(
            [
                {"kind": "text", "value": "echo hi"},
                {"kind": "rest", "value": 200},
                {"kind": "ctrl", "value": "Enter"},
            ],
            "balanced",
            "runner-demo",
            "rest-action",
            "shell",
            allow_paste=False,
        )
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch("interactive_runner.time.monotonic", side_effect=lambda: clock[0]), mock.patch(
            "interactive_runner.time.sleep", side_effect=sleep
        ):
            _send_typing_plan(
                self.store,
                self.transport,
                pane_id,
                plan,
                "rest-action",
                None,
            )
        # The rest sends nothing: only the literal text reaches the pane, and
        # the full human-paced duration (including the 200 ms hold) elapsed.
        self.assertEqual(
            "echo hi",
            "".join(value for _pane, value in self.transport.literal_sends),
        )
        self.assertAlmostEqual(plan.planned_duration_ms / 1000.0, clock[0] - 100.0, places=6)

    def test_rule_plan_can_interrupt_between_lines(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        plan = plan_input(
            [
                {"kind": "text", "value": "alpha"},
                {"kind": "ctrl", "value": "Enter"},
                {"kind": "text", "value": "beta"},
                {"kind": "ctrl", "value": "Enter"},
                {"kind": "text", "value": "gamma"},
            ],
            "demo",
            "runner-demo",
            "interrupt-action",
            "code",
        )
        checks = [False, True]

        def interrupted():
            return checks.pop(0) if checks else True

        clock = [0.0]
        with mock.patch("interactive_runner.time.monotonic", side_effect=lambda: clock[0]), mock.patch(
            "interactive_runner.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        ):
            with self.assertRaises(TypingInterrupted) as raised:
                _send_typing_plan(
                    self.store,
                    self.transport,
                    pane_id,
                    plan,
                    "interrupt-action",
                    interrupted,
                )
        self.assertGreater(raised.exception.sent, 0)
        self.assertGreater(raised.exception.remaining, 0)

    def test_screen_change_guard_rejects_stale_terminal_action(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        runner = InteractiveRunner(self.store, self.transport, object(), action_settle_seconds=0, typing_interval_seconds=0)
        before = runner._pane_snapshot(pane_id)
        self.assertIsNotNone(before)
        runner._screen_before_think = before
        # The screen changes while the model "thinks" (simulate by emitting
        # pane output that changes the captured text).
        self.transport.send_tokens(pane_id, [{"kind": "text", "value": "change"}])
        now = runner._pane_snapshot(pane_id)
        self.assertNotEqual(before, now)
        action = make_action("command", command="pwd", path="")
        self.assertFalse(runner._reject_stale_action("req-a", "resp-a", action))
        self.assertFalse(runner._reject_stale_action("req-b", "resp-b", action))
        stopped = runner._reject_stale_action("req-c", "resp-c", action)
        self.assertTrue(stopped, "three consecutive stale actions must stop the cycle")
        events = self.store.events()
        rejected = [
            e for e in events
            if e["kind"] == "action"
            and e.get("data", {}).get("reason") == "screen_changed_before_exec"
        ]
        self.assertEqual(3, len(rejected))
        self.assertEqual("waiting_for_user", self.store.meeting.phase)
        reasons = [
            e.get("data", {}).get("reason")
            for e in self.store.events()
            if e["kind"] == "state" and e.get("data", {}).get("reason")
        ]
        self.assertIn("screen_changed_repeatedly", reasons)

    def test_progress_rejection_points_at_a_plain_chat(self):
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        self.store.append_screen("bash-3.2$", "bash", str(self.store.workspace_path))
        claimed = make_action("chat", message_type="progress", content="still checking")
        with self.assertRaisesRegex(AgentProtocolError, "send a plain chat or observe"):
            runner._validate_action_for_current_phase(claimed)

    def test_new_message_supersedes_model_result_without_executing_it(self):
        stale_command = make_action("command", command="pwd")
        question = make_action(
            "chat",
            message_type="question",
            content="Should I update the existing file or create a new one?",
        )
        client = BlockingClient([stale_command, question])
        shared_lock = threading.RLock()
        runner = InteractiveRunner(
            self.store,
            self.transport,
            client,
            lock=shared_lock,
            action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        thread = threading.Thread(target=runner.start)
        thread.start()
        self.assertTrue(client.started.wait(2))
        with shared_lock:
            external = MeetingStore.load(self.root, "runner-demo")
            external.append_chat("human", "Use the existing file.", "request")
            external.update(
                request_generation=external.meeting.request_generation + 1,
                cycle_action_count=0,
                cycle_started_at=utc_now(),
            )
        client.release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())

        self.assertFalse(any(item[1] == "pwd" for item in self.transport.commands if item[0] != "create"))
        events = MeetingStore.load(self.root, "runner-demo").events()
        self.assertTrue(
            any(event["kind"] == "action" and event["data"].get("status") == "superseded" for event in events)
        )

    def test_api_error_is_not_retried(self):
        client = FakeClient([ModelApiError("temporary API failure")])
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual(1, len(client.calls))
        self.assertEqual("api_error", loaded.meeting.phase)
        self.assertTrue(loaded.meeting.recovery_required)

    def test_invalid_model_action_is_retried_with_feedback_then_recovers(self):
        client = FakeClient(
            [
                make_action(),
                AgentActionError("command action requires a real bash command line"),
                make_action("command", command="git status"),
                make_action("done", message_type="final", content="Repository inspected."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        rejected = [
            event
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("reason") == "invalid_action"
        ]
        self.assertEqual(1, len(rejected))
        self.assertIn("requires a real bash command line", rejected[0]["content"])
        # The retry request's observation carries the rejection so the model
        # can correct itself.
        self.assertEqual(4, len(client.calls))
        self.assertIn("requires a real bash command line", client.calls[2][2])

    def test_consecutive_invalid_actions_still_fail_the_cycle(self):
        client = FakeClient(
            [make_action()]
            + [AgentActionError(f"invalid action {index}") for index in range(3)]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual(4, len(client.calls))
        self.assertEqual("api_error", loaded.meeting.phase)
        self.assertTrue(loaded.meeting.recovery_required)

    def test_stop_before_worker_start_creates_no_tmux_or_api_conversation(self):
        self.store.request_stop()
        client = FakeClient([make_action()])
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        self.assertFalse(self.transport.exists)
        self.assertEqual([], client.conversations)
        self.assertEqual([], client.calls)

    def test_fresh_recovery_creates_conversation_and_rebuilds_local_context(self):
        self.transport.exists = True
        self.transport.path = str(self.store.workspace_path)
        self.store.set_session("%1")
        self.store.update(
            status="running",
            phase="api_error",
            control_owner="none",
            conversation_id="old-conversation",
            pending_action_id="unresolved-action",
            recovery_required=True,
        )
        self.store.append_chat("actor", "Earlier visible result.", "progress")
        client = FakeClient(
            [
                make_action(
                    "chat",
                    message_type="question",
                    content="I recovered the context. What should I do next?",
                )
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).resume(
            fresh_conversation=True
        )

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("conv-1", loaded.meeting.conversation_id)
        self.assertIn("Earlier visible result.", client.calls[0][2])
        self.assertTrue(
            any(event["kind"] == "action" and event["data"].get("status") == "abandoned" for event in loaded.events())
        )
        self.assertEqual([], self.transport.tokens)

    def test_last_action_budget_allows_question_or_done_without_progress_evidence(self):
        client = FakeClient(
            [
                make_action(),
                make_action(
                    "chat",
                    message_type="question",
                    content="I reached the action budget. Which part should I inspect next?",
                ),
            ]
        )
        with mock.patch("interactive_runner.ACTION_LIMIT", 2):
            InteractiveRunner(
                self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
            ).start()
        self.assertEqual(("chat", "done"), client.calls[1][3]["allowed_types"])
        self.assertEqual(
            ("none", "ack", "progress", "question", "final"),
            client.calls[1][3]["allowed_message_types"],
        )
        self.assertEqual(
            "waiting_for_user",
            MeetingStore.load(self.root, "runner-demo").meeting.phase,
        )

    def test_cycle_budget_uses_monotonic_time_not_manifest_wall_clock(self):
        client = FakeClient([])
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        runner._cycle_started_monotonic = 100.0
        self.store.update(
            current_command="bash",
            cycle_action_count=1,
            cycle_started_at="2000-01-01T00:00:00.000Z",
        )
        with mock.patch("interactive_runner.time.monotonic", return_value=101.0):
            allowed_types, _, budget_final = runner._phase_constraints()
        self.assertEqual(("chat", "command", "diff", "done", "observe"), allowed_types)
        self.assertFalse(budget_final)

    def test_first_cycle_action_is_not_pinned_to_an_acknowledgement(self):
        # Conversation policy lives in the Skill; the runner only reports what
        # the pane allows, so a first action may already be real work.
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        self.store.append_screen("bash-3.2$", "bash", str(self.store.workspace_path))
        allowed_types, allowed_messages, _ = runner._phase_constraints()
        self.assertEqual(("chat", "command", "diff", "done", "observe"), allowed_types)
        self.assertEqual(("none", "ack", "progress", "question", "final"), allowed_messages)

    def test_done_is_the_agents_call_even_without_a_screen_marker(self):
        # The runner no longer audits the final message against a marker; the
        # Skill tells the Agent to claim completion only with visible evidence,
        # and the human confirms the proposal.
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        self.store.append_screen("bash-3.2$", "bash", str(self.store.workspace_path))
        runner._validate_action_for_current_phase(
            make_action("done", message_type="final", content="已回答完，收工。")
        )

    def test_cycle_timeout_is_distinct_and_requires_explicit_recovery(self):
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0, typing_interval_seconds=0
        )
        runner._cycle_timeout("request-timeout")
        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("running", loaded.meeting.status)
        self.assertEqual("cycle_timeout", loaded.meeting.phase)
        self.assertTrue(loaded.meeting.recovery_required)
        self.assertEqual("agent cycle time limit reached", loaded.meeting.failure)

    def test_workspace_paths_cannot_escape(self):
        current = str(self.store.workspace_path)
        with self.assertRaises(RunnerError):
            resolve_workspace_path(self.store, current, "../outside", "cd")
        with self.assertRaises(RunnerError):
            resolve_workspace_path(self.store, current, "/tmp/outside", "cd")
        with self.assertRaises(RunnerError):
            resolve_workspace_path(self.store, current, "missing.py", "python")
        script = self.store.workspace_path / "snake.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        self.assertEqual(
            script.resolve(),
            resolve_workspace_path(self.store, current, "snake.py", "python"),
        )

    def _typed_for_command(self, name, line, backend="trae-acp"):
        """让 actor 只发这一条 `command`，返回真正打进了共享终端的那几行程序名。"""
        store = MeetingStore.create(
            self.root,
            name,
            "Read notes.md and tell me what changed.",
            self.skill,
            f"remotebuddy-{name}",
            "test-model",
            agent_backend=backend,
            agent_provider="deepseek" if backend == "claude-code" else "openai",
        )
        transport = FakeTransport()
        client = FakeClient(
            [
                make_action(),
                make_action("command", command=line),
                make_action("done", message_type="final", content="Said what changed."),
            ]
        )
        client.backend_name = backend
        InteractiveRunner(
            store, transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        self.typed = [item[1] for item in transport.commands if item[0] != "create"]
        self.last_observation = client.calls[2][2]
        return self.typed

    def test_plain_readers_never_reach_the_shared_terminal(self):
        typed = self._typed_for_command("runner-reader", "sed -n '1,40p' notes.md")

        self.assertNotIn("sed", typed)
        self.assertIn("for changes, not for reading", self.last_observation)
        self.assertIn("Read/Glob/Grep", self.last_observation)

    def test_commands_that_change_something_still_reach_the_shared_terminal(self):
        for name, line, program in (
            ("runner-git", "git diff notes.md", "git"),
            ("runner-edit", "sed -i 's/a/b/' notes.md", "sed"),
            ("runner-pipeline", "cat notes.md | wc -l", "cat"),
            ("runner-outside", "cat /etc/hosts", "cat"),
        ):
            with self.subTest(line=line):
                typed = self._typed_for_command(name, line)
                self.assertIn(program, typed)

    def test_a_reader_of_a_file_outside_the_workspace_is_still_allowed(self):
        """外面的东西 backend 自己的工具本来就够不着，终端是唯一那条路。"""
        typed = self._typed_for_command("runner-reader-outside", "sed -n '1,20p' /etc/hosts")

        self.assertIn("sed", typed)

    def test_a_python_one_liner_that_opens_a_file_is_a_reader_too(self):
        typed = self._typed_for_command(
            "runner-reader-python", "python -c \"print(open('notes.md').read())\""
        )

        self.assertNotIn("python", typed)
        self.assertIn("for changes, not for reading", self.last_observation)

    def test_a_backend_without_read_tools_still_reads_through_the_terminal(self):
        """纯对话的 backend 没有自己的读工具：终端是它唯一那条路，不许挡。"""
        typed = self._typed_for_command(
            "runner-reader-fallback", "sed -n '1,40p' notes.md", backend="responses"
        )

        self.assertIn("sed", typed)

    def test_every_action_is_preceded_by_an_investigation_step(self):
        client = InvestigatingClient(
            [
                make_action(),
                make_action("command", command="pwd"),
                make_action("done", message_type="final", content="Done."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual(3, len(client.investigations))
        self.assertIn(INVESTIGATE_STEP_TEXT, client.investigations[0])
        # 行动那一次请求：明说这一步不读，而且带着上一次请求调查的结论。
        self.assertIn(ACT_STEP_TEXT, client.calls[0][2])
        self.assertIn("我看了 notes.md", client.calls[1][2])
        started = [
            event["data"].get("step")
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "request_started"
        ]
        self.assertEqual(["investigate", "act", "investigate", "act", "investigate", "act"], started)
        findings = [
            event
            for event in loaded.events()
            if event["kind"] == "state" and event["data"].get("step") == "investigate"
        ]
        self.assertEqual(3, len(findings))
        self.assertEqual("我看了 notes.md：里面还是空的。", findings[0]["content"])

    def test_a_silent_investigation_leaves_no_record(self):
        """没什么可查的它会静默：那一刻不留空记录，行动步照样接着走。"""
        client = InvestigatingClient(
            [make_action("done", message_type="final", content="Done.")], findings=""
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual(1, len(client.investigations))
        findings = [
            event
            for event in loaded.events()
            if event["kind"] == "state" and event["data"].get("step") == "investigate"
        ]
        self.assertEqual([], findings)
        self.assertEqual(
            ["investigate", "act"],
            [
                event["data"].get("step")
                for event in loaded.events()
                if event["kind"] == "action" and event["data"].get("status") == "request_started"
            ],
        )

    def test_an_action_written_as_text_is_not_a_finding(self):
        """它在调查步把动作写成文字：那不是结论，别记下来当"我做过了"。"""
        client = InvestigatingClient(
            [make_action("command", command="git status")],
            findings='{"action":"command","command":"git status","path":""}',
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        findings = [
            event
            for event in loaded.events()
            if event["kind"] == "state" and event["data"].get("step") == "investigate"
        ]
        self.assertEqual([], findings)
        self.assertTrue(any(item[1] == "git" for item in self.transport.commands))

    def test_the_next_cycle_resumes_the_session_and_does_not_replay_history(self):
        """第二轮带上上一轮的 agent_session_id，而且不再把整段历史重发一遍。"""
        client = RecordingCycleClient([
            make_action("done", message_type="final", content="Done."),
            make_action("done", message_type="final", content="Done again."),
        ])
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        runner.start()
        loaded = MeetingStore.load(self.root, "runner-demo")
        first_session = loaded.meeting.agent_session_id
        self.assertTrue(first_session)

        runner.resume()

        self.assertEqual("", client.cycles[0]["resume"])
        self.assertEqual(first_session, client.cycles[1]["resume"])
        # 续会话那一轮的初始上下文里不该再出现第一次那句人类请求。
        self.assertIn("Create notes.md", client.cycles[0]["context"])
        self.assertNotIn("Create notes.md", client.cycles[1]["context"])

    def test_a_backend_without_tools_is_asked_for_json_and_may_read_in_the_pane(self):
        """没工具的 backend：既不能叫它去调工具，也不能叫它"这里不许读"。

        它手里没有读工具，共享终端就是它唯一能看东西的地方；跟它说"读已经结束"它就只能
        去问人要路径（20260927-193126-f896ad 就是这么卡住的）。
        """
        client = JsonActionClient([make_action("done", message_type="final", content="Done.")])
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        observation = client.calls[0][2]
        self.assertIn(ACT_STEP_OPEN_JSON_TEXT, observation)
        self.assertNotIn("remotebuddy_action", observation)
        self.assertIn("the response schema asks for", observation)
        self.assertIn("is also where you look", observation)
        # 自己有读工具的 backend 仍然是原来那句（共享终端拒收只读命令）。
        self.assertNotIn("Reading is over", observation)

    def test_a_backend_with_tools_is_still_asked_to_call_the_tool(self):
        """有工具的还是原来那句：调 `remotebuddy_action`。"""
        client = InvestigatingClient([make_action("done", message_type="final", content="Done.")])
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        observation = client.calls[0][2]
        self.assertIn("`remotebuddy_action` tool", observation)
        self.assertNotIn("the response schema asks for", observation)

    def test_a_failed_investigation_does_not_kill_the_cycle(self):
        """调查步超时/断掉：记一笔就进行动步，别把整轮弄成 api_error。"""
        client = StuckInvestigationClient([make_action("done", message_type="final", content="Done.")])
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        runner.start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        failed = [
            event for event in loaded.events()
            if event["kind"] == "state" and event["data"].get("status") == "failed"
            and event["data"].get("step") == "investigate"
        ]
        self.assertEqual(1, len(failed))
        self.assertIn("直接进行动步", failed[0]["content"])
        self.assertTrue(self.transport.commands or client.calls)

    def test_the_investigation_step_refuses_every_action(self):
        runner = InteractiveRunner(
            self.store, self.transport, InvestigatingClient([]),
            action_settle_seconds=0, typing_interval_seconds=0,
        )
        answer = runner._investigation_refusal(make_action("command", command="pwd"), {})

        self.assertFalse(answer["success"])
        # pause 必须是 False：True 会让 Claude Code 把这次退回当成"这一次请求的动作"。
        self.assertFalse(answer["pause"])
        self.assertIn("closed in the investigation step", answer["content"])
        rejected = [
            event
            for event in self.store.events()
            if event["kind"] == "action" and event["data"].get("reason") == "investigation_step"
        ]
        self.assertEqual(1, len(rejected))
        self.assertEqual(0, self.store.meeting.cycle_action_count)

    def test_a_backend_without_read_tools_has_no_investigation_step(self):
        """没有自己读工具的 backend：终端就是它唯一的读路，两步拆不开，每一次请求都是行动步。"""
        client = FakeClient([make_action("done", message_type="final", content="Done.")])
        Runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        Runner.start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("responses", loaded.meeting.agent_backend)
        started = [
            event["data"].get("step")
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "request_started"
        ]
        self.assertEqual(["act"], started)

    def test_dynamic_backend_actions_still_pass_through_runner_and_one_pane(self):
        backend = DynamicBackend()
        InteractiveRunner(
            self.store, self.transport, backend, action_settle_seconds=0, typing_interval_seconds=0
        ).start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("codex-app-server", loaded.meeting.agent_backend)
        self.assertEqual("thread-1", loaded.meeting.agent_session_id)
        self.assertEqual("turn-1", loaded.meeting.agent_turn_id)
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        self.assertEqual(3, loaded.meeting.cycle_action_count)
        self.assertEqual(1, len([item for item in self.transport.commands if item[0] == "create"]))
        self.assertTrue(any(item[1] == "pwd" for item in self.transport.commands if item[0] != "create"))
        self.assertEqual(
            ["request", "ack", "final"],
            [event["message_type"] for event in loaded.chat_messages()],
        )
        dynamic_received = [
            event
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("dynamic_tool")
        ]
        self.assertEqual(2, len(dynamic_received))


if __name__ == "__main__":
    unittest.main()


class OpenWorkspaceTest(unittest.TestCase):
    """`runner.open()`：开一块工作区——终端开出来、停在等人说话，agent 不动。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: workspace-collaborator\n---\n", encoding="utf-8")
        self.store = MeetingStore.create(
            self.root, "open-demo", "", skill, "remotebuddy-open-demo", "test-model"
        )
        self.transport = FakeTransport()
        self.client = FakeClient([])

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self):
        return InteractiveRunner(self.store, self.transport, self.client, lock=threading.RLock())

    def test_it_opens_the_pane_and_stops_there(self):
        self.runner().open()
        meeting = MeetingStore.load(self.root, "open-demo").meeting
        self.assertEqual("running", meeting.status)
        self.assertEqual("waiting_for_user", meeting.phase)
        self.assertEqual("none", meeting.control_owner)
        self.assertTrue(meeting.pane_id)
        self.assertEqual("running", meeting.session_state)
        # 一帧真画面已经在 journal 里（页面不再空等），而且一个 agent 回合都没起。
        self.assertTrue([e for e in self.store.events() if e["kind"] == "screen"])
        self.assertEqual([], self.client.calls)

    def test_starting_again_reuses_the_open_pane(self):
        runner = self.runner()
        runner.open()
        pane_id = self.store.meeting.pane_id
        # 同一个工作区不会开出第二个 tmux 会话。
        self.assertEqual(pane_id, runner._ensure_pane())
        self.assertEqual(pane_id, MeetingStore.load(self.root, "open-demo").meeting.pane_id)


class AttachmentObservationTest(unittest.TestCase):
    """带图的消息交给 agent 时，图在哪儿要写在里面（agent 读工作区的文件就能看）。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: workspace-collaborator\n---\n", encoding="utf-8")
        self.store = MeetingStore.create(
            self.root, "image-demo", "看这张", skill, "remotebuddy-image-demo", "test-model",
            workspace=self.root,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_observation_names_the_file(self):
        descriptor = self.store.save_attachment("image/png", b"\x89PNG\r\n\x1a\nbody")
        self.store.append_chat("human", "看这张", "request", attachments=[descriptor])
        runner = InteractiveRunner(self.store, FakeTransport(), FakeClient([]),
                                   lock=threading.RLock())
        events = self.store.events_after(0)
        observation = runner._observation(events, False, force_history=True)
        self.assertIn(str(self.store.attachment_path(descriptor["name"])), observation)
        self.assertIn('"attachments"', observation)
        # 图片是交给模型自己看的：观察里直说一句，别去命令行里找 OCR。
        self.assertIn("look at them directly", observation)
        # journal 本身没有被改写：路径只是交给 agent 时补上的。
        self.assertNotIn("file", self.store.events()[-1]["data"]["attachments"][0])

    def test_the_picture_rides_along_with_the_request(self):
        descriptor = self.store.save_attachment("image/png", b"\x89PNG\r\n\x1a\nbody")
        self.store.append_chat("human", "看这张", "request", attachments=[descriptor])
        client = FakeClient([make_action()])
        runner = InteractiveRunner(self.store, FakeTransport(), client, lock=threading.RLock())
        images = runner._images_of(self.store.events_after(0))
        self.assertEqual([{
            "name": descriptor["name"],
            "media_type": "image/png",
            "path": str(self.store.attachment_path(descriptor["name"])),
        }], images)

        runner._next_backend_action(
            "conv-1", "instructions", "observation", images=images,
            allowed_types=None, allowed_messages=None, timeout=5.0,
            generation=0, input_seq=0,
        )
        self.assertEqual(images, client.last_images)

    def test_a_picture_that_is_too_big_for_one_request_is_left_out(self):
        descriptor = self.store.save_attachment(
            "image/png", b"x" * (MAX_IMAGE_BYTES_PER_TURN + 1))
        self.store.append_chat("human", "看这张", "request", attachments=[descriptor])
        runner = InteractiveRunner(self.store, FakeTransport(), FakeClient([]),
                                   lock=threading.RLock())
        self.assertEqual([], runner._images_of(self.store.events_after(0)))
