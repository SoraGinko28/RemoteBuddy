"""工单和工单程序分别在哪儿：**数据**在 store 里，**程序**在规范里。

store 里有这几样（都是**数据**）：

* `<store>/orders/<这一趟>/ticket` —— 这一趟发的是哪张单（名字而已，程序不发副本）；
* `<store>/orders/<这一趟>/norm` —— 发的时候读的是哪一版规范（摘要）；
* `<store>/<案子>/<数据路径>/prepare/<算子>.viba` —— 走到哪一步、要哪个算子、这一步拿到了什么。

最后那样 `prepare/<算子>.viba` 就是 viba 的**工单**：`$call` 已经固定，`$measured` 还是 nil。它是自足的——
数据路径说有哪一步、要哪个算子，`$call` 说这一步拿到了什么；能力表里有那个算子的服务据此算出结果，落到同一条数据路径下
（`replayed` 读写的 `<数据路径>/value.viba`），再把同一份结果填回 `$measured`。

工单和规则那几份**程序**一行都不在 store 里：它们属于规范（`RbNorm`），跑的时候现读
（`Workflow.ticket_file` / `search_path`）。assistant 发单就是两件事：在 store 里记下这一趟是哪张单，
然后自己跑一遍——它做不了的那一步当场留下上面那份文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

from ticket_centered import _viba  # noqa: F401

from ticket_centered.prepare import call_of, measured_of, read_prepare
from ticket_centered.store import Store

ORDER_DIR = "orders"
VIBA_SUFFIX = ".viba"
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


def open_orders(store: Store) -> List[Tuple[str, str]]:
    """还没有结果的工单：`$measured` 还是 nil 的那些（不管归谁接）。

    这一份 store 自己就说得清"这一趟跑到哪儿了"：谁接单、谁算完，不看进程还在不在。
    """
    return [(module_path, name) for module_path, name in store.stored()
            if not measured(store, module_path, name)[1]]


def prepared(store: Store, module_path: str, name: str):
    """这条数据路径上那份 Prepare，没有就是 None。"""
    return read_prepare(store.env(module_path), name)


def prepared_call(store: Store, module_path: str, name: str):
    """工单里固定下来的 `$call`：这一步拿到了什么。"""
    return call_of(prepared(store, module_path, name))


def measured(store: Store, module_path: str, name: str) -> Tuple[Any, bool]:
    """(这条数据路径上的结果, 有没有结果)。"""
    return measured_of(prepared(store, module_path, name))


def owed(store: Store, caps: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """欠着的工单：这份 store 里还没有结果、而这张能力表做得了的那些步骤。

    每一条是 (数据路径, 算子, `$call`)。能力表里没有的算子不算欠——那是别人的事。
    """
    outstanding: List[Tuple[str, str, Any]] = []
    for module_path, name in store.stored():
        if name not in caps:
            continue
        _value, answered = measured(store, module_path, name)
        if answered:
            continue
        outstanding.append((module_path, name, prepared_call(store, module_path, name)))
    return outstanding
