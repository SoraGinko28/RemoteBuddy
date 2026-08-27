"""Domain views for ordered Chat and Screen streams.

Screen and Chat references share one TextRegion model. Event ids, sequence
numbers and source hashes remain immutable persistence anchors.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


@dataclass(frozen=True)
class TextRegion:
    start: int
    end: int

    def __post_init__(self) -> None:
        _integer(self.start, "text region start")
        _integer(self.end, "text region end")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("text region must be a non-empty half-open range")


@dataclass(frozen=True)
class ScreenReference:
    event_id: str
    seq: int
    source_sha256: str
    capture_time: str
    stream_name: str
    region: TextRegion
    excerpt: str


@dataclass(frozen=True)
class ChatReference:
    event_id: str
    seq: int
    source_sha256: str
    msg_time: str
    user_name: str
    stream_name: str
    region: TextRegion
    excerpt: str


ContentReference = Union[ScreenReference, ChatReference]


@dataclass(frozen=True)
class ScreenCapture:
    screen_data: str
    captured_at: str
    event_id: str = ""
    seq: int = 0
    source_sha256: str = ""


@dataclass(frozen=True)
class ChatMessage:
    message: str
    user_name: str
    time: str
    reference: Optional[ContentReference] = None
    event_id: str = ""
    seq: int = 0


@dataclass(frozen=True)
class ScreenStreamChunk:
    chunks: List[ScreenCapture]
    stream_name: str
    owner_user_name: str = "workspace"


@dataclass(frozen=True)
class ChatStreamChunk:
    stream_name: str
    chunks: List[ChatMessage]


@dataclass(frozen=True)
class StreamChunkDict:
    stream_name2stream_chunk: Dict[str, Union[ScreenStreamChunk, ChatStreamChunk]]
    start: str
    end: str


@dataclass(frozen=True)
class Meeting:
    sorted_stream_chunks: List[StreamChunkDict] = field(default_factory=list)


def _source(event: Mapping[str, Any], kind: str) -> Tuple[str, int, str, str]:
    if event.get("kind") != kind or not isinstance(event.get("event_id"), str):
        raise ValueError("reference source event is invalid")
    seq, content, timestamp = event.get("seq"), event.get("content", ""), event.get("captured_at")
    if (
        isinstance(seq, bool)
        or not isinstance(seq, int)
        or seq < 1
        or not isinstance(content, str)
        or not isinstance(timestamp, str)
    ):
        raise ValueError("reference source event has invalid fields")
    return event["event_id"], seq, timestamp, content


def _excerpt(content: str, region: TextRegion) -> str:
    if region.end > len(content):
        raise ValueError("reference is outside the source content")
    excerpt = content[region.start : region.end]
    if not excerpt.strip():
        raise ValueError("reference cannot be blank")
    return excerpt


def reference_from_event(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate an anchor and derive the selected text from the source event."""
    if not isinstance(payload, Mapping) or payload.get("kind") not in {"screen", "chat"}:
        raise ValueError("reference kind must be screen or chat")
    kind = str(payload["kind"])
    event_id, seq, timestamp, content = _source(event, kind)
    if payload.get("event_id") != event_id:
        raise ValueError("reference event does not match the selected source")
    raw = payload.get("region")
    if not isinstance(raw, Mapping) or set(raw) != {"start", "end"}:
        raise ValueError("reference region must contain start and end")
    try:
        region = TextRegion(_integer(raw["start"], "text region start"), _integer(raw["end"], "text region end"))
        excerpt = _excerpt(content, region)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if kind == "screen":
        reference: ContentReference = ScreenReference(event_id, seq, _hash(content), timestamp, "screen", region, excerpt)
    else:
        reference = ChatReference(event_id, seq, _hash(content), timestamp, str(event.get("actor", "")), "chat", region, excerpt)
    return {"kind": kind, **asdict(reference)}


def _reference_from_dict(value: Mapping[str, Any]) -> Optional[ContentReference]:
    """Read only the V3 text shape; legacy rectangle data is ignored."""
    try:
        region_data = value.get("region")
        if not isinstance(region_data, Mapping) or set(region_data) != {"start", "end"}:
            return None
        region = TextRegion(region_data["start"], region_data["end"])
        if value.get("kind") == "screen":
            return ScreenReference(value["event_id"], _integer(value["seq"], "reference seq"), value["source_sha256"], value["capture_time"], value["stream_name"], region, value["excerpt"])
        if value.get("kind") == "chat":
            return ChatReference(value["event_id"], _integer(value["seq"], "reference seq"), value["source_sha256"], value["msg_time"], value["user_name"], value["stream_name"], region, value["excerpt"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def meeting_from_events(events: Sequence[Mapping[str, Any]]) -> Meeting:
    chunks: List[StreamChunkDict] = []
    for event in sorted(events, key=lambda item: int(item.get("seq", 0))):
        kind = event.get("kind")
        timestamp = str(event.get("captured_at", ""))
        seq = int(event.get("seq", 0))
        event_id = str(event.get("event_id", ""))
        if kind == "screen":
            content = str(event.get("content", ""))
            chunk: Union[ScreenStreamChunk, ChatStreamChunk] = ScreenStreamChunk([ScreenCapture(content, timestamp, event_id, seq, _hash(content))], "screen")
            name = "screen"
        elif kind == "chat":
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            reference = _reference_from_dict(data["reference"]) if isinstance(data.get("reference"), Mapping) else None
            chunk = ChatStreamChunk("chat", [ChatMessage(str(event.get("content", "")), str(event.get("actor", "")), timestamp, reference, event_id, seq)])
            name = "chat"
        else:
            continue
        chunks.append(StreamChunkDict({name: chunk}, timestamp, timestamp))
    return Meeting(chunks)


def to_dict(value: Any) -> Any:
    return asdict(value)
