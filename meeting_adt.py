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


