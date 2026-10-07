"""一个成员作为一个服务：一张能力表，在共用的 store 上接单。

一个服务只有三样：名字、角色、**能力表**（它会哪些算子）。它不像从前那样拿一份发给自己的包——
程序在规范里（现读），数据在共用的 store 里（`<案子>/<数据路径>/`）。
它做两件事：

* **接单**（`answer_service`）：store 里欠着的那些工单，能力表里有哪个算子就照它算哪个——跑的就是工单
  上那一次调用（`$call`），值落在同一条数据路径下，这一次尝试写进这张单的进度。这是"谁是服务"的正脸：
  一张能力表，外加一摞别人写好的工单。
* **跑一遍程序**（`run_service`）：自己会的算子算、store 里已有值的回放、其余递延。发单那一侧
  （assistant）走的也是这条路，它做不了的那一步就是一张新工单。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_centered import _viba  # noqa: F401
from .orders import owed, ticket_of

from viba.interpret import exec, interpret

from ticket_centered.host import (Member, answer_of, failure_in, finished,
                                  unimplemented_step)
from ticket_centered.store import Store
from ticket_centered.tickets import write_stage



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
    owed: Optional[Tuple[str, str, Any]] = None

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


@dataclass
class Service:
    """这场会里的一个成员：一个名字、一个角色，和外加一条接单的路。

    接单的路只有两条：**自己算**（`caps` 那张能力表，照工单的 `$call` 算）或者**交给一个进程**
    （`command`：宿主把它跑起来，那个进程照 store 里的工单自己算，账从它印的那行 JSON 读回来）。
    两条路对这场会是一样的：store 里欠着的那几步有人答了。
    """

    name: str
    role: str
    caps: Optional[Dict[str, Callable]] = None
    command: Optional[List[str]] = None
    cwd: Optional[str] = None

    def line(self) -> str:
        """一行，给人看的。"""
        if self.command:
            return f"{self.name}（{self.role}）：{' '.join(self.command)}"
        return f"{self.name}（{self.role}）"


def service_of(one) -> Service:
    """把 `(名字, 角色, 能力表)` 那一对写法也收下来——老的那套调用不用改。"""
    if isinstance(one, Service):
        return one
    name, role, caps = one
    return Service(name, role, caps=caps)


def run_service(name: str, role: str, caps: Dict[str, Callable], *, store_root, order: str,
                case: str, workflow, record_path=None) -> Report:
    """这个服务在共用的 store 上跑一遍这张工单。

    跑的是**规范里**那张单子（`workflow.ticket_file`）：store 里只有"这一趟是哪张单"这条数据，程序
    一行都不发副本。模块按 `workflow.search_path()` 找，所以单子 import 得到根上那几个 api。
    """
    ticket = ticket_of(store_root, order)
    store = Store(store_root, record_path)
    runner = Member(name, role, caps).at(store, case, workflow.search_path())
    answer = interpret(str(workflow.ticket_file(ticket)), runner.env(""))

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
        failure=failure_in(answer),
        value=answer_of(answer),
        owed=unimplemented_step(answer),
    )


# ----------------------------------------------------------------------
# 接单：照工单上那次调用跑一遍
# ----------------------------------------------------------------------


def answer_service(name: str, role: str, caps: Dict[str, Callable], *, store_root, order: str,
                   case: str, workflow, record_path=None) -> Report:
    """这个服务接单：store 里欠着的、它会的那些算子，照工单上那次调用跑一遍。

    跑的就是工单上那一次调用（`$call`），环境是这条数据路径的，实现是它自己这张能力表——和它跑一整张
    工单时走的是同一条路（`Runner._step`）。答下的值落在数据路径下，这一次尝试写进这张单的进度。
    """
    ticket = ticket_of(store_root, order)
    store = Store(store_root, record_path)
    runner = Member(name, role, caps).at(store, case, workflow.search_path())
    failure = ""
    for module_path, func_name, call in owed(store, caps):
        result = exec(call, runner.env(module_path))
        write_stage(store_root, module_path, func_name, name, result)
        if not finished(result):
            failure = failure or failure_in(result)
    return Report(
        service=name,
        role=role,
        order=order,
        ticket=ticket,
        answered=[(path, op) for path, op, value in runner.did if value != "replayed"],
        replayed=[(path, op) for path, op, value in runner.did if value == "replayed"],
        deferred=list(runner.deferred),
        failure=failure,
    )


# ----------------------------------------------------------------------
# 交给一个进程：跑一遍，账从它印的那行 JSON 读回来
# ----------------------------------------------------------------------


def run_command(service: Service, *, store_root, case: str, order: str, cwd=None,
                timeout: Optional[float] = None) -> Report:
    """把这一单交给一个外部进程：宿主把它跑起来，它照 store 里的工单自己算。

    一个进程成员的全部约定就两条：命令后面接着 `--store / --case / --order --phase answer`；算完了把
    「答了哪几步」印成一行 JSON（`ordered_meeting.actor.report_line` 那个写法）。它印不出来（或者根本
    起不来），账上就是没跑完——这一轮那几步没人答，下一轮还可以再来。
    """
    command = list(service.command or [])
    if not command:
        raise ValueError(f"{service.name!r} 不是一个进程成员：没有命令")
    where = cwd or service.cwd
    try:
        done = subprocess.run(command, cwd=str(where) if where else None, env=_child_env(where),
                              capture_output=True, text=True, timeout=timeout, check=False)
    except OSError as bad:
        return _failed(service, order, f"起不来：{bad}")
    reported = _report_of(service, order, done.stdout)
    if reported is not None:
        return reported
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        return _failed(service, order, tail[-1] if tail else f"退出码 {done.returncode}")
    return _failed(service, order, "它没有印出那行 JSON")


def _failed(service: Service, order: str, why: str) -> Report:
    return Report(service=service.name, role=service.role, order=order, ticket="", failure=why)


def _report_of(service: Service, order: str, output: str) -> Optional[Report]:
    """那个进程印的那行 JSON。"""
    for line in reversed((output or "").splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            said = json.loads(text)
        except ValueError:
            continue
        return Report(
            service=said.get("service") or service.name,
            role=said.get("role") or service.role,
            order=said.get("order") or order,
            ticket=said.get("ticket") or "",
            answered=[tuple(one) for one in said.get("answered") or ()],
            replayed=[tuple(one) for one in said.get("replayed") or ()],
            deferred=[tuple(one) for one in said.get("deferred") or ()],
            failure=said.get("failure") or "",
        )
    return None


def _child_env(cwd=None) -> Dict[str, str]:
    """那个进程的环境：照跑起来的这个解释器能 import 到的那几个目录，加上它自己的目录。

    这样它才 import 得到规范里那些 api 用的东西，也 import 得到它自己被写在哪儿。
    """
    env = dict(os.environ)
    seen: List[str] = []
    for entry in [*sys.path, str(cwd) if cwd else ""]:
        if entry and Path(entry).is_dir() and entry not in seen:
            seen.append(entry)
    given = [one for one in env.get("PYTHONPATH", "").split(os.pathsep) if one]
    for one in given:
        if one not in seen:
            seen.append(one)
    env["PYTHONPATH"] = os.pathsep.join(seen)
    return env
