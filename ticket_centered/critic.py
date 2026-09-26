"""critic 的进程：判案。判的是哪一份，由工单给；**哪些规则可能被违反，由它自己找**。

    python -m ticket_centered.critic --meeting <会议目录> --case case/quicksort \\
        --bundle <发给我的那一包>

能力表（`critic_caps`）就是 api 里那一件事 `judge`，加上跑规则要用的那几个算子。规则是 viba 程序
（`rules/*.viba`），用仓库自己的 `viba.compliance.is_compliant` 跑——规则自己给门槛、自己答 bool，
这里不替它判。

判的是哪一份，由工单给；critic 把那一份放在案子自己的数据路径下（这一步的 `<数据路径>/judgment`），
规则从那儿读。结论落在这一步的数据路径下：`judge` 是那个 bool，`objection` 是说不合格的那句话。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, List

from . import _viba  # noqa: F401

from viba.compliance import is_compliant, measured_of, read_prepare, record_prepare
from viba.interpret import Environment, EnvironmentCompute, read_snapshot, write_snapshot
from viba.type import Ok

from .host import text_of, value_of
from .member import bundle_main
from .workflow import Workflow

JUDGMENT_DIR = "judgment"
CODE_UNDER_JUDGMENT = "code_under_judgment"

# 判语怎么说：规则名 ->（它量出来的那个数在哪个算子下，那句话）。规则自己给门槛，话由 critic 说。
RULE_WORDINGS = {
    "parameter_count_le_4": ("widest_parameters",
                             "参数最多的那个函数有 {count} 个参数，超过 4 个参数的上限"),
}


def critic_caps(workflow: Workflow, judgment: str = JUDGMENT_DIR) -> Dict[str, Callable]:
    """critic 的能力表：`judge`（拿那一份、自己找规则、逐条判）加上规则那几个算子。"""

    def judge(env, code):
        written = text_of(env, code)
        ops = _rule_compute()
        case = Environment(env.storage.sub(judgment),
                           EnvironmentCompute(lambda module_path, func_name: ops.get(func_name)),
                           env.viba_path)
        write_snapshot(case, written, CODE_UNDER_JUDGMENT)
        objections: List[str] = []
        for rule in workflow.rules():
            answer = is_compliant(str(Path(env.viba_path) / f"{rule}.viba"), case)
            if not isinstance(answer, Ok):
                raise RuntimeError(f"规则 {rule} 没跑完：{answer!r}")
            if not answer.ok_value:
                objections.append(_objection(case, rule))
        record_prepare(env, "objection", None, "；".join(objections))
        return not objections

    caps = {"judge": judge}
    caps.update(_rule_compute())
    return caps


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

    return {name: _recorded(name, impl) for name, impl in (
        ("code_under_judgment", code_under_judgment),
        ("widest_parameters", widest_parameters),
        ("within_limit", within_limit))}


def _recorded(name: str, impl: Callable) -> Callable:
    """跑完把这个数留在这条数据路径下：判语要用它说话。"""

    def op(env, *args):
        value = impl(env, *args)
        record_prepare(env, name, None, value)
        return value

    return op


def _objection(case: Environment, rule: str) -> str:
    """规则说"不合格"时 critic 说的那句话：用它自己量出来的数。"""
    written = RULE_WORDINGS.get(rule)
    if written is None:
        return f"{rule} 说不合格"
    measured_name, wording = written
    measured, answered = measured_of(read_prepare(case, measured_name))
    if not answered:
        return f"{rule} 说不合格"
    return wording.format(count=value_of(measured))


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


def main(argv=None) -> int:
    def caps_for(_args, workflow):
        return critic_caps(workflow)

    return bundle_main("critic", caps_for, argv,
                       description="critic 的进程：拿工单给的那一份，自己找规则判")


if __name__ == "__main__":
    sys.exit(main())
