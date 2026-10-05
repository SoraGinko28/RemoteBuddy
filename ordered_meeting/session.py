"""一场会的一轮一轮：assistant 发单，服务接单。

一轮做两件事，顺序不能反：

1. **assistant 发单**：跑一遍工单程序，它做不了的那一步写成工单（`prepare/<算子>.viba`）；
2. **每个服务跑一遍**：自己会的算子算、store 里有结果的回放、其余递延。

服务答下的值落在 store 里，下一轮的 assistant 跑到那里就直接回放，接着往前走。跑到哪一步都没有欠账、
程序给出结果，就是 `ok`；一轮下来谁都没答出东西、程序也没跑完，就是 `stuck`（那一份工单没人接）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ticket_centered.store import Store

from . import assistant as assistant_side
from .orders import owed, program_dir, ticket_of
from .service import Report, run_service

OK = "ok"
STUCK = "stuck"
UNFINISHED = "unfinished"
BROKEN = "broken"


@dataclass
class Outcome:
    outcome: str
    rounds: int
    value: Any = None
    reports: List[Report] = field(default_factory=list)
    work_orders: List[Tuple[str, str, Any]] = field(default_factory=list)

    def line(self) -> str:
        return f"结局：{self.outcome}，{self.rounds} 轮"


class Session:
    """一场会：一份 store，一个案子，一张已经发出去的工单。"""

    def __init__(self, store_root, case: str, order: str, *, workflow=None, ticket=None,
                 record_path=None):
        self.store_root = store_root
        self.case = case
        self.order = order
        self.workflow = workflow
        self.ticket = ticket
        self.record_path = record_path

    # ------------------------------------------------------------------
    # store 上那两样东西
    # ------------------------------------------------------------------

    @property
    def store(self) -> Store:
        return Store(self.store_root, self.record_path)

    @property
    def program(self):
        return program_dir(self.store_root, self.order)

    def state_the_task(self, words: str) -> None:
        """消息先到 assistant 手上：先把人类说的话放进 store。"""
        assistant_side.state_the_task(self.store_root, self.case, words,
                                      record_path=self.record_path)

    def publish(self, ticket: Optional[str] = None) -> Any:
        """assistant 发一张工单：把程序放进 store。"""
        ticket = ticket or self.ticket
        if not ticket:
            raise ValueError("发单要说清是哪一张（ticket）")
        self.ticket = ticket
        return assistant_side.publish(self.store_root, self.order, self.workflow, ticket)

    # ------------------------------------------------------------------
    # 一轮里的两件事
    # ------------------------------------------------------------------

    def issue(self) -> Tuple[Report, List[Tuple[str, str, Any]]]:
        """assistant 跑一遍，把做不了的步骤写成工单。"""
        return assistant_side.issue(store_root=self.store_root, order=self.order, case=self.case,
                                    record_path=self.record_path)

    def work(self, name: str, role: str, caps: Dict[str, Callable]) -> Report:
        """一个服务跑一遍这张工单。"""
        return run_service(name, role, caps, store_root=self.store_root, order=self.order,
                           case=self.case, record_path=self.record_path)

    def owed(self, caps: Dict[str, Callable]) -> List[Tuple[str, str, Any]]:
        """欠着的工单：还没有结果、而这张能力表做得了的那些步骤。"""
        return owed(self.store, caps)

    @property
    def ticket_name(self) -> str:
        return ticket_of(self.program)

    # ------------------------------------------------------------------
    # 一轮一轮跑到头
    # ------------------------------------------------------------------

    def run(self, services: Sequence[Tuple[str, str, Dict[str, Callable]]],
            max_rounds: int = 1024) -> Outcome:
        """一轮一轮：assistant 发单，每个服务接一遍。"""
        reports: List[Report] = []
        work_orders: List[Tuple[str, str, Any]] = []
        for index in range(1, max_rounds + 1):
            issued, written = self.issue()
            reports.append(issued)
            work_orders.extend(written)
            if issued.failure:
                return Outcome(BROKEN, index, reports=reports, work_orders=work_orders)

            progressed = False
            for name, role, caps in services:
                report = self.work(name, role, caps)
                reports.append(report)
                if report.failure:
                    return Outcome(BROKEN, index, reports=reports, work_orders=work_orders)
                if report.value is not None:
                    return Outcome(OK, index, value=report.value, reports=reports,
                                   work_orders=work_orders)
                progressed = progressed or report.worked
            if not progressed:
                return Outcome(STUCK, index, reports=reports, work_orders=work_orders)
        return Outcome(UNFINISHED, max_rounds, reports=reports, work_orders=work_orders)
