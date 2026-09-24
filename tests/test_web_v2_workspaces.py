"""工作区那一层：一块工作区 = 一个目录，会议数据进 `<工作区>/.meeting/`。

服务默认就用项目根当工作区；对话框里填一个目录就换成它（相对路径按默认那个算）。
填过的目录记在默认工作区的 `.meeting/workspaces` 里，所以重启之后它的会议还在列表上；
列表按目录分组，组名是目录的 basename。
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
from web_v2 import WORKSPACE_INDEX, MeetingWebService, WebError, _as_workspace  # noqa: E402

from tests.test_web_v2_http import FakeClient, FakeTransport, RecordingRunner, write_model_config  # noqa: E402


class AsWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        (self.base / "project").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_means_the_default_one(self):
        self.assertEqual(self.base, _as_workspace(None, self.base))
        self.assertEqual(self.base, _as_workspace("", self.base))
        self.assertEqual(self.base, _as_workspace("   ", self.base))

    def test_a_typed_directory_may_be_absolute_or_relative(self):
        self.assertEqual((self.base / "project").resolve(), _as_workspace(str(self.base / "project"), self.base))
        self.assertEqual((self.base / "project").resolve(), _as_workspace("project", self.base))

    def test_a_directory_that_is_not_there_is_rejected(self):
        with self.assertRaisesRegex(WebError, "目录不存在"):
            _as_workspace(str(self.base / "nope"), self.base)


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
        self.other = self.root / "other-project"
        self.other.mkdir()
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

    def start(self, meeting_id, message="Write a note.", workspace=None):
        payload = {"message": message, "meeting_id": meeting_id}
        if workspace is not None:
            payload["workspace"] = str(workspace)
        store = self.service.start(payload)
        self.wait_for_workers()
        return store

    def test_the_default_workspace_is_the_project_root(self):
        store = self.start("meeting-in-root", message="把快速排序写进 quicksort.py")
        self.assertEqual((self.root / ".meeting" / "meeting-in-root").resolve(), store.run_dir)
        # The agent's terminal opens in the workspace root itself, not in the
        # hidden data directory: that is the project being worked on.
        self.assertEqual(str(self.root.resolve()), self.transport.path)
        self.assertFalse((self.root / ".meeting" / "meeting-in-root" / "workspace").exists())

    def test_a_typed_directory_becomes_the_workspace(self):
        store = self.start("meeting-elsewhere", workspace=self.other)
        self.assertEqual((self.other / ".meeting" / "meeting-elsewhere").resolve(), store.run_dir)
        self.assertEqual(self.other.resolve(), store.workspace_path)
        # 默认工作区里只留一个索引文件，这场会不记在它名下。
        self.assertFalse((self.root / ".meeting" / "meeting-elsewhere").exists())
        index = self.root / ".meeting" / WORKSPACE_INDEX
        self.assertIn(str(self.other.resolve()), index.read_text(encoding="utf-8").splitlines())

    def test_a_typed_workspace_survives_a_restart(self):
        self.start("meeting-elsewhere", workspace=self.other)
        restarted = MeetingWebService(
            project_root=self.root,
            model_api_path=self.model_api,
            skill_path=self.skill,
            runner_factory=RecordingRunner,
            client_factory=lambda _config: FakeClient(),
            transport_factory=lambda _binary: self.transport,
        )
        listing = restarted.list_meetings()
        self.assertEqual([], listing["warnings"])
        self.assertEqual(["meeting-elsewhere"], [one["meeting_id"] for one in listing["meetings"]])
        self.assertEqual(str(self.root.resolve()), listing["workspace"])
        self.assertIn(str(self.other.resolve()), listing["workspaces"])

    def test_listing_names_each_meeting_and_its_directory(self):
        self.start("meeting-one", message="把快速排序写进 quicksort.py")
        self.start("meeting-two", message="给 README 补一节用法", workspace=self.other)
        listing = self.service.list_meetings()
        self.assertEqual([], listing["warnings"])
        self.assertEqual(str(self.root.resolve()), listing["workspace"])
        by_id = {one["meeting_id"]: one for one in listing["meetings"]}
        self.assertEqual(str(self.root.resolve()), by_id["meeting-one"]["workspace_root"])
        self.assertEqual("把快速排序写进 quicksort.py", by_id["meeting-one"]["title"])
        self.assertEqual(str(self.other.resolve()), by_id["meeting-two"]["workspace_root"])
        # 组名（basename）由页面算，服务给的是那条数据路径。
        self.assertEqual("other-project", Path(by_id["meeting-two"]["workspace_root"]).name)

    def test_a_directory_that_is_not_there_is_refused(self):
        with self.assertRaisesRegex(WebError, "目录不存在"):
            self.service.start({"message": "hi", "workspace": str(self.root / "elsewhere")})

    def test_the_service_finds_a_meeting_by_id_alone(self):
        store = self.start("meeting-two", workspace=self.other)
        snapshot = self.service.snapshot("meeting-two")
        self.assertEqual(store.meeting.meeting_id, snapshot["meeting"]["meeting_id"])
        self.assertEqual(str(self.other.resolve()), snapshot["meeting"]["workspace_root"])

    def test_a_meeting_without_a_first_message_still_gets_its_directory(self):
        store = self.service.start({})
        self.assertEqual("created", store.meeting.phase)
        self.assertTrue((store.run_dir / "meeting.json").is_file())
        self.assertTrue(os.path.isdir(store.workspace_path))


if __name__ == "__main__":
    unittest.main()


class ChooseDirectoryTest(unittest.TestCase):
    """「选择目录…」走服务端的系统选目录框（macOS 的 Finder），浏览器拿不到绝对路径。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = MeetingWebService(project_root=self.root, workspace=self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def run_osascript(self, stdout="", returncode=0):
        done = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
        return mock.patch("web_v2.subprocess.run", return_value=done)

    def test_the_chosen_directory_is_returned_without_the_trailing_slash(self):
        with self.run_osascript("/tmp/some project/\n") as run:
            self.assertEqual({"directory": "/tmp/some project"}, self.service.choose_directory({}))
        script = run.call_args.args[0][-1]
        self.assertIn("Finder", script)
        self.assertIn("choose folder", script)

    def test_a_cancelled_dialog_changes_nothing(self):
        with self.run_osascript("", returncode=1):
            self.assertEqual({"directory": ""}, self.service.choose_directory({}))

    def test_the_current_directory_is_the_default_location(self):
        mine = self.root / 'with "quotes"'
        mine.mkdir()
        with self.run_osascript(str(mine) + "/") as run:
            self.service.choose_directory({"directory": str(mine)})
        script = run.call_args.args[0][-1]
        self.assertIn("default location", script)
        self.assertIn('\\"quotes\\"', script)

    def test_a_directory_that_is_not_there_is_not_a_default(self):
        with self.run_osascript("/tmp/x/") as run:
            self.service.choose_directory({"directory": str(self.root / "nope")})
        self.assertNotIn("default location", run.call_args.args[0][-1])

    def test_osascript_missing_is_reported(self):
        with mock.patch("web_v2.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(WebError, "osascript"):
                self.service.choose_directory({})
