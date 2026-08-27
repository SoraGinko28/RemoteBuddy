import shlex
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from interactive_runner import (
    InteractiveRunner,
    RunnerError,
    TypingInterrupted,
    _send_typing_plan,
    resolve_workspace_path,
    send_command_with_captures,
    send_tokens_with_captures,
    typing_chunks,
    validate_agent_vim_keys,
)
from meeting_v2 import MeetingStore, utc_now
from model_api_client import AgentAction, AgentProtocolError, AgentResponse, ModelApiError
from typing_policy import plan_input


def make_action(action_type="chat", **changes):
    payload = {
        "type": action_type,
        "message_type": "ack" if action_type == "chat" else "none",
        "content": "I will inspect the shared workspace." if action_type == "chat" else "",
        "command": "none",
        "path": "",
        "keys": [],
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

    def create_conversation(self, meeting_id):
        value = f"conv-{len(self.conversations) + 1}"
        self.conversations.append((meeting_id, value))
        return value

    def create_action(self, conversation_id, instructions, observation, **kwargs):
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

    def create_workspace_session(self, session_name, workdir):
        self.exists = True
        self.path = str(workdir.resolve())
        self.commands.append(("create", session_name, self.path))
        return self.pane_id

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

    def capture_visible(self, _pane_id):
        return (
            f"workspace screen {self.screen_revision}\n"
            f"{self.command} {self.path}\n{self.pending_input}\n"
            "PROJECT TESTS PASSED"
        )

    def send_literal(self, pane_id, text):
        self.literal_sends.append((pane_id, text))
        if self.command in {"vim", "vi"} or self.command.startswith("python"):
            self.tokens.append((pane_id, [{"kind": "literal", "value": text}]))
        self.pending_input += text
        self.screen_revision += 1

    def send_keys(self, pane_id, text):
        self.send_literal(pane_id, text)
        self.send_enter(pane_id)

    def send_special(self, pane_id, key):
        if key == "C-c" and self.command in {"bash", "zsh", "sh"}:
            self.tokens.append((pane_id, [{"kind": "key", "value": key}]))
            self.pending_input = ""
            self.screen_revision += 1
            return
        if key == "Enter" and self.command in {"bash", "zsh", "sh"}:
            self.send_enter(pane_id)
            return
        self.tokens.append((pane_id, [{"kind": "key", "value": key}]))
        self.screen_revision += 1

    def send_enter(self, pane_id):
        parts = shlex.split(self.pending_input)
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
        self.tokens.append((pane_id, list(keys)))
        for token in keys:
            if self.command not in {"vim", "vi"}:
                continue
            if token.get("kind") == "key" and token.get("value") == "Escape":
                self.pending_input = ""
            elif token.get("kind") == "literal":
                self.pending_input += token.get("value", "")
            elif token.get("kind") == "key" and token.get("value") == "Enter":
                if self.pending_input in {":q", ":q!", ":wq", ":wq!", ":x", ":x!"}:
                    self.command = "bash"
                self.pending_input = ""
        self.screen_revision += 1


class DelayedVimTransport(FakeTransport):
    """Mimic Vim drawing after the command echo has already been captured."""

    def __init__(self):
        super().__init__()
        self.vim_captures = 0

    def capture_visible(self, _pane_id):
        if self.command != "vim":
            return super().capture_visible(_pane_id)
        self.vim_captures += 1
        if self.vim_captures <= 2:
            return "bash-3.2$ vim -- notes.md"
        return '~\n~\n"notes.md" [New] 0L, 0B'


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

    def test_typing_chunks_preserve_exact_horizontal_whitespace(self):
        text = "  print('hello',  name)\t# note"
        self.assertEqual(text, "".join(typing_chunks(text)))
        self.assertEqual(
            ("  ", "print('hello',  ", "name)\t", "# ", "note"),
            typing_chunks(text),
        )

    def test_literal_and_safe_command_fast_path_preserves_input(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        send_command_with_captures(
            self.store,
            self.transport,
            pane_id,
            "vim",
            str(self.store.workspace_path / "notes file.md"),
            0,
        )
        send_tokens_with_captures(
            self.store,
            self.transport,
            pane_id,
            [{"kind": "literal", "value": "alpha  beta gamma"}],
            typing_interval_seconds=0,
        )

        self.assertTrue(any(item[1] == "vim" for item in self.transport.commands))
        self.assertEqual(
            "alpha  beta gamma",
            self.transport.tokens[-1][1][0]["value"],
        )

    def test_rule_plan_uses_deadlines_and_persists_screen_metadata(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        plan = plan_input(
            [{"kind": "literal", "value": "print(alpha + beta)"}],
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
        self.assertGreaterEqual(len(screens), 6)
        self.assertTrue(
            all(event["data"].get("action_id") == "paced-action" for event in screens)
        )
        self.assertTrue(all(event["data"].get("planned_delay_ms") for event in screens))

    def test_rule_plan_can_interrupt_between_bursts(self):
        pane_id = self.transport.create_workspace_session(
            self.store.meeting.session_name,
            self.store.workspace_path,
        )
        self.store.set_session(pane_id)
        plan = plan_input(
            [{"kind": "literal", "value": "abcdefghijk"}],
            "demo",
            "runner-demo",
            "interrupt-action",
            "code",
        )
        checks = [False, False, True]

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

    def test_control_request_interrupts_partial_shell_command_and_clears_it(self):
        self.store.update(typing_profile="demo")
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
            ]
        )
        shared_lock = threading.RLock()
        runner = InteractiveRunner(
            self.store,
            self.transport,
            client,
            lock=shared_lock,
            action_settle_seconds=0,
        )
        worker = threading.Thread(target=runner.start)
        worker.start()
        deadline = time.monotonic() + 2
        while not self.transport.pending_input and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.transport.pending_input)

        requested_at = time.monotonic()
        with shared_lock:
            external = MeetingStore.load(self.root, "runner-demo")
            external.update(
                control_requested="human",
                request_generation=external.meeting.request_generation + 1,
            )
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - requested_at, 0.2)
        self.assertEqual("", self.transport.pending_input)
        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("human_control", loaded.meeting.phase)
        self.assertEqual("human", loaded.meeting.control_owner)
        self.assertTrue(
            any(
                event["kind"] == "action"
                and event["data"].get("status") == "interrupted"
                and event["data"].get("partial") is True
                for event in loaded.events()
            )
        )

    def test_agent_uses_one_pane_and_done_waits_for_user(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                make_action(
                    "vim_keys",
                    keys=[
                        {"kind": "literal", "value": "i"},
                        {"kind": "literal", "value": "# Notes"},
                        {"kind": "key", "value": "Escape"},
                        {"kind": "literal", "value": ":wq"},
                        {"kind": "key", "value": "Enter"},
                    ],
                ),
                make_action(
                    "done",
                    message_type="final",
                    content="Created the title in notes.md.",
                ),
            ]
        )
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        runner.start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("running", loaded.meeting.status)
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        self.assertEqual("none", loaded.meeting.control_owner)
        self.assertEqual(1, len([item for item in self.transport.commands if item[0] == "create"]))
        self.assertTrue(any(item[1] == "vim" for item in self.transport.commands if item[0] != "create"))
        self.assertTrue(self.transport.tokens)
        self.assertEqual(("chat",), client.calls[0][3]["allowed_types"])
        self.assertEqual(
            ("command", "done"),
            client.calls[1][3]["allowed_types"],
        )
        self.assertEqual(("none", "final"), client.calls[1][3]["allowed_message_types"])
        self.assertEqual(
            ("chat", "vim_keys", "observe"),
            client.calls[2][3]["allowed_types"],
        )
        self.assertIn("Be specific.", client.calls[0][1])
        chat_types = [event["message_type"] for event in loaded.chat_messages()]
        self.assertEqual(["request", "ack", "final"], chat_types)
        statuses = [
            event["data"].get("status")
            for event in loaded.events()
            if event["kind"] == "action"
        ]
        self.assertIn("received", statuses)
        self.assertIn("started", statuses)
        self.assertIn("completed", statuses)
        self.assertTrue(loaded.latest_screen()["content"])

    def test_phase_mismatch_is_rejected_and_agent_replans(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                # This is structurally valid, but invalid once Vim owns the pane.
                make_action("command", command="pwd"),
                make_action(
                    "vim_keys",
                    keys=[
                        {"kind": "literal", "value": ":wq"},
                        {"kind": "key", "value": "Enter"},
                    ],
                ),
                make_action(
                    "done",
                    message_type="final",
                    content="I noticed the stale action and exited Vim safely.",
                ),
            ]
        )
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )

        runner.start()

        loaded = MeetingStore.load(self.root, "runner-demo")
        self.assertEqual("running", loaded.meeting.status)
        self.assertEqual("completion_proposed", loaded.meeting.phase)
        self.assertIsNone(loaded.meeting.failure)
        rejected = [
            event
            for event in loaded.events()
            if event["kind"] == "action"
            and event["data"].get("status") == "rejected"
            and event["data"].get("reason") == "phase_mismatch"
        ]
        self.assertEqual(1, len(rejected))
        self.assertIn("current_command=vim", rejected[0]["content"])
        self.assertEqual("command", rejected[0]["data"]["action"]["type"])
        self.assertEqual(5, len(client.calls))
        self.assertEqual(
            ("chat", "vim_keys", "observe"),
            client.calls[3][3]["allowed_types"],
        )

    def test_progress_must_be_followed_by_terminal_action(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                make_action(
                    "vim_keys",
                    keys=[
                        {"kind": "literal", "value": "i"},
                        {"kind": "literal", "value": "draft"},
                    ],
                ),
                make_action("chat", message_type="progress", content="Vim is now visibly open."),
                make_action(
                    "vim_keys",
                    keys=[
                        {"kind": "literal", "value": ":wq"},
                        {"kind": "key", "value": "Enter"},
                    ],
                ),
                make_action("done", message_type="final", content="Stopped after checking constraints."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        self.assertEqual(
            ("vim_keys", "observe"),
            client.calls[4][3]["allowed_types"],
        )
        self.assertEqual(("none",), client.calls[4][3]["allowed_message_types"])

    def test_agent_capture_pump_records_delayed_vim_redraw(self):
        self.transport = DelayedVimTransport()
        client = PauseOnThirdClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                make_action(
                    "chat",
                    message_type="question",
                    content="Vim is visibly open. Shall I continue editing?",
                ),
            ]
        )
        InteractiveRunner(
            self.store,
            self.transport,
            client,
            action_settle_seconds=0,
            agent_capture_interval_seconds=0.01,
            typing_interval_seconds=0,
        ).start()

        screens = [
            event["content"]
            for event in MeetingStore.load(self.root, "runner-demo").events()
            if event["kind"] == "screen"
        ]
        self.assertIn('~\n~\n"notes.md" [New] 0L, 0B', screens)
        self.assertGreaterEqual(self.transport.vim_captures, 3)

    def test_agent_literalizes_single_printable_key_from_model(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                make_action(
                    "vim_keys",
                    keys=[{"kind": "key", "value": "i"}],
                ),
                make_action("done", message_type="final", content="Inserted text."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        self.assertEqual(
            [{"kind": "literal", "value": "i"}],
            self.transport.tokens[-1][1],
        )

    def test_vim_normal_mode_rejects_shell_text_and_accepts_explicit_insert(self):
        with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": "python bubble_sort.py"}],
                '~\n"bubble_sort.py" [New]',
            )
        validate_agent_vim_keys(
            [
                {"kind": "literal", "value": "i"},
                {"kind": "literal", "value": "print('ok')"},
                {"kind": "key", "value": "Escape"},
                {"kind": "literal", "value": ":wq"},
                {"kind": "key", "value": "Enter"},
            ],
            '~\n"bubble_sort.py" [New]',
        )

    def test_vim_insert_screen_accepts_document_text_but_ex_command_is_restricted(self):
        validate_agent_vim_keys(
            [{"kind": "literal", "value": "python bubble_sort.py"}],
            "python\n-- INSERT --",
        )
        with self.assertRaisesRegex(RunnerError, "already in insert mode"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": "i"}],
                "python\n-- INSERT --",
            )
        with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": ":edit ~/.codex/config.toml"}],
                '~\n"notes.md"',
            )

    def test_vim_safe_recovery_and_split_ex_commands_are_allowed(self):
        validate_agent_vim_keys(
            [
                {"kind": "literal", "value": "ggdG"},
                {"kind": "literal", "value": "i"},
                {"kind": "literal", "value": "replacement"},
                {"kind": "key", "value": "Escape"},
                {"kind": "literal", "value": ":"},
                {"kind": "literal", "value": "wq"},
                {"kind": "key", "value": "Enter"},
            ],
            '~\n"notes.md"',
        )
        with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": "d:!sh"}],
                '~\n"notes.md"',
            )
        validate_agent_vim_keys(
            [
                {"kind": "literal", "value": ":%s/) order/); order/"},
                {"kind": "key", "value": "Enter"},
            ],
            '~\n"notes.md"',
        )
        for unsafe in (":%s/x/\\=system('id')/", ":%s/x/y/|q", ":%s/.*/x/"):
            with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
                validate_agent_vim_keys(
                    [{"kind": "literal", "value": unsafe}],
                    '~\n"notes.md"',
                )
        with self.assertRaisesRegex(RunnerError, "safe allowlist"):
            validate_agent_vim_keys(
                [
                    {"kind": "literal", "value": ":"},
                    {"kind": "literal", "value": "edit ~/.codex/config.toml"},
                    {"kind": "key", "value": "Enter"},
                ],
                '~\n"notes.md"',
            )
        validate_agent_vim_keys(
            [{"kind": "literal", "value": "32G0dd"}],
            '~\n"notes.md"',
        )
        with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": "4G0dd;:!sh"}],
                '~\n"notes.md"',
            )

    def test_vim_forward_search_is_allowed_but_control_text_is_not(self):
        validate_agent_vim_keys(
            [
                {"kind": "literal", "value": "/parse_line"},
                {"kind": "key", "value": "Enter"},
            ],
            '~\n"notes.md"',
        )
        with self.assertRaisesRegex(RunnerError, "unsafe Vim normal-mode literal"):
            validate_agent_vim_keys(
                [{"kind": "literal", "value": "/parse_line\n:!sh"}],
                '~\n"notes.md"',
            )

    def test_runner_rejects_shell_text_in_vim_normal_mode_without_sending_keys(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="bubble_sort.py"),
                make_action(
                    "vim_keys",
                    keys=[{"kind": "literal", "value": "python bubble_sort.py"}],
                ),
                make_action(
                    "chat",
                    message_type="question",
                    content="The unsafe Vim input was rejected. Shall I exit the editor?",
                ),
            ]
        )
        InteractiveRunner(
            self.store,
            self.transport,
            client,
            action_settle_seconds=0,
            typing_interval_seconds=0,
        ).start()

        self.assertEqual([], self.transport.tokens)
        rejected = [
            event
            for event in MeetingStore.load(self.root, "runner-demo").events()
            if event["kind"] == "action" and event["data"].get("status") == "rejected"
        ]
        self.assertEqual(1, len(rejected))
        self.assertIn("unsafe Vim normal-mode literal", rejected[0]["content"])

    def test_each_vim_token_is_captured_as_a_separate_change(self):
        client = FakeClient(
            [
                make_action(),
                make_action("command", command="vim", path="notes.md"),
                make_action(
                    "vim_keys",
                    keys=[
                        {"kind": "key", "value": "Escape"},
                        {"kind": "literal", "value": ":w"},
                        {"kind": "key", "value": "Enter"},
                    ],
                ),
                make_action("done", message_type="final", content="Saved the file."),
            ]
        )
        InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        ).start()
        self.assertEqual(3, len(self.transport.tokens))
        screen_events = [
            event
            for event in MeetingStore.load(self.root, "runner-demo").events()
            if event["kind"] == "screen"
        ]
        self.assertGreaterEqual(len(screen_events), 5)

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
        self.store.append_chat("agent", "Earlier visible result.", "progress")
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
            ("progress", "question", "final"),
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
        runner._cycle_acknowledged = True
        runner._cycle_started_monotonic = 100.0
        self.store.update(
            current_command="bash",
            cycle_action_count=1,
            cycle_started_at="2000-01-01T00:00:00.000Z",
        )
        with mock.patch("interactive_runner.time.monotonic", return_value=101.0):
            allowed_types, _, budget_final = runner._phase_constraints()
        self.assertEqual(("chat", "command", "done"), allowed_types)
        self.assertFalse(budget_final)

    def test_first_cycle_action_requires_ack_before_questions_or_terminal_actions(self):
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        allowed_types, allowed_messages, _ = runner._phase_constraints()
        self.assertEqual(("chat",), allowed_types)
        self.assertEqual(("ack",), allowed_messages)

    def test_done_cannot_claim_a_test_marker_missing_from_latest_screen(self):
        runner = InteractiveRunner(
            self.store, self.transport, FakeClient([]), action_settle_seconds=0,
            typing_interval_seconds=0,
        )
        runner._cycle_acknowledged = True
        self.store.append_screen("bash-3.2$", "bash", str(self.store.workspace_path))
        claimed = make_action(
            "done", message_type="final", content="Finished: PROJECT TESTS PASSED"
        )
        with self.assertRaisesRegex(AgentProtocolError, "not present"):
            runner._validate_action_for_current_phase(claimed)
        self.store.append_screen(
            "PROJECT TESTS PASSED\nbash-3.2$", "bash", str(self.store.workspace_path)
        )
        runner._validate_action_for_current_phase(claimed)

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
            resolve_workspace_path(self.store, current, "../outside", "vim")
        with self.assertRaises(RunnerError):
            resolve_workspace_path(self.store, current, "/tmp/outside", "vim")
        with self.assertRaises(RunnerError):
            resolve_workspace_path(self.store, current, "missing.py", "python")
        script = self.store.workspace_path / "snake.py"
        script.write_text("print('ok')\n", encoding="utf-8")
        self.assertEqual(
            script.resolve(),
            resolve_workspace_path(self.store, current, "snake.py", "python"),
        )

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
