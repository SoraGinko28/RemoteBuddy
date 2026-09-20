"""meeting_core_reflect — read the stable types of MEETING_ADT.md as a map over a Python record.

This is the access side of viba-reflect.md, landing on the stable ADT:

- **Design** (the map): the "Stable types" chapter of MEETING_ADT.md (module
  name meeting_core), compiled into a descriptor pool by the descriptor side
  (viba.viba_type_descriptor). The map only concerns the document, not the
  implementation.
- **Material** (Data): the Python record in meeting_adt.py — dataclass
  instances, list / tuple / set / dict, and scalars. The protocol does not
  specify what Data looks like; this layer binds it to Python objects.

Names follow viba.reflect's convention for viba-reflect.md: class
names from the protocol are kept as they are (``VibaStep``, ``VibaPath``,
``VibaNode``, ``VibaAccess``), functions land in snake_case (``viba_resolve`` /
``viba_get_by_path`` / ``viba_list_fields``), the four address branches are
``by_tag`` / ``by_field_index`` / ``at_index`` / ``at_key``; the six cells live
on ``VibaAccess``: ``root`` / ``has`` / ``get`` / ``leaf`` / ``length`` /
``keys``.

Only what the protocol leaves unnamed is this layer's own:

    the Data parameter           -> _Record: one Python record
    section 5.4 "throw directly" -> VibaReflectError (5.4's Python landing name)
    reading the map and traversal -> underscore methods on VibaAccess and this
                                     module's underscore functions
    where the design is read from -> the private _design_source / _design_hash /
                                     _design_pool / _definition

Two rules from the protocol:

* ``root`` only pairs (descriptor, data). The version protocol does not care:
  which version of the design a piece of material was made against is the upper
  layer's own business.
* ``has`` only answers true or false — no such coordinate in the design and no
  such piece in the data are both ``False``.
  In ``get``, "not in the data" is ``Ok(nil)``; only "no such coordinate on the
  map at all" is ``Err``; ``leaf`` / ``length`` / ``keys`` give ``Err`` when
  they get nothing.

The entry point starts as the protocol says:
``access(_definition(name)).root(_Record(record))``, and the ``Ok`` that comes
back holds the node (``Err`` means it never matched at all). The protocol's
``Ok`` / ``Err`` are exactly the two this module exposes.

The Python landing of section 5.4 is on VibaNode: ``get_{name}()`` /
``has_{name}()`` / ``in`` / ``try_get_{name}()`` / ``node[i]`` / ``node.value``
/ ``len(node)`` / iteration / ``keys()`` / ``values()`` / ``items()``. The
value-taking kind raises; the asking-whether kind never raises.

How the data matches the design (this layer's binding rules):

* a dataclass field name is the tag with ``$`` removed: ``$file_id`` matches
  ``file_id``.
* containers: list / tuple / set go by index, dict goes by key (keys compared
  as strings).
* a sum-type branch matches by three things: the data's class name is the
  definition the branch points to (``$screen_ref`` matches ``ScreenReference``);
  the branch name matches the class name (``$ok`` / ``$err`` match ``Ok`` /
  ``Err``); or the branch is the unit and the enum matches this tag
  (``$deleted ()`` matches ``DataStatus.DELETED``, ``$generator ()`` matches
  ``AgentRole.GENERATOR`` — the value, the value with $, and the member name all
  count). Names are compared case-insensitively.
* built-in leaves are recognised as Python scalars: bool / int / float / str /
  None.

A mismatch means nothing can be read (``Err`` or ``False``) — that is the
Python side disagreeing with the document (for example wrapping ``$palette
int`` in a ``PaletteColor`` class), not this layer's business.
"""

from __future__ import annotations

import hashlib
import re
import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

# viba is a submodule, bundled under its own root directory; adding that root to
# sys.path is what makes the import findable.
_VENDORED_VIBA = Path(__file__).resolve().parent / "viba"
if (_VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file() and str(_VENDORED_VIBA) not in sys.path:
    sys.path.insert(0, str(_VENDORED_VIBA))

from viba import viba_ast
from viba.type import AstNodeType, Err, Ok, Result, module_get_type
from viba.viba_type_descriptor import (
    CODE_BLOCK,
    ELLIPSIS,
    EXPONENT,
    LITERAL,
    NEVER,
    NIL,
    PRODUCT,
    SUM,
    TAGGED,
    TUPLE,
    TYPE_APP,
    TYPE_REF,
    VibaChainDescriptor,
    VibaDefinitionDescriptor,
    VibaPool,
    VibaTaggedDescriptor,
    VibaTupleDescriptor,
    VibaTypeAppDescriptor,
    VibaTypeDescriptor,
    empty_pool,
    parse_viba_file,
    pool_add_file,
    pool_find_definition,
)

DESIGN_FILE_NAME = "MEETING_ADT.md"
DESIGN_MODULE_NAME = "meeting_core"
DESIGN_CHAPTER = "Stable types"
DESIGN_PATH = Path(__file__).with_name(DESIGN_FILE_NAME)

CONTAINERS = ("list", "set", "dict")
LEAF_NAMES = ("bool", "int", "float", "str", "nil", "void", "None")
SCALAR_KINDS = (bool, int, float, str)
_MISSING = object()
_NIL = object()  # explicit nil: the branch matched and what it holds is nil


# ----------------------------------------------------------------------
# Design: compile the descriptor pool out of the "Stable types" chapter
# ----------------------------------------------------------------------


def _normalize(source: str) -> str:
    return source.replace("\r\n", "\n").replace("\r", "\n")


@lru_cache(maxsize=1)
def _design_source() -> str:
    """The design source: the code block of the "Stable types" chapter."""
    text = _normalize(DESIGN_PATH.read_text(encoding="utf-8"))
    heading = re.search(r"(?m)^##\s*" + re.escape(DESIGN_CHAPTER), text)
    if heading is None:
        raise VibaReflectError(f"{DESIGN_FILE_NAME} has no chapter named {DESIGN_CHAPTER!r}")
    fence = text.index("```", heading.end())
    body_start = text.index("\n", fence) + 1
    body_end = text.index("\n```", body_start)
    return text[body_start:body_end].rstrip("\n") + "\n"


@lru_cache(maxsize=1)
def _design_hash() -> str:
    """Hash of this version of the design: newlines normalised to \\n, then
    SHA-256 over the UTF-8 bytes."""
    return hashlib.sha256(_design_source().encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _design_pool() -> VibaPool:
    """Compile this chapter into a pool; the pool is the descriptor side's
    map."""
    pool = empty_pool()
    parsed = parse_viba_file(pool, _design_source(), DESIGN_FILE_NAME, DESIGN_MODULE_NAME)
    if isinstance(parsed, Err):
        raise VibaReflectError(f"the {DESIGN_CHAPTER} chapter does not compile: {parsed.err_msg}")
    added = pool_add_file(pool, parsed.ok_value)
    if isinstance(added, Err):
        raise VibaReflectError(f"the {DESIGN_CHAPTER} chapter does not go into the pool: {added.err_msg}")
    return added.ok_value


@lru_cache(maxsize=None)
def _definition(name: str) -> VibaDefinitionDescriptor:
    """Look up a definition descriptor by name; both ``'FileId'`` and
    ``'meeting_core.FileId'`` work."""
    full_name = name if "." in name else f"{DESIGN_MODULE_NAME}.{name}"
    found = pool_find_definition(_design_pool(), full_name)
    if isinstance(found, Err):
        raise VibaReflectError(found.err_msg)
    return found.ok_value


@lru_cache(maxsize=None)
def _expression_definition(type_expression: str):
    """(access, definition) for one type expression, resolved in the design.

    The expression is written the way the data-interface chapter writes return
    types (``mc.Result[list[mc.datetime]]``); the ``mc.`` prefix is dropped so
    it resolves inside the stable-types module.
    """
    written = re.sub(r"\bmc\.", "", type_expression).strip()
    source = _design_source() + f"\nSdkEntry := {written}\n"
    pool = empty_pool()
    parsed = parse_viba_file(pool, source, "sdk_entry.viba", DESIGN_MODULE_NAME)
    if isinstance(parsed, Err):
        return Err(f"cannot compile the return type {type_expression!r}: {parsed.err_msg}")
    added = pool_add_file(pool, parsed.ok_value)
    if isinstance(added, Err):
        return added
    found = pool_find_definition(added.ok_value, f"{DESIGN_MODULE_NAME}.SdkEntry")
    if isinstance(found, Err):
        return found
    return Ok((access, found.ok_value))


def access_for(type_expression: str):
    """(access, definition) for one type expression, resolved in the design."""
    return _expression_definition(type_expression)


# ----------------------------------------------------------------------
# VibaStep and VibaPath
# ----------------------------------------------------------------------


class VibaStep:
    """One step of an address: one of the four branches."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return f"{self.kind}({self.value!r})"

    def __eq__(self, other):
        return isinstance(other, VibaStep) and (self.kind, self.value) == (other.kind, other.value)

    def __hash__(self):
        return hash((self.kind, self.value))


def by_tag(name: str) -> VibaStep:
    return VibaStep("by_tag", _tag_of(name))


def by_field_index(index: int) -> VibaStep:
    return VibaStep("by_field_index", index)


def at_index(index: int) -> VibaStep:
    return VibaStep("at_index", index)


def at_key(key: str) -> VibaStep:
    return VibaStep("at_key", key)


VibaPath = List[VibaStep]


def _tag_of(name: str) -> str:
    """In chain style the name has no $; one with $ also works."""
    return name if name.startswith("$") else "$" + name


def _tag_name(tag: str) -> str:
    return tag[1:] if tag.startswith("$") else tag


class VibaReflectError(Exception):
    """Raised when the value-taking path fails, carrying the protocol's ``Err``
    message.

    The section 5.4 Python landing: the value-taking kind raises this on
    failure, with the ``Err`` message kept as it is.
    """


# ----------------------------------------------------------------------
# Binding of Data
# ----------------------------------------------------------------------


class _Record:
    """This layer's binding of the protocol's Data parameter: one Python record.

    The protocol does not specify what Data looks like. What is bound here is
    the record in meeting_adt.py (dataclass instances, containers and scalars
    all count). Version and the like are carried by the upper layer; the
    protocol does not care.
    """

    __slots__ = ("record",)

    def __init__(self, record):
        self.record = record

    def __repr__(self):
        return f"_Record({type(self.record).__name__})"


# ----------------------------------------------------------------------
# VibaNode[Data]
# ----------------------------------------------------------------------


class VibaNode:
    """One node: a piece of descriptor on the design side, a piece of data on
    the implementation side.

    ``path`` is the VibaPath walked from the entry point to here (the
    protocol's node has no such slot; this layer keeps it so addresses can be
    stored, printed and compared).
    """

    __slots__ = ("_access", "descriptor", "data", "path")

    def __init__(self, access: "VibaAccess", descriptor: VibaTypeDescriptor, data,
                 path: Sequence[VibaStep] = ()):
        self._access = access
        self.descriptor = descriptor
        self.data = data
        self.path = tuple(path)

    def __repr__(self):
        where = ".".join(repr(step) for step in self.path) or "root"
        return f"VibaNode({where})"

    # ---- Section 5.4 node accessors: the value-taking path raises ----

    def by_tag(self, name: str) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, by_tag(name)))

    def by_field_index(self, index: int) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, by_field_index(index)))

    def at_index(self, index: int) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, at_index(index)))

    def at_key(self, key: str) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, at_key(key)))

    @property
    def leaf(self):
        """The node's $leaf cell: the literal this piece carries."""
        return self._access._unwrap(self._access.leaf(self))

    @property
    def value(self):
        """The section 5.4 Python landing: ``leaf`` lands on the naked value
        (bool / int / float / str / None)."""
        return self.leaf

    # ---- Shape: only the written chain head, names are not unfolded ----

    @property
    def is_list(self) -> bool:
        return self._access.container_kind(self.descriptor) == "list"

    @property
    def is_set(self) -> bool:
        return self._access.container_kind(self.descriptor) == "set"

    @property
    def is_dict(self) -> bool:
        return self._access.container_kind(self.descriptor) == "dict"

    def __len__(self) -> int:
        return self._access._unwrap(self._access.length(self))

    def keys(self) -> List[str]:
        return list(self._access._unwrap(self._access.keys(self)))

    def values(self):
        return [self[key] for key in self.keys()]

    def items(self):
        return [(key, self[key]) for key in self.keys()]

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def __getitem__(self, key):
        if isinstance(key, int) and not isinstance(key, bool):
            return self._access._unwrap(self._access.get(self, at_index(key)))
        return self._access._unwrap(self._access.get(self, at_key(str(key))))

    # ---- The Python landing's magic methods ----

    def _has_tag(self, name: str) -> bool:
        given = self._access.has(self, by_tag(name))
        return bool(given.ok_value) if isinstance(given, Ok) else False

    def _has_field(self, index: int) -> bool:
        given = self._access.has(self, by_field_index(index))
        return bool(given.ok_value) if isinstance(given, Ok) else False

    def __contains__(self, name: str) -> bool:
        return self._has_tag(name)

    def __getattr__(self, name: str):
        if name.startswith("get_field_"):
            return lambda: self.by_field_index(int(name[len("get_field_"):]))
        if name.startswith("has_field_"):
            return lambda: self._has_field(int(name[len("has_field_"):]))
        if name.startswith("try_get_field_"):
            return lambda: self._access.get(
                self, by_field_index(int(name[len("try_get_field_"):])))
        if name.startswith("get_"):
            return lambda: self.by_tag(name[len("get_"):])
        if name.startswith("has_"):
            return lambda: self._has_tag(name[len("has_"):])
        if name.startswith("try_get_"):
            return lambda: self._access.get(self, by_tag(name[len("try_get_"):]))
        raise AttributeError(name)

    def __dir__(self):
        names = ["value", "leaf", "is_list", "is_set", "is_dict",
                 "keys", "values", "items", "path", "descriptor", "data"]
        positional = 0
        for tag, _ in self._access.members(self) or []:
            if tag:
                short = tag[1:]
                names += [f"get_{short}", f"has_{short}", f"try_get_{short}"]
            else:
                names += [f"get_field_{positional}", f"has_field_{positional}",
                          f"try_get_field_{positional}"]
                positional += 1
        return sorted(names)


# ----------------------------------------------------------------------
# VibaAccess[Data]
# ----------------------------------------------------------------------


class Config:
    """Which written names stand for the units ``never`` and ``nil``.

    ``never_eqv`` holds the names equivalent to ``never``, the sum's unit, and
    ``nil_eqv`` the names equivalent to ``nil``, the product's unit. The units
    themselves are units by kind, so naming them again changes nothing. Whoever
    builds an accessor fills these in.
    """

    def __init__(self, never_eqv=(), nil_eqv=()):
        self.never_eqv = frozenset(str(name).lstrip("$") for name in never_eqv)
        self.nil_eqv = frozenset(str(name).lstrip("$") for name in nil_eqv)

    def __repr__(self):
        return (f"Config(never_eqv={sorted(self.never_eqv)!r}, "
                f"nil_eqv={sorted(self.nil_eqv)!r})")

    def is_unit_name(self, name: str) -> bool:
        """Whether this written name is one of the units."""
        return name in self.never_eqv or name in self.nil_eqv


class VibaAccess:
    """An accessor built from a Config: the six cells of section 5.2, plus this
    layer's map-reading helpers."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

    def __repr__(self):
        return f"VibaAccess({self.config!r})"

    # ---- The protocol's six cells ----

    def root(self, definition: VibaDefinitionDescriptor, data: _Record) -> Result:
        """The entry point: gives the (descriptor, data) pair."""
        return Ok(VibaNode(self, definition.body, data.record))

    def has(self, node: VibaNode, step: VibaStep) -> Result:
        """VibaHas: whether this step exists. No such coordinate in the design
        and no such piece in the data are both false."""
        slots = self.members(node)
        if not self._knows(node, step, slots):
            return Ok(False)
        target = self.target_of(node, step, slots)
        return Ok(self._pick(node, step, target) is not None)

    def get(self, node: VibaNode, step: VibaStep) -> Result:
        """VibaGet: walk one step in. Not in the data is Ok(nil); only no such
        coordinate on the map is Err."""
        slots = self.members(node)
        if not self._knows(node, step, slots):
            return Err(f"the design has no such address: {step} ({node!r})")
        target = self.target_of(node, step, slots)
        if target is None:
            return Err(f"the design has no such address: {step} ({node!r})")
        piece = self._pick(node, step, target)
        if piece is None:
            return Ok(None)
        return Ok(VibaNode(self, target, piece, tuple(node.path) + (step,)))

    def leaf(self, node: VibaNode) -> Result:
        """VibaLeaf: walk to a leaf and take its value, five kinds.

        Sums, products, tuples and containers are not yet leaves — they have to
        be walked one step further in; a value that is itself nil (an explicit
        unit branch, or a field that is present but holds nil) also counts as a
        leaf.
        """
        data = node.data
        if data is _NIL:
            return Ok(None)
        if data is None or isinstance(data, SCALAR_KINDS):
            shape = self.unfold(node.descriptor)
            if (shape.kind in (PRODUCT, TUPLE)
                    or self.container_kind(shape) is not None):
                return Err(f"this piece is not a leaf: {node!r}")
            if shape.kind == SUM and not self._branch_is_a_leaf(node):
                # A sum has to be walked one step further in: the $agent_name
                # branch is the leaf; only the literal kind of sum (Style) and
                # Optional[float] are "the value is itself that branch"
                return Err(f"this piece is not a leaf: {node!r}")
            return Ok(data)
        return Err(f"this piece is not a leaf: {node!r}")

    def length(self, node: VibaNode) -> Result:
        """VibaLength: the number of elements of a list / set / tuple, the
        number of keys of a dict."""
        if not isinstance(node.data, (list, tuple, set, frozenset, dict)):
            return Err(f"this piece is not a container: {node!r}")
        return Ok(len(node.data))

    def keys(self, node: VibaNode) -> Result:
        """VibaKeys: the dict's keys, in implementation order."""
        if not isinstance(node.data, dict):
            return Err(f"this piece is not a dict: {node!r}")
        return Ok([str(key) for key in node.data.keys()])


    def resolve(self, node: VibaNode, path: Sequence[VibaStep]) -> Result:
        """VibaResolve: walk VibaGet step by step.

        Reaching "the data has no such piece" (``Ok(nil)``) stops there: if steps
        remain it is ``Err("this step has no value")``, and if no steps remain the
        ``Ok(nil)`` is handed out.
        """
        current = node
        for step in path:
            if current is None:
                return Err("this step has no value")
            given = node._access.get(current, step)
            if isinstance(given, Err):
                return given
            current = given.ok_value
        return Ok(current)

    def get_by_path(self, node: VibaNode, path: Sequence[VibaStep]) -> Result:
        """VibaGetByPath: VibaResolve first, then VibaLeaf."""
        resolved = self.resolve(node, path)
        if isinstance(resolved, Err):
            return resolved
        if resolved.ok_value is None:
            return Err("this step has no value")
        return node._access.leaf(resolved.ok_value)

    def list_fields(self, node: VibaNode, definition: VibaDefinitionDescriptor) -> Result:
        """VibaListFields: VibaGet one by one over DefinitionMembers, what is
        taken goes into the list."""
        accessor = self
        members = _definition_members(definition)
        if isinstance(members, Err):
            return members
        out = []
        positional = 0
        for member in members.ok_value:
            if member.tag:
                step = by_tag(member.tag)
            else:
                if accessor._is_unit_member(member):
                    continue
                step = by_field_index(positional)
                positional += 1
            given = accessor.get(node, step)
            # what is taken goes into the list; what is missing (including this
            # piece not being a product/sum at all, so nothing can be asked) does
            # not.
            if isinstance(given, Ok) and given.ok_value is not None:
                out.append(given.ok_value)
        return Ok(out)

    # ---- Private: the value-taking path (raises on both Err and Ok(nil)) ----

    def _unwrap(self, given: Result):
        if isinstance(given, Err):
            raise VibaReflectError(given.err_msg)
        if given.ok_value is None:
            raise VibaReflectError("this piece has no value")
        return given.ok_value

    # ---- Private: the map (descriptors read into an addressable shape) ----

    def unfold(self, descriptor: VibaTypeDescriptor,
                seen: Optional[set] = None) -> VibaTypeDescriptor:
        """Unfold the piece written as a name into an addressable shape.

        Two things, both by the definitions in the pool: a name unfolds into its
        definition body; a generic application substitutes its arguments and
        lands on the definition body too. Sums / products / exponents follow one
        rule, no other exceptions.
        """
        seen = seen or set()
        if id(descriptor) in seen:
            return descriptor
        seen = seen | {id(descriptor)}
        if descriptor.kind == TYPE_REF:
            resolved = self._resolve_ref(descriptor)
            if resolved is None:
                return descriptor
            return self.unfold(resolved, seen)
        if descriptor.kind == TYPE_APP:
            applied = self._apply_generic(descriptor)
            if applied is not None:
                return self.unfold(applied, seen)
        return descriptor

    def _apply_generic(self, descriptor: VibaTypeDescriptor) -> Optional[VibaTypeDescriptor]:
        """When the constructor points at a generic definition, give the
        definition body descriptor with the arguments substituted in."""
        payload = descriptor.payload
        if payload.resolvable_type is None:
            return None
        resolved = module_get_type(payload.resolvable_type.container_module,
                                   payload.constructor_name)
        if isinstance(resolved, Err) or not isinstance(resolved.ok_value, AstNodeType):
            return None
        target = resolved.ok_value
        if not isinstance(target.ast_node, viba_ast.GenericDefinition):
            return None
        params = list(target.ast_node.generic_params or [])
        if len(params) != len(payload.args):
            return None
        from viba.viba_type_descriptor import _build_type

        body_node = viba_ast.convert_to_chain_style(target.ast_node.body)
        body = _build_type(descriptor.payload.pool, target.container_module, body_node)
        return self._substitute(body, dict(zip(params, payload.args)))

    def _substitute(self, descriptor: VibaTypeDescriptor,
                    bindings: dict) -> VibaTypeDescriptor:
        """Replace parameters by name with argument descriptors; only the branch
        written as a name is touched."""
        payload = descriptor.payload
        if descriptor.kind == TYPE_REF:
            return bindings.get(payload.type_name, descriptor)
        if descriptor.kind in (PRODUCT, SUM, EXPONENT):
            return VibaTypeDescriptor(descriptor.kind, VibaChainDescriptor(
                payload.pool, payload.resolvable_type,
                [self._substitute(e, bindings) for e in payload.elements]))
        if descriptor.kind == TYPE_APP:
            return VibaTypeDescriptor(TYPE_APP, VibaTypeAppDescriptor(
                payload.pool, payload.resolvable_type, payload.constructor_name,
                [self._substitute(a, bindings) for a in payload.args]))
        if descriptor.kind == TUPLE:
            return VibaTypeDescriptor(TUPLE, VibaTupleDescriptor(
                payload.pool, payload.resolvable_type,
                [self._substitute(e, bindings) for e in payload.elements]))
        if descriptor.kind == TAGGED:
            return VibaTypeDescriptor(TAGGED, VibaTaggedDescriptor(
                payload.pool, payload.resolvable_type, payload.tag,
                self._substitute(payload.tagged_type, bindings)))
        return descriptor

    def _resolve_ref(self, descriptor: VibaTypeDescriptor) -> Optional[VibaTypeDescriptor]:
        """When a name points at a transparent definition, give that definition
        body's descriptor."""
        resolvable = descriptor.payload.resolvable_type
        if resolvable is None:
            return None
        resolved = module_get_type(resolvable.container_module, descriptor.payload.type_name)
        if isinstance(resolved, Err) or not isinstance(resolved.ok_value, AstNodeType):
            return None  # built-in leaves, generic parameters etc: stop here
        target = resolved.ok_value
        if not isinstance(target.ast_node, viba_ast.TypeDefinition):
            return None  # a generic definition needs arguments; bare names stop
        from viba.viba_type_descriptor import _build_type

        return _build_type(descriptor.payload.pool, target.container_module, target.ast_node.body)

    def container_kind(self, descriptor: VibaTypeDescriptor) -> Optional[str]:
        """The three built-in containers list / set / dict; a tuple does not
        count, it is a product aligned by position."""
        if descriptor.kind == TYPE_APP and descriptor.payload.constructor_name in CONTAINERS:
            return descriptor.payload.constructor_name
        return None

    def _has_elements_by_index(self, descriptor: VibaTypeDescriptor) -> bool:
        """What can be counted and taken by index: list / set / tuple.

        A dict does not count: its elements are taken with keys + at_key, so
        asking by index means there is no such coordinate on the map.
        """
        return self.container_kind(descriptor) in ("list", "set") or descriptor.kind == TUPLE

    def members(self, node: VibaNode) -> Optional[List[tuple]]:
        """Which members this piece has: [(tag or None, descriptor), ...]; None
        when it has no members.

        Sum chains, product chains and exponent chains follow one rule: the
        members are $elements; a chain head that is the unit does not count, and
        a member the design writes as the product's own unit is not a member at
        all (the prose words are written that way).
        """
        descriptor = self.unfold(node.descriptor)
        elements = None
        if descriptor.kind in (PRODUCT, SUM, EXPONENT):
            elements = list(descriptor.payload.elements)
        elif descriptor.kind == TAGGED:
            return [(descriptor.payload.tag, descriptor.payload.tagged_type)]
        if elements is None:
            return None
        if elements and _is_unit_descriptor(elements[0], self.config):
            elements = elements[1:]
        slots = []
        for element in elements:
            if element.kind == TAGGED:
                slots.append((element.payload.tag, element.payload.tagged_type))
            elif descriptor.kind == PRODUCT and self._is_product_unit(element):
                continue                # a unit: the design writes no member here
            else:
                slots.append((None, element))
        return slots

    def _is_product_unit(self, descriptor) -> bool:
        """The product's unit: nil by kind, or a name the config calls nil.
        never is the sum's unit and is no product unit."""
        shape = self.unfold(descriptor)
        if shape.kind == NIL:
            return True
        return (shape.kind == TYPE_REF
                and shape.payload.type_name in self.config.nil_eqv)

    def inline_cycle(self, descriptor) -> Optional[str]:
        """The name a product's inline chain comes back to, or None.

        This landing does not splice an untagged member's own product into the
        one around it, so no chain can come back to where it started: what a
        product writes as an untagged member stays that member.
        """
        return None

    def carries_nil(self, descriptor: VibaTypeDescriptor) -> bool:
        """Whether this written type admits nil.

        Unfolded, that is a sum with a branch that is the product unit: a writer
        asking whether a slot with no value may be written ``nil``.
        """
        shape = self.unfold(descriptor)
        if shape.kind != SUM:
            return False
        return any(_is_unit_descriptor(element, self.config)
                   for element in shape.payload.elements)

    def _is_unit_descriptor(self, descriptor) -> bool:
        """A unit chain head: nil / never by kind, the empty tuple, or a name
        the config calls one — asked of the name it lands on, a name being an
        alias of its body."""
        descriptor = self.unfold(descriptor)
        if descriptor.kind in (NIL, NEVER):
            return True
        if descriptor.kind == TUPLE and not descriptor.payload.elements:
            return True
        return (descriptor.kind == TYPE_REF
                and self.config.is_unit_name(descriptor.payload.type_name))

    def member_steps(self, node: VibaNode) -> List[tuple]:
        """[(tag or None, that step, descriptor)]: the members of this piece,
        each with the step that takes you there; a None tag goes by position.

        The step is what VibaGet takes, so a writer walks a material the same
        way a reader does.
        """
        out = []
        positional = 0
        for tag, member in self.members(node) or []:
            if tag:
                out.append((tag, by_tag(tag), member))
            else:
                out.append((None, by_field_index(positional), member))
                positional += 1
        return out

    def _bare_sum(self, descriptor: VibaTypeDescriptor) -> Optional[List[tuple]]:
        """The positional branches of an untagged sum a material may write
        without the sum's own layer: [(index, shape, descriptor), ...], else None.

        A sum with at most one inner node can be written without that layer: the
        material carries the branch's content straight, and leaf branches are
        told apart by the value itself. Two inner nodes cannot be told apart, and
        a tagged sum is addressed by tag, so neither qualifies.
        """
        shape = self.unfold(descriptor)
        if shape.kind != SUM:
            return None
        elements = list(shape.payload.elements)
        if elements and _is_unit_descriptor(elements[0], self.config):
            elements = elements[1:]
        members, inner = [], 0
        for index, element in enumerate(elements):
            if element.kind == TAGGED:
                return None
            branch = self._branch_shape(element)
            inner += 1 if branch == "inner" else 0
            members.append((index, branch, element))
        return members if inner <= 1 else None

    def _branch_shape(self, descriptor: VibaTypeDescriptor) -> str:
        """How one branch of a sum reads: "unit", "leaf" or "inner"."""
        shape = self.unfold(descriptor)
        if shape.kind in (NIL, NEVER):
            return "unit"
        if shape.kind == LITERAL:
            return "leaf"
        if shape.kind == TYPE_REF and shape.payload.type_name in LEAF_NAMES:
            return "leaf"
        return "inner"

    def _bare_sum_tag_target(self, descriptor: VibaTypeDescriptor, tag: str):
        """The descriptor a tag names when it lives in a skipped sum's branch."""
        members = self._bare_sum(descriptor)
        if members is None:
            return None
        for _, branch_shape, branch in members:
            if branch_shape != "inner":
                continue
            for branch_tag, branch_type in self._design_members(branch) or []:
                if _tag_name(branch_tag) == _tag_name(tag):
                    return branch_type
        return None

    def _knows(self, node: VibaNode, step: VibaStep, slots: Optional[List[tuple]]) -> bool:
        if step.kind == "by_tag":
            if any(tag == step.value for tag, _ in slots or []):
                return True
            return self._bare_sum_tag_target(node.descriptor, step.value) is not None
        if step.kind == "by_field_index":
            positional = [tag for tag, _ in slots or [] if tag is None]
            return 0 <= step.value < len(positional)
        if step.kind == "at_index":
            return self._has_elements_by_index(self.unfold(node.descriptor))
        if step.kind == "at_key":
            return self.container_kind(self.unfold(node.descriptor)) == "dict"
        return False

    def target_of(self, node: VibaNode, step: VibaStep, slots: Optional[List[tuple]]):
        if step.kind == "by_tag":
            for tag, descriptor in slots or []:
                if tag == step.value:
                    return descriptor
            return self._bare_sum_tag_target(node.descriptor, step.value)
        if step.kind == "by_field_index":
            positional = [descriptor for tag, descriptor in slots or [] if tag is None]
            if 0 <= step.value < len(positional):
                return positional[step.value]
            return None
        if step.kind == "at_index":
            shape = self.unfold(node.descriptor)
            if not self._has_elements_by_index(shape):
                return None
            if shape.kind == TUPLE:
                if 0 <= step.value < len(shape.payload.elements):
                    return shape.payload.elements[step.value]
                return None
            return shape.payload.args[0]
        if step.kind == "at_key":
            shape = self.unfold(node.descriptor)
            if self.container_kind(shape) != "dict":
                return None
            return shape.payload.args[1]
        return None

    # ---- Private: the data side (the Python record) ----

    def _pick(self, node: VibaNode, step: VibaStep,
              target: Optional[VibaTypeDescriptor] = None):
        """The piece in the data that corresponds; None when there is none.

        Taking by tag takes a record field; when that fails it then checks
        whether this tag is a sum-type branch (the data's class name against the
        definition name, the enum value against the tag). Taking by position is
        a tuple index; when the data is not a tuple it likewise checks whether
        it is the branch for this position.
        """
        data = node.data
        if step.kind == "by_tag":
            tag_name = _tag_name(step.value)
            # A sum type recognises its branch first: whichever branch matches
            # is the one walked. A same-named field in the data does not count
            # (VersionedFileContentReference itself has $versioned_file_ref).
            if self.unfold(node.descriptor).kind != SUM:
                found = _attribute(data, tag_name)
                if found is not _MISSING:
                    return _present(found)      # field present; nil counts too
            if target is not None and self._shape_matches(target, data, tag_name):
                return _payload(target, data)
            return None
        if step.kind == "by_field_index":
            if isinstance(data, (tuple, list)) and 0 <= step.value < len(data):
                return _present(data[step.value])
            if target is not None and self._shape_matches(target, data, None):
                return _payload(target, data)
            return None
        if step.kind == "at_index":
            elements = self._elements_of(data)
            if elements is None or not (0 <= step.value < len(elements)):
                return None
            return _present(elements[step.value])
        if step.kind == "at_key":
            if not isinstance(data, dict):
                return None
            for key, value in data.items():
                if str(key) == str(step.value):
                    return _present(value)
            return None
        return None

    def _branch_is_a_leaf(self, node: VibaNode) -> bool:
        """Whether the sum branch that matched is itself a leaf shape (literal /
        built-in leaf / unit)."""
        for tag, step, member in self.member_steps(node):
            if self._pick(node, step, member) is None:
                continue
            if tag:
                return False        # tagged branch: value hangs under that tag
            shape = self.unfold(member)
            if shape.kind in (LITERAL, NIL, NEVER):
                return True
            return (shape.kind == TYPE_REF
                    and shape.payload.type_name in LEAF_NAMES)
        return False

    def _shape_matches(self, descriptor: VibaTypeDescriptor, data,
                       tag_name: Optional[str]) -> bool:
        """Whether this piece of data is this piece of descriptor.

        Four ways to recognise: the data's class name is the definition this
        piece points to (``ScreenReference`` for
        ``$screen_ref ScreenReference``, ``Rgb`` for ``$truecolor Rgb``); the
        enum value is called exactly this tag (``DataStatus.DELETED`` for
        ``$deleted ()``); a sum type recurses into its branches; and finally
        a built-in leaf against a Python scalar.
        """
        if tag_name is not None:
            if isinstance(data, Enum) and _enum_matches(data, tag_name):
                return True
            # branch name matches the data's class name
            # ($ok / $err for Ok / Err)
            if tag_name.lower() == type(data).__name__.lower():
                return True
        if descriptor.kind == TYPE_REF:
            written = descriptor.payload.type_name
            if written.lower() == type(data).__name__.lower():
                return True
            unfolded = self.unfold(descriptor)
            if unfolded is not descriptor:
                return self._shape_matches(unfolded, data, tag_name)
            return _leaf_matches(written, data)
        if descriptor.kind == TYPE_APP:
            if descriptor.payload.constructor_name.lower() == type(data).__name__.lower():
                return True
            unfolded = self.unfold(descriptor)
            return unfolded is not descriptor and self._shape_matches(unfolded, data, tag_name)
        if descriptor.kind == TAGGED:
            tag = _tag_name(descriptor.payload.tag)
            if isinstance(data, Enum) and _enum_matches(data, tag):
                return True
            if _is_unit_descriptor(self.unfold(descriptor.payload.tagged_type), self.config):
                return False        # unit branch: only tag / enum value match
            return self._shape_matches(descriptor.payload.tagged_type, data, tag)
        if descriptor.kind == SUM:
            return any(self._shape_matches(element, data, _element_tag(element))
                       for element in descriptor.payload.elements)
        if descriptor.kind == LITERAL:
            return _literal_matches(descriptor.payload.value, data)
        if descriptor.kind in (NIL, NEVER):
            return data is None
        return False

    def _elements_of(self, data) -> Optional[List]:
        if isinstance(data, (list, tuple, set, frozenset)):
            return list(data)
        return None

    # ---- Private: member traversal (used by VibaListFields and walk) ----

    def _is_unit_member(self, member) -> bool:
        written = _member_type_name(member)
        return written is not None and self.config.is_unit_name(written)

    def _walk(self, node: VibaNode) -> List[VibaNode]:
        """Walk every coordinate reachable on this map (used for the check
        list)."""
        out = [node]
        positional = 0
        members = self.members(node) or []
        for tag, _ in members:
            if tag:
                step = by_tag(tag)
            else:
                step = by_field_index(positional)
                positional += 1
            given = self.get(node, step)
            if isinstance(given, Ok) and given.ok_value is not None:
                out += self._walk(given.ok_value)
        shape = self.unfold(node.descriptor)
        if self.container_kind(shape) == "dict":
            given_keys = self.keys(node)
            if isinstance(given_keys, Ok):
                for key in given_keys.ok_value:
                    given = self.get(node, at_key(key))
                    if isinstance(given, Ok) and given.ok_value is not None:
                        out += self._walk(given.ok_value)
        elif self._has_elements_by_index(shape):
            given_length = self.length(node)
            if isinstance(given_length, Ok):
                for index in range(given_length.ok_value):
                    given = self.get(node, at_index(index))
                    if isinstance(given, Ok) and given.ok_value is not None:
                        out += self._walk(given.ok_value)
        return out


# ----------------------------------------------------------------------
# Section 5.3 convenience functions
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Entry point: this layer's default accessor
# ----------------------------------------------------------------------

# The language layer's units: what viba/type.viba writes for the sum unit and
# the product unit. Our design defines the three prose words as Object too, so
# they are units as well.
DEFAULT_CONFIG = Config(never_eqv={"Oneof"},
                        nil_eqv={"Object", "Assert", "Appendix", "Hint"})

access = VibaAccess(DEFAULT_CONFIG)


# ----------------------------------------------------------------------
# Private small helpers
# ----------------------------------------------------------------------


def _scalar_name(value) -> Optional[str]:
    """The name the design writes for this literal: bool / int / float / str."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return None


def _is_unit_descriptor(descriptor: VibaTypeDescriptor,
                        config: "Config" = None) -> bool:
    if descriptor.kind in (NIL, NEVER):
        return True
    if descriptor.kind == TUPLE and not descriptor.payload.elements:
        return True                                     # (): the unit
    return (descriptor.kind == TYPE_REF
            and (config or DEFAULT_CONFIG).is_unit_name(descriptor.payload.type_name))


def _attribute(data, name: str):
    """One field in the record: a dict by key, an object by attribute; _MISSING
    when there is none.

    Scalars and containers are not taken by attribute — the methods on them are
    not data.
    """
    if name.startswith("_"):
        return _MISSING
    if isinstance(data, dict):
        return data[name] if name in data else _MISSING
    if isinstance(data, (SCALAR_KINDS, list, tuple, set, frozenset, dict)):
        return _MISSING
    return getattr(data, name, _MISSING)


def _enum_matches(data: Enum, tag_name: str) -> bool:
    """The enum matches the tag: the value itself, the value written with $, or
    the member name (case-insensitively) all count."""
    value = str(data.value)
    return tag_name in (value, value.lstrip("$")) or data.name.lower() == tag_name.lower()


def _payload_of(data):
    """When a branch matched an object with a single slot, that slot is this
    branch's payload.

    When ``$ok T`` matches ``Ok`` (a single ``value`` slot), what is taken is
    the payload itself, so ``result.get_ok().value`` reads the value; an enum
    is left alone (it is itself that branch's value).
    """
    if isinstance(data, Enum):
        return data
    slots = getattr(type(data), "__slots__", None)
    if isinstance(slots, str):
        slots = (slots,)
    if isinstance(slots, tuple) and len(slots) == 1:
        return getattr(data, slots[0], data)
    fields = getattr(data, "__dict__", None)
    if isinstance(fields, dict) and len(fields) == 1:
        return next(iter(fields.values()))
    return data


def _present(payload):
    """A matched branch means "this branch is there": when it holds nil, _NIL is
    given, not "no such piece"."""
    return _NIL if payload is None else payload


def _payload(target: VibaTypeDescriptor, data):
    """The branch's payload: the unit branch is nil, other branches are
    unwrapped one layer of single-slot wrapping."""
    if _is_unit_descriptor(target):
        return _NIL
    return _present(_payload_of(data))


def _leaf_matches(written: str, data) -> bool:
    """Whether the written built-in leaf name matches this Python data."""
    if written == "str":
        return isinstance(data, str)
    if written == "bool":
        return isinstance(data, bool)
    if written == "int":
        return isinstance(data, int) and not isinstance(data, bool)
    if written == "float":
        return isinstance(data, float)
    if written in ("nil", "void", "None"):
        return data is None
    return False


def _literal_matches(written, data) -> bool:
    """The literal branch matches when the written value is the data's value."""
    return _scalar_name(data) is not None and type(written) is type(data) and written == data


def _element_tag(element: VibaTypeDescriptor) -> Optional[str]:
    if element.kind == TAGGED:
        return _tag_name(element.payload.tag)
    return None


def _definition_members(definition: VibaDefinitionDescriptor) -> Result:
    from viba.viba_type_descriptor import definition_members

    return definition_members(definition)


def _member_type_name(member) -> Optional[str]:
    from viba.viba_type_descriptor import member_type_name

    written = member_type_name(member)
    return written.ok_value if isinstance(written, Ok) else None


__all__ = [
    "access", "Config",
    "VibaAccess", "VibaNode", "VibaStep", "VibaPath",
    "by_tag", "by_field_index", "at_index", "at_key",
    "access_for",
]
