"""一个成员，和它在一份 store 上的一次跑。

**能力表**是成员唯一的能力面：表里没有的算子，它做不了——`interpret` 自己不带实现，一段 viba 能
调到的算子一个都跑不出这张表。

一个成员在一份 store 上跑一段程序（`Runner`）：自己会的算子就按实现了算；store 里已经有答案的就
**回放**（同一个副作用不做第二遍）；都不会就**递延**——那一步交给别人。每一步答完，交出去的是这条
数据路径（`$module_path … * $func_name …`），值留在 store 里（`<数据路径>/<算子>.viba`）。

解释器问的是**这一段是哪一步**（`get_func(module, name)`：算子在哪个模块里、叫什么），数据路径在
环境上——实现拿到环境的时候才拿得到，所以回放、算、交出去这三件事都在那一次调用里定（`Runner._step`）。

一次跑交回的答复也在这里读（`$ok` 是答下的值，`$err` 是停在哪、为什么停）：跑完了没有、答下的是什么、
停下的那句话，以及「哪一步没人实现」——那一步就是接着要发的一张工单。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import (Environment, EnvironmentStorage, not_implemented,
                            read_snapshot, viba_data, write_snapshot)

from .store import Store, leaf_value

# 一次跑的答复上那几个名字（`viba/interpret_result.viba`）
OK_TAG = "$ok"
ERR_TAG = "$err"
NO_IMPLEMENTATION_TAG = "$not_implemented_err"
STOP_MSG = "$msg"
STOP_PATH = "$module_path"
STOP_NAME = "$full_qualified_func_name"
STOP_CALL = "$call"


def capability_digest() -> str:
    """这一侧代码（`ticket_centered/` 里这几个文件）的内容摘要。

    能力面就是这几个文件：谁能做哪些事、做出来是什么效果，全在里面。这个数变了，就说明
    "会做的事"变了——会议记录里记着每次开会用的是哪个数，事后再看对不上就是有人动过。
    """
    seed = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        seed.update(path.name.encode("utf-8"))
        seed.update(b"\0")
        seed.update(path.read_bytes())
        seed.update(b"\0")
    return seed.hexdigest()


class Member:
    """One member of the meeting: a name, a role, and its capability table."""

    def __init__(self, name: str, role: str, caps: Dict[str, Callable]):
        self.name = name
        self.role = role
        self.caps: Dict[str, Callable] = dict(caps)

    def at(self, store: Store, case: str = "", viba_path=None) -> "Runner":
        """This member, running against one store, at one case."""
        return Runner(self, store, case, viba_path)


class Runner:
    """One member on one store, at one case: where its program actually runs.

    `case` is this run's case (a data path inside the store). Every path a
    ticket writes is relative to it, so the same ticket runs for any case.
    """

    def __init__(self, member: Member, store: Store, case: str = "", viba_path=None):
        self.member = member
        self.store = store
        self.case = case
        self.viba_path = viba_path
        # What this member really did: replays and deferrals are not work.
        self.did: List[Tuple[str, str, Any]] = []
        self.deferred: List[Tuple[str, str]] = []

    def absolute(self, module_path: str) -> str:
        """A ticket's data path, made absolute: "" is the case itself."""
        if not module_path:
            return self.case
        if module_path == self.case or (self.case and module_path.startswith(f"{self.case}/")):
            return module_path
        return f"{self.case}/{module_path}" if self.case else module_path

    def env(self, module_path: str = "") -> Environment:
        """The environment of one data path of this run."""
        return self.store.env(self.absolute(module_path), self.viba_path, self.get_func, self.case)

    def get_func(self, module: str, func_name: str):
        """The interpreter asks: is this step implemented here?

        It hands the module the step is declared in and the step's name. The data path is not among
        them: it is the environment's, and the implementation is given that environment when it is
        called — so all three answers (replay, compute, hand it out) are decided there.
        """
        return self._step(func_name)

    def _step(self, func_name: str):
        """这一步在这里怎么答：store 里有值就回放，能力表里有这个算子就算，都没有就交出去。"""

        def step(env, *args):
            module_path = env.storage.cur_storage_path
            if read_snapshot(env, func_name) is not None:
                self.did.append((module_path, func_name, "replayed"))
                return step_address(module_path, func_name)
            impl = self.member.caps.get(func_name)
            if impl is None:
                self.deferred.append((module_path, func_name))
                return not_implemented(f"{self.member.name} 不会 {func_name}",
                                       module_path=module_path,
                                       full_qualified_func_name=func_name)
            value = impl(env, *args)
            write_snapshot(env, value, func_name)
            self.did.append((module_path, func_name, value))
            return step_address(module_path, func_name)

        return step


def absolute_of(env, module_path: str) -> str:
    """A ticket's data path under the case this environment belongs to."""
    storage = env.storage
    if hasattr(storage, "absolute"):
        return storage.absolute(module_path)
    return module_path


def environment_at(env, module_path: str) -> Environment:
    """Another data path of the same store, derived from the environment in hand."""
    storage = env.storage
    if hasattr(storage, "at"):
        return Environment(storage.at(module_path), env.compute, env.viba_path)
    return Environment(EnvironmentStorage(module_path, None, storage.store_root_dir),
                       env.compute, env.viba_path)


def text_of(env, piece) -> str:
    """A stored value as text: an address is read (the store has it), a literal is itself."""
    return str(value_of(stored_of(env, piece)))


def value_of(piece) -> Any:
    """The leaf a piece carries (a bool / int / str / None), or the piece itself."""
    return getattr(piece, "value", piece)


def stored_of(env, piece):
    """The value a step address points at; a piece that is no address is itself."""
    try:
        step_of(piece)
    except RuntimeError:
        return piece
    return read_stored(env, piece)


def read_stored(env, address) -> Any:
    """Read the value one step answered: `($module_path "…" * $func_name "…")`.

    The address is what a step hands over; the value stays in the store, so
    this is how an operator gets at it.
    """
    module_path, name = step_of(address)
    absolute = absolute_of(env, module_path)
    value = leaf_value(read_snapshot(environment_at(env, absolute), name))
    if value is None:
        raise RuntimeError(f"那一步还没答：{absolute} + {name}")
    return value


def step_address(module_path: str, func_name: str):
    """A step written as an address: `($module_path "…" * $func_name "…")`."""
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$module_path", viba_ast.Constant(module_path)),
        viba_ast.Tagged("$func_name", viba_ast.Constant(func_name)),
    ]))


def elements_of(piece):
    """Flatten a product: `Product` (binary) and `ProductChain` alike."""
    if isinstance(piece, viba_ast.Product):
        return elements_of(piece.left) + elements_of(piece.right)
    if isinstance(piece, viba_ast.ProductChain):
        out = []
        for member in piece.elements:
            out.extend(elements_of(member))
        return out
    return [piece]


def step_of(address) -> Tuple[str, str]:
    """A step address read back: (data path, definition)."""
    fields = {}
    piece = getattr(address, "data", address)
    for member in elements_of(piece):
        if isinstance(member, viba_ast.Tagged) and isinstance(member.type, viba_ast.Constant):
            fields[member.tag] = str(member.type.value)
    if "$module_path" not in fields or "$func_name" not in fields:
        raise RuntimeError(f"这不是一个步骤：{viba_ast.unparse_type(piece)!r}")
    return fields["$module_path"], fields["$func_name"]


# ----------------------------------------------------------------------
# 一次跑交回的答复
# ----------------------------------------------------------------------


def tag_of(answer) -> str:
    """这个答复上是哪一支（`$ok` 还是 `$err`，以及停在哪一步那种）；读不出来就是空串。"""
    piece = getattr(answer, "data", answer)
    return str(getattr(piece, "tag", "") or "")


def finished(answer) -> bool:
    """这次跑完了没有。"""
    return tag_of(answer) == OK_TAG


def answer_of(answer):
    """这次跑答下的值（一个节点）；没跑完就是 None。"""
    if not finished(answer):
        return None
    return answer.by_tag(OK_TAG)


def _stopped(answer):
    """停下的那一支（一个节点）；跑完了就是 None。"""
    if tag_of(answer) != ERR_TAG:
        return None
    return answer.by_tag(ERR_TAG)


def failure_in(answer) -> str:
    """停下的那句话；跑完了、或者停在「这一步没人实现」，都是空串。"""
    stopped = _stopped(answer)
    if stopped is None or tag_of(stopped) == NO_IMPLEMENTATION_TAG:
        return ""
    said = _member_in(stopped, STOP_MSG)
    return "" if said is None else str(said.value)


def unimplemented_step(answer) -> Optional[Tuple[str, str, Any]]:
    """这次跑停在哪一步没人实现：`(数据路径, 算子, $call)`；不是停在那儿就是 None。"""
    stopped = _stopped(answer)
    if stopped is None or tag_of(stopped) != NO_IMPLEMENTATION_TAG:
        return None
    path = _member_in(stopped, STOP_PATH)
    name = _member_in(stopped, STOP_NAME)
    return ("" if path is None else str(path.value),
            "" if name is None else str(name.value),
            _member_in(stopped, STOP_CALL))


def _member_in(node, tag: str):
    """一个答复里那一段；没有就是 None。

    答复外面还套着一支（`$err ($not_implemented_err …)` 那种）就先走到那一支底下：那几段在 tag 里面。
    """
    if node is None:
        return None
    piece = getattr(node, "data", None)
    if isinstance(piece, viba_ast.Tagged):
        node = viba_data(piece.type)
    try:
        return node.by_tag(tag)
    except Exception:                 # 这一段这份数据里没有
        return None
