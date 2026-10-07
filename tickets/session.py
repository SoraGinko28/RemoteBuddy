"""一场会跑在一张一张单上：消息先到 assistant，它发单，处理者接单。

一轮做两件事，顺序不能反：

1. **发单**：读这个案子的值，算出现在该发哪一张（`plan.next_ticket`），把它写成一张单；
2. **接单**：每个处理者接一轮——轮到它的那些单，各自跑一遍、写一篇进度。

值落在案子里，下一轮谁再跑那段程序，跑到那一步就是回放，接着往前走。**到这儿算完**：每一张发出去的
单都做完了（`progress.finished`），而且没有该发的下一张。**走不动**：一整轮下来没有哪一步答出了新值、
也没有哪张单做完——那一步谁都不会，没人接。

一轮只往前走一步，是因为单上只留得下已经算好的实参：下一步的调用要等上一步答完才写得出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from . import issue as issuing
from . import plan
from .binding import Binding
from .handler import Handler
from .layout import case_root, case_of, record_path
from .progress import Stage, finished, unfinished
from .values import Store
from .workflow import Workflow

OK = "ok"
STUCK = "stuck"
UNFINISHED = "unfinished"
BROKEN = "broken"
# 停下的一手里，这一手是"不是它的事"：轮换，不是走不动。
NOT_MINE = "$not_implemented_err"


@dataclass
class Outcome:
    """这一趟的结局：跑到哪儿、走的是哪几张单。"""

    outcome: str
    rounds: int
    case: str = ""
    value: Any = None
    stages: List[Tuple[str, Stage]] = field(default_factory=list)
    failure: str = ""

    def line(self) -> str:
        """一句话，给人看。"""
        said = f"结局：{self.outcome}，{self.rounds} 轮"
        return f"{said}（{self.failure}）" if self.failure else said


class Session:
    """一场会的一趟：一个会议目录、一个案子、一份规范。"""

    def __init__(self, meeting_dir, case: str, *, workflow, record=None,
                 viba_path=None) -> None:
        self.meeting_dir = Path(meeting_dir)
        self.case = case
        self.workflow = workflow
        self.record_path = Path(record) if record is not None else record_path(meeting_dir)
        self.viba_path = viba_path if viba_path is not None else workflow.search_path()

    # ------------------------------------------------------------------
    # 这个案子的值
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """这个案子的值放在哪儿。"""
        return case_root(self.meeting_dir, self.case)

    @property
    def values(self) -> Store:
        """这个案子的值。"""
        return Store(self.root, self.record_path)

    def stated_task(self, words: str, issuer: Handler) -> None:
        """消息先到 assistant 手上：人类说的那句话落在案子自己的数据路径下。

        这一步不在任何单里，单只读它的结果——发单这件事本身也不在单里（它是派单的那一方）。
        """
        make = issuer.caps.get("stated_task")
        if make is None:
            raise RuntimeError(f"{issuer.name} 的能力表里没有 stated_task")
        make(Binding(issuer.name, issuer.caps, self.root, self.viba_path).env(""), words)
        self.values.record(f"- 人类说了一句：{words}")

    # ------------------------------------------------------------------
    # 发单
    # ------------------------------------------------------------------

    def due(self) -> Optional[str]:
        """现在该发哪一张：读这个案子的值算。"""
        return plan.next_ticket(self.workflow, self.values)

    def due_why(self, name: str) -> str:
        """这一张为什么该发（或者不该发）：一句话。"""
        return plan.due(self.workflow, self.values, name)[1]

    def ticket_of(self, name: str) -> Optional[str]:
        """叫这个名字的单发过没有。"""
        return plan.ticket_named(self.meeting_dir, name)

    def issue(self, name: str):
        """发出这一张单，答它落在哪儿。"""
        ticket = issuing.issue(self.meeting_dir, name, values_root=self.root,
                               workflow=self.workflow)
        self.values.record(f"- 发了一张单 {ticket.uid}（跑的是 {name}，案子 {self.case}）")
        return ticket

    # ------------------------------------------------------------------
    # 一轮一轮
    # ------------------------------------------------------------------

    def run(self, handlers: Sequence[Handler], max_rounds: int = 64,
            out: Optional[Callable[[str], None]] = None) -> Outcome:
        """一轮一轮：发单、接单，直到每一张单都做完、也没有该发的下一张。

        `out` 给一行一行看的人：每接一手一行，最后结局一行。
        """
        handlers = list(handlers)
        stages: List[Tuple[str, Stage]] = []
        for index in range(1, max_rounds + 1):
            due = self.due()
            if due is not None and self.ticket_of(due) is None:
                ticket = self.issue(due)
                _say(out, f"- 发了一张单：{ticket.uid}")
            before = set(self.values.stored())
            written: List[Tuple[str, Stage]] = []
            broken = ""
            for handler in handlers:
                for stage in handler.tick():
                    written.append((handler.name, stage))
                    _say(out, handler.line(stage))
                for module_path, func_name in getattr(handler, "did", ()):
                    line = handler.answered_line(module_path, func_name)
                    self.values.record(line)
                    _say(out, line)
                    kind = _stop_kind(stage)
                    if kind and kind != NOT_MINE:
                        broken = f"{handler.name} 在 {stage.uid} 上没跑起来：{stage.stop()}"
            stages.extend(written)
            if broken:
                return Outcome(BROKEN, index, case=self.case, stages=stages, failure=broken)
            advanced = bool(set(self.values.stored()) - before) or any(
                stage.answered() for _who, stage in written)
            if self.due() is None and finished(self.meeting_dir):
                return Outcome(OK, index, case=self.case, stages=stages)
            if not advanced:
                outstanding = unfinished(self.meeting_dir)
                return Outcome(STUCK, index, case=self.case, stages=stages,
                               failure=f"没人接：{'、'.join(outstanding) or '没有下一张单'}")
        return Outcome(UNFINISHED, max_rounds, case=self.case, stages=stages,
                       failure=f"跑满了 {max_rounds} 轮")


def _stop_kind(stage: Stage) -> str:
    """这一手是跑完了、是"不是它的事"，还是没跑起来。"""
    from .binding import stop_kind

    return stop_kind(stage.result)


def open_work(meeting_dir) -> Optional[Tuple[str, str]]:
    """这场会有没有没跑完的一趟：`(案子, 人类那句话)`，没有就是 None。

    说得清"没跑完"的只有这些单自己：还欠着哪一张，是读它的进度得到的——进程在不在、死在哪一步，
    都不必知道。
    """
    outstanding = unfinished(meeting_dir)
    if not outstanding:
        return None
    from .ticket import read_ticket

    ticket = read_ticket(meeting_dir, outstanding[-1])
    if ticket is None:
        return None
    case = case_of(ticket.storage_path, meeting_dir)
    if not case:
        return None
    values = Store(case_root(meeting_dir, case), record_path(meeting_dir))
    words = values.value("", "stated_task")
    return case, str(words) if words is not None else ""


def _say(out: Optional[Callable[[str], None]], line: str) -> None:
    if out is not None:
        out(line)


__all__ = ["Session", "Outcome", "open_work", "OK", "STUCK", "UNFINISHED", "BROKEN"]
