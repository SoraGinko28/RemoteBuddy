"""assistant 这一侧：消息先到它手上，它再发工单。

两件事：

1. **把人类说的话放进 store**（`stated_task`，落在案子自己的数据路径下）——消息只经过它这一道；
2. **发一张工单**：把程序放进 `orders/`，自己跑一遍。它做不了的那些步骤当场写成工单，留在 store 里
   （`tickets/<算子>/ticket.viba`：文件里就是那一次调用）。谁的能力表里有那个算子，谁就接。

发单这件事因此有一个可查的凭据：store 里那份文件。assistant 说"我发了这张单"的时候，指的就是它；
它不必知道谁会接（这一版只有 actor 会接）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_centered import _viba  # noqa: F401

from ticket_centered.assistant import assistant_caps
from ticket_centered.store import Store
from ticket_centered.workflow import is_api_file

from .orders import published as order_published, publish as publish_program
from .orders import write_order
from .plan import next_ticket, workflow_of
from .service import Report, run_service


def state_the_task(store_root, case: str, words: str, *, record_path=None) -> None:
    """人类刚说的那句话：先做掉这一步——它不在任何工单里，工单只读它的结果。"""
    caps = assistant_caps()
    env = Store(store_root, record_path).env(case, None, case=case)
    caps["stated_task"](env, words)


def publish(store_root, order: str, workflow, ticket: str) -> Path:
    """发这张工单：在 store 里记下是哪张、哪一版规范（程序在规范目录里，不发副本）。"""
    return publish_program(store_root, order, workflow, ticket)


def issue(*, store_root, order: str, case: str, workflow,
          caps: Optional[Dict[str, Callable]] = None,
          record_path=None) -> Tuple[Report, List[Tuple[str, str, Any]]]:
    """发单：assistant 跑一遍这张工单，做不了的步骤写成工单留在 store 里。

    返回 (这一趟的账, 写下哪几张工单)。
    """
    report = run_service("assistant", "assistant", caps or assistant_caps(),
                         store_root=store_root, order=order, case=case,
                         workflow=workflow, record_path=record_path)
    return report, write_work_orders(report, store_root=store_root,
                                     record_path=record_path)


def write_work_orders(report: Report, *, store_root, record_path=None
                      ) -> List[Tuple[str, str, Any]]:
    """把这一趟做不了的那一步写成工单：文件里就是那一次调用。

    一份工单要两部分，少一个都不成：**viba 数据**（那次调用本身，环境留空）和**数据路径**
    （文件落在 `<数据路径>/tickets/<算子>/ticket.viba` 这条路上）。
    """
    if report.owed is None:
        return []
    module_path, func_name, call = report.owed
    write_order(Store(store_root, record_path), module_path, func_name, call)
    return [(module_path, func_name, call)]


def publish_due(store_root, order: str, workflow, case: str, record_path=None) -> str:
    """发 store 说该发的那一张；一张都不该发就当场说清楚。"""
    source = workflow_of(workflow)
    ticket = next_ticket(source, Store(store_root, record_path), case)
    if ticket is None:
        raise ValueError("store 说没有该发的工单：要读的那几步还没答完")
    publish(store_root, order, source, ticket)
    return ticket


def published(store_root, order: str) -> bool:
    """这一趟发过单没有：store 里有没有那份记录。"""
    return order_published(store_root, order)


def main(argv=None) -> int:
    """assistant 的进程：收消息（`--words`）、发单、把做不了的写成工单。

        python -m ordered_meeting.assistant --store <store> --case <案子> --order <工单> \\
            --workflow <规范目录> --words "人类说的话"

    不给 `--ticket` 就发 store 说该发的那一张（`plan.next_ticket` 读出来的）。一行 JSON 报这一趟：
    发的是哪一张、写下了哪几张工单、递延在哪一步。
    """
    parser = argparse.ArgumentParser(description="assistant 的进程：收消息、发单")
    parser.add_argument("--store", required=True, help="共用的 store")
    parser.add_argument("--case", required=True, help="这张工单跑在哪个案子下")
    parser.add_argument("--order", required=True, help="这一趟（`orders/<这一趟>/`）")
    parser.add_argument("--workflow", required=True, help="规范在哪（工单和 api 在那里面）")
    parser.add_argument("--ticket", default=None, help="发哪一张（不给就发 store 说该发的那一张）")
    parser.add_argument("--words", default=None, help="人类刚说的话")
    args = parser.parse_args(argv)

    workflow = workflow_of(args.workflow)
    if args.words:
        state_the_task(args.store, args.case, args.words)

    if args.ticket:
        publish(args.store, args.order, workflow, args.ticket)
    elif not published(args.store, args.order):
        publish_due(args.store, args.order, workflow, args.case)

    report, work_orders = issue(store_root=args.store, order=args.order, case=args.case,
                                workflow=workflow)
    print(json.dumps({
        "service": report.service,
        "order": report.order,
        "ticket": report.ticket,
        "wrote": [list(pair[:2]) for pair in work_orders],
        "deferred": [list(pair) for pair in report.deferred],
        "failure": report.failure,
    }, ensure_ascii=False))
    return 1 if report.failure else 0


if __name__ == "__main__":
    sys.exit(main())
