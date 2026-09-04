import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meeting_v2 import MeetingError, MeetingStore, UnsupportedMeetingError


class MeetingStoreV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test collaborator.\n---\n",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "workspace-demo",
            "Please edit a note.",
            self.skill,
            "remotebuddy-workspace-demo",
            "test-model",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_schema_v2_layout_and_initial_human_message(self):
        manifest = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual("human-agent-workspace", manifest["mode"])
        self.assertEqual("balanced", manifest["typing_profile"])
        self.assertEqual("1", manifest["typing_policy_version"])
        self.assertTrue(self.store.workspace_path.is_dir())
        self.assertTrue(self.store.runtime_skill_path.is_file())
        self.assertFalse((self.store.run_dir / "brief.md").exists())
        self.assertFalse((self.store.run_dir / "turns").exists())
        events = self.store.events()
        self.assertEqual([1], [event["seq"] for event in events])
        self.assertEqual("human", events[0]["actor"])
        self.assertEqual("request", events[0]["message_type"])

    def test_typing_profile_round_trip_and_old_v2_default(self):
        self.store.update(typing_profile="demo")
        self.assertEqual(
            "demo",
            MeetingStore.load(self.root, "workspace-demo").meeting.typing_profile,
        )
        manifest = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        manifest.pop("typing_profile")
        manifest.pop("typing_policy_version")
        self.store.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        loaded = MeetingStore.load(self.root, "workspace-demo")
        self.assertEqual("balanced", loaded.meeting.typing_profile)
        self.assertEqual("1", loaded.meeting.typing_policy_version)

    def test_global_sequence_conversation_and_screen_deduplication(self):
        first = self.store.append_screen(
            "screen A",
            "bash",
            str(self.store.workspace_path),
            change_kind="typing",
            metadata={"planned_delay_ms": 120, "action_id": "action-1"},
        )
        duplicate = self.store.append_screen("screen A", "vim", str(self.store.workspace_path))
        self.store.append_event("action", "agent", "agent_acting", content="not chat")
        self.store.append_chat("agent", "I updated the note.", "final")

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        events = self.store.events()
        self.assertEqual(list(range(1, len(events) + 1)), [event["seq"] for event in events])
        self.assertEqual(1, len([event for event in events if event["kind"] == "screen"]))
        self.assertEqual("typing", first["data"]["change_kind"])
        self.assertEqual(120, first["data"]["planned_delay_ms"])
        loaded = MeetingStore.load(self.root, "workspace-demo")
        self.assertEqual("vim", loaded.meeting.current_command)
        conversation = (loaded.run_dir / "conversation.md").read_text(encoding="utf-8")
        self.assertIn("Please edit a note.", conversation)
        self.assertIn("I updated the note.", conversation)
        self.assertNotIn("not chat", conversation)

    def test_screen_append_uses_tail_sequence_and_defers_full_stream_hash(self):
        initial_hash = self.store.meeting.hashes["streams/events.jsonl"]
        with mock.patch.object(
            self.store, "events", side_effect=AssertionError("screen append must not scan history")
        ):
            screen = self.store.append_screen(
                "screen B",
                "vim",
                str(self.store.workspace_path),
                metadata={"cursor_x": 1, "cursor_y": 2},
            )
        self.assertEqual(2, screen["seq"])
        self.assertEqual(initial_hash, self.store.meeting.hashes["streams/events.jsonl"])

        # Every non-terminal write defers the O(journal) hash; only a terminal
        # status transition refreshes the integrity marker.
        self.store.append_event("action", "harness", "agent_acting")
        self.assertEqual(initial_hash, self.store.meeting.hashes["streams/events.jsonl"])
        self.assertEqual([1, 2, 3], [event["seq"] for event in self.store.events()])
        self.store.update(status="completed", phase="completed")
        self.assertNotEqual(initial_hash, self.store.meeting.hashes["streams/events.jsonl"])

    def test_cursor_movement_produces_a_screen_event_without_content_change(self):
        first = self.store.append_screen(
            "same screen",
            "vim",
            str(self.store.workspace_path),
            metadata={"cursor_x": 0, "cursor_y": 0, "pane_width": 120, "pane_height": 36},
        )
        moved = self.store.append_screen(
            "same screen",
            "vim",
            str(self.store.workspace_path),
            metadata={"cursor_x": 0, "cursor_y": 12, "pane_width": 120, "pane_height": 36},
        )
        duplicate = self.store.append_screen(
            "same screen",
            "vim",
            str(self.store.workspace_path),
            metadata={"cursor_x": 0, "cursor_y": 12, "pane_width": 120, "pane_height": 36},
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(moved)
        self.assertIsNone(duplicate)
        self.assertEqual(12, moved["data"]["cursor_y"])

    def test_stale_store_updates_merge_instead_of_overwriting(self):
        first = MeetingStore.load(self.root, "workspace-demo")
        second = MeetingStore.load(self.root, "workspace-demo")
        first.update(status="running", phase="agent_thinking")
        second.update(control_owner="agent")

        loaded = MeetingStore.load(self.root, "workspace-demo")
        self.assertEqual("running", loaded.meeting.status)
        self.assertEqual("agent_thinking", loaded.meeting.phase)
        self.assertEqual("agent", loaded.meeting.control_owner)

    def test_atomic_manifest_failure_preserves_previous_file(self):
        before = self.store.manifest_path.read_text(encoding="utf-8")
        real_replace = os.replace

        def fail_manifest(source, destination):
            if Path(destination) == self.store.manifest_path:
                raise OSError("simulated atomic replace failure")
            return real_replace(source, destination)

        with mock.patch("meeting_v2.os.replace", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.store.update(status="running")

        self.assertEqual(before, self.store.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.store.run_dir.glob(".meeting.json.*")))

    def test_old_schema_is_rejected_without_modification(self):
        old_dir = self.root / "runs" / "old-meeting"
        old_dir.mkdir()
        old_manifest = old_dir / "meeting.json"
        old_manifest.write_text('{"schema_version":1,"meeting_id":"old-meeting"}\n', encoding="utf-8")
        before = old_manifest.read_bytes()

        with self.assertRaises(UnsupportedMeetingError):
            MeetingStore.load(self.root, "old-meeting")

        self.assertEqual(before, old_manifest.read_bytes())

    def test_agent_ordinal_and_reasoning_effort_round_trip(self):
        store = MeetingStore.create(
            self.root,
            "numbered-agent",
            "Use the shared workspace.",
            self.skill,
            "remotebuddy-numbered-agent",
            "test-model",
            agent_ordinal=3,
            reasoning_effort="high",
        )
        loaded = MeetingStore.load(self.root, store.meeting.meeting_id)
        self.assertEqual(3, loaded.meeting.agent_ordinal)
        self.assertEqual("high", loaded.meeting.reasoning_effort)
        loaded.update(reasoning_effort="low")
        self.assertEqual(
            "low",
            MeetingStore.load(self.root, "numbered-agent").meeting.reasoning_effort,
        )
        loaded.update(reasoning_effort=None)
        self.assertIsNone(
            MeetingStore.load(self.root, "numbered-agent").meeting.reasoning_effort
        )
        with self.assertRaisesRegex(MeetingError, "invalid reasoning effort"):
            loaded.update(reasoning_effort="turbo")
        with self.assertRaisesRegex(MeetingError, "invalid reasoning effort"):
            MeetingStore.create(
                self.root,
                "bad-effort",
                "Use the shared workspace.",
                self.skill,
                "remotebuddy-bad-effort",
                "test-model",
                reasoning_effort="turbo",
            )
        with self.assertRaisesRegex(MeetingError, "invalid agent ordinal"):
            MeetingStore.create(
                self.root,
                "bad-ordinal",
                "Use the shared workspace.",
                self.skill,
                "remotebuddy-bad-ordinal",
                "test-model",
                agent_ordinal=-1,
            )

    def test_old_v2_manifest_defaults_ordinal_and_effort(self):
        payload = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        payload.pop("agent_ordinal", None)
        payload.pop("reasoning_effort", None)
        self.store.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = MeetingStore.load(self.root, "workspace-demo")
        self.assertEqual(0, loaded.meeting.agent_ordinal)
        self.assertIsNone(loaded.meeting.reasoning_effort)

    def test_existing_v2_manifest_defaults_to_responses_backend(self):
        payload = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        for key in (
            "agent_backend",
            "agent_session_id",
            "agent_turn_id",
            "agent_protocol_version",
        ):
            payload.pop(key, None)
        self.store.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = MeetingStore.load(self.root, "workspace-demo")
        self.assertEqual("responses", loaded.meeting.agent_backend)
        self.assertEqual("openai", loaded.meeting.agent_provider)
        self.assertIsNone(loaded.meeting.agent_session_id)

    def test_claude_deepseek_identity_is_persisted(self):
        store = MeetingStore.create(
            self.root,
            "claude-deepseek",
            "Use the shared workspace.",
            self.skill,
            "remotebuddy-claude-deepseek",
            "deepseek-v4-pro[1m]",
            agent_backend="claude-code",
            agent_provider="deepseek",
        )
        loaded = MeetingStore.load(self.root, store.meeting.meeting_id)
        self.assertEqual("claude-code", loaded.meeting.agent_backend)
        self.assertEqual("deepseek", loaded.meeting.agent_provider)
        with self.assertRaisesRegex(MeetingError, "requires deepseek"):
            MeetingStore.create(
                self.root,
                "bad-claude-provider",
                "Use the shared workspace.",
                self.skill,
                "remotebuddy-bad-claude-provider",
                "test-model",
                agent_backend="claude-code",
                agent_provider="openai",
            )

    def test_paths_and_message_limits_are_enforced(self):
        with self.assertRaises(MeetingError):
            MeetingStore.load(self.root, "../escape")
        with self.assertRaises(MeetingError):
            MeetingStore.create(
                self.root,
                "empty-message",
                " ",
                self.skill,
                "remotebuddy-empty-message",
                "test-model",
            )
        large = MeetingStore.create(
            self.root,
            "large-message",
            "x" * (300 * 1024),
            self.skill,
            "remotebuddy-large-message",
            "test-model",
        )
        self.assertEqual(300 * 1024, len(large.chat_messages()[0]["content"]))

    def test_run_directory_symlink_is_rejected(self):
        target = self.root / "other-run"
        target.mkdir()
        (self.root / "runs" / "linked-run").symlink_to(target)
        with self.assertRaisesRegex(MeetingError, "symlink"):
            MeetingStore.load(self.root, "linked-run")


if __name__ == "__main__":
    unittest.main()
