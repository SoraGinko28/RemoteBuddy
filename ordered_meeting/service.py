"""一个成员作为一个服务：一张能力表，在共用的 store 上跑一张工单。

一个服务只有三样：名字、角色、**能力表**（它会哪些算子）。它不像从前那样拿一份发给自己的包——
程序在共用的 store 里（`orders/<工单>/program/`），值也在那儿（`<案子>/<数据路径>/prepare/`）。
跑一遍的结果有三种：

* 能力表里有那个算子：**自己算**，结果落到这条数据路径下，并填进工单的 `$measured`；
* store 里已经有结果：**回放**，同一个副作用不做第二遍（回放不是它干的活）；
* 都没有：**递延**——这一步交给别人，报回来的 `duty` 里有数据路径、算子和 `$call`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_centered import _viba  # noqa: F401
from .orders import program_dir, ticket_of

from viba.interpret import interpret
from viba.type import NotMyDutyException, Ok, UnderlyingVibaOpFailed, VibaProgramErr

from ticket_centered.host import Member
from ticket_centered.store import Store

VIBA_SUFFIX = ".viba"


@dataclass
class Report:
    """一个服务跑完一遍的账：谁、哪张工单、答了哪几步、还欠着哪一步。"""

    service: str
    role: str
    order: str
    ticket: str
    answered: List[Tuple[str, str]] = field(default_factory=list)
    replayed: List[Tuple[str, str]] = field(default_factory=list)
    deferred: List[Tuple[str, str]] = field(default_factory=list)
    failure: str = ""
    value: Any = None
    duty: Optional[NotMyDutyException] = None

    @property
    def worked(self) -> bool:
        """这一趟真答了没有（回放和递延都不是它干的活）。"""
        return bool(self.answered)

    def line(self) -> str:
        """一行账，给人看的。"""
        said = [f"{self.service}（{self.role}）：{self.ticket} 跑完了"]
        if self.answered:
            said.append("答了 " + "；".join(f"{path} + {name}" for path, name in self.answered))
        else:
            said.append("没有它的事")
        if self.replayed:
            said.append(f"回放 {len(self.replayed)} 步")
        if self.deferred:
            path, name = self.deferred[0]
            said.append(f"递延在 {path} + {name}")
        if self.failure:
            said.append(f"没跑完：{self.failure}")
        return "，".join(said)


def run_service(name: str, role: str, caps: Dict[str, Callable], *, store_root, order: str,
                case: str, program=None, record_path=None) -> Report:
    """这个服务在共用的 store 上跑一遍这张工单。"""
    directory = Path(program) if program is not None else program_dir(store_root, order)
    ticket = ticket_of(directory)
    store = Store(store_root, record_path)
    runner = Member(name, role, caps).at(store, case, str(directory))
    answer = interpret(str(directory / f"{ticket}{VIBA_SUFFIX}"), runner.env(""))

    answered = [(path, op) for path, op, value in runner.did if value != "replayed"]
    replayed = [(path, op) for path, op, value in runner.did if value == "replayed"]
    return Report(
        service=name,
        role=role,
        order=order,
        ticket=ticket,
        answered=answered,
        replayed=replayed,
        deferred=list(runner.deferred),
        failure=_failure(answer),
        value=answer.ok_value if isinstance(answer, Ok) else None,
        duty=answer if isinstance(answer, NotMyDutyException) else None,
    )


def _failure(answer) -> str:
    if isinstance(answer, VibaProgramErr):
        return answer.err_msg
    if isinstance(answer, UnderlyingVibaOpFailed):
        return answer.msg
    return ""
