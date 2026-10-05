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

from ordered_meeting.orders import measured, owed, ticket_of  # noqa: E402
from ordered_meeting.session import OK, STUCK, Session  # noqa: E402
from ticket_centered import actor_caps  # noqa: E402
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
    # 一轮一轮
    # ------------------------------------------------------------------

    def test_the_session_runs_to_ok_with_only_the_actor_working(self):
        self.session.state_the_task(WORDS)
        self.session.publish()
        outcome = self.session.run([("the-actor", "actor", scripted_actor())])
        self.assertEqual(outcome.outcome, OK)
        self.assertIsNotNone(outcome.value)             # 程序跑到了头，给出结果
        drafted, answered = self.session.store.read(f"{CASE}/drafted", "write_code")
        self.assertTrue(answered)
        self.assertEqual(drafted, CODE)
        # 一轮一步：assistant 每轮把下一步的实参固定成一张工单，一共三张
        self.assertEqual(outcome.rounds, 4)             # 三步 + 跑到底的那一轮
        self.assertEqual([tuple(pair[:2]) for pair in outcome.work_orders],
                         [(f"{CASE}/greeted", "acknowledge"),
                          (f"{CASE}/drafted", "write_code"),
                          (f"{CASE}/report_done", "report_done")])

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
        issued = self.last_line(self.run_module(*self.assistant_argv()))
        self.assertEqual(issued["wrote"], [[f"{CASE}/greeted", "acknowledge"]])

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

    def assistant_argv(self):
        return ("ordered_meeting.assistant", "--store", str(self.store_root), "--case", CASE,
                "--order", self.order, "--workflow", str(WORKFLOW), "--ticket", TICKET,
                "--words", WORDS)

    def actor_argv(self, *extra):
        return ("ordered_meeting.actor", "--store", str(self.store_root), "--case", CASE,
                "--order", self.order, "--writer", "tests.scripted_actor:write", *extra)

    def test_a_second_actor_process_replays_instead_of_writing_again(self):
        self.run_module(*self.assistant_argv())
        argv = self.actor_argv("--phase", "run")     # 自己跑一遍整张单：一次答三步
        first = self.last_line(self.run_module(*argv))
        self.assertEqual(len(first["answered"]), 3)
        again = self.last_line(self.run_module(*argv))
        self.assertEqual(again["answered"], [])
        self.assertEqual(len(again["replayed"]), 3)


if __name__ == "__main__":
    unittest.main()