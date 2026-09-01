import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_protocol import (
    AgentAction,
    AgentProtocolError,
    action_json_schema,
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
        "keys": [],
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
            if event["actor"] == "agent" and event["message_type"] == "ack"
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
            if event["actor"] == "agent" and event["message_type"] == "ack"
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


if __name__ == "__main__":
    unittest.main()
