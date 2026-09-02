import unittest

from references import TextRegion, reference_from_event


TS = "2026-09-01T10:00:00.000Z"


class ReferenceDomainTest(unittest.TestCase):
    def test_screen_reference_is_validated_and_excerpted(self):
        event = {
            "kind": "screen",
            "event_id": "event-1",
            "seq": 1,
            "captured_at": TS,
            "content": "abc\ndef",
        }
        value = reference_from_event(
            event, {"kind": "screen", "event_id": "event-1", "region": {"start": 1, "end": 5}}
        )
        self.assertEqual("bc\nd", value["excerpt"])
        self.assertEqual(64, len(value["source_sha256"]))
        self.assertEqual({"start": 1, "end": 5}, value["region"])
        self.assertEqual(1, value["seq"])

    def test_chat_reference_uses_unicode_codepoint_range(self):
        event = {
            "kind": "chat",
            "event_id": "event-2",
            "seq": 2,
            "captured_at": TS,
            "actor": "human",
            "content": "你好，Agent",
        }
        value = reference_from_event(
            event, {"kind": "chat", "event_id": "event-2", "region": {"start": 0, "end": 2}}
        )
        self.assertEqual("你好", value["excerpt"])
        self.assertEqual("human", value["user_name"])

    def test_invalid_regions_and_mismatched_sources_are_rejected(self):
        event = {
            "kind": "chat",
            "event_id": "event-2",
            "seq": 2,
            "captured_at": TS,
            "actor": "human",
            "content": "hello",
        }
        with self.assertRaisesRegex(ValueError, "outside"):
            reference_from_event(
                event, {"kind": "chat", "event_id": "event-2", "region": {"start": 1, "end": 99}}
            )
        with self.assertRaises(ValueError):
            reference_from_event(
                event, {"kind": "chat", "event_id": "event-2", "region": {"start": 1.5, "end": 2}}
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            reference_from_event(
                event, {"kind": "chat", "event_id": "event-other", "region": {"start": 0, "end": 2}}
            )
        with self.assertRaisesRegex(ValueError, "kind"):
            reference_from_event(
                event, {"kind": "action", "event_id": "event-2", "region": {"start": 0, "end": 2}}
            )
        # A chat payload cannot quote a screen event and vice versa.
        with self.assertRaises(ValueError):
            reference_from_event(
                event, {"kind": "screen", "event_id": "event-2", "region": {"start": 0, "end": 2}}
            )
        # Blank excerpts carry no evidence.
        spaces = dict(event, content="   x")
        with self.assertRaisesRegex(ValueError, "blank"):
            reference_from_event(
                spaces, {"kind": "chat", "event_id": "event-2", "region": {"start": 0, "end": 2}}
            )

    def test_source_requires_iso_timestamp_and_positive_sequence(self):
        base = {
            "kind": "screen",
            "event_id": "event-3",
            "seq": 3,
            "captured_at": TS,
            "content": "screen",
        }
        with self.assertRaisesRegex(ValueError, "datetime"):
            reference_from_event(
                dict(base, captured_at="not-a-time"),
                {"kind": "screen", "event_id": "event-3", "region": {"start": 0, "end": 3}},
            )
        with self.assertRaisesRegex(ValueError, "sequence"):
            reference_from_event(
                dict(base, seq=0),
                {"kind": "screen", "event_id": "event-3", "region": {"start": 0, "end": 3}},
            )

    def test_text_region_must_be_half_open_and_non_negative(self):
        with self.assertRaises(ValueError):
            TextRegion(-1, 2)
        with self.assertRaises(ValueError):
            TextRegion(2, 2)
        region = TextRegion(0, 5)
        self.assertEqual((0, 5), (region.start, region.end))


if __name__ == "__main__":
    unittest.main()
