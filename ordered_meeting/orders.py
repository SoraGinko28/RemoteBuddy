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
from ticket_centered.workflow import is_api_file

ORDER_DIR = "orders"
PROGRAM_DIR = "program"
VIBA_SUFFIX = ".viba"
TICKET_NAME_FILE = "ticket"
RULES_DIR = "rules"


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
    """程序目录里那张单子的名字。

    一个程序目录里不止有那张单子：几个 api、规则那几份也在（规则要跟单子同目录，`is_compliant` 才
    找得到）。所以发单的时候把单子的名字写进 `ticket` 那份小文件，这里照它读；没有那份文件的老目录
    就退回"不是 api 的那个"。
    """
    directory = Path(directory)
    marked = directory / TICKET_NAME_FILE
    if marked.is_file():
        return marked.read_text(encoding="utf-8").strip()
    for path in sorted(directory.glob(f"*{VIBA_SUFFIX}")):
        if not is_api_file(path.stem):
            return path.stem
    raise FileNotFoundError(f"这里没有工单程序：{directory}")


def publish(store_root, order: str, workflow, ticket: str) -> Path:
    """发一张工单：把这份程序放进 store，谁都能照着它跑。

    一个程序目录里放三样：这张单子、它要调的那几个 api、规则那几份（critic 跑规则时跟单子同一个
    目录才找得到）。换一张发就把上一张和上一批规则清掉，一次只有一张单子的程序。
    """
    files = sources_of(workflow)
    if ticket not in files:
        raise KeyError(f"这份流程里没有工单 {ticket!r}")
    target = program_dir(store_root, order)
    target.mkdir(parents=True, exist_ok=True)
    for path in target.glob(f"*{VIBA_SUFFIX}"):
        if not is_api_file(path.stem):
            path.unlink()
    for name in sorted(name for name in files if is_api_file(name)):
        (target / f"{name}{VIBA_SUFFIX}").write_text(files[name], encoding="utf-8")
    for rule in rules_of(workflow):
        (target / f"{rule}{VIBA_SUFFIX}").write_text(files[rule], encoding="utf-8")
    (target / f"{ticket}{VIBA_SUFFIX}").write_text(files[ticket], encoding="utf-8")
    (target / TICKET_NAME_FILE).write_text(ticket, encoding="utf-8")
    return target


def rules_of(workflow) -> Tuple[str, ...]:
    """这份流程里的规则都叫什么（规则程序跟单子放同一个目录）。"""
    if hasattr(workflow, "rule_names"):
        return tuple(workflow.rule_names)
    root = Path(workflow) / RULES_DIR
    return tuple(sorted(path.name[: -len(VIBA_SUFFIX)] for path in root.glob(f"*{VIBA_SUFFIX}")))


def open_orders(store: Store) -> List[Tuple[str, str]]:
    """还没有结果的工单：`$measured` 还是 nil 的那些（不管归谁接）。

    这一份 store 自己就说得清"这一趟跑到哪儿了"：谁接单、谁算完，不看进程还在不在。
    """
    return [(module_path, name) for module_path, name in store.stored()
            if not measured(store, module_path, name)[1]]


def sources_of(workflow) -> Dict[str, str]:
    """这份流程的每一份文件（短名 → 内容）：给流程对象就用它的，给目录就读那儿。"""
    if hasattr(workflow, "sources"):
        return dict(workflow.sources())
    root = Path(workflow)
    files = {path.name[: -len(VIBA_SUFFIX)]: path.read_text(encoding="utf-8")
             for path in root.glob(f"*{VIBA_SUFFIX}")}
    for directory in (root / "tickets", root / RULES_DIR):
        if directory.is_dir():
            for path in directory.glob(f"*{VIBA_SUFFIX}"):
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
