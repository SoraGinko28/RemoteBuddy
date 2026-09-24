"""页面那块"工单"：服务读这场会的工单那一侧，算出该发哪一张、哪几步答了没有。

`_ticket_board` 只认会议那三样（会议号、会议目录、journal），所以这里用一个桩当会议，
工单那一侧是真的（`ticket_centered.Meeting` 建的 store 和包）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ticket_centered import WORKFLOW_DIR, Meeting, Member, load_workflow  # noqa: E402
import web_v2  # noqa: E402

TASK = "写一个快速排序，放到 quicksort.py"


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
    root = run_dir / web_v2.TICKET_ROOT
    workflow = load_workflow(WORKFLOW_DIR)
    meeting = Meeting(root, workflow)
    for name in web_v2.TICKET_MEMBERS:
        meeting.join(Member(name, name, {}))
    case = meeting.case("quicksort")
    meeting.store.write(meeting.absolute(case, ""), "stated_task", TASK)

    board = web_v2._ticket_board(FakeStore(run_dir, "ticket-test"))
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
    board = web_v2._ticket_board(FakeStore(run_dir, "ticket-test"))
    write_a_draft = next(one for one in board["tickets"] if one["ticket"] == "write_a_draft")
    assert next(step for step in write_a_draft["answers"] if step["op"] == "write_code")["answered"]


def test_a_ticket_meeting_refuses_the_old_harness(tmp_path):
    """工单在开的会：assistant 的 watch 不起、@mention 也唤不醒 critic——同一件事只有一个驱动者。"""
    import meeting_v2 as m2

    skill = REPO_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
    store = m2.MeetingStore.create(tmp_path, "ticket-driven", "做点事", skill,
                                   "session-ticket", model="test-model")
    service = web_v2.MeetingWebService(project_root=tmp_path)

    assert service._ticket_driven(store) is False
    (store.run_dir / web_v2.TICKET_ROOT).mkdir(parents=True, exist_ok=True)
    assert service._ticket_driven(store) is True
    service._ensure_assistant_worker(store)              # 工单在开：这只 watch 不起
    assert service.assistant_jobs.get(store.meeting.meeting_id) is None

    # @mention 那条路也关掉：工单那边有自己的 critic
    service._start_reviewer(store.meeting.meeting_id, "critic")
    assert service.reviewer_jobs.get(store.meeting.meeting_id) in (None, {})
    routed = [event for event in store.events()
              if (event.get("data") or {}).get("reviewer_routing", {}).get("reason") == "ticket_driven"]
    assert routed, "该留一条 skip 的记录"
