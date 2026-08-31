"""Typed V6 projection for RemoteBuddy meeting streams.

V6 keeps chat content as an ordered list of text and content references. A
score is a separate stream item and may carry an optional human comment. The
event journal used by the web service remains the durable source of truth;
this module is a small, dependency-free projection/validation layer for
callers that work with the V6 shape directly.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


SCORE_TAGS = frozenset({"competent", "incompetent"})
VALID_SCORE_TAGS = SCORE_TAGS
SOURCE_KINDS = frozenset({"screen", "chat"})


def _datetime(value: Any, label: str = "time") -> datetime:
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


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    capture_time: datetime
    stream_name: str
    region: TextRegion
    event_id: str = ""
    seq: int = 0
    source_sha256: str = ""
    excerpt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "capture_time", _datetime(self.capture_time, "capture_time"))
        if not isinstance(self.stream_name, str) or not self.stream_name.strip():
            raise ValueError("screen reference stream name is required")
        if not isinstance(self.region, TextRegion):
            raise ValueError("screen reference region must be a TextRegion")
        if self.event_id and not isinstance(self.event_id, str):
            raise ValueError("screen reference event id must be text")
        if self.seq and (isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1):
            raise ValueError("screen reference sequence must be positive")


@dataclass(frozen=True)
class ChatReference:
    msg_time: datetime
    user_name: str
    region: TextRegion
    event_id: str = ""
    seq: int = 0
    source_sha256: str = ""
    stream_name: str = "chat"
    excerpt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "msg_time", _datetime(self.msg_time, "msg_time"))
        if not isinstance(self.user_name, str):
            raise ValueError("chat reference user name must be text")
        if not isinstance(self.region, TextRegion):
            raise ValueError("chat reference region must be a TextRegion")
        if not isinstance(self.stream_name, str) or not self.stream_name.strip():
            raise ValueError("chat reference stream name is required")
        if self.event_id and not isinstance(self.event_id, str):
            raise ValueError("chat reference event id must be text")
        if self.seq and (isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1):
            raise ValueError("chat reference sequence must be positive")


ContentReference = Union[ScreenReference, ChatReference]
ChatPart = Union[str, ContentReference]


@dataclass(frozen=True)
class ScreenCapture:
    screen_data: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.screen_data, str):
            raise ValueError("screen data must be text")
        object.__setattr__(self, "captured_at", _datetime(self.captured_at, "captured_at"))


@dataclass(frozen=True)
class ChatMessage:
    message: List[ChatPart]
    user_name: str
    time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.message, (list, tuple)):
            raise ValueError("chat message must be a list")
        parts = list(self.message)
        if not parts:
            raise ValueError("chat message must contain at least one part")
        if not all(isinstance(item, str) or isinstance(item, (ScreenReference, ChatReference)) for item in parts):
            raise ValueError("chat message parts must be text or content references")
        object.__setattr__(self, "message", parts)
        if not isinstance(self.user_name, str):
            raise ValueError("chat user name must be text")
        object.__setattr__(self, "time", _datetime(self.time, "time"))


@dataclass(frozen=True)
class ScoreMessage:
    tag: str
    score: Optional[float]
    comment: Optional[str]
    judge_name: str
    time: datetime
    reference: ContentReference

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or self.tag not in SCORE_TAGS:
            raise ValueError("score tag must be competent or incompetent")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise ValueError("score must be a finite number or null")
            if not math.isfinite(float(self.score)):
                raise ValueError("score must be a finite number or null")
            object.__setattr__(self, "score", float(self.score))
        if self.comment is not None:
            if not isinstance(self.comment, str):
                raise ValueError("score comment must be text or null")
            object.__setattr__(self, "comment", self.comment.strip() or None)
        if not isinstance(self.judge_name, str) or not self.judge_name.strip():
            raise ValueError("judge name must be non-empty")
        object.__setattr__(self, "judge_name", self.judge_name.strip())
        object.__setattr__(self, "time", _datetime(self.time, "time"))
        if not isinstance(self.reference, (ScreenReference, ChatReference)):
            raise ValueError("score reference must be a screen or chat reference")


@dataclass(frozen=True)
class ScreenStreamChunk:
    chunks: List[ScreenCapture]
    stream_name: str
    owner_user_name: str = "workspace"

    def __post_init__(self) -> None:
        if isinstance(self.chunks, str) and isinstance(self.stream_name, (list, tuple)):
            chunks, stream_name = self.chunks, self.stream_name
            object.__setattr__(self, "chunks", list(stream_name))
            object.__setattr__(self, "stream_name", chunks)
        if not isinstance(self.chunks, list):
            raise ValueError("screen chunks must be a list")
        if not isinstance(self.stream_name, str) or not self.stream_name:
            raise ValueError("screen stream name is required")
        if not isinstance(self.owner_user_name, str):
            raise ValueError("screen owner name must be text")


@dataclass(frozen=True)
class ChatStreamChunk:
    stream_name: str
    chunks: List[ChatMessage]

    def __post_init__(self) -> None:
        if isinstance(self.stream_name, (list, tuple)) and isinstance(self.chunks, str):
            stream_name, chunks = self.stream_name, self.chunks
            object.__setattr__(self, "stream_name", chunks)
            object.__setattr__(self, "chunks", list(stream_name))
        if not isinstance(self.stream_name, str) or not self.stream_name:
            raise ValueError("chat stream name is required")
        if not isinstance(self.chunks, list):
            raise ValueError("chat chunks must be a list")


@dataclass(frozen=True)
class ScoreStreamChunk:
    chunks: List[ScoreMessage]
    stream_name: str = "score"

    def __post_init__(self) -> None:
        if isinstance(self.chunks, str) and isinstance(self.stream_name, (list, tuple)):
            chunks, stream_name = self.chunks, self.stream_name
            object.__setattr__(self, "chunks", list(stream_name))
            object.__setattr__(self, "stream_name", chunks)
        if self.stream_name != "score":
            raise ValueError("score stream name must be score")
        if not isinstance(self.chunks, list):
            raise ValueError("score chunks must be a list")


StreamChunk = Union[ScreenStreamChunk, ChatStreamChunk, ScoreStreamChunk]


@dataclass(frozen=True)
class StreamChunkDict:
    stream_name2stream_chunk: Dict[str, StreamChunk]
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.stream_name2stream_chunk, dict):
            raise ValueError("stream chunk map must be a dictionary")
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
    if event.get("kind") != kind:
        raise ValueError("reference source event is invalid")
    event_id = event.get("event_id")
    seq = event.get("seq")
    content = event.get("content", "")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("reference source event id is required")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("source sequence must be positive")
    if not isinstance(content, str):
        raise ValueError("reference source content must be text")
    return event_id, seq, _datetime(event.get("captured_at"), "source captured_at"), content


def _reference_object(event: Mapping[str, Any], payload: Mapping[str, Any]) -> ContentReference:
    kind = payload.get("kind")
    if kind not in SOURCE_KINDS:
        raise ValueError("reference kind must be screen or chat")
    event_id, seq, timestamp, content = _source(event, str(kind))
    if payload.get("event_id") != event_id:
        raise ValueError("reference event does not match the selected source")
    raw_region = payload.get("region")
    if not isinstance(raw_region, Mapping) or set(raw_region) != {"start", "end"}:
        raise ValueError("reference region must contain start and end")
    region = TextRegion(_integer(raw_region["start"], "text region start"), _integer(raw_region["end"], "text region end"))
    chars = list(content)
    if region.end > len(chars):
        raise ValueError("reference is outside the source content")
    excerpt = "".join(chars[region.start : region.end])
    if not excerpt.strip():
        raise ValueError("reference cannot be blank")
    digest = _hash(content)
    if kind == "screen":
        return ScreenReference(timestamp, "screen", region, event_id, seq, digest, excerpt)
    return ChatReference(timestamp, str(event.get("actor", "")), region, event_id, seq, digest, "chat", excerpt)


def reference_from_event(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a client reference and return a serialized V6 reference."""
    reference = _reference_object(event, payload)
    return {"kind": str(payload["kind"]), **to_dict(reference)}


def _reference_from_payload(
    payload: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]]
) -> ContentReference:
    source = sources.get(str(payload.get("event_id")))
    if source is not None:
        return _reference_object(source, payload)
    kind = payload.get("kind")
    region = payload.get("region")
    if kind not in SOURCE_KINDS or not isinstance(region, Mapping):
        raise ValueError("reference is invalid")
    try:
        parsed_region = TextRegion(region["start"], region["end"])
        if kind == "screen":
            return ScreenReference(
                payload.get("capture_time"),
                str(payload.get("stream_name") or "screen"),
                parsed_region,
                str(payload.get("event_id") or ""),
                int(payload.get("seq", 0)),
                str(payload.get("source_sha256") or ""),
                str(payload.get("excerpt") or ""),
            )
        return ChatReference(
            payload.get("msg_time"),
            str(payload.get("user_name") or ""),
            parsed_region,
            str(payload.get("event_id") or ""),
            int(payload.get("seq", 0)),
            str(payload.get("source_sha256") or ""),
            str(payload.get("stream_name") or "chat"),
            str(payload.get("excerpt") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("reference is invalid") from exc


def meeting_from_events(events: Sequence[Mapping[str, Any]]) -> Meeting:
    """Build ordered V6 stream chunks from event-journal records."""
    ordered = sorted(events, key=lambda item: int(item.get("seq", 0)))
    sources = {
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
        if kind == "screen":
            capture = ScreenCapture(str(event.get("content", "")), timestamp)
            stream: StreamChunk = ScreenStreamChunk([capture], "screen", "workspace")
            name = "screen"
        elif kind == "chat":
            content = event.get("content", "")
            if not isinstance(content, str) or not content:
                raise ValueError("chat content must be text")
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            raw_refs = data.get("references") if isinstance(data.get("references"), list) else []
            if not raw_refs and isinstance(data.get("reference"), Mapping):
                raw_refs = [data["reference"]]
            refs: List[ContentReference] = []
            for raw in raw_refs:
                if isinstance(raw, Mapping):
                    try:
                        refs.append(_reference_from_payload(raw, sources))
                    except ValueError:
                        continue
            stream = ChatStreamChunk(
                "chat", [ChatMessage([content, *refs], str(event.get("actor", "")), timestamp)]
            )
            name = "chat"
        else:
            data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
            raw_reference = data.get("reference")
            if not isinstance(raw_reference, Mapping):
                raise ValueError("score event requires a reference")
            reference = _reference_from_payload(raw_reference, sources)
            stream = ScoreStreamChunk(
                [
                    ScoreMessage(
                        str(data.get("tag")),
                        data.get("score"),
                        data.get("comment"),
                        str(data.get("judge_name", "")),
                        timestamp,
                        reference,
                    )
                ]
            )
            name = "score"
        chunks.append(StreamChunkDict({name: stream}, timestamp, timestamp))
    return Meeting(chunks)


__all__ = [
    "ChatMessage", "ChatReference", "ChatStreamChunk", "ContentReference",
    "Meeting", "SCORE_TAGS", "VALID_SCORE_TAGS", "ScoreMessage",
    "ScoreStreamChunk", "ScreenCapture", "ScreenReference",
    "ScreenStreamChunk", "StreamChunk", "StreamChunkDict", "TextRegion",
    "meeting_from_events", "reference_from_event", "to_dict",
]
