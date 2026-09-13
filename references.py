"""Immutable content references quoting earlier journal events.

A reference selects one prior ``chat`` or ``screen`` event by id plus a
character region, and serializes with the excerpt and a source hash so the
quote stays verifiable even if the viewer has no journal access. The event
journal remains the durable source of truth; this module is a small,
dependency-free validation/projection layer shared by the web service, the
runner, and the storage layer.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple, Union


SOURCE_KINDS = frozenset({"screen", "chat", "terminal_control"})
# A reference kind names the ADT record; the journal stores them under slightly
# different event kinds.
SOURCE_EVENT_KINDS = {"screen": "screen", "chat": "chat", "terminal_control": "control"}


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


@dataclass(frozen=True)
class TerminalControlReference:
    """A quote of one terminal-control line.

    The line is written with the record, so it can be quoted like a message:
    the region is always the whole line, and the excerpt is that text.
    ``user_name`` is empty when the record says nobody holds the pane.
    """

    start_time: datetime
    user_name: str
    region: TextRegion
    event_id: str = ""
    seq: int = 0
    source_sha256: str = ""
    excerpt: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_time", _datetime(self.start_time, "start_time"))
        if not isinstance(self.user_name, str):
            raise ValueError("terminal control reference user name must be text")
        if not isinstance(self.region, TextRegion):
            raise ValueError("terminal control reference region must be a TextRegion")
        if self.event_id and not isinstance(self.event_id, str):
            raise ValueError("terminal control reference event id must be text")
        if self.seq and (isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 1):
            raise ValueError("terminal control reference sequence must be positive")


ContentReference = Union[ScreenReference, ChatReference, TerminalControlReference]


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
    if event.get("kind") != SOURCE_EVENT_KINDS[kind]:
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
        raise ValueError("reference kind must be screen, chat, or terminal_control")
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
    if kind == "terminal_control":
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        if data.get("status") != "terminal_control":
            raise ValueError("reference source event is not a terminal control record")
        owner = data.get("user_name")
        return TerminalControlReference(
            _datetime(data.get("start_time"), "start_time"),
            owner if isinstance(owner, str) else "",
            region,
            event_id,
            seq,
            digest,
            excerpt,
        )
    return ChatReference(timestamp, str(event.get("actor", "")), region, event_id, seq, digest, "chat", excerpt)


def reference_from_event(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a client reference and return its serialized form."""
    reference = _reference_object(event, payload)
    return {"kind": str(payload["kind"]), **to_dict(reference)}


__all__ = [
    "ChatReference",
    "ContentReference",
    "ScreenReference",
    "TerminalControlReference",
    "TextRegion",
    "reference_from_event",
    "to_dict",
]
