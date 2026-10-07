"""工单这一层：一次要跑的调用怎么写在盘上、谁试过它、谁答了什么、一场会怎么跑到头。

这里验的是几件真东西：工单就是那次调用本身（`__dyn_call__ << "<整名>"`，环境留空）、进度一次尝试
一个文件且"有 `$ok` 就算做完"只增不退、该发哪一张是读案子里的值算出来的、assistant 收下人类那句话
之后单真的落在 `<会议目录>/tickets/<工单号>/ticket.viba`、值落在 `<会议目录>/store/<案子>/` 下、
一场会五张单一路走到 `ok`。跑的是真解释器、真文件、真进程。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tickets import _viba  # noqa: E402,F401
from tickets.actor import actor_caps  # noqa: E402
from tickets.assistant import assistant_handler, what_to_make  # noqa: E402
from tickets.binding import finished, unimplemented_step  # noqa: E402
from tickets.critic import critic_handler  # noqa: E402
from tickets.handler import Handler, program_caps  # noqa: E402
from tickets.issue import ticket_uid  # noqa: E402
from tickets.layout import case_root, progress_root, tickets_root  # noqa: E402
from tickets.plan import due, next_ticket  # noqa: E402
from tickets.progress import (a_stage, answered, read_stages, the_state, unfinished,  # noqa: E402
                             write_stage)
from tickets.session import OK, STUCK, Session, open_work  # noqa: E402
from tickets.table import (Process, Row, members_for, members_of, read_table, row_of,  # noqa: E402
                           write_table)
from tickets.ticket import read_ticket, ticket_uids, tickets  # noqa: E402
from tickets.values import Store  # noqa: E402
from tickets.workflow import load_workflow, norm_home  # noqa: E402

CASE = "case/quicksort"
WORDS = "写一个快速排序，放到 quicksort.py"
# 四个参数：规则的上限是 4，所以第一版就合格。
CODE = "def quick_sort(items, key=None, reverse=False, bounds=(0, None)):\n    return items\n"
SIMPLE_CODE = "def quick_sort(items, key=None, reverse=False):\n    return items\n"


def a_workflow():
    return load_workflow(norm_home())


def a_writer(_prompt: str) -> str:
    """脚本化的写：测试里"写"这一步就是这一份。"""
    return CODE


def a_session(meeting, case: str = CASE):
    return Session(meeting, case, workflow=a_workflow())


def the_handlers(meeting, workflow):
    """三个成员：assistant、critic、脚本化的 actor。顺序就是它们接单的次序。"""
    return [assistant_handler(meeting, workflow),
            critic_handler(meeting, workflow),
            Handler("actor", "actor",
                    {**program_caps(workflow), **actor_caps(a_writer)}, meeting,
                    programs=workflow.tickets())]


class ATicketTest(unittest.TestCase):
    """一张单：它写在哪、写成什么、读回来是什么。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_the_ticket_is_the_call_itself(self):
        """单上就是那次调用：`__dyn_call__ << "<整名>"`，环境留空。"""
        session = a_session(self.meeting)
        ticket = session.issue("write_a_draft")
        self.assertEqual("write_a_draft-quicksort", ticket.uid)
        path = tickets_root(self.meeting) / ticket.uid / "ticket.viba"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("$ticket_name \"write_a_draft\"", text)
        self.assertIn('$call (__dyn_call__ << "write_a_draft")', text)
        self.assertIn(str(case_root(self.meeting, CASE)), text)
        self.assertNotIn("$storage_path \"\"", text)

    def test_it_reads_back_as_the_same_ticket(self):
        """读回来是同一张单：号、名字、那次调用、案子、找模块的路、时刻，一样不少。"""
        session = a_session(self.meeting)
        written = session.issue("judge_the_draft")
        back = read_ticket(self.meeting, written.uid)
        self.assertEqual(written.uid, back.uid)
        self.assertEqual("judge_the_draft", back.name)
        self.assertEqual(str(case_root(self.meeting, CASE)), back.storage_path)
        self.assertEqual(self.workflow.search_dirs(), list(back.viba_path))
        self.assertEqual(written.time, back.time)
        self.assertIsNotNone(back.call)

    def test_the_uid_says_which_ticket_and_which_case(self):
        """号一眼看得出是哪张单、哪个案子。"""
        self.assertEqual("write_a_draft-quicksort",
                         ticket_uid("write_a_draft", "/x/store/case/quicksort"))
        self.assertEqual("write_a_draft-quicksort-2",
                         ticket_uid("write_a_draft", "/x/store/case/quicksort-2"))
        self.assertEqual("write_a_draft", ticket_uid("write_a_draft", "/x/nothing"))

    def test_the_ticket_is_a_value_like_every_other(self):
        """单是一份值：和别处一样写得下、读得回（容器成员也照样）。"""
        session = a_session(self.meeting)
        ticket = session.issue("close_the_meeting")
        self.assertIn("/rb_norm/tickets", read_ticket(self.meeting, ticket.uid).viba_path[0])


class TheProgressTest(unittest.TestCase):
    """进度：一次尝试一个文件，只增不删，"有 `$ok` 就算做完"。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_one_file_per_attempt_under_ticket_progress(self):
        """两个成员各试一次：两个文件，名字带写它的时刻和是谁写的。"""
        session = a_session(self.meeting)
        session.issue("write_a_draft")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        handlers = the_handlers(self.meeting, self.workflow)
        handlers[0].tick()                       # assistant：不是它的事
        handlers[2].tick()                       # actor：做完了
        where = progress_root(self.meeting) / "write_a_draft-quicksort"
        names = sorted(one.name for one in where.glob("*.viba"))
        self.assertEqual(2, len(names))
        self.assertTrue(any(one.endswith("-assistant.viba") for one in names))
        self.assertTrue(any(one.endswith("-actor.viba") for one in names))
        self.assertTrue(answered(self.meeting, "write_a_draft-quicksort"))

    def test_a_stop_written_after_the_answer_cannot_take_it_back(self):
        """做完了之后又有人写下一篇停下的：那张单还是做完了（`$ok` 是只涨不退的那一条）。"""
        session = a_session(self.meeting)
        session.issue("write_a_draft")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        handlers = the_handlers(self.meeting, self.workflow)
        uid = "write_a_draft-quicksort"
        stopped = handlers[0].attempt(read_ticket(self.meeting, uid))
        handlers[2].tick()                       # actor 把它做完了
        # 做完之后才落下来的那一篇停下的（两个人同时在跑时躲不掉的那一下）
        write_stage(self.meeting, a_stage(uid, "critic", stopped.result))
        stages = read_stages(self.meeting, uid)
        self.assertEqual(3, len(stages))
        self.assertFalse(stages[0].answered())
        self.assertTrue(stages[1].answered())
        self.assertFalse(stages[2].answered())
        self.assertTrue(answered(self.meeting, uid))     # 那一条 `$ok` 顶不掉
        self.assertEqual("critic", the_state(self.meeting)[0]["last_who"])
        self.assertIn("不是它的事", the_state(self.meeting)[0]["last_stop"])

    def test_an_attempt_says_which_step_was_not_its_business(self):
        """停下那一篇说的是"停在哪一步"：那一步的整名和那次调用都在里面。"""
        session = a_session(self.meeting)
        session.issue("write_a_draft")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        stage = assistant_handler(self.meeting, self.workflow).tick()[0]
        self.assertFalse(finished(stage.result))
        step = unimplemented_step(stage.result)
        self.assertEqual("actor_api.acknowledge", step[1])
        # 那一次调用也在里面：环境留空、实参是当时写下的那些（这是盘上那一篇的原文）
        text = "".join(one.read_text(encoding="utf-8")
                       for one in (progress_root(self.meeting) / stage.uid).glob("*.viba"))
        self.assertIn('__dyn_call__ << "actor_api.acknowledge"', text)


class ThePlanTest(unittest.TestCase):
    """该发哪一张：读值算出来的。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_the_first_ticket_is_the_one_whose_readings_are_there(self):
        """一开始只有一张发得出去：它要读的那一步（人说的话）在，别的都还缺着。"""
        session = a_session(self.meeting)
        self.assertIsNone(next_ticket(self.workflow, session.values))
        # 人说了一句：`stated_task` 落在案子自己的数据路径下
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        self.assertEqual("write_a_draft", next_ticket(self.workflow, session.values))
        wanted, why = due(self.workflow, session.values, "judge_the_draft")
        self.assertFalse(wanted)
        self.assertIn("还缺", why)

    def test_the_next_one_is_due_once_the_one_before_it_answered(self):
        """前一张做完，后一张才该发（次序不是写死的，是读出来的）。"""
        session = a_session(self.meeting)
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        session.run(the_handlers(self.meeting, self.workflow), out=lambda _line: None)
        # 五张单都发过、都做完，没有下一张
        self.assertEqual(["close_the_meeting-quicksort", "judge_the_draft-quicksort",
                          "judge_the_revision-quicksort", "revise_the_draft-quicksort",
                          "write_a_draft-quicksort"], ticket_uids(self.meeting))
        self.assertIsNone(next_ticket(self.workflow, session.values))


class AWholeMeetingTest(unittest.TestCase):
    """一场会：一句话进来，五张单一路走到 ok。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def a_run(self, words: str = WORDS):
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(words, handlers[0])
        return session.run(handlers, out=lambda _line: None)

    def test_the_assistant_issues_a_ticket_for_the_human_message(self):
        """消息先到 assistant：它把话放进案子，接着单真的写在会议目录下面。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant)
        self.assertEqual(WORDS, session.values.value("", "stated_task"))
        session.issue("write_a_draft")
        self.assertTrue((tickets_root(self.meeting) / "write_a_draft-quicksort"
                         / "ticket.viba").is_file())
        self.assertEqual([], list(progress_root(self.meeting).glob("*")))

    def test_a_whole_meeting_ends_ok(self):
        """五张单一张一张发、一张一张接：到 `ok`，每一张的进度里都有一条 `$ok`。"""
        outcome = self.a_run()
        self.assertEqual(OK, outcome.outcome)
        state = the_state(self.meeting)
        self.assertEqual(5, len(state))
        self.assertTrue(all(one["answered"] for one in state))
        # 每一次尝试都落在会议目录下的 ticket_progress 里
        self.assertTrue(all((progress_root(self.meeting) / one["ticket"]).is_dir()
                            for one in state))

    def test_the_values_land_in_the_case_under_the_meeting(self):
        """答下的值落在 `<会议目录>/store/<案子>/` 下，一条数据路径一份。"""
        self.a_run()
        values = Store(case_root(self.meeting, CASE))
        self.assertTrue(values.answered("drafted", "write_code"))
        self.assertTrue(values.answered("revised", "revise_code"))
        self.assertTrue(values.answered("judged", "judge"))
        self.assertTrue(values.answered("closing", "closing_note"))
        self.assertEqual(CODE, values.value("drafted", "write_code"))

    def test_the_record_says_who_answered_what(self):
        """这场会的记录里：人说了什么、发了哪张单、谁答了哪一步。"""
        self.a_run()
        record = (self.meeting / "meeting_record.md").read_text(encoding="utf-8")
        self.assertIn(f"- 人类说了一句：{WORDS}", record)
        self.assertIn("- 发了一张单 write_a_draft-quicksort", record)
        self.assertIn("- actor（actor）答了 drafted + write_code", record)
        self.assertIn("- critic（critic）答了 judged + judge", record)
        self.assertIn("- assistant（assistant）答了 closing + closing_note", record)

    def test_a_second_attempt_replays_the_first_answers(self):
        """同一张单再试一次：案子里已有值的那几步回放，不重做（写只做了一次）。"""
        writes: list = []

        def counting(prompt: str) -> str:
            writes.append(prompt)
            return CODE

        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        handlers[2].caps.update(actor_caps(counting))
        session.stated_task(WORDS, handlers[0])
        session.run(handlers, out=lambda _line: None)
        self.assertEqual(1, len(writes))              # 它说合格，没有第二版要改
        # 同一张单再跑一遍：案子里已有值的那几步回放，写那一步不再做
        again = handlers[2].attempt(read_ticket(self.meeting, "write_a_draft-quicksort"))
        self.assertTrue(again.answered())
        self.assertEqual(1, len(writes))

    def test_a_step_nobody_does_leaves_the_meeting_stuck(self):
        """谁都不会的那一步没人接：当轮就走不动了，账上是 stuck（不假装跑完）。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant)
        outcome = session.run([assistant], out=lambda _line: None)
        self.assertEqual(STUCK, outcome.outcome)
        self.assertIn("write_a_draft-quicksort", outcome.failure)
        self.assertFalse(answered(self.meeting, "write_a_draft-quicksort"))

    def test_an_unfinished_meeting_says_where_it_stopped(self):
        """没跑完的一趟说得清：哪个案子、人说的哪句话（进程死没死都不必知道）。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant, "quicksort.py")
        session.run([assistant], out=lambda _line: None)
        self.assertEqual((CASE, WORDS, "quicksort.py"), open_work(self.meeting))


class WhatCountsAsWorkTest(unittest.TestCase):
    """人说的哪句话算一份活：规范里 `requests/` 那几份程序说了算。"""

    def setUp(self):
        self.workflow = a_workflow()

    def test_a_line_that_names_nothing_to_deliver_is_no_work(self):
        """一句闲聊不点要交什么：没有活可派（不开案子、不发单）。"""
        self.assertEqual("", what_to_make(self.workflow, "你还在吗？"))
        self.assertEqual("", what_to_make(self.workflow, "今天天气不错"))

    def test_a_line_that_names_what_to_deliver_is_work(self):
        """点了要交的那个文件名：这一句就是要它做活，答出来的就是那个名字。"""
        self.assertEqual("quicksort.py",
                         what_to_make(self.workflow, "写一个快速排序，放到 quicksort.py"))
        self.assertEqual("report.md", what_to_make(self.workflow, "report.md 改一下"))

    def test_the_norm_owns_the_rule(self):
        """这一条是规范里那几份程序定的：换一份 requests/，认的就变了。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        norm = Path(temporary.name) / "norm"
        shutil.copytree(norm_home(), norm)
        (norm / "requests" / "what_to_make.viba").unlink()
        (norm / "requests" / "only_readme.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\n"
            "args = __get_args__ << __decl__\n\n"
            "the_words =\n    str\n  <- $env Env\n  <- { 人刚说的那句 }\n\n"
            "__impl__ = the_words << $env args.env\n", encoding="utf-8")
        own = load_workflow(norm)
        self.assertEqual(["only_readme"], own.requests())
        # 这一份答的是人刚说的那句（`the_words` 那一步的值）：不说"要交什么"就不算活
        self.assertEqual("你还在吗？", what_to_make(own, "你还在吗？"))


class TheMembersTest(unittest.TestCase):
    """这场会有哪几个成员：读规范旁边那张表。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.norm = self.root / "norm"
        shutil.copytree(norm_home(), self.norm)
        self.workflow = load_workflow(self.norm)

    def test_the_table_the_norm_ships_names_three_members(self):
        """规范自己那张表：actor（会议里那个 agent）、critic、assistant。"""
        rows = members_of(self.workflow)
        self.assertEqual(["actor", "critic", "assistant"], [one.name for one in rows])
        self.assertEqual(["agent", "critic", "assistant"], [one.kind for one in rows])
        self.assertEqual(["actor", "critic", "assistant"], [one.role for one in rows])

    def test_a_row_that_says_nothing_about_where_to_find_it_is_refused(self):
        """表项写得不成话：当场说清楚，不留到跑的时候才发现没人接单。"""
        with self.assertRaises(ValueError):
            row_of("actor actor_api")
        with self.assertRaises(ValueError):
            row_of("actor actor agent")
        with self.assertRaises(ValueError):
            row_of("actor actor_api nowhere")

    def test_adding_a_member_is_a_line_of_the_table(self):
        """加一个成员就是往表里加一行，Python 一行不用改。"""
        # 新角色的 api 先写进规范，表里那一行才立得住
        (self.norm / "static_code_analysis_api.viba").write_text(
            "check_static =\n"
            "    ($module_path str * $func_name str)\n"
            "  <- $env Env\n"
            "  <- $code ($module_path str * $func_name str)\n"
            "  <- { 拿 $code 那份代码做静态检查 }\n", encoding="utf-8")
        self.workflow = load_workflow(self.norm)
        path = self.norm / "services"
        write_table(path, ["actor actor_api agent",
                           "checker static_code_analysis_api process python -m tests.process_checker"])
        rows = read_table(path)
        self.assertEqual("process", rows[-1].kind)
        self.assertEqual(("python", "-m", "tests.process_checker"), rows[-1].arguments)
        members = members_for(rows, workflow=self.workflow,
                              meeting_dir=self.root / "meeting", case=CASE,
                              agent_caps=actor_caps(a_writer))
        checker = next(one for one in members if one.name == "checker")
        self.assertIsInstance(checker, Process)
        self.assertEqual("static_code_analysis", checker.role)

    def test_a_table_without_an_assistant_still_has_one(self):
        """表里没写 assistant 也有它一个：人类那句话总得有人收、单总得有人发。"""
        rows = [row_of("actor actor_api agent"), row_of("critic critic_api critic")]
        members = members_for(rows, workflow=self.workflow, meeting_dir=self.root / "meeting",
                              case=CASE, agent_caps=actor_caps(a_writer))
        self.assertEqual(["actor", "critic", "assistant"], [one.role for one in members])

    def test_a_process_member_answers_its_own_way(self):
        """一个成员是另一个进程：宿主把它跑一遍，它自己接单、自己写进度。"""
        checker = self.root / "checker.py"
        checker.write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from tickets.actor import actor_caps\n"
            "from tickets.member import member_main\n"
            "sys.exit(member_main('actor', lambda _a, _w: actor_caps(lambda _p: %r)))\n"
            % (str(REPO_ROOT), SIMPLE_CODE),
            encoding="utf-8")
        process = Process("actor", "actor", [sys.executable, str(checker)],
                          self.root / "meeting", CASE, cwd=str(REPO_ROOT))
        session = a_session(self.root / "meeting")
        session.stated_task(WORDS, assistant_handler(self.root / "meeting", self.workflow))
        session.issue("write_a_draft")
        written = process.tick()
        self.assertTrue(written)                      # 它写了一篇进度
        self.assertEqual("actor", written[0].who)
        self.assertTrue(answered(self.root / "meeting", "write_a_draft-quicksort"))
        self.assertEqual([("greeted", "acknowledge"), ("drafted", "write_code"),
                          ("report_done", "report_done")], process.did)


class TheHandlerTest(unittest.TestCase):
    """一个处理者：轮到谁接哪张单。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_it_takes_an_unfinished_ticket_nobody_just_tried(self):
        """没做完、而且最后那一篇不是它写的：这才是它接的那张。"""
        session = a_session(self.meeting)
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        session.issue("write_a_draft")
        actor = the_handlers(self.meeting, self.workflow)[2]
        self.assertEqual(["write_a_draft-quicksort"], [one.uid for one in actor.takes()])
        actor.tick()
        self.assertEqual([], actor.takes())            # 刚才是它写的：下一手不是它的
        self.assertTrue(unfinished(self.meeting) or True)

    def test_the_same_value_twice_is_the_same_file(self):
        """一样的东西写两遍是同一份数据：写下来的字一个不差。"""
        session = a_session(self.meeting)
        first = session.issue("write_a_draft")
        path = tickets_root(self.meeting) / first.uid / "ticket.viba"
        before = path.read_text(encoding="utf-8")
        again = read_ticket(self.meeting, first.uid)
        self.assertEqual(str(case_root(self.meeting, CASE)), again.storage_path)
        self.assertEqual(before, (tickets_root(self.meeting) / again.uid
                                  / "ticket.viba").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
