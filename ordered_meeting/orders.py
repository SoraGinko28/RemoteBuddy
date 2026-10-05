"""一张工单在 store 里的位置。

store 里有两样东西，缺一样都跑不起来：

* `<store>/orders/<工单>/program/` —— 这份工单的**程序**：几个 api 加上它自己那张单子；
* `<store>/<案子>/<数据路径>/prepare/<算子>.viba` —— 走到哪一步、要哪个算子、这一步拿到了什么。

第二样就是 viba 的**工单**：`$call` 已经固定，`$measured` 还是 nil。它是自足的——数据路径说有哪一步、
要哪个算子，`$call` 说这一步拿到了什么；能力表里有那个算子的服务据此算出结果，落到同一条数据路径下
（`replayed` 读写的 `<数据路径>/value.viba`），再把同一份结果填回 `$measured`。

assistant 发单就是两件事：把程序放进 `orders/`，然后自己跑一遍——它做不了的那一步当场留下上面那份
文件。没有别的：没有发给谁的包，也没有第二份 store。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from ticket_centered import _viba  # noqa: F401

from viba.compliance import call_of, measured_of, read_prepare
from ticket_centered.store import Store

ORDER_DIR = "orders"
PROGRAM_DIR = "program"
VIBA_SUFFIX = ".viba"
API_FILES = ("actor_api.viba", "assistant_api.viba", "critic_api.viba")


def absolute(case: str, module_path: str) -> str:
    """工单里那条相对案子写的数据路径，接上案子变成 store 里的那条路。"""
    if not module_path:
        return case
    if module_path == case or (case and module_path.startswith(f"{case}/")):
        return module_path
    return f"{case}/{module_path}" if case else module_path


def program_dir(store_root, order: str) -> Path:
    """这份工单的程序在哪：`<store>/orders/<工单>/program/`。"""
    return Path(store_root) / ORDER_DIR / order / PROGRAM_DIR


def ticket_of(directory) -> str:
    """程序目录里那张单子的名字（api 文件不算）。"""
    for path in sorted(Path(directory).glob(f"*{VIBA_SUFFIX}")):
        if path.name not in API_FILES:
            return path.name[: -len(VIBA_SUFFIX)]
    raise FileNotFoundError(f"这里没有工单程序：{directory}")


def publish(store_root, order: str, workflow, ticket: str) -> Path:
    """发一张工单：把这份程序放进 store，谁都能照着它跑。"""
    files = sources_of(workflow)
    target = program_dir(store_root, order)
    target.mkdir(parents=True, exist_ok=True)
    # 一张工单的程序目录里只有一张单子：换一张发，先把上一张拿走，别留下两张
    for path in target.glob(f"*{VIBA_SUFFIX}"):
        if path.name not in API_FILES:
            path.unlink()
    for name in API_FILES:
        short = name[: -len(VIBA_SUFFIX)]
        if short in files:
            (target / name).write_text(files[short], encoding="utf-8")
    if ticket not in files:
        raise KeyError(f"这份流程里没有工单 {ticket!r}")
    (target / f"{ticket}{VIBA_SUFFIX}").write_text(files[ticket], encoding="utf-8")
    return target


def sources_of(workflow) -> Dict[str, str]:
    """这份流程的每一份文件（短名 → 内容）：给流程对象就用它的，给目录就读那儿。"""
    if hasattr(workflow, "sources"):
        return dict(workflow.sources())
    root = Path(workflow)
    files = {path.name[: -len(VIBA_SUFFIX)]: path.read_text(encoding="utf-8")
             for path in root.glob(f"*{VIBA_SUFFIX}")}
    tickets = root / "tickets"
    if tickets.is_dir():
        for path in tickets.glob(f"*{VIBA_SUFFIX}"):
            files[path.name[: -len(VIBA_SUFFIX)]] = path.read_text(encoding="utf-8")
    return files


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
