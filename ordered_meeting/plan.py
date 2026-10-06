"""该发哪一张：读 store 算出来，不是写死的次序。

一张工单发得出去，要两个条件都成立：

* 它要读的那几步，store 里都有结果了（`$call` 里写着的那些地址，照着去读）；
* 它自己那几步，还一步都没答过。

两条都是读 store 得到的，所以谁在什么时候发哪一张是可查的：把 store 拿出来，照上面两条算一遍，答案
是同一个。这与 `ticket_centered` 里那套"该发哪一张"是同一个读法，只是值不再装进发给谁的包，而在共用
的那份 store 里。
"""

from __future__ import annotations

from typing import Optional, Tuple

from .orders import absolute

from ticket_centered.store import Store
from ticket_centered.workflow import Workflow, load_workflow


def workflow_of(root) -> Workflow:
    """这份规范：给目录就读那个目录，给的就是规范本身就用它，不给就是规范目录。"""
    if hasattr(root, "tickets"):
        return root
    return load_workflow(root)


def due(workflow: Workflow, store: Store, case: str, ticket: str) -> Tuple[bool, str]:
    """这一张现在该发吗？读 store 算。"""
    for module_path, name in workflow.addresses_in(ticket):
        wanted = absolute(case, module_path)
        if not store.answered(wanted, name):
            return False, f"还缺 {wanted} + {name}"
    for module_path, name in workflow.answers_of(ticket):
        done = absolute(case, module_path)
        if store.answered(done, name):
            return False, f"{done} + {name} 已经答过了"
    return True, "要读的都在，自己还没做过"


def next_ticket(workflow: Workflow, store: Store, case: str) -> Optional[str]:
    """头一张该发的工单，一张都不该发就是 None。"""
    for ticket in workflow.tickets():
        wanted, _why = due(workflow, store, case, ticket)
        if wanted:
            return ticket
    return None
