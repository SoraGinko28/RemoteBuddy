"""一个成员作为一个服务：一张能力表，在共用的 store 上接单。

一个服务只有三样：名字、角色、**能力表**（它会哪些算子）。它不像从前那样拿一份发给自己的包——
程序在共用的 store 里（`orders/<工单>/program/`），值也在那儿（`<案子>/<数据路径>/prepare/`）。
它做两件事：

* **接单**（`answer_service`）：store 里欠着的那些工单，能力表里有哪个算子就照它算哪个——实参从工单
  的 `$call` 来，结果落到同一条数据路径下，并填回 `$measured`。这是"谁是服务"的正脸：一张能力表，
  外加一摞别人写好的工单。
* **跑一遍程序**（`run_service`）：自己会的算子算、store 里已有结果的回放、其余递延。发单那一侧
  （assistant）走的也是这条路，它做不了的那一步就是一张新工单。
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ticket_centered import _viba  # noqa: F401
from .orders import owed, program_dir, ticket_of

from viba import viba_ast
from viba.compliance import record_prepare
from viba.interpret import interpret, viba_data
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


# ----------------------------------------------------------------------
# 接单：照工单里的 `$call` 算，结果填回 `$measured`
# ----------------------------------------------------------------------


def arguments_taken(implementation) -> int:
    """这个实现要几个实参（环境不算）。

    一次调用把实参交给实现，一份工单把那些实参留成一份可序列化数据，所以这个数说的就是怎么再交一遍：
    环境单独一个参数是 0；`$call` 本身就是一个实参是 1；要拆开是 2 以上；签名看不出来（`*args`、
    或者根本读不出来）是 -1，那时整份 `$call` 交过去。
    """
    try:
        parameters = list(inspect.signature(implementation).parameters.values())
    except (TypeError, ValueError):
        return -1
    if any(one.kind is inspect.Parameter.VAR_POSITIONAL for one in parameters):
        return -1
    counted = [one for one in parameters
               if one.kind in (inspect.Parameter.POSITIONAL_ONLY,
                               inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    return len(counted) - 1


def the_arguments(prepared, count: int, func_name: str) -> tuple:
    """一份工单的 `$call` 里那几个实参，按写下来的次序。

    几个实参的调用把它们写成一个带 tag 的积（`$task … * $after …`）；一个实参的调用留的就是那个实参
    本身（它自己可能也是个带 tag 的积），所以整个交过去、不按大小拆；只有环境的调用留 `nil`，那是一个
    实参都没有。实现要几个就交几个，交不出来当场说清楚：能随工单走的只有可序列化数据。
    """
    if count == 1:
        return (prepared,)
    pieces = _the_pieces(prepared)
    if len(pieces) != count:
        raise ValueError(
            f"{func_name!r} 的实现要 {count} 个实参，这份工单里留了 {len(pieces)} 个："
            f"能随工单走的只有可序列化数据")
    return tuple(pieces)


def _the_pieces(prepared) -> list:
    """写下来的那个积，一层一层按书写次序摊开——一份实参是它自己，`nil` 是一个都没有。

    摊开只摊最外面那层：某个实参自己是个积的，整块留着，不再往下拆。
    """
    piece = getattr(prepared, "data", None)
    if piece is None or isinstance(piece, viba_ast.Nil):
        return []
    chain = viba_ast.convert_to_chain_style(piece)
    if not isinstance(chain, viba_ast.ProductChain):
        return [prepared]
    return [viba_data(one.type if isinstance(one, viba_ast.Tagged) else one)
            for one in chain.elements]


def answer_service(name: str, role: str, caps: Dict[str, Callable], *, store_root, order: str,
                   case: str, program=None, record_path=None) -> Report:
    """这个服务接单：store 里欠着的、它会的那些算子，照 `$call` 算掉。"""
    directory = Path(program) if program is not None else program_dir(store_root, order)
    ticket = ticket_of(directory)
    store = Store(store_root, record_path)
    answered: List[Tuple[str, str]] = []
    for module_path, func_name, call in owed(store, caps):
        implementation = caps[func_name]
        env = store.env(module_path, str(directory), case=case)
        value = _computed(implementation, env, call, func_name)
        record_prepare(env, func_name, call, value)
        answered.append((module_path, func_name))
    return Report(service=name, role=role, order=order, ticket=ticket, answered=answered)


def _computed(implementation, env, call, func_name: str):
    """照实现自己的参数个数，把工单里的 `$call` 交回给它。"""
    count = arguments_taken(implementation)
    if count == 0:
        return implementation(env)
    if count < 0:
        return implementation(env, call)
    return implementation(env, *the_arguments(call, count, func_name))


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

    这样它才 import 得到流程里那些 api 用的东西，也 import 得到它自己被写在哪儿。
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
