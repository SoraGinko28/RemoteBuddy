"""这场会有哪几个服务：读流程旁边那张表（`<流程>/services`），不是写在 Python 里。

这里验的是"加一个服务不用改 Python"：写一份 `<角色>_api.viba` 放进流程目录、再往表里加一行，
那个服务就进得来。表里那一行可以指一个另起的进程（`process <命令…>`）——进程自己照 store 里的工单算，
宿主不必知道它会什么。

写代码那一步照旧是脚本化的（`write(prompt) -> code`），别的都是真的：真解释器、真 store、真进程。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ordered_meeting.members import (command_for, due_rows, read_table,  # noqa: E402
                                     row_of, services_for, write_table)
from ordered_meeting.plan import workflow_of  # noqa: E402
from ordered_meeting.service import run_command  # noqa: E402
from ordered_meeting.session import OK, Session  # noqa: E402
from ticket_centered import actor_caps  # noqa: E402
from ticket_centered import norm_home  # noqa: E402
from ticket_centered.store import Store  # noqa: E402
from ticket_centered.workflow import load_workflow  # noqa: E402

WORKFLOW = norm_home()
CHECKER = REPO_ROOT / "tests" / "process_checker.py"
CASE = "case/quicksort"
WORDS = "写一个快速排序，放到 quicksort.py"
CODE = "def quick_sort(items, key=None, reverse=False, lo=0, hi=None):\n    return items\n"

STATIC_API = """\
# 静态代码检查对外的接口：一个算子，拿一段代码，回一个结论。

check_static =
    ($module_path str * $func_name str)
  <- $env Env
  <- $code ($module_path str * $func_name str)
  <- { 拿 $code 那份代码做静态检查，结论记在这条数据路径下 }
"""

CHECK_TICKET = """\
__decl__ =
    ($module_path str * $func_name str)
  <- $env Env

args = __get_args__ << __decl__

# 检查这一版：结论落在 checked 这条数据路径下。
import static_code_analysis_api as checker

__impl__ = checker.check_static
  << $env ($sub_env << args.env << "checked")
  << $code ($module_path "drafted" * $func_name "write_code")
"""

TINY_SERVICE = '''\
"""一个只会说自己跑过的服务：证明宿主不必知道一个进程成员会什么。"""

import json
import os

if __name__ == "__main__":
    open("ran.txt", "w", encoding="utf-8").write(os.getcwd())
    print(json.dumps({"service": "tiny", "role": "tiny", "order": "", "ticket": "",
                      "answered": [], "replayed": [], "deferred": [], "failure": ""}))
'''


class MembersTableTest(unittest.TestCase):
    """那张表读出来是什么，写错的地方当场说清楚。"""

    def test_the_shipped_table_names_the_two_services(self):
        rows = due_rows(load_workflow(WORKFLOW))
        self.assertEqual([(row.name, row.api, row.role, row.kind) for row in rows],
                         [("actor", "actor_api", "actor", "agent"),
                          ("critic", "critic_api", "critic", "critic")])

    def test_a_row_says_a_name_an_api_and_how_to_find_it(self):
        row = row_of("checker static_code_analysis_api process python -m static_check")
        self.assertEqual((row.name, row.role, row.kind), ("checker", "static_code_analysis", "process"))
        self.assertEqual(row.arguments, ("python", "-m", "static_check"))

    def test_a_row_that_does_not_say_how_to_find_it_is_refused(self):
        with self.assertRaises(ValueError):
            row_of("checker static_code_analysis_api 看缘分")

    def test_a_row_naming_an_api_this_workflow_does_not_have_is_refused(self):
        rows = [row_of("checker static_code_analysis_api process python -m static_check")]
        with self.assertRaises(ValueError):
            services_for(rows, workflow=load_workflow(WORKFLOW), store_root="/tmp/store",
                         case=CASE, order="meeting")

    def test_a_comment_line_is_not_a_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services"
            write_table(path, ["actor actor_api agent", "# 这一行是说明"])
            self.assertEqual([row.name for row in read_table(path)], ["actor"])

        # 写下去的表再读回来是同一份（`--add` 走的就是这条路）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "services"
            write_table(path, ["actor actor_api agent"])
            self.assertEqual([row.name for row in read_table(path)], ["actor"])


class ProcessMemberTest(unittest.TestCase):
    """表里加一行 `process …`，就多一个服务：它是个真进程，宿主不知道它会什么。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()
        self.store_root = self.tmp / "store"

    def extended_workflow(self, extra_row: str = None) -> Path:
        """程序带的那份流程加一份新 api、一张新单，表里再加一行。"""
        root = self.tmp / "workflow"
        shutil.copytree(WORKFLOW, root)
        (root / "static_code_analysis_api.viba").write_text(STATIC_API, encoding="utf-8")
        (root / "tickets" / "check_the_draft.viba").write_text(CHECK_TICKET, encoding="utf-8")
        row = extra_row or (f"checker static_code_analysis_api process python {CHECKER} "
                            f"--workflow {root}")
        add = read_table(root / "services") + [row_of(row)]
        write_table(root / "services", add)
        return root

    def test_a_new_api_and_one_row_are_all_a_new_service_needs(self):
        workflow = self.extended_workflow()
        rows = due_rows(load_workflow(workflow))
        self.assertEqual([row.name for row in rows], ["actor", "critic", "checker"])

        session = Session(self.store_root, CASE, "meeting", workflow=workflow,
                          record_path=self.tmp / "record.md")
        session.state_the_task(WORDS)
        services = services_for(rows, workflow=workflow, store_root=self.store_root, case=CASE,
                                order="meeting", actor_caps=actor_caps(write=lambda _p: CODE),
                                workspace=self.workspace)
        outcome = session.run(services)

        self.assertEqual(outcome.outcome, OK)
        # 答那一步的是那个进程：账上就是它印回来的那一行
        answered = [(path, name) for one in outcome.reports for path, name in one.answered]
        self.assertIn((f"{CASE}/checked", "check_static"), answered)
        value, got = Store(self.store_root).read(f"{CASE}/checked", "check_static")
        self.assertTrue(got)
        self.assertEqual(value, 1)              # 那一版只有一个顶层 def
        self.assertIn("- checker（static_code_analysis）答了",
                      "\n".join(session.store.records()))

    def test_a_process_member_runs_in_the_workspace_it_was_given(self):
        self.extended_workflow()
        (self.workspace / "tiny_service.py").write_text(TINY_SERVICE, encoding="utf-8")
        workflow = load_workflow(self.tmp / "workflow")
        service = services_for([row_of("tiny static_code_analysis_api process python -m tiny_service")],
                               workflow=workflow, store_root=self.store_root, case=CASE,
                               order="meeting", workspace=self.workspace)[0]
        report = run_command(service, store_root=self.store_root, case=CASE, order="meeting")

        self.assertEqual(report.failure, "")
        self.assertEqual(report.service, "tiny")     # 账是它自己印的那行 JSON，不是这一侧的
        # 它在自己的工作区里跑：那份服务文件是它自己带的，这一侧 import 都不用 import 它
        self.assertEqual(Path((self.workspace / "ran.txt").read_text(encoding="utf-8")).resolve(),
                         self.workspace.resolve())

    def test_a_process_that_never_prints_a_line_is_a_failure_not_a_crash(self):
        self.extended_workflow()
        service = services_for(
            [row_of("quiet static_code_analysis_api process python -c pass")],
            workflow=load_workflow(self.tmp / "workflow"), store_root=self.store_root,
            case=CASE, order="meeting")[0]
        report = run_command(service, store_root=self.store_root, case=CASE, order="meeting")
        self.assertTrue(report.failure)

    def test_the_command_gets_the_store_case_and_order(self):
        row = row_of("checker static_code_analysis_api process python -m static_check")
        self.assertEqual(command_for(row, store_root="/tmp/store", case=CASE, order="meeting"),
                         [sys.executable, "-m", "static_check", "--store", "/tmp/store",
                          "--case", CASE, "--order", "meeting", "--phase", "answer"])


if __name__ == "__main__":
    unittest.main()
