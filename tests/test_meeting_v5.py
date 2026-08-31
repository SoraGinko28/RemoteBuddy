import hashlib
import tempfile
import unittest
from datetime import datetime, timezone

from meetingV5 import (
    ChatReference,
    ChatStreamChunk,
    ScoreMessage,
    ScoreStreamChunk,
    ScreenStreamChunk,
    ScreenReference,
    TextRegion,
    meeting_from_events,
    to_dict,
)


UTC_TIME = "2026-08-28T08:00:00.000Z"


def event(seq, kind, content, actor="human", data=None):
    return {
        "seq": seq,
        "event_id": f"event-{seq:08d}",
        "captured_at": UTC_TIME,
        "kind": kind,
        "actor": actor,
        "phase": "waiting_for_user",
        "content": content,
        "message_type": "request" if kind == "chat" else None,
        "data": data or {},
    }


class MeetingV5ModelTest(unittest.TestCase):
    def test_frozen_model_normalizes_utc_and_serializes_iso(self):
        local = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
        capture = ScreenReference(
            "event-00000001",
            1,
            hashlib.sha256("屏幕".encode()).hexdigest(),
            "2026-08-28T16:00:00+08:00",
            "screen",
            TextRegion(0, 2),
            "屏幕",
        )
        self.assertIsInstance(capture.capture_time, datetime)
        self.assertEqual(local, capture.capture_time)
        self.assertEqual("2026-08-28T08:00:00.000Z", to_dict(capture)["capture_time"])
        with self.assertRaises((AttributeError, TypeError)):
            capture.seq = 2

    def test_score_projection_has_global_score_stream_and_provenance(self):
        source = event(1, "chat", "😀通过测试")
        reference = {
            "kind": "chat",
            "event_id": source["event_id"],
            "region": {"start": 1, "end": 2},
        }
        score = event(2, "score", "", data={
            "tag": "competent",
            "score": None,
            "judge_name": "Alice",
            "reference": reference,
        })
        meeting = meeting_from_events([score, source])
        score_chunk = meeting.sorted_stream_chunks[1].stream_name2stream_chunk["score"]
        self.assertIsInstance(score_chunk, ScoreStreamChunk)
        self.assertEqual("score", score_chunk.stream_name)
        message = score_chunk.chunks[0]
        self.assertEqual(("event-00000002", 2), (message.event_id, message.seq))
        self.assertIsInstance(message.reference, ChatReference)
        self.assertEqual("通", message.reference.excerpt)
        self.assertEqual(1, message.reference.region.start)
        self.assertEqual(2, message.reference.region.end)
        self.assertEqual("chat", message.reference.stream_name)
        self.assertIsNone(message.score)

    def test_invalid_tags_regions_and_score_stream_name_are_rejected(self):
        with self.assertRaises(ValueError):
            TextRegion(2, 2)
        with self.assertRaises(ValueError):
            ScoreStreamChunk([], "other")
        with self.assertRaises(ValueError):
            ScoreMessage("unknown", None, "human", UTC_TIME, ScreenReference(
                "event-00000001", 1, "hash", UTC_TIME, "screen", TextRegion(0, 1), "x"
            ))

    def test_chat_stream_shape_remains_compatible(self):
        self.assertEqual("chat", ChatStreamChunk("chat", []).stream_name)
        self.assertEqual("chat", ChatStreamChunk([], "chat").stream_name)
        self.assertEqual("screen", ScreenStreamChunk("screen", []).stream_name)
        self.assertEqual("score", ScoreStreamChunk("score", []).stream_name)


if __name__ == "__main__":
    unittest.main()
