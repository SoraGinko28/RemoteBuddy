"""assistant 这一侧：消息先到它手上，它再发工单。

两件事：

1. **把人类说的话放进 store**（`stated_task`，落在案子自己的数据路径下）——消息只经过它这一道；
2. **发一张工单**：把程序放进 `orders/`，自己跑一遍。它做不了的那些步骤当场写成工单，留在 store 里
   （`prepare/<算子>.viba`：`$call` 已固定、`$measured` 还是 nil）。谁的能力表里有那个算子，谁就接。

发单这件事因此有一个可查的凭据：store 里那份文件。assistant 说"我发了这张单"的时候，指的就是它；
它不必知道谁会接（这一版只有 actor 会接）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_centered import _viba  # noqa: F401

from viba.compliance import record_prepare

from ticket_centered.assistant import assistant_caps
from ticket_centered.store import Store

from .orders import program_dir, publish as publish_program
from .service import Report, run_service


def state_the_task(store_root, case: str, words: str, *, record_path=None) -> None:
    """人类刚说的那句话：先做掉这一步——它不在任何工单里，工单只读它的结果。"""
    caps = assistant_caps()
    env = Store(store_root, record_path).env(case, None, case=case)
    caps["stated_task"](env, words)


def publish(store_root, order: str, workflow, ticket: str) -> Path:
    """把这张工单的程序放进 store。"""
    return publish_program(store_root, order, workflow, ticket)


def issue(*, store_root, order: str, case: str, caps: Optional[Dict[str, Callable]] = None,
          record_path=None, program=None) -> Tuple[Report, List[Tuple[str, str, Any]]]:
    """发单：assistant 跑一遍这张工单，做不了的步骤写成工单留在 store 里。

    返回 (这一趟的账, 写下哪几张工单)。
    """
    report = run_service("assistant", "assistant", caps or assistant_caps(),
                         store_root=store_root, order=order, case=case,
                         program=program, record_path=record_path)
    return report, write_work_orders(report, store_root=store_root, case=case,
                                     record_path=record_path, program=program,
                                     order=order)


def write_work_orders(report: Report, *, store_root, case: str, record_path=None,
                      program=None, order: str = "") -> List[Tuple[str, str, Any]]:
    """把这一趟做不了的那一步写成工单：`$call` 就是递延里那一份，`$measured` 留 nil。

    一份工单要两部分，少一个都不成：**viba 代码**（`$call` 加还没填的 `$measured`）和**数据路径**
    （`<数据路径>/prepare/<算子>.viba`）。
    """
    if report.duty is None:
        return []
    step = report.duty.step
    directory = program if program is not None else program_dir(store_root, order)
    env = Store(store_root, record_path).env(step.module_path, str(directory), case=case)
    record_prepare(env, step.func_name, report.duty.call, None)
    return [(step.module_path, step.func_name, report.duty.call)]
