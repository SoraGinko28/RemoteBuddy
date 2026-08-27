import unittest

from MeetingV3 import TextRegion, meeting_from_events, reference_from_event


class MeetingV2DomainTest(unittest.TestCase):
    def test_screen_reference_is_validated_and_excerpted(self):
        event = {"kind": "screen", "event_id": "event-1", "seq": 1, "captured_at": "t", "content": "abc\ndef"}
        value = reference_from_event(event, {"kind": "screen", "event_id": "event-1", "region": {"start": 1, "end": 5}})
        self.assertEqual("bc\nd", value["excerpt"])
        self.assertEqual(64, len(value["source_sha256"]))

    def test_chat_reference_uses_unicode_codepoint_range(self):
        event = {"kind": "chat", "event_id": "event-2", "seq": 2, "captured_at": "t", "actor": "human", "content": "你好，Agent"}
        value = reference_from_event(event, {"kind": "chat", "event_id": "event-2", "region": {"start": 0, "end": 2}})
        self.assertEqual("你好", value["excerpt"])

    def test_invalid_reference_and_stream_order(self):
        event = {"kind": "chat", "event_id": "event-2", "seq": 2, "captured_at": "t", "actor": "human", "content": "hello"}
        with self.assertRaises(ValueError):
            reference_from_event(event, {"kind": "chat", "event_id": "event-2", "region": {"start": 1, "end": 99}})
        with self.assertRaises(ValueError):
            reference_from_event(event, {"kind": "chat", "event_id": "event-2", "region": {"start": 1.5, "end": 2}})
        screen = {"kind": "screen", "event_id": "event-3", "seq": 3, "captured_at": "t", "content": "screen"}
        with self.assertRaisesRegex(ValueError, "outside"):
            reference_from_event(screen, {"kind": "screen", "event_id": "event-3", "region": {"start": 0, "end": 99}})
        self.assertIsNone(
            meeting_from_events(
                [{"kind": "chat", "event_id": "event-4", "seq": 4, "captured_at": "t", "content": "x", "actor": "human", "data": {"reference": {"kind": "screen", "region": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}}}]
            ).sorted_stream_chunks[0].stream_name2stream_chunk["chat"].chunks[0].reference
        )
        meeting = meeting_from_events([event, {"kind": "screen", "event_id": "event-1", "seq": 1, "captured_at": "s", "content": "screen"}])
        self.assertEqual(["s", "t"], [chunk.start for chunk in meeting.sorted_stream_chunks])


if __name__ == "__main__":
    unittest.main()
