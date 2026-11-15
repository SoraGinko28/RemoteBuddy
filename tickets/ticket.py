"""一张工单：一次要跑的调用，和它落在哪几份数据上。

工单落在会议目录下面：

    <会议目录>/tickets/<工单号>/ticket.viba

**一张单上有什么，不是这一处写的**：成员的名字、次序、类型都在接口仓库那份 `ticket.viba` 里
（`RbApiDeclare/rb_api/ticket.viba`，见 `declare.py`）。这一处只做两件事：把值按那份定义排好、写成
一份快照；照那份定义一个一个读回来。写的时候照它判一遍（`declare.Definition.check`），多一个成员、少
一个成员当场说清楚。

所以这张单上那几个成员是（照那份定义）：

* `$ticket_uid` —— 这张单的号，也是它那份进度和那个案子的名字来源；
* `$ticket_name` —— 发的是规范里哪一张（`write_a_draft` 这种）。程序在规范里，单上不发副本；
* `$call` —— 那次调用本身，`__dyn_call__ << "<整名>"`：环境留空，给它一个环境就是跑它（`exec`）。
  名字是**规范里那段程序的名字**，所以同一个名字谁接单谁就跑同一段程序；
* `$storage_path` —— 这次调用跑在的那个 viba 环境的存储根，它写的每一份快照都落在那儿（一个案子）；
* `$VIBA_PATH` —— 跑这次调用时按哪几个目录找模块，一个目录一项（接口那个仓库的目录也在里面）；
* `$handler_name2api_declare_repo_commit_id` —— 哪个处理者的接口按的是哪一版（处理者名 → `RbApiDeclare`
  那个仓库的 commit id）。接口不在规范里，在它自己的仓库里，所以单上要记下这一趟读的是哪一版；
  规范自己带的角色不来自那个仓库，这一项里就没有它；
* `$time` —— 写下它的时刻。

读它的人按成员名取（`by_tag`），写的和读的是同一份 viba 数据：写用 `put_snapshot`，读用
`get_snapshot`，中间没有第二种写法。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                            get_snapshot, viba_data, put_snapshot)
from viba.reflect import access as reflect_access
from viba.type import Err

from .declare import ticket_definition
from .layout import TICKETS_DIR, TICKET_NAME, VIBA_SUFFIX

# `$call` 写成什么：这个名字是 viba 里"把名字当数据"的那种调用（`viba/exec/viba-interpreter.md`）。
DYNAMIC_CALL = "__dyn_call__"


def a_time(when: Optional[datetime] = None) -> str:
    """一个时刻写成单上那样的字。"""
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def the_call(name: str, *given):
    """一次调用写成能再跑一次的那种数据：`__dyn_call__ << "<整名>" << 实参…`。

    环境不在里面——给它一个环境就是跑它。整名就是规范里那段程序的名字（`write_a_draft`）。
    """
    node: viba_ast.AST = viba_ast.Partial(viba_ast.TypeRef(DYNAMIC_CALL),
                                          viba_ast.Constant(name))
    for one in given:
        node = viba_ast.Partial(node, _piece(one))
    return viba_data(node)


@dataclass
class Ticket:
    """一张要跑的调用，和它跑在的那个环境。"""

    uid: str
    name: str
    call: Any                  # 那次调用，viba 数据（`the_call` 写出来的那种）
    storage_path: str
    viba_path: List[str]
    # 哪个处理者的接口按的是哪一版：处理者名 → 接口仓库的 commit id
    handler_name2api_declare_repo_commit_id: Dict[str, str]
    time: str

    def the_values(self) -> Dict[str, Any]:
        """这张单那几个成员的值，按名字给（次序由那份定义说）。"""
        return {"$ticket_uid": self.uid,
                "$ticket_name": self.name,
                "$call": self.call,
                "$storage_path": self.storage_path,
                "$VIBA_PATH": list(self.viba_path),
                "$handler_name2api_declare_repo_commit_id":
                    dict(self.handler_name2api_declare_repo_commit_id),
                "$time": self.time}

    def as_fields(self):
        """这几个成员，按接口仓库那份 `ticket.viba` 说的次序。"""
        return ticket_definition().the_fields(self.the_values())

    def as_node(self):
        """这张单写成的那一段 viba 数据：写完照那份定义核一遍。"""
        definition = ticket_definition()
        node = viba_ast.ProductChain([
            viba_ast.Tagged(tag, _piece(value))
            for tag, value in definition.the_fields(self.the_values())])
        definition.check(node)
        return node

    def environ(self, get_func=None, viba_path=None) -> Environment:
        """跑这张单用的那个环境：存储根是 `$storage_path`，模块按 `$VIBA_PATH` 找。

        环境从存储的根上开始（`""`），所以单里那条数据路径就是相对这个案子的那条路。
        """
        return Environment(
            EnvironmentStorage("", None, str(self.storage_path)),
            EnvironmentCompute(get_func),
            viba_path if viba_path is not None else _path_string(self.viba_path))


def _path_string(paths) -> str:
    """几个目录写成一串（`Environment.viba_path` 要的那种写法）。"""
    import os
    seen: List[str] = []
    for one in paths or ():
        if one and one not in seen:
            seen.append(one)
    return os.pathsep.join(seen)


# ----------------------------------------------------------------------
# 盘上那几个动作
# ----------------------------------------------------------------------


def ticket_dir(meeting_dir, uid: str) -> Path:
    """这一张单的目录。"""
    return Path(meeting_dir) / TICKETS_DIR / uid


def ticket_path(meeting_dir, uid: str) -> Path:
    """这一张单写在哪儿。"""
    return ticket_dir(meeting_dir, uid) / f"{TICKET_NAME}{VIBA_SUFFIX}"


def write_ticket(meeting_dir, ticket: "Ticket") -> Path:
    """把这张单写下来，答它落在哪儿。

    写法和别处的值一样（`put_snapshot`）：先写在旁边、再改名落到这条路上，读的人不会读到半张单。
    """
    put_snapshot(_environ(meeting_dir, ticket.uid), ticket.as_node(), TICKET_NAME)
    return ticket_path(meeting_dir, ticket.uid)


def read_ticket(meeting_dir, uid: str) -> Optional["Ticket"]:
    """这张单，没写过就是 None。"""
    stored = get_snapshot(_environ(meeting_dir, uid), TICKET_NAME)
    if stored is None:
        return None
    return ticket_of_node(stored)


def ticket_of_node(node) -> "Ticket":
    """这一段 viba 数据当成一张单来读：成员照接口仓库那份 `Ticket` 一个一个取。"""
    fields = {}
    for tag in ticket_definition().tags:
        if tag == "$handler_name2api_declare_repo_commit_id":
            # 早先发的单上没有这个成员：那时候接口还在规范里，没有哪一版可记
            fields[tag[1:]] = a_dict(node.by_tag(tag)) if tag in node else {}
            continue
        got = node.by_tag(tag)
        if tag == "$call":
            fields["call"] = got
        elif tag == "$VIBA_PATH":
            fields["viba_path"] = a_list(got)
        else:
            fields[tag[1:]] = _leaf(got)
    return Ticket(uid=fields["ticket_uid"], name=fields["ticket_name"], call=fields["call"],
                  storage_path=fields["storage_path"], viba_path=fields["viba_path"],
                  handler_name2api_declare_repo_commit_id=fields[
                      "handler_name2api_declare_repo_commit_id"],
                  time=fields["time"])


def ticket_uids(meeting_dir) -> List[str]:
    """这场会发过的工单，按号排。目录下有那张单的才算——写的人先建目录，后落单。"""
    root = Path(meeting_dir) / TICKETS_DIR
    if not root.is_dir():
        return []
    return sorted(one.name for one in root.iterdir()
                  if one.is_dir() and (one / f"{TICKET_NAME}{VIBA_SUFFIX}").is_file())


def tickets(meeting_dir) -> List["Ticket"]:
    """这场会发过的每一张单，按号排。"""
    out = []
    for uid in ticket_uids(meeting_dir):
        one = read_ticket(meeting_dir, uid)
        if one is not None:
            out.append(one)
    return out


def ticket_text(meeting_dir, uid: str) -> Optional[str]:
    """这张单在盘上那份原文，没写过就是 None。"""
    try:
        return ticket_path(meeting_dir, uid).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _environ(meeting_dir, uid: str) -> Environment:
    """这张单的存储：根是 `tickets/`，路径是它的号。"""
    return Environment(EnvironmentStorage(uid, None, str(Path(meeting_dir) / TICKETS_DIR)),
                       None)


# ----------------------------------------------------------------------
# 读一个成员
# ----------------------------------------------------------------------


def a_list(node) -> list:
    """一个 `ListLiteral` 成员当成 python 的列来读，一项一项。"""
    kind = reflect_access.container_kind(reflect_access.unfold(node.descriptor))
    if kind != "list":
        raise RuntimeError(f"这个成员是一个列，而这个不是：{node!r}")
    return [_leaf(node.at_index(index)) for index in range(len(node))]


def a_dict(node) -> dict:
    """一个 `DictLiteral` 成员当成 python 的字典来读，一项一项。"""
    if not node.is_dict:
        raise RuntimeError(f"这个成员是一个字典，而这个不是：{node!r}")
    return {str(key): _leaf(node.at_key(key)) for key in node.keys()}


def _leaf(node):
    """一个成员上那个值。"""
    given = reflect_access.leaf(node)
    if isinstance(given, Err):
        raise RuntimeError(f"这个成员不是一个值：{given.msg}")
    return given.ok_value


def _piece(value) -> viba_ast.AST:
    """一个值写成 viba 数据的那一段。"""
    if isinstance(value, viba_ast.AST):
        return value
    data = getattr(value, "data", value)
    if isinstance(data, viba_ast.AST):
        return data
    if isinstance(value, list):
        return viba_ast.TypeApp("ListLiteral", [_piece(one) for one in value])
    if isinstance(value, dict):
        return viba_ast.TypeApp("DictLiteral", [
            viba_ast.Tuple([_piece(key), _piece(one)]) for key, one in value.items()])
    return viba_ast.Constant(value)
