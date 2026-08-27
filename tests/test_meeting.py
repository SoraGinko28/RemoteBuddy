import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from Meeting import (
    IntegrityError,
    InvalidTurnError,
    MeetingError,
    MeetingStore,
    TURN_COMPLETE_MARKER,
)


TURN_SPECS = (
    ("001-proposer", "proposer", "Propose."),
    ("002-reviewer", "reviewer", "Review."),
    ("003-proposer", "proposer", "Revise."),
)


class MeetingStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.brief = self.root / "source-brief.md"
        self.brief.write_text("# Topic\n\nChoose an approach.\n", encoding="utf-8")
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: meeting-participant\ndescription: Test skill.\n---\n",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "test-meeting",
            self.brief,
            self.skill,
            "remotebuddy-test-meeting",
            TURN_SPECS,
            None,
            300.0,
            1.0,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def finish_turn(self, turn_id, speaker, body):
        self.store.start_turn(turn_id)
        self.store.turn_path(turn_id).write_text(
            f"{body}\n\n{TURN_COMPLETE_MARKER}\n", encoding="utf-8"
        )
        content = self.store.read_completed_turn(turn_id)
        event = self.store.append_chat(turn_id, speaker, content)
        self.store.complete_turn(turn_id, event.event_id)
        return event

    def test_serialization_round_trip_and_expected_layout(self):
        self.store.set_panes({"proposer": "%1", "reviewer": "%2"})
        loaded = MeetingStore.load(self.root, "test-meeting")

        self.assertEqual("%1", loaded.meeting.participants["proposer"].pane_id)
        self.assertEqual("running", loaded.meeting.session_state)
        self.assertEqual(3, len(loaded.meeting.turns))
        self.assertEqual(
            self.brief.read_text(encoding="utf-8"),
            (loaded.run_dir / "brief.md").read_text(encoding="utf-8"),
        )
        self.assertTrue(
            (loaded.run_dir / ".agents/skills/meeting-participant/SKILL.md").is_file()
        )
        json.loads(loaded.manifest_path.read_text(encoding="utf-8"))

    def test_create_from_text_reuses_utf8_and_size_validation(self):
        created = MeetingStore.create_from_text(
            self.root,
            "from-web",
            "# Entered in the browser\n",
            self.skill,
            "remotebuddy-from-web",
            TURN_SPECS,
            None,
            12.0,
            0.5,
        )
        self.assertEqual(
            "# Entered in the browser\n",
            (created.run_dir / "brief.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("<web brief>", created.meeting.brief["source_path"])
        with self.assertRaises(MeetingError):
            MeetingStore.create_from_text(
                self.root,
                "empty-web",
                "   ",
                self.skill,
                "remotebuddy-empty-web",
                TURN_SPECS,
                None,
                12.0,
                0.5,
            )

    def test_manifest_id_and_paths_cannot_redirect_load(self):
        payload = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        payload["meeting_id"] = "different-id"
        self.store.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(MeetingError, "does not match"):
            MeetingStore.load(self.root, "test-meeting")

    def test_stop_request_is_durable_and_idempotent(self):
        self.assertFalse(self.store.stop_requested())
        self.store.request_stop()
        first = self.store.stop_request_path.read_text(encoding="utf-8")
        self.assertTrue(self.store.stop_requested())
        self.store.request_stop()
        self.assertTrue(self.store.stop_request_path.read_text(encoding="utf-8"))
        self.assertTrue(first)

    def test_chat_rejects_source_changed_after_snapshot(self):
        self.store.start_turn("001-proposer")
        path = self.store.turn_path("001-proposer")
        path.write_text(
            f"Original\n{TURN_COMPLETE_MARKER}\n", encoding="utf-8"
        )
        body, source_hash = self.store.read_completed_turn_snapshot("001-proposer")
        path.write_text(
            f"Changed\n{TURN_COMPLETE_MARKER}\n", encoding="utf-8"
        )
        with self.assertRaises(IntegrityError):
            self.store.append_chat(
                "001-proposer", "proposer", body, source_sha256=source_hash
            )

    def test_chat_append_rebuilds_conversation_and_hashes_source(self):
        event = self.finish_turn("001-proposer", "proposer", "Initial proposal")

        messages = self.store.chat_messages()
        self.assertEqual(["001-proposer"], [message.turn_id for message in messages])
        self.assertEqual(64, len(event.source_sha256))
        conversation = (self.store.run_dir / "conversation.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## 001-proposer · proposer", conversation)
        self.assertIn("Initial proposal", conversation)
        self.assertNotIn(TURN_COMPLETE_MARKER, conversation)

    def test_screen_capture_deduplicates_unchanged_content(self):
        first = self.store.append_screen("proposer", "%1", "screen A", "startup")
        duplicate = self.store.append_screen("proposer", "%1", "screen A", "startup")
        second = self.store.append_screen("proposer", "%1", "screen B", "turn")

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(first.content_sha256, second.previous_sha256)
        self.assertEqual(2, self.store.stream_event_count("screen:proposer"))

    def test_invalid_turn_markers_are_rejected(self):
        self.store.start_turn("001-proposer")
        path = self.store.turn_path("001-proposer")
        path.write_text(
            f"Body\n{TURN_COMPLETE_MARKER}\n{TURN_COMPLETE_MARKER}\n",
            encoding="utf-8",
        )
        with self.assertRaises(InvalidTurnError):
            self.store.read_completed_turn("001-proposer")

    def test_completion_marker_must_be_the_final_whole_line(self):
        self.store.start_turn("001-proposer")
        path = self.store.turn_path("001-proposer")
        path.write_text(
            f"Body {TURN_COMPLETE_MARKER}\n", encoding="utf-8"
        )
        self.assertIsNone(self.store.read_completed_turn("001-proposer"))

        path.write_text(
            f"Body\n{TURN_COMPLETE_MARKER}\n\n", encoding="utf-8"
        )
        self.assertIsNone(self.store.read_completed_turn("001-proposer"))

    def test_turns_must_start_in_order(self):
        with self.assertRaises(MeetingError):
            self.store.start_turn("002-reviewer")

    def test_protected_input_tampering_is_detected(self):
        self.store.start_turn("001-proposer")
        snapshot = self.store.protected_snapshot()
        (self.store.run_dir / "brief.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.store.assert_protected_unchanged(snapshot)

    def test_unassigned_turn_file_creation_is_detected(self):
        self.store.start_turn("001-proposer")
        snapshot = self.store.protected_snapshot("001-proposer")
        self.store.turn_path("002-reviewer").write_text("injected", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.store.assert_protected_unchanged(snapshot)

    def test_nested_unassigned_turn_file_creation_is_detected(self):
        self.store.start_turn("001-proposer")
        snapshot = self.store.protected_snapshot("001-proposer")
        nested = self.store.run_dir / "turns" / "unexpected" / "note.md"
        nested.parent.mkdir()
        nested.write_text("injected", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.store.assert_protected_unchanged(snapshot)

    def test_failed_atomic_manifest_replace_preserves_previous_state(self):
        before = self.store.manifest_path.read_text(encoding="utf-8")
        real_replace = os.replace

        def fail_manifest_replace(source, destination):
            if Path(destination) == self.store.manifest_path:
                raise OSError("simulated replace failure")
            return real_replace(source, destination)

        with mock.patch("Meeting.os.replace", side_effect=fail_manifest_replace):
            with self.assertRaises(OSError):
                self.store.set_status("running")

        self.assertEqual(before, self.store.manifest_path.read_text(encoding="utf-8"))
        temporary_files = list(self.store.run_dir.glob(".meeting.json.*"))
        self.assertEqual([], temporary_files)


if __name__ == "__main__":
    unittest.main()
