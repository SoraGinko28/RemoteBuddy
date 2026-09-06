"""2b routing test: an @mention of a non-generator member summons only that
member's reviewer worker; the generator worker is not started."""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import meeting_v2 as m2
import web_v2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


class _Action:
    def __init__(self, type_, content="", message_type="none"):
        self.type = type_
        self.content = content
        self.message_type = message_type
        self.command = ""
        self.path = ""
        self.keys = []
        self.delay_ms = 0
        self.references = ()


class _FakeBackend:
    def __init__(self, _config):
        self.config = SimpleNamespace(request_timeout_seconds=5.0)
        self.closed = False

    def start_cycle(self, *args, **kwargs):
        return "session-fake"

    def next_action(self, *args, **kwargs):
        return SimpleNamespace(response_id="r", executed=False, turn_id=None,
                               action=_Action("done", "reviewer agrees.", "final"))

    def close(self):
        self.closed = True


def _fake_factory(_config):
    return _FakeBackend(_config)


class _DummyTransport:
    pass


def _make_service(tmp_path):
    return web_v2.MeetingWebService(
        project_root=tmp_path,
        model_api_path=REPO / "model_api",
        skill_path=SKILL,
        client_factory=_fake_factory,
        transport_factory=lambda _bin: _DummyTransport(),
    )


@pytest.fixture()
def service(tmp_path):
    return _make_service(tmp_path)


class TestReviewerRouting:
    def _idle_meeting(self, tmp_path):
        store = m2.MeetingStore.create(
            tmp_path, "meeting-routing", "do the thing", SKILL, "s",
            model="deepseek-v4-flash",
            agent_name2role={"agent": "generator", "critic": "passive_discriminator"},
        )
        store.update(status="running", phase="waiting_for_user", control_owner="none")
        return store

    def test_at_critic_summons_only_reviewer(self, service, tmp_path) -> None:
        store = self._idle_meeting(tmp_path)
        service.message(store.meeting.meeting_id,
                        {"message": "@critic what do you think?"})
        # Reviewer worker finishes its fake turn quickly: wait for the final chat.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
            finals = [e for e in current.events()
                      if e.get("kind") == "chat" and e.get("actor") == "critic"
                      and e.get("message_type") == "final"]
            if finals:
                break
            time.sleep(0.05)
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        finals = [e for e in reloaded.events()
                  if e.get("kind") == "chat" and e.get("actor") == "critic"
                  and e.get("message_type") == "final"]
        assert finals and finals[-1]["content"] == "reviewer agrees."
        # Generator worker was never started for this message.
        assert service.jobs.get(store.meeting.meeting_id) is None
        assert reloaded.meeting.phase == "waiting_for_user"

    def test_plain_message_does_not_summon_reviewer(self, service, tmp_path) -> None:
        store = self._idle_meeting(tmp_path)
        service.message(store.meeting.meeting_id, {"message": "go on"})
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert service.reviewer_jobs.get(store.meeting.meeting_id) in ({}, None)
        assert reloaded.meeting.control_owner == "agent"  # generator wake path
        assert reloaded.meeting.phase == "agent_thinking"
