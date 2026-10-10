"""一个角色和一次跑之间那层绑定：这次跑的每一步由谁答。

**能力表**是一个角色唯一的能力面：表里没有的算子它做不了——`interpret` 自己不带实现，一段 viba 能
调到的算子一个都跑不出这张表。

解释器问的是**这一步属于哪个角色**：`get_func(module, name)`——算子在哪个模块里、叫什么。数据路径
不在里面，它在环境上，所以"这一步怎么答"是在实现拿到环境的时候才定的（`Binding._step`）：

* 这条数据路径下已经有值 → **回放**它（同一个副作用不做第二遍）；
* 能力表里有这个算子 → 按实现了算，值写在这条数据路径下；
* 都没有 → 交出去：答 `$not_implemented`（`viba.interpret` 的一份数据，不抛异常），谁的表里有
  那一步谁就接着做。

一次跑交回的答复也在这里读：`$ok` 是答下的值，`$err` 是停在哪、为什么停。`unimplemented_step`
说的就是"停在哪一步、那一步属于谁"——但停在那儿不是失败，是轮换的那一手。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                            not_implemented, get_snapshot, viba_data, put_snapshot)

from .builtins import the_table as the_builtin_table

class Forwarded:
    """一段程序转发出来的停下，包一下再交给接单那一层。

    它必须和"这一步答下的值"分得开：值要留成快照，转发出来的停下原样交回给 viba（那是"哪一步不是
    它的事"，不是一个答案），不能当成值记下来——记下来的话，下一个接单的人读到的就是上一次的停下。
    """

    def __init__(self, node) -> None:
        self.node = node


# 一次跑的答复上那几个名字（`viba/interpret_result.viba`）
OK_TAG = "$ok"
ERR_TAG = "$err"
NO_IMPLEMENTATION_TAG = "$not_implemented_err"
STOP_MSG = "$msg"
STOP_PATH = "$module_path"
STOP_NAME = "$full_qualified_func_name"
STOP_CALL = "$call"


class Binding:
    """一个角色，在一个案子上的一次跑：名字、能力表，和这次跑干过的事。"""

    def __init__(self, who: str, caps: Dict[str, Callable], root,
                 viba_path=None, programs=()) -> None:
        self.who = who
        self.caps: Dict[str, Callable] = dict(caps)
        self.root = root
        self.viba_path = viba_path
        # 那几个名字是"一整段程序"（单上那次调用），不是一个算子：它们不留值——程序自己每一步的值
        # 都落在各自的算子名下，程序的名字再记一份只会多出一份没用的东西。
        self.programs = set(programs)
        # viba 带的那些算子（`builtin.viba`）：宿主答，也不留值——它们是算出来的，不是这一场的账。
        self.builtins = the_builtin_table()
        # 这次跑真干过的事：回放和递延都不是它干的活。
        self.did: List[Tuple[str, str, Any]] = []
        self.deferred: List[Tuple[str, str]] = []

    def env(self, module_path: str = "") -> Environment:
        """这个案子里一条数据路径的环境。"""
        return Environment(EnvironmentStorage(module_path, None, str(self.root)),
                           EnvironmentCompute(self.get_func), self.viba_path)

    def get_func(self, module: str, func_name: str):
        """解释器问：这一步在这里怎么答。

        角色自己那张表里的算子答的是**地址**：api 就是那么声明的，值留在案子里，要用的人照地址来读。
        viba 带的那些算子（`builtin.viba`）答的是**值本身**——它们是算出来的，`add` 就是那个和、
        `if` 的开关就是那个分支；换成地址，程序拿到的东西就不是它声明的那个类型了。
        """
        impl = self.caps.get(func_name)
        if impl is None and module:
            impl = self.caps.get(f"{module}.{func_name}")
        if impl is not None:
            return self._answer(func_name, impl)
        one = self.builtins.get(f"{module}.{func_name}" if module else func_name)
        if one is None and not module:
            one = self.builtins.get(func_name)
        if one is not None:
            return one
        return self._from_the_storage(func_name)

    def _answer(self, func_name: str, impl: Callable):
        """这一步由它算：案子里已经有值就回放，没有就算一次、把值留在这条数据路径下。

        答回去的是**这一步的地址**（`($module_path "…" * $func_name "…")`），不是那个值：值留在案子
        里，要用它的人照地址来读（`read_stored`）。规范里那几个 api 就是这么声明的——一步答下的东西
        是一个地址，所以下一步可以拿它当"接在哪一步之后"。
        """

        def step(env, *args):
            module_path = env.storage.cur_storage_path
            recorded = get_snapshot(env, func_name)
            if recorded is not None:
                self.did.append((module_path, func_name, "replayed"))
                return step_address(module_path, func_name)
            value = impl(env, *args)
            if isinstance(value, Forwarded):
                # 一段程序在自己里面转发的停下：原样交回去，那是"哪一步不是它的事"，不是一个值。
                self.deferred.append((module_path, func_name))
                return value.node
            if func_name not in self.programs and func_name not in self.builtins:
                put_snapshot(env, value, func_name)
                self.did.append((module_path, func_name, value))
            return step_address(module_path, func_name)

        return step

    def _from_the_storage(self, func_name: str):
        """这一步不归它：案子里记过就照记的答，没记过就答"这一步不是谁的事"。

        `not_implemented()` 是 viba 的：它自己会把停在哪一步（模块、整名）和那次调用补上。
        """

        def step(env, *args):
            module_path = env.storage.cur_storage_path
            recorded = get_snapshot(env, func_name)
            if recorded is not None:
                self.did.append((module_path, func_name, "replayed"))
                return step_address(module_path, func_name)
            self.deferred.append((module_path, func_name))
            return not_implemented()

        return step



# ----------------------------------------------------------------------
# 数据路径：一步交出去的时候给的是这个
# ----------------------------------------------------------------------


def step_address(module_path: str, func_name: str):
    """一步写成一个地址：`($module_path "…" * $func_name "…")`。"""
    return viba_data(viba_ast.ProductChain([
        viba_ast.Tagged("$module_path", viba_ast.Constant(module_path)),
        viba_ast.Tagged("$func_name", viba_ast.Constant(func_name)),
    ]))


def elements_of(piece):
    """把一段积摊平：`Product`（二元）和 `ProductChain` 一样看。"""
    if isinstance(piece, viba_ast.Product):
        return elements_of(piece.left) + elements_of(piece.right)
    if isinstance(piece, viba_ast.ProductChain):
        out = []
        for member in piece.elements:
            out.extend(elements_of(member))
        return out
    return [piece]


def step_of(address) -> Tuple[str, str]:
    """一个步骤地址读回来：(数据路径, 定义)。"""
    fields = {}
    piece = getattr(address, "data", address)
    for member in elements_of(piece):
        if isinstance(member, viba_ast.Tagged) and isinstance(member.type, viba_ast.Constant):
            fields[member.tag] = str(member.type.value)
    if "$module_path" not in fields or "$func_name" not in fields:
        raise RuntimeError(f"这不是一个步骤：{viba_ast.unparse_type(piece)!r}")
    return fields["$module_path"], fields["$func_name"]


def environment_at(env, module_path: str) -> Environment:
    """同一个案子里的另一条数据路径，照着手上这个环境推出来。"""
    storage = env.storage
    if hasattr(storage, "at"):
        return Environment(storage.at(module_path), env.compute, env.viba_path)
    return Environment(EnvironmentStorage(module_path, None, storage.store_root_dir),
                       env.compute, env.viba_path)


def read_stored(env, address) -> Any:
    """读一步答下的值：`($module_path "…" * $func_name "…")`。

    地址是那一步交出来的东西；值留在案子那份数据里，所以要用的人照地址来读。
    """
    module_path, name = step_of(address)
    from .values import leaf_value

    value = leaf_value(get_snapshot(environment_at(env, module_path), name))
    if value is None:
        raise RuntimeError(f"那一步还没答：{module_path or '这个案子'} + {name}")
    return value


def stored_of(env, piece):
    """一个地址指向的那个值；不是地址就是它自己。"""
    try:
        step_of(piece)
    except RuntimeError:
        return piece
    return read_stored(env, piece)


def text_of(env, piece) -> str:
    """一个值当字来读：是地址就去读它（案子那份数据里有），不是就是它自己。"""
    return str(value_of(stored_of(env, piece)))


def value_of(piece) -> Any:
    """一段数据上那个值（一个叶子）。"""
    return getattr(piece, "value", piece)


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
    """停下的那句话；跑完了、或者停在"这一步不是它的事"，都是空串。"""
    stopped = _stopped(answer)
    if stopped is None or tag_of(stopped) == NO_IMPLEMENTATION_TAG:
        return ""
    said = _member_in(stopped, STOP_MSG)
    return "" if said is None else str(said.value)


def stop_kind(answer) -> str:
    """停下的是哪一支（`$not_implemented_err` / `$underlying_viba_op_err` / …）；没停下就是空串。"""
    stopped = _stopped(answer)
    return "" if stopped is None else tag_of(stopped)


def unimplemented_step(answer) -> Optional[Tuple[str, str, Any]]:
    """这次跑停在哪一步不是它的事：`(数据路径, 算子, $call)`；不是停在那儿就是 None。"""
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
