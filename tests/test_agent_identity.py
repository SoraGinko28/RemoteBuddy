"""Agent identity (slice 1): agent_name2role persistence and membership rules.

Goal: named agents + a single-source role map on the Meeting manifest,
keeping the legacy single agent ("actor"/actor) byte-for-byte compatible.
"""

import json
from pathlib import Path

import pytest

import meeting as m2

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
    def test_create_defaults_to_the_default_team(self, store) -> None:
        # Both judges ship with every new meeting (they only run when someone
        # @-mentions them or a work order calls them), and so does the assistant,
        # which looks while the actor works. None of them touches the terminal.
        assert store.meeting.agent_name2role == {
            m2.DEFAULT_AGENT_NAME: "actor",
            m2.DEFAULT_TECH_CRITIC_NAME: "tech_critic",
            m2.DEFAULT_MGMT_CRITIC_NAME: "mgmt_critic",
            m2.DEFAULT_ASSISTANT_NAME: "assistant",
        }

    def test_helpers_see_default_team(self, store) -> None:
        assert store.meeting.agent_names() == {m2.DEFAULT_AGENT_NAME,
                                               m2.DEFAULT_TECH_CRITIC_NAME,
                                               m2.DEFAULT_MGMT_CRITIC_NAME,
                                               m2.DEFAULT_ASSISTANT_NAME}
        assert store.meeting.role_of("actor") == "actor"
        assert store.meeting.role_of("tech_critic") == "tech_critic"
        assert store.meeting.role_of("mgmt_critic") == "mgmt_critic"
        assert store.meeting.role_of("assistant") == "assistant"
        assert store.meeting.is_agent_name("actor")
        assert not store.meeting.is_agent_name("human")
        assert store.meeting.actor_name() == "actor"

    def test_manifest_persists_role_map(self, store) -> None:
        payload = json.loads((store.run_dir / "meeting.json").read_text(encoding="utf-8"))
        assert payload["agent_name2role"] == dict(m2.DEFAULT_AGENT_ROLE_MAP)

    def test_reload_keeps_roles(self, store, tmp_path) -> None:
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert reloaded.meeting.agent_name2role == dict(m2.DEFAULT_AGENT_ROLE_MAP)

    def test_actor_name_default(self, store) -> None:
        assert store.meeting.actor_name() == "actor"

    def test_actor_name_explicit(self) -> None:
        meeting = m2.Meeting(
            meeting_id="m", status="created", phase="created",
            created_at="t", updated_at="t", session_name="s",
            session_state="x", pane_id=None, workspace="w",
            streams={}, model="m",
            agent_name2role={"coder": "actor", "tech_critic": "tech_critic"},
        )
        assert meeting.actor_name() == "coder"


class TestNamedControlOwner:
    def test_control_owner_accepts_actor_name(self, store) -> None:
        store.update(control_owner="actor")
        assert store.meeting.control_owner == "actor"

    def test_control_owner_accepts_any_member_name(self, store, tmp_path) -> None:
        store2 = m2.MeetingStore.create(
            tmp_path, "m-owner", "hi", SKILL, "s", model="m",
            agent_name2role={"coder": "actor", "tech_critic": "tech_critic"},
        )
        store2.update(control_owner="coder")
        assert store2.meeting.control_owner == "coder"

    def test_control_owner_rejects_stranger(self, store) -> None:
        with pytest.raises(m2.MeetingError, match="invalid control owner"):
            store.update(control_owner="stranger")


class TestMentions:
    def test_mentions_only_role_members_in_order(self) -> None:
        roles = {"actor": "actor", "tech_critic": "assistant"}
        assert m2.mentioned_agent_names("@tech_critic please look", roles) == ["tech_critic"]

    def test_unknown_and_human_ignored(self) -> None:
        roles = {"actor": "actor", "tech_critic": "tech_critic"}
        assert m2.mentioned_agent_names("@stranger @human hi", roles) == []

    def test_multiple_mentions_deduplicated(self) -> None:
        roles = {"actor": "actor", "tech_critic": "tech_critic", "mgmt_critic": "mgmt_critic"}
        assert m2.mentioned_agent_names("@tech_critic a @tech_critic b", roles) == ["tech_critic"]
        assert m2.mentioned_agent_names("@tech_critic and @actor", roles) == ["tech_critic", "actor"]
        assert m2.mentioned_agent_names("@mgmt_critic and @tech_critic", roles) == \
            ["mgmt_critic", "tech_critic"]

    def test_mention_boundaries(self) -> None:
        roles = {"tech_critic": "tech_critic"}
        # @tech_critic-extra is not a mention of @tech_critic; text before @ still counts
        assert m2.mentioned_agent_names("@tech_critic-extra", roles) == []
        assert m2.mentioned_agent_names("please @tech_critic look", roles) == ["tech_critic"]

    def test_non_string_content_ignored(self) -> None:
        assert m2.mentioned_agent_names(None, {"tech_critic": "tech_critic"}) == []
        assert m2.mentioned_agent_names(123, {"tech_critic": "tech_critic"}) == []


class TestExplicitRoles:
    def test_multi_agent_roles_roundtrip(self, tmp_path) -> None:
        roles = {"coder": "actor", "watcher": "assistant",
                 "sparring": "tech_critic", "auditor": "mgmt_critic"}
        store = m2.MeetingStore.create(tmp_path, "meeting-multi", "hi", SKILL,
                                       "s", model="m", agent_backend="responses",
                                       agent_provider="openai",
                                       agent_name2role=roles)
        assert store.meeting.agent_name2role == roles
        reloaded = m2.MeetingStore.load(tmp_path, "meeting-multi")
        assert reloaded.meeting.agent_name2role == roles

    def test_names_unique_and_order_irrelevant(self) -> None:
        normalized = m2.normalize_agent_role_map({"a": "actor", "b": "tech_critic",
                                                  "c": "mgmt_critic"})
        assert normalized["a"] == "actor"
        assert normalized["b"] == "tech_critic"
        assert normalized["c"] == "mgmt_critic"


class TestValidation:
    @pytest.mark.parametrize("roles", [None, {}])
    def test_none_or_empty_falls_back_to_the_default_team(self, roles) -> None:
        assert m2.normalize_agent_role_map(roles) == dict(m2.DEFAULT_AGENT_ROLE_MAP)

    def test_explicit_solo_actor_is_kept(self) -> None:
        # A caller that asks for one agent gets one agent; the judges are a
        # default, not a requirement.
        assert m2.normalize_agent_role_map({"actor": "actor"}) == {"actor": "actor"}

    @pytest.mark.parametrize("bad", [
        {"human": "actor"},
        {"": "actor"},
        {"a": "root"},
        {"a": "critic"},                  # the old single judge role is gone
        {"a": "actor", "b": 3},
        {"only_assistant": "assistant"},  # no actor
        {"only_tech": "tech_critic"},     # no actor
        {"only_mgmt": "mgmt_critic"},     # no actor
    ])
    def test_invalid_role_maps_rejected(self, bad) -> None:
        with pytest.raises(m2.MeetingError):
            m2.normalize_agent_role_map(bad)

    def test_create_rejects_bad_roles(self, tmp_path) -> None:
        with pytest.raises(m2.MeetingError):
            m2.MeetingStore.create(tmp_path, "m", "hi", SKILL, "s", model="m",
                                   agent_name2role={"human": "actor"})


class TestAppendMembership:
    def test_chat_allows_any_role_member(self, store) -> None:
        store.add_agent_role("tech_critic", "tech_critic")
        store.add_agent_role("mgmt_critic", "mgmt_critic")
        store.add_agent_role("coder", "actor")  # second actor not needed but legal
        store.append_chat("actor", "working", "ack")
        store.append_chat("tech_critic", "reviewing the code", "ack")
        store.append_chat("mgmt_critic", "reviewing the process", "ack")
        store.append_chat("human", "please", "request")
        kinds = [e["actor"] for e in store.events() if e["kind"] == "chat"]
        assert kinds[-4:] == ["actor", "tech_critic", "mgmt_critic", "human"]

    def test_chat_rejects_unknown_actor(self, store) -> None:
        with pytest.raises(m2.MeetingError, match="human or a meeting agent"):
            store.append_chat("stranger", "hi", "ack")

    def test_terminal_control_allows_actor_and_human(self, store) -> None:
        store.note_terminal_control("actor")
        store.note_terminal_control("human")
        ok = [e for e in store.events() if e["kind"] == "control"]
        assert len(ok) == 2

    def test_terminal_control_records_a_hand_back_with_no_owner(self, store) -> None:
        """A hand-back nobody picks up is still a record.

        No user_name means the pane is held by no one -- not a user, and not a
        missing record.
        """
        store.note_terminal_control("human")
        store.note_terminal_control(None)
        controls = [e for e in store.events() if e["kind"] == "control"]
        assert [event["data"]["user_name"] for event in controls] == ["human", None]
        assert controls[-1]["data"]["tmux_window_name"] == store.meeting.session_name
        assert controls[-1]["data"]["start_time"]

    def test_terminal_control_rejects_reviewer_or_stranger(self, store) -> None:
        store.add_agent_role("auditor", "assistant")
        with pytest.raises(m2.MeetingError, match="human or the actor"):
            store.note_terminal_control("auditor")
        # the default judges, still not the writer
        with pytest.raises(m2.MeetingError, match="human or the actor"):
            store.note_terminal_control("tech_critic")
        with pytest.raises(m2.MeetingError, match="human or the actor"):
            store.note_terminal_control("mgmt_critic")
        with pytest.raises(m2.MeetingError, match="human or the actor"):
            store.note_terminal_control("stranger")


class TestJoinAgent:
    def test_add_new_agent_and_reload(self, store, tmp_path) -> None:
        store.add_agent_role("sparring", "tech_critic")
        assert store.meeting.agent_name2role["sparring"] == "tech_critic"
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        assert reloaded.meeting.agent_name2role == {**m2.DEFAULT_AGENT_ROLE_MAP,
                                                    "sparring": "tech_critic"}
        store.append_chat("sparring", "noted", "ack")  # now a member

    def test_resume_same_role_is_noop(self, store) -> None:
        store.add_agent_role("tech_critic", "tech_critic")
        store.add_agent_role("tech_critic", "tech_critic")
        assert list(store.meeting.agent_name2role).count("tech_critic") == 1

    def test_role_change_rejected(self, store) -> None:
        store.add_agent_role("tech_critic", "tech_critic")
        with pytest.raises(m2.MeetingError, match="immutable"):
            store.add_agent_role("tech_critic", "mgmt_critic")

    def test_join_human_or_bad_role_rejected(self, store) -> None:
        with pytest.raises(m2.MeetingError, match="reserved"):
            store.add_agent_role("human", "actor")
        with pytest.raises(m2.MeetingError, match="invalid agent role"):
            store.add_agent_role("tech_critic", "root")
        with pytest.raises(m2.MeetingError, match="invalid agent role"):
            store.add_agent_role("tech_critic", "critic")  # the old single role is gone


class TestLegacyManifestLoad:
    def test_manifest_without_role_map_backfills_default(self, store, tmp_path) -> None:
        manifest = store.run_dir / "meeting.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.pop("agent_name2role")
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        reloaded = m2.MeetingStore.load(tmp_path, store.meeting.meeting_id)
        # pre-identity manifests stay on the legacy name
        assert reloaded.meeting.agent_name2role == {"agent": "actor"}


class TestConversationCredits:
    def test_exported_transcript_names_every_speaker(self, store) -> None:
        store.add_agent_role("tech_critic", "tech_critic")
        store.add_agent_role("mgmt_critic", "mgmt_critic")
        store.append_chat("actor", "listing files", "ack")
        store.append_chat("tech_critic", "the echo never ran", "final")
        store.append_chat("mgmt_critic", "the flow is complete", "final")
        store.append_chat("human", "carry on", "request")
        store.render_conversation()
        transcript = (store.run_dir / "conversation.md").read_text(encoding="utf-8")
        assert "## Actor · ack" in transcript
        assert "## Tech_critic · final" in transcript
        assert "## Mgmt_critic · final" in transcript
        assert "## You · request" in transcript
        assert "## Agent ·" not in transcript
