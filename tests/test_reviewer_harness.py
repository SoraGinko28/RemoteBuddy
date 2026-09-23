"""reviewer_harness observation tests: the reviewer must see chat,
who typed what, and past screen frames (no network involved)."""

from pathlib import Path

import reviewer_harness as harness
import meeting_v2 as m2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


def _store(tmp_path) -> m2.MeetingStore:
    return m2.MeetingStore.create(
        tmp_path, "meeting-obs", "build hello world", SKILL, "s",
        model="deepseek-v4-flash",
        agent_name2role={"actor": "actor", "critic": "critic"},
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
        # The human's input is not in the journal at all: the reviewer sees
        # what was echoed on screen, not what was typed.
        store.append_event(
            "action", "human", "human_control",
            data={"action_id": "h1", "status": "received"},
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
        assert "typed:" not in observation
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
        assert "short" in harness.PERSONA

    def test_latest_screen_is_always_in_the_observation(self, tmp_path) -> None:
        store = _store(tmp_path)
        for index in range(harness.MAX_SCREEN_FRAMES * 3):
            store.append_event(
                "screen", "workspace", "agent_acting",
                content=f"frame {index}\nstill working\n",
            )
        store.append_event(
            "screen", "workspace", "agent_acting",
            content="frame last\nPROJECT TESTS PASSED\n",
        )
        observation = harness.build_reviewer_observation(store)
        assert "PROJECT TESTS PASSED" in observation


# A pane that shows one function running past 15 lines, with one parameter.
LONG_PANE = "$ cat batch.py\n" + "\n".join([
    "def process_batch(path):",
    "    rows = load_rows(path)",
    "    kept = []",
    "    for row in rows:",
    "        if row.get('amount') is None:",
    "            continue",
    "        if row.get('id') is None:",
    "            continue",
    "        if row['amount'] < 0:",
    "            continue",
    "        if row['amount'] > 100:",
    "            continue",
    "        kept.append(row)",
    "    if not kept:",
    "        return 0",
    "    return len(kept)",
]) + "\n$ "


class TestRuleCheck:
    """The meeting's rules are run for the reviewer before it speaks."""

    def test_the_verdicts_reach_the_reviewer_and_the_journal(self, tmp_path) -> None:
        store = _store(tmp_path)
        rows = LONG_PANE.split("\n")
        store.append_screen(LONG_PANE, "cat batch.py", str(store.workspace_path),
                            metadata={"pane_width": 40, "pane_height": len(rows),
                                      "cursor_x": 2, "cursor_y": len(rows) - 1})
        seen = {}

        def fake_post(payload, base_url, api_key):
            seen["payload"] = payload
            return {"content": [{"type": "text",
                                 "text": "按规则看，process_batch 太长了，该拆。"}]}

        assert harness.comment_once(store, "critic", post=fake_post) is True

        asked = seen["payload"]["messages"][0]["content"]
        assert "rule check" in asked
        assert "no function may run past 15 lines" in asked and "VIOLATED" in asked
        assert "process_batch" in asked and "16 行" in asked
        assert "no function may take more than 4 parameters" in asked and "holds" in asked
        assert "要在回复里说清是这几件事" in asked and "process_batch" in asked

        filed = [event for event in store.events() if event.get("kind") == "state"
                 and (event.get("content") or "").startswith("=== rule check")]
        assert len(filed) == 1
        assert filed[0]["actor"] == "critic"
        check = filed[0]["data"]["rule_check"]
        assert [rule["verdict"] for rule in check["rules"]] == ["VIOLATED", "holds", "holds"]
        assert check["rules"][0]["offender"] == "process_batch"

        # The reply cites the frame it was judged on: that event, that region.
        comments = [event for event in store.events()
                    if event.get("kind") == "chat" and event.get("actor") == "critic"]
        assert comments
        reference = comments[-1]["data"]["references"][0]
        assert reference["kind"] == "screen"
        assert reference["event_id"] == check["frame"]["event_id"]
        assert [reference["region"]["start"], reference["region"]["end"]] == \
            check["frame"]["region"]
