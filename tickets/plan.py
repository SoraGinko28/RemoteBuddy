"""该发哪一张：读这个案子的值算出来，不是写死的次序。

一张单发得出去，要两个条件都成立：

* 它要读的那几步，案子里都有值了（`addresses_in` 说的那几条数据路径）；
* 它自己那几步，还一步都没答过（`answers_of` 说的那几条）。

两条都是读值得到的，所以谁在什么时候发哪一张是可查的：把这个案子那份值拿出来，照上面两条算一遍，
答案是同一个。
"""

from __future__ import annotations

from typing import Optional, Tuple

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
    """这一张现在该发吗？读这个案子的值算。"""
    for module_path, name in workflow.addresses_in(ticket):
        if not values.answered(module_path, name):
            return False, f"还缺 {module_path or '这个案子'} + {name}"
    for module_path, name in workflow.answers_of(ticket):
        if values.answered(module_path, name):
            return False, f"{module_path or '这个案子'} + {name} 已经答过了"
    return True, "要读的都在，自己还没做过"


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


def ticket_named(meeting_dir, name: str) -> Optional[str]:
    """这场会里发过的那张叫这个名字的单的号；没发过就是 None。

    一个案子里同一张单只发一次：发过就不再发第二张（做完了才算走到下一步）。
    """
    head = f"{name}-"
    for uid in ticket_uids(meeting_dir):
        if uid == name or uid.startswith(head):
            return uid
    return None


__all__ = ["workflow_of", "due", "next_ticket", "issued", "ticket_named", "TICKETS_DIR"]
