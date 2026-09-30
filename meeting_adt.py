"""Meeting ADT - the capability-layer definition of RemoteBuddy's meeting model.

Design intent (finalized in MEETING_ADT.md; this module is its executable spec):
- The ADT only describes the capability layer; this module is the single source of the storage schema, sliced into each type,
  with no separate schema file.
- Data-layer machinery (replication/snapshot/dedup/indexing) is outside the ADT; assess it on the spot in the concrete goal only when actually needed.
- Once frozen, data is immutable; increments only append, never mutate in place.
- Type changes must stay consistent with the ADT document; the document leads.

Stream order is fixed: screen/chat/score/agent_cmd/terminal_control (shared by replay and validation).
Keystrokes are not a stream: they live in the harness and are never persisted.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Final, Generic, Optional, TypeVar

# Stream names: the ADT's streams, order fixed; do not add, remove, or reorder them.
SCREEN_STREAM_NAME = "screen"
CHAT_STREAM_NAME = "chat"
SCORE_STREAM_NAME = "score"
AGENT_CMD_STREAM_NAME = "agent_cmd"
TERMINAL_CONTROL_STREAM_NAME = "terminal_control"

STREAM_NAMES: Final[tuple[str, ...]] = (
    SCREEN_STREAM_NAME,
    CHAT_STREAM_NAME,
    SCORE_STREAM_NAME,
    AGENT_CMD_STREAM_NAME,
    TERMINAL_CONTROL_STREAM_NAME,
)

__all__ = [
    "SCREEN_STREAM_NAME",
    "CHAT_STREAM_NAME",
    "SCORE_STREAM_NAME",
    "AGENT_CMD_STREAM_NAME",
    "TERMINAL_CONTROL_STREAM_NAME",
    "STREAM_NAMES",
    "HUMAN_USER_NAME",
    "is_human",
    "AgentRole",
    "agent_role_from_name",
]


# =====================================================================
# G2: UserName / AgentRole (record-layer naming)
# =====================================================================
#
# UserName = Oneof | $agent_name str | $human ()
# Storage/JSON representation: an agent is stored as its name string; human as the reserved "$human" sentinel.
# "$human" is reserved: no agent name may equal it (is_human is the predicate).

HUMAN_USER_NAME: Final[str] = "$human"


def is_human(user_name: str) -> bool:
    """Whether the UserName is the $human sentinel (agent names never are)."""
    return user_name == HUMAN_USER_NAME


# AgentRole = Oneof | $actor | $assistant | $critic
# Storage/JSON representation: enum name -> "$"-prefixed string, one-to-one with the role map JSON.
class AgentRole(Enum):
    ACTOR = "$actor"
    ASSISTANT = "$assistant"
    CRITIC = "$critic"


def agent_role_from_name(role_name: str) -> AgentRole:
    """Rebuild an AgentRole from a JSON string; unknown or non-string input raises ValueError."""
    return AgentRole(role_name)


# =====================================================================
# G3: ScreenCapture (screen-stream record; pure field layer)
# =====================================================================
#
# ScreenCapture = DataClass
#   * $screen_data str                   # plain text; rows right-trimmed of trailing spaces
#   * $terminal_text_content list[TerminalTextSegment]   # styled segments; defined in G6
#   * $cursor_x / $cursor_y int          # cell coordinates, 0-based
#   * $screen_width / $screen_height int
#   * $captured_at datetime
#
# This goal only defines fields (invariants are implemented in G4/G5 as assert functions, not baked into the dataclass).

@dataclass
class ScreenCapture:
    screen_data: str = ""
    terminal_text_content: "list[TerminalTextSegment]" = field(default_factory=list)
    cursor_x: int = 0
    cursor_y: int = 0
    screen_width: int = 0
    screen_height: int = 0
    captured_at: Optional[_dt.datetime] = None


# =====================================================================
# G4: ScreenCapture assertion #1 -- plain_of(segments) == screen_data
# =====================================================================
#
# ADT Appendix helpers (copied verbatim from MEETING_ADT.md; names unchanged):
#   frame_rows(s) = s.split('\n')
#   row_plain(row) = row.rstrip(' ')
#   concat_text_field_of_list(list) = ''.join(seg.text for seg in list)
#   plain_of(list) = '\n'.join(row_plain(r)
#                               for r in frame_rows(concat_text_field_of_list(list)))
#
# Assert functions share one signature: take a record, raise AssertionError on violation (locating it via captured_at),
# and silently return None on success -- feeding the violation manifest collected by the G13 validator.

def concat_text_field_of_list(segments: "list[TerminalTextSegment]") -> str:
    return "".join(seg.text for seg in segments)


def frame_rows(text: str) -> "list[str]":
    return text.split("\n")


def row_plain(row: str) -> str:
    return row.rstrip(" ")


def plain_of(segments: "list[TerminalTextSegment]") -> str:
    return "\n".join(row_plain(r) for r in frame_rows(concat_text_field_of_list(segments)))


def assert_screen_plain_matches(capture: ScreenCapture) -> None:
    """Assert[{ plain_of($terminal_text_content) == $screen_data }]"""
    if plain_of(capture.terminal_text_content) != capture.screen_data:
        raise AssertionError(
            f"plain_of(terminal_text_content) != screen_data @ {capture.captured_at}: "
            f"segments => {plain_of(capture.terminal_text_content)!r}, "
            f"screen_data => {capture.screen_data!r}"
        )


# =====================================================================
# G5: ScreenCapture assertions #2-#5 (width / row count / cursor in bounds / segment validity)
# =====================================================================

import unicodedata as _ucd  # noqa: E402
import wcwidth as _wc  # noqa: E402


def _char_cell_width(ch: str) -> int:
    """Cells a single character occupies: CJK (W/F) = 2; control/zero-width = 0; otherwise 1."""
    if _ucd.category(ch) in ("Cc", "Cf", "Mn", "Me", "Zl", "Zp", "Cs"):
        return 0
    w = _wc.wcwidth(ch)
    return w if w > 0 else 1 if w == 1 else 0


def cell_width(row: str) -> int:
    """Terminal display width, in cells."""
    return sum(_char_cell_width(ch) for ch in row)


def seg_attrs(segment: "TerminalTextSegment") -> tuple:
    """ADT Appendix: seg_attrs(seg) = (seg.fg, seg.bg, frozenset(seg.styles))"""
    return (segment.fg, segment.bg, frozenset(segment.styles))


def assert_screen_row_widths_fit(capture: ScreenCapture) -> None:
    """Assert[{ all(cell_width(r) <= $screen_width
                     for r in frame_rows(concat_text_field_of_list($terminal_text_content))) }]"""
    text = concat_text_field_of_list(capture.terminal_text_content)
    for row in frame_rows(text):
        if cell_width(row) > capture.screen_width:
            raise AssertionError(
                f"row {row!r} (width {cell_width(row)}) exceeds screen_width "
                f"{capture.screen_width} @ {capture.captured_at}"
            )


def assert_screen_rows_match_height(capture: ScreenCapture) -> None:
    """Assert[{ len(frame_rows($screen_data)) == $screen_height }]"""
    rows = len(frame_rows(capture.screen_data))
    if rows != capture.screen_height:
        raise AssertionError(
            f"screen_data rows {rows} != screen_height {capture.screen_height} "
            f"@ {capture.captured_at}"
        )


def assert_cursor_in_bounds(capture: ScreenCapture) -> None:
    """Assert[{ 0 <= $cursor_y < $screen_height and 0 <= $cursor_x < $screen_width }]"""
    if not (0 <= capture.cursor_y < capture.screen_height
            and 0 <= capture.cursor_x < capture.screen_width):
        raise AssertionError(
            f"cursor ({capture.cursor_x},{capture.cursor_y}) out of bounds "
            f"({capture.screen_width}x{capture.screen_height}) @ {capture.captured_at}"
        )


def assert_segments_valid(capture: ScreenCapture) -> None:
    """Segment texts are non-empty, and adjacent segments differ in attributes.

    Assert[{ all(len(seg.text) > 0 for seg in $terminal_text_content) }]
    Assert[{ ∀ i: seg_attrs($terminal_text_content[i])
                  != seg_attrs($terminal_text_content[i + 1]) }]
    """
    segs = capture.terminal_text_content
    for idx, seg in enumerate(segs):
        if not seg.text:
            raise AssertionError(f"empty segment at index {idx} @ {capture.captured_at}")
        if idx > 0 and seg_attrs(seg) == seg_attrs(segs[idx - 1]):
            raise AssertionError(
                f"adjacent segments {idx - 1}/{idx} share attrs {seg_attrs(seg)} "
                f"@ {capture.captured_at}"
            )


__all__ += [
    "cell_width",
    "seg_attrs",
    "assert_screen_row_widths_fit",
    "assert_screen_rows_match_height",
    "assert_cursor_in_bounds",
    "assert_segments_valid",
]


# =====================================================================
# G6: TerminalTextSegment / Color / Rgb / Style (screen style layer)
# =====================================================================
#
# Style = Oneof | "bold" | "underline" | "italic" | "reverse" | "blink" | "dim"
# Color = Oneof | $palette int(0..255) | $truecolor Rgb
# Rgb   = DataClass r/g/b int each in 0..255
# TerminalTextSegment =
#     DataClass
#   * $text str                    # consecutive text sharing identical attributes; may contain \n
#   * $fg / $bg Optional[Color]    # None = terminal default foreground/background
#   * $styles set[Style]           # semantically a set; order-independent
#
# Storage/JSON representation: Style = string; Rgb = {"r":..,"g":..,"b":..};
# palette color = {"palette":n}; truecolor color = {"r":..,"g":..,"b":..}.

# Style values (order matches the ADT document)
STYLE_BOLD = "bold"
STYLE_UNDERLINE = "underline"
STYLE_ITALIC = "italic"
STYLE_REVERSE = "reverse"
STYLE_BLINK = "blink"
STYLE_DIM = "dim"

STYLE_NAMES: Final[frozenset[str]] = frozenset({
    STYLE_BOLD,
    STYLE_UNDERLINE,
    STYLE_ITALIC,
    STYLE_REVERSE,
    STYLE_BLINK,
    STYLE_DIM,
})


@dataclass(frozen=True)
class Rgb:
    """Truecolor."""
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for name, value in (("r", self.r), ("g", self.g), ("b", self.b)):
            if not 0 <= value <= 255:
                raise ValueError(f"Rgb.{name} out of range: {value}")


@dataclass(frozen=True)
class PaletteColor:
    """SGR palette index, 0..255."""
    index: int

    def __post_init__(self) -> None:
        if not 0 <= self.index <= 255:
            raise ValueError(f"palette index out of range: {self.index}")


# Color = Oneof | $palette int | $truecolor Rgb
Color = PaletteColor | Rgb


@dataclass(frozen=True)
class TerminalTextSegment:
    text: str
    fg: Optional[Color] = None
    bg: Optional[Color] = None
    styles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = set(self.styles) - STYLE_NAMES
        if unknown:
            raise ValueError(f"unknown styles: {sorted(unknown)}")
        for attr in ("fg", "bg"):
            color = getattr(self, attr)
            if color is not None and not isinstance(color, (PaletteColor, Rgb)):
                raise TypeError(f"{attr} must be PaletteColor|Rgb|None, got {type(color).__name__}")


__all__ += [
    "ImageAttachment",
    "image_attachment_to_dict",
    "image_attachment_from_dict",
    "STYLE_BOLD",
    "STYLE_UNDERLINE",
    "STYLE_ITALIC",
    "STYLE_REVERSE",
    "STYLE_BLINK",
    "STYLE_DIM",
    "STYLE_NAMES",
    "Rgb",
    "PaletteColor",
    "TerminalTextSegment",
]


# =====================================================================
# G7: ChatMessage / ChatStreamChunk (chat stream)
# =====================================================================
#
# ChatMessage = DataClass
#   * $message list[$raw_msg str | $at_user_name UserName | $reference ContentReference]
#   * $user_name UserName
#   * $time datetime
#
# Persistence mapping for real-journal chat events (actor/content/captured_at):
#   - user_name: actor == "human" -> "$human"; otherwise the actor is the agent name.
#   - time: captured_at (UTC, with a "Z" suffix, ISO8601).
#   - message: the journal only carries plain-text content today -> [{"raw": content}];
#     @-mention / reference elements gain {"at_user_name" | "ref"} in G8.
#   - phase/message_type is runtime noise, not part of the ADT, and is not persisted on ChatMessage.
#
# ChatMessage persisted JSON:
#   {"user_name": <str>, "time": <ISO8601>, "message": [part_json, ...],
#    "attachments": [ImageAttachment_json, ...]}   (attachments omitted when empty)
#   part_json = {"raw": str} | {"at_user_name": str} | {"ref": {...G8}}
#   ImageAttachment_json = {"name": str, "media_type": str, "sha256": str, "byte_count": int}
#   The picture itself is a file of the meeting (attachments/<name>), not a copy in the journal.
# ChatStreamChunk persisted JSON:
#   {"stream_name": "chat", "chunks": [ChatMessage_json, ...]}

def _iso_parse(text: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso_format(moment: _dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RawMessage:
    """$raw_msg str."""
    text: str


@dataclass(frozen=True)
class AtUserMessage:
    """$at_user_name UserName."""
    user_name: str


@dataclass(frozen=True)
class ImageAttachment:
    """$image_msg ImageAttachment — 随消息一起交出去的图片。

    图本身是这场会存下来的一份文件，落在会议目录 `attachments/<name>` 下；这里只记它的
    名字和内容指纹，不记它从哪儿来。
    """
    name: str
    media_type: str
    sha256: str = ""
    byte_count: int = 0


@dataclass
class ChatMessage:
    user_name: str
    time: _dt.datetime
    message: list[ChatMessagePart] = field(default_factory=list)
    attachments: list[ImageAttachment] = field(default_factory=list)


def image_attachment_to_dict(attachment: ImageAttachment) -> dict:
    return {
        "name": attachment.name,
        "media_type": attachment.media_type,
        "sha256": attachment.sha256,
        "byte_count": attachment.byte_count,
    }


def image_attachment_from_dict(payload: dict) -> ImageAttachment:
    try:
        return ImageAttachment(
            name=payload["name"],
            media_type=payload["media_type"],
            sha256=payload.get("sha256", ""),
            byte_count=int(payload.get("byte_count", 0) or 0),
        )
    except KeyError as exc:
        raise ValueError(f"image attachment missing key {exc.args[0]}: {payload!r}") from exc


@dataclass
class ChatStreamChunk:
    stream_name: str = CHAT_STREAM_NAME
    chunks: list[ChatMessage] = field(default_factory=list)


def chat_part_to_dict(part: ChatMessagePart) -> dict:
    if isinstance(part, RawMessage):
        return {"raw": part.text}
    if isinstance(part, AtUserMessage):
        return {"at_user_name": part.user_name}
    if isinstance(part, _REFERENCE_TYPES):
        return {"ref": content_reference_to_dict(part)}
    raise TypeError(f"unknown chat part: {type(part).__name__}")


def chat_part_from_dict(payload: dict) -> ChatMessagePart:
    if "raw" in payload:
        return RawMessage(text=payload["raw"])
    if "at_user_name" in payload:
        return AtUserMessage(user_name=payload["at_user_name"])
    if "ref" in payload:
        return content_reference_from_dict(payload["ref"])
    raise ValueError(f"unknown chat part json: {payload!r}")


def chat_message_to_dict(message: ChatMessage) -> dict:
    payload = {
        "user_name": message.user_name,
        "time": _iso_format(message.time),
        "message": [chat_part_to_dict(p) for p in message.message],
    }
    if message.attachments:
        payload["attachments"] = [image_attachment_to_dict(a) for a in message.attachments]
    return payload


def chat_message_from_dict(payload: dict) -> ChatMessage:
    try:
        user_name = payload["user_name"]
        time = _iso_parse(payload["time"])
        parts = [chat_part_from_dict(p) for p in payload["message"]]
    except KeyError as exc:
        raise ValueError(f"chat message missing key {exc.args[0]}: {payload!r}") from exc
    attachments = [image_attachment_from_dict(item)
                   for item in payload.get("attachments") or []]
    return ChatMessage(user_name=user_name, time=time, message=parts, attachments=attachments)


def chat_stream_chunk_to_dict(chunk: ChatStreamChunk) -> dict:
    return {"stream_name": chunk.stream_name, "chunks": [chat_message_to_dict(m) for m in chunk.chunks]}


def chat_stream_chunk_from_dict(payload: dict) -> ChatStreamChunk:
    if payload.get("stream_name") != CHAT_STREAM_NAME:
        raise ValueError(f"not a chat chunk: {payload.get('stream_name')!r}")
    return ChatStreamChunk(
        stream_name=payload["stream_name"],
        chunks=[chat_message_from_dict(m) for m in payload["chunks"]],
    )


# =====================================================================
# G8: the three ContentReference variants + resolve()
# =====================================================================
#
# TextRegion = DataClass $start int $end int
# ScreenReference = $capture_time datetime $stream_name str $region TextRegion
# ChatReference   = $msg_time datetime $user_name UserName $region TextRegion
# ScoreReference  = $judge_time datetime $judge_name UserName
# ContentReference = Oneof | $screen_ref | $chat_ref | $score_ref
#
# Storage design: references store keys only (time + user/stream), never a copy of the referenced content;
# resolve() looks the key up in the same meeting's streams; no match or several matches both count as a dangling reference (raised, for validation to collect).
#
# Persisted JSON (the Oneof is disambiguated by a single-key envelope):
#   {"screen_ref": {"capture_time": iso, "stream_name": s, "region": {start,end}}}
#   {"chat_ref":   {"msg_time": iso, "user_name": u, "region": {start,end}}}
#   {"score_ref":  {"judge_time": iso, "judge_name": u}}

@dataclass(frozen=True)
class TextRegion:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid region [{self.start},{self.end})")


@dataclass(frozen=True)
class ScreenReference:
    capture_time: _dt.datetime
    stream_name: str
    region: TextRegion


@dataclass(frozen=True)
class ChatReference:
    msg_time: _dt.datetime
    user_name: str
    region: TextRegion


@dataclass(frozen=True)
class ScoreReference:
    judge_time: _dt.datetime
    judge_name: str


@dataclass(frozen=True)
class TerminalControlReference:
    """$start_time datetime * $user_name Optional[UserName] * $region TextRegion

    An empty user_name = nobody took over after that hand-back. Persistence goes
    through the references.py set; this is only the ADT's mirror.
    """

    start_time: Optional[_dt.datetime]
    user_name: Optional[str] = None
    region: Optional[TextRegion] = None


@dataclass(frozen=True)
class VersionedFileReference:
    """$file_id FileId * $version_time datetime -- the latest version at or
    before that moment."""

    file_id: "FileId"
    version_time: Optional[_dt.datetime] = None


@dataclass(frozen=True)
class VersionedFileContentReference:
    """$versioned_file_ref VersionedFileReference * $region TextRegion"""

    versioned_file_ref: VersionedFileReference
    region: Optional[TextRegion] = None


ContentReference = (ScreenReference | ChatReference | ScoreReference
                    | TerminalControlReference | VersionedFileReference
                    | VersionedFileContentReference)

# a part of $message can be a reference (the six of G8)
_REFERENCE_TYPES = (ScreenReference, ChatReference, ScoreReference,
                    TerminalControlReference, VersionedFileReference,
                    VersionedFileContentReference)
ChatMessagePart = RawMessage | AtUserMessage | ContentReference


def text_region_to_dict(region: TextRegion) -> dict:
    return {"start": region.start, "end": region.end}


def text_region_from_dict(payload: dict) -> TextRegion:
    return TextRegion(start=payload["start"], end=payload["end"])


def content_reference_to_dict(reference: ContentReference) -> dict:
    if isinstance(reference, ScreenReference):
        return {"screen_ref": {
            "capture_time": _iso_format(reference.capture_time),
            "stream_name": reference.stream_name,
            "region": text_region_to_dict(reference.region),
        }}
    if isinstance(reference, ChatReference):
        return {"chat_ref": {
            "msg_time": _iso_format(reference.msg_time),
            "user_name": reference.user_name,
            "region": text_region_to_dict(reference.region),
        }}
    if isinstance(reference, ScoreReference):
        return {"score_ref": {
            "judge_time": _iso_format(reference.judge_time),
            "judge_name": reference.judge_name,
        }}
    if isinstance(reference, TerminalControlReference):
        return {"terminal_control_ref": {
            "start_time": _iso_format(reference.start_time) if reference.start_time else None,
            "user_name": reference.user_name,
            "region": text_region_to_dict(reference.region) if reference.region else None,
        }}
    if isinstance(reference, VersionedFileReference):
        return {"versioned_file_ref": {
            "file_id": file_id_to_dict(reference.file_id),
            "version_time": _iso_format(reference.version_time) if reference.version_time else None,
        }}
    if isinstance(reference, VersionedFileContentReference):
        return {"versioned_file_content_ref": {
            "versioned_file_ref": content_reference_to_dict(reference.versioned_file_ref),
            "region": text_region_to_dict(reference.region) if reference.region else None,
        }}
    raise TypeError(f"unknown content reference: {type(reference).__name__}")


def content_reference_from_dict(payload: dict) -> ContentReference:
    if "screen_ref" in payload:
        inner = payload["screen_ref"]
        return ScreenReference(
            capture_time=_iso_parse(inner["capture_time"]),
            stream_name=inner["stream_name"],
            region=text_region_from_dict(inner["region"]),
        )
    if "chat_ref" in payload:
        inner = payload["chat_ref"]
        return ChatReference(
            msg_time=_iso_parse(inner["msg_time"]),
            user_name=inner["user_name"],
            region=text_region_from_dict(inner["region"]),
        )
    if "score_ref" in payload:
        inner = payload["score_ref"]
        return ScoreReference(
            judge_time=_iso_parse(inner["judge_time"]),
            judge_name=inner["judge_name"],
        )
    if "terminal_control_ref" in payload:
        inner = payload["terminal_control_ref"]
        start = inner.get("start_time")
        region = inner.get("region")
        return TerminalControlReference(
            start_time=_iso_parse(start) if isinstance(start, str) else None,
            user_name=inner.get("user_name"),
            region=text_region_from_dict(region) if region else None,
        )
    if "versioned_file_ref" in payload:
        inner = payload["versioned_file_ref"]
        moment = inner.get("version_time")
        return VersionedFileReference(
            file_id=file_id_from_dict(inner["file_id"]),
            version_time=_iso_parse(moment) if isinstance(moment, str) else None,
        )
    if "versioned_file_content_ref" in payload:
        inner = payload["versioned_file_content_ref"]
        region = inner.get("region")
        return VersionedFileContentReference(
            versioned_file_ref=content_reference_from_dict(inner["versioned_file_ref"]),
            region=text_region_from_dict(region) if region else None,
        )
    raise ValueError(f"unknown content reference json: {payload!r}")


def _resolve_matches(matches: list, what: str) -> object:
    if not matches:
        raise LookupError(f"dangling reference: no {what} found")
    if len(matches) > 1:
        raise ValueError(f"ambiguous reference: {len(matches)} {what} matched")
    return matches[0]


def resolve_screen_reference(reference: ScreenReference,
                             screen_captures: "list[ScreenCapture]") -> ScreenCapture:
    """ScreenReference.resolve: captured_at == capture_time."""
    matches = [c for c in screen_captures if c.captured_at == reference.capture_time]
    return _resolve_matches(matches, "screen capture")  # type: ignore[return-value]


def resolve_chat_reference(reference: ChatReference,
                           chat_messages: "list[ChatMessage]") -> ChatMessage:
    """ChatReference.resolve: match time == msg_time and user_name == the referenced user."""
    matches = [m for m in chat_messages
               if m.time == reference.msg_time and m.user_name == reference.user_name]
    return _resolve_matches(matches, "chat message")  # type: ignore[return-value]


def resolve_score_reference(reference: ScoreReference,
                            score_messages: "list[ScoreMessage]") -> "ScoreMessage":
    """ScoreReference.resolve: match judge_name and time == judge_time (ScoreMessage is defined in G9)."""
    matches = [m for m in score_messages
               if m.judge_name == reference.judge_name and m.time == reference.judge_time]
    return _resolve_matches(matches, "score message")  # type: ignore[return-value]


def resolve_content_reference(reference: ContentReference,
                              screen_captures: "list[ScreenCapture]",
                              chat_messages: "list[ChatMessage]",
                              score_messages: "list[ScoreMessage]") -> object:
    if isinstance(reference, ScreenReference):
        return resolve_screen_reference(reference, screen_captures)
    if isinstance(reference, ChatReference):
        return resolve_chat_reference(reference, chat_messages)
    if isinstance(reference, ScoreReference):
        return resolve_score_reference(reference, score_messages)
    raise TypeError(f"unknown content reference: {type(reference).__name__}")


__all__ += [
    "TextRegion",
    "ScreenReference",
    "ChatReference",
    "ScoreReference",
    "TerminalControlReference",
    "VersionedFileReference",
    "VersionedFileContentReference",
    "text_region_to_dict",
    "text_region_from_dict",
    "content_reference_to_dict",
    "content_reference_from_dict",
    "resolve_screen_reference",
    "resolve_chat_reference",
    "resolve_score_reference",
    "resolve_content_reference",
]


# =====================================================================
# G9: ScoreMessage / ScoreStreamChunk (score stream)
# =====================================================================
#
# ScoreMessage = DataClass
#   * $tag str
#   * $score Optional[float]
#   * $comment Optional[str]
#   * $judge_name UserName
#   * $time datetime
#   * $reference ContentReference
#   * Appendix[{
#       summon_chained = role($judge_name) != $critic
#                         or the message's $reference resolves to a message that summoned it
#     }]
#   * Assert[{ summon_chained }]
#
# The real journal has no score events today -> the absent stream is an empty stream: ScoreStreamChunk(chunks=[]).
# Persisted JSON: {"tag": s, "score": float|null, "comment": str|null,
#             "judge_name": u, "time": iso, "reference": <ContentReference JSON>}

@dataclass
class ScoreMessage:
    tag: str
    score: Optional[float] = None
    comment: Optional[str] = None
    judge_name: str = ""
    time: Optional[_dt.datetime] = None
    reference: Optional[ContentReference] = None


@dataclass
class ScoreStreamChunk:
    stream_name: str = SCORE_STREAM_NAME
    chunks: list[ScoreMessage] = field(default_factory=list)


def score_message_to_dict(message: ScoreMessage) -> dict:
    payload: dict = {
        "tag": message.tag,
        "score": message.score,
        "comment": message.comment,
        "judge_name": message.judge_name,
        "time": _iso_format(message.time) if message.time is not None else None,
    }
    if message.reference is not None:
        payload["reference"] = content_reference_to_dict(message.reference)
    return payload


def score_message_from_dict(payload: dict) -> ScoreMessage:
    if "tag" not in payload:
        raise ValueError(f"score message missing tag: {payload!r}")
    reference = None
    if "reference" in payload and payload["reference"] is not None:
        reference = content_reference_from_dict(payload["reference"])
    time = payload.get("time")
    return ScoreMessage(
        tag=payload["tag"],
        score=payload.get("score"),
        comment=payload.get("comment"),
        judge_name=payload.get("judge_name", ""),
        time=_iso_parse(time) if isinstance(time, str) else None,
        reference=reference,
    )


def score_stream_chunk_to_dict(chunk: ScoreStreamChunk) -> dict:
    return {
        "stream_name": chunk.stream_name,
        "chunks": [score_message_to_dict(m) for m in chunk.chunks],
    }


def score_stream_chunk_from_dict(payload: dict) -> ScoreStreamChunk:
    if payload.get("stream_name") != SCORE_STREAM_NAME:
        raise ValueError(f"not a score chunk: {payload.get('stream_name')!r}")
    return ScoreStreamChunk(
        stream_name=payload["stream_name"],
        chunks=[score_message_from_dict(m) for m in payload["chunks"]],
    )


def score_summon_chained(message: ScoreMessage,
                         role_of: object,
                         referenced_message: Optional[object]) -> bool:
    """A decidable version of the Appendix's summon_chained.

    Role lookup is delegated to the caller-injected role_of (a MeetingData.agent_name2role query),
    referenced_message is the message the reference resolves to (None when there is no reference).
    passive check: chained only when the reference resolves to a summoning message that mentions @judge.
    """
    role = role_of(message.judge_name) if callable(role_of) else None
    if role != AgentRole.CRITIC:
        return True  # actor / active / non-agent (e.g. human) never needs a summon
    if referenced_message is None:
        return False
    mention = getattr(referenced_message, "message", None)
    if mention is None:
        return False
    return any(isinstance(p, AtUserMessage) and p.user_name == message.judge_name
               for p in mention)


def assert_score_summon_chained(message: ScoreMessage,
                                role_of: object,
                                referenced_message: Optional[object]) -> None:
    """Assert[{ summon_chained }]"""
    if not score_summon_chained(message, role_of, referenced_message):
        raise AssertionError(
            f"passive judge {message.judge_name!r} not summon_chained @ {message.time}: "
            f"reference resolves to no summoning mention"
        )


__all__ += [
    "ScoreMessage",
    "ScoreStreamChunk",
    "score_message_to_dict",
    "score_message_from_dict",
    "score_stream_chunk_to_dict",
    "score_stream_chunk_from_dict",
    "score_summon_chained",
    "assert_score_summon_chained",
]


# =====================================================================
# G11: AgentCmdMessage / AgentCmdStreamChunk (agent_cmd stream)
# =====================================================================
#
# AgentCmd = Oneof | $cmd str | $appliable_diff str
# AgentCmdMessage = DataClass
#   * $agent_cmd AgentCmd
#   * $agent_name UserName
#   * $produced_time datetime
#   * $effective_time Optional[datetime]
#   * $token Optional[str]
#   * Appendix[{
#       $agent_cmd is the text as handed over: $cmd is the line the Agent gave (without the
#         markers the harness adds); $appliable_diff is the diff text handed to the
#         file-update path.
#       $effective_time absent = that run never happened in the pane (refused, never typed...).
#       $token is the marker number the harness prints in front of the command
#         (the `<token>` of `cmd-remotebuddy-<token>`); a $token with no $effective_time
#         means the run failed before it was typed.
#       The diff path has no such marker: $token there is that application's number, and it
#         does the same job -- tying the raw diff to the screen and file traces it left.
#       Failed runs are kept too: the stream does not filter by success.
#     }]
#
# With the screen stream, every command's stdout and retcode are derivable from this stream:
#   for a record with $token, stdout is the text between `cmd-remotebuddy-<token>` and
#   `retcode-remotebuddy-<token>` in the screen stream, and retcode is the number on the
#   latter line (when the leading marker line fell out of the buffer, what comes out is the
#   tail) -- the same shape as TmuxCmdResult.
#
# Persisted JSON (the AgentCmd Oneof is disambiguated by a single-key envelope):
#   {"agent_cmd": {"cmd": s} | {"appliable_diff": s}, "agent_name": u,
#    "produced_time": iso, "effective_time": iso|null, "token": t|null}

@dataclass(frozen=True)
class CmdLine:
    cmd: str


@dataclass(frozen=True)
class DiffText:
    appliable_diff: str


AgentCmd = CmdLine | DiffText


@dataclass
class AgentCmdMessage:
    agent_cmd: AgentCmd
    agent_name: str
    produced_time: Optional[_dt.datetime] = None
    effective_time: Optional[_dt.datetime] = None
    token: Optional[str] = None


@dataclass
class AgentCmdStreamChunk:
    stream_name: str = AGENT_CMD_STREAM_NAME
    chunks: list[AgentCmdMessage] = field(default_factory=list)


def agent_cmd_to_dict(agent_cmd: AgentCmd) -> dict:
    if isinstance(agent_cmd, CmdLine):
        return {"cmd": agent_cmd.cmd}
    if isinstance(agent_cmd, DiffText):
        return {"appliable_diff": agent_cmd.appliable_diff}
    raise TypeError(f"unknown agent cmd: {type(agent_cmd).__name__}")


def agent_cmd_from_dict(payload: dict) -> AgentCmd:
    if not isinstance(payload, dict) or len(payload) != 1:
        raise ValueError(f"agent cmd must be a single-key envelope: {payload!r}")
    if "cmd" in payload:
        return CmdLine(cmd=payload["cmd"])
    if "appliable_diff" in payload:
        return DiffText(appliable_diff=payload["appliable_diff"])
    raise ValueError(f"unknown agent cmd branch: {payload!r}")


def agent_cmd_message_to_dict(message: AgentCmdMessage) -> dict:
    return {
        "agent_cmd": agent_cmd_to_dict(message.agent_cmd),
        "agent_name": message.agent_name,
        "produced_time": _iso_format(message.produced_time) if message.produced_time is not None else None,
        "effective_time": _iso_format(message.effective_time) if message.effective_time is not None else None,
        "token": message.token,
    }


def agent_cmd_message_from_dict(payload: dict) -> AgentCmdMessage:
    if "agent_cmd" not in payload:
        raise ValueError(f"agent cmd message missing agent_cmd: {payload!r}")
    produced = payload.get("produced_time")
    effective = payload.get("effective_time")
    token = payload.get("token")
    if token is not None and not isinstance(token, str):
        raise ValueError(f"agent cmd token must be text: {token!r}")
    return AgentCmdMessage(
        agent_cmd=agent_cmd_from_dict(payload["agent_cmd"]),
        agent_name=payload.get("agent_name", ""),
        produced_time=_iso_parse(produced) if isinstance(produced, str) else None,
        effective_time=_iso_parse(effective) if isinstance(effective, str) else None,
        token=token,
    )


def agent_cmd_stream_chunk_to_dict(chunk: AgentCmdStreamChunk) -> dict:
    return {
        "stream_name": chunk.stream_name,
        "chunks": [agent_cmd_message_to_dict(m) for m in chunk.chunks],
    }


def agent_cmd_stream_chunk_from_dict(payload: dict) -> AgentCmdStreamChunk:
    if payload.get("stream_name") != AGENT_CMD_STREAM_NAME:
        raise ValueError(f"not an agent cmd chunk: {payload.get('stream_name')!r}")
    return AgentCmdStreamChunk(
        stream_name=payload["stream_name"],
        chunks=[agent_cmd_message_from_dict(m) for m in payload["chunks"]],
    )


__all__ += [
    "AgentCmd",
    "CmdLine",
    "DiffText",
    "AgentCmdMessage",
    "AgentCmdStreamChunk",
    "agent_cmd_to_dict",
    "agent_cmd_from_dict",
    "agent_cmd_message_to_dict",
    "agent_cmd_message_from_dict",
    "agent_cmd_stream_chunk_to_dict",
    "agent_cmd_stream_chunk_from_dict",
]


# =====================================================================
# G10: Keystroke (harness-internal, never persisted) / TerminalControl
# =====================================================================
#
# KeystrokeStr = Oneof | $text str | $ctrl str | $rest ($millisecond int)
# Keystroke = DataClass
#   * $data KeystrokeStr
#   * $user_name UserName
#   * $stroke_time datetime
# TerminalControl = DataClass
#   * $tmux_window_name str
#   * $tmux_pane int
#   * $user_name UserName
#   * $start_time datetime
#   * Assert[{ user_name == $human or role(user_name)==$actor }]   # terminal_capable
#   * Appendix[{ owner_at(t) = the user_name of the control interval containing t; intervals are delimited by start_time }]
#   * Assert[{ ∀ keystroke k: owner_at(k.stroke_time) == k.user_name }]
#
# Persistence mapping for real-journal control events (actor/phase/data.status=acquired|released):
#   - The journal has no tmux_window_name/pane today -> record them via a mapping: acquired starts an interval,
#     user_name=actor (human -> $human); a release event only advances the interval and persists no record;
#     tmux names/panes come from meeting.json (filled in by the G12 loader).
#   - Keystrokes are never persisted: they stay in the harness; there is no keystroke stream and no chunk.
#
# Persisted JSON:
#   TerminalControl: {"tmux_window_name": w, "tmux_pane": p,
#                     "user_name": u, "start_time": iso}

@dataclass(frozen=True)
class TextKey:
    text: str


@dataclass(frozen=True)
class CtrlKey:
    ctrl: str


@dataclass(frozen=True)
class RestKey:
    millisecond: int


KeystrokeStr = TextKey | CtrlKey | RestKey


@dataclass
class Keystroke:
    data: KeystrokeStr
    user_name: str
    stroke_time: _dt.datetime


@dataclass
class TerminalControl:
    tmux_window_name: str = ""
    tmux_pane: int = 0
    user_name: str = ""
    start_time: Optional[_dt.datetime] = None


@dataclass
class TerminalControlStreamChunk:
    stream_name: str = TERMINAL_CONTROL_STREAM_NAME
    chunks: list[TerminalControl] = field(default_factory=list)


def keystroke_str_to_dict(key: KeystrokeStr) -> dict:
    if isinstance(key, TextKey):
        return {"text": key.text}
    if isinstance(key, CtrlKey):
        return {"ctrl": key.ctrl}
    if isinstance(key, RestKey):
        return {"rest": {"millisecond": key.millisecond}}
    raise TypeError(f"unknown keystroke str: {type(key).__name__}")


def keystroke_str_from_dict(payload: dict) -> KeystrokeStr:
    if "text" in payload:
        return TextKey(text=payload["text"])
    if "ctrl" in payload:
        return CtrlKey(ctrl=payload["ctrl"])
    if "rest" in payload:
        return RestKey(millisecond=payload["rest"]["millisecond"])
    raise ValueError(f"unknown keystroke str json: {payload!r}")


def keystroke_to_dict(key: Keystroke) -> dict:
    return {
        "data": keystroke_str_to_dict(key.data),
        "user_name": key.user_name,
        "stroke_time": _iso_format(key.stroke_time),
    }


def keystroke_from_dict(payload: dict) -> Keystroke:
    return Keystroke(
        data=keystroke_str_from_dict(payload["data"]),
        user_name=payload["user_name"],
        stroke_time=_iso_parse(payload["stroke_time"]),
    )


def terminal_control_to_dict(control: TerminalControl) -> dict:
    return {
        "tmux_window_name": control.tmux_window_name,
        "tmux_pane": control.tmux_pane,
        "user_name": control.user_name,
        "start_time": _iso_format(control.start_time) if control.start_time else None,
    }


def terminal_control_from_dict(payload: dict) -> TerminalControl:
    start = payload.get("start_time")
    return TerminalControl(
        tmux_window_name=payload.get("tmux_window_name", ""),
        tmux_pane=payload.get("tmux_pane", 0),
        user_name=payload.get("user_name", ""),
        start_time=_iso_parse(start) if isinstance(start, str) else None,
    )


def terminal_control_stream_chunk_to_dict(chunk: TerminalControlStreamChunk) -> dict:
    return {
        "stream_name": chunk.stream_name,
        "chunks": [terminal_control_to_dict(c) for c in chunk.chunks],
    }


def terminal_control_stream_chunk_from_dict(payload: dict) -> TerminalControlStreamChunk:
    if payload.get("stream_name") != TERMINAL_CONTROL_STREAM_NAME:
        raise ValueError(f"not a terminal_control chunk: {payload.get('stream_name')!r}")
    return TerminalControlStreamChunk(
        stream_name=payload["stream_name"],
        chunks=[terminal_control_from_dict(c) for c in payload["chunks"]],
    )


def owner_at(moment: _dt.datetime,
             controls: "list[TerminalControl]") -> Optional[str]:
    """Appendix owner_at(t): the user_name of the last interval whose start_time <= t;
    intervals are sorted by start_time and half-open [start, next_start); returns None when no interval covers t."""
    owner: Optional[str] = None
    for control in sorted(controls, key=lambda c: c.start_time
                          if c.start_time is not None else _dt.datetime.min):
        if control.start_time is not None and control.start_time <= moment:
            owner = control.user_name or None
        else:
            break
    return owner


def terminal_capable(user_name: str, role_of: object) -> bool:
    """terminal_capable = { $human() } ∪ { a | role(a)==$actor }"""
    if is_human(user_name):
        return True
    role = role_of(user_name) if callable(role_of) else None
    return role == AgentRole.ACTOR


def assert_terminal_control_capable(control: TerminalControl, role_of: object) -> None:
    """Assert[{ user_name == $human or role==$actor }]"""
    if not terminal_capable(control.user_name, role_of):
        raise AssertionError(
            f"terminal_control owner {control.user_name!r} not terminal_capable "
            f"@ {control.start_time}"
        )


def assert_keystroke_owner_matches(keystroke: Keystroke,
                                   controls: "list[TerminalControl]") -> None:
    """Assert[{ ∀ k: owner_at(k.stroke_time) == k.user_name }]"""
    owner = owner_at(keystroke.stroke_time, controls)
    if owner != keystroke.user_name:
        raise AssertionError(
            f"keystroke owner {keystroke.user_name!r} != owner_at {owner!r} "
            f"@ {keystroke.stroke_time}"
        )


__all__ += [
    "TextKey",
    "CtrlKey",
    "RestKey",
    "Keystroke",
    "TerminalControl",
    "TerminalControlStreamChunk",
    "keystroke_str_to_dict",
    "keystroke_str_from_dict",
    "keystroke_to_dict",
    "keystroke_from_dict",
    "terminal_control_to_dict",
    "terminal_control_from_dict",
    "terminal_control_stream_chunk_to_dict",
    "terminal_control_stream_chunk_from_dict",
    "owner_at",
    "terminal_capable",
    "assert_terminal_control_capable",
    "assert_keystroke_owner_matches",
]


# =====================================================================
# G15: file layer -- FileKvStore / FileDesktop (files, versioned parts, desktop tree)
# =====================================================================
#
# FileId = Object * $file_id str
#   * Assert[{ $file_id ∈ keys(FileKvStore.file_id2file) }]
# VersionedFile = Object * $file_name AppendOnlyList[Versioned[str]]
#   * $file_meta AppendOnlyList[Versioned[DataStatus]]
#   * $file_content AppendOnlyList[Versioned[str]]
#   (three chains: reading at a moment takes the last version on the chain with
#    time <= that moment; an empty status chain = writable)
# FileKvStore = Object * $file_id2file dict[str, VersionedFile]
# FileEntry = Object * $entry_name str * $file_id FileId
#   (the name belongs to the entry a tree holds, not to the identity: the same
#    file can hang under a workspace or inside a fold, and a rename is a new entry)
# FileFold = Object * $fold_name Optional[str] * $member_file_ids AppendOnlyList[FileEntry]
# FileWorkspace = Object * $member_files AppendOnlyList[FileFold | FileEntry]
#   * $workspace_name Optional[str] * $workspace_status Optional[DataStatus]
# FileDesktop = Object * $workspaces AppendOnlyList[FileWorkspace]
#   * $reorder_indexes Versioned[DesktopMemberOrder]
#   * Assert[{ names are unique within a level: the workspace_name are pairwise
#               different; inside one workspace, a fold's fold_name and the
#               entry_name of a file hanging directly under it are pairwise
#               different; inside one fold, the entry_name of the files are
#               pairwise different }]
#   * Assert[{ a file_id appears at most once in the tree: an entry carries a
#               name and an identity, so no two paths reach the same file }]
# DesktopMemberOrder = Object * $workspace_indexes ReadOnlyList[WorkspaceMemberOrder]
#   * Assert[{ the workspace_index are exactly a permutation of
#               0..len(workspaces)-1 }]
# WorkspaceMemberOrder = Object * $workspace_index int
#   * $file_indexes ReadOnlyList[($file_index int, Optional[FoldMemberOrder])]
#   * Assert[{ the file_index are exactly a permutation of
#               0..len(member_files)-1; every fold's member_indexes are exactly
#               a permutation of 0..len(member_file_ids)-1 }]
# FoldMemberOrder = Object * $member_indexes ReadOnlyList[$member_index int]
#
# Persisted JSON:
#   DataStatus: "deleted" | "read_only"
#   Versioned[T]: {"data": ..., "time": iso|null, "author": user_name}
#   FileId: {"file_id": id}
#   FileEntry: {"entry_name": name, "file_id": FileId}
#   VersionedFile: {"file_name": [Versioned[str]], "file_meta": [Versioned[status|null]],
#                   "file_content": [Versioned[str]]}   # one chain per part
#   FileKvStore: {"file_id2file": {file_id: VersionedFile}}
#   FileFold: {"fold_name": name|null, "member_file_ids": [FileEntry]}
#   FileWorkspace: {"member_files": [{"fold": FileFold} | {"file": FileEntry}],
#                   "workspace_name": name|null, "workspace_status": status|null}
#   FoldMemberOrder: {"member_indexes": [int]}
#   WorkspaceMemberOrder: {"workspace_index": int,
#                          "file_indexes": [[index, FoldMemberOrder|null]]}
#   DesktopMemberOrder: {"workspace_indexes": [WorkspaceMemberOrder]}
#   FileDesktop: {"workspaces": [FileWorkspace], "reorder_indexes": Versioned[DesktopMemberOrder]}


class DataStatus(Enum):
    """DataStatus = Oneof | $deleted () | $read_only ()"""

    DELETED = "deleted"
    READ_ONLY = "read_only"


def data_status_from_name(status_name: str) -> DataStatus:
    for status in DataStatus:
        if status.value == status_name:
            return status
    raise ValueError(f"unknown data status: {status_name!r}")


_T = TypeVar("_T")


@dataclass
class Versioned(Generic[_T]):
    """Versioned[T] = Object * $data T * $time datetime * $author UserName"""

    data: _T
    time: Optional[_dt.datetime] = None
    author: str = ""


def versioned_to_dict(value: Versioned, encode: Callable[[object], object]) -> dict:
    return {
        "data": encode(value.data),
        "time": _iso_format(value.time) if value.time is not None else None,
        "author": value.author,
    }


def versioned_from_dict(payload: dict, decode: Callable[[object], object]) -> Versioned:
    moment = payload.get("time")
    return Versioned(
        data=decode(payload.get("data")),
        time=_iso_parse(moment) if isinstance(moment, str) else None,
        author=payload.get("author", ""),
    )


def _str_decode(payload: object) -> str:
    return "" if payload is None else str(payload)


def _data_status_encode(status: object) -> object:
    return None if status is None else status.value


def _data_status_decode(payload: object) -> object:
    return None if payload is None else data_status_from_name(str(payload))


@dataclass
class FileId:
    """FileId = Object * $file_id str

    An identity: the store's key, and nothing else. Where a file hangs and what
    it is called there is the tree's business (FileEntry).
    """

    file_id: str = ""


def file_id_to_dict(file_id: FileId) -> dict:
    return {"file_id": file_id.file_id}


def file_id_from_dict(payload: dict) -> FileId:
    return FileId(file_id=payload.get("file_id", ""))


@dataclass
class FileEntry:
    """FileEntry = Object * $entry_name str * $file_id FileId

    One name in one place: what a workspace or a fold holds. The same file may
    hang in two places under two names, and a rename is a new entry.
    """

    entry_name: str = ""
    file_id: FileId = field(default_factory=FileId)


def file_entry_to_dict(entry: FileEntry) -> dict:
    return {"entry_name": entry.entry_name, "file_id": file_id_to_dict(entry.file_id)}


def file_entry_from_dict(payload: dict) -> FileEntry:
    payload = payload or {}
    return FileEntry(entry_name=payload.get("entry_name", ""),
                     file_id=file_id_from_dict(payload.get("file_id") or {}))


@dataclass
class VersionedFile:
    """VersionedFile = Object * $file_name AppendOnlyList[Versioned[str]]
    * $file_meta AppendOnlyList[Versioned[DataStatus]]
    * $file_content AppendOnlyList[Versioned[str]]

    Three chains, one per part. Appending a version is the only write there is:
    existing versions stay put, and reading at a moment takes the last version
    whose time is at or before that moment. An empty meta chain means writable: the
    status is something added later, writable is the default.
    """

    file_name: list = field(default_factory=list)     # list[Versioned[str]]
    file_meta: list = field(default_factory=list)     # list[Versioned[DataStatus]]
    file_content: list = field(default_factory=list)  # list[Versioned[str]]


def versioned_file_to_dict(versioned_file: VersionedFile) -> dict:
    return {
        "file_name": [versioned_to_dict(v, str) for v in versioned_file.file_name],
        "file_meta": [versioned_to_dict(v, _data_status_encode) for v in versioned_file.file_meta],
        "file_content": [versioned_to_dict(v, str) for v in versioned_file.file_content],
    }


def versioned_file_from_dict(payload: dict) -> VersionedFile:
    return VersionedFile(
        file_name=[versioned_from_dict(v, _str_decode)
                   for v in payload.get("file_name") or []],
        file_meta=[versioned_from_dict(v, _data_status_decode)
                   for v in payload.get("file_meta") or []],
        file_content=[versioned_from_dict(v, _str_decode)
                      for v in payload.get("file_content") or []],
    )


@dataclass
class FileKvStore:
    """FileKvStore = Object * $file_id2file dict[str, VersionedFile]"""

    file_id2file: dict = field(default_factory=dict)  # dict[str, VersionedFile]


def file_kv_store_to_dict(store: FileKvStore) -> dict:
    return {
        "file_id2file": {
            file_id: versioned_file_to_dict(versioned_file)
            for file_id, versioned_file in store.file_id2file.items()
        }
    }


def file_kv_store_from_dict(payload: dict) -> FileKvStore:
    payload = payload or {}
    return FileKvStore(
        file_id2file={
            file_id: versioned_file_from_dict(entry)
            for file_id, entry in (payload.get("file_id2file") or {}).items()
        }
    )


@dataclass
class FileFold:
    """FileFold = Object * $fold_name Optional[str]
    * $member_file_ids AppendOnlyList[FileEntry]"""

    fold_name: Optional[str] = None
    member_file_ids: list = field(default_factory=list)  # list[FileEntry]


def file_fold_to_dict(fold: FileFold) -> dict:
    return {
        "fold_name": fold.fold_name,
        "member_file_ids": [file_entry_to_dict(f) for f in fold.member_file_ids],
    }


def file_fold_from_dict(payload: dict) -> FileFold:
    payload = payload or {}
    return FileFold(
        fold_name=payload.get("fold_name"),
        member_file_ids=[file_entry_from_dict(f) for f in payload.get("member_file_ids", [])],
    )


@dataclass
class FileWorkspace:
    """FileWorkspace = Object * $member_files AppendOnlyList[FileFold | FileEntry]
    * $workspace_name Optional[str] * $workspace_status Optional[DataStatus]"""

    member_files: list = field(default_factory=list)  # list[FileFold | FileEntry]
    workspace_name: Optional[str] = None
    workspace_status: Optional[DataStatus] = None


def _workspace_member_to_dict(member: object) -> dict:
    if isinstance(member, FileFold):
        return {"fold": file_fold_to_dict(member)}
    return {"file": file_entry_to_dict(member)}


def _workspace_member_from_dict(payload: dict) -> object:
    if "fold" in payload:
        return file_fold_from_dict(payload["fold"])
    return file_entry_from_dict(payload["file"])


def file_workspace_to_dict(workspace: FileWorkspace) -> dict:
    return {
        "member_files": [_workspace_member_to_dict(m) for m in workspace.member_files],
        "workspace_name": workspace.workspace_name,
        "workspace_status": _data_status_encode(workspace.workspace_status),
    }


def file_workspace_from_dict(payload: dict) -> FileWorkspace:
    payload = payload or {}
    return FileWorkspace(
        member_files=[_workspace_member_from_dict(m) for m in payload.get("member_files", [])],
        workspace_name=payload.get("workspace_name"),
        workspace_status=_data_status_decode(payload.get("workspace_status")),
    )


@dataclass
class FoldMemberOrder:
    """FoldMemberOrder = Object * $member_indexes ReadOnlyList[$member_index int]"""

    member_indexes: list = field(default_factory=list)  # list[int]


def fold_member_order_to_dict(order: FoldMemberOrder) -> dict:
    return {"member_indexes": list(order.member_indexes)}


def fold_member_order_from_dict(payload: dict) -> FoldMemberOrder:
    payload = payload or {}
    return FoldMemberOrder(member_indexes=list(payload.get("member_indexes", [])))


@dataclass
class WorkspaceMemberOrder:
    """WorkspaceMemberOrder = Object * $workspace_index int
    * $file_indexes ReadOnlyList[($file_index int, Optional[FoldMemberOrder])]"""

    workspace_index: int = 0
    file_indexes: list = field(default_factory=list)  # list[tuple[int, Optional[FoldMemberOrder]]]


def workspace_member_order_to_dict(order: WorkspaceMemberOrder) -> dict:
    return {
        "workspace_index": order.workspace_index,
        "file_indexes": [
            [index, fold_member_order_to_dict(fold_order) if fold_order is not None else None]
            for index, fold_order in order.file_indexes
        ],
    }


def workspace_member_order_from_dict(payload: dict) -> WorkspaceMemberOrder:
    payload = payload or {}
    file_indexes = []
    for index, fold_order in payload.get("file_indexes", []):
        file_indexes.append(
            (index, fold_member_order_from_dict(fold_order) if fold_order is not None else None)
        )
    return WorkspaceMemberOrder(
        workspace_index=payload.get("workspace_index", 0), file_indexes=file_indexes
    )


@dataclass
class DesktopMemberOrder:
    """DesktopMemberOrder = Object * $workspace_indexes ReadOnlyList[WorkspaceMemberOrder]"""

    workspace_indexes: list = field(default_factory=list)  # list[WorkspaceMemberOrder]


def desktop_member_order_to_dict(order: DesktopMemberOrder) -> dict:
    return {"workspace_indexes": [workspace_member_order_to_dict(o) for o in order.workspace_indexes]}


def desktop_member_order_from_dict(payload: dict) -> DesktopMemberOrder:
    payload = payload or {}
    return DesktopMemberOrder(
        workspace_indexes=[
            workspace_member_order_from_dict(o) for o in payload.get("workspace_indexes", [])
        ]
    )


def identity_workspace_order(workspace: FileWorkspace, workspace_index: int = 0) -> WorkspaceMemberOrder:
    """The identity order of one workspace: every member in place, folds expanded."""
    file_indexes = []
    for file_index, member in enumerate(workspace.member_files):
        fold_order = (
            FoldMemberOrder(member_indexes=list(range(len(member.member_file_ids))))
            if isinstance(member, FileFold)
            else None
        )
        file_indexes.append((file_index, fold_order))
    return WorkspaceMemberOrder(workspace_index=workspace_index, file_indexes=file_indexes)


def identity_desktop_order(desktop: FileDesktop) -> DesktopMemberOrder:
    """The identity order of a whole desktop: workspaces in place, members in place."""
    return DesktopMemberOrder(
        workspace_indexes=[
            identity_workspace_order(workspace, workspace_index)
            for workspace_index, workspace in enumerate(desktop.workspaces)
        ]
    )


@dataclass
class FileDesktop:
    """FileDesktop = Object * $workspaces AppendOnlyList[FileWorkspace]
    * $reorder_indexes Versioned[DesktopMemberOrder]"""

    workspaces: list = field(default_factory=list)  # list[FileWorkspace]
    reorder_indexes: Versioned = field(
        default_factory=lambda: Versioned(DesktopMemberOrder())
    )


def file_desktop_to_dict(desktop: FileDesktop) -> dict:
    return {
        "workspaces": [file_workspace_to_dict(w) for w in desktop.workspaces],
        "reorder_indexes": versioned_to_dict(
            desktop.reorder_indexes, desktop_member_order_to_dict
        ),
    }


def file_desktop_from_dict(payload: dict) -> FileDesktop:
    payload = payload or {}
    return FileDesktop(
        workspaces=[file_workspace_from_dict(w) for w in payload.get("workspaces", [])],
        reorder_indexes=versioned_from_dict(
            payload.get("reorder_indexes") or {}, desktop_member_order_from_dict
        ),
    )


def file_ids_of_desktop(desktop: FileDesktop) -> list[FileId]:
    """Every FileId the desktop tree carries: direct entries and fold entries."""
    found: list[FileId] = []
    for workspace in desktop.workspaces:
        for member in workspace.member_files:
            entries = member.member_file_ids if isinstance(member, FileFold) else [member]
            found.extend(entry.file_id for entry in entries)
    return found


def assert_file_id_in_store(file_id: FileId, store: FileKvStore) -> None:
    """Assert[{ $file_id ∈ keys(FileKvStore.file_id2file) }]"""
    if file_id.file_id not in store.file_id2file:
        raise AssertionError(
            f"file id {file_id.file_id!r} not in FileKvStore.file_id2file"
        )


def _workspace_member_names(workspace: FileWorkspace) -> list[tuple[str, str]]:
    """(kind, name) for every named member directly under a workspace."""
    named: list[tuple[str, str]] = []
    for member in workspace.member_files:
        if isinstance(member, FileFold):
            if member.fold_name is not None:
                named.append(("fold", member.fold_name))
        elif member.entry_name:
            named.append(("file", member.entry_name))
    return named


def assert_file_desktop_distinct_file_ids(desktop: FileDesktop) -> None:
    """Assert: a $file_id appears at most once in the tree.

    A path picks a file by the names along it; with no links in the tree, no two
    paths may come out at the same file.
    """
    ids = [file_id.file_id for file_id in file_ids_of_desktop(desktop)]
    duplicates = sorted({f for f in ids if ids.count(f) > 1})
    if duplicates:
        raise AssertionError(f"one file id under two paths: {duplicates}")


def assert_file_desktop_unique_names(desktop: FileDesktop) -> None:
    """Assert: names are unique on each level, so a path resolves to one entry."""
    workspace_names = [w.workspace_name for w in desktop.workspaces if w.workspace_name]
    duplicates = sorted({n for n in workspace_names if workspace_names.count(n) > 1})
    if duplicates:
        raise AssertionError(f"duplicate workspace names: {duplicates}")
    for workspace in desktop.workspaces:
        direct = _workspace_member_names(workspace)
        names = [name for _, name in direct]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise AssertionError(
                f"duplicate names under workspace {workspace.workspace_name!r}: {duplicates}"
            )
        for member in workspace.member_files:
            if not isinstance(member, FileFold):
                continue
            entry_names = [e.entry_name for e in member.member_file_ids if e.entry_name]
            duplicates = sorted({n for n in entry_names if entry_names.count(n) > 1})
            if duplicates:
                raise AssertionError(
                    f"duplicate entry names under fold {member.fold_name!r}: {duplicates}"
                )


def assert_file_desktop_member_order(desktop: FileDesktop) -> None:
    """Asserts on DesktopMemberOrder / WorkspaceMemberOrder / FoldMemberOrder."""
    order = desktop.reorder_indexes.data
    indexes = [o.workspace_index for o in order.workspace_indexes]
    if sorted(indexes) != list(range(len(desktop.workspaces))) or len(set(indexes)) != len(indexes):
        raise AssertionError(
            f"workspace_indexes is not a permutation of 0..{len(desktop.workspaces) - 1}: {indexes}"
        )
    seen: set[int] = set()
    for workspace_order in order.workspace_indexes:
        index = workspace_order.workspace_index
        if index in seen:
            raise AssertionError(f"workspace_index {index} appears twice")
        seen.add(index)
        workspace = desktop.workspaces[index]
        file_indexes = [i for i, _ in workspace_order.file_indexes]
        if (sorted(file_indexes) != list(range(len(workspace.member_files)))
                or len(set(file_indexes)) != len(file_indexes)):
            raise AssertionError(
                f"file_indexes of workspace {index} is not a permutation of "
                f"0..{len(workspace.member_files) - 1}: {file_indexes}"
            )
        for file_index, fold_order in workspace_order.file_indexes:
            if fold_order is None:
                continue
            member = workspace.member_files[file_index]
            if not isinstance(member, FileFold):
                raise AssertionError(
                    f"workspace {index}: member {file_index} carries a fold order but is not a fold"
                )
            member_indexes = list(fold_order.member_indexes)
            if (sorted(member_indexes) != list(range(len(member.member_file_ids)))
                    or len(set(member_indexes)) != len(member_indexes)):
                raise AssertionError(
                    f"member_indexes of fold {member.fold_name!r} is not a permutation of "
                    f"0..{len(member.member_file_ids) - 1}: {member_indexes}"
                )


__all__ += [
    "DataStatus",
    "data_status_from_name",
    "Versioned",
    "versioned_to_dict",
    "versioned_from_dict",
    "FileId",
    "file_id_to_dict",
    "file_id_from_dict",
    "FileEntry",
    "file_entry_to_dict",
    "file_entry_from_dict",
    "VersionedFile",
    "versioned_file_to_dict",
    "versioned_file_from_dict",
    "FileKvStore",
    "file_kv_store_to_dict",
    "file_kv_store_from_dict",
    "FileFold",
    "file_fold_to_dict",
    "file_fold_from_dict",
    "FileWorkspace",
    "file_workspace_to_dict",
    "file_workspace_from_dict",
    "FoldMemberOrder",
    "fold_member_order_to_dict",
    "fold_member_order_from_dict",
    "WorkspaceMemberOrder",
    "workspace_member_order_to_dict",
    "workspace_member_order_from_dict",
    "DesktopMemberOrder",
    "desktop_member_order_to_dict",
    "desktop_member_order_from_dict",
    "identity_workspace_order",
    "identity_desktop_order",
    "FileDesktop",
    "file_desktop_to_dict",
    "file_desktop_from_dict",
    "file_ids_of_desktop",
    "assert_file_id_in_store",
    "assert_file_desktop_unique_names",
    "assert_file_desktop_distinct_file_ids",
    "assert_file_desktop_member_order",
]


# =====================================================================
# G11: screen chunk containers + MeetingData (the meeting record proper)
# =====================================================================
#
# ScreenStreamChunk = DataClass
#   * $stream_name str
#   * $owner_user_name UserName
#   * $chunks list[ScreenCapture]
#
# MeetingData = DataClass
#   * $meeting_uid str
#   * $meeting_name str
#   * $sorted_stream_chunks list[StreamChunk]   # time-ordered within and across streams
#   * $agent_name2role dict[str, AgentRole]
#   * Appendix[{
#       user_names = every UserName appearing across all chunks
#                      (screen.owner / chat.user_name / score.judge_name /
#                       terminal_control.user_name)
#     }]
#   * Assert[{ keys($agent_name2role) ⊆ user_names(this record) }]
#
# Storage design (meeting manifest):
#   - agent_name2role only grows: added on join, fixed once frozen; never modified in place.
#   - Frozen = the meeting record is read-only; increments only append to events.jsonl.
#   - Manifest JSON (stream chunks are told apart by stream_name):
#     {"meeting_uid":..., "meeting_name":..., "agent_name2role":{name: "$actor"...},
#      "sorted_stream_chunks":[{stream_name:"screen",...}|{stream_name:"chat",...}|...]}

@dataclass
class ScreenStreamChunk:
    stream_name: str = SCREEN_STREAM_NAME
    owner_user_name: str = ""
    chunks: list[ScreenCapture] = field(default_factory=list)


StreamChunk = (ScreenStreamChunk | ChatStreamChunk | ScoreStreamChunk | AgentCmdStreamChunk
               | TerminalControlStreamChunk)


def _color_to_dict(color: Optional[Color]) -> Optional[dict]:
    if color is None:
        return None
    if isinstance(color, PaletteColor):
        return {"palette": color.index}
    return {"r": color.r, "g": color.g, "b": color.b}


def _color_from_dict(payload: Optional[dict]) -> Optional[Color]:
    if payload is None:
        return None
    if "palette" in payload:
        return PaletteColor(payload["palette"])
    return Rgb(payload["r"], payload["g"], payload["b"])


def terminal_text_segment_to_dict(segment: TerminalTextSegment) -> dict:
    return {
        "text": segment.text,
        "fg": _color_to_dict(segment.fg),
        "bg": _color_to_dict(segment.bg),
        "styles": sorted(segment.styles),
    }


def terminal_text_segment_from_dict(payload: dict) -> TerminalTextSegment:
    return TerminalTextSegment(
        text=payload["text"],
        fg=_color_from_dict(payload.get("fg")),
        bg=_color_from_dict(payload.get("bg")),
        styles=frozenset(payload.get("styles", [])),
    )


def screen_capture_to_dict(capture: ScreenCapture) -> dict:
    return {
        "screen_data": capture.screen_data,
        "terminal_text_content": [
            terminal_text_segment_to_dict(s) for s in capture.terminal_text_content
        ],
        "cursor_x": capture.cursor_x,
        "cursor_y": capture.cursor_y,
        "screen_width": capture.screen_width,
        "screen_height": capture.screen_height,
        "captured_at": _iso_format(capture.captured_at) if capture.captured_at else None,
    }


def screen_capture_from_dict(payload: dict) -> ScreenCapture:
    captured_at = payload.get("captured_at")
    return ScreenCapture(
        screen_data=payload.get("screen_data", ""),
        terminal_text_content=[
            terminal_text_segment_from_dict(s) for s in payload.get("terminal_text_content", [])
        ],
        cursor_x=payload.get("cursor_x", 0),
        cursor_y=payload.get("cursor_y", 0),
        screen_width=payload.get("screen_width", 0),
        screen_height=payload.get("screen_height", 0),
        captured_at=_iso_parse(captured_at) if isinstance(captured_at, str) else None,
    )


def screen_stream_chunk_to_dict(chunk: ScreenStreamChunk) -> dict:
    return {
        "stream_name": chunk.stream_name,
        "owner_user_name": chunk.owner_user_name,
        "chunks": [screen_capture_to_dict(c) for c in chunk.chunks],
    }


def screen_stream_chunk_from_dict(payload: dict) -> ScreenStreamChunk:
    if payload.get("stream_name") != SCREEN_STREAM_NAME:
        raise ValueError(f"not a screen chunk: {payload.get('stream_name')!r}")
    return ScreenStreamChunk(
        stream_name=payload["stream_name"],
        owner_user_name=payload.get("owner_user_name", ""),
        chunks=[screen_capture_from_dict(c) for c in payload.get("chunks", [])],
    )


def stream_chunk_to_dict(chunk: StreamChunk) -> dict:
    if isinstance(chunk, ScreenStreamChunk):
        return screen_stream_chunk_to_dict(chunk)
    if isinstance(chunk, ChatStreamChunk):
        return chat_stream_chunk_to_dict(chunk)
    if isinstance(chunk, ScoreStreamChunk):
        return score_stream_chunk_to_dict(chunk)
    if isinstance(chunk, AgentCmdStreamChunk):
        return agent_cmd_stream_chunk_to_dict(chunk)
    if isinstance(chunk, TerminalControlStreamChunk):
        return terminal_control_stream_chunk_to_dict(chunk)
    raise TypeError(f"unknown stream chunk: {type(chunk).__name__}")


_STREAM_CHUNK_NAMES = frozenset(STREAM_NAMES)


def stream_chunk_from_dict(payload: dict) -> StreamChunk:
    stream_name = payload.get("stream_name")
    if stream_name not in _STREAM_CHUNK_NAMES:
        raise ValueError(f"unknown stream_name {stream_name!r}")
    if stream_name == SCREEN_STREAM_NAME:
        return screen_stream_chunk_from_dict(payload)
    if stream_name == CHAT_STREAM_NAME:
        return chat_stream_chunk_from_dict(payload)
    if stream_name == SCORE_STREAM_NAME:
        return score_stream_chunk_from_dict(payload)
    if stream_name == AGENT_CMD_STREAM_NAME:
        return agent_cmd_stream_chunk_from_dict(payload)
    return terminal_control_stream_chunk_from_dict(payload)


@dataclass
class MeetingData:
    meeting_uid: str = ""
    meeting_name: str = ""
    agent_name2role: dict = field(default_factory=dict)  # dict[str, AgentRole]
    sorted_stream_chunks: list = field(default_factory=list)  # list[StreamChunk]
    file_kv_store: FileKvStore = field(default_factory=FileKvStore)
    file_desktop: FileDesktop = field(default_factory=FileDesktop)


def _file_layer_versioned(data: MeetingData):
    """Every Versioned value the file layer carries (each has an author).

    The three file parts are chains now, so every version on them counts.
    """
    for versioned_file in data.file_kv_store.file_id2file.values():
        yield from versioned_file.file_name
        yield from versioned_file.file_meta
        yield from versioned_file.file_content
    yield data.file_desktop.reorder_indexes


def user_names_of_meeting_data(data: MeetingData) -> set[str]:
    """Appendix user_names: every UserName in the streams plus every file-layer author."""
    names: set[str] = set()
    for chunk in data.sorted_stream_chunks:
        if isinstance(chunk, ScreenStreamChunk):
            if chunk.owner_user_name:
                names.add(chunk.owner_user_name)
        elif isinstance(chunk, ChatStreamChunk):
            names.update(m.user_name for m in chunk.chunks)
        elif isinstance(chunk, ScoreStreamChunk):
            names.update(m.judge_name for m in chunk.chunks)
        elif isinstance(chunk, TerminalControlStreamChunk):
            names.update(c.user_name for c in chunk.chunks if c.user_name)
    names.update(v.author for v in _file_layer_versioned(data) if v.author)
    return names


def assert_role_map_keys_in_user_names(data: MeetingData) -> None:
    """Assert[{ keys($agent_name2role) ⊆ user_names(this record) }]"""
    user_names = user_names_of_meeting_data(data)
    missing = sorted(set(data.agent_name2role) - user_names)
    if missing:
        raise AssertionError(
            f"role map keys not in user_names: {missing}; "
            f"user_names={sorted(user_names)}"
        )


def meeting_data_to_dict(data: MeetingData) -> dict:
    return {
        "meeting_uid": data.meeting_uid,
        "meeting_name": data.meeting_name,
        "agent_name2role": {
            name: role.value for name, role in data.agent_name2role.items()
        },
        "sorted_stream_chunks": [stream_chunk_to_dict(c) for c in data.sorted_stream_chunks],
        "file_kv_store": file_kv_store_to_dict(data.file_kv_store),
        "file_desktop": file_desktop_to_dict(data.file_desktop),
    }


def meeting_data_from_dict(payload: dict) -> MeetingData:
    try:
        uid = payload["meeting_uid"]
        name = payload["meeting_name"]
        roles = payload["agent_name2role"]
        chunks = [stream_chunk_from_dict(c) for c in payload["sorted_stream_chunks"]]
    except KeyError as exc:
        raise ValueError(f"meeting data missing key {exc.args[0]}") from exc
    return MeetingData(
        meeting_uid=uid,
        meeting_name=name,
        agent_name2role={n: agent_role_from_name(v) for n, v in roles.items()},
        sorted_stream_chunks=chunks,
        file_kv_store=file_kv_store_from_dict(payload.get("file_kv_store") or {}),
        file_desktop=file_desktop_from_dict(payload.get("file_desktop") or {}),
    )


__all__ += [
    "ScreenStreamChunk",
    "StreamChunk",
    "terminal_text_segment_to_dict",
    "terminal_text_segment_from_dict",
    "screen_capture_to_dict",
    "screen_capture_from_dict",
    "screen_stream_chunk_to_dict",
    "screen_stream_chunk_from_dict",
    "stream_chunk_to_dict",
    "stream_chunk_from_dict",
    "MeetingData",
    "user_names_of_meeting_data",
    "assert_role_map_keys_in_user_names",
    "meeting_data_to_dict",
    "meeting_data_from_dict",
]






__all__ += [
    "RawMessage",
    "AtUserMessage",
    "ChatMessage",
    "ChatStreamChunk",
    "chat_part_to_dict",
    "chat_part_from_dict",
    "chat_message_to_dict",
    "chat_message_from_dict",
    "chat_stream_chunk_to_dict",
    "chat_stream_chunk_from_dict",
]





__all__ += [
    "concat_text_field_of_list",
    "frame_rows",
    "row_plain",
    "plain_of",
    "assert_screen_plain_matches",
]



__all__ += [
    "ScreenCapture",
]


# ----------------------------------------------------------------------
# Building reference values (the make_* group of the data interface)
# ----------------------------------------------------------------------
#
# These take no ctx and never touch a record: they are the values a caller
# passes in as arguments. They live here because they build this module's own
# records; the data interface's chapter declares their signatures.


def make_text_region(start: int, end: int) -> "TextRegion":
    return TextRegion(start=start, end=end)


def make_screen_reference(capture_time, stream_name: str,
                          region: "TextRegion") -> "ScreenReference":
    return ScreenReference(capture_time=capture_time, stream_name=stream_name, region=region)


def make_chat_reference(msg_time, user_name: str, region: "TextRegion") -> "ChatReference":
    return ChatReference(msg_time=msg_time, user_name=user_name, region=region)


def make_score_reference(judge_time, judge_name: str) -> "ScoreReference":
    return ScoreReference(judge_time=judge_time, judge_name=judge_name)


def make_terminal_control_reference(start_time, user_name, region: "TextRegion"):
    return TerminalControlReference(start_time=start_time, user_name=user_name, region=region)


def make_versioned_file_reference(file_id: "FileId", version_time) -> "VersionedFileReference":
    return VersionedFileReference(file_id=file_id, version_time=version_time)


def make_versioned_file_content_reference(versioned_file_ref: "VersionedFileReference",
                                          region: "TextRegion"):
    return VersionedFileContentReference(versioned_file_ref=versioned_file_ref, region=region)
