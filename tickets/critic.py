"""critic 这一侧：判案。判的是哪一份由单上那次调用给；**哪些规则可能被违反，由它自己找**。

能力表（`critic_caps`）就是 api 里那一件事 `judge`，加上跑规则要用的那几个算子。规则是 viba 程序
（`rules/*.viba`），用 `interpret` 跑——规则自己给门槛、自己答 bool，这里不替它判。

判的是哪一份由单给，critic 把那一份放在这条数据路径下（`judgment/code_under_judgment`），规则从那儿
读。结论落在这条数据路径下：`judge` 是那个 bool，`objection` 是说不合格的那句话。

这一段是一个**处理者**（`handler.Handler`）：名字、角色 `critic`、上面那张能力表，加上规范里那几张单
的程序。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, List

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import (Environment, EnvironmentCompute, interpret, read_snapshot,
                            write_snapshot)

from .binding import answer_of, failure_in, finished, text_of, value_of
from .handler import Handler, program_caps
from .workflow import Workflow

JUDGMENT_DIR = "judgment"
CODE_UNDER_JUDGMENT = "code_under_judgment"

# 判语上这一处只说"是哪条规则说不合格"：规则叫什么、要说什么话，都是规范的事，这一处不认识它们。
# （要规则自己说那句话，就得有一处 api 让它把话交出来，那是另一样东西。）


def critic_caps(workflow: Workflow, judgment: str = JUDGMENT_DIR,
                rules_dir=None) -> Dict[str, Callable]:
    """critic 的能力表：`judge`（拿那一份、自己找规则、逐条判）加上规则那几个算子。

    `rules_dir` 是规则程序在哪儿；不给就看这份流程是从哪个目录读出来的（规范那一套：规则的代码在
    `<规范>/rules/`，由 `Workflow.rule_file` 指）。两份都没有（流程是拿现成的源码拼的）才退回这次
    运行的那个 `viba_path`。
    """

    def judge(env, code):
        written = text_of(env, code)
        ops = _rule_compute()
        case = Environment(env.storage.sub(judgment),
                           EnvironmentCompute(lambda module, func_name: ops.get(func_name)),
                           env.viba_path)
        write_snapshot(case, written, CODE_UNDER_JUDGMENT)
        objections: List[str] = []
        for rule in workflow.rules():
            answer = interpret(str(_rule_file(rule, workflow, env, rules_dir)), case)
            if not finished(answer):
                raise RuntimeError(f"规则 {rule} 没跑完：{failure_in(answer)}")
            if not _verdict(answer, rule):
                objections.append(_objection(rule))
        write_snapshot(env, "；".join(objections), "objection")
        return not objections

    caps = {"judge": judge}
    caps.update(_rule_compute())
    return caps


def _verdict(answer, rule: str) -> bool:
    """规则跑完答下的那个 bool；它不是 bool 就当场说清楚。"""
    said = answer_of(answer)
    piece = None if said is None else getattr(said, "data", None)
    if not (isinstance(piece, viba_ast.Constant) and isinstance(piece.value, bool)):
        raise RuntimeError(f"规则 {rule} 答的不是 bool：{piece!r}")
    return piece.value


def _rule_file(rule: str, workflow, env, rules_dir):
    """这条规则的程序在哪儿。"""
    if rules_dir is not None:
        return Path(rules_dir) / f"{rule}.viba"
    if getattr(workflow, "root", None) is not None:
        return workflow.rule_file(rule)
    return Path(env.viba_path) / f"{rule}.viba"


def _rule_compute() -> Dict[str, Callable]:
    """规则能调到的算子。它们交回的是**值**（不是地址）：规则程序把它们当值接着用。"""

    def code_under_judgment(env):
        return read_snapshot(env, CODE_UNDER_JUDGMENT)

    def widest_parameters(env, code):
        """参数最多的那个函数有几个参数：数最外层括号里的逗号。"""
        widest = 0
        for line in text_of(env, code).splitlines():
            stripped = line.lstrip()
            if stripped.startswith("def ") and "(" in stripped:
                widest = max(widest, _count_parameters(_inside_parentheses(stripped)))
        return widest

    def within_limit(env, count, limit):
        return value_of(count) <= value_of(limit)

    return {"code_under_judgment": code_under_judgment,
            "widest_parameters": widest_parameters,
            "within_limit": within_limit}


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
