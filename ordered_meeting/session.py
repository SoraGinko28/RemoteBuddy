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
from ticket_centered.workflow import Workflow

from . import assistant as assistant_side
from .orders import owed, program_dir, ticket_of
from .plan import due, next_ticket, workflow_of
from .service import Report, answer_service, run_service

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

    @property
    def the_workflow(self) -> Workflow:
        """这一场读的那份流程：给目录就读那个目录，给的就是流程本身就用它。"""
        if not isinstance(self.workflow, Workflow):
            self.workflow = workflow_of(self.workflow)
        return self.workflow

    def due_ticket(self) -> Optional[str]:
        """该发哪一张：读 store 算，不是写死的次序。"""
        return next_ticket(self.the_workflow, self.store, self.case)

    def due_why(self, ticket: str) -> str:
        """为什么这一张该发（或不该发）：一句话，给人看。"""
        return due(self.the_workflow, self.store, self.case, ticket)[1]

    def published(self) -> bool:
        """这张工单的程序发出去没有。"""
        return assistant_side.published(self.store_root, self.order)

    def publish(self, ticket: Optional[str] = None) -> Any:
        """assistant 发一张工单：把程序放进 store。不给就发 store 说该发的那一张。"""
        ticket = ticket or self.ticket or self.due_ticket()
        if not ticket:
            raise ValueError("没有该发的工单：它要读的那几步还没答完")
        self.ticket = ticket
        return assistant_side.publish(self.store_root, self.order, self.the_workflow, ticket)

    # ------------------------------------------------------------------
    # 一轮里的两件事
    # ------------------------------------------------------------------

    def issue(self) -> Tuple[Report, List[Tuple[str, str, Any]]]:
        """assistant 跑一遍，把做不了的步骤写成工单。"""
        return assistant_side.issue(store_root=self.store_root, order=self.order, case=self.case,
                                    record_path=self.record_path)

    def work(self, name: str, role: str, caps: Dict[str, Callable]) -> Report:
        """一个服务自己跑一遍这张工单（它会的算子当场全答掉）。"""
        return run_service(name, role, caps, store_root=self.store_root, order=self.order,
                           case=self.case, record_path=self.record_path)

    def answer(self, name: str, role: str, caps: Dict[str, Callable]) -> Report:
        """一个服务接单：store 里欠着的、它会的那些算子，照 `$call` 算掉。"""
        return answer_service(name, role, caps, store_root=self.store_root, order=self.order,
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
        """一轮一轮：assistant 跑一遍工单（把下一步的 `$call` 固定成一张工单），服务接单。

        一轮只往前走一步：工单里只留得下已经算好的实参，所以下一步的工单要等上一步答完、assistant
        再跑一遍才写得出来。assistant 这一趟没有递延，就说明整张工单跑到了头。
        """
        if not self.published():
            self.publish()
        reports: List[Report] = []
        work_orders: List[Tuple[str, str, Any]] = []
        for index in range(1, max_rounds + 1):
            issued, written = self.issue()
            reports.append(issued)
            work_orders.extend(written)
            if issued.failure:
                return Outcome(BROKEN, index, reports=reports, work_orders=work_orders)
            if issued.duty is None:
                return Outcome(OK, index, value=issued.value, reports=reports,
                               work_orders=work_orders)

            progressed = False
            for name, role, caps in services:
                answered = self.answer(name, role, caps)
                reports.append(answered)
                progressed = progressed or bool(answered.answered)
            if not progressed:
                return Outcome(STUCK, index, reports=reports, work_orders=work_orders)
        return Outcome(UNFINISHED, max_rounds, reports=reports, work_orders=work_orders)
