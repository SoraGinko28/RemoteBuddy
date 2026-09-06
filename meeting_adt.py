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
]
