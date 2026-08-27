import re
import tempfile
import unittest
from pathlib import Path

from demo import (
    HarnessError,
    MeetingStopped,
    MeetingHarness,
    MeetingRunner,
    READY_PROMPT_TEXT,
    STARTUP_GATE_TEXT,
    TRUST_PROMPT_TEXT,
    TURN_SPECS,
    UPDATE_PROMPT_TEXT,
    positive_float,
)
from Meeting import IntegrityError, InvalidTurnError, MeetingStore, TURN_COMPLETE_MARKER


class FakeTransport:
    def __init__(
        self, run_dir, behavior="success", trust_prompt=False, update_prompt=False
    ):
        self.run_dir = run_dir
        self.behavior = behavior
        self.panes = {"proposer": "%1", "reviewer": "%2"}
        self.participant_by_pane = {value: key for key, value in self.panes.items()}
        self.screens = {
            "proposer": f"Codex proposer ready\n{READY_PROMPT_TEXT}",
            "reviewer": f"Codex reviewer ready\n{READY_PROMPT_TEXT}",
        }
        self.sent = []
        self.entered = []
        self.menu_choices = []
        self.pending_prompts = {}
        self.dead_panes = set()
        if trust_prompt:
            self.screens = {
                participant: f"{TRUST_PROMPT_TEXT}\n{STARTUP_GATE_TEXT}"
                for participant in self.panes
            }
        if update_prompt:
            self.screens = {
                participant: f"{UPDATE_PROMPT_TEXT}\n{STARTUP_GATE_TEXT}"
                for participant in self.panes
            }

    def pane_alive(self, pane_id):
        return pane_id not in self.dead_panes

    def capture(self, pane_id):
        return self.screens[self.participant_by_pane[pane_id]]

    def send_enter(self, pane_id):
        participant = self.participant_by_pane[pane_id]
        prompt = self.pending_prompts.pop(pane_id, None)
        if prompt is not None:
            self._submit_prompt(pane_id, prompt)
            return
        self.entered.append(pane_id)
        self.screens[participant] += f"\nCodex {participant} ready\n{READY_PROMPT_TEXT}"

    def send_menu_choice(self, pane_id, choice):
        self.menu_choices.append((pane_id, choice))
        participant = self.participant_by_pane[pane_id]
        self.screens[participant] += (
            f"\nCodex {participant} ready\n{READY_PROMPT_TEXT}"
        )

    def send_literal(self, pane_id, prompt):
        self.sent.append((pane_id, prompt))
        self.pending_prompts[pane_id] = prompt

    def _submit_prompt(self, pane_id, prompt):
        turn_id = re.search(r"turn_id=([^;]+);", prompt).group(1)
        output_path = re.search(r"output_path=([^;]+);", prompt).group(1)
        participant = self.participant_by_pane[pane_id]

        if self.behavior == "pane-exit":
            self.dead_panes.add(pane_id)
            return
        if self.behavior == "tamper":
            (self.run_dir / "brief.md").write_text("tampered", encoding="utf-8")
            return
        if self.behavior == "invalid-file":
            (self.run_dir / output_path).write_text(
                f"Message for {turn_id}\n\n{TURN_COMPLETE_MARKER}\ntrailing text\n",
                encoding="utf-8",
            )
            self.screens[participant] += f"\nTURN_COMPLETE {turn_id}"
            return
        if self.behavior != "ack-only":
            (self.run_dir / output_path).write_text(
                f"Message for {turn_id}\n\n{TURN_COMPLETE_MARKER}\n",
                encoding="utf-8",
            )
        if self.behavior != "file-only":
            suffix = " extra" if self.behavior == "ack-suffix" else ""
            prefix = "• " if self.behavior == "ack-bullet" else ""
            self.screens[participant] += f"\n{prefix}TURN_COMPLETE {turn_id}{suffix}"


class MeetingHarnessTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.brief = self.root / "brief-source.md"
        self.brief.write_text("# Runtime topic\n", encoding="utf-8")
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: meeting-participant\ndescription: Test skill.\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def make_store(self, meeting_id):
        store = MeetingStore.create(
            self.root,
            meeting_id,
            self.brief,
            self.skill,
            f"remotebuddy-{meeting_id}",
            TURN_SPECS,
            None,
            0.02,
            0.001,
        )
        store.set_panes({"proposer": "%1", "reviewer": "%2"})
        return store

    def run_harness(
        self, meeting_id, behavior="success", trust_prompt=False, update_prompt=False
    ):
        store = self.make_store(meeting_id)
        transport = FakeTransport(
            store.run_dir, behavior, trust_prompt, update_prompt
        )
        harness = MeetingHarness(
            store,
            transport,
            transport.panes,
            turn_timeout=0.02,
            capture_interval=0.001,
            startup_timeout=0.02,
            input_settle_seconds=0.0,
        )
        return store, transport, harness

    def test_startup_rejects_unexpected_trust_prompt(self):
        store, transport, harness = self.run_harness(
            "trust-prompt", trust_prompt=True
        )
        with self.assertRaisesRegex(HarnessError, "unexpected trust prompt"):
            harness.run()

        self.assertEqual([], transport.entered)
        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(0, len(transport.sent))

    def test_startup_skips_update_before_accepting_trust(self):
        store, transport, harness = self.run_harness(
            "update-prompt", update_prompt=True
        )
        harness.run()

        self.assertEqual([("%1", "2"), ("%2", "2")], transport.menu_choices)
        self.assertEqual([], transport.entered)
        self.assertEqual("completed", store.meeting.status)

    def test_ack_must_be_an_exact_normalized_line(self):
        store, transport, harness = self.run_harness("ack-suffix", "ack-suffix")
        with self.assertRaisesRegex(HarnessError, "both completion signals"):
            harness.run()
        self.assertEqual(0, store.stream_event_count("chat"))

        store, transport, harness = self.run_harness("ack-bullet", "ack-bullet")
        harness.run()
        self.assertEqual("completed", store.meeting.status)

    def test_stop_request_wins_before_pane_state(self):
        store, _transport, harness = self.run_harness("requested-stop")
        store.request_stop()
        with self.assertRaises(MeetingStopped):
            harness.run()
        self.assertEqual("interrupted", store.meeting.status)
        self.assertEqual("stopped", store.meeting.session_state)

    def test_runner_cleans_session_when_stop_arrives_during_creation(self):
        store = MeetingStore.create(
            self.root,
            "runner-stop-race",
            self.brief,
            self.skill,
            "remotebuddy-runner-stop-race",
            TURN_SPECS,
            None,
            1.0,
            0.01,
        )

        class RaceTransport:
            def __init__(self):
                self.killed = []

            def create_two_agent_session(self, session_name, run_dir, codex, model):
                store.request_stop()
                return {"proposer": "%1", "reviewer": "%2"}

            def kill_session(self, session_name):
                self.killed.append(session_name)
                return True

            def session_exists(self, session_name):
                return bool(self.killed)

        transport = RaceTransport()
        runner = MeetingRunner(
            store,
            {"codex": "/codex", "tmux": "/tmux"},
            None,
            1.0,
            0.01,
            transport=transport,
        )
        with self.assertRaises(MeetingStopped):
            runner.run()
        self.assertEqual([store.meeting.session_name], transport.killed)
        self.assertEqual("interrupted", store.meeting.status)
        self.assertEqual("stopped", store.meeting.session_state)

    def test_positive_float_rejects_non_finite_values(self):
        for value in ("nan", "inf", "-inf", "0", "-1"):
            with self.assertRaises(Exception):
                positive_float(value)

    def test_old_startup_text_in_capture_history_is_not_replayed(self):
        store, transport, harness = self.run_harness("old-gate")
        transport.screens = {
            participant: (
                f"{UPDATE_PROMPT_TEXT}\n{STARTUP_GATE_TEXT}\n"
                f"Codex {participant} ready\n{READY_PROMPT_TEXT}"
            )
            for participant in transport.panes
        }
        harness.run()

        self.assertEqual([], transport.menu_choices)
        self.assertEqual([], transport.entered)
        self.assertEqual("completed", store.meeting.status)

    def test_successful_fixed_three_turn_meeting(self):
        store, transport, harness = self.run_harness("success")
        harness.run()

        self.assertEqual("completed", store.meeting.status)
        self.assertEqual("retained", store.meeting.session_state)
        self.assertEqual(
            ["001-proposer", "002-reviewer", "003-proposer"],
            [message.turn_id for message in store.chat_messages()],
        )
        self.assertEqual(["completed"] * 3, [turn.status for turn in store.meeting.turns])
        self.assertEqual(3, len(transport.sent))
        for _pane, prompt in transport.sent:
            self.assertNotRegex(prompt, r"TURN_COMPLETE [0-9]")
            self.assertIn("non-interactive Vim Ex", prompt)
        self.assertGreater(store.stream_event_count("screen:proposer"), 0)
        self.assertGreater(store.stream_event_count("screen:reviewer"), 0)
        conversation = (store.run_dir / "conversation.md").read_text(encoding="utf-8")
        self.assertLess(
            conversation.index("001-proposer"), conversation.index("002-reviewer")
        )
        self.assertLess(
            conversation.index("002-reviewer"), conversation.index("003-proposer")
        )

    def test_file_without_terminal_ack_times_out_without_retry(self):
        store, transport, harness = self.run_harness("file-only", "file-only")
        with self.assertRaisesRegex(HarnessError, "both completion signals"):
            harness.run()

        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(1, len(transport.sent))
        self.assertEqual(0, store.stream_event_count("chat"))

    def test_terminal_ack_without_file_fails_immediately_without_retry(self):
        store, transport, harness = self.run_harness("ack-only", "ack-only")
        with self.assertRaisesRegex(InvalidTurnError, "without a valid turn file"):
            harness.run()

        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(1, len(transport.sent))

    def test_terminal_ack_with_invalid_file_fails_immediately(self):
        store, transport, harness = self.run_harness("invalid-file", "invalid-file")
        with self.assertRaisesRegex(InvalidTurnError, "without a valid turn file"):
            harness.run()

        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(1, len(transport.sent))

    def test_pane_exit_fails_immediately_without_retry(self):
        store, transport, harness = self.run_harness("pane-exit", "pane-exit")
        with self.assertRaisesRegex(HarnessError, "pane exited"):
            harness.run()

        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(1, len(transport.sent))

    def test_input_tampering_fails_without_retry(self):
        store, transport, harness = self.run_harness("tamper", "tamper")
        with self.assertRaises(IntegrityError):
            harness.run()

        self.assertEqual("failed", store.meeting.status)
        self.assertEqual(1, len(transport.sent))
        self.assertIn("protected meeting inputs changed", store.meeting.failure)


if __name__ == "__main__":
    unittest.main()
