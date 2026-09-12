import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

from meeting_v2 import MeetingStore
from web_v2 import CSRF_HEADER, MeetingWebService, RemoteBuddyHTTPServer, WebError


class FakeClient:
    pass


class FakeTransport:
    def __init__(self):
        self.exists = False
        self.pane_id = "%9"
        self.path = ""
        self.command = "bash"
        self.revision = 0
        self.commands = []
        self.tokens = []
        self.kills = 0

    def create_workspace_session(self, session_name, workdir):
        self.exists = True
        self.path = str(workdir.resolve())
        self.commands.append(("create", session_name, self.path))
        return self.pane_id

    def session_exists(self, _session_name):
        return self.exists

    def pane_info(self, pane_id):
        return {
            "dead": "0" if self.exists and pane_id == self.pane_id else "1",
            "command": self.command,
            "path": self.path,
            "cursor_x": "0",
            "cursor_y": "0",
            "pane_width": "120",
            "pane_height": "36",
        }

    def capture_visible(self, _pane_id):
        return f"fake shared terminal {self.revision}\n{self.command} {self.path}"

    def capture_visible_ansi(self, pane_id):
        return self.capture_visible(pane_id)

    def start_output_pipe(self, pane_id, target_path):
        self.commands.append((pane_id, "pipe", target_path))

    def stop_output_pipe(self, pane_id):
        self.commands.append((pane_id, "unpipe", None))

    def resize_workspace_session(self, session_name, cols, rows):
        self.commands.append((session_name, "resize", cols, rows))

    def send_hex(self, pane_id, hex_text):
        self.tokens.append((pane_id, [{"kind": "hex", "value": hex_text}]))
        self.revision += 1

    def send_workspace_command(self, pane_id, command, path=""):
        self.commands.append((pane_id, command, path))
        self.revision += 1
        if command == "cd":
            self.path = path
        elif command == "vim":
            self.command = "vim"

    def send_tokens(self, pane_id, keys):
        self.tokens.append((pane_id, list(keys)))
        self.revision += 1

    def kill_session(self, _session_name):
        self.kills += 1
        was_running = self.exists
        self.exists = False
        return was_running


class RecordingRunner:
    instances = []

    def __init__(self, store, transport, client, lock=None):
        self.store = store
        self.transport = transport
        self.client = client
        self.lock = lock
        self.resume_modes = []
        RecordingRunner.instances.append(self)

    def start(self):
        pane = self.transport.create_workspace_session(
            self.store.meeting.session_name, self.store.workspace_path
        )
        self.store.set_session(pane)
        self.store.append_screen(
            self.transport.capture_visible(pane),
            self.transport.command,
            self.transport.path,
        )
        self.store.update(status="running", phase="waiting_for_user", control_owner="none")

    def resume(self, fresh_conversation=False):
        self.resume_modes.append(fresh_conversation)
        self.store.update(
            status="running",
            phase="waiting_for_user",
            control_owner="none",
            recovery_required=False,
        )


def write_model_config(root):
    directory = root / "model_api"
    directory.mkdir()
    (directory / "config.toml").write_text(
        'model="gpt-5.6-luna"\n'
        'base_url="https://api.openai.com/v1"\n'
        'request_timeout_seconds=60\n'
        'reasoning_effort="low"\n',
        encoding="utf-8",
    )
    auth = directory / "auth.json"
    auth.write_text('{"api_key":"test-secret"}\n', encoding="utf-8")
    os.chmod(auth, 0o600)
    return directory


class MeetingWebServiceTest(unittest.TestCase):
    def setUp(self):
        RecordingRunner.instances.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\n",
            encoding="utf-8",
        )
        self.model_api = write_model_config(self.root)
        frontend = self.root / "frontend"
        frontend.mkdir()
        (frontend / "workspace.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
        (frontend / "workspace.js").write_text("'use strict';", encoding="utf-8")
        (frontend / "workspace.css").write_text("body{}", encoding="utf-8")
        self.transport = FakeTransport()
        self.service = MeetingWebService(
            project_root=self.root,
            model_api_path=self.model_api,
            skill_path=self.skill,
            runner_factory=RecordingRunner,
            client_factory=lambda _config: FakeClient(),
            transport_factory=lambda _binary: self.transport,
        )

    def tearDown(self):
        for meeting in self.service.list_meetings()["meetings"]:
            if meeting["status"] not in {"completed", "failed", "interrupted"}:
                self.service.stop(meeting["meeting_id"])
        self.wait_for_workers()
        self.temporary.cleanup()

    def wait_for_workers(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            with self.service.lock:
                active = any(worker.is_alive() for worker in self.service.jobs.values())
                queued = bool(self.service.queued_jobs)
                capturing = any(
                    worker.is_alive() for worker in self.service.capture_jobs.values()
                )
            if not active and not queued and not capturing:
                return
            time.sleep(0.01)
        self.fail("background worker did not finish")

    def start_meeting(self, meeting_id="web-demo"):
        store = self.service.start(
            {"message": "Please create a note.", "meeting_id": meeting_id}
        )
        self.wait_for_workers()
        return MeetingStore.load(self.root, store.meeting.meeting_id)

    def test_start_list_snapshot_and_incremental_events(self):
        store = self.start_meeting()
        self.assertEqual(2, store.meeting.schema_version)
        self.assertEqual("waiting_for_user", store.meeting.phase)
        self.assertEqual(1, len(RecordingRunner.instances))

        old = self.root / "runs" / "old-v1"
        old.mkdir()
        (old / "meeting.json").write_text(
            '{"schema_version":1,"meeting_id":"old-v1"}', encoding="utf-8"
        )
        corrupt = self.root / "runs" / "corrupt-v2"
        corrupt.mkdir()
        (corrupt / "meeting.json").write_text(
            '{"schema_version":2,"meeting_id":"corrupt-v2"}', encoding="utf-8"
        )

        listing = self.service.list_meetings()
        self.assertEqual(["web-demo"], [item["meeting_id"] for item in listing["meetings"]])
        self.assertTrue(listing["warnings"])
        snapshot = self.service.snapshot("web-demo")
        # Chat and scores are rebuilt client-side from the event timeline and
        # are intentionally absent from the per-poll snapshot payload.
        self.assertNotIn("chat", snapshot)
        self.assertTrue(snapshot["latest_screen"]["content"])
        all_events = self.service.events("web-demo", 0)
        chats = [event for event in all_events["events"] if event["kind"] == "chat"]
        self.assertEqual("Please create a note.", chats[0]["content"])
        repeated = self.service.events("web-demo", all_events["cursor"])
        self.assertEqual([], repeated["events"])
        self.assertTrue(all_events["revision"])
        with self.assertRaises(WebError):
            self.service.snapshot("old-v1")

    def test_start_accepts_and_persists_typing_profile(self):
        store = self.service.start(
            {
                "message": "Please create a note.",
                "meeting_id": "profile-demo",
                "typing_profile": "demo",
            }
        )
        self.wait_for_workers()
        self.assertEqual("demo", store.meeting.typing_profile)
        self.assertEqual(
            "demo",
            self.service.snapshot("profile-demo")["meeting"]["typing_profile"],
        )

    def test_every_meeting_starts_with_agent_one(self):
        first = self.service.start({"message": "First.", "meeting_id": "ordinal-a"})
        second = self.service.start({"message": "Second.", "meeting_id": "ordinal-b"})
        self.wait_for_workers()
        self.assertEqual(1, first.meeting.agent_ordinal)
        self.assertEqual(1, second.meeting.agent_ordinal)
        self.assertEqual(
            1, self.service.snapshot("ordinal-b")["meeting"]["agent_ordinal"]
        )

    def test_reasoning_effort_creation_and_mid_session_change(self):
        captured = []

        def capturing_factory(config):
            captured.append(config)
            return FakeClient()

        self.service.client_factory = capturing_factory
        store = self.service.start(
            {
                "message": "Please create a note.",
                "meeting_id": "effort-demo",
                "reasoning_effort": "high",
            }
        )
        self.wait_for_workers()
        self.assertEqual("high", store.meeting.reasoning_effort)
        self.assertEqual("high", captured[-1].reasoning_effort)

        self.service.set_reasoning_effort("effort-demo", {"reasoning_effort": "medium"})
        self.service.message("effort-demo", {"message": "Continue please."})
        self.wait_for_workers()
        self.assertEqual("medium", captured[-1].reasoning_effort)

        # null resets to the config.toml default ("low" in this fixture).
        self.service.set_reasoning_effort("effort-demo", {"reasoning_effort": None})
        self.service.message("effort-demo", {"message": "One more."})
        self.wait_for_workers()
        self.assertEqual("low", captured[-1].reasoning_effort)

        with self.assertRaisesRegex(WebError, "reasoning_effort"):
            self.service.set_reasoning_effort("effort-demo", {"reasoning_effort": "turbo"})
        with self.assertRaisesRegex(WebError, "only reasoning_effort"):
            self.service.set_reasoning_effort("effort-demo", {"other": 1})
        with self.assertRaisesRegex(WebError, "reasoning_effort"):
            self.service.start({"message": "Bad.", "reasoning_effort": "turbo"})
        self.service.stop("effort-demo")
        self.wait_for_workers()
        with self.assertRaisesRegex(WebError, "finished"):
            self.service.set_reasoning_effort("effort-demo", {"reasoning_effort": "low"})

    def test_trae_agent_meetings_start_and_support_cli_default_model(self):
        store = self.service.start(
            {
                "message": "Trae backend trial.",
                "meeting_id": "trae-default",
                "agent_backend": "trae-agent",
                "agent_provider": "openai",
                "model": "",
            }
        )
        self.wait_for_workers()
        self.assertEqual("trae-agent", store.meeting.agent_backend)
        self.assertEqual("", store.meeting.model)
        with self.assertRaises(WebError):
            self.service.start(
                {
                    "message": "Bad pair.",
                    "agent_backend": "trae-agent",
                    "agent_provider": "deepseek",
                }
            )

    def test_empty_model_uses_codex_cli_default(self):
        store = self.service.start(
            {
                "message": "Use the CLI default model.",
                "meeting_id": "cli-default",
                "agent_backend": "codex-app-server",
                "agent_provider": "openai",
                "model": "",
            }
        )
        self.wait_for_workers()
        self.assertEqual("", store.meeting.model)
        # Resume paths re-resolve the empty model without raising.
        self.service.message("cli-default", {"message": "Continue."})
        self.wait_for_workers()
        self.assertEqual("", MeetingStore.load(self.root, "cli-default").meeting.model)
        with self.assertRaisesRegex(WebError, "codex-app-server"):
            self.service.start({"message": "Bad.", "model": ""})

    def test_acquire_terminal_keys_and_release_human_control(self):
        self.start_meeting("control-demo")
        acquired = self.service.acquire("control-demo")
        self.assertEqual("human", acquired.meeting.control_owner)
        self.assertEqual("human_control", acquired.meeting.phase)

        store = self.service.terminal_input(
            "control-demo",
            {"keys": ["$text git status", "$ctrl Enter", "$rest 20"]},
        )
        self.assertTrue(self.transport.tokens)
        flattened = [token for (_pane, keys) in self.transport.tokens for token in keys]
        self.assertEqual(
            [{"kind": "literal", "value": "git status"}, {"kind": "key", "value": "Enter"}],
            flattened,
        )
        events = store.events()
        human_actions = [
            event
            for event in events
            if event["kind"] == "action" and event["actor"] == "human"
        ]
        self.assertTrue(
            any(event["data"].get("status") == "completed" for event in human_actions),
            "human key actions must be journaled to completion",
        )
        # The pane was a plain bash shell, yet keys were allowed: human
        # takeover is free-form, unlike the Agent's process-gated actions.
        self.assertEqual("bash", self.transport.command)

        runners_before = len(RecordingRunner.instances)
        released = self.service.release("control-demo")
        self.assertEqual("none", released.meeting.control_owner)
        self.assertEqual("waiting_for_user", released.meeting.phase)
        self.wait_for_workers()
        # Takeover started from an idle meeting, so a plain 交还 must NOT wake
        # the Agent into another autonomous run.
        self.assertEqual(runners_before, len(RecordingRunner.instances))

    def test_takeover_is_refused_when_the_terminal_is_gone(self):
        self.start_meeting("gone-demo")
        before = MeetingStore.load(self.root, "gone-demo").meeting
        # The tmux session was killed outside RemoteBuddy (or the machine
        # restarted); takeover must not pretend to succeed and then fail the
        # meeting through the capture worker.
        self.transport.exists = False
        with self.assertRaises(WebError) as refused:
            self.service.acquire("gone-demo")
        self.assertEqual(410, refused.exception.status)
        after = MeetingStore.load(self.root, "gone-demo").meeting
        self.assertEqual(before.phase, after.phase)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.control_owner, after.control_owner)
        # The refusal records the discovery so the UI stops offering takeover.
        self.assertEqual("stopped", after.session_state)
        self.wait_for_workers()

    def test_terminal_input_requires_human_control(self):
        self.start_meeting("control-gate")
        with self.assertRaises(WebError) as gate:
            self.service.terminal_input("control-gate", {"keys": ["$text ls"]})
        self.assertEqual(409, gate.exception.status)

    def test_terminal_input_validates_keystroke_dsl(self):
        self.start_meeting("control-validate")
        self.service.acquire("control-validate")
        with self.assertRaises(WebError) as short_rest:
            self.service.terminal_input("control-validate", {"keys": ["$rest 5"]})
        self.assertEqual(400, short_rest.exception.status)
        with self.assertRaises(WebError):
            self.service.terminal_input("control-validate", {"keys": ["$ctrl Bogus"]})
        with self.assertRaises(WebError):
            self.service.terminal_input("control-validate", {"keys": []})
        with self.assertRaises(WebError):
            self.service.terminal_input("control-validate", {"kind": "keys", "keys": ["$text x"]})
        self.service.release("control-validate")
        self.wait_for_workers()

    def test_terminal_input_accepts_raw_bytes(self):
        self.start_meeting("control-bytes")
        self.service.acquire("control-bytes")
        store = self.service.terminal_input("control-bytes", {"bytes": "67697420737461747573"})
        sent = [
            token
            for (_pane, keys) in self.transport.tokens
            for token in keys
            if token.get("kind") == "hex"
        ]
        self.assertEqual([{"kind": "hex", "value": "67697420737461747573"}], sent)
        events = store.events()
        human_actions = [
            event for event in events if event["kind"] == "action" and event["actor"] == "human"
        ]
        self.assertTrue(
            any(event["data"].get("status") == "completed" for event in human_actions)
        )
        with self.assertRaises(WebError):
            self.service.terminal_input("control-bytes", {"bytes": "zz"})
        with self.assertRaises(WebError):
            self.service.terminal_input("control-bytes", {"bytes": ""})
        with self.assertRaises(WebError):
            self.service.terminal_input("control-bytes", {"keys": ["$text x"], "bytes": "6769"})
        self.service.release("control-bytes")
        self.wait_for_workers()

    def test_terminal_stream_preparation(self):
        self.start_meeting("stream-gate")
        with self.assertRaises(WebError) as gate:
            self.service.human_stream_start("stream-gate")
        self.assertEqual(409, gate.exception.status)

        self.service.acquire("stream-gate")
        # The browser reports its own viewport; the pane is resized to fit it.
        prepared = self.service.human_stream_start("stream-gate", cols=100, rows=28)
        self.assertTrue(prepared["seed"])
        self.assertEqual(0, prepared["seed_offset"])
        self.assertTrue(prepared["path"])
        self.assertEqual("human", self.service.meeting_live_state("stream-gate")["owner"])
        self.assertTrue(
            any(item[1] == "resize" and item[2:] == (100, 28) for item in self.transport.commands)
        )
        # The output pipe was opened while human owned the terminal and the
        # pre-takeover size is restored by release.
        self.assertTrue(any(item[1] == "pipe" for item in self.transport.commands))
        self.service.release("stream-gate")
        self.wait_for_workers()
        self.assertTrue(any(item[1] == "unpipe" for item in self.transport.commands))
        self.assertTrue(
            any(item[1] == "resize" and item[2:] == (120, 36) for item in self.transport.commands)
        )

    def test_terminal_control_and_keystroke_records(self):
        self.start_meeting("adt-records")
        self.service.acquire("adt-records")
        events = MeetingStore.load(self.root, "adt-records").events()
        controls = [
            e for e in events
            if e["kind"] == "control" and e.get("data", {}).get("status") == "terminal_control"
        ]
        self.assertTrue(controls, "acquire must record a terminal_control")
        self.assertEqual("human", controls[-1]["data"]["user_name"])
        self.assertEqual(9, controls[-1]["data"]["tmux_pane"])
        self.assertTrue(controls[-1]["data"]["tmux_window_name"])
        self.assertTrue(controls[-1]["data"]["start_time"])

        store = self.service.terminal_input(
            "adt-records",
            {
                "bytes": "636c6561720d",
                "strokes": [
                    {"hex": "63", "stroke_time": "2026-09-03T00:00:01.000Z"},
                    {"hex": "6c", "stroke_time": "2026-09-03T00:00:02.000Z"},
                    {"hex": "656172", "stroke_time": "2026-09-03T00:00:03.000Z"},
                    {"hex": "0d", "stroke_time": "2026-09-03T00:00:04.000Z"},
                ],
            },
        )
        actions = [
            e for e in store.events()
            if e["kind"] == "action" and e.get("data", {}).get("status") == "completed"
        ]
        last = actions[-1]["data"]["strokes"]
        self.assertEqual(
            [
                {"user_name": "human", "stroke_time": "2026-09-03T00:00:01.000Z", "data": {"kind": "text", "value": "c"}},
                {"user_name": "human", "stroke_time": "2026-09-03T00:00:02.000Z", "data": {"kind": "text", "value": "l"}},
                {"user_name": "human", "stroke_time": "2026-09-03T00:00:03.000Z", "data": {"kind": "text", "value": "ear"}},
                {"user_name": "human", "stroke_time": "2026-09-03T00:00:04.000Z", "data": {"kind": "ctrl", "value": "Enter"}},
            ],
            last,
        )
        with self.assertRaises(WebError):
            self.service.terminal_input(
                "adt-records",
                {
                    "bytes": "6162",
                    "strokes": [{"hex": "61", "stroke_time": "2026-09-03T00:00:05.000Z"}],
                },
            )
        self.service.release("adt-records")
        self.wait_for_workers()

    def test_message_during_human_control_hands_back_and_resumes(self):
        self.start_meeting("control-message")
        self.service.acquire("control-message")
        self.assertEqual(
            "human",
            MeetingStore.load(self.root, "control-message").meeting.control_owner,
        )
        store = self.service.message("control-message", {"message": "接管结束，继续改 README"})
        self.assertEqual("actor", store.meeting.control_owner)
        loaded = MeetingStore.load(self.root, "control-message")
        controls = [
            e for e in loaded.events()
            if e["kind"] == "control" and e.get("data", {}).get("status") == "terminal_control"
        ]
        self.assertEqual("actor", controls[-1]["data"]["user_name"])
        self.wait_for_workers()
        self.assertEqual([False], RecordingRunner.instances[-1].resume_modes)
        chats = MeetingStore.load(self.root, "control-message").chat_messages()
        self.assertEqual("接管结束，继续改 README", chats[-1]["content"])

    def test_immediate_message_wakes_a_mid_cycle_agent(self):
        """delivery=interrupt wakes the agent; the deferred mode only queues.

        The composer now always sends ``interrupt``, so this is the path every
        human message takes while the agent is working. ``_job_alive`` is
        stubbed because the fake worker exits instantly: without a live job the
        meeting counts as orphaned and the deferred mode would wake it too.
        """
        self.start_meeting("immediate-message")
        store = MeetingStore.load(self.root, "immediate-message")
        with mock.patch.object(self.service, "_job_alive", return_value=True):
            store.update(status="running", phase="agent_thinking", control_owner="actor")
            before = len(RecordingRunner.instances)
            self.service.message(
                "immediate-message",
                {"message": "stop and look at this", "delivery": "next_action"},
            )
            self.wait_for_workers()
            self.assertEqual(before, len(RecordingRunner.instances))
            self.assertEqual(
                "agent_thinking",
                MeetingStore.load(self.root, "immediate-message").meeting.phase,
            )

            store.update(status="running", phase="agent_thinking", control_owner="actor")
            self.service.message(
                "immediate-message",
                {"message": "no really, look now", "delivery": "interrupt"},
            )
            self.wait_for_workers()
            self.assertEqual(before + 1, len(RecordingRunner.instances))
        reloaded = MeetingStore.load(self.root, "immediate-message")
        self.assertEqual("waiting_for_user", reloaded.meeting.phase)
        human = [e for e in reloaded.chat_messages() if e["actor"] == "human"]
        self.assertEqual("no really, look now", human[-1]["content"])

    def test_message_reference_is_derived_from_source_event(self):
        self.start_meeting("reference-demo")
        store = MeetingStore.load(self.root, "reference-demo")
        source = next(event for event in store.events() if event["kind"] == "screen")
        payload = {
            "message": "请看终端这一段",
            "delivery": "next_action",
            "reference": {
                "kind": "screen",
                "event_id": source["event_id"],
                "region": {"start": 0, "end": 10},
                "excerpt": "客户端伪造的摘录不应被保存",
            },
        }
        self.service.message("reference-demo", payload)
        loaded = MeetingStore.load(self.root, "reference-demo")
        message = loaded.chat_messages()[-1]
        reference = message["data"]["reference"]
        self.assertEqual(source["event_id"], reference["event_id"])
        self.assertNotEqual(payload["reference"]["excerpt"], reference["excerpt"])
        self.assertEqual(source["content"][:10].rstrip(), reference["excerpt"].rstrip())
        with self.assertRaisesRegex(WebError, "outside"):
            self.service.message(
                "reference-demo",
                {
                    "message": "bad",
                    "reference": {
                        "kind": "screen",
                        "event_id": source["event_id"],
                        "region": {"start": 0, "end": 99999},
                    },
                },
            )

    def test_api_error_resume_uses_fresh_conversation(self):
        self.start_meeting("recover-demo")
        store = MeetingStore.load(self.root, "recover-demo")
        store.update(
            phase="api_error",
            control_owner="none",
            recovery_required=True,
            failure="timeout",
        )
        self.service.resume("recover-demo")
        self.wait_for_workers()
        self.assertTrue(RecordingRunner.instances[-1].resume_modes[-1])
        self.assertEqual(
            "waiting_for_user",
            MeetingStore.load(self.root, "recover-demo").meeting.phase,
        )

    def test_service_restart_recovers_orphaned_agent_with_fresh_conversation(self):
        self.start_meeting("restart-demo")
        store = MeetingStore.load(self.root, "restart-demo")
        store.update(
            phase="agent_acting",
            control_owner="actor",
            pending_action_id="unknown-action",
            conversation_id="old-conversation",
        )
        restarted = MeetingWebService(
            project_root=self.root,
            model_api_path=self.model_api,
            skill_path=self.skill,
            runner_factory=RecordingRunner,
            client_factory=lambda _config: FakeClient(),
            transport_factory=lambda _binary: self.transport,
        )
        self.service = restarted
        restarted.resume("restart-demo")
        self.wait_for_workers()
        self.assertTrue(RecordingRunner.instances[-1].resume_modes[-1])

        loaded = MeetingStore.load(self.root, "restart-demo")
        loaded.update(phase="agent_thinking", control_owner="actor")
        # A mid-turn meeting with no live worker is an orphaned Agent, so
        # acquire hands the terminal over immediately instead of waiting.
        store = restarted.acquire("restart-demo")
        self.assertEqual("human", store.meeting.control_owner)
        self.assertEqual("human_control", store.meeting.phase)
        # Takeover interrupted an Agent that was mid-work: a plain 交还
        # resumes the Agent where it stopped.
        runners_before = len(RecordingRunner.instances)
        restarted.release("restart-demo")
        self.wait_for_workers()
        self.assertEqual(runners_before + 1, len(RecordingRunner.instances))
        self.assertTrue(RecordingRunner.instances[-1].resume_modes[-1])

    def test_complete_retains_and_cleanup_or_stop_removes_session(self):
        self.start_meeting("complete-demo")
        completed = self.service.complete("complete-demo")
        self.assertEqual("completed", completed.meeting.status)
        self.assertEqual("retained", completed.meeting.session_state)
        self.assertTrue(self.transport.exists)
        with self.assertRaises(WebError):
            self.service.stop("complete-demo")
        cleaned = self.service.cleanup("complete-demo")
        self.assertEqual("stopped", cleaned.meeting.session_state)
        self.assertFalse(self.transport.exists)

        self.transport = FakeTransport()
        self.service.transport_factory = lambda _binary: self.transport
        self.start_meeting("stop-demo")
        first = self.service.stop("stop-demo")
        second = self.service.stop("stop-demo")
        self.assertEqual("interrupted", first.meeting.status)
        self.assertEqual("stopped", second.meeting.session_state)
        self.assertTrue(second.stop_requested())
        self.assertFalse(self.transport.exists)

    def test_validation_and_missing_model_configuration(self):
        with self.assertRaises(WebError):
            self.service.start({"message": ""})
        with self.assertRaises(WebError):
            self.service.start({"brief": "legacy input"})
        with self.assertRaises(WebError):
            self.service.start({"message": "x", "meeting_id": "../escape"})
        with self.assertRaises(WebError):
            self.service.start({"message": "x", "typing_profile": "human"})
        with self.assertRaises(WebError):
            self.service.events("missing", "not-an-int")

        (self.model_api / "auth.json").unlink()
        health = self.service.health()
        self.assertFalse(health["ok"])
        # The config still loads without a key; only the responses combination
        # is blocked, so key-less codex installs stay usable.
        self.assertTrue(next(item for item in health["checks"] if item["name"] == "model-api")["ok"])
        responses_row = health["backends"]["responses/openai"]
        self.assertFalse(responses_row["ok"])
        self.assertTrue(responses_row["needs_api_key"])
        with self.assertRaisesRegex(WebError, "auth.json"):
            self.service.start({"message": "cannot start"})


class HTTPBoundaryTest(unittest.TestCase):
    def setUp(self):
        MeetingWebServiceTest.setUp(self)
        self.server = RemoteBuddyHTTPServer(("127.0.0.1", 0), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        MeetingWebServiceTest.tearDown(self)

    wait_for_workers = MeetingWebServiceTest.wait_for_workers

    def request(self, path, method="GET", body=None, csrf=False, headers=None):
        request_headers = dict(headers or {})
        data = None
        if body is not None:
            data = body.encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if csrf:
            request_headers[CSRF_HEADER] = self.service.csrf_token
        return urllib.request.urlopen(
            urllib.request.Request(
                self.base + path,
                data=data,
                headers=request_headers,
                method=method,
            ),
            timeout=3,
        )

    def error_status(self, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request(*args, **kwargs)
        body = context.exception.read()
        payload = json.loads(body) if body else {}
        return context.exception.code, payload

    def test_session_static_health_and_csrf(self):
        session = json.load(self.request("/api/session"))
        self.assertEqual(self.service.csrf_token, session["csrf_token"])
        self.assertEqual(200, self.request("/").status)
        self.assertEqual("running", json.load(self.request("/api/health"))["service"])
        status, _payload = self.error_status(
            "/api/meetings",
            method="POST",
            body=json.dumps({"message": "topic"}),
        )
        self.assertEqual(403, status)

    def test_create_events_etag_and_boundaries(self):
        response = self.request(
            "/api/meetings",
            method="POST",
            body=json.dumps({"message": "Create a note", "meeting_id": "http-demo"}),
            csrf=True,
        )
        self.assertEqual(201, response.status)
        self.wait_for_workers()
        snapshot = self.request("/api/meetings/http-demo/snapshot")
        etag = snapshot.headers["ETag"]
        self.assertTrue(etag)
        status, _body = self.error_status(
            "/api/meetings/http-demo/snapshot", headers={"If-None-Match": etag}
        )
        self.assertEqual(304, status)

        status, _payload = self.error_status(
            "/api/meetings", method="POST", body="{", csrf=True
        )
        self.assertEqual(400, status)
        status, _payload = self.error_status("/assets/%2e%2e%2fweb.py")
        self.assertEqual(404, status)
        status, _payload = self.error_status("/api/meetings/..%2Fescape/snapshot")
        self.assertIn(status, {400, 404})
        huge = json.dumps({"message": "x" * (1024 * 1024 + 1)})
        status, _payload = self.error_status(
            "/api/meetings", method="POST", body=huge, csrf=True
        )
        self.assertEqual(413, status)


if __name__ == "__main__":
    unittest.main()
