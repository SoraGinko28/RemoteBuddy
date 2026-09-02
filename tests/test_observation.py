import json
import tempfile
import unittest
from pathlib import Path

from interactive_runner import HISTORY_BYTES_LIMIT, HISTORY_EVENT_LIMIT, InteractiveRunner
from meeting_v2 import MeetingStore


def record(seq, kind="chat", actor="human", content="hello", message_type="request"):
    return {
        "seq": seq,
        "captured_at": "2026-09-01T10:00:00.000Z",
        "kind": kind,
        "actor": actor,
        "phase": "agent_thinking",
        "message_type": message_type,
        "content": content,
        "data": {},
    }


class BoundedHistoryTest(unittest.TestCase):
    def test_scores_are_never_exposed_to_agent_prompts(self):
        events = [
            record(1),
            record(2, kind="score", actor="human", content="secret annotation"),
            record(3, kind="action", actor="agent", content=""),
        ]
        records = InteractiveRunner._bounded_history(events)
        self.assertEqual([1, 3], [item["seq"] for item in records])
        self.assertNotIn("secret annotation", json.dumps(records))

    def test_event_limit_keeps_the_first_record_and_the_tail(self):
        events = [record(seq) for seq in range(1, HISTORY_EVENT_LIMIT + 51)]
        records = InteractiveRunner._bounded_history(events)
        self.assertEqual(HISTORY_EVENT_LIMIT, len(records))
        self.assertEqual(1, records[0]["seq"])
        self.assertEqual(HISTORY_EVENT_LIMIT + 50, records[-1]["seq"])

    def test_byte_limit_is_enforced_by_dropping_from_the_middle(self):
        events = [record(seq, content="x" * 4000) for seq in range(1, 101)]
        records = InteractiveRunner._bounded_history(events)
        encoded = len(json.dumps(records, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(encoded, HISTORY_BYTES_LIMIT + 64)
        self.assertEqual(1, records[0]["seq"])
        self.assertEqual(100, records[-1]["seq"])
        self.assertGreater(len(records), 1)

    def test_single_oversized_record_is_tail_truncated(self):
        events = [record(1, content="y" * (HISTORY_BYTES_LIMIT + 1024))]
        records = InteractiveRunner._bounded_history(events)
        self.assertEqual(1, len(records))
        self.assertEqual(4096, len(records[0]["content"]))


class FakeTransport:
    pass


class StatefulClient:
    stateful = True
    config = None


class StatelessClient:
    stateful = False
    config = None


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "SKILL.md"
        skill.write_text(
            "---\nname: workspace-collaborator\ndescription: Test.\n---\nBe specific.",
            encoding="utf-8",
        )
        self.store = MeetingStore.create(
            self.root,
            "observe-demo",
            "Please inspect the repository.",
            skill,
            "remotebuddy-observe-demo",
            "test-model",
        )
        self.store.append_screen("visible screen", "bash", str(self.store.workspace_path))
        self.store.append_chat("agent", "Working on it.", "progress")

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self, client):
        return InteractiveRunner(
            self.store, FakeTransport(), client, action_settle_seconds=0, typing_interval_seconds=0
        )

    def observation(self, client, unread=(), force_history=False):
        return json.loads(
            self.runner(client)._observation(list(unread), False, force_history=force_history)
            .split("\n\n", 1)[1]
        )

    def test_stateless_backend_receives_bounded_history_every_time(self):
        self.store.update(agent_cursor=3)
        packet = self.observation(StatelessClient())
        self.assertTrue(packet["local_context"]["chat_history"])
        self.assertIn("Please inspect the repository.", json.dumps(packet))

    def test_stateful_backend_gets_history_only_on_the_first_turn(self):
        self.store.update(agent_cursor=0)
        first = self.observation(StatefulClient())
        self.assertTrue(first["local_context"]["chat_history"])
        self.store.update(agent_cursor=3)
        later = self.observation(StatefulClient())
        self.assertEqual([], later["local_context"]["chat_history"])
        self.assertEqual([], later["local_context"]["action_history"])
        forced = self.observation(StatefulClient(), force_history=True)
        self.assertTrue(forced["local_context"]["chat_history"])

    def test_terminal_block_carries_the_latest_screen(self):
        packet = self.observation(StatelessClient())
        self.assertEqual("visible screen", packet["terminal"]["screen"])
        self.assertEqual("bash", packet["terminal"]["current_command"])

    def test_scores_and_screens_are_filtered_from_new_events(self):
        unread = [
            record(10, kind="screen", actor="workspace", content="frame"),
            record(11, kind="chat", actor="human", content="New request"),
        ]
        packet = self.observation(StatelessClient(), unread=unread)
        kinds = [event["kind"] for event in packet["new_events"]]
        self.assertEqual(["chat"], kinds)


if __name__ == "__main__":
    unittest.main()
