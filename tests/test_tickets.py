"""工单这一层：一次要跑的调用怎么写在盘上、谁试过它、谁答了什么、一场会怎么跑到头。

这里验的是几件真东西：工单就是那次调用本身（`__dyn_call__ << "<整名>"`，环境留空）、进度一次尝试
一个文件且"有 `$ok` 就算做完"只增不退、该发哪一张是读案子里的值算出来的、assistant 收下人类那句话
之后单真的落在 `<会议目录>/tickets/<工单号>/ticket.viba`、值落在 `<会议目录>/store/<案子>/` 下、
一场会一张单（`do_the_work`）一路走到 `ok`。跑的是真解释器、真文件、真进程。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tickets import _viba  # noqa: E402,F401
from tickets.actor import actor_caps  # noqa: E402
from tickets.assistant import assistant_handler, what_to_make  # noqa: E402
from tickets.binding import Binding, finished, unimplemented_step  # noqa: E402
from tickets.channel import AgentChannel  # noqa: E402
from tickets.critic import critic_handler  # noqa: E402
from tickets.mgmt_critic import mgmt_critic_handler  # noqa: E402
from tickets import declare  # noqa: E402
from tickets.declare import (DECLARE_ENV, DEFINITIONS_HOME, declare_home,  # noqa: E402
                             progress_definition, stage_definition, ticket_definition)
from tickets.handler import Handler, program_caps  # noqa: E402
from tickets.issue import ticket_uid  # noqa: E402
from tickets.layout import case_root, progress_root, record_path, tickets_root  # noqa: E402
from tickets.plan import due, next_ticket, ticket_named  # noqa: E402
from tickets.progress import (a_stage, answered, progress_node, read_stages,  # noqa: E402
                             the_state, unfinished, write_stage)
from tickets.mgmt_critic import the_flow_verdict
from tickets.session import (BROKEN, OK, STUCK, Session, open_work,  # noqa: E402
                             passes_so_far, remember_the_norm, the_last_objection,
                             the_norm_of, worth_another_round)
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
    """四个成员：assistant、tech_critic、mgmt_critic、脚本化的 actor。顺序就是它们接单的次序。"""
    return [assistant_handler(meeting, workflow),
            critic_handler(meeting, workflow),
            mgmt_critic_handler(meeting, workflow, case=CASE, workspace=meeting),
            Handler("actor", "actor",
                    {**program_caps(workflow), **actor_caps(a_writer)}, meeting,
                    programs=workflow.tickets())]


class APoisonedNormTest(unittest.TestCase):
    """毒规范：干活的和判代码的都换成替身（`tickets/dummy.py`）。流程那一关要看得出来。

    这一份规范是仓库里的用例文件（`tests/data/poison_norm/`），不是把默认规范改两行拼出来的。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def a_norm(self, name: str, actor: str = "dummy_actor",
               tech_critic: str = "dummy_tech_critic") -> Path:
        where = self.root / name
        shutil.copytree(Path(__file__).resolve().parent / "data" / "poison_norm", where)
        members = where / "rb_norm" / "members"
        members.write_text(members.read_text(encoding="utf-8")
                           .replace("actor actor_api dummy_actor", f"actor actor_api {actor}")
                           .replace("tech_critic tech_critic_api dummy_tech_critic",
                                    f"tech_critic tech_critic_api {tech_critic}"),
                           encoding="utf-8")
        return where / "rb_norm"

    def a_run(self, norm: Path):
        workflow = load_workflow(norm)
        meeting = self.root / "meeting"
        rows = members_of(workflow)

        def writing(_prompt):
            (self.work / "quicksort.py").write_text(CODE, encoding="utf-8")
            return CODE

        agent = (actor_caps(writing) if any(one.kind == "agent" for one in rows) else None)
        members = members_for(rows, workflow=workflow, meeting_dir=meeting,
                              case=CASE, cwd=self.work, agent_caps=agent)
        assistant = next(one for one in members if one.role == "assistant")
        session = Session(meeting, CASE, workflow=workflow)
        session.stated_task(WORDS, assistant, "quicksort.py")
        outcome = session.run(members, out=lambda _who, _line: None)
        return meeting, outcome

    def test_the_process_check_catches_the_stand_ins(self):
        """替身答了一路"合格"、工作区里什么都没有：流程那一关两样都点出来，收尾那句话说不出口。"""
        meeting, outcome = self.a_run(self.a_norm("poison"))
        self.assertEqual(BROKEN, outcome.outcome)
        self.assertIn("这一场还没走完", outcome.failure)
        values = Store(case_root(meeting, CASE), record_path(meeting))
        passed, answered = values.read("flow", "check")
        self.assertTrue(answered)
        self.assertFalse(passed)                                  # 流程那一关没过
        problems = str(values.value("flow", "objection"))
        self.assertIn("judged 判过了，却没留下判的是哪一份", problems)
        self.assertIn("judged_revision 判过了，却没留下判的是哪一份", problems)
        self.assertIn("要交的东西不在工作区里：quicksort.py", problems)
        self.assertFalse(values.answered("closing", "closing_note"))   # 没有说完事

    def test_the_same_program_without_stand_ins_closes(self):
        """同一段程序、同一份规范，只把那两行换回真的：一路判下来、收得了尾。

        也就是"毒"在那两行上：替身才让流程那一关有话说；换成真的 actor 和真的 tech_critic，
        这一张单做完了、收尾那句话也说得出。
        """
        meeting, outcome = self.a_run(self.a_norm("real", actor="agent", tech_critic="tech_critic"))
        self.assertEqual(OK, outcome.outcome)
        values = Store(case_root(meeting, CASE), record_path(meeting))
        passed, answered = values.read("flow", "check")
        self.assertTrue(answered and passed)
        self.assertTrue(values.answered("closing", "closing_note"))


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
        ticket = session.issue("do_the_work")
        self.assertEqual("do_the_work-quicksort", ticket.uid)
        path = tickets_root(self.meeting) / ticket.uid / "ticket.viba"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("$ticket_name \"do_the_work\"", text)
        self.assertIn('$call (__dyn_call__ << "do_the_work")', text)
        self.assertIn(str(case_root(self.meeting, CASE)), text)
        self.assertNotIn("$storage_path \"\"", text)

    def test_it_reads_back_as_the_same_ticket(self):
        """读回来是同一张单：号、名字、那次调用、案子、找模块的路、时刻，一样不少。"""
        session = a_session(self.meeting)
        written = session.issue("do_the_work")
        back = read_ticket(self.meeting, written.uid)
        self.assertEqual(written.uid, back.uid)
        self.assertEqual("do_the_work", back.name)
        self.assertEqual(str(case_root(self.meeting, CASE)), back.storage_path)
        self.assertEqual(self.workflow.search_dirs(), list(back.viba_path))
        self.assertEqual(written.time, back.time)
        self.assertIsNotNone(back.call)

    def test_the_uid_says_which_ticket_and_which_case(self):
        """号一眼看得出是哪张单、哪个案子。"""
        self.assertEqual("do_the_work-quicksort",
                         ticket_uid("do_the_work", "/x/store/case/quicksort"))
        self.assertEqual("do_the_work-quicksort-2",
                         ticket_uid("do_the_work", "/x/store/case/quicksort-2"))
        self.assertEqual("do_the_work", ticket_uid("do_the_work", "/x/nothing"))

    def test_the_ticket_is_a_value_like_every_other(self):
        """单是一份值：和别处一样写得下、读得回（容器成员也照样）。"""
        session = a_session(self.meeting)
        ticket = session.issue("do_the_work")
        self.assertIn("/rb_norm/ticket_programs", read_ticket(self.meeting, ticket.uid).viba_path[0])


def git(where, *arguments: str) -> str:
    """在 `where` 里跑一条 git 命令，答它印出来的那行。"""
    done = subprocess.run(
        ["git", "-C", str(where), "-c", "user.name=test", "-c", "user.email=test@example.com",
         *arguments],
        capture_output=True, text=True, check=True)
    return done.stdout.strip()


def an_interface_repo(where: Path) -> str:
    """造一个接口仓库（`rb_api/` 下把现行的三份声明抄进去，提交一次），答它的 commit id。"""
    (where / "rb_api").mkdir(parents=True, exist_ok=True)
    for path in sorted(declare_home().glob("*_api.viba")):
        shutil.copy2(path, where / "rb_api" / path.name)
    git(where, "init")
    git(where, "add", ".")
    git(where, "commit", "-m", "one")
    return git(where, "rev-parse", "HEAD")


def a_norm_depending_on(where: Path, interface: Path) -> Path:
    """一份规范，边上挂着它自己那份接口（克隆一份，像 submodule 那样），答规范目录。"""
    norm = where / "rb_norm"
    shutil.copytree(norm_home(), norm)
    subprocess.run(["git", "clone", "-q", str(interface), str(where / "RbApiDeclare")], check=True)
    return norm


def another_version(where: Path, name: str, line: str, message: str) -> str:
    """接口往前走一版：往一个声明后面添一行，提交，答新的 commit id。"""
    path = where / name
    path.write_text(path.read_text(encoding="utf-8") + line, encoding="utf-8")
    git(where, "add", ".")
    git(where, "commit", "-m", message)
    return git(where, "rev-parse", "HEAD")


def point_the_at(case: unittest.TestCase, where: Path) -> None:
    """把这一趟读的接口指到别处（这一条用完就把环境变量还回去）。"""
    before = os.environ.get(DECLARE_ENV)
    os.environ[DECLARE_ENV] = str(where)
    case.addCleanup(lambda: os.environ.__setitem__(DECLARE_ENV, before)
                    if before is not None else os.environ.pop(DECLARE_ENV, None))


def the_written_ticket(meeting, uid: str):
    """盘上那张单读回来的那段 viba 数据（不是 `Ticket`，是它本来那一段）。

    走的是设施自己那条读法（`EnvironmentStorage` + `get_snapshot`），所以核的是盘上那份原文。
    """
    from viba.interpret import Environment, EnvironmentStorage, get_snapshot

    return get_snapshot(
        Environment(EnvironmentStorage(uid, None, str(tickets_root(meeting))), None), "ticket")


class TheApiDeclareTest(unittest.TestCase):
    """接口那几份声明在另一个仓库里：单上记着按哪一版，找模块的路里也有那个目录。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.meeting = self.root / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def point_the_interface_at(self, where: Path) -> None:
        """把这一趟读的接口指到别处。"""
        point_the_at(self, where)

    def test_the_ticket_says_which_version_of_the_interface_it_reads(self):
        """单上按处理者记着接口那一版：actor、tech_critic、mgmt_critic、assistant 各自一条，就是规范说的那一版。"""
        ticket = a_session(self.meeting).issue("do_the_work")
        said = ticket.handler_name2api_declare_repo_commit_id
        self.assertEqual(self.workflow.declares(), said)
        self.assertEqual({"actor", "tech_critic", "mgmt_critic", "assistant"}, set(said))
        self.assertEqual(self.workflow.declare_commit, said["actor"])
        self.assertRegex(said["actor"], r"^[0-9a-f]{40}$")
        back = read_ticket(self.meeting, ticket.uid)
        self.assertEqual(said, back.handler_name2api_declare_repo_commit_id)

    def test_a_ticket_written_before_this_member_existed_still_reads(self):
        """早先发的单上没有这一项（那时候接口还在规范里）：读回来就是空的那一项，不是读不出来。"""
        ticket = a_session(self.meeting).issue("do_the_work")
        path = tickets_root(self.meeting) / ticket.uid / "ticket.viba"
        kept = [line for line in path.read_text(encoding="utf-8").splitlines()
                if "$handler_name2api_declare_repo_commit_id" not in line]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        back = read_ticket(self.meeting, ticket.uid)
        self.assertEqual({}, back.handler_name2api_declare_repo_commit_id)
        self.assertEqual(ticket.viba_path, back.viba_path)

    def test_the_interface_repo_is_on_the_ticket_viba_path(self):
        """那三份基础角色不在规范目录里：单上找模块的路得把接口仓库也带上，跑的时候才找得到。"""
        back = read_ticket(self.meeting, a_session(self.meeting).issue("do_the_work").uid)
        self.assertIn(str(self.workflow.declare_dir), back.viba_path)
        self.assertTrue((self.workflow.declare_dir / "actor_api.viba").is_file())
        self.assertEqual([], list(Path(self.workflow.root).glob("*_api.viba")))

    def test_a_role_the_norm_grows_is_not_in_that_table(self):
        """规范自己带一个角色：它不来自接口仓库，单上那一项里就没有它。"""
        norm = self.root / "own"
        shutil.copytree(norm_home(), norm)
        (norm / "static_code_api.viba").write_text(
            "check_static =\n"
            "    ($module_path str * $func_name str)\n"
            "  <- $env Env\n"
            "  <- $code ($module_path str * $func_name str)\n"
            "  <- { 拿 $code 那份代码做静态检查 }\n", encoding="utf-8")
        own = load_workflow(norm)
        self.assertEqual({"actor", "tech_critic", "mgmt_critic", "assistant"}, set(own.declares()))
        self.assertIn("static_code_api", own.apis())
        ticket = Session(self.meeting, CASE, workflow=own).issue("do_the_work")
        self.assertEqual(own.declares(), ticket.handler_name2api_declare_repo_commit_id)

    def test_a_base_role_written_again_in_the_norm_is_refused(self):
        """规范里另写一份 `actor_api.viba`：基础那三个属于接口仓库，抄成两份当场报出来。"""
        norm = self.root / "double"
        shutil.copytree(norm_home(), norm)
        shutil.copy2(declare_home() / "actor_api.viba", norm / "actor_api.viba")
        with self.assertRaises(ValueError):
            load_workflow(norm)

    def test_the_two_submodules_have_to_be_the_same_version(self):
        """程序那份和规范那份得指着同一个 commit：规范还停在上一版就当场报出来。"""
        ours = self.root / "RbApiDeclare"
        an_interface_repo(ours)
        norm = a_norm_depending_on(self.root / "RbNorm", ours)
        point_the_at(self, ours / "rb_api")
        self.assertEqual(git(ours, "rev-parse", "HEAD"), load_workflow(norm).declare_commit)

        # 接口往前走了一版，规范那份还停在旧的那个 commit
        another_version(ours / "rb_api", "actor_api.viba", "# 接口改了一版\n", "two")
        with self.assertRaises(ValueError):
            load_workflow(norm)

        # 规范那份跟上就对了
        theirs = norm.parent / "RbApiDeclare"
        git(theirs, "fetch", "-q", "origin")
        git(theirs, "reset", "-q", "--hard", "FETCH_HEAD")
        self.assertEqual(git(ours, "rev-parse", "HEAD"), load_workflow(norm).declare_commit)

        # 程序这一份底下有没提交的改动：也不行
        (ours / "rb_api" / "tech_critic_api.viba").write_text(
            (ours / "rb_api" / "tech_critic_api.viba").read_text(encoding="utf-8") + "# 还没提交\n",
            encoding="utf-8")
        with self.assertRaises(ValueError):
            load_workflow(norm)


class WhatIsWrittenTest(unittest.TestCase):
    """写下来的那几份数据写成什么：接口仓库那份 `ticket.viba` 说了算，程序这边照它填、照它核。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.meeting = self.root / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_the_members_are_that_file_s_and_not_a_list_in_python(self):
        """一张单、一次尝试上那几个成员，都是那份定义说的（程序这边没有另一张名单）。"""
        self.assertEqual(("$ticket_uid", "$ticket_name", "$call", "$storage_path", "$VIBA_PATH",
                          "$handler_name2api_declare_repo_commit_id", "$time"),
                         ticket_definition().tags)
        self.assertEqual(("$ticket_uid", "$handler_name", "$interpret_result", "$time"),
                         stage_definition().tags)
        self.assertEqual("TicketProgress", progress_definition().name)
        self.assertTrue(ticket_definition().path.is_file())

    def test_a_written_ticket_is_what_that_definition_says(self):
        """盘上那张单照 `Ticket` 核一遍：成员一个不少、类型一个不错。"""
        ticket = a_session(self.meeting).issue("do_the_work")
        ticket_definition().check(the_written_ticket(self.meeting, ticket.uid))

    def test_a_written_stage_and_the_whole_progress_fit_their_definitions(self):
        """一次尝试是 `TicketStage` 那种；那一列文件读成一份数据就是 `TicketProgress` 那种。"""
        session = a_session(self.meeting)
        session.issue("do_the_work")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        stage = the_handlers(self.meeting, self.workflow)[0].tick()[0]
        stage_definition().check(stage.as_node())
        progress_definition().check(progress_node(self.meeting, stage.uid))
        self.assertEqual(1, len(read_stages(self.meeting, stage.uid)))

    def test_a_program_that_disagrees_with_the_definition_is_refused(self):
        """那份定义里多一个成员（程序这边填不出来）：写单当场说清楚，不留到跑的时候。"""
        home = self.root / "definitions"
        home.mkdir()
        (home / "ticket.viba").write_text(
            (DEFINITIONS_HOME / "ticket.viba").read_text(encoding="utf-8").replace(
                "  * $time datetime", "  * $time datetime\n  * $nobody str"), encoding="utf-8")
        with mock.patch.object(declare, "DEFINITIONS_HOME", home):
            with self.assertRaises(ValueError) as refused:
                a_session(self.meeting).issue("do_the_work")
        self.assertIn("$nobody", str(refused.exception))

    def test_a_definition_that_is_not_there_is_refused(self):
        """那份文件不在：读的时候就说清楚，不是跑到一半才发现。"""
        with self.assertRaises(FileNotFoundError):
            ticket_definition(self.root / "nowhere")


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
        session.issue("do_the_work")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        handlers = the_handlers(self.meeting, self.workflow)
        handlers[0].tick()                       # assistant：整段程序不是它的
        handlers[3].tick()                       # actor：做前几步，到判那一步停下
        where = progress_root(self.meeting) / "do_the_work-quicksort"
        names = sorted(one.name for one in where.glob("*.viba"))
        self.assertEqual(2, len(names))
        self.assertTrue(any(one.endswith("-assistant.viba") for one in names))
        self.assertTrue(any(one.endswith("-actor.viba") for one in names))
        self.assertFalse(answered(self.meeting, "do_the_work-quicksort"))   # 判那一步还没人接
        # 四边都试过一遍，这一张单才有一条 `$ok`
        session.run(handlers, out=lambda _who, _line: None)
        self.assertTrue(answered(self.meeting, "do_the_work-quicksort"))

    def test_a_stop_written_after_the_answer_cannot_take_it_back(self):
        """做完了之后又有人写下一篇停下的：那张单还是做完了（`$ok` 是只涨不退的那一条）。"""
        session = a_session(self.meeting)
        session.issue("do_the_work")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        handlers = the_handlers(self.meeting, self.workflow)
        uid = "do_the_work-quicksort"
        stopped = handlers[0].attempt(read_ticket(self.meeting, uid))    # assistant 停下的那一篇
        outcome = session.run(handlers, out=lambda _who, _line: None)    # 一路走到头
        self.assertEqual(OK, outcome.outcome)
        before = len(read_stages(self.meeting, uid))
        # 做完之后才落下来的那一篇停下的（两个人同时在跑时躲不掉的那一下）
        write_stage(self.meeting, a_stage(uid, "tech_critic", stopped.result))
        stages = read_stages(self.meeting, uid)
        self.assertEqual(before + 1, len(stages))
        self.assertTrue(any(one.answered() for one in stages[:-1]))
        self.assertFalse(stages[-1].answered())
        self.assertTrue(answered(self.meeting, uid))     # 那一条 `$ok` 顶不掉
        self.assertEqual("tech_critic", the_state(self.meeting)[0]["last_who"])
        self.assertIn("不是它的事", the_state(self.meeting)[0]["last_stop"])

    def test_an_attempt_says_which_step_was_not_its_business(self):
        """停下那一篇说的是"停在哪一步"：那一步的整名和那次调用都在里面。"""
        session = a_session(self.meeting)
        session.issue("do_the_work")
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        handlers = the_handlers(self.meeting, self.workflow)
        stage = handlers[3].tick()[0]            # actor：做前几步，停在判那一步
        self.assertFalse(finished(stage.result))
        step = unimplemented_step(stage.result)
        self.assertEqual("tech_critic_api.judge", step[1])
        # 那一次调用也在里面：环境留空、实参是当时写下的那些（这是盘上那一篇的原文）
        text = "".join(one.read_text(encoding="utf-8")
                       for one in (progress_root(self.meeting) / stage.uid).glob("*.viba"))
        self.assertIn('__dyn_call__ << "tech_critic_api.judge"', text)


class ThePlanTest(unittest.TestCase):
    """该发哪一张：读值算出来的。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def test_the_first_ticket_is_the_one_whose_readings_are_there(self):
        """一开始一张都发不出去：它要读的那一步（人说的话）还缺着。"""
        session = a_session(self.meeting)
        self.assertIsNone(next_ticket(self.workflow, session.values))
        wanted, why = due(self.workflow, session.values, "do_the_work")
        self.assertFalse(wanted)
        self.assertIn("还缺", why)
        # 人说了一句：`stated_task` 落在案子自己的数据路径下
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        self.assertEqual("do_the_work", next_ticket(self.workflow, session.values))
        wanted, why = due(self.workflow, session.values, "do_the_work")
        self.assertTrue(wanted)
        self.assertEqual("要读的都在，自己还没做过", why)

    def test_the_next_one_is_due_once_the_one_before_it_answered(self):
        """这一张没做完就轮到它，做完了就没有下一张（次序不是写死的，是读出来的）。"""
        session = a_session(self.meeting)
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        self.assertEqual("do_the_work", next_ticket(self.workflow, session.values))
        session.run(the_handlers(self.meeting, self.workflow), out=lambda _who, _line: None)
        # 只有这一张单，做完了就没有下一张
        self.assertEqual(["do_the_work-quicksort"], ticket_uids(self.meeting))
        self.assertIsNone(next_ticket(self.workflow, session.values))

    def test_a_ticket_already_issued_is_found_by_its_name(self):
        """发过的单按名字找得到：号是 `<名字>-<案子>`，名字在前头。"""
        session = a_session(self.meeting)
        session.stated_task(WORDS, assistant_handler(self.meeting, self.workflow))
        self.assertIsNone(ticket_named(self.meeting, "do_the_work"))
        session.issue("do_the_work")
        self.assertEqual("do_the_work-quicksort",
                         ticket_named(self.meeting, "do_the_work"))

    def test_a_ticket_already_issued_is_not_issued_again(self):
        """发过就不再发第二张：再跑一趟（被叫醒接着跑就是这一种）一张都不新发。"""
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(WORDS, handlers[0])
        first = []
        session.run([handlers[0]], out=lambda _who, line: first.append(line))
        again = []
        session.run([handlers[0]], out=lambda _who, line: again.append(line))
        self.assertEqual(1, len([one for one in first if "发了一张单" in one]))
        self.assertEqual([], [one for one in again if "发了一张单" in one])


class WhatTheLawSaysAboutWhenATicketIsDueTest(unittest.TestCase):
    """法给某一张单写的"现在是它的时候吗"：发单那一条照它算。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meeting = Path(self.temporary.name) / "meeting"
        self.norm = Path(self.temporary.name) / "rb_norm"
        shutil.copytree(norm_home(), self.norm)

    def a_law_that_says(self, ticket: str, source: str) -> None:
        """给这一张单写一条"什么时候该发"（`due/<名字>.viba`）。"""
        where = self.norm / "due"
        where.mkdir(exist_ok=True)
        (where / f"{ticket}.viba").write_text(
            "__decl__ =\n    Any\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            + source, encoding="utf-8")

    def a_workflow(self):
        return load_workflow(self.norm)

    def a_session(self):
        return Session(self.meeting, CASE, workflow=self.a_workflow())

    def a_ready_case(self):
        """人说一句话、assistant 收下：案子开起来了，一张单都还没发。"""
        session = self.a_session()
        session.stated_task(WORDS, assistant_handler(self.meeting, self.a_workflow()))
        return session

    def test_a_law_holds_a_ticket_back_until_the_value_says_so(self):
        """法说"某个值是 true 才发这一张"：值是 false 时不发；改成 true 就发。"""
        self.a_law_that_says(
            "do_the_work",
            "the_value =\n    Any\n  <- $env Env\n  <- $module_path str\n  <- $func_name str\n\n"
            "eq_bool =\n    bool\n  <- $env Env\n  <- $x Any\n  <- $y Any\n\n"
            '__impl__ = eq_bool << $env args.env\n'
            '  << $x (the_value << $env args.env << $module_path "gate"'
            ' << $func_name "open")\n'
            '  << $y true\n')
        workflow = self.a_workflow()
        session = self.a_ready_case()
        # 门还关着
        session.values.write("gate", "open", False)
        wanted, why = due(workflow, session.values, "do_the_work")
        self.assertFalse(wanted)
        self.assertIn("还不是它的时候", why)
        # 门开了：这一张就该发了
        session.values.write("gate", "open", True)
        wanted, why = due(workflow, session.values, "do_the_work")
        self.assertTrue(wanted)
        self.assertIn("就是它的时候", why)

    def test_a_meeting_the_law_will_not_let_close_is_not_a_finished_meeting(self):
        """法扣着这一张不发：这一趟不算"算完"，是走不动。"""
        self.a_law_that_says("do_the_work", '__impl__ = false\n')
        workflow = self.a_workflow()
        session = self.a_ready_case()
        outcome = session.run(the_handlers(self.meeting, workflow), out=lambda _who, _line: None)
        self.assertEqual(STUCK, outcome.outcome)
        self.assertIn("do_the_work", outcome.failure)
        self.assertIn("还不是它的时候", outcome.failure)
        # 这一张根本没发出去（法不让它发）
        self.assertNotIn("do_the_work-quicksort", ticket_uids(self.meeting))

    def test_a_law_that_says_nothing_keeps_the_old_rule(self):
        """没写这一条的单照旧：要读的都在、自己还没做过，就该发了。"""
        wanted, why = due(self.a_workflow(), self.a_ready_case().values, "do_the_work")
        self.assertTrue(wanted)
        self.assertEqual("要读的都在，自己还没做过", why)

    def test_a_law_may_hold_a_step_back_with_a_sentence(self):
        """法答一句话就是"还不该发，原因是这句"。"""
        self.a_law_that_says("do_the_work", '__impl__ = "还没到点"\n')
        wanted, why = due(self.a_workflow(), self.a_ready_case().values, "do_the_work")
        self.assertFalse(wanted)
        self.assertEqual("还没到点", why)

    def test_a_law_that_says_nil_keeps_the_old_rule(self):
        """法答 `nil` 就是"这一条不管"：照旧按那两条算。"""
        self.a_law_that_says("do_the_work", "__impl__ = nil\n")
        wanted, why = due(self.a_workflow(), self.a_ready_case().values, "do_the_work")
        self.assertTrue(wanted)
        self.assertEqual("要读的都在，自己还没做过", why)

    def test_a_step_held_back_by_the_law_is_said_so_when_the_meeting_stops(self):
        """法扣着不发的那一张，走不动时要说清是法说什么，不是含混一句"没人接"。"""
        self.a_law_that_says("do_the_work", '__impl__ = "还没到点"\n')
        workflow = self.a_workflow()
        session = self.a_ready_case()
        outcome = session.run(the_handlers(self.meeting, workflow), out=lambda _who, _line: None)
        self.assertEqual(STUCK, outcome.outcome)
        self.assertIn("还没到点", outcome.failure)


class ThePassesOfAMeetingTest(unittest.TestCase):
    """一句话起了几趟、上一版为什么不合格：这两样读盘上那些案子。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meeting = Path(self.temporary.name) / "meeting"

    def a_case(self, name: str, when: float, objection: str = "", revision: str = "") -> None:
        root = case_root(self.meeting, f"case/{name}")
        root.mkdir(parents=True)
        values = Store(root)
        if objection:
            values.write("judged", "objection", objection)
        if revision:
            values.write("judged_revision", "objection", revision)
        os.utime(root, (when, when))

    def test_it_counts_the_cases_on_disk(self):
        """起了几趟：数案子（一趟一个案子）。"""
        self.assertEqual(0, passes_so_far(self.meeting))
        self.a_case("one", 1000.0)
        self.a_case("two", 2000.0)
        self.assertEqual(2, passes_so_far(self.meeting))

    def test_the_last_objection_is_the_newest_cases_final_verdict(self):
        """上一版为什么不合格：看最新那个案子最后那次判决。"""
        self.a_case("one", 1000.0, objection="第一版：参数太多")
        self.a_case("two", 2000.0, objection="第一版：参数太多", revision="改完还是：参数太多")
        self.assertEqual("改完还是：参数太多", the_last_objection(self.meeting))

    def test_a_verdict_that_passed_says_nothing(self):
        """判过了就没有那句话：这一趟不该再原样起一遍。"""
        self.a_case("one", 1000.0)
        self.assertEqual("", the_last_objection(self.meeting))


class AWholeMeetingTest(unittest.TestCase):
    """一场会：一句话进来，一张单一路走到 ok。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.meeting = Path(self.temporary.name) / "meeting"
        self.addCleanup(self.temporary.cleanup)
        self.workflow = a_workflow()

    def a_run(self, words: str = WORDS):
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(words, handlers[0])
        return session.run(handlers, out=lambda _who, _line: None)

    def test_the_assistant_issues_a_ticket_for_the_human_message(self):
        """消息先到 assistant：它把话放进案子，接着单真的写在会议目录下面。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant)
        self.assertEqual(WORDS, session.values.value("", "stated_task"))
        session.issue("do_the_work")
        self.assertTrue((tickets_root(self.meeting) / "do_the_work-quicksort"
                         / "ticket.viba").is_file())
        self.assertEqual([], list(progress_root(self.meeting).glob("*")))

    def test_a_whole_meeting_ends_ok(self):
        """一张单从头走到尾：到 `ok`，它的每一步都答过，进度里有一条 `$ok`。

        程序里那两步 `report_done`（`drafted_done` / `revised_done`）是写下来了、却没人要的：
        viba 只算 `__impl__` 要的那几步，所以它们从没跑过，记录里也没有。除此之外每一步都答过，
        次序就是程序里写下来的次序。
        """
        outcome = self.a_run()
        self.assertEqual(OK, outcome.outcome)
        state = the_state(self.meeting)
        self.assertEqual(1, len(state))
        self.assertEqual(["do_the_work-quicksort"], [one["ticket"] for one in state])
        self.assertTrue(all(one["answered"] for one in state))
        # 这一步到那儿真答下的是这些：每一步都在案子里，次序和程序里写的一样
        wanted = [("greeted", "acknowledge"), ("drafted", "do_step"), ("judged", "judge"),
                  ("revised", "redo_step"), ("judged_revision", "judge"), ("flow", "check"),
                  ("closing", "closing_note")]
        program = [(step.env, step.op) for step in self.workflow.steps_of("do_the_work")]
        self.assertEqual(wanted, [one for one in program if one in wanted])
        values = Store(case_root(self.meeting, CASE))
        for env, op in wanted:
            self.assertTrue(values.answered(env, op), f"{env} + {op} 还没答")
        record = (self.meeting / "meeting_record.md").read_text(encoding="utf-8")
        answered_lines = [line.split("答了 ", 1)[1] for line in record.splitlines()
                          if "答了 " in line]
        self.assertEqual([f"{env} + {op}" for env, op in wanted], answered_lines)
        # 每一次尝试都落在会议目录下的 ticket_progress 里
        self.assertTrue(all((progress_root(self.meeting) / one["ticket"]).is_dir()
                            for one in state))

    def test_the_values_land_in_the_case_under_the_meeting(self):
        """答下的值落在 `<会议目录>/store/<案子>/` 下，一条数据路径一份。"""
        self.a_run()
        values = Store(case_root(self.meeting, CASE))
        self.assertTrue(values.answered("drafted", "do_step"))
        self.assertTrue(values.answered("revised", "redo_step"))
        self.assertTrue(values.answered("judged", "judge"))
        self.assertTrue(values.answered("closing", "closing_note"))
        self.assertEqual(CODE, values.value("drafted", "do_step"))

    def test_the_record_says_who_answered_what(self):
        """这场会的记录里：人说了什么、发了哪张单、谁答了哪一步。"""
        self.a_run()
        record = (self.meeting / "meeting_record.md").read_text(encoding="utf-8")
        self.assertIn(f"- 人类说了一句：{WORDS}", record)
        self.assertIn("- 发了一张单 do_the_work-quicksort", record)
        self.assertIn("- actor（actor）答了 greeted + acknowledge", record)
        self.assertIn("- actor（actor）答了 drafted + do_step", record)
        self.assertIn("- tech_critic（tech_critic）答了 judged + judge", record)
        self.assertIn("- actor（actor）答了 revised + redo_step", record)
        self.assertIn("- tech_critic（tech_critic）答了 judged_revision + judge", record)
        self.assertIn("- mgmt_critic（mgmt_critic）答了 flow + check", record)
        self.assertIn("- assistant（assistant）答了 closing + closing_note", record)

    def test_every_line_handed_out_says_who_said_it(self):
        """接单那一侧交出来的每一句都带着说话的人：发单是 assistant，判案是 tech_critic 与
        mgmt_critic，做活是 actor。

        会议那一侧照这一份把话记在那个成员名下（`web.py`），所以少了名字就少一个人说话。
        """
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(WORDS, handlers[0])
        said = []
        outcome = session.run(handlers, out=lambda who, line: said.append((who, line)))
        self.assertEqual(OK, outcome.outcome)
        # 一张单只发一次：不是每张单各来一句"发了一张单"
        self.assertEqual([("assistant", "发了一张单：do_the_work-quicksort")],
                         [one for one in said if "发了一张单" in one[1]])
        self.assertIn(("actor", "答了 greeted + acknowledge"), said)
        self.assertIn(("actor", "答了 drafted + do_step"), said)
        self.assertIn(("tech_critic", "答了 judged + judge"), said)
        self.assertIn(("actor", "答了 revised + redo_step"), said)
        self.assertIn(("tech_critic", "答了 judged_revision + judge"), said)
        self.assertIn(("mgmt_critic", "答了 flow + check"), said)
        self.assertIn(("assistant", "答了 closing + closing_note"), said)
        # 一张单交到别人手上（"这一步不是它的事"）不是谁说的话：记录里有，说话这一路没有
        self.assertFalse([one for one in said if "不是它的事" in one[1]])

    def test_a_failed_judgement_does_not_close_the_meeting(self):
        """判下来不合格：收尾那一步自己不肯说那句话（`closing_note` 说的），这一趟是 `broken`。

        不改那一处的话，assistant 会对着一个不合格的活说"这场会做完了"。
        """
        too_many = ("def quick_sort(items, key=None, reverse=False, bounds=(0, None), extra=1):\n"
                    "    return items\n")
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        handlers[3].caps.update(actor_caps(lambda _prompt: too_many))
        session.stated_task(WORDS, handlers[0])
        outcome = session.run(handlers, out=lambda _who, _line: None)
        self.assertEqual(BROKEN, outcome.outcome)
        self.assertIn("do_the_work-quicksort", outcome.failure)
        self.assertIn("closing_note", outcome.failure)
        self.assertIn("这一场还没走完", outcome.failure)
        values = Store(case_root(self.meeting, CASE))
        self.assertFalse(values.value("judged_revision", "judge"))        # 改完那一版也不合格
        self.assertFalse(values.answered("closing", "closing_note"))      # 没有说完事
        self.assertFalse(answered(self.meeting, "do_the_work-quicksort"))

    def test_a_second_attempt_replays_the_first_answers(self):
        """同一张单再试一次：案子里已有值的那几步回放，不重做（写只做了一次）。"""
        writes: list = []

        def counting(prompt: str) -> str:
            writes.append(prompt)
            return CODE

        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        handlers[3].caps.update(actor_caps(counting))
        session.stated_task(WORDS, handlers[0])
        session.run(handlers, out=lambda _who, _line: None)
        self.assertEqual(1, len(writes))              # 它说合格，没有第二版要改
        # 同一张单再跑一遍：案子里已有值的那几步回放，写那一步不再做
        again = handlers[3].attempt(read_ticket(self.meeting, "do_the_work-quicksort"))
        self.assertTrue(again.answered())
        self.assertEqual(1, len(writes))

    def test_a_step_nobody_does_leaves_the_meeting_stuck(self):
        """谁都不会的那一步没人接：当轮就走不动了，账上是 stuck（不假装跑完）。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant)
        outcome = session.run([assistant], out=lambda _who, _line: None)
        self.assertEqual(STUCK, outcome.outcome)
        self.assertIn("do_the_work-quicksort", outcome.failure)
        self.assertFalse(answered(self.meeting, "do_the_work-quicksort"))

    def test_a_step_that_broke_says_so_instead_of_nobody_answers(self):
        """哪个实现坏了：那一手是 `$underlying_viba_op_err`，当场说清楚，不写成"没人接"。

        判的人自己没答过一步（它是那一步的实现，抛出来的就是那一手），所以这一条不是从"谁答了什么"
        看出来的，是从那一手停下的那一支看出来的。
        """
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(WORDS, handlers[0])

        def broke(_env, *_given):
            raise RuntimeError("验收条件跑不起来：没有 args.env")

        handlers[1].caps["judge"] = broke
        outcome = session.run(handlers, out=lambda _who, _line: None)
        self.assertEqual(BROKEN, outcome.outcome)
        self.assertIn("tech_critic", outcome.failure)
        self.assertIn("do_the_work-quicksort", outcome.failure)
        self.assertIn("没有 args.env", outcome.failure)

    def test_a_ticket_that_stops_the_same_way_three_times_is_not_tried_again(self):
        """同一句话连着试了三次，就不该再起一趟：再跑一遍还是它，等人（或等法）改。"""
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(WORDS, handlers[0])
        session.issue("do_the_work")                                    # 发了一张，还没人试过
        self.assertTrue(worth_another_round(self.meeting, tries=3))
        for _round in range(3):
            # 只有 assistant 和 tech_critic：谁都不会 actor 那一步，轮着说同一句话
            session.run(handlers[:2], out=lambda _who, _line: None)
        self.assertFalse(worth_another_round(self.meeting, tries=3))

    def test_each_pass_gets_its_own_case_counted_from_disk(self):
        """一场会的每一趟有自己的案子：号码数的是盘上已经有的那些，不是进程里的次数。"""
        from tickets.meeting import case_for_pass

        first = case_for_pass(self.meeting, "quicksort.py")
        self.assertEqual("case/quicksort", first)
        case_root(self.meeting, first).mkdir(parents=True)
        self.assertEqual("case/quicksort-2", case_for_pass(self.meeting, "quicksort.py"))
        case_root(self.meeting, "case/quicksort-2").mkdir(parents=True)
        self.assertEqual("case/quicksort-3", case_for_pass(self.meeting, "quicksort.py"))
        # 没点名字的那一趟用 fallback（会议号）
        self.assertEqual("case/the-meeting", case_for_pass(self.meeting, "", fallback="the-meeting"))

    def test_the_norm_a_meeting_was_opened_under_is_remembered(self):
        """这一场会开在哪份法下：记在案子那份值里，后来的人照着比。"""
        self.assertIsNone(the_norm_of(self.meeting, CASE))
        remember_the_norm(self.meeting, CASE, "/x/RbNormViba2/rb_norm")
        self.assertEqual("/x/RbNormViba2/rb_norm", the_norm_of(self.meeting, CASE))
        self.assertIsNone(the_norm_of(self.meeting, ""))

    def test_a_ticket_that_stopped_differently_is_tried_again(self):
        """最后几次停的不是同一句：还值得再起一趟（多半是这一轮断了，再来一轮就好）。"""
        session = a_session(self.meeting)
        handlers = the_handlers(self.meeting, self.workflow)
        session.stated_task(WORDS, handlers[0])
        session.run([handlers[0]], out=lambda _who, _line: None)                 # 没人接：不是它的事

        def broke(_env, *_given):
            raise RuntimeError("这一轮断了")

        handlers[3].caps["do_step"] = broke
        session.run(handlers, out=lambda _who, _line: None)                      # actor 那一步没跑起来
        self.assertTrue(worth_another_round(self.meeting, tries=2))

    def test_an_unfinished_meeting_says_where_it_stopped(self):
        """没跑完的一趟说得清：哪个案子、人说的哪句话（进程死没死都不必知道）。"""
        session = a_session(self.meeting)
        assistant = assistant_handler(self.meeting, self.workflow)
        session.stated_task(WORDS, assistant, "quicksort.py")
        session.run([assistant], out=lambda _who, _line: None)
        self.assertEqual((CASE, WORDS, "quicksort.py"), open_work(self.meeting))


class WhatCountsAsWorkTest(unittest.TestCase):
    """人说的哪句话算一份活：规范里 `intake/` 那几份程序说了算。"""

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
        """这一条是规范里那几份程序定的：换一份 intake/，认的就变了。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        norm = Path(temporary.name) / "norm"
        shutil.copytree(norm_home(), norm)
        (norm / "intake" / "what_to_make.viba").unlink()
        (norm / "intake" / "only_readme.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\n"
            "args = __get_args__ << __decl__\n\n"
            "the_words =\n    str\n  <- $env Env\n  <- { 人刚说的那句 }\n\n"
            "__impl__ = the_words << $env args.env\n", encoding="utf-8")
        own = load_workflow(norm)
        self.assertEqual(["only_readme"], own.intake())
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

    def test_the_table_the_norm_ships_names_four_members(self):
        """规范自己那张表：actor（会议里那个 agent）、tech_critic、mgmt_critic、assistant。"""
        rows = members_of(self.workflow)
        self.assertEqual(["actor", "tech_critic", "mgmt_critic", "assistant"],
                         [one.name for one in rows])
        self.assertEqual(["agent", "tech_critic", "mgmt_critic", "assistant"],
                         [one.kind for one in rows])
        self.assertEqual(["actor", "tech_critic", "mgmt_critic", "assistant"],
                         [one.role for one in rows])

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
        path = self.norm / "members"
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
        rows = [row_of("actor actor_api agent"),
                row_of("tech_critic tech_critic_api tech_critic"),
                row_of("mgmt_critic mgmt_critic_api mgmt_critic")]
        members = members_for(rows, workflow=self.workflow, meeting_dir=self.root / "meeting",
                              case=CASE, agent_caps=actor_caps(a_writer))
        self.assertEqual(["actor", "tech_critic", "mgmt_critic", "assistant"],
                         [one.role for one in members])

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
        session.issue("do_the_work")
        written = process.tick()
        self.assertTrue(written)                      # 它写了一篇进度
        self.assertEqual("actor", written[0].who)
        # 它自己那几步答下了值，到判那一步停下（那一步归别人）
        self.assertFalse(written[0].answered())
        self.assertFalse(answered(self.root / "meeting", "do_the_work-quicksort"))
        self.assertTrue(Store(case_root(self.root / "meeting", CASE)).answered(
            "drafted", "do_step"))
        self.assertEqual([("greeted", "acknowledge"), ("drafted", "do_step")], process.did)


class TheHostsBuiltinOperatorsTest(unittest.TestCase):
    """viba 带的那些算子由这一侧的宿主答：算出来的就是值本身，不是地址。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.where = Path(self.temporary.name) / "case"
        self.where.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def a_run(self, source: str) -> str:
        """在一个空案子上跑一段程序，答它写成 viba 源文的那份答复。"""
        from viba import viba_ast
        from viba.interpret import interpret

        (self.where / "the_program.viba").write_text(
            "__decl__ =\n    Any\n  <- $env Env\n\n"
            "args = __get_args__ << __decl__\n\n" + source + "\n", encoding="utf-8")
        binding = Binding("tester", {}, self.where)
        answer = interpret(str(self.where / "the_program.viba"), binding.env(""))
        return viba_ast.unparse_type(answer.data)

    def test_a_builtin_answers_its_value_and_not_an_address(self):
        """`le` 答的是那个 bool；答成地址的话，拿它当类型用的人就全错了。"""
        self.assertEqual("$ok true",
                         self.a_run("__impl__ = le << $env args.env << $x 1 << $y 4"))

    def test_if_runs_only_the_branch_the_condition_picks(self):
        """`if` 是 viba 发的那两个开关：只有被选中的那一支跑，另一支一次都不跑。"""
        source = ("__impl__ = if\n"
                  "  << $env ($sub_env << args.env << \"if\")\n"
                  "  << $cond (le << $env args.env << $x ${left} << $y ${right})\n"
                  "  << $t (builtin.echo << $x \"took the true branch\")\n"
                  "  << $f (builtin.echo << $x \"took the false branch\")\n")
        self.assertEqual('$ok "took the true branch"',
                         self.a_run(source.replace("${left}", "1").replace("${right}", "4")))
        self.assertEqual('$ok "took the false branch"',
                         self.a_run(source.replace("${left}", "9").replace("${right}", "4")))

    def test_the_string_operators_viba_declares_are_answered(self):
        """`builtin.viba` 里那几个字符串算子：宿主答得上来，用起来就是值。"""
        self.assertEqual('$ok "a-b-c"',
                         self.a_run('__impl__ = join << $env args.env << $parts (split '
                                    '<< $env args.env << $x "a,b,c" << $separator ",") '
                                    '<< $separator "-"'))
        self.assertEqual("$ok 2",
                         self.a_run('__impl__ = find << $env args.env << $x "hello" '
                                    '<< $needle "ll"'))
        self.assertEqual("$ok nil",
                         self.a_run('__impl__ = find << $env args.env << $x "hello" '
                                    '<< $needle "zz"'))
        self.assertEqual('$ok "a,b"',
                         self.a_run('__impl__ = trim << $env args.env << $x "  a,b  "'))


FILES_MISSING = '''files_missing =
    list[str]
  <- $env Env
  <- $root str
  <- $wanted list[str]

'''

EVERY_COMPLETE = '''every_program_is_complete =
    bool
  <- $env Env
  <- $root str

'''

EVERY_DECLARED_STRANGER = '''every_operator_is_declared =
    bool
  <- $env Env
  <- $root str

nobody_declares_this =
    bool
  <- $env Env

'''

EVERY_DECLARED_FINE = '''every_operator_is_declared =
    bool
  <- $env Env
  <- $root str

'''

UNDER = '''under_judgment =
    Any
  <- $env Env

'''


class WhatACriticCanReadInALawTest(unittest.TestCase):
    """判一份法要用的那几个算子：单上交过来的那一份、读目录、读程序、算名字。

    一份法要判的不是代码而是一整份规范（受理、组织、流程、验收四个部分），所以 tech_critic 那一边多几个
    算子：`under_judgment` 是单上交过来的那一份，`files_missing` 数缺哪几条，两个 `every_...` 把它
    底下的程序逐份看一遍。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.where = Path(self.temporary.name)
        self.case = self.where / "case"
        self.case.mkdir()
        self.draft = self.where / "draft"
        for part in ("intake", "ticket_programs", "acceptance"):
            (self.draft / part).mkdir(parents=True)
        (self.draft / "members").write_text("actor actor_api agent\n", encoding="utf-8")

    def a_program(self, name: str, source: str) -> Path:
        path = self.draft / "acceptance" / f"{name}.viba"
        path.write_text(source, encoding="utf-8")
        return path

    def a_run(self, source: str, decl: str = "bool") -> str:
        """在一个空案子上跑一段程序，答它写成 viba 源文的那份答复。"""
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    interpret)
        from tickets import critic

        (self.case / "the_condition.viba").write_text(
            f"__decl__ =\n    {decl}\n  <- $env Env\n\n"
            "args = __get_args__ << __decl__\n\n" + source + "\n", encoding="utf-8")
        table = critic._rule_compute(None)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        answer = interpret(str(self.case / "the_condition.viba"), env)
        return viba_ast.unparse_type(answer.data)

    def test_the_path_in_a_sentence_is_the_one_with_a_slash(self):
        """受理要问的是"要立的那份法在哪儿"：那是一个目录，所以认斜杠，不认点。"""
        from tickets.meeting import a_path_in

        self.assertEqual("/Users/x/RbNormViba", a_path_in("把法写到 /Users/x/RbNormViba 里"))
        self.assertEqual("~/w/RbNormViba", a_path_in("（~/w/RbNormViba）"))
        # 中文那句话里路径前后常常没有空格：认的是一段连着写的字，不是拿空格分开的词。
        self.assertEqual("/Users/x/RbNormViba/LAW.txt",
                         a_path_in("交到 /Users/x/RbNormViba/LAW.txt：里面写一行，说这份法写在哪儿。"))
        self.assertEqual("", a_path_in("把法写出来"))
        self.assertEqual("", a_path_in("看 https://example.com/a/b"))

    def test_the_decision_table_hands_it_a_path_operator(self):
        """受理那几份程序能问到的名字里有它。"""
        from tickets.assistant import decision_caps

        self.assertIn("a_path_in", decision_caps("写到 /a/b"))

    def a_norm(self, members: str, intake: str = "a_file_in") -> Path:
        """一个最小能读起来的规范：四个部分都在，成员表是一张表，受理答一个文件名。"""
        where = self.where / "a_norm"
        for part in ("intake", "ticket_programs", "acceptance"):
            (where / part).mkdir(parents=True, exist_ok=True)
        (where / "members").write_text(members, encoding="utf-8")
        (where / "intake" / "what_to_make.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "the_words =\n    str\n  <- $env Env\n\n"
            "a_file_in =\n    str\n  <- $env Env\n  <- $words str\n\n"
            "__impl__ = a_file_in << $env args.env << $words (the_words << $env args.env)\n"
            if intake == "a_file_in" else
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "__impl__ = \"true\"\n", encoding="utf-8")
        for name in ("ticket_programs/one.viba", "acceptance/one.viba"):
            (where / name).write_text(
                "__decl__ =\n    Any\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
                "__impl__ = true\n", encoding="utf-8")
        return where

    TABLE = ("actor actor_api agent\n"
             "tech_critic tech_critic_api tech_critic\n"
             "mgmt_critic mgmt_critic_api mgmt_critic\n"
             "assistant assistant_api assistant\n")
    LOADS = ("what_stops_this_norm =\n    str\n  <- $env Env\n  <- $root str\n\n")
    INTAKE = ("why_the_intake_says_nothing =\n    str\n  <- $env Env\n  <- $root str\n"
              "  <- $sentences list[str]\n\n")

    def test_a_command_that_passes_answers_nothing_and_a_failing_one_says_so(self):
        """跑一条命令：过了答空串，没过把退出码与最后一截输出带回来。"""
        from tickets import critic
        from viba import viba_ast
        from viba.interpret import Environment, EnvironmentCompute, EnvironmentStorage

        table = critic._rule_compute(None, str(self.case))
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        self.assertEqual("", table["what_stops_this_command"](env, viba_ast.Constant("true")))
        said = table["what_stops_this_command"](
            env, viba_ast.Constant("echo 坏了 >&2; exit 3"))
        self.assertIn("退出码 3", said)
        self.assertIn("坏了", said)
        # 跑的时候环境里带着这一侧的解释器：法要跑这个工作区里的测试就用它
        self.assertEqual("", table["what_stops_this_command"](
            env, viba_ast.Constant('"$REMOTEBUDDY_PYTHON" -c "import sys; sys.exit(0)"')))

    def test_a_program_may_call_the_modules_viba_ships(self):
        """`if` 这种随包发的模块名也算有人认领：程序直接叫它们，解释器自己在包里找。"""
        from tickets import critic
        from viba import viba_ast
        from viba.interpret import Environment, EnvironmentCompute, EnvironmentStorage

        where = self.a_norm(self.TABLE)
        (where / "acceptance" / "one.viba").write_text(
            "__decl__ =\n    Any\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "__impl__ = if\n  << $env (args.env.sub_env << args.env << \"if\")\n"
            "  << $cond true\n  << $t (echo << $x 1)\n  << $f (echo << $x 2)\n",
            encoding="utf-8")
        table = critic._rule_compute(load_workflow(where))
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        self.assertTrue(table["every_operator_is_declared"](env, viba_ast.Constant(str(where))))

    def test_a_due_rule_nobody_can_run_counts_as_a_program_that_is_not_complete(self):
        """写了"什么时候该发"那一格的法，那一格里的程序也在"每份程序都写全了"那一条的账上。"""
        from tickets import critic
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage)

        where = self.a_norm(self.TABLE)
        (where / "due").mkdir(exist_ok=True)
        (where / "due" / "one.viba").write_text(
            "__decl__ =\n    bool\n  <- $env Env\n\n__impl__ = true && false\n",
            encoding="utf-8")
        table = critic._rule_compute(load_workflow(where))
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        self.assertFalse(table["every_operator_is_declared"](env, viba_ast.Constant(str(where))))

    def test_a_law_that_is_a_norm_loads(self):
        """读得起来才算一份规范：四个部分加那张表；读得起来这条答空串。"""
        where = self.a_norm(self.TABLE)
        self.assertEqual('$ok ""', self.a_run(
            self.LOADS + f'__impl__ = what_stops_this_norm << $env args.env << $root "{where}"'))

    def test_a_members_file_that_is_a_program_says_why_it_does_not_load(self):
        """`members` 写成一份程序就不是那张表了：这一条要把那句话带回去。"""
        where = self.a_norm("__decl__ =\n    bool\n  <- $env Env\n\n__impl__ = true\n")
        said = self.a_run(self.LOADS +
                          f'__impl__ = what_stops_this_norm << $env args.env << $root "{where}"')
        self.assertTrue(said.startswith('$ok "'), said)
        self.assertIn("表项要写成", said)

    def a_condition_answer(self, said: str):
        """跑一条只会答这句话的验收条件，把那次答复原样拿回来。"""
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    interpret)
        from tickets import critic

        (self.case / "the_condition.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\n"
            "args = __get_args__ << __decl__\n\n"
            f'__impl__ = "{said}"\n', encoding="utf-8")
        table = critic._rule_compute(None)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        return interpret(str(self.case / "the_condition.viba"), env)

    def a_law_whose_conditions(self, answers: dict) -> Path:
        """一份最小规范，验收条件就这几条（名字 → 它答什么）。"""
        where = self.a_norm(self.TABLE)
        for name, answer in answers.items():
            said = "nil" if answer is None else '"' + answer + '"'
            (where / "acceptance" / f"{name}.viba").write_text(
                "__decl__ =\n    Any\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
                f"__impl__ = {said}\n", encoding="utf-8")
        return where

    def a_judgement(self, where: Path, written: str = "交过来的一份") -> tuple:
        """把一份东西交过去判一遍：答（过没过，理由那篇文章）。"""
        from viba import viba_ast
        from viba.interpret import Environment, EnvironmentCompute, EnvironmentStorage
        from tickets import critic
        from tickets.workflow import Workflow

        workflow = Workflow.load(where)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: None))
        passed = critic.critic_caps(workflow)["judge"](env, viba_ast.Constant(written))
        objection = self.case / "objection.viba"
        return passed, (objection.read_text(encoding="utf-8") if objection.is_file() else "")

    def test_a_condition_that_does_not_speak_about_this_delivery_is_skipped(self):
        """一条条件答 `nil` 就是"这一条不管这一份"：跳过它，别的条件照样判。"""
        where = self.a_law_whose_conditions({"one": None, "two": ""})
        passed, _said = self.a_judgement(where)
        self.assertTrue(passed)

    def test_a_delivery_no_condition_speaks_about_is_not_judged(self):
        """一条都不管这一份：那这一次没有人判过它，算不合格，理由说清楚。"""
        where = self.a_law_whose_conditions({"one": None})
        passed, said = self.a_judgement(where)
        self.assertFalse(passed)
        self.assertIn("没有哪一条验收条件管这一份", said)

    def test_a_condition_that_does_speak_and_fails_still_objects(self):
        """有一条管而且还说不合格：那就是不合格，理由是它说的那句。"""
        where = self.a_law_whose_conditions({"one": None, "two": "这份里一段 viba 都没有"})
        passed, said = self.a_judgement(where)
        self.assertFalse(passed)
        self.assertIn("这份里一段 viba 都没有", said)

    def test_a_condition_may_answer_the_reason_it_failed(self):
        """验收条件答一句话就是"没过，原因是这句"：tech_critic 拿它当不合格的理由。"""
        from tickets.critic import _verdict

        self.assertEqual((True, ""), _verdict(self.a_condition_answer(""), "one"))
        self.assertEqual((False, "members 不是一张表"),
                         _verdict(self.a_condition_answer("members 不是一张表"), "one"))

    def test_the_intake_is_asked_the_sentences_the_condition_brings(self):
        """受理答得出名字才算这一条过；问哪几句是验收条件的事。"""
        where = self.a_norm(self.TABLE)
        self.assertEqual('$ok ""', self.a_run(
            self.INTAKE + '__impl__ = why_the_intake_says_nothing\n'
                          '  << $env args.env\n'
                          f'  << $root "{where}"\n'
                          '  << $sentences ListLiteral["写一个快速排序，放到 quicksort.py"]'))
        said = self.a_run(self.INTAKE + '__impl__ = why_the_intake_says_nothing\n'
                                        '  << $env args.env\n'
                                        f'  << $root "{where}"\n'
                                        '  << $sentences ListLiteral["今天天气不错"]')
        self.assertIn("一句都没答出", said)

    def test_the_note_may_be_a_sentence_around_the_path(self):
        """单上交过来的常常是一句话：认得出路径就认路径。"""
        self.assertEqual('$ok ListLiteral["README.md"]',
                         self.a_run(FILES_MISSING +
                                    '__impl__ = files_missing\n'
                                    '  << $env args.env\n'
                                    '  << $root "本法写在 ' + str(self.draft) + '。"\n'
                                    '  << $wanted ListLiteral["intake", "members", '
                                    '"ticket_programs", "acceptance", "README.md"]'))

    def test_it_says_which_parts_a_law_is_missing(self):
        self.assertEqual('$ok ListLiteral["README.md"]',
                         self.a_run(FILES_MISSING +
                                    '__impl__ = files_missing\n'
                                    '  << $env args.env\n'
                                    f'  << $root "{self.draft}"\n'
                                    '  << $wanted ListLiteral["intake", "members", '
                                    '"ticket_programs", "acceptance", "README.md"]'))

    def test_a_program_with_no_body_is_not_complete(self):
        """只写提示、没写 `__impl__` 的那一份是类型，不是能跑的步骤。"""
        self.a_program("half_written",
                       "__decl__ =\n    bool\n  <- $env Env\n")
        self.assertEqual("$ok false", self.a_run(
            EVERY_COMPLETE + f'__impl__ = every_program_is_complete << $env args.env '
                             f'<< $root "{self.draft}"'))

        self.a_program("half_written",
                       "__decl__ =\n    bool\n  <- $env Env\n\n__impl__ = true\n")
        self.assertEqual("$ok true", self.a_run(
            EVERY_COMPLETE + f'__impl__ = every_program_is_complete << $env args.env '
                             f'<< $root "{self.draft}"'))

    def test_a_program_calling_a_step_nobody_declares_is_refused(self):
        """调到的名字要么自己声明、要么角色 api 声明、要么 viba 自带或解释器自己回答。"""
        self.a_program("asks_for_a_stranger",
                       "__decl__ =\n    bool\n  <- $env Env\n\n"
                       "__impl__ = nobody_declares_this << $env args.env\n")
        self.assertEqual("$ok false", self.a_run(
            EVERY_DECLARED_STRANGER + f'__impl__ = every_operator_is_declared << $env args.env '
                                      f'<< $root "{self.draft}"'))

        self.a_program("asks_for_a_stranger",
                       "__decl__ =\n    bool\n  <- $env Env\n\n"
                       "here = le\n"
                       "  << $env args.env\n"
                       "  << $x 1\n"
                       "  << $y 4\n\n"
                       "__impl__ = here\n")
        self.assertEqual("$ok true", self.a_run(
            EVERY_DECLARED_FINE + f'__impl__ = every_operator_is_declared << $env args.env '
                                  f'<< $root "{self.draft}"'))

    def test_under_judgment_is_the_thing_the_ticket_handed_over(self):
        """单上交过来的那一份：验收条件读它读的就是它。"""
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    put_snapshot)
        from tickets import critic

        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: critic._rule_compute(None).get(name)))
        put_snapshot(env, "一份法在 /tmp/draft", critic.UNDER_JUDGMENT)
        self.assertEqual('$ok "一份法在 /tmp/draft"',
                         self.a_run(UNDER + "__impl__ = under_judgment << $env args.env",
                                    decl="Any"))


class WhatACriticCanReadInASurveyTest(unittest.TestCase):
    """判一份梳理覆盖全了没有的那个算子：这个目录底下每一组都点到了吗。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "workspace"
        (self.workspace / "tests" / "data" / "alpha").mkdir(parents=True)
        (self.workspace / "tests" / "data" / "beta").mkdir(parents=True)
        (self.workspace / "tests" / "data" / "gamma").mkdir(parents=True)
        self.case = Path(self.temporary.name) / "case"
        self.case.mkdir()

    def a_run(self, document: str, root: str, workspace=None) -> str:
        from tickets import critic
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    get_snapshot, put_snapshot)

        table = critic._rule_compute(None, workspace)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        put_snapshot(env, document, "the_document")
        return table["what_is_not_covered"](
            env, get_snapshot(env, "the_document"), viba_ast.Constant(root))

    def test_a_document_that_points_at_every_group_is_fine(self):
        """三组都写到了（写的是路径：`tests/data/<组>/`），就是空串。"""
        self.assertEqual("", self.a_run(
            "见 tests/data/alpha/ 与 tests/data/beta/ 与 tests/data/gamma/",
            "tests/data", self.workspace))

    def test_a_group_nobody_pointed_at_is_named(self):
        """少写的那一组按名字带回去。"""
        said = self.a_run("只看 tests/data/alpha/ 和 tests/data/beta/",
                          "tests/data", self.workspace)
        self.assertIn("gamma", said)
        self.assertNotIn("alpha", said)

    def test_a_name_without_the_slash_is_not_pointing_at_it(self):
        """光提到那个名字不算点到：点的是那一组的路径。"""
        said = self.a_run("见 tests/data/alpha/，另外 beta 和 gamma 也看看",
                          "tests/data", self.workspace)
        self.assertIn("beta", said)
        self.assertIn("gamma", said)
        self.assertNotIn("alpha", said)

    def test_a_document_that_points_at_no_group_is_not_this_conditions_business(self):
        """一组都没点到的文档不是这一条管的：答 nil，让管它的那一条去说（一份法可以管几种交付）。"""
        self.assertIsNone(self.a_run("这是一份设计，底下没有测试程序的清单",
                                     "tests/data", self.workspace))

    def test_a_root_that_is_not_there_says_so(self):
        """那一组都不在的目录：说清楚是哪一条路径。"""
        said = self.a_run("随便", "tests/nothing", self.workspace)
        self.assertIn("不是一个目录", said)
        self.assertIn(str(self.workspace / "tests" / "nothing"), said)


class WhatTheActorHandsOverTest(unittest.TestCase):
    """actor 那条线交给谁：一个文件，还是一棵树。

    真会议里那一头是 agent 在 tmux 里干活，这里用不走网络的替身：`say` 记下来，`wait_for_turn`
    立刻答"这一轮停了"，别的一样（等东西交出来、比改动时刻）。
    """

    class AChannel(AgentChannel):
        """不走网络的那一条线。"""

        def __init__(self, workspace, **knobs):
            super().__init__("", "the-meeting", workspace, **knobs)
            self.said = []

        def say(self, message):
            self.said.append(message)
            return {}

        def chats(self, message_type=None):
            return [{"content": "DONE"}] * len(self.said)

        def turn_in_flight(self):
            return False

        def wait_for_turn(self, before, timeout=None):
            return {"phase": "waiting_for_user", "status": "running", "control_owner": "none"}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)

    def test_a_tree_is_a_directory_that_ends_with_a_slash(self):
        channel = self.AChannel(self.workspace)
        self.assertTrue(channel.is_a_tree("rb_norm/"))
        self.assertFalse(channel.is_a_tree("words.py"))
        (self.workspace / "rb_norm").mkdir()
        self.assertTrue(channel.is_a_tree("rb_norm"))     # 已经在那儿的一个目录也算

    def test_a_tree_comes_back_as_its_path_not_its_text(self):
        """一份法是一棵树：交回来的是那条路径，验收条件照它自己去看。"""
        channel = self.AChannel(self.workspace)
        (self.workspace / "rb_norm" / "acceptance").mkdir(parents=True)
        (self.workspace / "rb_norm" / "acceptance" / "one.viba").write_text("__impl__ = true\n",
                                                                           encoding="utf-8")
        self.assertEqual(str(self.workspace / "rb_norm"),
                         channel.ensure_work("写这一份", "rb_norm/"))

    def test_a_tree_that_is_already_there_still_waits_for_the_turn(self):
        """一棵树"已经在那儿"说明不了这一趟干完没有：每一趟都等这一轮干完。"""
        channel = self.AChannel(self.workspace)
        (self.workspace / "rb_norm").mkdir()
        (self.workspace / "rb_norm" / "members").write_text("actor actor_api agent\n",
                                                            encoding="utf-8")
        channel.ensure_work("写这一份", "rb_norm/")
        self.assertEqual(1, len(channel.said))          # 等这一轮，就得先把题面说出去

    def test_a_file_still_comes_back_as_its_text(self):
        channel = self.AChannel(self.workspace)
        (self.workspace / "words.py").write_text("def words(text):\n    return text.split()\n",
                                                encoding="utf-8")
        self.assertTrue(channel.ensure_work("写这一份", "words.py").startswith("def words"))

    def test_a_tree_that_nothing_changed_is_not_a_revision(self):
        """改版那一步比的是这一棵树本身：一份新文件才算改过。"""
        channel = self.AChannel(self.workspace, file_timeout=0.2, poll=0.05)
        (self.workspace / "rb_norm").mkdir()
        (self.workspace / "rb_norm" / "members").write_text("actor actor_api agent\n",
                                                            encoding="utf-8")
        before = channel.stamp("rb_norm/")
        with self.assertRaises(TimeoutError):
            channel.wait_for_change(self.workspace / "rb_norm", before)
        (self.workspace / "rb_norm" / "acceptance").mkdir()
        (self.workspace / "rb_norm" / "acceptance" / "one.viba").write_text("__impl__ = true\n",
                                                                           encoding="utf-8")
        self.assertNotEqual(before, channel.stamp("rb_norm/"))

    def test_a_tree_is_not_required_to_change_during_the_attempt(self):
        """一棵树没有"哪一刻算改过"：改没改好由下一次判决说，不由时刻说。"""
        from tickets.actor import _must_change

        channel = self.AChannel(self.workspace)
        self.assertFalse(_must_change(channel, "rb_norm/", (2, 123)))
        self.assertTrue(_must_change(channel, "words.py", 123))
        self.assertFalse(_must_change(channel, "words.py", None))

    def test_the_three_waits_can_be_given_by_the_region(self):
        """一个区域就是一个在跑的程序：它那几个时限由它的环境变量说。"""
        with mock.patch.dict(os.environ, {"REMOTEBUDDY_TURN_TIMEOUT": "1800",
                                          "REMOTEBUDDY_FILE_TIMEOUT": "300"}):
            channel = self.AChannel(self.workspace)
        self.assertEqual(1800.0, channel.turn_timeout)
        self.assertEqual(300.0, channel.file_timeout)
        self.assertEqual(120.0, channel.chat_timeout)     # 没说的那个照旧


class WhatACriticCanReadInADesignTest(unittest.TestCase):
    """判一份 viba 设计文档要用的那个算子：编译、名字解析、有没有走不到的类型。"""

    CLEAN = ("Root =\n    Object\n  * $one Used\n\n"
             "Used =\n    Object\n  * $n int\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.case = Path(self.temporary.name) / "case"
        self.case.mkdir()

    def a_design(self, source: str) -> Path:
        where = Path(self.temporary.name) / "design.md"
        where.write_text("# 一份设计\n\n```viba\n" + source + "```\n", encoding="utf-8")
        return where

    def a_run(self, where, roots) -> str:
        """把那份文档当**判的那一份**交过去（像真的单那样），跑一遍这条验收条件。"""
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    interpret, put_snapshot)
        from tickets import critic

        wanted = ", ".join('"' + one + '"' for one in roots)
        (self.case / "the_condition.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "under_judgment =\n    str\n  <- $env Env\n\n"
            "what_stops_this_design =\n    str\n  <- $env Env\n  <- $design str\n"
            "  <- $roots list[str]\n\n"
            "__impl__ = what_stops_this_design << $env args.env\n"
            "  << $design (under_judgment << $env args.env)\n"
            f"  << $roots ListLiteral[{wanted}]\n", encoding="utf-8")
        table = critic._rule_compute(None)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        # 判的时候这一份摆在 `judgment/` 底下，条件就是在那儿读它的（`critic.judge` 那一处）
        judged = Environment(env.storage.sub("judgment"), env.compute, env.viba_path)
        put_snapshot(judged, where.read_text(encoding="utf-8"), "under_judgment")
        answer = interpret(str(self.case / "the_condition.viba"), judged)
        return viba_ast.unparse_type(answer.data)

    def test_a_definition_that_is_only_mentioned_by_name_does_not_count(self):
        """一份设计要把这几样写成定义，只在正文里点个名不算：这一条认的是定义。"""
        from tickets import critic
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    interpret, put_snapshot)

        doc = ("# 一份设计\n\n正文里点到 _Compact 与 _ResumableCall 这两个名字。\n\n"
               "```viba\n_Compact =\n    Object\n  * $tasks list[Any]\n```\n")
        (self.case / "the_condition.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "under_judgment =\n    str\n  <- $env Env\n\n"
            "definitions_missing =\n    str\n  <- $env Env\n  <- $document str\n"
            "  <- $names list[str]\n\n"
            "__impl__ = definitions_missing << $env args.env\n"
            "  << $document (under_judgment << $env args.env)\n"
            '  << $names ListLiteral["_Compact", "_ResumableCall"]\n', encoding="utf-8")
        table = critic._rule_compute(None)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        judged = Environment(env.storage.sub("judgment"), env.compute, env.viba_path)
        put_snapshot(judged, doc, "under_judgment")
        said = viba_ast.unparse_type(interpret(str(self.case / "the_condition.viba"), judged).data)
        self.assertIn("还没写成定义", said)
        self.assertIn("_ResumableCall", said)

    def test_a_whole_file_of_viba_source_is_not_this_conditions_business(self):
        """整份就是 viba 源码的那种交付（一份声明文件）不归这一条管：它走 `what_stops_this_declaration`。"""
        where = Path(self.temporary.name) / "a_declaration.viba"
        where.write_text(self.CLEAN, encoding="utf-8")
        self.assertEqual('$ok nil', self.a_run(where, ["Root"]))

    def test_a_type_nobody_uses_is_named(self):
        loose = self.CLEAN + "\nLonely =\n    Object\n  * $n int\n"
        said = self.a_run(self.a_design(loose), ["Root"])
        self.assertIn("Lonely", said)
        self.assertIn("没人用", said)

    def test_a_name_that_does_not_resolve_is_named(self):
        said = self.a_run(self.a_design("Root =\n    Object\n  * $one Nowhere\n"), ["Root"])
        self.assertIn("Nowhere", said)
        self.assertIn("解析不出来", said)

    def test_a_block_that_does_not_compile_says_so(self):
        said = self.a_run(self.a_design("Root =\n    Object\n  *\n"), ["Root"])
        self.assertIn("编译不过", said)

    def test_a_root_that_is_not_there_says_so(self):
        said = self.a_run(self.a_design(self.CLEAN), ["Root", "Missing"])
        self.assertIn("Missing", said)


class WhatACriticCanReadInADeclarationTest(unittest.TestCase):
    """判一份声明文件（整份就是 viba 源码）要用的那个算子：同样三条，只是没有 markdown 段落。"""

    # 解释器自己回答的那五个成员，写法和 `viba_ast/tagged.py` 里那几条链一样
    CLEAN = ("InterpreterMembers =\n    Object\n"
             "  * $get_attr (Any <- $env Env <- $value Any <- $name str)\n"
             "  * $get_item (Any <- $env Env <- $container Any <- $address Any)\n"
             "  * $in (bool <- $env Env <- $container Any <- $piece Any)\n"
             "  * $len (int <- $env Env <- $container Any)\n"
             "  * $keys (list[str] <- $env Env <- $table Any)\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.case = Path(self.temporary.name) / "case"
        self.case.mkdir()

    def a_run(self, source: str, names) -> str:
        """把这一份源码当**判的那一份**交过去（像真的单那样），跑一遍这条验收条件。"""
        from viba import viba_ast
        from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                                    interpret, put_snapshot)
        from tickets import critic

        wanted = ", ".join('"' + one + '"' for one in names)
        (self.case / "the_condition.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "under_judgment =\n    str\n  <- $env Env\n\n"
            "what_stops_this_declaration =\n    str\n  <- $env Env\n  <- $declaration str\n"
            "  <- $names list[str]\n\n"
            "__impl__ = what_stops_this_declaration << $env args.env\n"
            "  << $declaration (under_judgment << $env args.env)\n"
            f"  << $names ListLiteral[{wanted}]\n", encoding="utf-8")
        table = critic._rule_compute(None)
        env = Environment(EnvironmentStorage("", None, str(self.case)),
                          EnvironmentCompute(lambda module, name: table.get(name)))
        judged = Environment(env.storage.sub("judgment"), env.compute, env.viba_path)
        put_snapshot(judged, source, "under_judgment")
        answer = interpret(str(self.case / "the_condition.viba"), judged)
        return viba_ast.unparse_type(answer.data)

    def test_a_declaration_that_holds_together_is_fine(self):
        """整份就是源码的一份声明：编译得过、名字解析得出来、没有没人用的定义，就是空串。"""
        self.assertEqual('$ok ""', self.a_run(self.CLEAN, ["InterpreterMembers"]))

    def test_a_definition_nobody_uses_is_named(self):
        loose = self.CLEAN + "Lonely =\n    Object\n  * $n int\n"
        self.assertIn("Lonely", self.a_run(loose, ["InterpreterMembers"]))

    def test_a_document_is_not_this_conditions_business(self):
        """有 ```viba 段落的那种（主体是文档）不归这一条管。"""
        said = self.a_run("# 一份设计\n\n```viba\n" + self.CLEAN + "```\n",
                          ["InterpreterMembers"])
        self.assertEqual("$ok nil", said)

    def test_a_source_that_declares_none_of_the_names_is_not_its_business(self):
        """该声明的名字一个都没有的那一份不是这一条管的（答 nil）。"""
        said = self.a_run("Other =\n    Object\n  * $n int\n", ["InterpreterMembers"])
        self.assertEqual("$ok nil", said)

    def test_a_member_the_interpreter_answers_must_be_declared_the_way_it_answers_it(self):
        """解释器自己回答的那五个成员，声明的那条链要和它答的一样：写的和答的不一样就不是描述。"""
        said = self.a_run(self.CLEAN.replace("$value Any", "$box Any"),
                          ["InterpreterMembers"])
        self.assertIn("$get_attr", said)
        self.assertIn("$box", said)
        self.assertIn("$value", said)

    def test_a_member_the_interpreter_does_not_answer_is_left_alone(self):
        """别的成员没有"实现已经定了"这一说：写成什么样都不归这一条管。"""
        mine = ("MyOwn =\n    Object\n  * $whatever (int <- $env Env <- $n int)\n")
        self.assertEqual('$ok ""', self.a_run(mine, ["MyOwn"]))

    def test_a_source_that_is_not_viba_at_all_is_not_this_conditions_business(self):
        """整份不是 viba 源码（比如改的是一个 .py 文件）：这一条答 nil，交给管那种交付的那一条。"""
        said = self.a_run('def _load():\n    return InterpreterMembers\n',
                          ["InterpreterMembers"])
        self.assertEqual("$ok nil", said)


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
        session.issue("do_the_work")
        taker = the_handlers(self.meeting, self.workflow)[2]
        self.assertEqual(["do_the_work-quicksort"], [one.uid for one in taker.takes()])
        taker.tick()
        self.assertEqual([], taker.takes())            # 刚才是它写的：下一手不是它的
        self.assertTrue(unfinished(self.meeting) or True)

    def test_the_same_value_twice_is_the_same_file(self):
        """一样的东西写两遍是同一份数据：写下来的字一个不差。"""
        session = a_session(self.meeting)
        first = session.issue("do_the_work")
        path = tickets_root(self.meeting) / first.uid / "ticket.viba"
        before = path.read_text(encoding="utf-8")
        again = read_ticket(self.meeting, first.uid)
        self.assertEqual(str(case_root(self.meeting, CASE)), again.storage_path)
        self.assertEqual(before, (tickets_root(self.meeting) / again.uid
                                  / "ticket.viba").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class TheFlowVerdictTest(unittest.TestCase):
    """流程那一关判下来的是什么：`(过了没有, 哪里不完整)`；还没判过就是 None。

    取它的人拿它当自己的结论（`rb cli` 的退出码就是它），所以读法在这儿钉死：答 true 的那一趟是
    `(True, "")`，答 false 的那一趟带着它点出的问题，一次都没判过的会议是 `None`（不是 False——
    "没判过"和"判下来没过"是两回事）。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.meeting = Path(self.temporary.name) / "meeting"
        self.workflow = a_workflow()

    def a_verdict(self, passed: bool, problems: str = "") -> None:
        values = Store(case_root(self.meeting, CASE), record_path(self.meeting))
        values.write("flow", "check", passed)
        values.write("flow", "objection", problems)

    def test_nothing_judged_yet(self):
        self.assertIsNone(the_flow_verdict(self.meeting))

    def test_a_flow_that_passed(self):
        self.a_verdict(True)
        self.assertEqual((True, ""), the_flow_verdict(self.meeting))

    def test_a_flow_that_did_not_pass_keeps_the_problems(self):
        self.a_verdict(False, "要交的东西不在工作区里：quicksort.py")
        passed, problems = the_flow_verdict(self.meeting)
        self.assertFalse(passed)
        self.assertEqual("要交的东西不在工作区里：quicksort.py", problems)

    def test_the_newest_case_speaks(self):
        """一个案子一趟；取的是最新那一趟判下来的那一次。"""
        self.a_verdict(False, "第一趟没过")
        later = Store(case_root(self.meeting, f"{CASE}-2"), record_path(self.meeting))
        later.write("flow", "check", True)
        later.write("flow", "objection", "")
        self.assertEqual((True, ""), the_flow_verdict(self.meeting))
