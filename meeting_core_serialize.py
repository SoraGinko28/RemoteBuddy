"""meeting_core_serialize — writes meeting_core object data out as viba code
(a witness).

The contract (the note at the end of the "Stable types" chapter):

    rule_reflect ∘ meeting_core_serialize ≡ meeting_core_reflect

Both must hold:

1. The written code is a resident of that definition:
   ``is_compliant(result, the definition) == Ok(True)``.
2. Reading result with viba.rule.reflect reads exactly the same thing as
   handing the same data to meeting_core_reflect.

The map and the values are walked together: the shape comes from the descriptor
(the design), the values from the data. The prose words (Assert / Appendix / Hint)
are not part of the data shape and are written nil — they are units, so a definition
that carries them is still judged fine.

Three known gaps (all unspellable, give Err — do not force a wrong spelling):

1. Empty list / set / dict: a viba literal needs at least one element
   (``ListLiteral[]`` does not parse).
2. A generic alias does not accept a literal resident: under the spelling
   ``AppendOnlyList[T] := list[T]``, ``ListLiteral[...]`` fails
   ``is_compliant`` (writing ``list[T]`` directly passes).
3. The chat message's ``$raw_msg`` / ``$at_user_name`` branches: on the Python
   side these are the two classes ``RawMessage`` / ``AtUserMessage``, which
   match neither the design's tag nor its leaf type.
"""

from __future__ import annotations

import sys
from pathlib import Path

import meeting_core_reflect as _reflect

# viba is a submodule under its own root; add it to sys.path to import it.
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
    """No viba spelling for this part yet (the empty-container case)."""


def serialize(definition_name: str, data) -> Result:
    """Object data -> a chunk of viba source: a definition of the same name,
    whose body is the spelling of that data.

    ``viba_ast.parse(result)`` directly gives a usable witness.
    """
    given = witness(definition_name, data)
    if isinstance(given, Err):
        return given
    module = viba_ast.Module([viba_ast.TypeDefinition(definition_name, given.value)])
    return Ok(viba_ast.unparse(module) + "\n")


def witness(definition_name: str, data) -> Result:
    """Object data -> the witness AST (the code serialize writes out)."""
    access = _reflect.access(_reflect._definition(definition_name))
    root = access.root(_reflect._Record(data))
    if isinstance(root, Err):
        return root
    try:
        return Ok(_emit(access, root.value))
    except SerializeGap as gap:
        return Err(str(gap))


# ----------------------------------------------------------------------
# Walking the map and the values together
# ----------------------------------------------------------------------


def _emit(access, node):
    """The viba spelling of this part of the data."""
    shape = access._unfold(node.descriptor)
    if node.data is _reflect._NIL:
        return viba_ast.Nil()               # carries a unit: design writes nil
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
    raise SerializeGap(f"cannot write this piece out: {shape.kind}")


def _emit_sum(access, node):
    """Sum: write the selected branch; none selected is the default (nil)."""
    for tag, step, _ in access._member_steps(node):
        child = access.get(node, step)
        if not isinstance(child, Ok) or child.value is None:
            continue
        inner = _payload(access, child.value)
        return viba_ast.Tagged(tag, inner) if tag else inner
    if node.data is None:
        return viba_ast.Nil()               # the default branch (Optional nil)
    raise SerializeGap(f"no branch of this sum matches: {type(node.data).__name__}")


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
            raise SerializeGap(f"no value here: {tag}")
    return viba_ast.ProductChain(elements)


def _is_optional(access, step, node) -> bool:
    """Does this slot's design type carry nil (Optional[T] = T | nil)."""
    target = access._target_of(node, step, access._members(node))
    if target is None:
        return False
    shape = access._unfold(target)
    if shape.kind != SUM:
        return False
    return any(_reflect._is_unit_descriptor(element)
               for element in shape.payload.elements)


def _emit_container(access, node, container: str):
    """Container: a literal (ListLiteral / SetLiteral / DictLiteral)."""
    length = access.length(node)
    if isinstance(length, Err):
        raise SerializeGap(length.message)
    if length.value == 0:
        raise SerializeGap(
            f"an empty {container} has no viba spelling yet: a literal takes at least "
            f"one element")
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
    raise SerializeGap(f"cannot write this container out: {container}")


def _emit_leaf(data):
    """Built-in leaves: five kinds."""
    if data is None:
        return viba_ast.Nil()
    if isinstance(data, (bool, int, float, str)):
        return viba_ast.Constant(data)
    raise SerializeGap(f"cannot write this leaf out: {type(data).__name__}")


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


def _steps(access, node):
    return [step for _, step, _ in access._member_steps(node)]


def _child(access, node, step):
    given = access.get(node, step)
    if not isinstance(given, Ok) or given.value is None:
        raise SerializeGap(f"this step has no value: {step}")
    return given.value


def _payload(access, node):
    """The payload a branch carries (the unit branch is ``()`` per _emit)."""
    return _emit(access, node)


def _annotation(member):
    """A prose word, copied together with its prose."""
    if member.kind == TYPE_APP and member.payload.constructor_name in ANNOTATIONS:
        args = member.payload.args
        if len(args) == 1 and args[0].kind == CODE_BLOCK:
            return viba_ast.TypeApp(member.payload.constructor_name,
                                    [viba_ast.CodeBlock(args[0].payload.code)])
    raise SerializeGap(f"this prose word cannot be written: {member.kind}")
