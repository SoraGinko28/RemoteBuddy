"""Meeting ADT - the capability-layer definition of RemoteBuddy's meeting model.

Design intent (finalized in MEETING_ADT.md; this module is its executable spec):
- The ADT only describes the capability layer; this module is the single source of the storage schema, sliced into each type,
  with no separate schema file.
- Data-layer machinery (replication/snapshot/dedup/indexing) is outside the ADT; assess it on the spot in the concrete goal only when actually needed.
- Once frozen, data is immutable; increments only append, never mutate in place.
- Type changes must stay consistent with the ADT document; the document leads.

Stream order is fixed: screen/chat/score/terminal_control (shared by replay and validation).
Keystrokes are not a stream: they live in the harness and are never persisted.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Optional

# Stream names: the ADT's four streams, order fixed; do not add, remove, or reorder them.
SCREEN_STREAM_NAME = "screen"
CHAT_STREAM_NAME = "chat"
SCORE_STREAM_NAME = "score"
TERMINAL_CONTROL_STREAM_NAME = "terminal_control"

STREAM_NAMES: Final[tuple[str, ...]] = (
    SCREEN_STREAM_NAME,
    CHAT_STREAM_NAME,
    SCORE_STREAM_NAME,
    TERMINAL_CONTROL_STREAM_NAME,
)

__all__ = [
    "SCREEN_STREAM_NAME",
    "CHAT_STREAM_NAME",
    "SCORE_STREAM_NAME",
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
# UserName := Oneof | $agent_name str | $human ()
# Storage/JSON representation: an agent is stored as its name string; human as the reserved "$human" sentinel.
# "$human" is reserved: no agent name may equal it (is_human is the predicate).

HUMAN_USER_NAME: Final[str] = "$human"


def is_human(user_name: str) -> bool:
    """Whether the UserName is the $human sentinel (agent names never are)."""
    return user_name == HUMAN_USER_NAME


# AgentRole := Oneof | $generator | $active_discriminator | $passive_discriminator
# Storage/JSON representation: enum name -> "$"-prefixed string, one-to-one with the role map JSON.
class AgentRole(Enum):
    GENERATOR = "$generator"
    ACTIVE_DISCRIMINATOR = "$active_discriminator"
    PASSIVE_DISCRIMINATOR = "$passive_discriminator"


def agent_role_from_name(role_name: str) -> AgentRole:
    """Rebuild an AgentRole from a JSON string; unknown or non-string input raises ValueError."""
    return AgentRole(role_name)


# =====================================================================
# G3: ScreenCapture (screen-stream record; pure field layer)
# =====================================================================
#
# ScreenCapture := DataClass
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
#   frame_rows(s) := s.split('\n')
#   row_plain(row) := row.rstrip(' ')
#   concat_text_field_of_list(list) := ''.join(seg.text for seg in list)
#   plain_of(list) := '\n'.join(row_plain(r)
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
    """ADT Appendix: seg_attrs(seg) := (seg.fg, seg.bg, frozenset(seg.styles))"""
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
# Style := Oneof | "bold" | "underline" | "italic" | "reverse" | "blink" | "dim"
# Color := Oneof | $palette int(0..255) | $truecolor Rgb
# Rgb   := DataClass r/g/b int each in 0..255
# TerminalTextSegment :=
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


# Color := Oneof | $palette int | $truecolor Rgb
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
# ChatMessage := DataClass
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
#   {"user_name": <str>, "time": <ISO8601>, "message": [part_json, ...]}
#   part_json := {"raw": str} | {"at_user_name": str} | {"ref": {...G8}}
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


ChatMessagePart = RawMessage | AtUserMessage


@dataclass
class ChatMessage:
    user_name: str
    time: _dt.datetime
    message: list[ChatMessagePart] = field(default_factory=list)


@dataclass
class ChatStreamChunk:
    stream_name: str = CHAT_STREAM_NAME
    chunks: list[ChatMessage] = field(default_factory=list)


def chat_part_to_dict(part: ChatMessagePart) -> dict:
    if isinstance(part, RawMessage):
        return {"raw": part.text}
    if isinstance(part, AtUserMessage):
        return {"at_user_name": part.user_name}
    raise TypeError(f"unknown chat part: {type(part).__name__}")


def chat_part_from_dict(payload: dict) -> ChatMessagePart:
    if "raw" in payload:
        return RawMessage(text=payload["raw"])
    if "at_user_name" in payload:
        return AtUserMessage(user_name=payload["at_user_name"])
    raise ValueError(f"unknown chat part json: {payload!r}")


def chat_message_to_dict(message: ChatMessage) -> dict:
    return {
        "user_name": message.user_name,
        "time": _iso_format(message.time),
        "message": [chat_part_to_dict(p) for p in message.message],
    }


def chat_message_from_dict(payload: dict) -> ChatMessage:
    try:
        user_name = payload["user_name"]
        time = _iso_parse(payload["time"])
        parts = [chat_part_from_dict(p) for p in payload["message"]]
    except KeyError as exc:
        raise ValueError(f"chat message missing key {exc.args[0]}: {payload!r}") from exc
    return ChatMessage(user_name=user_name, time=time, message=parts)


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
# TextRegion := DataClass $start int $end int
# ScreenReference := $capture_time datetime $stream_name str $region TextRegion
# ChatReference   := $msg_time datetime $user_name UserName $region TextRegion
# ScoreReference  := $judge_time datetime $judge_name UserName
# ContentReference := Oneof | $screen_ref | $chat_ref | $score_ref
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


ContentReference = ScreenReference | ChatReference | ScoreReference


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
# ScoreMessage := DataClass
#   * $tag str
#   * $score Optional[float]
#   * $comment Optional[str]
#   * $judge_name UserName
#   * $time datetime
#   * $reference ContentReference
#   * Appendix[{
#       summon_chained := role($judge_name) != $passive_discriminator
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
    if role != AgentRole.PASSIVE_DISCRIMINATOR:
        return True  # generator / active / non-agent (e.g. human) never needs a summon
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
# G10: Keystroke (harness-internal, never persisted) / TerminalControl
# =====================================================================
#
# KeystrokeStr := Oneof | $text str | $ctrl str | $rest ($millisecond int)
# Keystroke := DataClass
#   * $data KeystrokeStr
#   * $user_name UserName
#   * $stroke_time datetime
# TerminalControl := DataClass
#   * $tmux_window_name str
#   * $tmux_pane int
#   * $user_name UserName
#   * $start_time datetime
#   * Assert[{ user_name == $human or role(user_name)==$generator }]   # terminal_capable
#   * Appendix[{ owner_at(t) := the user_name of the control interval containing t; intervals are delimited by start_time }]
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
            owner = control.user_name
        else:
            break
    return owner


def terminal_capable(user_name: str, role_of: object) -> bool:
    """terminal_capable := { $human() } ∪ { a | role(a)==$generator }"""
    if is_human(user_name):
        return True
    role = role_of(user_name) if callable(role_of) else None
    return role == AgentRole.GENERATOR


def assert_terminal_control_capable(control: TerminalControl, role_of: object) -> None:
    """Assert[{ user_name == $human or role==$generator }]"""
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
# G11: screen chunk containers + MeetingData (the meeting record proper)
# =====================================================================
#
# ScreenStreamChunk := DataClass
#   * $stream_name str
#   * $owner_user_name UserName
#   * $chunks list[ScreenCapture]
#
# MeetingData := DataClass
#   * $meeting_uid str
#   * $meeting_name str
#   * $sorted_stream_chunks list[StreamChunk]   # time-ordered within and across streams
#   * $agent_name2role dict[str, AgentRole]
#   * Appendix[{
#       user_names := every UserName appearing across all chunks
#                      (screen.owner / chat.user_name / score.judge_name /
#                       terminal_control.user_name)
#     }]
#   * Assert[{ keys($agent_name2role) ⊆ user_names(this record) }]
#
# Storage design (meeting manifest):
#   - agent_name2role only grows: added on join, fixed once frozen; never modified in place.
#   - Frozen = the meeting record is read-only; increments only append to events.jsonl.
#   - Manifest JSON (stream chunks are told apart by stream_name):
#     {"meeting_uid":..., "meeting_name":..., "agent_name2role":{name: "$generator"...},
#      "sorted_stream_chunks":[{stream_name:"screen",...}|{stream_name:"chat",...}|...]}

@dataclass
class ScreenStreamChunk:
    stream_name: str = SCREEN_STREAM_NAME
    owner_user_name: str = ""
    chunks: list[ScreenCapture] = field(default_factory=list)


StreamChunk = (ScreenStreamChunk | ChatStreamChunk | ScoreStreamChunk
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
    return terminal_control_stream_chunk_from_dict(payload)


@dataclass
class MeetingData:
    meeting_uid: str = ""
    meeting_name: str = ""
    agent_name2role: dict = field(default_factory=dict)  # dict[str, AgentRole]
    sorted_stream_chunks: list = field(default_factory=list)  # list[StreamChunk]


def user_names_of_meeting_data(data: MeetingData) -> set[str]:
    """Appendix user_names: collect every UserName that appears in the streams."""
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
            names.update(c.user_name for c in chunk.chunks)
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


