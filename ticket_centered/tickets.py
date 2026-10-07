"""一张工单：一次要跑的调用，和它被处理的每一次。

工单落在数据路径下面：

    <数据路径>/tickets/<算子>/ticket.viba              这一张单：文件里就是那次调用
    <数据路径>/tickets/<算子>/progress/<时刻>-<谁>.viba  一次尝试：谁试的、那次跑答了什么、什么时候

**单子就是那次调用本身**：`__dyn_call__ << "<整名>" << 实参…` 那份 viba 数据，环境留空——给它一个
环境就是跑它（`viba.interpret.exec`）。数据路径不写在文件里：文件落在哪儿就是哪条路径，一条数据路径
下一个算子一张单。

**进度只增**：一次尝试写一个文件，名字带写它的时刻和是谁写的，所以两个人同时写也不会碰上；它是账，
不是"做完没有"的来源——一步答下了没有，读的是这条数据路径下那份值在不在（`store.Store.read`）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import Environment, EnvironmentStorage, read_snapshot, write_snapshot

TICKETS_DIR = "tickets"
TICKET_NAME = "ticket"
PROGRESS_DIR = "progress"
VIBA_SUFFIX = ".viba"

# 一篇进度上那三段
STAGE_WHO = "$service"
STAGE_ANSWER = "$interpret_result"
STAGE_TIME = "$time"


def a_time(when: Optional[datetime] = None) -> str:
    """一个时刻写成文件里那样的字。"""
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def tickets_dir(store_root, module_path: str) -> Path:
    """这条数据路径下面放工单的地方。"""
    return _at(store_root, module_path) / TICKETS_DIR


def ticket_dir(store_root, module_path: str, name: str) -> Path:
    """一个算子那一张单的目录。"""
    return tickets_dir(store_root, module_path) / name


def ticket_file(store_root, module_path: str, name: str) -> Path:
    """一个算子那一张单写在哪儿。"""
    return ticket_dir(store_root, module_path, name) / f"{TICKET_NAME}{VIBA_SUFFIX}"


def progress_dir(store_root, module_path: str, name: str) -> Path:
    """这个算子那些尝试写在哪儿。"""
    return ticket_dir(store_root, module_path, name) / PROGRESS_DIR


def write_ticket(store_root, module_path: str, name: str, call) -> Path:
    """把这一张单写下来：文件里就是那次调用，环境留空。

    写法和别处的值一样（`write_snapshot`）：先写在旁边、再改名落到这条路上，读的人不会读到半张单。
    """
    path = ticket_file(store_root, module_path, name)
    write_snapshot(_environ(store_root, module_path),
                   call if isinstance(call, viba_ast.AST) or hasattr(call, "data") else call,
                   _snapshot_name(name, TICKET_NAME))
    return path


def read_ticket(store_root, module_path: str, name: str):
    """这一张单上那次调用（一个节点）；没写过就是 None。"""
    return read_snapshot(_environ(store_root, module_path), _snapshot_name(name, TICKET_NAME))


def tickets_in(store_root) -> List[Tuple[str, str]]:
    """这份 store 里写过的工单：`(数据路径, 算子)`，按路径排。

    一条数据路径下、一个算子一张单，所以这份清单就是"有哪几张单"。
    """
    root = Path(store_root)
    if not root.is_dir():
        return []
    found: List[Tuple[str, str]] = []
    for path in sorted(root.rglob(f"{TICKET_NAME}{VIBA_SUFFIX}")):
        parts = path.relative_to(root).parts
        if TICKETS_DIR not in parts:
            continue
        index = parts.index(TICKETS_DIR)
        if index + 2 != len(parts) - 1:      # …/tickets/<算子>/ticket.viba
            continue
        found.append(("/".join(parts[:index]), parts[index + 1]))
    return found


def write_stage(store_root, module_path: str, name: str, service: str, result,
                when: Optional[datetime] = None) -> Path:
    """把这一次尝试写成一篇进度：谁试的、那次跑答了什么、什么时候。"""
    stamp = _stamp(when)
    node = viba_ast.ProductChain([
        viba_ast.Tagged(STAGE_WHO, viba_ast.Constant(service)),
        viba_ast.Tagged(STAGE_ANSWER, _piece(result)),
        viba_ast.Tagged(STAGE_TIME, viba_ast.Constant(a_time(when))),
    ])
    write_snapshot(_environ(store_root, module_path), node,
                   _snapshot_name(name, PROGRESS_DIR, f"{stamp}-{service}"))
    return progress_dir(store_root, module_path, name) / f"{stamp}-{service}{VIBA_SUFFIX}"


def stages(store_root, module_path: str, name: str) -> List[Any]:
    """这个算子那些尝试，按写的次序（文件名带时刻，所以名字序就是写的次序）。"""
    where = progress_dir(store_root, module_path, name)
    if not where.is_dir():
        return []
    out = []
    for path in sorted(where.glob(f"*{VIBA_SUFFIX}")):
        stored = read_snapshot(_environ(store_root, module_path),
                               _snapshot_name(name, PROGRESS_DIR, path.stem))
        if stored is not None:
            out.append(stored)
    return out


def who_of(stage) -> str:
    """这一篇是谁写的；读不出来就是空串。"""
    return _text_in(stage, STAGE_WHO)


def answer_of(stage):
    """这一篇里那次跑交回的答复（一个节点）；读不出来就是 None。"""
    return _member_in(stage, STAGE_ANSWER)


# ----------------------------------------------------------------------
# 文件落在哪儿、名字怎么写
# ----------------------------------------------------------------------


def _at(store_root, module_path: str) -> Path:
    return Path(store_root).joinpath(*[part for part in str(module_path).split("/") if part])


def _environ(store_root, module_path: str) -> Environment:
    """这条数据路径的存储：根是 store，路径就是这条数据路径。"""
    return Environment(EnvironmentStorage(module_path, None, str(store_root)), None)


def _snapshot_name(name: str, *rest: str) -> str:
    """快照的名字就是它在数据路径下面那条路（`write_snapshot` 拿它拼文件名）。"""
    return "/".join([TICKETS_DIR, name, *rest])


def _stamp(when: Optional[datetime]) -> str:
    """一次尝试的文件名里那一段时刻：能按名字排序，且没有冒号。"""
    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S.%fZ")


def _piece(value):
    """一个值写成 viba 数据的那一段。"""
    if isinstance(value, viba_ast.AST):
        return value
    data = getattr(value, "data", value)
    if isinstance(data, viba_ast.AST):
        return data
    return viba_ast.Constant(value)


def _member_in(node, tag: str):
    """一个对象里那一个成员；没有就是 None。"""
    if node is None:
        return None
    try:
        return node.by_tag(tag)
    except Exception:                 # 这一段这份数据里没有
        return None


def _text_in(node, tag: str) -> str:
    """那一个成员作为一串字；读不出来就是空串。"""
    said = _member_in(node, tag)
    if said is None:
        return ""
    try:
        return str(said.value)
    except Exception:                 # 不是一个叶子
        return ""
