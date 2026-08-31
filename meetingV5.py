"""Executable V5 domain views for chat, screen, and score streams.

The durable source of truth remains the v2 event journal. This module is a
typed projection of that journal and keeps the immutable event anchors needed
to validate and replay text references.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


SCORE_TAGS = frozenset({"competent", "incompetent"})
# Keep the longer name available to callers that use the persistence/API
# vocabulary.  The frozenset prevents accidental mutation of the contract.
VALID_SCORE_TAGS = SCORE_TAGS
SOURCE_KINDS = frozenset({"screen", "chat"})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _datetime(value: Any, label: str = "time") -> datetime:
    """Parse an aware timestamp and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{label} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class TextRegion:
    """A non-empty half-open range measured in Unicode code points."""

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
    capture_time: datetime
    stream_name: str
    region: TextRegion
    excerpt: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("screen reference event id is required")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise ValueError("screen reference sequence must be positive")
        if not isinstance(self.source_sha256, str) or not self.source_sha256:
            raise ValueError("screen reference source hash is required")
        if not isinstance(self.stream_name, str) or not self.stream_name:
            raise ValueError("screen reference stream name is required")
        if not isinstance(self.excerpt, str) or not self.excerpt:
            raise ValueError("screen reference excerpt is required")
        object.__setattr__(self, "capture_time", _datetime(self.capture_time, "capture_time"))
        if not isinstance(self.region, TextRegion):
            raise ValueError("screen reference region must be a TextRegion")


@dataclass(frozen=True)
class ChatReference:
    event_id: str
    seq: int
    source_sha256: str
    msg_time: datetime
    user_name: str
    stream_name: str
    region: TextRegion
    excerpt: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("chat reference event id is required")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1:
            raise ValueError("chat reference sequence must be positive")
        if not isinstance(self.source_sha256, str) or not self.source_sha256:
            raise ValueError("chat reference source hash is required")
        if not isinstance(self.user_name, str):
            raise ValueError("chat reference user name must be text")
        if not isinstance(self.stream_name, str) or not self.stream_name:
            raise ValueError("chat reference stream name is required")
        if not isinstance(self.excerpt, str) or not self.excerpt:
            raise ValueError("chat reference excerpt is required")
        object.__setattr__(self, "msg_time", _datetime(self.msg_time, "msg_time"))
        if not isinstance(self.region, TextRegion):
            raise ValueError("chat reference region must be a TextRegion")


ContentReference = Union[ScreenReference, ChatReference]


@dataclass(frozen=True)
class ScreenCapture:
    screen_data: str
    captured_at: datetime
    event_id: str = ""
    seq: int = 0
    source_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "captured_at", _datetime(self.captured_at, "captured_at"))


@dataclass(frozen=True)
class ChatMessage:
    message: str
    user_name: str
    time: datetime
    reference: Optional[ContentReference] = None
    event_id: str = ""
    seq: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", _datetime(self.time, "time"))
        if not isinstance(self.message, str):
            raise ValueError("chat message must be text")
        if self.reference is not None and not isinstance(self.reference, (ScreenReference, ChatReference)):
            raise ValueError("chat reference must be a screen or chat reference")


@dataclass(frozen=True)
class ScoreMessage:
    tag: str
    score: Optional[float]
    judge_name: str
    time: datetime
    reference: ContentReference
    event_id: str = ""
    seq: int = 0
    comment: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or self.tag not in SCORE_TAGS:
            raise ValueError("score tag must be competent or incompetent")
        if not isinstance(self.judge_name, str) or not self.judge_name.strip():
            raise ValueError("judge name must be non-empty")
        object.__setattr__(self, "judge_name", self.judge_name.strip())
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
        ):
            raise ValueError("score must be a finite number or null")
        if self.score is not None:
            object.__setattr__(self, "score", float(self.score))
        if self.comment is not None:
            if not isinstance(self.comment, str):
                raise ValueError("score comment must be text or null")
            object.__setattr__(self, "comment", self.comment.strip() or None)
        object.__setattr__(self, "time", _datetime(self.time, "time"))
        if not isinstance(self.reference, (ScreenReference, ChatReference)):
            raise ValueError("score reference must be a screen or chat reference")


@dataclass(frozen=True)
class ScreenStreamChunk:
    chunks: List[ScreenCapture]
    stream_name: str
    owner_user_name: str = "workspace"

    def __post_init__(self) -> None:
        # V3 exposed both ``chunks, stream_name`` and ``stream_name, chunks``
        # positional conventions.  Accept the latter while keeping the V3
        # field layout and serialized shape unchanged.
        if isinstance(self.chunks, str) and isinstance(self.stream_name, (list, tuple)):
            chunks, stream_name = self.chunks, self.stream_name
            object.__setattr__(self, "chunks", stream_name)
            object.__setattr__(self, "stream_name", chunks)


@dataclass(frozen=True)
class ChatStreamChunk:
    stream_name: str
    chunks: List[ChatMessage]

    def __post_init__(self) -> None:
        if isinstance(self.stream_name, (list, tuple)) and isinstance(self.chunks, str):
            stream_name, chunks = self.stream_name, self.chunks
            object.__setattr__(self, "stream_name", chunks)
            object.__setattr__(self, "chunks", stream_name)


@dataclass(frozen=True)
class ScoreStreamChunk:
    chunks: List[ScoreMessage]
    stream_name: str = "score"

    def __post_init__(self) -> None:
        if isinstance(self.chunks, str) and isinstance(self.stream_name, (list, tuple)):
            chunks, stream_name = self.chunks, self.stream_name
            object.__setattr__(self, "chunks", stream_name)
            object.__setattr__(self, "stream_name", chunks)
        if self.stream_name != "score":
            raise ValueError("score stream name must be score")


StreamChunk = Union[ScreenStreamChunk, ChatStreamChunk, ScoreStreamChunk]


@dataclass(frozen=True)
class StreamChunkDict:
    stream_name2stream_chunk: Dict[str, StreamChunk]
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = _datetime(self.start, "stream start")
        end = _datetime(self.end, "stream end")
        if end < start:
            raise ValueError("stream end must not precede stream start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True)
class Meeting:
    sorted_stream_chunks: List[StreamChunkDict] = field(default_factory=list)


def to_dict(value: Any) -> Any:
    """Serialize domain values into JSON-compatible primitives."""
    if isinstance(value, datetime):
        return _datetime_text(value)
    if is_dataclass(value):
        return {item.name: to_dict(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    return value


def _source(event: Mapping[str, Any], kind: str) -> Tuple[str, int, datetime, str]:
    if event.get("kind") != kind or not isinstance(event.get("event_id"), str):
        raise ValueError("reference source event is invalid")
    event_id = str(event["event_id"])
    if not event_id:
        raise ValueError("reference source event id is required")
    seq = _integer(event.get("seq"), "source sequence")
    if seq < 1:
        raise ValueError("source sequence must be positive")
    content = event.get("content", "")
    if not isinstance(content, str):
        raise ValueError("reference source content must be text")
    timestamp = _datetime(event.get("captured_at"), "source captured_at")
    return event_id, seq, timestamp, content


def _excerpt(content: str, region: TextRegion) -> str:
    if region.end > len(content):
        raise ValueError("reference is outside the source content")
    excerpt = content[region.start : region.end]
    if not excerpt.strip():
        raise ValueError("reference cannot be blank")
    return excerpt


def _reference_object(event: Mapping[str, Any], payload: Mapping[str, Any]) -> ContentReference:
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise ValueError("reference kind must be screen or chat")
    event_id, seq, timestamp, content = _source(event, str(kind))
    if payload.get("event_id") != event_id:
        raise ValueError("reference event does not match the selected source")
    raw = payload.get("region")
    if not isinstance(raw, Mapping) or set(raw) != {"start", "end"}:
        raise ValueError("reference region must contain start and end")
    region = TextRegion(_integer(raw["start"], "text region start"), _integer(raw["end"], "text region end"))
    excerpt = _excerpt(content, region)
    source_hash = _hash(content)
    stream_name = "screen" if kind == "screen" else "chat"
    if kind == "screen":
        return ScreenReference(event_id, seq, source_hash, timestamp, stream_name, region, excerpt)
    return ChatReference(
        event_id,
        seq,
        source_hash,
        timestamp,
        str(event.get("actor", "")),
        stream_name,
        region,
        excerpt,
    )


def reference_from_event(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a client anchor and derive all source-owned reference fields."""
    if not isinstance(event, Mapping) or not isinstance(payload, Mapping):
        raise ValueError("reference must be an object")
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise ValueError("reference kind must be screen or chat")
    reference = _reference_object(event, payload)
    return {"kind": str(kind), **to_dict(reference)}


def _reference_from_dict(value: Mapping[str, Any]) -> Optional[ContentReference]:
    """Parse a persisted full reference, accepting the older V3 shape."""
    if not isinstance(value, Mapping):
        return None
    try:
        kind = value.get("kind")
        raw_region = value.get("region")
        if not isinstance(kind, str) or kind not in SOURCE_KINDS or not isinstance(raw_region, Mapping):
            return None
        region = TextRegion(raw_region["start"], raw_region["end"])
        event_id = value["event_id"]
        if not isinstance(event_id, str) or not event_id:
            return None
        seq = _integer(value["seq"], "reference seq")
        source_sha256 = value["source_sha256"]
        if not isinstance(source_sha256, str) or not source_sha256:
            return None
        excerpt = value.get("excerpt")
        if not isinstance(excerpt, str):
            return None
        stream_name = str(value.get("stream_name") or kind)
        if kind == "screen":
            return ScreenReference(
                event_id,
                seq,
                source_sha256,
                _datetime(value["capture_time"], "capture_time"),
                stream_name,
                region,
                excerpt,
            )
        return ChatReference(
            event_id,
            seq,
            source_sha256,
            _datetime(value["msg_time"], "msg_time"),
            value["user_name"] if isinstance(value["user_name"], str) else str(value["user_name"]),
            stream_name,
            region,
            excerpt,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _reference_from_event_payload(
    payload: Mapping[str, Any], source_events: Mapping[str, Mapping[str, Any]]
) -> ContentReference:
    if not isinstance(payload, Mapping):
        raise ValueError("score reference is invalid")
    source_id = payload.get("event_id")
    source = source_events.get(str(source_id))
    if source is not None:
        return _reference_object(source, payload)
    reference = _reference_from_dict(payload)
    if reference is None:
        raise ValueError("score reference is invalid")
    return reference


def meeting_from_events(events: Sequence[Mapping[str, Any]]) -> Meeting:
    """Build ordered V5 stream chunks from a v2 event sequence."""
    ordered = sorted(events, key=lambda item: int(item.get("seq", 0)))
    source_events = {
        str(event.get("event_id")): event
        for event in ordered
        if event.get("kind") in SOURCE_KINDS and event.get("event_id")
    }
    chunks: List[StreamChunkDict] = []
    for event in ordered:
        kind = event.get("kind")
        if kind not in {"screen", "chat", "score"}:
            continue
        timestamp = _datetime(event.get("captured_at"), "event captured_at")
        seq = _integer(event.get("seq"), "event sequence")
        event_id = str(event.get("event_id", ""))
        if kind == "screen":
            content = event.get("content", "")
            if not isinstance(content, str):
                raise ValueError("screen content must be text")
            capture = ScreenCapture(content, timestamp, event_id, seq, _hash(content))
            stream: StreamChunk = ScreenStreamChunk([capture], "screen", "workspace")
            name = "screen"
        elif kind == "chat":
            content = event.get("content", "")
            if not isinstance(content, str):
                raise ValueError("chat content must be text")
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            raw_references = data.get("references") if isinstance(data.get("references"), list) else []
            if not raw_references and isinstance(data.get("reference"), Mapping):
                raw_references = [data["reference"]]
            references: List[ContentReference] = []
            for raw_reference in raw_references:
                # Older v2 journals may contain the pre-V3 rectangle shape.
                # Keep those chat messages readable while retaining strict
                # validation for new score references below.
                try:
                    if isinstance(raw_reference, Mapping):
                        references.append(_reference_from_event_payload(raw_reference, source_events))
                except ValueError:
                    continue
            message = ChatMessage(
                content,
                str(event.get("actor", "")),
                timestamp,
                references[0] if references else None,
                event_id,
                seq,
            )
            stream = ChatStreamChunk("chat", [message])
            name = "chat"
        else:
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            tag = data.get("tag")
            judge_name = data.get("judge_name")
            if not isinstance(tag, str) or tag not in SCORE_TAGS:
                raise ValueError("score event has an invalid tag")
            if not isinstance(judge_name, str) or not judge_name.strip():
                raise ValueError("score event has an invalid judge name")
            raw_reference = data.get("reference")
            if not isinstance(raw_reference, Mapping):
                raise ValueError("score event requires a reference")
            reference = _reference_from_event_payload(raw_reference, source_events)
            score_value = data.get("score")
            comment = data.get("comment")
            if comment is not None and not isinstance(comment, str):
                raise ValueError("score event has an invalid comment")
            score_message = ScoreMessage(
                tag,
                score_value if score_value is None else float(score_value),
                judge_name,
                timestamp,
                reference,
                event_id,
                seq,
                comment,
            )
            stream = ScoreStreamChunk([score_message])
            name = "score"
        chunks.append(StreamChunkDict({name: stream}, timestamp, timestamp))
    return Meeting(chunks)


__all__ = [
    "ChatMessage",
    "ChatReference",
    "ChatStreamChunk",
    "ContentReference",
    "Meeting",
    "SCORE_TAGS",
    "VALID_SCORE_TAGS",
    "ScoreMessage",
    "ScoreStreamChunk",
    "ScreenCapture",
    "ScreenReference",
    "ScreenStreamChunk",
    "StreamChunk",
    "StreamChunkDict",
    "TextRegion",
    "meeting_from_events",
    "reference_from_event",
    "to_dict",
]
