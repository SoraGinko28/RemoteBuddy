"""meeting_core_serialize — 把 meeting_core 的对象数据写成 viba 代码（witness）。

The contract (the note at the end of the "Stable types" chapter):

    rule_reflect ∘ meeting_core_serialize ≡ meeting_core_reflect

两条都要成立：

1. 写出来的那段代码是那个定义的居民：``is_compliant(result, 定义) == Ok(True)``。
2. 在 result 上用 viba.rule.reflect 读，和把同一份数据交给 meeting_core_reflect 读，
   两边读出来一模一样。

The map and the values are walked together: the shape comes from the descriptor
(the design), the values from the data. The prose words (Assert / Appendix / Hint)
are not part of the data shape and are written nil — they are units, so a definition
that carries them is still judged fine.

三条已知缺口（都写不出来，给 Err——不硬凑一个错的写法）：

1. 空的 list / set / dict：viba 的字面量至少要一个元素（``ListLiteral[]`` 解析不过）。
2. 泛型别名不认字面量居民：``AppendOnlyList[T] := list[T]`` 那种写法下，
   ``ListLiteral[...]`` 过不了 ``is_compliant``（直接写 ``list[T]`` 就过）。
3. 聊天消息的 ``$raw_msg`` / ``$at_user_name`` 两支：Python 那边是
   ``RawMessage`` / ``AtUserMessage`` 两个类，跟设计的 tag 与叶子类型都对不上。
"""

from __future__ import annotations

import sys
from pathlib import Path

import meeting_core_reflect as _reflect

# viba 是子模块，包在它自己的根目录下面；加进 sys.path 才 import 得到。
_VENDORED_VIBA = Path(__file__).resolve().parent / "viba"
if (_VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file() and str(_VENDORED_VIBA) not in sys.path:
    sys.path.insert(0, str(_VENDORED_VIBA))

from viba import viba_ast  # noqa: E402
from viba.type import Err, Ok, Result  # noqa: E402
from viba.viba_type_descriptor import (  # noqa: E402
    LITERAL,
    NEVER,
    NIL,
    PRODUCT,
    SUM,
    TAGGED,
    TUPLE,
    TYPE_APP,
    TYPE_REF,
)

__all__ = ["serialize", "witness"]


class SerializeGap(Exception):
    """这一段现在没有 viba 写法（空容器那种）。"""


def serialize(definition_name: str, data) -> Result:
    """对象数据 -> 一段 viba 源码：同名定义，体就是那份数据的写法。

    直接 ``viba_ast.parse(result)`` 就是可用的 witness。
    """
    given = witness(definition_name, data)
    if isinstance(given, Err):
        return given
    module = viba_ast.Module([viba_ast.TypeDefinition(definition_name, given.value)])
    return Ok(viba_ast.unparse(module) + "\n")


def witness(definition_name: str, data) -> Result:
    """对象数据 -> witness 的 AST（就是 serialize 写出来那段代码）。"""
    access = _reflect.access(_reflect._definition(definition_name))
    root = access.root(_reflect._Record(data))
    if isinstance(root, Err):
        return root
    try:
        return Ok(_emit(access, root.value))
    except SerializeGap as gap:
        return Err(str(gap))


# ----------------------------------------------------------------------
# 一边走地图一边写值
# ----------------------------------------------------------------------


def _emit(access, node):
    """这一段数据的 viba 写法。"""
    shape = access._unfold(node.descriptor)
    if node.data is _reflect._NIL:
        return viba_ast.Nil()               # 装的是 unit：设计里一律写 nil
    if shape.kind == SUM:
        return _emit_sum(access, node)
    if shape.kind == PRODUCT:
        return _emit_product(access, node)
    if shape.kind == TUPLE:
        return viba_ast.Tuple([_payload(access, _child(access, node, step))
                               for step in _steps(access, node)])
    container = access._container_kind(shape)
    if container is not None:
        return _emit_container(access, node, container)
    if shape.kind == TAGGED:
        return _payload(access, _child(access, node, _reflect.by_field_index(0)))
    if shape.kind in (TYPE_REF, LITERAL):
        return _emit_leaf(node.data)
    if shape.kind in (NIL, NEVER):
        return viba_ast.Nil()
    raise SerializeGap(f"这一段写不出来：{shape.kind}")


def _emit_sum(access, node):
    """和：写选中的那一支；一支都没选中就是缺省（nil）。"""
    for tag, step, _ in access._member_steps(node):
        child = access.get(node, step)
        if not isinstance(child, Ok) or child.value is None:
            continue
        inner = _payload(access, child.value)
        return viba_ast.Tagged(tag, inner) if tag else inner
    if node.data is None:
        return viba_ast.Nil()               # 缺省那一支（Optional 的 nil）
    raise SerializeGap(f"这个和类型没有对得上的支：{type(node.data).__name__}")


def _emit_product(access, node):
    """A product: tagged members are written out, the prose words are units."""
    elements = [viba_ast.TypeRef("Object")]
    for tag, step, _ in access._member_steps(node):
        if tag is None:
            elements.append(viba_ast.Nil())     # a prose word: the unit
            continue
        child = access.get(node, step)
        if isinstance(child, Ok) and child.value is not None:
            elements.append(viba_ast.Tagged(tag, _payload(access, child.value)))
        elif _is_optional(access, step, node):
            elements.append(viba_ast.Tagged(tag, viba_ast.Nil()))
        else:
            raise SerializeGap(f"这一段没有值：{tag}")
    return viba_ast.ProductChain(elements)


def _is_optional(access, step, node) -> bool:
    """这一格的设计类型里带 nil 吗（Optional[T] = T | nil）。"""
    target = access._target_of(node, step, access._members(node))
    if target is None:
        return False
    shape = access._unfold(target)
    if shape.kind != SUM:
        return False
    return any(_reflect._is_unit_descriptor(element)
               for element in shape.payload.elements)


def _emit_container(access, node, container: str):
    """容器：写成字面量（ListLiteral / SetLiteral / DictLiteral）。"""
    length = access.length(node)
    if isinstance(length, Err):
        raise SerializeGap(length.message)
    if length.value == 0:
        raise SerializeGap(
            f"空的 {container} 现在没有 viba 写法：字面量至少要一个元素")
    if container in ("list", "set"):
        constructor = "ListLiteral" if container == "list" else "SetLiteral"
        return viba_ast.TypeApp(constructor, [
            _payload(access, _child(access, node, _reflect.at_index(index)))
            for index in range(length.value)
        ])
    if container == "dict":
        keys = access.keys(node)
        if isinstance(keys, Err):
            raise SerializeGap(keys.message)
        return viba_ast.TypeApp("DictLiteral", [
            viba_ast.Tuple([viba_ast.Constant(key),
                            _payload(access, _child(access, node, _reflect.at_key(key)))])
            for key in keys.value
        ])
    raise SerializeGap(f"容器写不出来：{container}")


def _emit_leaf(data):
    """内建叶子：五种植。"""
    if data is None:
        return viba_ast.Nil()
    if isinstance(data, (bool, int, float, str)):
        return viba_ast.Constant(data)
    raise SerializeGap(f"叶子写不出来：{type(data).__name__}")


# ----------------------------------------------------------------------
# 私有小工具
# ----------------------------------------------------------------------


def _steps(access, node):
    return [step for _, step, _ in access._member_steps(node)]


def _child(access, node, step):
    given = access.get(node, step)
    if not isinstance(given, Ok) or given.value is None:
        raise SerializeGap(f"这一步没有值：{step}")
    return given.value


def _payload(access, node):
    """分支装的载荷（单位元那一支由 _emit 写成 ``()``）。"""
    return _emit(access, node)


def _annotation(member):
    """A prose word, copied together with its prose."""
    if member.kind == TYPE_APP and member.payload.constructor_name in ANNOTATIONS:
        args = member.payload.args
        if len(args) == 1 and args[0].kind == CODE_BLOCK:
            return viba_ast.TypeApp(member.payload.constructor_name,
                                    [viba_ast.CodeBlock(args[0].payload.code)])
    raise SerializeGap(f"this prose word cannot be written: {member.kind}")
