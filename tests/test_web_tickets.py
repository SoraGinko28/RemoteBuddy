"""页面那块"工单"：服务读这场会的工单那一侧，算出该发哪一张、哪几步答了没有。

`_ticket_board` 只认会议那三样（会议号、会议目录、journal），所以这里用一个桩当会议，
工单那一侧是真的（`ticket_centered.Meeting` 建的 store 和包）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ticket_centered import Meeting, Member, load_workflow, norm_home  # noqa: E402
import pytest  # noqa: E402

import web  # noqa: E402

TASK = "写一个快速排序，放到 quicksort.py"


@pytest.fixture(autouse=True)
def _the_norm_is_the_one_in_the_tree(tmp_path, monkeypatch):
    """这一趟读的规范就是 `rbNorm` 那一份（本机默认那条路径），不受环境影响。"""
    import ticket_centered.workflow as wf
    monkeypatch.delenv(wf.NORM_ENV, raising=False)
    monkeypatch.delenv(wf.WORKFLOW_ENV, raising=False)


class FakeStore:
    """A meeting's three things the board asks for."""

    def __init__(self, run_dir: Path, meeting_id: str):
        self.run_dir = run_dir
        self.meeting = type("Meeting", (), {"meeting_id": meeting_id})()
        self._events = [{"kind": "chat", "actor": "human", "content": TASK}]

    def events(self):
        return list(self._events)


def test_the_board_says_what_is_due_and_who_answers_it(tmp_path):
    run_dir = tmp_path / "runs" / "ticket-test"
    root = run_dir / web.TICKET_ROOT
    workflow = load_workflow(norm_home())
    meeting = Meeting(root, workflow)
    for name in web.TICKET_MEMBERS:
        meeting.join(Member(name, name, {}))
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)

    board = web._ticket_board(FakeStore(run_dir, "ticket-test"))
    assert board["digest"] == workflow.digest()
    assert board["case"] == case
    assert board["target"] == "quicksort.py"
    assert [one["ticket"] for one in board["tickets"]] == sorted(workflow.tickets())

    write_a_draft = next(one for one in board["tickets"] if one["ticket"] == "write_a_draft")
    assert write_a_draft["due"] is True and write_a_draft["issued"] is False
    assert [step["op"] for step in write_a_draft["answers"]] == [
        "acknowledge", "write_code", "report_done"]
    assert all(step["role"] == "actor" for step in write_a_draft["answers"])
    assert all(step["answered"] is False for step in write_a_draft["answers"])

    judge = next(one for one in board["tickets"] if one["ticket"] == "judge_the_draft")
    assert judge["due"] is False and "还缺" in judge["why"]

    meeting.store.write(meeting.absolute(case, "drafted"), "write_code", "def f():\n    pass\n")
    board = web._ticket_board(FakeStore(run_dir, "ticket-test"))
    write_a_draft = next(one for one in board["tickets"] if one["ticket"] == "write_a_draft")
    assert next(step for step in write_a_draft["answers"] if step["op"] == "write_code")["answered"]
