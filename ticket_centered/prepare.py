"""一条数据路径上留下的那份值：`$call` 说这一步拿到了什么，`$measured` 说它答了什么。

这份值落在 `<数据路径>/prepare/<算子>.viba`：文件里两段，`$call`（这次调用的实参，已经固定下来）
和 `$measured`（答下的结果；还没人答就是 `nil`）。读它的人照数据路径来读，所以一张工单是自足的——
数据路径说有哪一步、要哪个算子，`$call` 说这一步拿到了什么。

这个模块只管读写这份文件、从里面取出那两段。谁该做哪一步不在这里：那是能力表和服务的事。
"""

from __future__ import annotations

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import Environment, read_snapshot, write_snapshot, viba_data
from viba.reflect import access as reflect_access, by_tag
from viba.type import VibaProgramErr

PREPARE_SEGMENT = "prepare"
CALL_TAG = "$call"
MEASURED_TAG = "$measured"
VIBA_SUFFIX = ".viba"
# 一次调用写成可执行的那两个名字：`__dyn_call__ << "名字" << 实参…`，成员调用再多一个「从哪个值上取」
DYN_CALL_NAME = "__dyn_call__"
DYN_METHOD_NAME = "__dyn_method__"


def prepare_path(name: str) -> str:
    """这份值在 store 里的那条路（`name` 带不带 `.viba` 都行，写的人自己带上）。"""
    return f"{PREPARE_SEGMENT}/{name}"


def read_prepare(environ: Environment, name: str):
    """这条数据路径下留下的那份值，没有就是 None。"""
    return read_snapshot(environ, prepare_path(name))


def record_prepare(environ: Environment, name: str, call, measured=None) -> None:
    """把这份值写到这条数据路径下：`$call` 固定下来，`$measured` 是答下的结果（没有就是 nil）。"""
    write_snapshot(environ, prepare_data(call, measured), prepare_path(name))


def prepare_data(call, measured=None):
    """`$call` 加 `$measured`，写成一段可序列化数据。"""
    return viba_ast.ProductChain([
        viba_ast.Tagged(CALL_TAG, _piece(call)),
        viba_ast.Tagged(MEASURED_TAG,
                        _piece(measured) if measured is not None else viba_ast.Nil()),
    ])


def call_of(prepare):
    """这份值固定下来的那次调用，没有就是 None。"""
    return member_of(prepare, CALL_TAG)


def measured_of(prepare):
    """(答下的结果, 答了没有)。答下的是那一个叶子；没有答案、或者答案不是一个叶子，就是 (None, False)。"""
    node = member_of(prepare, MEASURED_TAG)
    if node is None:
        return None, False
    leaf = reflect_access.leaf(node)
    if isinstance(leaf, VibaProgramErr) or leaf.ok_value is None:
        return None, False
    return leaf.ok_value, True


def member_of(node, tag: str):
    """一个对象里那一个成员；没有、或者它写着 nil，就是 None。

    对象外面还套着一支（`$err ($not_implemented_err …)` 那种）就先走到那一支底下：成员在 tag 里面。
    """
    if node is None:
        return None
    piece = getattr(node, "data", None)
    if isinstance(piece, viba_ast.Tagged):
        node = viba_data(piece.type)
    given = reflect_access.get(node, by_tag(tag))
    if isinstance(given, VibaProgramErr) or given.ok_value is None:
        return None
    return given.ok_value


def call_arguments(call) -> list:
    """一份 `$call` 里那几个实参，按写下来的次序。

    调用写成 `__dyn_call__ << "名字" << 实参…`，名字不是实参，这里摊掉它；成员调用
    （`__dyn_method__`）还多一个「从哪个值上取的」，那个也不算。老那种写法（整份 `$call` 就是那一个
    实参）原样交回一个。
    """
    piece = getattr(call, "data", call)
    if piece is None:
        return []
    if not isinstance(piece, viba_ast.Partial):
        return [piece]
    peeled = []
    while isinstance(piece, viba_ast.Partial):
        if piece.argument is not None:
            peeled.append(piece.argument)
        piece = piece.function
    peeled.reverse()
    if isinstance(piece, viba_ast.TypeRef):
        if piece.name == DYN_METHOD_NAME:
            return peeled[2:]
        if piece.name == DYN_CALL_NAME:
            return peeled[1:]
    return peeled


def _piece(value):
    """一个值写成 viba 数据的那一段。"""
    return viba_data(value).data
