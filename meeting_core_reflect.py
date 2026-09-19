"""meeting_core_reflect — read the stable types of MEETING_ADT.md as a map over a Python record.

这是 viba-reflect.md 的访问侧，落在稳定 ADT 上：

- **设计**（地图）：the "Stable types" chapter of MEETING_ADT.md (module name meeting_core)，由描述符侧
  （viba.viba_type_descriptor）编译成描述符池。地图只跟文档有关，跟实现无关。
- **材料**（Data）：meeting_adt.py 那份 Python 记录——dataclass 实例、list / tuple /
  set / dict、以及标量。协议不规定 Data 长什么样，这一层把它绑到 Python 对象上。

名字照 viba.rule.reflect 对 viba-reflect.md 的老规矩来：协议里的类名原样保留（``VibaStep``、
``VibaPath``、``VibaNode``、``VibaAccess``），函数落 snake_case（``viba_resolve`` /
``viba_get_by_path`` / ``viba_list_fields``），四支地址是 ``by_tag`` /
``by_field_index`` / ``at_index`` / ``at_key``；六格在 ``VibaAccess`` 上：``root`` /
``has`` / ``get`` / ``leaf`` / ``length`` / ``keys``。

协议里没给名字的，才是本层自己的：

    Data 这个形参         -> _Record：一份 Python 记录
    第 5.4 节"直接抛异常"  -> VibaReflectError（第 5.4 节的 Python 落点名字）
    读图与遍历            -> VibaAccess 上的下划线方法、以及本模块的下划线函数
    设计从哪读            -> 私有的 _design_source / _design_hash / _design_pool / _definition

两条协议里的规矩：

* ``root`` 只把 (描述符, 数据) 配成对。版本协议不管：一份材料对着哪一版设计做的，
  是上层自己的事。
* ``has`` 只答真假——设计里没这个坐标、数据里没这一段，都是 ``False``。
  ``get`` 里"数据里没有"是 ``Ok(nil)``，只有"图上压根没这个坐标"才是 ``Err``；
  ``leaf`` / ``length`` / ``keys`` 取不到就是 ``Err``。

起点按协议起：``access(_definition(name)).root(_Record(record))``，回来的 ``Ok`` 里就是节点
（``Err`` 是根本没对上）。协议里的 ``Ok`` / ``Err`` 就是本模块露出的这两个。

第 5.4 节的 Python 落点在 VibaNode 上：``get_{name}()`` / ``has_{name}()`` /
``in`` / ``try_get_{name}()`` / ``node[i]`` / ``node.value`` / ``len(node)`` /
迭代 / ``keys()`` / ``values()`` / ``items()``。取值的那一类抛异常，问有没有的
那一类永不抛。

数据怎么跟设计对上（本层的绑定规则）：

* dataclass 的字段名就是 tag 去掉 ``$`` 之后的名字：``$file_id`` 对 ``file_id``。
* 容器：list / tuple / set 按下标走，dict 按键走（键按字符串比）。
* 和类型的分支看三条：数据的类名就是那一支指向的定义（``$screen_ref`` 对
  ``ScreenReference``）；分支名跟类名对得上（``$ok`` / ``$err`` 对 ``Ok`` / ``Err``）；
  或者那一支是单位元、而枚举对得上这个 tag（``$deleted ()`` 对 ``DataStatus.DELETED``，
  ``$generator ()`` 对 ``AgentRole.GENERATOR``——值、带 $ 的值、成员名都认）。
  名字比大小写不敏感。
* 内建叶子按 Python 标量认：bool / int / float / str / None。

对不上的地方就是读不出来（``Err`` 或 ``False``）——那是 Python 那边跟文档不一致
（比如把 ``$palette int`` 包成了 ``PaletteColor`` 类），不是这一层的事。
"""

from __future__ import annotations

import hashlib
import re
import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

# viba 是子模块，包在它的根目录下面；把子模块根加进 sys.path，import 才找得到。
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
    VibaConstantValue,
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
UNIT_HEADS = ("Object", "nil", "Oneof", "never")
SCALAR_KINDS = (bool, int, float, str)
_MISSING = object()
_NIL = object()  # 显式的 nil：分支命中了，里面装的就是 nil


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
    """这一版设计的哈希：原文按 \\n 统一换行，再对 UTF-8 字节算 SHA-256。"""
    return hashlib.sha256(_design_source().encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _design_pool() -> VibaPool:
    """把这一章编进一个池子；池子就是描述符侧的那份地图。"""
    pool = empty_pool()
    parsed = parse_viba_file(pool, _design_source(), DESIGN_FILE_NAME, DESIGN_MODULE_NAME)
    if isinstance(parsed, Err):
        raise VibaReflectError(f"the {DESIGN_CHAPTER} chapter does not compile: {parsed.message}")
    added = pool_add_file(pool, parsed.value)
    if isinstance(added, Err):
        raise VibaReflectError(f"the {DESIGN_CHAPTER} chapter does not go into the pool: {added.message}")
    return added.value


@lru_cache(maxsize=None)
def _definition(name: str) -> VibaDefinitionDescriptor:
    """按名字取定义描述符；``'FileId'`` 与 ``'meeting_core.FileId'`` 都认。"""
    full_name = name if "." in name else f"{DESIGN_MODULE_NAME}.{name}"
    found = pool_find_definition(_design_pool(), full_name)
    if isinstance(found, Err):
        raise VibaReflectError(found.message)
    return found.value


# ----------------------------------------------------------------------
# VibaStep 与 VibaPath
# ----------------------------------------------------------------------


class VibaStep:
    """地址的一步：四支之一。"""

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
    """链式写法里名字不带 $；带上也认。"""
    return name if name.startswith("$") else "$" + name


def _tag_name(tag: str) -> str:
    return tag[1:] if tag.startswith("$") else tag


class VibaReflectError(Exception):
    """取值那一路失败时抛的异常，带的是协议那句 ``Err`` 的话。

    第 5.4 节的 Python 落点：取值那一类失败都抛这个，``Err`` 的话原样在信息里。
    """


# ----------------------------------------------------------------------
# Data 的绑定
# ----------------------------------------------------------------------


class _Record:
    """本层对协议里 Data 形参的绑定：一份 Python 记录。

    协议没规定 Data 长什么样。这边绑的是 meeting_adt.py 那份记录（dataclass 实例、
    容器、标量都算）。版本之类的信息由上层自己带，协议不管。
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
    """一个节点：设计侧的一段描述符 + 实现侧的一段数据。

    ``path`` 是从起点走到这里的 VibaPath（协议的节点上没有这一栏，是本层为了让
    地址可存、可打印、可比较而留的痕）。
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

    # ---- 第 5.4 节的节点访问器：取值那一路抛异常 ----

    def by_tag(self, name: str) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, by_tag(name)))

    def by_field_index(self, index: int) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, by_field_index(index)))

    def at_index(self, index: int) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, at_index(index)))

    def at_key(self, key: str) -> "VibaNode":
        return self._access._unwrap(self._access.get(self, at_key(key)))

    @property
    def leaf(self) -> VibaConstantValue:
        return self._access._unwrap(self._access.leaf(self))

    @property
    def value(self):
        """第 5.4 节的 Python 落点：``leaf`` 落到原生标量。"""
        return self.leaf.value

    # ---- 形状：只看写出来的链头，不展开名字 ----

    @property
    def is_list(self) -> bool:
        return self._access._container_kind(self.descriptor) == "list"

    @property
    def is_set(self) -> bool:
        return self._access._container_kind(self.descriptor) == "set"

    @property
    def is_dict(self) -> bool:
        return self._access._container_kind(self.descriptor) == "dict"

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

    # ---- Python 落点的魔术方法 ----

    def _has_tag(self, name: str) -> bool:
        given = self._access.has(self, by_tag(name))
        return bool(given.value) if isinstance(given, Ok) else False

    def _has_field(self, index: int) -> bool:
        given = self._access.has(self, by_field_index(index))
        return bool(given.value) if isinstance(given, Ok) else False

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
        for tag, _ in self._access._members(self) or []:
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


class VibaAccess:
    """绑好一段稳定类型的访问器：第 5.2 节的六格，加本层的读图辅助。"""

    def __init__(self, definition: VibaDefinitionDescriptor):
        self.definition = definition
        self.pool = definition.pool

    def __repr__(self):
        return f"VibaAccess({self.definition.full_name!r})"

    # ---- 协议六格 ----

    def root(self, data: _Record) -> Result:
        """起点：给出 (描述符, 数据) 这一对。"""
        return Ok(VibaNode(self, self.definition.body, data.record))

    def has(self, node: VibaNode, step: VibaStep) -> Result:
        """VibaHas：这一步有没有。设计里没这个坐标、数据里没这一段，都是 false。"""
        slots = self._members(node)
        if not self._knows(node, step, slots):
            return Ok(False)
        target = self._target_of(node, step, slots)
        return Ok(self._pick(node, step, target) is not None)

    def get(self, node: VibaNode, step: VibaStep) -> Result:
        """VibaGet：往里走一步。数据里没有是 Ok(nil)，图上没这个坐标才是 Err。"""
        slots = self._members(node)
        if not self._knows(node, step, slots):
            return Err(f"the design has no such address: {step} ({node!r})")
        target = self._target_of(node, step, slots)
        if target is None:
            return Err(f"the design has no such address: {step} ({node!r})")
        piece = self._pick(node, step, target)
        if piece is None:
            return Ok(None)
        return Ok(VibaNode(self, target, piece, tuple(node.path) + (step,)))

    def leaf(self, node: VibaNode) -> Result:
        """VibaLeaf：走到叶子取值，五种。

        和、积、元组、容器都还不是叶子——它们得再往里走一步；
        值本身是 nil（显式的单位元支、或字段在但值是 nil）也算叶子。
        """
        data = node.data
        if data is _NIL:
            return Ok(VibaConstantValue("nil", None))
        if data is None or isinstance(data, SCALAR_KINDS):
            shape = self._unfold(node.descriptor)
            if (shape.kind in (PRODUCT, TUPLE)
                    or self._container_kind(shape) is not None):
                return Err(f"this piece is not a leaf: {node!r}")
            if shape.kind == SUM and not self._branch_is_a_leaf(node):
                # 和要往里走一步：$agent_name 那一支才是叶子；
                # 只有字面量那种和（Style）与 Optional[float] 才是"值自己就是那一支"
                return Err(f"this piece is not a leaf: {node!r}")
            return Ok(_constant_value(data))
        return Err(f"this piece is not a leaf: {node!r}")

    def length(self, node: VibaNode) -> Result:
        """VibaLength：list / set / tuple 的元素个数，dict 的键数。"""
        if not isinstance(node.data, (list, tuple, set, frozenset, dict)):
            return Err(f"this piece is not a container: {node!r}")
        return Ok(len(node.data))

    def keys(self, node: VibaNode) -> Result:
        """VibaKeys：dict 的键，按实现给的顺序。"""
        if not isinstance(node.data, dict):
            return Err(f"this piece is not a dict: {node!r}")
        return Ok([str(key) for key in node.data.keys()])

    # ---- 私有：取值那一路（Err 与 Ok(nil) 都抛） ----

    def _unwrap(self, given: Result):
        if isinstance(given, Err):
            raise VibaReflectError(given.message)
        if given.value is None:
            raise VibaReflectError("this piece has no value")
        return given.value

    # ---- 私有：地图（把描述符读成可寻址的形状） ----

    def _unfold(self, descriptor: VibaTypeDescriptor,
                seen: Optional[set] = None) -> VibaTypeDescriptor:
        """把写成名字的那一段展开到能寻址的形状。

        两件事，都按池子里的定义来：名字展开成定义体；泛型应用代入实参，
        也落到定义体上。和/积/指数一个规矩，没有别的例外。
        """
        seen = seen or set()
        if id(descriptor) in seen:
            return descriptor
        seen = seen | {id(descriptor)}
        if descriptor.kind == TYPE_REF:
            resolved = self._resolve_ref(descriptor)
            if resolved is None:
                return descriptor
            return self._unfold(resolved, seen)
        if descriptor.kind == TYPE_APP:
            applied = self._apply_generic(descriptor)
            if applied is not None:
                return self._unfold(applied, seen)
        return descriptor

    def _apply_generic(self, descriptor: VibaTypeDescriptor) -> Optional[VibaTypeDescriptor]:
        """构造子指向一个泛型定义时，给出代入实参后的定义体描述符。"""
        payload = descriptor.payload
        if payload.resolvable_type is None:
            return None
        resolved = module_get_type(payload.resolvable_type.container_module,
                                   payload.constructor_name)
        if isinstance(resolved, Err) or not isinstance(resolved.value, AstNodeType):
            return None
        target = resolved.value
        if not isinstance(target.ast_node, viba_ast.GenericDefinition):
            return None
        params = list(target.ast_node.generic_params or [])
        if len(params) != len(payload.args):
            return None
        from viba.viba_type_descriptor import _build_type

        body_node = viba_ast.convert_to_chain_style(target.ast_node.body)
        body = _build_type(self.pool, target.container_module, body_node)
        return self._substitute(body, dict(zip(params, payload.args)))

    def _substitute(self, descriptor: VibaTypeDescriptor,
                    bindings: dict) -> VibaTypeDescriptor:
        """按名字把形参换成实参描述符；只碰写名字的那一支。"""
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
        """一个名字指向一个透明定义时，给出那个定义体的描述符。"""
        resolvable = descriptor.payload.resolvable_type
        if resolvable is None:
            return None
        resolved = module_get_type(resolvable.container_module, descriptor.payload.type_name)
        if isinstance(resolved, Err) or not isinstance(resolved.value, AstNodeType):
            return None  # 内建叶子、泛型形参之类：到此为止
        target = resolved.value
        if not isinstance(target.ast_node, viba_ast.TypeDefinition):
            return None  # 泛型定义要带实参才有体，裸名字到此为止
        from viba.viba_type_descriptor import _build_type

        return _build_type(self.pool, target.container_module, target.ast_node.body)

    def _container_kind(self, descriptor: VibaTypeDescriptor) -> Optional[str]:
        """list / set / dict 三种内建容器；元组不算，它是按位置对位的积。"""
        if descriptor.kind == TYPE_APP and descriptor.payload.constructor_name in CONTAINERS:
            return descriptor.payload.constructor_name
        return None

    def _has_elements_by_index(self, descriptor: VibaTypeDescriptor) -> bool:
        """能按下标数、按下标取的：list / set / tuple。

        dict 不算：它的元素靠 keys + at_key 取，按下标问就是图上没这个坐标。
        """
        return self._container_kind(descriptor) in ("list", "set") or descriptor.kind == TUPLE

    def _members(self, node: VibaNode) -> Optional[List[tuple]]:
        """这一段有哪些成员：[(tag 或 None, 描述符), ...]；没有成员就给 None。

        和链、积链、指数链一个规矩：成员就是 $elements；链头是单位元就不算。
        """
        descriptor = self._unfold(node.descriptor)
        elements = None
        if descriptor.kind in (PRODUCT, SUM, EXPONENT):
            elements = list(descriptor.payload.elements)
        elif descriptor.kind == TAGGED:
            return [(descriptor.payload.tag, descriptor.payload.tagged_type)]
        if elements is None:
            return None
        if elements and _is_unit_descriptor(elements[0]):
            elements = elements[1:]
        slots = []
        for element in elements:
            if element.kind == TAGGED:
                slots.append((element.payload.tag, element.payload.tagged_type))
            else:
                slots.append((None, element))
        return slots

    def _member_steps(self, node: VibaNode) -> List[tuple]:
        """[(标签或 None, 那一步, 描述符)]：标签为 None 的按位置走。"""
        out = []
        positional = 0
        for tag, member in self._members(node) or []:
            if tag:
                out.append((tag, by_tag(tag), member))
            else:
                out.append((None, by_field_index(positional), member))
                positional += 1
        return out

    def _knows(self, node: VibaNode, step: VibaStep, slots: Optional[List[tuple]]) -> bool:
        if step.kind == "by_tag":
            return any(tag == step.value for tag, _ in slots or [])
        if step.kind == "by_field_index":
            positional = [tag for tag, _ in slots or [] if tag is None]
            return 0 <= step.value < len(positional)
        if step.kind == "at_index":
            return self._has_elements_by_index(self._unfold(node.descriptor))
        if step.kind == "at_key":
            return self._container_kind(self._unfold(node.descriptor)) == "dict"
        return False

    def _target_of(self, node: VibaNode, step: VibaStep, slots: Optional[List[tuple]]):
        if step.kind == "by_tag":
            for tag, descriptor in slots or []:
                if tag == step.value:
                    return descriptor
            return None
        if step.kind == "by_field_index":
            positional = [descriptor for tag, descriptor in slots or [] if tag is None]
            if 0 <= step.value < len(positional):
                return positional[step.value]
            return None
        if step.kind == "at_index":
            shape = self._unfold(node.descriptor)
            if not self._has_elements_by_index(shape):
                return None
            if shape.kind == TUPLE:
                if 0 <= step.value < len(shape.payload.elements):
                    return shape.payload.elements[step.value]
                return None
            return shape.payload.args[0]
        if step.kind == "at_key":
            shape = self._unfold(node.descriptor)
            if self._container_kind(shape) != "dict":
                return None
            return shape.payload.args[1]
        return None

    # ---- 私有：数据侧（Python 记录） ----

    def _pick(self, node: VibaNode, step: VibaStep,
              target: Optional[VibaTypeDescriptor] = None):
        """数据里对应的那一段；没有就是 None。

        按 tag 取的是记录字段；取不到时再看这一 tag 是不是和类型里的那一支
        （数据的类名对定义名，枚举值对 tag）。按位置取的是元组的下标；
        数据不是元组时，同样看它是不是这个位置那一支。
        """
        data = node.data
        if step.kind == "by_tag":
            tag_name = _tag_name(step.value)
            # 和类型先认分支：认上哪一支就走哪一支。数据里正好有个同名字段
            # 不算（VersionedFileContentReference 身上就有 $versioned_file_ref）。
            if self._unfold(node.descriptor).kind != SUM:
                found = _attribute(data, tag_name)
                if found is not _MISSING:
                    return _present(found)      # 字段在、值是 nil 也算在
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
        """和命中的那一支自己是不是叶子形状（字面量 / 内建叶子 / 单位元）。"""
        for tag, step, member in self._member_steps(node):
            if self._pick(node, step, member) is None:
                continue
            if tag:
                return False        # 带标签的支：值挂在那个 tag 底下，还得走一步
            shape = self._unfold(member)
            if shape.kind in (LITERAL, NIL, NEVER):
                return True
            return (shape.kind == TYPE_REF
                    and shape.payload.type_name in LEAF_NAMES)
        return False

    def _shape_matches(self, descriptor: VibaTypeDescriptor, data,
                       tag_name: Optional[str]) -> bool:
        """数据这一段是不是描述符这一段。

        四条认法：数据那个类的名字就是这一段指向的定义（``ScreenReference`` 之于
        ``$screen_ref ScreenReference``，``Rgb`` 之于 ``$truecolor Rgb``）；枚举值
        正好叫这个 tag（``DataStatus.DELETED`` 之于 ``$deleted ()``）；和类型往分支
        里递归；最后是内建叶子对 Python 标量。
        """
        if tag_name is not None:
            if isinstance(data, Enum) and _enum_matches(data, tag_name):
                return True
            # 分支名跟数据那个类的名字对得上（$ok / $err 对 Ok / Err）
            if tag_name.lower() == type(data).__name__.lower():
                return True
        if descriptor.kind == TYPE_REF:
            written = descriptor.payload.type_name
            if written.lower() == type(data).__name__.lower():
                return True
            unfolded = self._unfold(descriptor)
            if unfolded is not descriptor:
                return self._shape_matches(unfolded, data, tag_name)
            return _leaf_matches(written, data)
        if descriptor.kind == TYPE_APP:
            if descriptor.payload.constructor_name.lower() == type(data).__name__.lower():
                return True
            unfolded = self._unfold(descriptor)
            return unfolded is not descriptor and self._shape_matches(unfolded, data, tag_name)
        if descriptor.kind == TAGGED:
            tag = _tag_name(descriptor.payload.tag)
            if isinstance(data, Enum) and _enum_matches(data, tag):
                return True
            if _is_unit_descriptor(self._unfold(descriptor.payload.tagged_type)):
                return False        # 单位元支：只有 tag / 枚举值对得上才算命中
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

    # ---- 私有：成员遍历（VibaListFields 与 walk 用） ----

    def _is_unit_member(self, member) -> bool:
        written = _member_type_name(member)
        return written in UNIT_HEADS

    def _walk(self, node: VibaNode) -> List[VibaNode]:
        """把这张图上能走到的坐标全走一遍（体检表用）。"""
        out = [node]
        positional = 0
        members = self._members(node) or []
        untagged_is_the_value = self._unfold(node.descriptor).kind == SUM
        for tag, _ in members:
            if tag:
                step = by_tag(tag)
            elif untagged_is_the_value:
                continue        # 和的无标签支（Style / Optional 的 T）：值自己就是那一支
            else:
                step = by_field_index(positional)
                positional += 1
            given = self.get(node, step)
            if isinstance(given, Ok) and given.value is not None:
                out += self._walk(given.value)
        shape = self._unfold(node.descriptor)
        if self._container_kind(shape) == "dict":
            given_keys = self.keys(node)
            if isinstance(given_keys, Ok):
                for key in given_keys.value:
                    given = self.get(node, at_key(key))
                    if isinstance(given, Ok) and given.value is not None:
                        out += self._walk(given.value)
        elif self._has_elements_by_index(shape):
            given_length = self.length(node)
            if isinstance(given_length, Ok):
                for index in range(given_length.value):
                    given = self.get(node, at_index(index))
                    if isinstance(given, Ok) and given.value is not None:
                        out += self._walk(given.value)
        return out


# ----------------------------------------------------------------------
# 第 5.3 节的便利函数
# ----------------------------------------------------------------------


def viba_resolve(node: VibaNode, path: Sequence[VibaStep]) -> Result:
    """VibaResolve：逐个 step 走 VibaGet。

    走到"数据里没有这一段"（``Ok(nil)``）就到此为止：后面还有 step 就是
    ``Err("this step has no value")``，没有 step 了就把 ``Ok(nil)`` 交出去。
    """
    current = node
    for step in path:
        if current is None:
            return Err("this step has no value")
        given = node._access.get(current, step)
        if isinstance(given, Err):
            return given
        current = given.value
    return Ok(current)


def viba_get_by_path(node: VibaNode, path: Sequence[VibaStep]) -> Result:
    """VibaGetByPath：先 VibaResolve 再 VibaLeaf。"""
    resolved = viba_resolve(node, path)
    if isinstance(resolved, Err):
        return resolved
    if resolved.value is None:
        return Err("this step has no value")
    return node._access.leaf(resolved.value)


def viba_list_fields(node: VibaNode, definition: VibaDefinitionDescriptor) -> Result:
    """VibaListFields：按 DefinitionMembers 逐个 VibaGet，取到的收进表。"""
    accessor = node._access
    members = _definition_members(definition)
    if isinstance(members, Err):
        return members
    out = []
    positional = 0
    for member in members.value:
        if member.tag:
            step = by_tag(member.tag)
        else:
            if accessor._is_unit_member(member):
                continue
            step = by_field_index(positional)
            positional += 1
        given = accessor.get(node, step)
        # 取到的收进表；缺的（含这一段压根不是积/和，问不出来的）不进表。
        if isinstance(given, Ok) and given.value is not None:
            out.append(given.value)
    return Ok(out)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------


def access(definition_descriptor: VibaDefinitionDescriptor) -> VibaAccess:
    """把一段稳定类型（定义描述符）当图，返回访问器。"""
    return VibaAccess(definition_descriptor)


# ----------------------------------------------------------------------
# 私有小工具
# ----------------------------------------------------------------------


def _constant_value(value) -> VibaConstantValue:
    if isinstance(value, bool):
        return VibaConstantValue("bool", value)
    if isinstance(value, int):
        return VibaConstantValue("int", value)
    if isinstance(value, float):
        return VibaConstantValue("float", value)
    if isinstance(value, str):
        return VibaConstantValue("str", value)
    if value is None:
        return VibaConstantValue("nil", None)
    raise TypeError(f"no constant kind for {value!r}")


def _is_unit_descriptor(descriptor: VibaTypeDescriptor) -> bool:
    if descriptor.kind in (NIL, NEVER):
        return True
    if descriptor.kind == TUPLE and not descriptor.payload.elements:
        return True                                     # ()：单位元
    return descriptor.kind == TYPE_REF and descriptor.payload.type_name in UNIT_HEADS


def _attribute(data, name: str):
    """记录里的一个字段：dict 按键、对象按属性；没有就给 _MISSING。

    标量与容器不给按属性取——它们身上的方法不是数据。
    """
    if name.startswith("_"):
        return _MISSING
    if isinstance(data, dict):
        return data[name] if name in data else _MISSING
    if isinstance(data, (SCALAR_KINDS, list, tuple, set, frozenset, dict)):
        return _MISSING
    return getattr(data, name, _MISSING)


def _enum_matches(data: Enum, tag_name: str) -> bool:
    """枚举跟 tag 对得上：值本身、值带 $ 的写法、或者成员名（去掉大小写）都算。"""
    value = str(data.value)
    return tag_name in (value, value.lstrip("$")) or data.name.lower() == tag_name.lower()


def _payload_of(data):
    """分支命中了一个只有一格的对象，那一格就是这一支的载荷。

    ``$ok T`` 命中 ``Ok``（只有 ``value`` 一格）时，取到的是载荷本身，所以
    ``result.get_ok().value`` 读得出值；枚举不动（它自己就是那一支的值）。
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
    """分支命中了就是"这一支在"：里面装的是 nil 时给 _NIL，而不是"没有这一段"。"""
    return _NIL if payload is None else payload


def _payload(target: VibaTypeDescriptor, data):
    """分支的载荷：单位元那一支是 nil，别的支按单槽包装拆一层。"""
    if _is_unit_descriptor(target):
        return _NIL
    return _present(_payload_of(data))


def _leaf_matches(written: str, data) -> bool:
    """写出来的内建叶子名字对不对得上这一份 Python 数据。"""
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


def _literal_matches(written: VibaConstantValue, data) -> bool:
    """字面量那一支：写的值和数据的值一样才算。"""
    try:
        given = _constant_value(data)
    except TypeError:
        return False
    return written.kind == given.kind and written.value == given.value


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
    return written.value if isinstance(written, Ok) else None


__all__ = [
    "access",
    "VibaAccess", "VibaNode", "VibaStep", "VibaPath",
    "by_tag", "by_field_index", "at_index", "at_key",
    "viba_resolve", "viba_get_by_path", "viba_list_fields",
]
