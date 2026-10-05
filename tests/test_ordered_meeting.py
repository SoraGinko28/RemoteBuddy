"""工单系统：消息先到 assistant，发单；只有 actor 接单干活。

这里验的是 `ordered_meeting/` 那一套：一份共用的 store，没有发给谁的包。

* assistant 收到人类那句话，把它放进 store；
* assistant 发一张工单（`workflow/tickets/write_a_draft.viba`，三步全是 actor 的算子），自己跑一遍，
  做不了的那一步在 store 里留下一份 viba 工单（`$call` 已固定、`$measured` 还是 nil）；
* actor 作为一个服务接单：照它的能力表算，结果落到这条数据路径下，并填回 `$measured`。

写代码那一步是脚本化的（`write(prompt) -> code`），别的都是真的：真解释器、真 store、真工单文件。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ordered_meeting.actor import live_service  # noqa: E402
from ordered_meeting.meeting import case_for, record_path, run_for_meeting, store_root, target_for  # noqa: E402
from ordered_meeting.run import run_message  # noqa: E402
from ordered_meeting.orders import measured, owed, ticket_of  # noqa: E402
from ordered_meeting.session import OK, STUCK, Session  # noqa: E402
from ticket_centered import actor_caps  # noqa: E402
from ticket_centered.actor import AgentChannel  # noqa: E402
from ticket_centered.store import Store  # noqa: E402

WORKFLOW = REPO_ROOT / "workflow"
TICKET = "write_a_draft"
CASE = "case/quicksort"
WORDS = ("写一个快速排序，放到 quicksort.py。接口必须是 "
         "quick_sort(items, key=None, reverse=False, lo=0, hi=None)，就地排序。")
CODE = "def quick_sort(items, key=None, reverse=False, lo=0, hi=None):\n    return items\n"


def scripted_actor():
    """一个会写代码的 actor：写这一步是脚本化的，别的都是真的。"""
    return actor_caps(write=lambda _prompt: CODE)


def scripted_critic():
    """critic 那一张能力表：真的那套（`judge` 加上规则要用的算子），规则也真跑。"""
    from ordered_meeting.critic import critic_service
    from ordered_meeting.plan import workflow_of

    return critic_service(workflow_of(WORKFLOW))


class OrderedMeetingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_root = Path(self._tmp.name) / "store"
        self.session = Session(self.store_root, CASE, "draft-1", workflow=WORKFLOW, ticket=TICKET)

    def issue_a_draft(self):
        """走到"assistant 已经把单发出去"这一刻。"""
        self.session.state_the_task(WORDS)
        self.session.publish()
        return self.session.issue()

    # ------------------------------------------------------------------
    # 消息先到 assistant，它发单
    # ------------------------------------------------------------------

    def test_the_assistant_puts_the_message_in_the_store_first(self):
        self.session.state_the_task(WORDS)
        store = self.session.store
        value, answered = store.read(CASE, "stated_task")
        self.assertTrue(answered)
        self.assertEqual(value, WORDS)

    def test_the_order_program_lands_in_the_store_and_is_the_actor_s(self):
        self.issue_a_draft()
        program = self.session.program
        self.assertEqual(ticket_of(program), TICKET)
        self.assertTrue((program / "actor_api.viba").is_file())
        self.assertTrue((program / f"{TICKET}.viba").is_file())

    def test_the_assistant_alone_leaves_a_work_order_not_a_result(self):
        _report, work_orders = self.issue_a_draft()
        self.assertEqual([(path, name) for path, name, _call in work_orders],
                         [(f"{CASE}/greeted", "acknowledge")])
        store = self.session.store
        value, answered = measured(store, f"{CASE}/greeted", "acknowledge")
        self.assertFalse(answered)          # `$measured` 还是 nil：欠着的才是工单
        self.assertIsNone(value)
        # 工单就是 store 里那份文件：`$call` 已经固定（它拿到了那件事的地址），`$measured` 还是 nil
        work_order = self.store_root / CASE / "greeted" / "prepare" / "acknowledge.viba"
        text = work_order.read_text(encoding="utf-8")
        self.assertIn("$call", text)
        self.assertIn("stated_task", text)
        self.assertIn("$measured nil", text)

    def test_what_is_owed_is_what_the_capability_table_can_do(self):
        self.issue_a_draft()
        store = self.session.store
        self.assertEqual([(path, name) for path, name, _call in owed(store, scripted_actor())],
                         [(f"{CASE}/greeted", "acknowledge")])
        self.assertEqual(owed(store, {}), [])   # 能力表里没有的算子不是它的账

    # ------------------------------------------------------------------
    # actor 作为一个服务接单
    # ------------------------------------------------------------------

    def test_the_actor_answers_every_step_of_the_work_order(self):
        self.issue_a_draft()
        report = self.session.work("the-actor", "actor", scripted_actor())
        self.assertTrue(report.worked)
        self.assertEqual(report.answered, [(f"{CASE}/greeted", "acknowledge"),
                                           (f"{CASE}/drafted", "write_code"),
                                           (f"{CASE}/report_done", "report_done")])
        self.assertFalse(report.deferred)

    def test_what_the_actor_answered_is_in_the_store(self):
        self.issue_a_draft()
        self.session.work("the-actor", "actor", scripted_actor())
        store = self.session.store
        for path, name in ((f"{CASE}/greeted", "acknowledge"),
                           (f"{CASE}/drafted", "write_code"),
                           (f"{CASE}/report_done", "report_done")):
            value, answered = measured(store, path, name)
            self.assertTrue(answered, f"{path} + {name} 还没答")
        drafted, _answered = store.read(f"{CASE}/drafted", "write_code")
        self.assertEqual(drafted, CODE)
        # 答完就没有欠账了
        self.assertEqual(owed(store, scripted_actor()), [])

    def test_a_second_run_replays_and_does_no_work_again(self):
        self.issue_a_draft()
        self.session.work("the-actor", "actor", scripted_actor())
        again = self.session.work("the-actor", "actor", scripted_actor())
        self.assertFalse(again.worked)          # 回放不是它干的活
        self.assertEqual(again.answered, [])
        self.assertEqual(len(again.replayed), 3)

    # ------------------------------------------------------------------
    # 该发哪一张：读 store 算
    # ------------------------------------------------------------------

    def test_the_assistant_publishes_the_ticket_the_store_says_is_due(self):
        # 人类还没说话：`write_a_draft` 要读的那一步还没有，谁都不该发
        self.assertIsNone(self.session.due_ticket())
        self.session.state_the_task(WORDS)
        self.assertEqual(self.session.due_ticket(), TICKET)
        self.session.publish()                       # 不给名字，发 store 说该发的那一张
        self.assertEqual(self.session.ticket_name, TICKET)

    def test_only_the_actor_stops_at_the_ticket_that_needs_the_critic(self):
        self.session.state_the_task(WORDS)
        outcome = self.session.run([("the-actor", "actor", scripted_actor())])
        # 写完了就该轮到判语那一张，而它要的是 critic 的算子：没人接，账上是 stuck
        self.assertEqual(outcome.outcome, STUCK)
        self.assertEqual(outcome.reports[-1].ticket, "judge_the_draft")
        self.assertEqual([tuple(pair[:2]) for pair in outcome.work_orders][-1],
                         (f"{CASE}/judged", "judge"))
        # 那张单留在 store 里等会它的服务：判语还没人答
        _value, answered = self.session.store.read(f"{CASE}/judged", "judge")
        self.assertFalse(answered)

    def test_the_session_publishes_the_due_ticket_without_being_told_which(self):
        self.session.state_the_task(WORDS)
        outcome = self.session.run([("the-actor", "actor", scripted_actor())])
        self.assertEqual(outcome.reports[0].ticket, TICKET)   # 头一张就是 store 说该发的那张

    # ------------------------------------------------------------------
    # 一轮一轮
    # ------------------------------------------------------------------

    def test_the_actor_writes_and_the_critic_judges_the_whole_meeting(self):
        """五张单一路走下来：actor 写、critic 判、assistant 收尾，每张都由 assistant 读 store 发出去。"""
        self.session.state_the_task(WORDS)
        outcome = self.session.run([("the-actor", "actor", scripted_actor()),
                                    ("the-critic", "critic", scripted_critic())])
        self.assertEqual(outcome.outcome, OK)
        drafted, answered = self.session.store.read(f"{CASE}/drafted", "write_code")
        self.assertTrue(answered)
        self.assertEqual(drafted, CODE)

        # 一轮一步：写那一张的三步 + 判那一张的一步
        self.assertEqual([tuple(pair[:2]) for pair in outcome.work_orders][:4],
                         [(f"{CASE}/greeted", "acknowledge"),
                          (f"{CASE}/drafted", "write_code"),
                          (f"{CASE}/report_done", "report_done"),
                          (f"{CASE}/judged", "judge")])
        # 五张单都发过：发哪一张、发了几张，看账上的 ticket 就够了
        published = []
        for report in outcome.reports:
            if report.ticket not in published:
                published.append(report.ticket)
        self.assertEqual(published, ["write_a_draft", "judge_the_draft", "revise_the_draft",
                                     "judge_the_revision", "close_the_meeting"])
        # 判语是 critic 答的：第一版 5 个参数，规则的上限是 4，它说不合格，并说出那句话
        judged, answered = self.session.store.read(f"{CASE}/judged", "judge")
        self.assertTrue(answered)
        self.assertFalse(judged)
        objection, answered = self.session.store.read(f"{CASE}/judged", "objection")
        self.assertTrue(answered)
        self.assertIn("超过 4 个参数", str(objection))
        # 收尾是 assistant 自己说的，读的是改完那一版的判语
        closing, answered = self.session.store.read(f"{CASE}/closing", "closing_note")
        self.assertTrue(answered)
        self.assertIn("判语", str(closing))

    def test_the_call_is_fixed_before_the_actor_answers(self):
        self.session.state_the_task(WORDS)
        self.session.publish()
        caps = scripted_actor()
        owed_steps = []
        for _ in range(3):
            _report, written = self.session.issue()
            self.assertEqual(len(written), 1)           # 一轮只往前走一步
            path, name, _call = written[0]
            text = (self.store_root / path / "prepare" / f"{name}.viba").read_text(encoding="utf-8")
            self.assertIn("$call", text)                # 实参已经固定
            self.assertIn("$measured nil", text)        # 结果还没人算
            owed_steps.append((path, name))
            self.assertEqual(self.session.answer("the-actor", "actor", caps).answered,
                             [(path, name)])

        self.assertEqual(owed_steps, [(f"{CASE}/greeted", "acknowledge"),
                                      (f"{CASE}/drafted", "write_code"),
                                      (f"{CASE}/report_done", "report_done")])
        # 两步实参的调用：工单里按写下来的名字各留一份，填完还是两份都在
        text = (self.store_root / CASE / "drafted" / "prepare" / "write_code.viba").read_text(
            encoding="utf-8")
        self.assertIn("$task", text)
        self.assertIn("$after", text)
        self.assertIn("stated_task", text)
        self.assertNotIn("$measured nil", text)

    def test_a_service_that_cannot_do_the_work_leaves_the_order_stuck(self):
        self.session.state_the_task(WORDS)
        self.session.publish()
        outcome = self.session.run([("an-actor-that-cannot-write", "actor", {})])
        self.assertEqual(outcome.outcome, STUCK)
        self.assertEqual(outcome.rounds, 1)


class ActorServiceProcessTest(unittest.TestCase):
    """actor 是一个服务进程：一条命令、一份共用的 store，没有发给谁的包。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_root = Path(self._tmp.name) / "store"
        self.order = "draft-1"

    def run_module(self, *argv):
        """照真会议那样起一个进程：cwd 在仓库根上，模块才找得到。"""
        return subprocess.run([sys.executable, "-m", *argv], cwd=str(REPO_ROOT),
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})

    def last_line(self, finished):
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return json.loads(finished.stdout.strip().splitlines()[-1])

    def test_the_assistant_and_the_actor_are_two_processes_on_one_store(self):
        """一轮一步：assistant 发单、actor 接单，各是一个进程，一份 store。"""
        issued = self.last_line(self.run_module(*self.assistant_argv(words=True)))
        self.assertEqual(issued["wrote"], [[f"{CASE}/greeted", "acknowledge"]])
        self.assertEqual(issued["ticket"], TICKET)       # 发哪一张是它读 store 算出来的

        rounds = []
        for _ in range(3):
            answered = self.last_line(self.run_module(*self.actor_argv()))
            rounds.append(answered["answered"])
            self.last_line(self.run_module(*self.assistant_argv()))    # 答完一步，发下一步
        self.assertEqual(rounds, [[[f"{CASE}/greeted", "acknowledge"]],
                                  [[f"{CASE}/drafted", "write_code"]],
                                  [[f"{CASE}/report_done", "report_done"]]])

        store = Store(self.store_root)
        drafted, ok = store.read(f"{CASE}/drafted", "write_code")
        self.assertTrue(ok)
        self.assertEqual(drafted, CODE)
        # 每一步的工单里都留着它拿到的实参，答完也还在
        text = (self.store_root / CASE / "drafted" / "prepare" / "write_code.viba").read_text(
            encoding="utf-8")
        self.assertIn("$call", text)
        self.assertIn("$task", text)
        self.assertIn("stated_task", text)
        self.assertNotIn("$measured nil", text)

    def assistant_argv(self, words: bool = False):
        # 不给 --ticket：发哪一张由 assistant 读 store 算
        argv = ("ordered_meeting.assistant", "--store", str(self.store_root), "--case", CASE,
                "--order", self.order, "--workflow", str(WORKFLOW))
        return argv + ("--words", WORDS) if words else argv

    def actor_argv(self, *extra):
        return ("ordered_meeting.actor", "--store", str(self.store_root), "--case", CASE,
                "--order", self.order, "--writer", "tests.scripted_actor:write", *extra)

    def test_the_critic_answers_a_judge_work_order_in_its_own_process(self):
        """写在另一个进程里发出来的单子上接着干：critic 也是一个服务进程。"""
        session = Session(self.store_root, CASE, self.order, workflow=WORKFLOW)
        session.state_the_task(WORDS)
        outcome = session.run([("the-actor", "actor", scripted_actor())])
        self.assertEqual(outcome.outcome, STUCK)          # 只有 actor 接单，停在判语那张
        self.assertEqual([tuple(pair[:2]) for pair in outcome.work_orders][-1],
                         (f"{CASE}/judged", "judge"))

        finished = self.run_module("ordered_meeting.critic", "--store", str(self.store_root),
                                   "--case", CASE, "--order", self.order,
                                   "--workflow", str(WORKFLOW))
        self.assertEqual(self.last_line(finished)["answered"], [[f"{CASE}/judged", "judge"]])
        judged, answered = Store(self.store_root).read(f"{CASE}/judged", "judge")
        self.assertTrue(answered)
        self.assertFalse(judged)                          # 5 个参数，规则说不合格

    def test_a_second_actor_process_replays_instead_of_writing_again(self):
        self.run_module(*self.assistant_argv(words=True))
        argv = self.actor_argv("--phase", "run")     # 自己跑一遍整张单：一次答三步
        first = self.last_line(self.run_module(*argv))
        self.assertEqual(len(first["answered"]), 3)
        again = self.last_line(self.run_module(*argv))
        self.assertEqual(again["answered"], [])
        self.assertEqual(len(again["replayed"]), 3)


class MessageDriverTest(unittest.TestCase):
    """一句话进来，剩下的是工单：`python -m ordered_meeting.run`。"""

    def run_driver(self, store_root, *extra):
        return subprocess.run(
            [sys.executable, "-m", "ordered_meeting.run", "--store", str(store_root),
             "--case", CASE, "--order", "draft-1", "--workflow", str(WORKFLOW),
             "--words", WORDS, "--writer", "tests.scripted_actor:write", *extra],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})

    def test_one_message_runs_the_whole_meeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            finished = self.run_driver(store_root)
            self.assertEqual(finished.returncode, 0, finished.stderr)
            lines = finished.stdout.strip().splitlines()
            self.assertEqual(lines[-1], "结局：ok，12 轮")
            self.assertTrue(any(line.startswith("critic") and "judge" in line for line in lines))

            store = Store(store_root)
            drafted, answered = store.read(f"{CASE}/drafted", "write_code")
            self.assertTrue(answered)
            self.assertEqual(drafted, CODE)
            _judged, answered = store.read(f"{CASE}/judged", "judge")
            self.assertTrue(answered)

    def test_only_the_actor_stops_where_the_critic_is_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            finished = self.run_driver(Path(tmp) / "store", "--actor-only")
            self.assertEqual(finished.returncode, 1)
            self.assertEqual(finished.stdout.strip().splitlines()[-1], "结局：stuck，5 轮")

    def test_a_message_nobody_can_answer_leaves_the_work_order_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            session_work = run_message(store_root=store_root, case=CASE, order="draft-1",
                                       workflow=WORKFLOW, words=WORDS, services=[])
            self.assertEqual(session_work.outcome, STUCK)
            self.assertEqual(session_work.rounds, 1)
            # 没人接的那张工单留在 store 里，等会它的服务来
            self.assertEqual([tuple(pair[:2]) for pair in session_work.work_orders],
                             [(f"{CASE}/greeted", "acknowledge")])
            store = Store(store_root)
            _value, answered = store.read(f"{CASE}/greeted", "acknowledge")
            self.assertFalse(answered)


class MeetingDirectoryTest(unittest.TestCase):
    """会议目录那一侧：`<会议目录>/ordered/` 就是这一场会的 store 和记录。"""

    def services(self):
        return [("the-actor", "actor", scripted_actor()),
                ("the-critic", "critic", scripted_critic())]

    def test_a_message_runs_inside_a_meeting_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            meeting_dir = Path(tmp) / "runs" / "m-1"
            meeting_dir.mkdir(parents=True)
            case = case_for(WORDS)
            self.assertEqual(case, "case/quicksort")
            self.assertEqual(target_for(WORDS), "quicksort.py")

            outcome = run_for_meeting(meeting_dir, case=case, words=WORDS, workflow=WORKFLOW,
                                      services=self.services())
            self.assertEqual(outcome.outcome, OK)

            store = Store(store_root(meeting_dir))
            drafted, answered = store.read(f"{case}/drafted", "write_code")
            self.assertTrue(answered)
            self.assertEqual(drafted, CODE)
            # 工单程序放在会议目录下面那份 store 里
            program = Path(store_root(meeting_dir)) / "orders" / "meeting" / "program"
            self.assertTrue((program / "ticket").is_file())
            self.assertTrue((program / "actor_api.viba").is_file())

    def test_the_record_says_who_spoke_and_who_answered_what(self):
        with tempfile.TemporaryDirectory() as tmp:
            meeting_dir = Path(tmp) / "runs" / "m-1"
            meeting_dir.mkdir(parents=True)
            run_for_meeting(meeting_dir, case=case_for(WORDS), words=WORDS, workflow=WORKFLOW,
                            services=self.services())
            record = record_path(meeting_dir).read_text(encoding="utf-8")
            self.assertIn(f"- 人类说了一句：{WORDS}", record)
            for ticket in ("write_a_draft", "judge_the_draft", "revise_the_draft",
                           "judge_the_revision", "close_the_meeting"):
                self.assertIn(f"- 工单 {ticket}（案子 case/quicksort）", record)
            self.assertIn("- the-actor（actor）答了 case/quicksort/drafted + write_code", record)
            self.assertIn("- the-critic（critic）答了 case/quicksort/judged + judge", record)


class LiveActorWiringTest(unittest.TestCase):
    """真会议那张能力表：工单里的实参 → 说给 agent 的题面 → 它交的文件 → 工单的 `$measured`。

    这里把会议那条 HTTP 线换成假的（`say` / 等它干完 / 交文件那三步），能力表本身是真的
    （`ticket_centered.actor.meeting_actor_caps`）。
    """

    class Channel(AgentChannel):
        def __init__(self, workspace, code: str):
            super().__init__("http://127.0.0.1:1", "meeting-x", workspace)
            self.said = []
            self.code = code

        def say(self, message):
            self.said.append(message)
            return {}

        def wait_for_turn(self, before, timeout=None):
            return {"phase": "waiting_for_user", "status": "running"}

        def wait_for_chat(self, message_type, timeout=None):
            return "知道了" if message_type == "ack" else "做好了"

        def ensure_work(self, instruction, target, **kwargs):
            self.said.append(instruction)
            path = self.file(target)
            path.write_text(self.code, encoding="utf-8")
            return self.code

    def test_the_work_order_reaches_the_agent_and_its_file_comes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            channel = self.Channel(workspace, CODE)
            session = Session(Path(tmp) / "store", CASE, "draft-1", workflow=WORKFLOW)
            session.state_the_task(WORDS)

            outcome = session.run([("the-actor", "actor", live_service(channel, "quicksort.py")),
                                   ("the-critic", "critic", scripted_critic())])
            self.assertEqual(outcome.outcome, OK)

            drafted, answered = session.store.read(f"{CASE}/drafted", "write_code")
            self.assertTrue(answered)
            self.assertEqual(drafted, CODE)              # agent 交的那个文件，落在工单里
            said, answered = session.store.read(f"{CASE}/greeted", "acknowledge")
            self.assertTrue(answered)
            self.assertEqual(said, "知道了")              # 回话读的是 agent 自己说的那句

            told = channel.said[0]                       # 头一句就是写那一版的题面
            self.assertIn("照这份题面写一版代码", told)
            self.assertIn("写一个快速排序", told)          # 题面来自工单里的 $task
            self.assertIn("quicksort.py", told)           # 交到哪个文件
            self.assertTrue((workspace / "quicksort.py").is_file())


if __name__ == "__main__":
    unittest.main()