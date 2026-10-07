"""工单和工单程序分别在哪儿：**数据**在 store 里，**程序**在规范里。

store 里有这几样（都是**数据**）：

* `<store>/orders/<这一趟>/ticket` —— 这一趟发的是哪张单（名字而已，程序不发副本）；
* `<store>/orders/<这一趟>/norm` —— 发的时候读的是哪一版规范（摘要）；
* `<store>/<案子>/<数据路径>/tickets/<算子>/ticket.viba` —— 一张**工单**：文件里就是那次调用
  （`__dyn_call__ << "<整名>" << 实参…`），环境留空——给它一个环境就是跑它。它的数据路径不写在文件
  里：文件落在哪条路径下，就是哪条路径上的一步。
* 同一张单旁边的 `progress/` —— 它被处理的每一次：谁试的、那次跑答了什么（`tickets.py`）。

**答下的值**落在同一条数据路径下（`<数据路径>/<算子>.viba`）：那是 viba 自己的快照，谁再跑同一个
调用读到的就是它（`store.Store.read`）。工单是"欠着哪一步"，值是"这一步答了没有"，进度是账。

工单和规则那几份**程序**一行都不在 store 里：它们属于规范（`RbNorm`），跑的时候现读
（`Workflow.ticket_file` / `search_path`）。assistant 发单就是两件事：在 store 里记下这一趟是哪张单，
然后自己跑一遍——它做不了的那一步当场写成上面那张工单。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from ticket_centered import _viba  # noqa: F401

from ticket_centered.store import Store
from ticket_centered.tickets import read_ticket, tickets_in, write_ticket

ORDER_DIR = "orders"
TICKET_NAME_FILE = "ticket"
NORM_FILE = "norm"
# 老那些 store（规范还没搬走时发的单）把程序副本和单名放在这个子目录里；新的一律不发副本
OLD_PROGRAM_DIR = "program"


def absolute(case: str, module_path: str) -> str:
    """工单里那条相对案子写的数据路径，接上案子变成 store 里的那条路。"""
    if not module_path:
        return case
    if module_path == case or (case and module_path.startswith(f"{case}/")):
        return module_path
    return f"{case}/{module_path}" if case else module_path


def order_dir(store_root, order: str) -> Path:
    """这一趟的单子记在哪儿：`<store>/orders/<这一趟>/`。"""
    return Path(store_root) / ORDER_DIR / order


def publish(store_root, order: str, workflow, ticket: str) -> Path:
    """发一张工单：在 store 里记下这一趟是哪张单、哪一版规范。程序本身不发副本。

    发单是**数据**：程序在规范目录里，一条会一条都现读，所以 store 里只留这张单子的名字和发它时的
    规范摘要（哪一版规范发的，事后查得到）。
    """
    if ticket not in workflow.tickets():
        raise KeyError(f"这份规范里没有工单 {ticket!r}")
    target = order_dir(store_root, order)
    target.mkdir(parents=True, exist_ok=True)
    (target / TICKET_NAME_FILE).write_text(ticket, encoding="utf-8")
    (target / NORM_FILE).write_text(workflow.digest(), encoding="utf-8")
    return target


def ticket_of(store_root, order: str) -> str:
    """这一趟发的是哪张单：读 store 里那份记录。

    没有记录就是这个案子还没发过单（或者发单的那一步没走完）——当场说清楚，不要拿别的单顶上。
    老那些 store（规范还没搬走时发的单）把名字写在程序副本目录里，照样读得到。
    """
    for marked in (order_dir(store_root, order) / TICKET_NAME_FILE,
                   order_dir(store_root, order) / OLD_PROGRAM_DIR / TICKET_NAME_FILE):
        if marked.is_file():
            return marked.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"这里没有发过单：{order_dir(store_root, order)}")


def published(store_root, order: str) -> bool:
    """这一趟发过单没有：store 里有没有那份记录。"""
    return any((order_dir(store_root, order) / name / TICKET_NAME_FILE).is_file()
               for name in ("", OLD_PROGRAM_DIR))


def norm_of(store_root, order: str) -> str:
    """这一趟发的时候读的是哪一版规范（摘要）；没有就空着。"""
    marked = order_dir(store_root, order) / NORM_FILE
    return marked.read_text(encoding="utf-8").strip() if marked.is_file() else ""


# ----------------------------------------------------------------------
# 一步一步的工单：一次调用，和它答下的值
# ----------------------------------------------------------------------


def write_order(store: Store, module_path: str, name: str, call) -> Path:
    """把一步没人实现的调用写成一张工单：数据路径由文件落在哪儿说。"""
    return write_ticket(store.root, module_path, name, call)


def the_call(store: Store, module_path: str, name: str):
    """这一张单上那次调用（一个节点）；没发过就是 None。"""
    return read_ticket(store.root, module_path, name)


def orders(store: Store) -> List[Tuple[str, str, Any]]:
    """这份 store 里发过的工单：`(数据路径, 算子, 那次调用)`。"""
    return [(module_path, name, read_ticket(store.root, module_path, name))
            for module_path, name in tickets_in(store.root)]


def open_orders(store: Store) -> List[Tuple[str, str]]:
    """还没有值的工单：这一步还没有人答。

    这一份 store 自己就说得清"这一趟跑到哪儿了"：谁接单、谁算完，不看进程还在不在。
    """
    return [(module_path, name) for module_path, name in tickets_in(store.root)
            if not store.answered(module_path, name)]


def measured(store: Store, module_path: str, name: str) -> Tuple[Any, bool]:
    """(这一步答下的值, 答了没有)。"""
    return store.read(module_path, name)


def owed(store: Store, caps) -> List[Tuple[str, str, Any]]:
    """欠着的工单：这份 store 里还没有值、而这张能力表做得了的那些步骤。

    每一条是 (数据路径, 算子, 那次调用)。能力表里没有的算子不算欠——那是别人的事。
    """
    outstanding: List[Tuple[str, str, Any]] = []
    for module_path, name, call in orders(store):
        if name not in caps or store.answered(module_path, name):
            continue
        outstanding.append((module_path, name, call))
    return outstanding
