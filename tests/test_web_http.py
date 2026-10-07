import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from meeting import MAX_ATTACHMENT_BYTES, MeetingStore
from tickets.actor import actor_caps, meeting_actor_caps
from tickets.table import write_table
from tickets.workflow import norm_home
from tickets.values import Store
from web import CSRF_HEADER, MeetingWebService, RemoteBuddyHTTPServer, WebError

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        self.skill_reloads = 0
        self.backend_restarts = 0
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

    def open(self):
        """打开工作区：终端开出来，不叫 agent（第一句话在聊天框里说）。"""
        self.start()

    def request_skill_reload(self):
        self.skill_reloads += 1

    def request_backend_restart(self):
        self.backend_restarts += 1

    def resume(self, fresh_conversation=False):
        self.resume_modes.append(fresh_conversation)
        self.store.update(
            status="running",
            phase="waiting_for_user",
            control_owner="none",
            recovery_required=False,
        )


DRAFT_ORDERS = """\
def quick_sort(items, key=None, reverse=False, bounds=(0, None)):
    lo, hi = bounds
    if hi is None:
        hi = len(items) - 1
    return items
"""


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


def scripted_actor(service, meeting_id, write=None):
    """测试里表里 `agent` 那一行用的那张能力表。

    "写"是脚本化的；题面照真那一侧的做法在 `acknowledge` 那一步说进会议——那一下就是叫醒 agent，
    所以页面那一侧（终端、拿手、消息）走的是同一条路。判语没说不合格时不叫 agent（真那一侧也是）。
    """
    from tickets.binding import text_of

    author = write or (lambda _prompt: DRAFT_ORDERS)

    def writing(instruction):
        with service.tickets_speaking(meeting_id):
            service.message(meeting_id, {"message": instruction, "actor": "actor"})

    def acknowledge(env, about):
        writing(f"照这份题面写一版代码：\n{text_of(env, about)}\n"
                "结果写进 quicksort.py（就地写文件），写完回复 DONE，不要问我问题。")
        return "知道了，这就照题目写一版"

    def write_code(env, task, after):
        return author(text_of(env, task))

    def report_done(env, code, after):
        return "做好了"

    def revise_code(env, code, objection):
        previous = text_of(env, code)
        what = text_of(env, objection)
        if not what:
            return previous                      # 判语没说不合格：照原样，不必再叫一次 agent
        writing(f"判语说这一版不合格：{what}\n照判语改一版：\n{previous}")
        return author(previous)

    return {"acknowledge": acknowledge, "write_code": write_code,
            "report_done": report_done, "revise_code": revise_code}


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
            tickets_caps_factory=lambda _store, meeting_id, _target: scripted_actor(
                self.service, meeting_id),
        )

    def tearDown(self):
        for meeting in self.service.list_meetings()["meetings"]:
            if meeting["status"] not in {"completed", "failed", "interrupted"}:
                self.service.stop(meeting["meeting_id"])
        self.wait_for_workers()
        self.temporary.cleanup()

    def tickets_service(self, caps_factory):
        """同一套测试用的会议服务：表里 `agent` 那一行用给定的那张能力表。"""
        return MeetingWebService(
            project_root=self.root,
            model_api_path=self.model_api,
            skill_path=self.skill,
            runner_factory=RecordingRunner,
            client_factory=lambda _config: FakeClient(),
            transport_factory=lambda _binary: self.transport,
            tickets_caps_factory=caps_factory,
        )

    def test_a_message_runs_as_work_orders(self):
        """工单那一侧：那句话先进 assistant，由它一张一张发单，actor / critic 接单。"""
        words = "写一个快速排序，放到 quicksort.py"
        service = self.tickets_service(lambda _store, _meeting, _target: actor_caps(
            lambda _prompt: DRAFT_ORDERS))
        store = service.start({"message": words, "meeting_id": "orders-demo"})
        self.wait_for_tickets(service, "orders-demo")

        ordered = Path(store.run_dir) / "store"
        written, answered = Store(ordered).read("case/quicksort/drafted", "write_code")
        self.assertTrue(answered)
        self.assertIn("def quick_sort", written)
        _judged, answered = Store(ordered).read("case/quicksort/judged", "judge")
        self.assertTrue(answered)
        self.assertTrue((Path(store.run_dir) / "meeting_record.md").is_file())

        events = service.events("orders-demo", 0)["events"]
        started = [one for one in events if one.get("phase") == "tickets_started"]
        finished = [one for one in events if one.get("phase") == "tickets_finished"]
        self.assertEqual(1, len(started))
        self.assertEqual("ok", finished[-1]["data"]["outcome"])
        self.assertEqual("completion_proposed",
                         MeetingStore.load_in_workspace(self.root, "orders-demo").meeting.phase)

        # 页面那块"工单"读的就是这一份：五张单、案子、谁答的、答了什么
        board = service.tickets("orders-demo")["tickets"]
        self.assertEqual(board["case"], "case/quicksort")
        self.assertEqual(board["target"], "quicksort.py")
        self.assertTrue(board["ran"])
        self.assertEqual([one["ticket"] for one in board["tickets"]],
                         ["write_a_draft", "judge_the_draft", "revise_the_draft",
                          "judge_the_revision", "close_the_meeting"])
        self.assertTrue(all(one["issued"] for one in board["tickets"]))
        judged = next(one for one in board["tickets"] if one["ticket"] == "judge_the_draft")
        answer = judged["answers"][0]
        # 这一版是 4 个参数，规则的上限是 4：判语说合格（true）
        self.assertEqual((answer["op"], answer["role"], answer["answered"], answer["value"]),
                         ("judge", "critic", True, True))
        self.assertTrue(any("- 人类说了一句" in line for line in board["record"]))

    def test_the_service_table_says_who_serves_a_ticket(self):
        """这场会有哪几个服务由规范旁边那张表说：把 critic 那一行换成另一个进程，照样跑到头。

        这一场里没有第二个能答 `judge` 的东西：那张单要真是这个进程答的，整场才到得了 `ok`。
        """
        norm = self.root / "norm"
        shutil.copytree(norm_home(), norm)
        write_table(norm / "services", [
            "actor actor_api agent",
            f"critic critic_api process python -m tickets.critic --workflow {norm}",
        ])
        words = "写一个快速排序，放到 quicksort.py"
        service = self.tickets_service(lambda _store, _meeting, _target: actor_caps(
            lambda _prompt: DRAFT_ORDERS))
        with mock.patch.dict(os.environ, {"REMOTEBUDDY_NORM": str(norm)}):
            store = service.start({"message": words, "meeting_id": "orders-table"})
            self.wait_for_tickets(service, "orders-table")

        ordered = Path(store.run_dir) / "store"
        judged, answered = Store(ordered).read("case/quicksort/judged", "judge")
        self.assertTrue(answered)
        self.assertTrue(judged)
        finished = [one for one in service.events("orders-table", 0)["events"]
                    if one.get("phase") == "tickets_finished"]
        self.assertEqual("ok", finished[-1]["data"]["outcome"])
        record = (Path(store.run_dir) / "meeting_record.md").read_text(encoding="utf-8")
        self.assertIn("- critic（critic）答了", record)

    def test_an_instruction_from_the_order_is_not_a_new_human_message(self):
        """工单派活那句走的也是消息接口：会议得认得出它是工单说的，不再从头发一遍单。"""
        words = "写一个快速排序，放到 quicksort.py"
        seen = {}

        class FakeChannel:
            """真会议那条线的替身：`say` 真的走一遍消息接口。"""

            def __init__(self, service, working):
                self.service = service
                self.working = working

            def say(self, message):
                with self.service.tickets_speaking("orders-instruction"):
                    self.service.message("orders-instruction",
                                         {"message": message, "actor": "actor"})
                seen.setdefault("instructions", []).append(message)

            def wait_for_chat(self, _message_type, timeout=None):
                return "知道了"

            def ensure_work(self, instruction, target, **_kwargs):
                self.say(instruction)
                path = self.working / target
                path.write_text(DRAFT_ORDERS, encoding="utf-8")
                return DRAFT_ORDERS

        working = self.root / "workspace"
        working.mkdir(exist_ok=True)
        service = self.tickets_service(
            lambda _store, _meeting, _target: actor_caps(None) if False else None)
        service.tickets_caps_factory = lambda _store, meeting_id, _target: meeting_actor_caps(
            FakeChannel(service, working), "quicksort.py")
        store = service.start({"message": words, "meeting_id": "orders-instruction"})
        self.wait_for_tickets(service, "orders-instruction")

        events = service.events("orders-instruction", 0)["events"]
        started = [one for one in events if one.get("phase") == "tickets_started"]
        self.assertEqual(1, len(started))                       # 没有再起一趟
        self.assertTrue(seen["instructions"])                   # 题面真发进会议了
        chats = [one for one in events if one["kind"] == "chat"]
        self.assertTrue(any("照这份题面写一版代码" in one["content"] for one in chats))
        self.assertTrue(all(one["actor"] != "human" or "照这份题面" not in one["content"]
                            for one in chats))
        written, answered = Store(Path(store.run_dir) / "store").read(
            "case/quicksort/drafted", "write_code")
        self.assertTrue(answered)
        self.assertIn("def quick_sort", written)

    def test_a_message_that_arrives_mid_run_is_queued(self):
        """一趟还在跑，人又说了话：排着，跑完那一趟接着跑这一句（不是弹一个 409）。"""
        import threading

        gate = threading.Event()
        written: list = []

        def slow_write(prompt):
            written.append(prompt)
            gate.wait(5)
            return DRAFT_ORDERS

        service = self.tickets_service(
            lambda _store, _meeting, _target: actor_caps(slow_write))
        service.start({"message": "写一个快速排序，放到 quicksort.py",
                       "meeting_id": "orders-queued"})
        deadline = time.time() + 5
        while time.time() < deadline and not written:
            time.sleep(0.01)
        self.assertTrue(written)                                  # 头一趟真跑起来了

        service.message("orders-queued", {"message": "再写一版，放到 quicksort.py"})
        self.assertIn("orders-queued", service.ticket_pending)     # 排着，没弹错
        gate.set()
        # 等两趟都跑完：第二趟是在头一趟收尾那一小段里起的，等"没有活着的活"会早一步看到
        reading = Store(Path(service.load_store("orders-queued").run_dir)
                        / "store")
        deadline = time.time() + 30
        stored = ""
        while time.time() < deadline:
            stored, _answered = reading.read("case/quicksort-2", "stated_task")
            if stored == "再写一版，放到 quicksort.py":
                break
            time.sleep(0.02)
        self.assertEqual("再写一版，放到 quicksort.py", stored)     # 第二句真跑过了
        # 两趟都收完尾再走：别让这个用例收场的时候第二趟还在写 store
        finished: list = []
        deadline = time.time() + 30
        while time.time() < deadline:
            finished = [one for one in service.events("orders-queued", 0)["events"]
                        if one.get("phase") == "tickets_finished"]
            if len(finished) == 2:
                break
            time.sleep(0.02)
        started = [one for one in service.events("orders-queued", 0)["events"]
                   if one.get("phase") == "tickets_started"]
        self.assertEqual(2, len(started))
        self.assertEqual(2, len(finished))

    def test_a_pass_that_died_is_taken_up_by_a_fresh_service(self):
        """上一趟断在半路（谁也没接那一步）：换个服务实例起来，store 里欠着的那些接着跑完。"""
        words = "写一个快速排序，放到 quicksort.py"
        broken = self.tickets_service(lambda _store, _meeting, _target: {})   # 谁也不会，一步就停
        store = broken.start({"message": words, "meeting_id": "orders-resume"})
        self.wait_for_tickets(broken, "orders-resume")
        self.assertFalse(Store(Path(store.run_dir) / "store").read(
            "case/quicksort/greeted", "acknowledge")[1])          # 那一步还欠着

        fresh = self.tickets_service(lambda _store, _meeting, _target: actor_caps(
            lambda _prompt: DRAFT_ORDERS))
        self.assertEqual(["orders-resume"], fresh.resume_unfinished_tickets())
        # 等到这一趟真收完尾：`tickets_finished` 那行先落，阶段是紧跟着改的，别抢那几毫秒
        deadline = time.time() + 30
        while time.time() < deadline:
            done = [one for one in fresh.events("orders-resume", 0)["events"]
                    if one.get("phase") == "tickets_finished"
                    and one["data"].get("outcome") == "ok"]
            phase = MeetingStore.load_in_workspace(self.root, "orders-resume").meeting.phase
            if done and phase == "completion_proposed":
                break
            time.sleep(0.02)
        reading = Store(Path(store.run_dir) / "store")
        written, answered = reading.read("case/quicksort/drafted", "write_code")
        self.assertTrue(answered)                                 # 接着跑完了
        self.assertIn("def quick_sort", written)
        self.assertEqual("completion_proposed",
                         MeetingStore.load_in_workspace(self.root, "orders-resume").meeting.phase)

    def test_the_manual_issue_button_has_no_place_in_orders_mode(self):
        """工单模式下发单的只有 assistant：手按那一下要当场说清楚，不去动老那一侧。"""
        service = self.tickets_service(lambda _store, _meeting, _target: actor_caps(
            lambda _prompt: DRAFT_ORDERS))
        service.start({"message": "写一个快速排序，放到 quicksort.py",
                       "meeting_id": "orders-button"})
        self.wait_for_tickets(service, "orders-button")
        with self.assertRaises(WebError) as caught:
            service.issue_ticket("orders-button", "write_a_draft")
        self.assertEqual(409, caught.exception.status)
        self.assertIn("assistant 发单", caught.exception.message)
        # 发单只走 assistant 那一条路：这场会的工单和进度真的落在了会议目录下
        run_dir = Path(service.load_store("orders-button").run_dir)
        self.assertTrue(any((run_dir / "tickets").glob("*/ticket.viba")))
        self.assertTrue(any((run_dir / "ticket_progress").glob("*/*.viba")))

    def test_a_second_message_runs_its_own_pass(self):
        """同一场会里再说一句：再来一趟，有自己的案子。"""
        words = "写一个快速排序，放到 quicksort.py"
        service = self.tickets_service(lambda _store, _meeting, _target: actor_caps(
            lambda _prompt: DRAFT_ORDERS))
        store = service.start({"message": words, "meeting_id": "orders-twice"})
        self.wait_for_tickets(service, "orders-twice")
        service.message("orders-twice", {"message": "再写一版，放到 quicksort.py"})
        deadline = time.time() + 30
        finished: list = []
        while time.time() < deadline:
            finished = [one for one in service.events("orders-twice", 0)["events"]
                        if one.get("phase") == "tickets_finished"]
            if len(finished) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(2, len(finished))
        self.assertEqual(["ok", "ok"], [one["data"]["outcome"] for one in finished])

        reading = Store(Path(store.run_dir) / "store")
        first, answered = reading.read("case/quicksort/drafted", "write_code")
        self.assertTrue(answered)
        second, answered = reading.read("case/quicksort-2/drafted", "write_code")
        self.assertTrue(answered)                        # 第二趟有自己的那几条路
        self.assertIn("def quick_sort", second)

    def test_the_scripted_writer_seam_runs_a_whole_meeting(self):
        """`REMOTEBUDDY_TICKETS_WRITER` 把"写"换成脚本化的：这一侧不叫真 agent 也能跑一整场。"""
        words = "写一个快速排序，放到 quicksort.py"
        service = self.tickets_service(None)
        with mock.patch.dict(os.environ, {"REMOTEBUDDY_TICKETS_WRITER":
                                          "tests.scripted_actor:write"}):
            store = service.start({"message": words, "meeting_id": "orders-scripted"})
            self.wait_for_tickets(service, "orders-scripted")

        written, answered = Store(Path(store.run_dir) / "store").read(
            "case/quicksort/drafted", "write_code")
        self.assertTrue(answered)
        self.assertIn("def quick_sort", written)
        finished = [one for one in service.events("orders-scripted", 0)["events"]
                    if one.get("phase") == "tickets_finished"]
        self.assertEqual("ok", finished[-1]["data"]["outcome"])

    def wait_for_tickets(self, service, meeting_id, timeout=20):
        """等这一场会那趟工单跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with service.lock:
                job = service.ticket_jobs.get(meeting_id)
            if job is None or not job.is_alive():
                return
            time.sleep(0.01)
        self.fail("orders thread did not finish")

    def wait_for_workers(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            with self.service.lock:
                active = any(worker.is_alive() for worker in self.service.jobs.values())
                queued = bool(self.service.queued_jobs)
                capturing = any(
                    worker.is_alive() for worker in self.service.capture_jobs.values()
                )
                # 接单那一趟也在后台跑：它还在写这场会的数据，清理之前得等它。
                taking = any(worker.is_alive()
                             for worker in self.service.ticket_jobs.values())
            if not active and not queued and not capturing and not taking:
                return
            time.sleep(0.01)
        self.fail("background worker did not finish")

    def start_meeting(self, meeting_id="web-demo"):
        store = self.service.start(
            {"message": "Please create a note.", "meeting_id": meeting_id}
        )
        self.wait_for_workers()
        return MeetingStore.load_in_workspace(self.root, store.meeting.meeting_id)

    def test_start_list_snapshot_and_incremental_events(self):
        store = self.start_meeting()
        self.assertEqual(2, store.meeting.schema_version)
        # 这一句话交给 assistant 那一趟：它跑完（这里的两三毫秒）就把这一场报到"可以收尾"，
        # 还没跑完就停在"等下一句"。两种都是活着的会，页面读得到。
        self.assertIn(store.meeting.phase, {"waiting_for_user", "completion_proposed"})
        self.assertEqual(1, len(RecordingRunner.instances))

        # 1 版的清单直接跳过，2 版但读不出来的记一条警告；两个都在工作区的 .meeting/ 下。
        old = self.root / ".meeting" / "old-v1"
        old.mkdir(parents=True)
        (old / "meeting.json").write_text(
            '{"schema_version":1,"meeting_id":"old-v1"}', encoding="utf-8"
        )
        corrupt = self.root / ".meeting" / "corrupt-v2"
        corrupt.mkdir()
        (corrupt / "meeting.json").write_text(
            '{"schema_version":2,"meeting_id":"corrupt-v2"}', encoding="utf-8"
        )

        listing = self.service.list_meetings()
        self.assertEqual(["web-demo"], [item["meeting_id"] for item in listing["meetings"]])
        self.assertTrue(listing["warnings"])
        snapshot = self.service.snapshot("web-demo")
        # Chat is rebuilt client-side from the event timeline and
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
        self.assertEqual("", MeetingStore.load_in_workspace(self.root, "cli-default").meeting.model)
        with self.assertRaisesRegex(WebError, "codex-app-server, trae-agent or trae-acp"):
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
            [{"kind": "text", "value": "git status"}, {"kind": "ctrl", "value": "Enter"}],
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
        # Every takeover and every hand-back is a control record, including a
        # hand-back that nobody picks up: it records that no one holds the pane.
        controls = [
            event for event in released.events()
            if event["kind"] == "control"
            and event.get("data", {}).get("status") == "terminal_control"
        ]
        self.assertEqual(["human", None],
                         [event["data"].get("user_name") for event in controls])

    def test_takeover_reopens_a_terminal_that_went_away(self):
        self.start_meeting("reopen-demo")
        # 终端会话在外面被杀了（tmux 被杀、机器重启）：那条路径还在，接管在原来的目录
        # 重开一个终端，而不是把这场会卡死。
        below = self.root / "project"
        below.mkdir()
        MeetingStore.load_in_workspace(self.root, "reopen-demo").update(current_path=str(below))
        self.transport.exists = False

        store = self.service.acquire("reopen-demo")

        self.assertEqual("human", store.meeting.control_owner)
        self.assertEqual("human_control", store.meeting.phase)
        self.assertEqual("running", store.meeting.session_state)
        self.assertTrue(self.transport.exists)
        self.assertEqual(below.resolve(), Path(self.transport.path).resolve())
        self.assertTrue(store.meeting.pane_id)
        reopened = [
            event for event in store.events()
            if (event.get("data") or {}).get("status") == "terminal_reopened"
        ]
        self.assertEqual(1, len(reopened))
        self.assertEqual(below.resolve(), Path(reopened[0]["data"]["path"]).resolve())
        # 接管期间抓屏 worker 是活着的：交还之后它才停。
        self.service.release("reopen-demo")
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

    def test_terminal_control_is_recorded_and_input_is_not(self):
        self.start_meeting("adt-records")
        self.service.acquire("adt-records")
        events = MeetingStore.load_in_workspace(self.root, "adt-records").events()
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
            "adt-records", {"bytes": "636c6561720d"}  # "clear\r"
        )
        actions = [
            e for e in store.events()
            if e["kind"] == "action" and e.get("data", {}).get("action_id", "").startswith("human-action-")
        ]
        self.assertTrue(actions, "human input must still be booked as an action")
        for event in actions:
            self.assertNotIn("input", event["data"])
            self.assertNotIn("strokes", event["data"])
            self.assertEqual("", event.get("content") or "")
        # Keystrokes are harness-internal: nothing about what was typed
        # may reach the journal, in any event.
        journal = json.dumps(store.events())
        self.assertNotIn("636c656172", journal)
        self.assertNotIn("clear", journal)
        self.assertNotIn("strokes", journal)

    def test_message_during_human_control_hands_back_and_resumes(self):
        self.start_meeting("control-message")
        self.service.acquire("control-message")
        self.assertEqual(
            "human",
            MeetingStore.load_in_workspace(self.root, "control-message").meeting.control_owner,
        )
        store, _agent_error = self.service.message("control-message", {"message": "接管结束，继续改 README"})
        self.assertEqual("actor", store.meeting.control_owner)
        loaded = MeetingStore.load_in_workspace(self.root, "control-message")
        controls = [
            e for e in loaded.events()
            if e["kind"] == "control" and e.get("data", {}).get("status") == "terminal_control"
        ]
        self.assertEqual("actor", controls[-1]["data"]["user_name"])
        self.wait_for_workers()
        self.assertEqual([False], RecordingRunner.instances[-1].resume_modes)
        chats = MeetingStore.load_in_workspace(self.root, "control-message").chat_messages()
        self.assertEqual("接管结束，继续改 README", chats[-1]["content"])

    def test_a_stored_message_is_not_handed_back_when_the_agent_cannot_start(self):
        """配置坏了：话已经收下了，就别回错误。

        回错误的话前端会把刚发出去的话塞回输入框，人以为没发出去，再发一遍 —— 真会里就
        这么重复发出去了三条。这里要的是：消息留在对话里，会议进 api_error 并写着原因，
        响应里带着 agent_error。人那句话现在先进 assistant，所以叫醒 agent 的是会里成员说的那句
        （工单派活那条路），这里就照那个身份发。
        """
        self.start_meeting("bad-config")
        # 会开起来之后才把配置弄坏：请求预算超出允许范围。
        (self.model_api / "config.toml").write_text(
            'model="gpt-5.6-luna"\n'
            'base_url="https://api.openai.com/v1"\n'
            'request_timeout_seconds=99999\n'
            'reasoning_effort="low"\n',
            encoding="utf-8",
        )
        # 人的那句话走 assistant（那条路上 agent 还没被叫）；叫醒 agent 的是工单派活那句，
        # 这里就照会里成员的身份发一句，试的是"起不来时不回错误"。
        store, agent_error = self.service.message("bad-config",
                                                  {"message": "继续", "actor": "actor"})
        self.assertIn("request_timeout_seconds", agent_error)
        self.assertEqual("api_error", store.meeting.phase)
        self.assertTrue(store.meeting.recovery_required)
        self.assertIn("request_timeout_seconds", store.meeting.failure or "")
        chats = MeetingStore.load_in_workspace(self.root, "bad-config").chat_messages()
        self.assertEqual("继续", chats[-1]["content"])
        self.wait_for_workers()

    def test_a_meeting_can_be_deleted_while_another_service_drives_it(self):
        """两个服务共用一块工作区时，删除不该被"这场会归谁驱动"挡住。

        真事：页面上点「删除会话」没删掉 —— 另一个服务（同一个工作区的第二份页面）握着驱动声明，
        删除走到 stop() 就被 409 挡回来了。删除是数据的事，停信号写下去、tmux 收掉，然后照删。
        """
        import json as _json
        import subprocess
        import sys
        import time as _time

        self.start_meeting("delete-while-driven")
        store = MeetingStore.load_in_workspace(self.root, "delete-while-driven")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        (store.run_dir / "driver.json").write_text(
            _json.dumps({
                "pid": child.pid,
                "url": "http://127.0.0.1:9",
                "workspace": str(self.root),
                "meeting_id": "delete-while-driven",
                "heartbeat": _time.time(),
            }),
            encoding="utf-8",
        )
        # 这份声明真的挡得住别的操作：停一下就被挡回来。
        with self.assertRaises(WebError) as caught:
            self.service.stop("delete-while-driven")
        self.assertEqual(409, caught.exception.status)

        self.service.delete_meeting("delete-while-driven")
        self.assertFalse(store.run_dir.exists(), "数据目录该删掉")
        self.assertEqual(
            [], [m for m in self.service.list_meetings()["meetings"]
                 if m["meeting_id"] == "delete-while-driven"]
        )

    def test_immediate_message_wakes_a_mid_cycle_agent(self):
        """delivery=interrupt wakes the agent; the deferred mode only queues.

        The composer now always sends ``interrupt``, so this is the path every
        human message takes while the agent is working. ``_job_alive`` is
        stubbed because the fake worker exits instantly: without a live job the
        meeting counts as orphaned and the deferred mode would wake it too.
        """
        self.start_meeting("immediate-message")
        store = MeetingStore.load_in_workspace(self.root, "immediate-message")
        with mock.patch.object(self.service, "_job_alive", return_value=True):
            store.update(status="running", phase="agent_thinking", control_owner="actor")
            before = len(RecordingRunner.instances)
            self.service.message(
                "immediate-message",
                {"message": "stop and look at this", "delivery": "next_action",
                 "actor": "actor"},
            )
            self.wait_for_workers()
            self.assertEqual(before, len(RecordingRunner.instances))
            self.assertEqual(
                "agent_thinking",
                MeetingStore.load_in_workspace(self.root, "immediate-message").meeting.phase,
            )

            store.update(status="running", phase="agent_thinking", control_owner="actor")
            self.service.message(
                "immediate-message",
                {"message": "no really, look now", "delivery": "interrupt",
                 "actor": "actor"},
            )
            self.wait_for_workers()
            self.assertEqual(before + 1, len(RecordingRunner.instances))
        reloaded = MeetingStore.load_in_workspace(self.root, "immediate-message")
        self.assertEqual("waiting_for_user", reloaded.meeting.phase)
        # 会里成员说的那句记在它自己名下：人说的话和会里派活说的话不再混在一起。
        said = [e for e in reloaded.chat_messages() if e["actor"] == "actor"]
        self.assertEqual("no really, look now", said[-1]["content"])

    def test_message_reference_is_derived_from_source_event(self):
        self.start_meeting("reference-demo")
        store = MeetingStore.load_in_workspace(self.root, "reference-demo")
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
        loaded = MeetingStore.load_in_workspace(self.root, "reference-demo")
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

    def test_message_can_quote_a_terminal_control_line(self):
        """A control line is quoted like a message: same region/excerpt rules."""
        self.start_meeting("control-quote")
        store = MeetingStore.load_in_workspace(self.root, "control-quote")
        store.note_terminal_control("human")
        store.note_terminal_control(None)
        records = [
            event for event in store.events()
            if event["kind"] == "control"
            and event.get("data", {}).get("status") == "terminal_control"
        ]
        self.assertEqual(["你 接管终端", "你 交还终端"],
                         [event["content"] for event in records])
        hand_back = records[-1]
        self.service.message(
            "control-quote",
            {
                "message": "看这一行",
                "references": [
                    {
                        "kind": "terminal_control",
                        "event_id": hand_back["event_id"],
                        "region": {"start": 0, "end": len(hand_back["content"])},
                        "excerpt": "客户端伪造的摘录不应被保存",
                    }
                ],
            },
        )
        reference = MeetingStore.load_in_workspace(self.root, "control-quote").chat_messages()[-1]["data"]["references"][-1]
        self.assertEqual(hand_back["content"], reference["excerpt"])
        self.assertEqual(hand_back["event_id"], reference["event_id"])
        self.assertEqual("", reference["user_name"])
        self.assertEqual(hand_back["data"]["start_time"], reference["start_time"])
        with self.assertRaisesRegex(WebError, "outside"):
            self.service.message(
                "control-quote",
                {
                    "message": "bad",
                    "references": [
                        {
                            "kind": "terminal_control",
                            "event_id": hand_back["event_id"],
                            "region": {"start": 0, "end": 9999},
                        }
                    ],
                },
            )

    def test_api_error_resume_uses_fresh_conversation(self):
        self.start_meeting("recover-demo")
        store = MeetingStore.load_in_workspace(self.root, "recover-demo")
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
            MeetingStore.load_in_workspace(self.root, "recover-demo").meeting.phase,
        )

    def test_service_restart_recovers_orphaned_agent_with_fresh_conversation(self):
        self.start_meeting("restart-demo")
        store = MeetingStore.load_in_workspace(self.root, "restart-demo")
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

        loaded = MeetingStore.load_in_workspace(self.root, "restart-demo")
        loaded.update(phase="agent_thinking", control_owner="actor")
        # The restarted service owns a live driver lease after resume(); make
        # this scenario an actual orphan explicitly.
        (loaded.run_dir / "driver.json").write_text(
            json.dumps({"pid": 999999, "heartbeat": time.time() - 3600}),
            encoding="utf-8",
        )
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

    def test_live_remote_driver_is_not_an_orphan_and_release_resumes_old_conversation(self):
        self.start_meeting("remote-driver-demo")
        store = MeetingStore.load_in_workspace(self.root, "remote-driver-demo")
        store.update(
            phase="agent_thinking",
            control_owner="actor",
            recovery_required=False,
        )
        (store.run_dir / "driver.json").write_text(
            json.dumps({"pid": os.getpid(), "heartbeat": time.time()}),
            encoding="utf-8",
        )

        acquired = self.service.acquire("remote-driver-demo")
        self.assertFalse(acquired.meeting.recovery_required)
        self.assertFalse(self.service._orphaned_agent(acquired, "remote-driver-demo"))
        acquired.update(phase="human_control", control_owner="human")
        with mock.patch.object(self.service, "_ensure_worker") as ensure_worker:
            self.service.release("remote-driver-demo")
        ensure_worker.assert_called_once()
        self.assertFalse(ensure_worker.call_args.kwargs["fresh_conversation"])

    def test_dead_remote_driver_is_an_orphan(self):
        self.start_meeting("dead-driver-demo")
        store = MeetingStore.load_in_workspace(self.root, "dead-driver-demo")
        store.update(phase="agent_thinking", control_owner="actor")
        (store.run_dir / "driver.json").write_text(
            json.dumps({"pid": 999999, "heartbeat": time.time()}),
            encoding="utf-8",
        )

        acquired = self.service.acquire("dead-driver-demo")
        self.assertTrue(acquired.meeting.recovery_required)

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

    def test_meeting_can_be_opened_without_a_first_message(self):
        """打开的是一块工作区：终端当场开出来，agent 等到第一句话才动。

        页面上"打开工作区"走的就是这条路：人先看见自己的项目（一帧真画面已经在
        journal 里），第一句话在聊天框里说，那时才把 agent 叫起来、复用这个 pane。
        """
        store = self.service.start({})
        self.wait_for_workers()
        # The workspace IS the project root now: the meeting's data sits in
        # `<root>/.meeting/<id>/`, and the agent works in `<root>` itself.
        self.assertTrue((self.root / ".meeting" / store.meeting.meeting_id / "meeting.json").is_file())
        reloaded = MeetingStore.load_in_workspace(self.root, store.meeting.meeting_id)
        self.assertEqual(self.root.resolve(), reloaded.workspace_path)
        self.assertEqual("waiting_for_user", reloaded.meeting.phase)
        self.assertEqual([], reloaded.chat_messages())
        self.assertEqual("running", reloaded.meeting.session_state)
        self.assertTrue(reloaded.meeting.pane_id)
        self.assertEqual(str(self.root.resolve()), self.transport.path)
        self.assertTrue([e for e in reloaded.events() if e["kind"] == "screen"])

        before = len(RecordingRunner.instances)
        self.service.message(store.meeting.meeting_id, {"message": "start here"})
        self.wait_for_workers()
        self.assertEqual(before + 1, len(RecordingRunner.instances))
        # 这个 pane 是现成的：是接着它跑（resume），不是再建一个。
        self.assertEqual([False], RecordingRunner.instances[-1].resume_modes)
        loaded = MeetingStore.load_in_workspace(self.root, store.meeting.meeting_id)
        chats = [e for e in loaded.chat_messages() if e["actor"] == "human"]
        self.assertEqual(["start here"], [e["content"] for e in chats])

    def test_an_older_empty_meeting_gets_its_terminal_when_it_is_looked_at(self):
        """更早建的空会话（那时还不开终端）看一眼就把终端补上，页面不用空等画面。"""
        store = MeetingStore.create(
            self.root, "old-empty", " ", self.skill, "remotebuddy-old-empty", "test-model",
            workspace=self.root,
        )
        self.assertEqual("created", store.meeting.phase)
        self.assertIsNone(store.meeting.pane_id)

        snapshot = self.service.snapshot("old-empty")
        self.assertEqual("old-empty", snapshot["meeting"]["meeting_id"])
        self.wait_for_workers()

        reloaded = MeetingStore.load_in_workspace(self.root, "old-empty")
        self.assertTrue(reloaded.meeting.pane_id)
        self.assertEqual("waiting_for_user", reloaded.meeting.phase)
        self.assertTrue([e for e in reloaded.events() if e["kind"] == "screen"])

    def test_validation_and_missing_model_configuration(self):
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
    start_meeting = MeetingWebServiceTest.start_meeting

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

    def test_deleting_a_meeting_works_without_a_request_body(self):
        """删除会话：正文可以不带（前端就漏过一次）。

        真事：页面上点「删除会话」一直 400（`request body must be UTF-8 JSON`），点了没反应。
        """
        self.start_meeting("delete-no-body")
        response = self.request(
            "/api/meetings/delete-no-body/delete", method="POST", body="", csrf=True
        )
        payload = json.load(response)
        self.assertTrue(payload["deleted"], payload)
        self.assertEqual(
            [], [m for m in self.service.list_meetings()["meetings"]
                 if m["meeting_id"] == "delete-no-body"]
        )

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

    def test_asset_change_is_observable_without_reloading_the_page(self):
        first = json.load(self.request("/api/assets/changes"))
        css = self.root / "frontend" / "workspace.css"
        css.write_text("body { color: rgb(1, 2, 3); }", encoding="utf-8")
        changed = json.load(self.request(
            "/api/assets/changes?version=" + urllib.parse.quote(first["version"])
        ))
        self.assertNotEqual(first["version"], changed["version"])
        self.assertNotEqual(
            first["assets"]["workspace.css"], changed["assets"]["workspace.css"]
        )
        with self.request(
            "/assets/workspace.css?v=" + changed["assets"]["workspace.css"]
        ) as response:
            self.assertIn("rgb(1, 2, 3)", response.read().decode("utf-8"))

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

        self.start_meeting("scores-removed")
        before = len(self.service.load_store("scores-removed").events())
        status, _payload = self.error_status(
            "/api/meetings/scores-removed/scores",
            method="POST",
            body=json.dumps({"tag": "competent"}),
            csrf=True,
        )
        self.assertEqual(404, status)
        after = len(self.service.load_store("scores-removed").events())
        self.assertEqual(before, after)

        huge = json.dumps({"message": "x" * (1024 * 1024 + 1)})
        status, _payload = self.error_status(
            "/api/meetings", method="POST", body=huge, csrf=True
        )
        self.assertEqual(413, status)

    def test_skill_reload_and_agent_restart_routes(self):
        # 页面上那两个按钮：更新只换文本，重启要真有一个在跑的进程
        self.request(
            "/api/meetings",
            method="POST",
            body=json.dumps({"message": "写个冒泡排序", "meeting_id": "skill-demo"}),
            csrf=True,
        )
        self.wait_for_workers()
        runtime_skill = self.service.load_store("skill-demo").runtime_skill_path
        self.skill.write_text("---\nname: workspace-collaborator\n---\n新的一句。\n", encoding="utf-8")

        # 没有在跑的进程：更新照样换文本，但不假装它进了某个进程
        response = self.request("/api/meetings/skill-demo/skill/reload", method="POST",
                                body="{}", csrf=True)
        payload = json.load(response)
        self.assertTrue(payload["changed"])
        self.assertEqual(False, payload["agent_running"])
        self.assertEqual("新的一句。\n", runtime_skill.read_text(encoding="utf-8").split("---\n")[-1])
        self.assertFalse(json.load(
            self.request("/api/meetings/skill-demo/skill/reload", method="POST", body="{}",
                         csrf=True))["changed"], "同一份文本不该说变过")

        # 人握着终端的时候不许抢：重启先被挡回来
        self.request("/api/meetings/skill-demo/control/acquire", method="POST",
                     body="{}", csrf=True)
        status, payload = self.error_status("/api/meetings/skill-demo/agent/restart",
                                            method="POST", body="{}", csrf=True)
        self.assertEqual(409, status)
        self.assertIn("交回控制", payload["error"])
        self.request("/api/meetings/skill-demo/control/release", method="POST",
                     body="{}", csrf=True)

        # 没有在跑的进程：按"重启"就现在起一个，用新的 Skill
        response = self.request("/api/meetings/skill-demo/agent/restart", method="POST",
                                body="{}", csrf=True)
        payload = json.load(response)
        self.assertTrue(payload["restarting"])
        self.assertTrue(payload["started"], "空闲时按重启要真的起一个进程")
        self.wait_for_workers()
        self.assertIn("新的一句。",
                      MeetingStore.load_in_workspace(self.root, "skill-demo")
                      .runtime_skill_path.read_text(encoding="utf-8"))

        reloaded = MeetingStore.load_in_workspace(self.root, "skill-demo")
        events = [e for e in reloaded.events() if e.get("phase") == "skill_reload"]
        self.assertTrue(events, "更新要在 journal 里留一条")

    def test_add_workspace_route(self):
        # 页面侧边栏那个「＋」：记下一个目录，不开会
        response = self.request(
            "/api/workspaces",
            method="POST",
            body=json.dumps({"directory": str(self.root)}),
            csrf=True,
        )
        self.assertEqual(200, response.status)
        payload = json.load(response)
        self.assertEqual(str(self.root.resolve()), payload["workspace"])
        self.assertIn(str(self.root.resolve()), payload["workspaces"])
        self.assertEqual([], json.load(self.request("/api/meetings"))["meetings"])
        status, _payload = self.error_status(
            "/api/workspaces",
            method="POST",
            body=json.dumps({"directory": str(self.root / "elsewhere")}),
            csrf=True,
        )
        self.assertEqual(400, status)

    def test_delete_meeting_route_removes_the_data_and_the_row(self):
        """侧边栏那条会话的"删除会话"：数据、终端、列表里那一行一起走。"""
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "写个便签", "meeting_id": "delete-demo"}),
                     csrf=True)
        self.wait_for_workers()
        run_dir = self.root / ".meeting" / "delete-demo"
        self.assertTrue(run_dir.is_dir())

        response = self.request("/api/meetings/delete-demo/delete", method="POST",
                                body="{}", csrf=True)
        payload = json.load(response)
        self.assertEqual(200, response.status)
        self.assertTrue(payload["deleted"])
        self.assertFalse(run_dir.exists())
        self.assertEqual(1, self.transport.kills)
        self.assertEqual(
            [],
            [one["meeting_id"] for one in json.load(self.request("/api/meetings"))["meetings"]],
        )
        # 已经删掉的再删一次：找不到，而不是又删一遍。
        status, _payload = self.error_status("/api/meetings/delete-demo/delete", method="POST",
                                             body="{}", csrf=True)
        self.assertEqual(404, status)

    def test_a_meeting_copied_into_another_workspace_is_listed_once(self):
        """整目录拷过去的那种副本：会议号是这层接口唯一的名字，所以一个号只出现一次，
        留下的是真正属于那个工作区的一份。"""
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "写个便签", "meeting_id": "copy-demo"}),
                     csrf=True)
        self.wait_for_workers()
        other = self.root / "second"
        (other / ".meeting").mkdir(parents=True)
        self.request("/api/workspaces", method="POST",
                     body=json.dumps({"directory": str(other)}), csrf=True)
        shutil.copytree(self.root / ".meeting" / "copy-demo", other / ".meeting" / "copy-demo")

        listed = json.load(self.request("/api/meetings"))["meetings"]
        self.assertEqual(["copy-demo"], [one["meeting_id"] for one in listed])
        self.assertEqual(str(self.root.resolve()), listed[0]["workspace_root"])
        self.assertEqual((self.root / ".meeting" / "copy-demo").resolve(),
                         self.service._open_store("copy-demo").run_dir)

    def test_a_meeting_being_driven_elsewhere_refuses_local_writes(self):
        """两个服务共享同一个工作区时：读照旧，写只有一个能写。

        另一个服务正驱着这场会（pid 活着、心跳新鲜）时，这边的写要明确拒绝，
        并报出它的地址——不然两个 CLI 会往同一个 tmux pane 里各打各的。
        """
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "写个便签", "meeting_id": "share-demo"}),
                     csrf=True)
        self.wait_for_workers()
        run_dir = self.root / ".meeting" / "share-demo"
        (run_dir / "driver.json").write_text(
            json.dumps({"pid": 1, "url": "http://127.0.0.1:9999", "heartbeat": time.time()}),
            encoding="utf-8",
        )

        status, payload = self.error_status("/api/meetings/share-demo/agent/resume",
                                            method="POST", body="{}", csrf=True)

        self.assertEqual(409, status)
        self.assertIn("127.0.0.1:9999", payload["error"])
        # 收尾前把那份"别的服务"的假声明撤掉，否则 tearDown 的停止也会被它挡住。
        (run_dir / "driver.json").unlink()
        self.wait_for_workers()

    def test_a_stale_claim_can_be_taken_over(self):
        """写声明的进程没了或心跳过期：声明作废，这边可以接手。"""
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "写个便签", "meeting_id": "stale-demo"}),
                     csrf=True)
        self.wait_for_workers()
        run_dir = self.root / ".meeting" / "stale-demo"
        driver = run_dir / "driver.json"
        driver.write_text(
            json.dumps({"pid": 1, "url": "http://127.0.0.1:9999",
                        "heartbeat": time.time() - 3600}),
            encoding="utf-8",
        )

        response = self.request("/api/meetings/stale-demo/agent/resume",
                                method="POST", body="{}", csrf=True)

        self.assertEqual(200, response.status)
        self.assertEqual(os.getpid(), json.loads(driver.read_text(encoding="utf-8"))["pid"])
        self.wait_for_workers()

    def test_the_claim_is_released_when_nothing_is_running(self):
        """一轮跑完就把声明撤掉：闲着的会谁先动手谁驱。"""
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "写个便签", "meeting_id": "free-demo"}),
                     csrf=True)
        self.wait_for_workers()

        driver = self.root / ".meeting" / "free-demo" / "driver.json"
        self.assertFalse(driver.exists())
        self.assertEqual("", self.service.driver_url("free-demo"))

    def test_meetings_can_be_dragged_into_an_order(self):
        for suffix in ("a", "b", "c"):
            self.request("/api/meetings", method="POST",
                         body=json.dumps({"message": f"写个便签 {suffix}",
                                          "meeting_id": f"order-{suffix}"}),
                         csrf=True)
        self.wait_for_workers()
        listed = json.load(self.request("/api/meetings"))["meetings"]
        self.assertEqual(["order-a", "order-b", "order-c"],
                         sorted(one["meeting_id"] for one in listed))

        response = self.request(
            "/api/meetings/order",
            method="POST",
            body=json.dumps({"workspace": str(self.root),
                             "meetings": ["order-a", "order-c", "order-b"]}),
            csrf=True,
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            ["order-a", "order-c", "order-b"],
            [one["meeting_id"] for one in json.load(self.request("/api/meetings"))["meetings"]],
        )
        # 拖动之后新开的会不在那份顺序里：照旧排在最上面，不会挤掉谁。
        self.request("/api/meetings", method="POST",
                     body=json.dumps({"message": "又一个", "meeting_id": "order-d"}), csrf=True)
        self.wait_for_workers()
        self.assertEqual(
            ["order-d", "order-a", "order-c", "order-b"],
            [one["meeting_id"] for one in json.load(self.request("/api/meetings"))["meetings"]],
        )

    def test_workspaces_can_be_dragged_into_an_order(self):
        for name in ("alpha", "beta"):
            (self.root / name).mkdir()
            self.request("/api/workspaces", method="POST",
                         body=json.dumps({"directory": str(self.root / name)}), csrf=True)
        self.assertEqual(
            [str(self.root.resolve()), str((self.root / "alpha").resolve()),
             str((self.root / "beta").resolve())],
            json.load(self.request("/api/meetings"))["workspaces"],
        )

        self.request("/api/workspaces/order", method="POST",
                     body=json.dumps({"workspaces": [str((self.root / "beta").resolve()),
                                                     str(self.root.resolve()),
                                                     str((self.root / "alpha").resolve())]}),
                     csrf=True)

        self.assertEqual(
            [str((self.root / "beta").resolve()), str(self.root.resolve()),
             str((self.root / "alpha").resolve())],
            json.load(self.request("/api/meetings"))["workspaces"],
        )
        # 名单上没点到的（后来才加的）补在后面，不会因为一次拖动就不见。
        (self.root / "gamma").mkdir()
        self.request("/api/workspaces", method="POST",
                     body=json.dumps({"directory": str(self.root / "gamma")}), csrf=True)
        workspaces = json.load(self.request("/api/meetings"))["workspaces"]
        self.assertEqual(4, len(workspaces))
        self.assertEqual(str((self.root / "gamma").resolve()), workspaces[-1])

    def test_workspace_rename_and_delete_routes(self):
        other = self.root / "elsewhere"
        other.mkdir()
        self.request("/api/workspaces", method="POST",
                     body=json.dumps({"directory": str(other)}), csrf=True)
        renamed = self.request("/api/workspaces/rename", method="POST",
                               body=json.dumps({"directory": str(other), "title": "别名"}), csrf=True)
        self.assertEqual(200, renamed.status)
        self.assertEqual("别名", json.load(renamed)["workspace_titles"][str(other.resolve())])
        deleted = self.request("/api/workspaces/delete", method="POST",
                               body=json.dumps({"directory": str(other)}), csrf=True)
        self.assertEqual(200, deleted.status)
        self.assertNotIn(str(other.resolve()), json.load(deleted)["workspaces"])
        self.assertTrue(other.is_dir())


if __name__ == "__main__":
    unittest.main()


class AttachmentTest(MeetingWebServiceTest):
    """聊天里的图片：交一张图、把它随消息发出去、再取回来。"""

    PNG = bytes.fromhex("89504e470d0a1a0a") + b"fake png body"

    def upload(self, meeting_id="attach-demo", payload=None, media_type="image/png"):
        return self.service.save_attachment(meeting_id, media_type,
                                            self.PNG if payload is None else payload)

    def test_an_image_becomes_a_file_of_the_meeting(self):
        self.start_meeting("attach-demo")
        descriptor = self.upload()
        self.assertEqual("image/png", descriptor["media_type"])
        self.assertEqual(len(self.PNG), descriptor["byte_count"])
        self.assertTrue(descriptor["name"].startswith("image-"))
        found = self.service.attachment("attach-demo", descriptor["name"])
        self.assertEqual(self.PNG, found["bytes"])
        self.assertEqual("image/png", found["media_type"])

    def test_the_message_carries_the_image_into_the_journal(self):
        self.start_meeting("attach-demo")
        descriptor = self.upload()
        store, _agent_error = self.service.message("attach-demo", {
            "message": "看这张", "attachments": [{"name": descriptor["name"]}],
        })
        chats = [e for e in store.events() if e["kind"] == "chat" and e["actor"] == "human"]
        self.assertEqual([descriptor["name"]],
                         [item["name"] for item in chats[-1]["data"]["attachments"]])
        # 图本身是这场会存下来的一份文件，不在 journal 里。
        self.assertNotIn("fake png body", json.dumps(chats[-1]))

    def test_a_message_may_be_nothing_but_pictures(self):
        self.start_meeting("attach-demo")
        descriptor = self.upload()
        store, _agent_error = self.service.message("attach-demo", {"message": "", "attachments": [descriptor["name"]]})
        chats = [e for e in store.events() if e["kind"] == "chat" and e["actor"] == "human"]
        self.assertEqual("", chats[-1]["content"])

    def test_only_images_stored_in_this_meeting_are_accepted(self):
        self.start_meeting("attach-demo")
        with self.assertRaisesRegex(WebError, "not stored"):
            self.service.message("attach-demo", {"message": "hi", "attachments": ["image-9.png"]})
        with self.assertRaisesRegex(WebError, "must be an array"):
            self.service.message("attach-demo", {"message": "hi", "attachments": "image-1.png"})
        # 一场会的图不能借给另一场。
        other = self.service.start({"message": "另一场", "meeting_id": "attach-other"})
        self.wait_for_workers()
        descriptor = self.upload()
        with self.assertRaisesRegex(WebError, "not stored"):
            self.service.message(other.meeting.meeting_id,
                                 {"message": "hi", "attachments": [descriptor["name"]]})

    def test_what_may_not_be_stored(self):
        self.start_meeting("attach-demo")
        with self.assertRaisesRegex(WebError, "png/jpeg"):
            self.upload(media_type="text/plain")
        with self.assertRaisesRegex(WebError, "empty"):
            self.upload(payload=b"")
        with self.assertRaisesRegex(WebError, "exceeds"):
            self.upload(payload=b"x" * (MAX_ATTACHMENT_BYTES + 1))
        with self.assertRaises(WebError):
            self.service.attachment("attach-demo", "../escape.png")
        with self.assertRaises(WebError):
            self.service.attachment("attach-demo", "image-404.png")
