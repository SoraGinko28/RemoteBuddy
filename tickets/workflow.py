"""一份规范：开会前定好的那几类 viba 文件，和"读它们"这件事。

    1. `actor_api.viba`        actor 会做的事
    2. `assistant_api.viba`    assistant 会做的事
    3. `critic_api.viba`       critic 会做的事
    4. `tickets/*.viba`        工单程序：一场会很多张，都是开会前写好的
    5. `rules/*.viba`          规则：critic 判案时跑的程序

它们合起来就是这场会的规范：工单是一张单要跑的那段程序，规则是 critic 判案时跑的程序，几个 api 的
声明是每一步能调到的算子。

**规范不在程序里，在它自己的仓库里**（就像 Claude Code 的 skills 之于它的二进制）：这份程序只负责
找一个目录，找到谁读谁——

    1. 明说的 `--workflow <dir>`（或者调用时给的 root）
    2. 环境变量 `REMOTEBUDDY_WORKFLOW`
    3. 环境变量 `REMOTEBUDDY_NORM`
    4. 默认那条路径：程序仓库旁边那一层的 `RbNorm/rb_norm/`

程序本身一行这类代码都不带，会里也一行都不发副本：工单和规则是**现读**的，处理的人每次轮询都照这份
读一遍；一张单上只记着它发的是哪一段程序（`$ticket_name`）。

**工单就是 viba 代码**，没有另一种东西叫"工单"：这里读一份文件读出来的，就是它写了哪几步（调哪个
角色的哪个算子、在哪条数据路径上）、要读哪几条数据路径、答哪几步、import 了哪些 api；读一份角色 api
读出来的是它会哪些算子。读法只有这一份，几类文件都用它。

它答的：这场会有哪几张工单、哪几条规则（按类分好）、一份一份文件的全套、它叫什么名字
（**内容摘要**：摘要一样，就是同一份规范）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast

ROLES = ("actor_api", "assistant_api", "critic_api")
API_SUFFIX = "_api"


def is_api_file(name: str) -> bool:
    """这份文件是不是一个角色 api。

    三个 api 是程序带的那一份流程里的三个角色，不是上限：谁给一份 `<角色>_api.viba`，谁就多一个角色，
    别的部分一个字都不用改。名字后面没有 `_api` 的是工单（或者规则）。
    """
    return name.endswith(API_SUFFIX) and len(name) > len(API_SUFFIX)


def role_of_api(api: str) -> str:
    """这个 api 是哪个角色的（`actor_api` → `actor`）。"""
    return api[: -len(API_SUFFIX)] if is_api_file(api) else api


# 规范（这场会怎么开的那几份 viba 程序）不在程序里，在它自己的仓库里：
# `RemoteBuddy-core` 旁边那一层的 `RbNorm/rb_norm/`，用 `REMOTEBUDDY_NORM` 可以另指。
NORM_ENV = "REMOTEBUDDY_NORM"
WORKFLOW_ENV = "REMOTEBUDDY_WORKFLOW"
# 规范仓库里，规范本身放在这个包目录下（`RbNorm/rb_norm/`）——和别的仓库一样有个包目录
NORM_PACKAGE = "rb_norm"
DEFAULT_NORM = Path(__file__).resolve().parent.parent.parent / "RbNorm" / NORM_PACKAGE
# 程序里那三个角色（不是上限：多一份 api 文件就多一个角色）
ROLE_OF_API = {api: role_of_api(api) for api in ROLES}
TICKETS_DIR = "tickets"
RULES_DIR = "rules"
# 模块按哪儿找（冒号分开的几个目录，次序就是这个）：单子自己的目录先被找，然后这三个
SEARCH_DIRS = (TICKETS_DIR, "", RULES_DIR)
VIBA_SUFFIX = ".viba"
RET = "__impl__"
SUB_ENVS = ("sub_env", "tmp_sub_env")


class Step(NamedTuple):
    """One step of a ticket: the definition it is written as, and the call it makes."""

    definition: str     # the ticket's own name for this step (`greeted`, `__impl__`, …)
    env: str            # the data path it runs on, relative to the case ("" is the case)
    api: str            # the role api it calls (`actor_api`)
    op: str             # the operator it calls (`acknowledge`)


class Workflow:
    """The classes of viba files a meeting runs, read from one directory."""

    def __init__(self, files: Dict[str, str], rules: Iterable[str] = (), root=None):
        self.files = dict(files)
        self.rule_names = tuple(rules)
        # 这一份流程是从哪个目录读出来的（服务表 `services` 就在它旁边）；不是从目录读的就没有
        self.root = Path(root) if root is not None else None

    # ------------------------------------------------------------------
    # Reading the directory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, root) -> "Workflow":
        root = Path(root)
        files: Dict[str, str] = {}
        for path in sorted(root.glob(f"*{API_SUFFIX}{VIBA_SUFFIX}")):
            files[path.stem] = path.read_text(encoding="utf-8")
        if ROLES[0] not in files:
            raise FileNotFoundError(f"少了 {root / f'{ROLES[0]}{VIBA_SUFFIX}'}")
        for folder in (TICKETS_DIR, RULES_DIR):
            directory = root / folder
            if not directory.is_dir():
                raise FileNotFoundError(f"少了目录 {directory}")
            for path in sorted(directory.glob(f"*{VIBA_SUFFIX}")):
                files[path.stem] = path.read_text(encoding="utf-8")
        rules = sorted(path.stem for path in (root / RULES_DIR).glob(f"*{VIBA_SUFFIX}"))
        return cls(files, rules, root=root)

    def apis(self) -> List[str]:
        """Every role api this workflow has, by short name (the shipped three, and any the
        facility grew: a new `<role>_api.viba` next to them is a new role)."""
        return sorted(name for name in self.files if is_api_file(name))

    def tickets(self) -> Dict[str, str]:
        """The ticket programs, by short name."""
        return {name: source for name, source in self.files.items()
                if not is_api_file(name) and name not in self.rule_names}

    def rules(self) -> Dict[str, str]:
        """The rule programs, by short name."""
        return {name: self.files[name] for name in self.rule_names}

    def sources(self) -> Dict[str, str]:
        """Every file by name: the norm's own source, read but never copied anywhere."""
        return dict(self.files)

    # ------------------------------------------------------------------
    # 那几份程序在规范目录下的哪儿
    # ------------------------------------------------------------------

    def ticket_file(self, ticket: str) -> Path:
        """这张工单的程序在哪：`<规范>/tickets/<工单>.viba`。"""
        return self._under(TICKETS_DIR, f"{ticket}{VIBA_SUFFIX}")

    def rule_file(self, rule: str) -> Path:
        """这条规则的程序在哪：`<规范>/rules/<规则>.viba`。"""
        return self._under(RULES_DIR, f"{rule}{VIBA_SUFFIX}")

    def search_dirs(self) -> List[str]:
        """跑这个规范里的程序时，模块按哪儿找，一个目录一项。

        写 import 的那个文件自己的目录先被找，所以单子放在 `tickets/` 里也 import 得到根上那几个 api；
        规则在 `rules/` 里，也找得到根上那几个 api。
        """
        if self.root is None:
            raise ValueError("这份流程不是从目录读出来的，没有规范目录可以找程序")
        return [str(self.root / one) if one else str(self.root) for one in SEARCH_DIRS]

    def search_path(self) -> str:
        """同上，写成一串（`Environment.viba_path` 要的那种写法）。"""
        return ":".join(self.search_dirs())

    def _under(self, folder: str, name: str) -> Path:
        if self.root is None:
            raise ValueError("这份流程不是从目录读出来的，没有规范目录可以找程序")
        return self.root / folder / name

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
        for api in self.apis():
            if op in self.ops_of(api):
                return role_of_api(api)
        return None


# ----------------------------------------------------------------------
# 找流程：程序带一份默认，改的是你自己的配置目录
# ----------------------------------------------------------------------


def norm_home() -> Path:
    """规范目录在哪儿（放着 api、工单、规则、服务表的那一层）。

    `$REMOTEBUDDY_NORM` 指到哪儿就照哪儿——指规范仓库根也行，那就用它底下的 `rb_norm/`；没有这个
    环境变量就是程序仓库旁边那一层的 `RbNorm/rb_norm/`。程序一行这类代码都不带：全在规范目录里，
    跑的时候现读。
    """
    given = os.environ.get(NORM_ENV)
    return _the_norm_dir(Path(given).expanduser() if given else DEFAULT_NORM)


def _the_norm_dir(root: Path) -> Path:
    """规范目录：给的就是那一层就用它；给的是仓库根就用它底下的 `rb_norm/`。"""
    if _looks_like_workflow(root):
        return root
    inside = root / NORM_PACKAGE
    return inside if _looks_like_workflow(inside) else root


def workflow_candidates(project_root=None) -> List[Path]:
    """按次序找一遍。只有规范那一个地方——工单和规则不属于某一个项目或某一场会。"""
    return [norm_home()]


def workflow_root(explicit=None, project_root=None) -> Path:
    """这一趟该读哪份规范。

    `explicit`（`--workflow` / `REMOTEBUDDY_WORKFLOW`）指到哪儿就是哪儿；没指就是规范目录
    （`$REMOTEBUDDY_NORM`，默认 `RbNorm/rb_norm/`）。指的那个地方不像一份规范就当场报出来。
    """
    asked = explicit or os.environ.get(WORKFLOW_ENV)
    if asked:
        root = Path(asked).expanduser()
        if not _looks_like_workflow(root):
            raise FileNotFoundError(f"{asked} 不是一份规范（缺 actor_api.viba 或 tickets/）")
        return root
    root = norm_home()
    if not _looks_like_workflow(root):
        raise FileNotFoundError(
            f"找不到规范：{root} 里没有 actor_api.viba 或 tickets/（用 {NORM_ENV} 另指一个目录）")
    return root


def _looks_like_workflow(root) -> bool:
    root = Path(root)
    return (root / f"{ROLES[0]}{VIBA_SUFFIX}").is_file() and (root / TICKETS_DIR).is_dir()


def load_workflow(root=None, project_root=None) -> Workflow:
    """Read this run's workflow: `root` when given, otherwise the one norm."""
    return Workflow.load(root if root is not None else workflow_root(project_root=project_root))


def main(argv=None) -> int:
    """`python -m tickets.workflow`：看现在读的是哪份规范，里面有什么。"""
    parser = argparse.ArgumentParser(description="规范：看这一趟读的是哪一份")
    parser.add_argument("--where", default=None, metavar="DIR",
                        help="指到哪个目录；不给就看现在读的是哪份")
    args = parser.parse_args(argv)

    root = workflow_root(explicit=args.where)
    workflow = Workflow.load(root)
    print(f"这一趟读的规范：{root}")
    print(f"  角色：{'、'.join(workflow.apis())}")
    print(f"  工单：{'、'.join(sorted(workflow.tickets()))}")
    print(f"  规则：{'、'.join(sorted(workflow.rules()))}")
    print(f"  摘要：{workflow.digest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


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
    """The steps one work order's `$call` names: what that step itself read.

    The call is written `__dyn_call__ << "name" << $task <an address> …`, and each argument
    may be an address itself or a product carrying one.
    """
    if call is None:
        return []
    out: List[Tuple[str, str]] = []
    for argument in call_arguments(call):
        inner = argument.type if isinstance(argument, viba_ast.Tagged) else argument
        found = _address(inner)
        if found is None:
            for element in _elements(inner):
                inside = element.type if isinstance(element, viba_ast.Tagged) else element
                nested = _address(inside)
                if nested is not None:
                    out.append(nested)
        else:
            out.append(found)
    return out


def call_arguments(call) -> List[viba_ast.AST]:
    """一次调用里那几个实参，按写下来的次序。

    调用写成 `__dyn_call__ << "名字" << 实参…`，名字不是实参，这里摊掉它；成员调用
    （`__dyn_method__`）还多一个「从哪个值上取的」，那个也不算。不是这种写法的，整份就是它自己。
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
        if piece.name == "__dyn_method__":
            return peeled[2:]
        if piece.name == "__dyn_call__":
            return peeled[1:]
    return peeled


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
    if isinstance(node, viba_ast.Partial):
        # 链要摊平看：环境成员把环境自己当第一个实参，名字写在最后——
        # `$sub_env << environ << "名字"`（tag 写法）和 `environ.sub_env << environ << "名字"`
        # 一样，摊平后基名在 `Member.tag` 或 `TypeRef.name` 上。
        base, arguments = node, []
        while isinstance(base, viba_ast.Partial):
            if base.argument is not None:
                arguments.append(base.argument)
            base = base.function
        if isinstance(base, viba_ast.Member):
            called = base.tag.lstrip("$")
        elif isinstance(base, viba_ast.TypeRef):
            called = base.name.split(".")[-1]
        else:
            return ""
        if (called in SUB_ENVS and arguments
                and isinstance(arguments[0], viba_ast.Constant)):
            return str(arguments[0].value)
    return ""


def _address(piece) -> Optional[Tuple[str, str]]:
    """A step written as an address: `($module_path "…" * $func_name "…")`."""
    if not isinstance(piece, (viba_ast.Product, viba_ast.ProductChain, viba_ast.Tuple)):
        return None
    fields = {}
    for member in _elements(piece):
        if isinstance(member, viba_ast.Tagged) and isinstance(member.type, viba_ast.Constant):
            fields[member.tag] = str(member.type.value)
    if "$module_path" in fields and "$func_name" in fields:
        return fields["$module_path"], fields["$func_name"]
    return None
