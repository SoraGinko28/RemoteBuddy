"""Meeting ADT — RemoteBuddy 会议模型的能力层定义。

设计初衷（定稿于 MEETING_ADT.md，本模块是它的可执行规格）：
- ADT 只描述能力层；本模块是存储 schema 的唯一源，存储按切片融进各类型，
  不另设 schema 文件。
- 数据层（复制/快照/去重/索引）不在 ADT；确有需要时在具体目标内现场评估。
- 冻结后不可变；增量只 append，绝不原地改。
- 类型改动必须与 ADT 文档一致；文档先行。

流顺序固定：screen/chat/score/keystroke/terminal_control（回放与校验共用）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Optional

# 流名：ADT 五流，顺序固定，不得增删改（含顺序）。
SCREEN_STREAM_NAME = "screen"
CHAT_STREAM_NAME = "chat"
SCORE_STREAM_NAME = "score"
KEYSTROKE_STREAM_NAME = "keystroke"
TERMINAL_CONTROL_STREAM_NAME = "terminal_control"

STREAM_NAMES: Final[tuple[str, ...]] = (
    SCREEN_STREAM_NAME,
    CHAT_STREAM_NAME,
    SCORE_STREAM_NAME,
    KEYSTROKE_STREAM_NAME,
    TERMINAL_CONTROL_STREAM_NAME,
)

__all__ = [
    "SCREEN_STREAM_NAME",
    "CHAT_STREAM_NAME",
    "SCORE_STREAM_NAME",
    "KEYSTROKE_STREAM_NAME",
    "TERMINAL_CONTROL_STREAM_NAME",
    "STREAM_NAMES",
    "HUMAN_USER_NAME",
    "is_human",
    "AgentRole",
    "agent_role_from_name",
]


# =====================================================================
# G2：UserName / AgentRole（记录层命名）
# =====================================================================
#
# UserName := Oneof | $agent_name str | $human ()
# 存储/JSON 表示：agent 用其名字字符串；human 用保留哨兵 "$human"。
# "$human" 是保留字：任何 agent 名不得等于它（is_human 即为判定）。

HUMAN_USER_NAME: Final[str] = "$human"


def is_human(user_name: str) -> bool:
    """UserName 是否为 $human 哨兵（agent 名永不为 True）。"""
    return user_name == HUMAN_USER_NAME


# AgentRole := Oneof | $generator | $active_discriminator | $passive_discriminator
# 存储/JSON 表示：枚举名 -> 带 $ 前缀的字符串，与 role map JSON 一一对应。
class AgentRole(Enum):
    GENERATOR = "$generator"
    ACTIVE_DISCRIMINATOR = "$active_discriminator"
    PASSIVE_DISCRIMINATOR = "$passive_discriminator"


def agent_role_from_name(role_name: str) -> AgentRole:
    """按 JSON 字符串还原 AgentRole；未知或非字符串抛 ValueError。"""
    return AgentRole(role_name)


# =====================================================================
# G3：ScreenCapture（屏幕流记录；纯字段层）
# =====================================================================
#
# ScreenCapture := DataClass
#   * $screen_data str                   # 纯文本；逐行右去尾部空格
#   * $terminal_text_content list[TerminalTextSegment]   # 带样式段；G6 定义
#   * $cursor_x / $cursor_y int          # 格坐标 0 起
#   * $screen_width / $screen_height int
#   * $captured_at datetime
#
# 本目标只定义字段（不变量在 G4/G5 以断言函数实现，不塞进 dataclass）。

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
# G4：ScreenCapture 断言①—— plain_of(segments) == screen_data
# =====================================================================
#
# ADT Appendix helper（照抄 MEETING_ADT.md，名字不改）：
#   frame_rows(s) := s.split('\n')
#   row_plain(row) := row.rstrip(' ')
#   concat_text_field_of_list(list) := ''.join(seg.text for seg in list)
#   plain_of(list) := '\n'.join(row_plain(r)
#                               for r in frame_rows(concat_text_field_of_list(list)))
#
# 断言函数统一签名：接收记录，违规抛 AssertionError（带 captured_at 定位），
# 通过则静默返回 None——供 G13 校验器收集违规清单。

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
# G5：ScreenCapture 断言②-⑤（宽度 / 行数 / 光标界内 / 段合法）
# =====================================================================

import unicodedata as _ucd  # noqa: E402
import wcwidth as _wc  # noqa: E402


def _char_cell_width(ch: str) -> int:
    """单字符占格数：CJK(W/F)=2；控制符/零宽=0；其余=1。"""
    if _ucd.category(ch) in ("Cc", "Cf", "Mn", "Me", "Zl", "Zp", "Cs"):
        return 0
    w = _wc.wcwidth(ch)
    return w if w > 0 else 1 if w == 1 else 0


def cell_width(row: str) -> int:
    """终端显示宽度（格）。"""
    return sum(_char_cell_width(ch) for ch in row)


def seg_attrs(segment: "TerminalTextSegment") -> tuple:
    """ADT Appendix：seg_attrs(seg) := (seg.fg, seg.bg, frozenset(seg.styles))"""
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
    """段文本非空，且相邻段属性不同。

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
# G6：TerminalTextSegment / Color / Rgb / Style（屏幕样式层）
# =====================================================================
#
# Style := Oneof | "bold" | "underline" | "italic" | "reverse" | "blink" | "dim"
# Color := Oneof | $palette int(0..255) | $truecolor Rgb
# Rgb   := DataClass r/g/b int 每个 0..255
# TerminalTextSegment :=
#     DataClass
#   * $text str                    # 连续同属性文本；可含 \n
#   * $fg / $bg Optional[Color]    # None = 终端默认前景/背景
#   * $styles set[Style]           # 语义是集合，顺序无关
#
# 存储/JSON 表示：Style=字符串；Rgb={"r":..,"g":..,"b":..}；
# palette 色={"palette":n}；truecolor 色={"r":..,"g":..,"b":..}。

# Style 取值（顺序与 ADT 文档一致）
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
    """真彩色。"""
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for name, value in (("r", self.r), ("g", self.g), ("b", self.b)):
            if not 0 <= value <= 255:
                raise ValueError(f"Rgb.{name} out of range: {value}")


@dataclass(frozen=True)
class PaletteColor:
    """SGR 调色板下标 0..255。"""
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
# G7：ChatMessage / ChatStreamChunk（聊天流）
# =====================================================================
#
# ChatMessage := DataClass
#   * $message list[$raw_msg str | $at_user_name UserName | $reference ContentReference]
#   * $user_name UserName
#   * $time datetime
#
# 对照真 journal 的 chat 事件（actor/content/captured_at）落盘映射：
#   - user_name：actor=="human" → "$human"；否则 actor 即 agent 名。
#   - time：captured_at（UTC，带 Z 后缀，ISO8601）。
#   - message：当前 journal 只有纯文本 content → [{"raw": content}]；
#     @点名/引用元素在 G8 加 {"at_user_name"|"ref"}。
#   - phase/message_type 是运行噪音，不属 ADT，不落 ChatMessage。
#
# ChatMessage 落盘 JSON：
#   {"user_name": <str>, "time": <ISO8601>, "message": [part_json, ...]}
#   part_json := {"raw": str} | {"at_user_name": str} | {"ref": {...G8}}
# ChatStreamChunk 落盘 JSON：
#   {"stream_name": "chat", "chunks": [ChatMessage_json, ...]}

def _iso_parse(text: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso_format(moment: _dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RawMessage:
    """$raw_msg str。"""
    text: str


@dataclass(frozen=True)
class AtUserMessage:
    """$at_user_name UserName。"""
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
# G8：ContentReference 三件套 + resolve()
# =====================================================================
#
# TextRegion := DataClass $start int $end int
# ScreenReference := $capture_time datetime $stream_name str $region TextRegion
# ChatReference   := $msg_time datetime $user_name UserName $region TextRegion
# ScoreReference  := $judge_time datetime $judge_name UserName
# ContentReference := Oneof | $screen_ref | $chat_ref | $score_ref
#
# 存储设计：引用只存键（时间+user/stream），不落目标内容副本；
# resolve 靠同会流内按键查找，找不到/多解都算引用坏链（抛错，供校验收集）。
#
# 落盘 JSON（Oneof 用单键信封区分）：
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
    """ScreenReference.resolve：captured_at == capture_time。"""
    matches = [c for c in screen_captures if c.captured_at == reference.capture_time]
    return _resolve_matches(matches, "screen capture")  # type: ignore[return-value]


def resolve_chat_reference(reference: ChatReference,
                           chat_messages: "list[ChatMessage]") -> ChatMessage:
    """ChatReference.resolve：time == msg_time ∧ user_name == 引用者。"""
    matches = [m for m in chat_messages
               if m.time == reference.msg_time and m.user_name == reference.user_name]
    return _resolve_matches(matches, "chat message")  # type: ignore[return-value]


def resolve_score_reference(reference: ScoreReference,
                            score_messages: "list[ScoreMessage]") -> "ScoreMessage":
    """ScoreReference.resolve：judge_name ∧ time == judge_time（ScoreMessage 在 G9 定义）。"""
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
# G9：ScoreMessage / ScoreStreamChunk（评分流）
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
#                         ∨ 该消息的 $reference 可解析到一条召唤它的消息
#     }]
#   * Assert[{ summon_chained }]
#
# 真 journal 目前无 score 事件 → 空流表示：ScoreStreamChunk(chunks=[])。
# 落盘 JSON：{"tag": s, "score": float|null, "comment": str|null,
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
    """Appendix summon_chained 的可判定版本。

    角色判定委托给调用方注入的 role_of（MeetingData.agent_name2role 查询），
    referenced_message 为按 reference resolve 到的目标消息（无引用则 None）。
    passive 判定：须引用可解析到含 @judge 的召唤消息。
    """
    role = role_of(message.judge_name) if callable(role_of) else None
    if role != AgentRole.PASSIVE_DISCRIMINATOR:
        return True  # generator / active / 非 agent(如 human) 无需被召唤
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


