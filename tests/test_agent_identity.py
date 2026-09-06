"""Agent identity (slice 1): agent_name2role persistence and membership rules.

Goal: named agents + a single-source role map on the Meeting manifest,
keeping the legacy single agent ("agent"/generator) byte-for-byte compatible.
"""

import json
from pathlib import Path

import pytest

import meeting_v2 as m2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


@pytest.fixture()
def store(tmp_path):
    return m2.MeetingStore.create(
        tmp_path,
        "meeting-agent-1",
        "hello",
        SKILL,
        "session-agent-1",
        model="deepseek-v4-flash",
        agent_backend="responses",
        agent_provider="openai",
    )


class TestDefaults:
    def test_create_defaults_to_legacy_single_agent(self, store) -> None:
        assert store.meeting.agent_name2role == {m2.DEFAULT_AGENT_NAME: "generator"}

    def test_helpers_see_default_agent(self, store) -> None:
        assert store.meeting.agent_names() == {m2.DEFAULT_AGENT_NAME}
        assert store.meeting.role_of("agent") == "generator"
        assert store.meeting.is_agent_name("agent")
        assert not store.meeting.is_agent_name("human")
        assert store.meeting.is_agent_name("human") is False

    def test_manifest_persists_role_map(self, store) -> None:
        payload = json.loads((store.run_dir / "meeting.json").read_text(encoding="utf-8"))
        assert payload["agent_name2role"] == {"agent": "generator"}

    def test_reload_keeps_roles(self, store, tmp_path) -> None:
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert reloaded.meeting.agent_name2role == {"agent": "generator"}


class TestExplicitRoles:
    def test_multi_agent_roles_roundtrip(self, tmp_path) -> None:
        roles = {"coder": "generator", "critic": "active_discriminator",
                 "sparring": "passive_discriminator"}
        store = m2.MeetingStore.create(tmp_path, "meeting-multi", "hi", SKILL,
                                       "s", model="m", agent_backend="responses",
                                       agent_provider="openai",
                                       agent_name2role=roles)
        assert store.meeting.agent_name2role == roles
        reloaded = m2.MeetingStore.load(tmp_path, "meeting-multi")
        assert reloaded.meeting.agent_name2role == roles

    def test_names_unique_and_order_irrelevant(self) -> None:
        normalized = m2.normalize_agent_role_map({"a": "generator", "b": "passive_discriminator"})
        assert normalized["a"] == "generator"
        assert normalized["b"] == "passive_discriminator"


class TestValidation:
    @pytest.mark.parametrize("roles", [
        None,
        {},
        {"agent": "generator"},
    ])
    def test_none_or_empty_falls_back(self, roles) -> None:
        assert m2.normalize_agent_role_map(roles) == {"agent": "generator"}

    @pytest.mark.parametrize("bad", [
        {"human": "generator"},
        {"": "generator"},
        {"a": "root"},
        {"a": "generator", "b": 3},
        {"only_critic": "active_discriminator"},  # no generator
        {"only_passive": "passive_discriminator"},
    ])
    def test_invalid_role_maps_rejected(self, bad) -> None:
        with pytest.raises(m2.MeetingError):
            m2.normalize_agent_role_map(bad)

    def test_create_rejects_bad_roles(self, tmp_path) -> None:
        with pytest.raises(m2.MeetingError):
            m2.MeetingStore.create(tmp_path, "m", "hi", SKILL, "s", model="m",
                                   agent_name2role={"human": "generator"})


class TestAppendMembership:
    def test_chat_allows_any_role_member(self, store) -> None:
        store.add_agent_role("critic", "passive_discriminator")
        store.add_agent_role("coder", "generator")  # second generator not needed but legal
        store.append_chat("agent", "working", "ack")
        store.append_chat("critic", "reviewing", "ack")
        store.append_chat("human", "please", "request")
        kinds = [e["actor"] for e in store.events() if e["kind"] == "chat"]
        assert kinds[-3:] == ["agent", "critic", "human"]

    def test_chat_rejects_unknown_actor(self, store) -> None:
        with pytest.raises(m2.MeetingError, match="human or a meeting agent"):
            store.append_chat("stranger", "hi", "ack")

    def test_terminal_control_allows_generator_and_human(self, store) -> None:
        store.note_terminal_control("agent")
        store.note_terminal_control("human")
        ok = [e for e in store.events() if e["kind"] == "control"]
        assert len(ok) == 2

    def test_terminal_control_rejects_discriminator_or_stranger(self, store) -> None:
        store.add_agent_role("critic", "active_discriminator")
        with pytest.raises(m2.MeetingError, match="human or the generator"):
            store.note_terminal_control("critic")
        with pytest.raises(m2.MeetingError, match="human or the generator"):
            store.note_terminal_control("stranger")


class TestJoinAgent:
    def test_add_new_agent_and_reload(self, store, tmp_path) -> None:
        store.add_agent_role("critic", "passive_discriminator")
        assert store.meeting.agent_name2role["critic"] == "passive_discriminator"
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert reloaded.meeting.agent_name2role == {"agent": "generator",
                                                    "critic": "passive_discriminator"}
        store.append_chat("critic", "noted", "ack")  # now a member

    def test_resume_same_role_is_noop(self, store) -> None:
        store.add_agent_role("critic", "passive_discriminator")
        store.add_agent_role("critic", "passive_discriminator")
        assert list(store.meeting.agent_name2role).count("critic") == 1

    def test_role_change_rejected(self, store) -> None:
        store.add_agent_role("critic", "passive_discriminator")
        with pytest.raises(m2.MeetingError, match="immutable"):
            store.add_agent_role("critic", "active_discriminator")

    def test_join_human_or_bad_role_rejected(self, store) -> None:
        with pytest.raises(m2.MeetingError, match="reserved"):
            store.add_agent_role("human", "generator")
        with pytest.raises(m2.MeetingError, match="invalid agent role"):
            store.add_agent_role("critic", "root")


class TestLegacyManifestLoad:
    def test_manifest_without_role_map_backfills_default(self, store, tmp_path) -> None:
        manifest = store.run_dir / "meeting.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.pop("agent_name2role")
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert reloaded.meeting.agent_name2role == {"agent": "generator"}
