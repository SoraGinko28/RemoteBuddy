"""meeting_core_serialize — writes meeting_core object data out as viba code
(a witness).

The contract (the note at the end of the "Stable types" chapter):

    rule_reflect ∘ meeting_core_serialize ≡ meeting_core_reflect

Both must hold:

1. The written code is a resident of that definition:
   ``is_compliant(result, the definition) == Ok(True)``.
2. Reading result with viba.reflect reads exactly the same thing as
   handing the same data to meeting_core_reflect.

The map and the values are walked together: the shape comes from the descriptor
(the design), the values from the data. The spelling itself comes from
viba.builder, so what is written is the language's canonical source and a slip
is refused where it is made. The prose words (Assert / Appendix / Hint) are not
part of the data shape and are written nil — they are units, so a definition
that carries them is still judged fine.

What the walk is given is a ``VibaNode``: the caller binds its own data into one
(``access.root(definition, data)``) and hands it over, so nothing below depends
on how the data is shaped or on this layer's own binding.

A piece of the design the data has no spelling for gives Err (SerializeGap):
this module writes what the design says and never forces a spelling. Which side
cannot be spelled — the design's definition, the judge, or the caller's binding
— is not something this module knows about, and no definition name is special
here.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

import meeting_core_reflect as _reflect

# viba is a submodule under its own root; add it to sys.path to import it.
_VENDORED_VIBA = Path(__file__).resolve().parent / "viba"
if (_VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file() and str(_VENDORED_VIBA) not in sys.path:
    sys.path.insert(0, str(_VENDORED_VIBA))

from viba import builder  # noqa: E402
from viba.reflect import at_index, at_key, by_field_index  # noqa: E402
from viba.type import Err, Ok, Result  # noqa: E402
from viba.viba_type_descriptor import (  # noqa: E402
    empty_pool,
    parse_viba_file,
    pool_add_file,
    pool_find_definition,
    NEVER,
    NIL,
    PRODUCT,
    SUM,
    TAGGED,
    TUPLE,
)

__all__ = ["serialize", "access_for"]


class SerializeGap(Exception):
    """No viba spelling for this part of the data."""


# Written names come out of a builder nobody defines anything on: `_NAMES.Object`
# is the name `Object`, `_NAMES.Object[...]` an application of it.
_NAMES = builder.Builder()


def serialize(name: str, access, node) -> Result:
    """A piece of the design in hand (a ``VibaNode``) -> viba source: the
    definition ``name`` whose body is that piece's spelling.

    Binding the caller's own data into a node is the caller's business —
    ``access.root(definition, data)`` gives the node to start from — so the walk
    below never sees anything but the accessor and the node.
    """
    return _source(name, access, node)


def access_for(type_expression: str):
    """(access, definition) for one type expression, resolved in meeting_core."""
    return _expression_definition(type_expression)


@lru_cache(maxsize=None)
def _expression_definition(type_expression: str):
    """(access, full name) for one type expression, resolved in meeting_core.

    The expression is written the way the data-interface chapter writes return
    types (``mc.Result[list[mc.datetime]]``); the ``mc.`` prefix is dropped so
    it resolves inside the stable-types module.
    """
    written = re.sub(r"\bmc\.", "", type_expression).strip()
    source = _reflect._design_source() + f"\nSdkEntry := {written}\n"
    pool = empty_pool()
    parsed = parse_viba_file(pool, source, "sdk_entry.viba", "meeting_core")
    if isinstance(parsed, Err):
        return Err(f"cannot compile the return type {type_expression!r}: {parsed.err_msg}")
    added = pool_add_file(pool, parsed.ok_value)
    if isinstance(added, Err):
        return added
    found = pool_find_definition(added.ok_value, "meeting_core.SdkEntry")
    if isinstance(found, Err):
        return found
    return Ok((_reflect.access, found.ok_value))


# ----------------------------------------------------------------------
# Walking the map and the values together
# ----------------------------------------------------------------------


def _source(name: str, access, node) -> Result:
    """The definition's source: what the node holds, written by the builder."""
    try:
        expression = _emit(access, node)
    except SerializeGap as gap:
        return Err(str(gap))
    vb = builder.Builder()
    setattr(vb, name, expression)
    try:
        builder.check(vb)               # read the written source back
    except ValueError as error:
        return Err(str(error))
    return Ok(str(vb))


def _emit(access, node):
    """This part of the data, as a builder expression.

    The value side is asked with the protocol's own cells, never by looking at
    what the binding put in the node: VibaLeaf answers "the value is itself this
    piece" (a literal, a unit branch, nil) and "walk one step further in" for
    the rest, which is exactly the question here.
    """
    shape = access._unfold(node.descriptor)
    given = access.leaf(node)
    if isinstance(given, Ok):
        return _emit_leaf(given.ok_value)
    if shape.kind == SUM:
        return _emit_sum(access, node)
    if shape.kind == PRODUCT:
        return _emit_product(access, node)
    if shape.kind == TUPLE:
        return tuple(_emit(access, _child(access, node, step))
                     for step in _steps(access, node))
    container = access._container_kind(shape)
    if container is not None:
        return _emit_container(access, node, container)
    if shape.kind == TAGGED:
        return _emit(access, _child(access, node, by_field_index(0)))
    if shape.kind in (NIL, NEVER):
        return None
    raise SerializeGap(f"cannot write this piece out: {shape.kind}")


def _emit_sum(access, node):
    """Sum: write the selected branch; none selected is the default (nil)."""
    for tag, step, _ in access.member_steps(node):
        child = access.get(node, step)
        if not isinstance(child, Ok) or child.ok_value is None:
            continue
        inner = _emit(access, child.ok_value)
        return inner if tag is None else _tag(tag)(inner)
    given = access.leaf(node)
    if isinstance(given, Ok):
        return _emit_leaf(given.ok_value)   # the sum's own unit: the default branch
    raise SerializeGap(f"no branch of this sum matches: {node!r}")


def _emit_product(access, node):
    """A product: tagged members are written out, the prose words are units."""
    written = [_NAMES.Object]
    for tag, step, _ in access.member_steps(node):
        if tag is None:
            written.append(None)        # a prose word: the unit
            continue
        child = access.get(node, step)
        if isinstance(child, Ok) and child.ok_value is not None:
            written.append(_tag(tag)(_emit(access, child.ok_value)))
        elif _may_be_nil(access, step, node):
            written.append(_tag(tag)(None))
        else:
            raise SerializeGap(f"no value here: {tag}")
    product = written[0]
    for element in written[1:]:
        product = product * element
    return product


def _may_be_nil(access, step, node) -> bool:
    """May this slot's written type be nil (a sum with a unit branch)?"""
    target = access._target_of(node, step, access._members(node))
    return target is not None and access.carries_nil(target)


def _emit_container(access, node, container: str):
    """Container: a literal (ListLiteral / SetLiteral / DictLiteral).

    The members are written in the order the data has them, not sorted: a set
    has no order of its own, but both readers walk it by position.
    """
    if container == "dict":
        keys = access.keys(node)
        if isinstance(keys, Err):
            raise SerializeGap(keys.err_msg)
        pairs = tuple((key, _emit(access, _child(access, node, at_key(key))))
                      for key in keys.ok_value)
        return _NAMES.DictLiteral[pairs]
    length = access.length(node)
    if isinstance(length, Err):
        raise SerializeGap(length.err_msg)
    payloads = tuple(_emit(access, _child(access, node, at_index(index)))
                     for index in range(length.ok_value))
    if container == "list":
        return _NAMES.ListLiteral[payloads]
    if container == "set":
        return _NAMES.SetLiteral[payloads]
    raise SerializeGap(f"cannot write this container out: {container}")


def _emit_leaf(value):
    """A leaf the language can write: nil, or one of the four literals."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    raise SerializeGap(f"cannot write this leaf out: {type(value).__name__}")


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


def _tag(name: str):
    """The builder's spelling of one written tag ($agent_name -> tag.agent_name)."""
    return getattr(builder.tag, name.lstrip("$"))


def _steps(access, node):
    return [step for _, step, _ in access.member_steps(node)]


def _child(access, node, step):
    given = access.get(node, step)
    if not isinstance(given, Ok) or given.ok_value is None:
        raise SerializeGap(f"this step has no value: {step}")
    return given.ok_value
