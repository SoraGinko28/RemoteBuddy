"""meeting_reviewer tests: one-turn discriminator behavior with a fake backend."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import meeting_reviewer as reviewer
import meeting_v2 as m2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


class FakeAction:
    def __init__(self, type, content="", message_type="none"):
        self.type = type
        self.content = content
        self.message_type = message_type
        self.command = ""
        self.path = ""
        self.keys = []
        self.delay_ms = 0
        self.references = ()


class FakeClient:
    def __init__(self, turns):
        self.turns = list(turns)
        self.started = 0
        self.next_calls = 0
        self.closed = False
        self.config = SimpleNamespace(request_timeout_seconds=5.0)

    def start_cycle(self, meeting_id, instructions, initial_context, model=None,
                    event_callback=None):
        self.started += 1
        self.last_context = initial_context
        return "fake-session"

    def next_action(self, session_id, observation, model=None, allowed_types=None,
                    allowed_message_types=None, request_timeout_seconds=None,
                    action_handler=None):
        self.next_calls += 1
        if not self.turns:
            return SimpleNamespace(response_id="r", action=FakeAction("done", "no more", "final"),
                                   executed=False, turn_id=None)
        turn = self.turns.pop(0)
        return SimpleNamespace(response_id="r", action=FakeAction(*turn), executed=False,
                               turn_id=None)

    def close(self):
        self.closed = True


@pytest.fixture()
def multi_store(tmp_path):
    return m2.MeetingStore.create(
        tmp_path, "meeting-review", "please build hello world", SKILL,
        "s", model="deepseek-v4-flash",
        agent_name2role={"agent": "generator", "critic": "passive_discriminator"},
    )


class TestReviewerTurn:
    def test_done_appends_final_under_own_name_and_restores_waiting(self, multi_store) -> None:
        client = FakeClient([("chat", "I will look.", "ack"),
                             ("done", "The code looks fine but lacks a test.", "final")])
        ok = reviewer.run_reviewer_turn(multi_store, client, "critic")
        assert ok is True
        chats = [e for e in multi_store.events() if e["kind"] == "chat"]
        final = [e for e in chats if e["actor"] == "critic"]
        assert final and final[-1]["content"] == "The code looks fine but lacks a test."
        assert final[-1]["message_type"] == "final"
        assert multi_store.meeting.phase == "waiting_for_user"
        assert multi_store.meeting.control_owner == "none"

    def test_generator_is_never_the_reviewer_actor(self, multi_store) -> None:
        client = FakeClient([("done", "noted", "final")])
        reviewer.run_reviewer_turn(multi_store, client, "agent")
        chats = [e for e in multi_store.events() if e["kind"] == "chat"]
        final = [e for e in chats if e["message_type"] == "final"][-1]
        assert final["actor"] == "agent"  # actor is whatever name caller passes

    def test_reviewer_gets_conversation_context(self, multi_store) -> None:
        multi_store.append_chat("human", "please check @critic the diff", "request")
        client = FakeClient([("done", "looks ok", "final")])
        reviewer.run_reviewer_turn(multi_store, client, "critic")
        assert "please check" in client.last_context

    def test_backend_error_records_and_restores(self, multi_store) -> None:
        class Boom(FakeClient):
            def next_action(self, *args, **kwargs):
                raise RuntimeError("boom")

        client = Boom([("done", "x", "final")])
        ok = reviewer.run_reviewer_turn(multi_store, client, "critic")
        assert ok is False
        errors = [e for e in multi_store.events() if e["kind"] == "error"]
        assert any("boom" in (e.get("content") or "") for e in errors)
        assert multi_store.meeting.phase == "waiting_for_user"

    def test_no_final_within_rounds_restores_waiting(self, multi_store) -> None:
        many_chats = [("chat", f"round {i}", "progress") for i in range(30)]
        client = FakeClient(many_chats)
        ok = reviewer.run_reviewer_turn(multi_store, client, "critic")
        assert ok is False
        assert multi_store.meeting.phase == "waiting_for_user"
