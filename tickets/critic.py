"""critic 这一侧：判案。判的是哪一份由单上那次调用给；**哪些验收条件可能不过，由它自己找**。

能力表（`critic_caps`）就是 api 里那一件事 `judge`，加上跑验收条件要用的那几个算子。验收条件是 viba
程序（`acceptance/*.viba`），用 `interpret` 跑——它自己给门槛、自己答 bool，这里不替它判。

判的是哪一份由单给，critic 把那一份放在这条数据路径下（`judgment/under_judgment`），验收条件从那儿
读。结论落在这条数据路径下：`judge` 是那个 bool，`objection` 是说不合格的那句话。

一份规范要判的可以是代码，也可以是一份法（`RbNormLegislative`）——那由验收条件自己说了算：它拿到的是
单上交过来的那一份，怎么读它、按什么规则判它，都是规范里 `acceptance/` 那几份程序的事。这一处多给几个
读目录、读程序的算子，谁用得上谁用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from . import _viba  # noqa: F401

from viba import is_complete as _design
from viba import viba_ast
from viba.interpret import (Environment, EnvironmentCompute, interpret, get_snapshot,
                            put_snapshot)

from .binding import answer_of, failure_in, finished, text_of
from .builtins import _a_list, the_table as the_builtin_table
from .assistant import what_to_make
from .handler import Handler, program_caps
from .meeting import a_path_in
from .table import members_of
from .ticket import a_list
from .workflow import ACCEPTANCE_DIR, INTAKE_DIR, TICKET_PROGRAMS_DIR, Workflow

JUDGMENT_DIR = "judgment"
UNDER_JUDGMENT = "under_judgment"

# 解释器自己回答的那几个名字（`viba-primitives.md` 第 4 节，加上 `viba/builtin.viba` 里 `Environment`
# 的成员）：一份程序调到它们，不用谁去声明——解释器自己有。名字按不带 `$` 的写法比。
NATIVE_NAMES = frozenset({
    "get_attr", "get_item", "in", "len", "keys",
    "sub_env", "tmp_env", "get_parent", "get_root", "get_relative_path",
    "find_by_relative_path", "compress_env_path", "try_compact",
    "uncompress_relative_path", "viba_path",
})

# 一份程序里本来就有、不归谁实现的那几个名字。
SYNTAX_NAMES = frozenset({"__decl__", "__impl__", "__get_args__", "__dyn_call__",
                          "__dyn_method__"})

# 这一处只说"是哪条验收条件说不合格"：它叫什么、要说什么话，都是规范的事，这一处不认识它们。
# （要它自己说那句话，就得有一处 api 让它把话交出来，那是另一样东西。）


def critic_caps(workflow: Workflow, judgment: str = JUDGMENT_DIR,
                acceptance_dir=None) -> Dict[str, Callable]:
    """critic 的能力表：`judge`（拿那一份、自己找验收条件、逐条判）加上跑它们的那几个算子。

    `acceptance_dir` 是验收条件在哪儿；不给就看这份流程是从哪个目录读出来的（规范那一套：它们在
    `<规范>/acceptance/`，由 `Workflow.condition_file` 指）。两份都没有（流程是拿现成的源码拼的）才退回
    这次运行的那个 `viba_path`。
    """

    def judge(env, under_judgment):
        written = text_of(env, under_judgment)
        ops = _rule_compute(workflow)
        case = Environment(env.storage.sub(judgment),
                           EnvironmentCompute(lambda module, func_name: ops.get(func_name)),
                           env.viba_path)
        put_snapshot(case, written, UNDER_JUDGMENT)
        objections: List[str] = []
        for condition in workflow.conditions():
            answer = interpret(str(_condition_file(condition, workflow, env, acceptance_dir)), case)
            if not finished(answer):
                raise RuntimeError(f"验收条件 {condition} 没跑完：{failure_in(answer)}")
            passed, why = _verdict(answer, condition)
            if not passed:
                objections.append(why or _objection(condition))
        put_snapshot(env, "；".join(objections), "objection")
        return not objections

    caps = {"judge": judge}
    caps.update(_rule_compute(workflow))
    return caps


def _verdict(answer, condition: str) -> Tuple[bool, str]:
    """这条验收条件过了没有；没过的话，它说的那句是什么。

    两种答法：答 `bool` 就是"过了没有"；答一句话就是"没过，原因是这句"——答空串是过了。答一句话
    的那种，是为了让不合格的理由回到做活的人手里（只说一句"某某条件说不合格"，他没法改）。
    """
    said = answer_of(answer)
    piece = None if said is None else getattr(said, "data", None)
    if isinstance(piece, viba_ast.Constant) and isinstance(piece.value, bool):
        return piece.value, ""
    if isinstance(piece, viba_ast.Constant) and isinstance(piece.value, str):
        return not piece.value, str(piece.value)
    raise RuntimeError(f"验收条件 {condition} 答的不是 bool、也不是一句话：{piece!r}")


def _condition_file(condition: str, workflow, env, acceptance_dir):
    """这条验收条件的程序在哪儿。"""
    if acceptance_dir is not None:
        return Path(acceptance_dir) / f"{condition}.viba"
    if getattr(workflow, "root", None) is not None:
        return workflow.condition_file(condition)
    return Path(env.viba_path) / f"{condition}.viba"


def _rule_compute(workflow: Workflow = None) -> Dict[str, Callable]:
    """验收条件能调到的算子：viba 带的那些，加上这一处自己给的这几个。

    一条验收条件能调到什么，跑不出跑它的那个宿主给的表（`viba/builtin.viba` 那些算术、比较、字符串
    也在里面——viba 自己不带库）。它们交回的是**值**（不是地址）：程序把它们当值接着用。

    `workflow` 给不给只影响一个算子：`every_operator_is_declared` 要拿这份规范声明的角色 api 去比。
    不给它，那一条就只认 viba 自带的与解释器原生的名字。
    """

    def under_judgment(env):
        return get_snapshot(env, UNDER_JUDGMENT)

    def widest_parameters(env, code):
        """参数最多的那个函数有几个参数：数最外层括号里的逗号。"""
        widest = 0
        for line in text_of(env, code).splitlines():
            stripped = line.lstrip()
            if stripped.startswith("def ") and "(" in stripped:
                widest = max(widest, _count_parameters(_inside_parentheses(stripped)))
        return widest

    def files_missing(env, root, wanted):
        """一份想要的清单里，那个目录底下没有的那几条。"""
        where = _root_of(env, root)
        have = set()
        if where.is_dir():
            have = {str(one.relative_to(where)) for one in where.rglob("*")}
        return _a_list([one for one in a_list(wanted) if str(one) not in have])

    def every_program_is_complete(env, root):
        """这个目录底下的每份程序都写全了没有：`__decl__` 与 `__impl__` 都在。"""
        for path in _program_files(_root_of(env, root)):
            try:
                source = path.read_text(encoding="utf-8")
                names = {one.name for one in viba_ast.parse(source).body
                         if isinstance(one, viba_ast.TypeDefinition)}
            except (OSError, SyntaxError, UnicodeDecodeError):
                return False
            if "__decl__" not in names or "__impl__" not in names:
                return False
        return True

    def every_operator_is_declared(env, root):
        """这个目录底下的每份程序，调到的名字都有人认领没有。

        认领的四种：这份程序自己声明的、这张角色 api 声明的、viba 自带那一套、解释器自己回答的。
        """
        known = {_bare(one) for one in the_builtin_table()}
        known |= NATIVE_NAMES | SYNTAX_NAMES
        if workflow is not None:
            for api in workflow.apis():
                known |= {_bare(one) for one in workflow.ops_of(api)}
        for path in _program_files(_root_of(env, root)):
            try:
                tree = viba_ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                return False
            local = {one.name for one in tree.body if isinstance(one, viba_ast.TypeDefinition)}
            for name in _called_names(tree):
                if name not in local and name not in known:
                    return False
        return True

    def what_stops_this_norm(env, root):
        """这份法读不起来的话，是哪一步读不起来；读得起来就是空串。

        "四个部分在不在"是 `files_missing` 的事；这一条问的是更实的一件：把它当一份规范读起来——
        那张成员表得是一张表，三个程序目录里得是能跑的程序。读不起来的话就把那句话带回去。
        """
        try:
            _norm_at(_root_of(env, root))
        except Exception as bad:               # noqa: BLE001 - 读不起来就是不合格，原因是这一句
            return f"{type(bad).__name__}: {bad}"
        return ""

    def why_the_intake_says_nothing(env, root, sentences):
        """拿这几句话问这份法的受理：有一句答出"要交的那个名字"就是空串，一句都没有就是那句话。

        哪几句是该问的，是验收条件那一侧的事（一份法认哪几种说法，只有它自己知道）；这一处只负责
        把它读起来、照着它自己的受理跑一遍。
        """
        try:
            workflow = _norm_at(_root_of(env, root))
        except Exception as bad:               # noqa: BLE001
            return f"{type(bad).__name__}: {bad}"
        for one in a_list(sentences):
            try:
                if what_to_make(workflow, str(one)):
                    return ""
            except Exception as bad:           # noqa: BLE001 - 受理跑不起来，原因带回去
                return f"受理跑不起来：{type(bad).__name__}: {bad}"
        return "这几句话，它的受理一句都没答出要交的那个名字"

    def what_stops_this_design(env, path, roots):
        """这份设计文档哪儿不成立；一段都不差就是空串。

        一份设计文档（markdown，主体是 ```viba 段落）要守的三条：
        每一段都编译得过；里面引用到的每个名字都解析得出来；除根以外，每个定义都被别的定义用到。

        哪些算"根"是**验收条件那一侧**说的事（这份文档的顶层是哪几个类型，写文档的人自己知道）；
        这一处只把这三条查一遍，把第一处不对的话带回去。
        """
        where = _root_of(env, path)
        try:
            source = where.read_text(encoding="utf-8")
        except OSError as bad:
            return f"{type(bad).__name__}: {bad}"
        blocks = re.findall(r"```viba\n(.*?)```", source, re.S)
        if not blocks:
            return f"{where} 里一段 ```viba 都没有"
        joined = "".join(blocks)
        try:
            tree = viba_ast.parse(joined)
        except SyntaxError as bad:
            return f"有一段编译不过：{bad}"
        checker = _design_checker(joined)
        if checker is None:
            return "有一段编译不过"
        definitions = checker._definitions_of(_design.ENTRY_MODULE_NAME)   # noqa: SLF001
        used = {name: _names_in(node.body) for name, node in definitions.items()}
        parameters = {one for node in definitions.values()
                      for one in (getattr(node, "generic_params", None) or ())}
        for name, names in sorted(used.items()):
            for one in sorted(names):
                if _bare(one) in definitions or one in parameters:
                    continue
                if _is_a_builtin_name(one):
                    continue
                if checker._resolve(one, _design.ENTRY_MODULE_NAME) is None:  # noqa: SLF001
                    return f"{name} 引用到的 {one} 解析不出来"
        wanted = {str(one) for one in a_list(roots)}
        loose = sorted(name for name in definitions
                       if name not in wanted
                       and not any(name in names for other, names in used.items() if other != name))
        if loose:
            return f"这几个定义了没人用，也不在根里：{'、'.join(loose)}"
        for name in sorted(wanted):
            if name not in definitions:
                return f"说好的根 {name} 这份文档里没有"
        assert tree is not None
        return ""

    table = dict(the_builtin_table())
    table["under_judgment"] = under_judgment
    table["widest_parameters"] = widest_parameters
    table["files_missing"] = files_missing
    table["every_program_is_complete"] = every_program_is_complete
    table["every_operator_is_declared"] = every_operator_is_declared
    table["what_stops_this_norm"] = what_stops_this_norm
    table["why_the_intake_says_nothing"] = why_the_intake_says_nothing
    table["what_stops_this_design"] = what_stops_this_design
    return table


def _is_a_builtin_name(name: str) -> bool:
    """这个名字是不是 viba 自带那一套里的（`Object`、`Oneof`、`list`、`Env`……）。

    `Any` 是个例外：它在自带那一套里查不到（它是顶类型，不是一条定义），所以单列出来。
    """
    if name == "Any":
        return True
    from viba.type import BUILTIN_MODULE, Ok

    return isinstance(BUILTIN_MODULE.lookup(name), Ok)


def _design_checker(source: str):
    """一份设计文档编译出来、并且把包里那几个模块都挂上的那个查名字的东西；编译不过就是 None。"""
    pool = _design._add_file(_design.empty_pool(), _design.ENTRY_FILE_NAME,     # noqa: SLF001
                             _design.ENTRY_MODULE_NAME, source)
    if pool is None:
        return None
    for file_name, module_name, text in _design._dependencies((), ()):          # noqa: SLF001
        added = _design._add_file(pool, file_name, module_name, text)           # noqa: SLF001
        if added is not None:
            pool = added
    return _design._Checker(pool, set())                                        # noqa: SLF001


def _names_in(node) -> set:
    """一段类型表达式里引用到的名字（点分名照原样留着）。"""
    out = set()
    stack = [node]
    while stack:
        here = stack.pop()
        for attr in ("name", "constructor"):
            got = getattr(here, attr, None)
            if isinstance(got, str):
                out.add(got)
        for attr in ("body", "type", "left", "right", "result", "argument", "function",
                     "tagged_type", "type_name", "constructor_name"):
            child = getattr(here, attr, None)
            if child is not None:
                stack.append(child)
        for attr in ("args", "elements"):
            stack.extend(getattr(here, attr, ()) or ())
    return out


def _norm_at(root: Path) -> Workflow:
    """把这个目录当成一份规范读起来：四个部分、那张成员表。

    读不起来就让它抛——调用的人自己决定那算不算不合格。
    """
    workflow = Workflow.load(root)
    members_of(workflow)
    return workflow


def _root_of(env, root) -> Path:
    """这一段字说的那个目录。

    单上交过来的常常是一句话（"本法写在 /…。"）：认得出路径就认路径，认不出就当整段就是那个目录。
    认路径的写法跟受理那一侧同一个（`meeting.a_path_in`）——一处认路径，两处用。
    """
    said = text_of(env, root)
    return Path(a_path_in(said) or said.strip())


def _bare(name: str) -> str:
    """一个名字去掉 `$`、去掉模块那一段：`$sub_env` 是 `sub_env`，`actor.do_step` 是 `do_step`。"""
    return str(name).lstrip("$").split(".")[-1]


def _program_files(root: Path) -> List[Path]:
    """一份法里那几份程序：受理、流程、验收三个目录底下的 `.viba`。

    别的 `.viba`（比如 `constitution.viba` 那样只写类型、不跑的文件）不算程序，不在这儿看。
    """
    out: List[Path] = []
    for part in (INTAKE_DIR, TICKET_PROGRAMS_DIR, ACCEPTANCE_DIR):
        out.extend(sorted((root / part).glob("*.viba")))
    return out


def _called_names(tree) -> List[str]:
    """一份程序调到了哪些名字：每条 `<<` 链最左边那个，去掉 `$` 与模块那一段。"""
    out: List[str] = []
    for node in viba_ast.walk(tree):
        if not isinstance(node, viba_ast.Partial):
            continue
        head = node.function
        while isinstance(head, viba_ast.Partial):
            head = head.function
        for attr in ("tag", "name"):
            got = getattr(head, attr, None)
            if isinstance(got, str):
                out.append(_bare(got))
                break
    return out


def _objection(rule: str) -> str:
    """规则说"不合格"时 critic 说的那句话：是哪条规则说的。"""
    return f"{rule} 说不合格"


def _inside_parentheses(text: str) -> str:
    """第一个 `(` 到它配对的 `)` 之间：`(0, None)` 里的逗号不算参数分隔。"""
    start = text.index("(")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    return text[start + 1:]


def _count_parameters(inside: str) -> int:
    """参数有几个：最外层的逗号加一。"""
    if not inside.strip():
        return 0
    depth, count = 0, 1
    for char in inside:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def critic_handler(meeting_dir, workflow, *, name: str = "critic") -> Handler:
    """一个 critic 处理者：`judge`、规则那几个算子，加上规范里那几张单的程序。"""
    return Handler(name, "critic", {**program_caps(workflow), **critic_caps(workflow)},
                   meeting_dir, programs=workflow.tickets())


def main(argv=None) -> int:
    """critic 的进程：接一轮单，把这一手印成一行 JSON。"""
    from .member import member_main

    def caps_for(_args, workflow):
        return critic_caps(workflow)

    return member_main("critic", caps_for, argv,
                       description="critic 的进程：拿单给的那一份，自己找规则判")


if __name__ == "__main__":
    sys.exit(main())
