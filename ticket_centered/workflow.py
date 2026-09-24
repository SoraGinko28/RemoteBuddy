"""一份流程：开会前定好的那五类 viba 文件，和"读它们"这件事。

    1. `actor_api.viba`        actor 会做的事
    2. `assistant_api.viba`    assistant 会做的事
    3. `critic_api.viba`       critic 会做的事
    4. `tickets/*.viba`        工单程序：一场会很多张，都是开会前写好的
    5. `rules/*.viba`          规则：critic 跑的程序

五类合起来就是这场会的流程：工单是它的步骤，规则是它的判据，三个 api 的声明是每一步能调到的算子。
读的是仓库根上那个 `workflow/` 目录（跟 `critic/`、`assistant/` 并列；设计文档 `ticket_centered.md` 里也是这个叫法）。

**工单就是 viba 代码**，没有另一种东西叫"工单"：这里读一份文件读出来的，就是它写了哪几步（调哪个
角色的哪个算子、在哪条数据路径上）、要读哪几条数据路径、答哪几步、import 了哪些 api；读一份角色 api
读出来的是它会哪些算子。读法只有这一份，五类文件都用它。

它答的：这场会有哪几张工单、哪几条规则（按类分好）、一份一份文件的全套（打包用）、它叫什么名字
（**内容摘要**：摘要一样，就是同一份流程）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast

ROLES = ("actor_api", "assistant_api", "critic_api")
ROLE_OF_API = {"actor_api": "actor", "assistant_api": "assistant", "critic_api": "critic"}
WORKFLOW_DIR = Path(__file__).resolve().parent.parent / "workflow"
TICKETS_DIR = "tickets"
RULES_DIR = "rules"
VIBA_SUFFIX = ".viba"
RET = "__ret__"
SUB_ENVS = ("sub_env", "tmp_sub_env")


class Step(NamedTuple):
    """One step of a ticket: the definition it is written as, and the call it makes."""

    definition: str     # the ticket's own name for this step (`greeted`, `__ret__`, …)
    env: str            # the data path it runs on, relative to the case ("" is the case)
    api: str            # the role api it calls (`actor_api`)
    op: str             # the operator it calls (`acknowledge`)


class Workflow:
    """The five classes of viba files a meeting runs, read from one directory."""

    def __init__(self, files: Dict[str, str], rules: Iterable[str] = ()):
        self.files = dict(files)
        self.rule_names = tuple(rules)

    # ------------------------------------------------------------------
    # Reading the directory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, root) -> "Workflow":
        root = Path(root)
        files: Dict[str, str] = {}
        for role in ROLES:
            path = root / f"{role}{VIBA_SUFFIX}"
            if not path.is_file():
                raise FileNotFoundError(f"少了 {path}")
            files[role] = path.read_text(encoding="utf-8")
        for folder in (TICKETS_DIR, RULES_DIR):
            directory = root / folder
            if not directory.is_dir():
                raise FileNotFoundError(f"少了目录 {directory}")
            for path in sorted(directory.glob(f"*{VIBA_SUFFIX}")):
                files[path.stem] = path.read_text(encoding="utf-8")
        return cls(files, sorted(path.stem for path in (root / RULES_DIR).glob(f"*{VIBA_SUFFIX}")))

    def tickets(self) -> Dict[str, str]:
        """The ticket programs, by short name."""
        return {name: source for name, source in self.files.items()
                if name not in ROLES and name not in self.rule_names}

    def rules(self) -> Dict[str, str]:
        """The rule programs, by short name."""
        return {name: self.files[name] for name in self.rule_names}

    def sources(self) -> Dict[str, str]:
        """Every file by name: what goes into a bundle's `program/`."""
        return dict(self.files)

    def digest(self) -> str:
        """What to call this workflow: a hash of every file, name and content alike."""
        seed = hashlib.sha256()
        for name in sorted(self.files):
            seed.update(name.encode("utf-8"))
            seed.update(b"\0")
            seed.update(self.files[name].encode("utf-8"))
            seed.update(b"\0")
        return seed.hexdigest()

    # ------------------------------------------------------------------
    # Reading one of the five classes
    # ------------------------------------------------------------------

    def steps_of(self, ticket: str) -> List[Step]:
        """The steps a ticket is made of, in the order they are written."""
        return steps_of(self.files[ticket])

    def addresses_in(self, ticket: str) -> List[Tuple[str, str]]:
        """Every step the ticket reads: (data path relative to the case, definition)."""
        return addresses_in(self.files[ticket])

    def answers_of(self, ticket: str) -> List[Tuple[str, str]]:
        """Every step the ticket answers: (its data path, its definition)."""
        return answers_of(self.files[ticket])

    def apis_used_in(self, ticket: str) -> List[str]:
        """The role apis a ticket imports (it must import every api it calls)."""
        return apis_used_in(self.files[ticket])

    def ops_of(self, api: str) -> List[str]:
        """The operators one role api declares, in the order they are written."""
        return [definition.name for definition in definitions_of(self.files[api])
                if not definition.name.startswith("__")]

    def role_of_op(self, op: str) -> Optional[str]:
        """Which role declares this operator; None when none does.

        An answer belongs to the role whose api declares the operator that
        produced it: nobody else's table can do that step, so the name is where
        the responsibility is.
        """
        for api, role in ROLE_OF_API.items():
            if api in self.files and op in self.ops_of(api):
                return role
        return None


def load_workflow(root) -> Workflow:
    """Read one meeting's workflow from a directory."""
    return Workflow.load(root)


# ----------------------------------------------------------------------
# Reading viba source: the same read for all five classes
# ----------------------------------------------------------------------


def steps_of(source: str) -> List[Step]:
    """Every call a ticket makes, in the order they are written."""
    aliases = {alias: module for module, alias in imports_in(source)}
    out: List[Step] = []
    for definition in definitions_of(source):
        call = _call_in(definition.body)
        if call is None:
            continue
        api, op, env = call
        out.append(Step(definition.name, env, aliases.get(api, api), op))
    return out


def addresses_in(source: str) -> List[Tuple[str, str]]:
    """Every step a ticket reads, by (data path, definition)."""
    out: List[Tuple[str, str]] = []
    for node in viba_ast.walk(viba_ast.parse(source)):
        if isinstance(node, (viba_ast.Product, viba_ast.ProductChain, viba_ast.Tuple)):
            found = _address(node)
            if found is not None and found not in out:
                out.append(found)
    return out


def answers_of(source: str) -> List[Tuple[str, str]]:
    """Every step a ticket is supposed to answer: (its data path, its definition)."""
    out: List[Tuple[str, str]] = []
    for step in steps_of(source):
        if (step.env, step.op) not in out:
            out.append((step.env, step.op))
    return out


def apis_used_in(source: str) -> List[str]:
    """The role apis a file imports (a ticket must import every api it calls)."""
    out: List[str] = []
    for module, _alias in imports_in(source):
        if module not in out:
            out.append(module)
    return out


def addresses_of_call(call) -> List[Tuple[str, str]]:
    """The steps one Prepare's `$call` names: what that step itself read.

    One material argument makes the `$call` the step itself (no tag to look
    under); several are a product, each member carrying its own tag.
    """
    if call is None:
        return []
    piece = getattr(call, "data", call)
    whole = _address(piece)
    if whole is not None:
        return [whole]
    out: List[Tuple[str, str]] = []
    for element in _elements(piece):
        inner = element.type if isinstance(element, viba_ast.Tagged) else element
        found = _address(inner)
        if found is not None:
            out.append(found)
    return out


def definitions_of(source: str) -> List[viba_ast.TypeDefinition]:
    """The definitions a file is written as, in order."""
    return [node for node in viba_ast.parse(source).body
            if isinstance(node, viba_ast.TypeDefinition)]


def imports_in(source: str) -> List[Tuple[str, str]]:
    """(module, alias) for every import in a file."""
    return [(node.module, node.alias) for node in viba_ast.parse(source).body
            if isinstance(node, viba_ast.Import)]


def is_a_call(body) -> bool:
    """Is this definition a step (a call), or does it define something of its own?"""
    return _call_in(body) is not None


def _elements(piece):
    """Flatten a product: `Product` (binary), `ProductChain` and `Tuple` alike."""
    if isinstance(piece, viba_ast.Product):
        return _elements(piece.left) + _elements(piece.right)
    if isinstance(piece, viba_ast.ProductChain):
        out = []
        for member in piece.elements:
            out.extend(_elements(member))
        return out
    return [piece]


def _call_in(body) -> Optional[Tuple[str, str, str]]:
    """The call a definition is written as: (alias, operator, data path)."""
    node, args = body, []
    while isinstance(node, viba_ast.Partial):
        if node.argument is not None:
            args.append(node.argument)
        node = node.function
    if not isinstance(node, viba_ast.TypeRef) or "." not in node.name:
        return None
    alias, op = node.name.split(".", 1)
    env = ""
    for arg in args:
        if isinstance(arg, viba_ast.Tagged) and arg.tag == "$env":
            env = _env_name(arg.type)
    return alias, op, env


def _env_name(node) -> str:
    """The data path a `$env` gives: the name handed to `sub_env`; `environ` is the case itself."""
    if isinstance(node, viba_ast.Constant):
        return str(node.value)
    if isinstance(node, viba_ast.TypeRef):
        return "" if node.name == "environ" else node.name
    if isinstance(node, viba_ast.Partial) and isinstance(node.function, viba_ast.TypeRef):
        if node.function.name.split(".")[-1] in SUB_ENVS:
            if isinstance(node.argument, viba_ast.Constant):
                return str(node.argument.value)
    return ""


def _address(piece) -> Optional[Tuple[str, str]]:
    """A step written as material: `($module_path "…" * $func_name "…")`."""
    if not isinstance(piece, (viba_ast.Product, viba_ast.ProductChain, viba_ast.Tuple)):
        return None
    fields = {}
    for member in _elements(piece):
        if isinstance(member, viba_ast.Tagged) and isinstance(member.type, viba_ast.Constant):
            fields[member.tag] = str(member.type.value)
    if "$module_path" in fields and "$func_name" in fields:
        return fields["$module_path"], fields["$func_name"]
    return None
