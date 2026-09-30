import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import demo
from meeting import MeetingError, MeetingStore


class DemoV2CliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def create_store(self, meeting_id="cli-demo"):
        return MeetingStore.create(
            self.root,
            meeting_id,
            "Initial request",
            self.skill,
            f"remotebuddy-{meeting_id}",
            "test-model",
        )

    def test_parser_exposes_management_commands_but_no_run(self):
        parser = demo.build_parser()
        action = next(
            item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
        )
        self.assertEqual({"doctor", "status", "stop", "cleanup"}, set(action.choices))
        self.assertNotIn("run", action.choices)

    def test_stop_does_not_claim_cleanup_when_tmux_is_missing(self):
        store = self.create_store()
        store.update(
            status="running",
            phase="waiting_for_user",
            session_state="retained",
        )
        args = argparse.Namespace(meeting_id="cli-demo")
        with mock.patch.object(demo, "PROJECT_ROOT", self.root), mock.patch(
            "demo.shutil.which", return_value=None
        ):
            with self.assertRaises(MeetingError):
                demo.command_stop(args)

        loaded = MeetingStore.load(self.root, "cli-demo")
        self.assertEqual("interrupted", loaded.meeting.status)
        self.assertEqual("retained", loaded.meeting.session_state)
        self.assertTrue(loaded.stop_requested())


if __name__ == "__main__":
    unittest.main()
