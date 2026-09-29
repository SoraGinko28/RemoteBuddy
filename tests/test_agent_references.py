import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_protocol import (
    AgentAction,
    AgentActionError,
    AgentProtocolError,
    action_json_schema,
    decode_action_text,
)
from interactive_runner import InteractiveRunner
from meeting_v2 import MeetingStore
from tests.test_interactive_runner import FakeClient, FakeTransport, make_action


def chat_payload(**changes):
    payload = {
        "type": "chat",
        "message_type": "ack",
        "content": "Looking at the earlier request now.",
        "command": "none",
        "path": "",
        "delay_ms": 0,
        "references": [],
    }
    payload.update(changes)
    return payload


class AgentReferenceProtocolTest(unittest.TestCase):
    def test_chat_action_accepts_seq_and_event_id_references(self):
        action = AgentAction.from_dict(
            chat_payload(
                references=[
                    {"kind": "chat", "seq": 1, "region": {"start": 0, "end": 6}},
                    {"kind": "screen", "seq": 2, "region": None},
                    {"kind": "chat", "event_id": "evt-abc", "region": {"start": 2, "end": 4}},
                ]
            )
        )
        self.assertEqual(3, len(action.references))
        self.assertEqual({"kind": "chat", "seq": 1, "region": {"start": 0, "end": 6}}, action.references[0])
        self.assertIsNone(action.references[1]["region"])
        self.assertEqual("evt-abc", action.references[2]["event_id"])

    def test_payload_without_references_field_still_parses(self):
        payload = chat_payload()
        payload.pop("references")
        action = AgentAction.from_dict(payload)
        self.assertEqual((), tuple(action.references))

    def test_invalid_references_are_rejected(self):
        cases = [
            [{"kind": "score", "seq": 1, "region": None}],
            [{"kind": "chat", "region": None}],
            [{"kind": "chat", "seq": 0, "region": None}],
            [{"kind": "chat", "seq": 1, "region": {"start": 3, "end": 3}}],
            [{"kind": "chat", "seq": 1, "region": {"start": -1, "end": 3}}],
            [{"kind": "chat", "seq": 1, "region": {"start": 0}}],
            [{"kind": "chat", "seq": 1, "region": None, "extra": 1}],
            [{"kind": "chat", "seq": 1, "region": None}] * 33,
            "not-a-list",
        ]
        for references in cases:
            with self.assertRaises(AgentProtocolError, msg=str(references)[:80]):
                AgentAction.from_dict(chat_payload(references=references))

    def test_only_chat_and_done_may_carry_references(self):
        with self.assertRaises(AgentProtocolError):
            AgentAction.from_dict(
                chat_payload(
                    type="command",
                    message_type="none",
                    content="",
                    command="pwd",
                    references=[{"kind": "chat", "seq": 1, "region": None}],
                )
            )
        action = AgentAction.from_dict(
            chat_payload(
                type="done",
                message_type="final",
                content="Finished.",
                references=[{"kind": "screen", "seq": 2, "region": None}],
            )
        )
        self.assertEqual(1, len(action.references))

    def test_action_schema_exposes_references_like_other_fields(self):
        schema = action_json_schema()
        variants = schema["properties"]["action"]["anyOf"]
        by_type = {variant["properties"]["type"]["enum"][0]: variant for variant in variants}
        for name, variant in by_type.items():
            self.assertIn("references", variant["required"], name)
            field = variant["properties"]["references"]
            if name in {"chat", "done"}:
                self.assertEqual(32, field["maxItems"], name)
                item = field["items"]
                self.assertEqual(["kind", "seq", "region"], item["required"])
            else:
                self.assertEqual(0, field["maxItems"], name)


class AgentReferenceRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\nBe specific.",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "reference-demo",
            "Create notes.md with a title.",
            self.skill,
            "remotebuddy-reference-demo",
            "test-model",
        )
        self.transport = FakeTransport()

    def tearDown(self):
        self.temporary.cleanup()

    def run_runner(self, outcomes):
        client = FakeClient(outcomes)
        runner = InteractiveRunner(
            self.store, self.transport, client, action_settle_seconds=0, typing_interval_seconds=0
        )
        runner.start()
        return client, MeetingStore.load(self.root, "reference-demo")

    def test_agent_chat_reference_is_stored_like_a_human_reference(self):
        _client, loaded = self.run_runner(
            [
                make_action(
                    content="I will start from your request.",
                    references=[{"kind": "chat", "seq": 1, "region": {"start": 0, "end": 6}}],
                ),
                make_action(
                    "done",
                    message_type="final",
                    content="Done for now.",
                    references=[{"kind": "screen", "seq": 2, "region": None}],
                ),
            ]
        )
        source = loaded.events()[0]
        self.assertEqual("chat", source["kind"])
        ack = next(
            event
            for event in loaded.chat_messages()
            if event["actor"] == "actor" and event["message_type"] == "ack"
        )
        references = ack["data"]["references"]
        self.assertEqual(1, len(references))
        self.assertEqual("chat", references[0]["kind"])
        self.assertEqual(source["event_id"], references[0]["event_id"])
        self.assertEqual("Create", references[0]["excerpt"])
        self.assertEqual(
            hashlib.sha256(source["content"].encode("utf-8")).hexdigest(),
            references[0]["source_sha256"],
        )
        final = next(
            event for event in loaded.chat_messages() if event["message_type"] == "final"
        )
        screen_source = next(event for event in loaded.events() if event["kind"] == "screen")
        screen_reference = final["data"]["references"][0]
        self.assertEqual("screen", screen_reference["kind"])
        self.assertEqual(screen_source["event_id"], screen_reference["event_id"])
        self.assertEqual(screen_source["content"], screen_reference["excerpt"])

    def test_unresolvable_reference_is_rejected_and_agent_can_retry(self):
        _client, loaded = self.run_runner(
            [
                make_action(
                    references=[{"kind": "chat", "seq": 999, "region": None}],
                ),
                make_action(content="Acknowledged without a quote."),
                make_action(
                    "done",
                    message_type="final",
                    content="Finishing after the retry.",
                ),
            ]
        )
        rejected = [
            event
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "rejected"
        ]
        self.assertEqual(1, len(rejected))
        self.assertIn("reference source event was not found", rejected[0]["content"])
        acks = [
            event
            for event in loaded.chat_messages()
            if event["actor"] == "actor" and event["message_type"] == "ack"
        ]
        self.assertEqual(1, len(acks))
        self.assertNotIn("references", acks[0]["data"])
        self.assertEqual("completion_proposed", loaded.meeting.phase)

    def test_region_outside_source_content_is_rejected(self):
        _client, loaded = self.run_runner(
            [
                make_action(
                    references=[{"kind": "chat", "seq": 1, "region": {"start": 0, "end": 10_000}}],
                ),
                make_action(content="Acknowledged without a quote."),
                make_action("done", message_type="final", content="Finished."),
            ]
        )
        rejected = [
            event
            for event in loaded.events()
            if event["kind"] == "action" and event["data"].get("status") == "rejected"
        ]
        self.assertEqual(1, len(rejected))
        self.assertIn("invalid reference", rejected[0]["content"])


class AgentActionFieldDiagnosticsTest(unittest.TestCase):
    """The rejection text is the model's correction hint: name the fields."""

    def test_unknown_field_is_named_in_the_error(self):
        with self.assertRaisesRegex(AgentProtocolError, "unexpected fields: reasoning"):
            AgentAction.from_dict(chat_payload(reasoning="thinking out loud"))

    def test_missing_required_field_is_named_in_the_error(self):
        payload = chat_payload()
        payload.pop("message_type")
        with self.assertRaisesRegex(AgentProtocolError, "missing fields: message_type"):
            AgentAction.from_dict(payload)


class AgentActionToleranceTest(unittest.TestCase):
    """Model families differ in how faithfully they copy the action schema."""

    def test_double_action_envelope_is_unwrapped(self):
        action = decode_action_text(json.dumps({"action": {"action": chat_payload()}}))
        self.assertEqual("chat", action.type)

    def test_split_action_envelope_is_merged(self):
        # Ground truth from the Trae backend: the model wrapped the action body
        # and left the envelope-level fields next to it.
        text = json.dumps(
            {
                "action": {
                    "type": "chat",
                    "content": "Looking at the directory now.",
                    "command": "none",
                    "path": "",
                    "delay_ms": 0,
                },
                "references": [],
                "message_type": "ack",
                "result_needed": False,
            }
        )
        action = decode_action_text(text)
        self.assertEqual("chat", action.type)
        self.assertEqual("ack", action.message_type)
        self.assertFalse(action.result_needed)

    def test_unused_fields_may_be_omitted(self):
        action = AgentAction.from_dict(
            {"type": "command", "message_type": "none", "command": "ls -la"}
        )
        self.assertEqual("ls -la", action.command)
        self.assertEqual("", action.diff)
        self.assertEqual(0, action.delay_ms)
        self.assertEqual("", action.content)

    def test_non_chat_action_defaults_message_type_to_none(self):
        action = AgentAction.from_dict(
            {"type": "diff", "diff": "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"}
        )
        self.assertEqual("none", action.message_type)

    def test_a_model_that_says_chat_final_means_done(self):
        # Ground truth from the Trae backend with a picture attached: the model
        # sent everything `done` carries, but named the type `chat`.
        for message_type in ("final", "done"):
            action = decode_action_text(json.dumps({
                "type": "chat", "message_type": message_type,
                "content": "图片里没有文字，只有一个红色圆形。",
            }))
            self.assertEqual("done", action.type)
            self.assertEqual("final", action.message_type)

    def test_chat_final_without_content_is_still_wrong(self):
        with self.assertRaisesRegex(AgentActionError, "chat requires"):
            decode_action_text(json.dumps({"type": "chat", "message_type": "final", "content": ""}))


class ActionShapeToleranceTest(unittest.TestCase):
    """模型把动作写成别的样子时，能纠回来的就纠，别再为字段名把一场会判死。"""

    def test_an_outer_placeholder_type_does_not_hide_the_real_action(self):
        """`{"type":"none","action":{...}}`：里层才是真的动作（真事：这么连来三次，一
        场会被判成 api_error）。"""
        action = AgentAction.from_dict(
            {
                "type": "none",
                "action": {
                    "type": "diff",
                    "diff": "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n",
                },
            }
        )
        self.assertEqual("diff", action.type)
        self.assertIn("+y", action.diff)

    def test_a_message_type_that_names_the_action_is_read_as_ack(self):
        """`"message_type": "chat"` 的意思是"这是一句话"，当成 ack，不是非法值。"""
        action = AgentAction.from_dict(
            {"type": "chat", "message_type": "chat", "content": "收到"}
        )
        self.assertEqual("chat", action.type)
        self.assertEqual("ack", action.message_type)

    def test_a_patch_pasted_into_command_moves_to_diff(self):
        """整份补丁塞进 `command`（还常用 Codex 的 `*** Begin Patch` 写法）：搬到 `diff`。"""
        pasted = (
            "*** Begin Patch\n*** Update File: a.py\n"
            "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        action = AgentAction.from_dict({"type": "diff", "command": pasted})
        self.assertEqual("diff", action.type)
        self.assertEqual("none", action.command)
        self.assertTrue(action.diff.startswith("*** Begin Patch"))

    def test_an_empty_diff_says_where_the_patch_goes(self):
        with self.assertRaisesRegex(Exception, "the unified diff goes in `diff`"):
            AgentAction.from_dict({"type": "diff", "command": "ls -l"})


if __name__ == "__main__":
    unittest.main()


class AgentActionRejectionHintTest(unittest.TestCase):
    """拒绝理由就是模型改错用的提示：要说清楚它该写哪几种。"""

    def test_an_unknown_type_is_told_which_types_exist(self):
        with self.assertRaisesRegex(
                AgentActionError,
                "model action type is invalid: expected chat, command, diff, "
                "observe, or done"):
            decode_action_text(json.dumps({"type": "write", "message_type": "none"}))

    def test_a_type_the_phase_forbids_is_told_what_the_phase_allows(self):
        with self.assertRaisesRegex(
                AgentActionError, "not allowed in the current phase: expected chat, or done"):
            AgentAction.from_dict({"type": "command", "message_type": "none", "command": "ls"},
                                  allowed_types=["chat", "done"])

    def test_the_rejection_carries_what_the_model_wrote(self):
        text = json.dumps({"type": "write", "path": "a.py", "message_type": "none"})
        with self.assertRaises(AgentActionError) as caught:
            decode_action_text(text)
        self.assertEqual(text, caught.exception.source)
