"""一份流程：开会前定好的那五类 viba 文件，和"读它们"这件事。

    1. `actor_api.viba`        actor 会做的事
    2. `assistant_api.viba`    assistant 会做的事
    3. `critic_api.viba`       critic 会做的事
    4. `tickets/*.viba`        工单程序：一场会很多张，都是开会前写好的
    5. `rules/*.viba`          规则：critic 跑的程序

五类合起来就是这场会的流程：工单是它的步骤，规则是它的判据，三个 api 的声明是每一步能调到的算子。

**流程不在程序里，在你自己的配置目录里**（就像 Claude Code 的 skills 之于它的二进制）：这份程序
只负责按下面的次序找一个目录，找到谁就用谁——

    1. 明说的 `--workflow <dir>`（或者调用时给的 root）
    2. 环境变量 `REMOTEBUDDY_WORKFLOW`
    3. 这个项目自己的 `<project_root>/.agents/workflow/`
    4. 你的配置目录 `$REMOTEBUDDY_HOME/workflow/`（默认 `~/.remotebuddy/workflow/`）
    5. 程序里带的那份默认（`ticket_centered/../workflow/`），没有别的就用它

自进化的人改的是第 4（或第 3）个，用的只有那个目录：`python -m ticket_centered.workflow --init` 会把
第 5 份抄一份到你的配置目录，从那儿开始改；程序本身不用给他，也不用动。

**工单就是 viba 代码**，没有另一种东西叫"工单"：这里读一份文件读出来的，就是它写了哪几步（调哪个
角色的哪个算子、在哪条数据路径上）、要读哪几条数据路径、答哪几步、import 了哪些 api；读一份角色 api
读出来的是它会哪些算子。读法只有这一份，五类文件都用它。

它答的：这场会有哪几张工单、哪几条规则（按类分好）、一份一份文件的全套（打包用）、它叫什么名字
（**内容摘要**：摘要一样，就是同一份流程）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast

ROLES = ("actor_api", "assistant_api", "critic_api")
ROLE_OF_API = {"actor_api": "actor", "assistant_api": "assistant", "critic_api": "critic"}
# 程序里带的那份默认（没有配置目录时用它），以及找流程的那几个地方
SHIPPED_WORKFLOW = Path(__file__).resolve().parent.parent / "workflow"
WORKFLOW_ENV = "REMOTEBUDDY_WORKFLOW"
HOME_ENV = "REMOTEBUDDY_HOME"
PROJECT_WORKFLOW = Path(".agents") / "workflow"
DEFAULT_HOME_NAME = ".remotebuddy"
TICKETS_DIR = "tickets"
RULES_DIR = "rules"
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


# ----------------------------------------------------------------------
# 找流程：程序带一份默认，改的是你自己的配置目录
# ----------------------------------------------------------------------


def config_home() -> Path:
    """你的配置目录：`$REMOTEBUDDY_HOME`，没有就是 `~/.remotebuddy`。"""
    given = os.environ.get(HOME_ENV)
    return Path(given).expanduser() if given else Path.home() / DEFAULT_HOME_NAME


def workflow_candidates(project_root=None) -> List[Path]:
    """按次序找一遍：项目自己的、配置目录里的、程序带的默认。"""
    candidates: List[Path] = []
    if project_root is not None:
        candidates.append(Path(project_root) / PROJECT_WORKFLOW)
    candidates.append(config_home() / "workflow")
    candidates.append(SHIPPED_WORKFLOW)
    return candidates


def workflow_root(explicit=None, project_root=None) -> Path:
    """这一趟该读哪份流程。

    `explicit`（`--workflow` / `REMOTEBUDDY_WORKFLOW`）指到哪儿就是哪儿；没指就按
    项目 → 配置目录 → 程序默认 的次序，取第一个像一份流程的目录；一个都没有就报出来。
    """
    asked = explicit or os.environ.get(WORKFLOW_ENV)
    if asked:
        root = Path(asked).expanduser()
        if not _looks_like_workflow(root):
            raise FileNotFoundError(f"{asked} 不是一份流程（缺 actor_api.viba 或 tickets/）")
        return root
    for root in workflow_candidates(project_root):
        if _looks_like_workflow(root):
            return root
    raise FileNotFoundError("找不到流程：给 --workflow，或者 python -m ticket_centered.workflow --init "
                            f"在 {config_home() / 'workflow'} 放一份")


def _looks_like_workflow(root) -> bool:
    root = Path(root)
    return (root / f"{ROLES[0]}{VIBA_SUFFIX}").is_file() and (root / TICKETS_DIR).is_dir()


def load_workflow(root=None, project_root=None) -> Workflow:
    """Read this run's workflow: `root` when given, otherwise the first one found."""
    return Workflow.load(root if root is not None else workflow_root(project_root=project_root))


def install(destination=None) -> Path:
    """把程序里带的那份默认抄到你的配置目录，从那儿开始改；已经有了就不动它。"""
    target = Path(destination).expanduser() if destination else config_home() / "workflow"
    if _looks_like_workflow(target):
        return target
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} 里有别的东西，不覆盖")
    shutil.copytree(SHIPPED_WORKFLOW, target, dirs_exist_ok=True)
    return target


def main(argv=None) -> int:
    """`python -m ticket_centered.workflow`：看现在读的是哪份，或者抄一份出来改。"""
    parser = argparse.ArgumentParser(description="流程：找、看、抄一份出来改")
    parser.add_argument("--init", action="store_true", help="把默认那份抄到配置目录")
    parser.add_argument("--where", default=None, metavar="DIR",
                        help="指到哪个目录（抄到这儿 / 看这个）；不给就看现在读的是哪份")
    parser.add_argument("--project", default=None, help="按这个项目根找（含 .agents/workflow）")
    args = parser.parse_args(argv)

    if args.init:
        target = install(args.where)
        print(f"这份流程在：{target}")
        print(f"改它就行：三份 api、{TICKETS_DIR}/、{RULES_DIR}/")
        return 0
    root = workflow_root(explicit=args.where, project_root=args.project)
    workflow = Workflow.load(root)
    print(f"这一趟读的流程：{root}")
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
    """The steps one Prepare's `$call` names: what that step itself read.

    One address argument makes the `$call` the step itself (no tag to look
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
