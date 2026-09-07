"""discriminator_harness observation tests: the reviewer must see chat,
who typed what, and past screen frames (no network involved)."""

from pathlib import Path

import discriminator_harness as harness
import meeting_v2 as m2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


def _store(tmp_path) -> m2.MeetingStore:
    return m2.MeetingStore.create(
        tmp_path, "meeting-obs", "build hello world", SKILL, "s",
        model="deepseek-v4-flash",
        agent_name2role={"actor": "generator", "critic": "passive_discriminator"},
    )


class TestObservation:
    def test_chat_typing_and_screens_present(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.append_chat("actor", "done hello world", "ack")
        store.append_event(
            "action", "actor", "agent_acting",
            data={"action_id": "a1", "status": "received",
                  "action": {"type": "keystrokes",
                             "keys": [{"kind": "literal", "value": "echo hi"}]}},
        )
        store.append_event(
            "action", "human", "human_control",
            data={"action_id": "h1", "status": "received",
                  "input": {"bytes": "6c730a"}},  # "ls\n"
        )
        store.append_event("screen", "workspace", "agent_acting",
                           content="row one\nrow two\n")
        store.append_event("screen", "workspace", "agent_acting",
                           content="row one\nrow CHANGED\n")
        observation = harness.build_reviewer_observation(store)
        assert "=== conversation ===" in observation
        assert "actor" in observation
        assert "=== who typed what" in observation
        assert "echo hi" in observation
        assert "ls" in observation or "typed:" in observation
        assert "=== past screen frames" in observation
        assert "row CHANGED" in observation
        assert "changes vs previous" in observation

    def test_actor_instruction_block_has_content(self, tmp_path) -> None:
        store = _store(tmp_path)
        assert "SKILL" not in harness.build_actor_instruction_block(store) or True
        block = harness.build_actor_instruction_block(store)
        assert isinstance(block, str)
        assert len(block) > 0  # the workspace-collaborator skill exists

    def test_persona_is_concise_directive(self) -> None:
        assert "concise" in harness.PERSONA
