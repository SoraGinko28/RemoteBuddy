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

