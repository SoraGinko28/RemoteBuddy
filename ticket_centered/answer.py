"""一次跑交回的答复：`$ok` 是答下的值，`$err` 是它停在哪、为什么停。

`interpret` 和 `exec` 交回的就是这一个节点：外层带着 `$ok` 或 `$err` 那个 tag。这里只做一件事——
把它读成能用的几样：这次跑完了没有、答下的是什么、停下的那句话，以及「哪一步没人实现」。

**没人实现不是坏了**：那一支说的是这一步该由别人的能力表来答，所以 `failure_of` 对它答空串，
要的是 `duty_of` 读出来的那一步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import _viba  # noqa: F401
from .prepare import member_of

from viba.type import ERR_TAG, NOT_IMPLEMENTED_TAG, OK_TAG

MSG_TAG = "$msg"
MODULE_PATH_TAG = "$module_path"
FUNC_NAME_TAG = "$full_qualified_func_name"
CALL_TAG = "$call"


@dataclass
class Duty:
    """没人实现的那一步：哪个数据路径、要哪个算子、这一步拿到了什么。"""

    module_path: str
    func_name: str
    call: Any = None


def tag_of(node) -> str:
    """这个节点上那个 tag；不带 tag 的节点给空串。"""
    piece = getattr(node, "data", node)
    return str(getattr(piece, "tag", "") or "")


def is_ok(result) -> bool:
    """这次跑跑完了没有。"""
    return tag_of(result) == OK_TAG


def error_of(result):
    """停下的那一支（一个节点）；跑完了就是 None。"""
    if tag_of(result) != ERR_TAG:
        return None
    return result.by_tag(ERR_TAG)


def value_of(result):
    """这次跑答下的值（一个节点）；没跑完就是 None。"""
    if not is_ok(result):
        return None
    return result.by_tag(OK_TAG)


def failure_of(result) -> str:
    """停下的那句话；跑完了、或者停在「这一步没人实现」，都是空串。"""
    node = error_of(result)
    if node is None or tag_of(node) == NOT_IMPLEMENTED_TAG:
        return ""
    said = member_of(node, MSG_TAG)
    return "" if said is None else str(said.value)


def duty_of(result) -> Optional[Duty]:
    """这次跑停在哪一步没人实现；不是停在那儿就是 None。"""
    node = error_of(result)
    if node is None or tag_of(node) != NOT_IMPLEMENTED_TAG:
        return None
    return Duty(_text(node, MODULE_PATH_TAG), _text(node, FUNC_NAME_TAG),
                member_of(node, CALL_TAG))


def _text(node, tag: str) -> str:
    """那个成员作为一串字；没有就是空串。"""
    said = member_of(node, tag)
    return "" if said is None else str(said.value)
