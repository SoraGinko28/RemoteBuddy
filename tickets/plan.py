"""该发哪一张：读这个案子的值算出来，不是写死的次序。

一张单发得出去，要两个条件都成立：

* 它要读的那几步，案子里都有值了（`addresses_in` 说的那几条数据路径）；
* 它自己那几步，还一步都没答过（`answers_of` 说的那几条）。

两条都是读值得到的，所以谁在什么时候发哪一张是可查的：把这个案子那份值拿出来，照上面两条算一遍，
答案是同一个。

**法还能说一句话**：一份法可以给某几张单写一条"现在是它的时候吗"（`<规范>/due/<单子的名字>.viba`，
没写就没有这一条）。它读这个案子的值，答"该发/还不该发"。有了它，法才写得出"判过了才收尾"这种话——
上面那两条只问哪几步**答过**，问不出那些值**说了什么**。
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from .issue import ticket_uid
from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import interpret

from .layout import TICKETS_DIR
from .ticket import ticket_uids
from .values import Store
from .workflow import Workflow, load_workflow


def workflow_of(root) -> Workflow:
    """这份规范：给目录就读那个目录，给的就是规范本身就用它，不给就是规范目录。"""
    if hasattr(root, "tickets"):
        return root
    return load_workflow(root)


def due(workflow: Workflow, values: Store, ticket: str) -> Tuple[bool, str]:
    """这一张现在该发吗？读这个案子的值算（法写了"什么时候该发"那一条就照它算）。"""
    ready, why = _reads_and_answers(workflow, values, ticket)
    if not ready:
        return False, why
    rule = workflow.due_file(ticket) if hasattr(workflow, "due_file") else None
    if rule is not None:
        said = the_law_says(rule, values, workflow)
        if said is not None:
            return said
    return True, "要读的都在，自己还没做过"


def the_law_holds_it_back(workflow: Workflow, values: Store, ticket: str) -> bool:
    """这一张是不是法扣着不发：别的都不缺，就是法说"还不是它的时候"。

    这一条与"没人接"、与"还缺哪一步"都不一样：那两种是这一场会自己的事，这一种是法的话——它是说
    这一次的活还没到能收的地步（比如判决还没过）。
    """
    ready, _why = _reads_and_answers(workflow, values, ticket)
    if not ready:
        return False
    rule = workflow.due_file(ticket) if hasattr(workflow, "due_file") else None
    if rule is None:
        return False
    said = the_law_says(rule, values, workflow)
    return said is not None and not said[0]


def _reads_and_answers(workflow: Workflow, values: Store, ticket: str) -> Tuple[bool, str]:
    """那两条老规矩：要读的都在、自己还没答过。"""
    for module_path, name in workflow.addresses_in(ticket):
        if not values.answered(module_path, name):
            return False, f"还缺 {module_path or '这个案子'} + {name}"
    for module_path, name in workflow.answers_of(ticket):
        if values.answered(module_path, name):
            return False, f"{module_path or '这个案子'} + {name} 已经答过了"
    return True, "要读的都在，自己还没做过"


def the_law_says(rule, values: Store, workflow) -> Optional[Tuple[bool, str]]:
    """这份法给这一张单写的"什么时候该发"：跑它，答（该发吗，为什么）；法没表态就是 None。

    它读的是这个案子：这一处给它的算子是 `answered`（这一步答过没有）与 `the_value`（那一步答下的
    值），加上 viba 自带那一套（比较、算术、字符串）。答 bool 就是"该发吗"；答一句话就是"还不该发，
    原因是这句"（空串是"该发"）；答 `nil` 是"法没表态"，照旧按上面那两条算。
    """
    from .binding import answer_of, failure_in, finished

    env = values.env("", os.pathsep.join(workflow.search_dirs()), _the_case_ops(values))
    answer = interpret(str(rule), env)
    if not finished(answer):
        raise RuntimeError(f"法写的「什么时候该发」没跑完：{failure_in(answer)}")
    said = answer_of(answer)
    piece = None if said is None else getattr(said, "data", None)
    if isinstance(piece, viba_ast.Constant) and isinstance(piece.value, bool):
        return (piece.value,
                "法说现在就是它的时候" if piece.value else "法说现在还不是它的时候")
    if isinstance(piece, viba_ast.Constant) and isinstance(piece.value, str):
        return (not piece.value,
                str(piece.value) or "法说现在就是它的时候")
    if piece is None or isinstance(piece, viba_ast.Nil):
        return None
    raise RuntimeError(f"法写的「什么时候该发」答的不是 bool、也不是一句话：{piece!r}")


def _the_case_ops(values: Store):
    """跑"什么时候该发"那一条时给它的算子：读这个案子的值。"""
    from .binding import text_of
    from .builtins import the_table

    table = dict(the_table())

    def answered(env, module_path, func_name):
        return values.answered(text_of(env, module_path), text_of(env, func_name))

    def the_value(env, module_path, func_name):
        return values.value(text_of(env, module_path), text_of(env, func_name))

    table["answered"] = answered
    table["the_value"] = the_value
    return lambda module, name: table.get(f"{module}.{name}" if module else name) or table.get(name)


def next_ticket(workflow: Workflow, values: Store) -> Optional[str]:
    """头一张该发的单，一张都不该发就是 None。"""
    for ticket in workflow.tickets():
        wanted, _why = due(workflow, values, ticket)
        if wanted:
            return ticket
    return None


def issued(meeting_dir) -> list:
    """这场会发过哪些单，按号排。"""
    return ticket_uids(meeting_dir)


def ticket_named(meeting_dir, name: str, values_root=None) -> Optional[str]:
    """这场会里那张叫这个名字的单的号；没发过就是 None。

    一个案子里同一张单只发一次：发过就不再发第二张（做完了才算走到下一步）。号是 `<名字>-<案子>`
    （`issue.ticket_uid`），所以给了 `values_root`（那个案子的值那一层）就只认这个案子的那一张；
    不给就认这场会里叫这个名字的任何一张（页面上清点用）。
    """
    if values_root is not None:
        uid = ticket_uid(name, values_root)
        return uid if uid in ticket_uids(meeting_dir) else None
    head = f"{name}-"
    for uid in ticket_uids(meeting_dir):
        if uid == name or uid.startswith(head):
            return uid
    return None


__all__ = ["workflow_of", "due", "next_ticket", "issued", "ticket_named", "TICKETS_DIR"]
