"""Compatibility exports for the V3 text-reference domain model.

Schema-v2 persistence remains in :mod:`meeting_v2`; this module is retained
only so older imports continue to resolve without reintroducing rectangles.
"""
from MeetingV3 import (
    ChatMessage,
    ChatReference,
    ChatStreamChunk,
    ContentReference,
    Meeting,
    ScreenCapture,
    ScreenReference,
    ScreenStreamChunk,
    StreamChunkDict,
    TextRegion,
    meeting_from_events,
    reference_from_event,
    to_dict,
)

__all__ = [
    "ChatMessage", "ChatReference", "ChatStreamChunk", "ContentReference",
    "Meeting", "ScreenCapture", "ScreenReference", "ScreenStreamChunk",
    "StreamChunkDict", "TextRegion", "meeting_from_events", "reference_from_event", "to_dict",
]
