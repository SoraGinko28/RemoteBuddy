"""工作区那一层：会议开在工作区里，数据进 `<workspace>/.meeting/`。

一个工作区可以有好多场会，列表按工作区分组；没有工作区的老会议（`runs/<id>/`）
照旧列在"未分组"里，不因为新布局消失。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from meeting_v2 import MeetingStore  # noqa: E402
from web_v2 import MeetingWebService, WebError, _resolve_workspaces  # noqa: E402

from tests.test_web_v2_http import FakeClient, FakeTransport, RecordingRunner, write_model_config  # noqa: E402


class ResolveWorkspacesTest(unittest.TestCase):
    def test_given_directories_win_and_must_exist(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            one = root / "one"
            one.mkdir()
            gone = root / "gone"
            self.assertEqual([one.resolve()], _resolve_workspaces([one, gone]))

    def test_default_scans_one_level_under_the_root(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for child in ("viba", "RemoteBuddy-core", ".cache"):
                (root / child).mkdir()
            (root / "notes.txt").write_text("x", encoding="utf-8")
            (root / "linked").symlink_to(root / "viba")
            with mock.patch.dict(os.environ, {"REMOTEBUDDY_WORKSPACE_ROOT": str(root)}):
                self.assertEqual(
                    [(root / "RemoteBuddy-core").resolve(), (root / "viba").resolve()],
                    _resolve_workspaces(None),
                )


class WorkspaceWebServiceTest(unittest.TestCase):
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
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.transport = FakeTransport()
        self.service = MeetingWebService(
            project_root=self.root,
            model_api_path=self.model_api,
            skill_path=self.skill,
            runner_factory=RecordingRunner,
            client_factory=lambda _config: FakeClient(),
            transport_factory=lambda _binary: self.transport,
            workspaces=[self.workspace],
        )

    def tearDown(self):
        self.wait_for_workers()
        self.temporary.cleanup()

    def wait_for_workers(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            with self.service.lock:
                active = any(worker.is_alive() for worker in self.service.jobs.values())
            if not active:
                return
            time.sleep(0.01)
        self.fail("background worker did not finish")

    def start(self, meeting_id, message="Write a note.", **extra):
        store = self.service.start(
            {"message": message, "meeting_id": meeting_id, "workspace": str(self.workspace), **extra}
        )
        self.wait_for_workers()
        return store

    def test_a_meeting_opened_in_a_workspace_lives_there(self):
        store = self.start("meeting-in-ws", message="把快速排序写进 quicksort.py")
        self.assertEqual((self.workspace / ".meeting" / "meeting-in-ws").resolve(), store.run_dir)
        # The agent's terminal opens in the workspace root itself, not in the
        # hidden data directory: that is the project being worked on.
        self.assertEqual(str(self.workspace.resolve()), self.transport.path)
        self.assertFalse((self.root / "runs").exists())

    def test_listing_groups_by_workspace_and_names_the_meeting(self):
        self.start("meeting-one", message="把快速排序写进 quicksort.py")
        self.start("meeting-two", message="给 README 补一节用法")
        legacy = MeetingStore.create(
            self.root,
            "legacy-demo",
            "老布局的会。",
            self.skill,
            "remotebuddy-legacy-demo",
            "test-model",
        )
        listing = self.service.list_meetings()
        self.assertEqual([], listing["warnings"])
        self.assertEqual([str(self.workspace.resolve())], listing["workspaces"])
        by_id = {item["meeting_id"]: item for item in listing["meetings"]}
        self.assertEqual(
            {"meeting-one", "meeting-two", "legacy-demo"}, set(by_id)
        )
        self.assertEqual(str(self.workspace.resolve()), by_id["meeting-one"]["workspace_root"])
        self.assertEqual("把快速排序写进 quicksort.py", by_id["meeting-one"]["title"])
        self.assertEqual("给 README 补一节用法", by_id["meeting-two"]["title"])
        self.assertEqual("", by_id["legacy-demo"]["workspace_root"])
        self.assertEqual("老布局的会。", by_id["legacy-demo"]["title"])
        self.assertEqual(str(legacy.run_dir), by_id["legacy-demo"]["run_path"])

    def test_the_service_finds_a_workspace_meeting_by_id_alone(self):
        store = self.start("meeting-in-ws")
        self.assertEqual(store.meeting.meeting_id, self.service.snapshot("meeting-in-ws")["meeting"]["meeting_id"])
        self.assertEqual(str(self.workspace.resolve()), self.service.snapshot("meeting-in-ws")["meeting"]["workspace_root"])

    def test_an_unknown_workspace_is_rejected(self):
        with self.assertRaisesRegex(WebError, "unknown workspace"):
            self.service.start(
                {"message": "hi", "workspace": str(self.root / "elsewhere")}
            )

    def test_no_workspace_keeps_the_old_runs_layout(self):
        store = self.service.start({"message": "hi", "meeting_id": "no-ws"})
        self.wait_for_workers()
        self.assertEqual((self.root / "runs" / "no-ws").resolve(), store.run_dir)
        self.assertEqual(store.run_dir / "workspace", store.workspace_path)


if __name__ == "__main__":
    unittest.main()
