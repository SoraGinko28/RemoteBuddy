"""设计文档里那几条，落在代码上是不是真的。

`ticket_centered_design/ticket_centered.md` 说了几件事，这里一条条验：五类文件、工单里的路径都相对
案子、一张工单至少 import 它要调的那些 api、每一步只写调用、一张工单发给会上的每个成员、各人拿一份
自己的副本跑同一个程序（自己会的算、有答案的回放、其余递延）、答下的材料抄回会议、对账靠成员包里
那份副本、"该发哪一张"是读 store 算出来的。

写代码那一步是脚本化的（`write(prompt) -> code`）——别的都是真的：真解释器、真 store、真规则程序、
真进程（三个角色各一个）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ticket_centered import (WORKFLOW_DIR, Meeting, Member, actor_caps,  # noqa: E402
                             assistant_caps, critic_caps, load_workflow)
from ticket_centered import workflow as reading  # noqa: E402

TASK = ("写一个快速排序，放到 quicksort.py。接口必须是 "
        "quick_sort(items, key=None, reverse=False, lo=0, hi=None)，就地排序。")
FLOW = ["write_a_draft", "judge_the_draft", "revise_the_draft",
        "judge_the_revision", "close_the_meeting"]

DRAFT = """\
def quick_sort(items, key=None, reverse=False, lo=0, hi=None):
    if hi is None:
        hi = len(items) - 1
    return items
"""

REVISION = """\
def quick_sort(items, key=None, reverse=False, bounds=(0, None)):
    lo, hi = bounds
    if hi is None:
        hi = len(items) - 1
    return items
"""


class ScriptedWriter:
    """The actor's写那一侧，脚本化的：题面要五个参数就写五个，判语说超了就收成一个。"""

    def __init__(self):
        self.prompts = []

    def write(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return REVISION if "不合格" in prompt else DRAFT

    def versions(self) -> int:
        return len(self.prompts)


def read(meeting: Meeting, case: str, path: str, name: str):
    return meeting.store.read(meeting.absolute(case, path), name)


def build(root: Path):
    workflow = load_workflow(WORKFLOW_DIR)
    writer = ScriptedWriter()
    meeting = Meeting(root, workflow)
    meeting.join(Member("actor", "actor", actor_caps(writer.write)))
    meeting.join(Member("assistant", "assistant", assistant_caps()))
    meeting.join(Member("critic", "critic", critic_caps(workflow)))
    return workflow, writer, meeting


# ----------------------------------------------------------------------
# 五类文件
# ----------------------------------------------------------------------


def test_the_five_classes_of_files_are_the_workflow():
    workflow = load_workflow(WORKFLOW_DIR)
    assert sorted(workflow.tickets()) == sorted(FLOW)
    assert list(workflow.rules()) == ["parameter_count_le_4"]
    assert set(workflow.files) == {"actor_api", "assistant_api", "critic_api",
                                   *FLOW, "parameter_count_le_4"}


def test_a_ticket_imports_the_apis_it_calls_and_only_writes_calls():
    workflow = load_workflow(WORKFLOW_DIR)
    for name, source in workflow.tickets().items():
        called = {step.api for step in workflow.steps_of(name)}
        imported = set(workflow.apis_used_in(name))
        assert called and called <= imported, f"{name} 调了没 import 的 api"
        for definition in reading.definitions_of(source):
            assert definition.name == reading.RET or reading.is_a_call(definition.body), \
                f"{name} 的 {definition.name} 不是一次调用"
        assert any(definition.name == reading.RET
                   for definition in reading.definitions_of(source)), f"{name} 没有 __ret__"


def test_every_operator_a_ticket_calls_is_declared_by_a_role():
    workflow = load_workflow(WORKFLOW_DIR)
    for name in workflow.tickets():
        for step in workflow.steps_of(name):
            assert step.op in workflow.ops_of(step.api), \
                f"{name} 调了 {step.api} 里没有的 {step.op}"
            assert workflow.role_of_op(step.op)


def test_no_design_file_names_a_case():
    workflow = load_workflow(WORKFLOW_DIR)
    for name, source in workflow.files.items():
        assert "case/" not in source, f"{name} 点了案子的名字"
        for step in reading.steps_of(source):
            assert not step.env.startswith("case/"), f"{name} 的 {step.env} 点了案子"


# ----------------------------------------------------------------------
# 一张工单发给每个成员，各人一份自己的副本
# ----------------------------------------------------------------------


def test_a_bundle_is_the_program_plus_the_data_the_ticket_reads(tmp_path):
    _workflow, writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)

    bundles = meeting.publish("write_a_draft", case)
    assert set(bundles) == set(meeting.members)          # 发给会上的每个成员
    bundle = bundles["actor"]
    program = {path.name for path in (bundle / "program").iterdir()}
    assert {"actor_api.viba", "critic_api.viba", "parameter_count_le_4.viba",
            "write_a_draft.viba"} <= program
    assert (bundle / "store" / case / "prepare" / "stated_task.viba").is_file()
    assert writer.versions() == 0                        # 发包不等于跑过


def test_the_whole_flow_runs_and_only_the_owner_answers(tmp_path):
    workflow, writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)

    issued = []
    for _round in range(8):
        ticket = meeting.next_ticket(case)
        if ticket is None:
            break
        issued.append(ticket)
        bundles = meeting.publish(ticket, case)
        for member in meeting.members.values():          # 各人在自己那份副本里跑一遍
            bundle = bundles[member.name]
            from ticket_centered.member import run_bundle
            run_bundle(bundle, meeting=meeting.root, case=case, caps=member.caps, out=lambda _: None)
    assert issued == FLOW
    assert writer.versions() == 2                        # 真写了两版
    assert read(meeting, case, "judged", "judge")[0] is False
    assert read(meeting, case, "judged_revision", "judge")[0] is True
    assert "5 个参数" in read(meeting, case, "judged", "objection")[0]
    assert "合格" in read(meeting, case, "closing", "closing_note")[0]
    assert meeting.reconcile() == []


def test_what_the_critic_judged_is_the_bytes_the_actor_handed_over(tmp_path):
    _workflow, _writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    bundles = meeting.publish("judge_the_draft", case)
    from ticket_centered.member import run_bundle
    run_bundle(bundles["critic"], meeting=meeting.root, case=case,
               caps=meeting.members["critic"].caps, out=lambda _: None)
    # 要读的还没答：critic 只能递延，不能凭空判
    assert not meeting.store.answered(meeting.absolute(case, "judged"), "judge")


def test_a_finished_step_is_replayed_not_done_again(tmp_path):
    _workflow, writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)
    from ticket_centered.member import run_bundle

    for _round in range(8):
        ticket = meeting.next_ticket(case)
        if ticket is None:
            break
        for member in meeting.members.values():
            run_bundle(meeting.publish(ticket, case)[member.name], meeting=meeting.root,
                       case=case, caps=member.caps, out=lambda _: None)
    assert writer.versions() == 2
    # 再发一次已经做完的工单：全是回放，写代码那一步不会再叫一次
    bundles = meeting.publish("write_a_draft", case)
    for member in meeting.members.values():
        run_bundle(bundles[member.name], meeting=meeting.root, case=case,
                   caps=member.caps, out=lambda _: None)
    assert writer.versions() == 2


# ----------------------------------------------------------------------
# 三个角色各一个进程
# ----------------------------------------------------------------------


MEMBER_SCRIPT = '''\
"""一个脚本化的 actor 进程：用 member.run_bundle 跑发给它的那一份。"""
import sys
from pathlib import Path

sys.path.insert(0, {repo!r})
from ticket_centered import WORKFLOW_DIR, actor_caps, load_workflow
from ticket_centered.member import run_bundle

bundle, case, meeting = sys.argv[1], sys.argv[2], sys.argv[3]
workflow = load_workflow(WORKFLOW_DIR)
run_bundle(bundle, meeting=meeting, case=case, caps=actor_caps(
    lambda prompt: "def quick_sort(items, key=None, reverse=False, bounds=(0, None)):\\n"
                   "    return items\\n" if "\\u4e0d\\u5408\\u683c" in prompt else
                   "def quick_sort(items, key=None, reverse=False, lo=0, hi=None):\\n"
                   "    return items\\n"))
'''


def test_a_member_is_a_process_that_runs_the_bundle_handed_to_it(tmp_path):
    workflow, _writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)
    bundles = meeting.publish("write_a_draft", case)

    script = tmp_path / "actor_member.py"
    script.write_text(MEMBER_SCRIPT.format(repo=str(REPO_ROOT)), encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    finished = subprocess.run([sys.executable, str(script), str(bundles["actor"]), case,
                               str(meeting.root)],
                              cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
                              timeout=180)
    assert finished.returncode == 0, finished.stderr
    # 答下的材料抄回会议：三个数据路径下都有东西了
    for path, name in (("greeted", "acknowledge"), ("drafted", "write_code"),
                       ("report_done", "report_done")):
        assert meeting.store.answered(meeting.absolute(case, path), name), f"{path} + {name}"


def test_the_assistant_process_is_where_the_humans_words_enter(tmp_path):
    _workflow, _writer, meeting = build(tmp_path)
    case = meeting.case("quicksort")
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    finished = subprocess.run([sys.executable, "-m", "ticket_centered.assistant",
                               "--meeting", str(meeting.root), "--case", case,
                               "--words", TASK],
                              cwd=REPO_ROOT, env=environment, capture_output=True, text=True,
                              timeout=180)
    assert finished.returncode == 0, finished.stderr
    words, answered = read(meeting, case, "", "stated_task")
    assert answered and "快速排序" in str(words)
