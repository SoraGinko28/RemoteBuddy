import json
import tempfile
import unittest
from pathlib import Path

from interactive_runner import InteractiveRunner
from meeting_v2 import MeetingStore


class _Client:
    stateful = False


class _Transport:
    pass


class InteractiveRunnerScoreIsolationTest(unittest.TestCase):
    def test_scores_never_enter_observation_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("skill", encoding="utf-8")
            store = MeetingStore.create(root, "isolation", "hello", skill, "session", "model")
            source = store.events()[0]
            score = store.append_score(
                "competent",
                "Judge Only",
                {
                    "kind": "chat",
                    "event_id": source["event_id"],
                    "region": {"start": 0, "end": 1},
                },
            )
            runner = InteractiveRunner(store, _Transport(), _Client())
            observation = runner._observation(store.events_after(0), False, force_history=True)
            self.assertNotIn(score["event_id"], observation)
            self.assertNotIn("Judge Only", observation)
            packet = json.loads(observation.split("\n\n", 1)[1])
            self.assertEqual(["chat"], [item["kind"] for item in packet["new_events"]])
            self.assertEqual([], packet["local_context"]["action_history"])
            self.assertEqual([1], [item["seq"] for item in packet["local_context"]["chat_history"]])


if __name__ == "__main__":
    unittest.main()
