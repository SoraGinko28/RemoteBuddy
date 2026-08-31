import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from meeting_v2 import MeetingStore
from web_v2 import MeetingWebService, WebError


class WebV2ScoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("---\nname: workspace-collaborator\n---\n", encoding="utf-8")
        self.store = MeetingStore.create(
            self.root,
            "score-demo",
            "请检查 😀通过",
            self.skill,
            "remotebuddy-score-demo",
            "test-model",
        )
        self.screen = self.store.append_screen("终端 😀通过", "bash", str(self.store.workspace_path))
        self.service = MeetingWebService(
            project_root=self.root,
            skill_path=self.skill,
            runner_factory=lambda *args, **kwargs: None,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_and_updated_judge_name_are_local_and_old_scores_are_immutable(self):
        self.assertEqual({"judge_name": "human"}, self.service.settings())
        self.assertEqual({"judge_name": "Alice"}, self.service.update_settings({"judge_name": " Alice "}))
        source = self.store.events()[0]
        first = self.service.score(
            "score-demo",
            {
                "tag": "competent",
                "reference": {
                    "kind": "chat",
                    "event_id": source["event_id"],
                    "region": {"start": 4, "end": 5},
                    "excerpt": "伪造",
                    "source_sha256": "伪造",
                    "judge_name": "攻击者",
                },
            },
        )
        self.assertEqual(3, first["seq"])
        self.assertIsNone(first["data"]["score"])
        self.assertEqual("Alice", first["data"]["judge_name"])
        self.assertEqual("😀", first["data"]["reference"]["excerpt"])
        self.assertEqual(
            hashlib.sha256(source["content"].encode("utf-8")).hexdigest(),
            first["data"]["reference"]["source_sha256"],
        )

        self.service.update_settings({"judge_name": "Bob"})
        second = self.service.score(
            "score-demo",
            {
                "tag": "incompetent",
                "reference": {
                    "kind": "chat",
                    "event_id": source["event_id"],
                    "region": {"start": 4, "end": 5},
                },
            },
        )
        self.assertEqual(4, second["seq"])
        self.assertEqual("Bob", second["data"]["judge_name"])
        loaded = MeetingStore.load(self.root, "score-demo")
        self.assertEqual(2, len(loaded.score_messages()))
        self.assertEqual("Alice", loaded.score_messages()[0]["data"]["judge_name"])
        self.assertEqual("Bob", loaded.score_messages()[1]["data"]["judge_name"])
        conversation = (loaded.run_dir / "conversation.md").read_text(encoding="utf-8")
        self.assertNotIn("competent", conversation)
        self.assertNotIn("competent", conversation)
        self.assertNotIn("incompetent", conversation)
        self.assertFalse(self.service.jobs)

    def test_scores_are_allowed_for_all_terminal_states(self):
        source = self.store.events()[0]
        for status in ("completed", "failed", "interrupted"):
            loaded = MeetingStore.load(self.root, "score-demo")
            loaded.update(status=status, phase=status)
            event = self.service.score(
                "score-demo",
                {
                    "tag": "competent",
                    "reference": {
                        "kind": "chat",
                        "event_id": source["event_id"],
                        "region": {"start": 0, "end": 2},
                    },
                },
            )
            self.assertEqual("score", event["kind"])

    def test_tag_and_region_are_revalidated(self):
        source = self.store.events()[0]
        with self.assertRaisesRegex(WebError, "tag"):
            self.service.score(
                "score-demo",
                {"tag": "maybe", "reference": {"kind": "chat", "event_id": source["event_id"], "region": {"start": 0, "end": 1}}},
            )
        with self.assertRaisesRegex(WebError, "outside"):
            self.service.score(
                "score-demo",
                {"tag": "competent", "reference": {"kind": "chat", "event_id": source["event_id"], "region": {"start": 0, "end": 999}}},
            )
        with self.assertRaisesRegex(WebError, "kind"):
            self.service.score(
                "score-demo",
                {"tag": "competent", "reference": {"kind": "score", "event_id": source["event_id"], "region": {"start": 0, "end": 1}}},
            )

    def test_settings_file_is_json_and_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(WebError, "only judge_name"):
            self.service.update_settings({"judge_name": "A", "extra": 1})
        self.service.update_settings({"judge_name": "评判者"})
        payload = json.loads((self.root / ".remotebuddy" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual({"judge_name": "评判者"}, payload)


if __name__ == "__main__":
    unittest.main()
