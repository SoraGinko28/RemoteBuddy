"""一个成员，和它在一份 store 上的一次跑。

**能力表**是成员唯一的能力面：表里没有的算子，它做不了——`interpret` 自己不带实现，一段 viba 能
调到的算子一个都跑不出这张表。

一个成员在一份 store 上跑一段程序（`Runner`）：自己会的算子就按实现了算；store 里已经有答案的就
**回放**（同一个副作用不做第二遍）；都不会就**递延**——那一步交给别人。每一步答完，交出去的是这条
数据路径（`$module_path … * $func_name …`），材料留在 store 里。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.compliance import (PreparedStorage, call_of, measured_of, read_prepare,
                             record_prepare)
from viba.interpret import Environment, material
from viba.type import NotMyDutyException

from .store import Store


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

    def get_func(self, module_path: str, func_name: str):
        """The interpreter asks: do you implement this step?"""
        _value, answered = self.store.read(module_path, func_name)
        if answered:
            self.did.append((module_path, func_name, "replayed"))
            return lambda *args: address_material(module_path, func_name)
        impl = self.member.caps.get(func_name)
        if impl is None:
            self.deferred.append((module_path, func_name))
            raise NotMyDutyException(reason=f"{self.member.name} 不会 {func_name}")
        return self._computing(impl, func_name)

    def _computing(self, impl, func_name):
        """Run the implementation, then answer with this step's own data path."""

        def leaf(env, *args):
            value = impl(env, *args)
            module_path = env.storage.cur_storage_path
            record_prepare(env, func_name, call_of(read_prepare(env, func_name)), value)
            self.did.append((module_path, func_name, value))
            return address_material(module_path, func_name)

        return leaf


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
    return Environment(PreparedStorage(module_path, None, storage.store_root_dir,
                                       getattr(storage, "prepare_root_dir", None)),
                       env.compute, env.viba_path)


def text_of(env, piece) -> str:
    """A material as text: an address is read (the store has it), a literal is itself."""
    return str(value_of(material_of(env, piece)))


def value_of(piece) -> Any:
    """The leaf a piece carries (a bool / int / str / None), or the piece itself."""
    return getattr(piece, "value", piece)


def material_of(env, piece):
    """The material a step address points at; a piece that is no address is itself."""
    try:
        step_of(piece)
    except RuntimeError:
        return piece
    return read_material(env, piece)


def read_material(env, address) -> Any:
    """Read the material one step answered: `($module_path "…" * $func_name "…")`.

    The address is what a step hands over; the material stays in the store, so
    this is how an operator gets at it.
    """
    module_path, name = step_of(address)
    absolute = absolute_of(env, module_path)
    value, answered = measured_of(read_prepare(environment_at(env, absolute), name))
    if not answered:
        raise RuntimeError(f"那一步还没答：{absolute} + {name}")
    return value


def address_material(module_path: str, func_name: str):
    """A step as material: `($module_path "…" * $func_name "…")`."""
    return material(viba_ast.ProductChain([
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
