"""一个处理者：它接哪几张单、怎么接、接完把这一手写进进度。

一个处理者只有三样：**名字**（它写的每一条进度都带着这个名字）、**角色**（它实现哪一份 api）、
**能力表**（它会哪些算子）。它做两件事：

* `takes` —— 现在轮到它接哪几张单。读的是进度：**没有 `$ok`**、而且**最后那一篇不是它自己写的**
  那些。后一条就是轮换：甲做一步停下，乙接着做下一步；也是不重复：一个人不会对着同一张单连写两篇
  没有进展的记录。
* `attempt` —— 接一张单：跑单上那次调用（`$call`，环境留空），把这次跑的答复写成一篇文章落在
  `<会议目录>/ticket_progress/<工单号>/` 下。这一次跑里，能算的算子按能力表算、案子里已有值的那几步
  回放、都不是的就答 `$not_implemented`——所以一张单被谁做过、做到哪儿，都在那些文件里。

**单上那次调用的名字是一段程序**（规范里 `tickets/<名字>.viba`）。跑这段程序也是能力表里的一项
（`program_caps`）：谁接单谁就跑同一段程序、同一份存储，只是各自多带一步自己那半边的实现。程序自己
在里面转发停下的那一手（`the_value`），所以进度里那篇停下记录说的仍然是程序里没被接下的那一步——
它的模块、整名和 `$call` 都留着。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import _viba  # noqa: F401

from viba.interpret import exec, interpret

from .binding import (ERR_TAG, OK_TAG, Binding, Forwarded, NO_IMPLEMENTATION_TAG, answer_of,
                      failure_in, finished, stop_kind, unimplemented_step)
from .progress import Stage, a_stage, answered, latest, read_stages, write_stage
from .ticket import Ticket, read_ticket, ticket_uids

# 一次跑在自己里面转发一次失败（`viba.interpret._completed`）；这两种停下是"哪一步不是它的事"，
# 原样答回去，谁的表里有那一步谁就接着做。
FORWARDED_STOPS = (NO_IMPLEMENTATION_TAG, "$underlying_viba_op_err")



def program_caps(workflow) -> Dict[str, Callable]:
    """规范里那几张单的程序，一张一项：名字就是在单上写着的那个。"""
    return {name: the_program(workflow, name) for name in workflow.tickets()}


def the_program(workflow, name: str) -> Callable:
    """这张单要跑的那段程序，当作一个算子来答。

    跑它跑的是**规范里那一份**（`workflow.ticket_file`）：模块按环境上那条找模块的路找，所以程序
    import 得到根上那几个 api。程序在**这次调用自己的环境**里跑（就是这个案子），所以它的每一步
    都落在单上 `$storage_path` 说的那个地方。
    """

    def run(environ, *given):
        return the_value(interpret(str(workflow.ticket_file(name)), environ))

    return run


def the_value(answer):
    """一次跑交回的东西，当作这次调用的答案答回去。

    跑完（`$ok <值>`）就把那个值本身答回去；停在"这一步不是它的事"（或者实现坏了）就把那份停下原样
    答回去——停下来的仍然是程序里那一步，它的模块、整名和 `$call` 都留着；程序根本跑不起来的那几支
    抛出去，让它按"这个实现坏了"记下来。
    """
    if answer.data.tag == OK_TAG:
        return answer.by_tag(OK_TAG)
    stop = answer.by_tag(ERR_TAG)
    if getattr(stop.data, "tag", "") in FORWARDED_STOPS:
        return Forwarded(stop)
    raise RuntimeError(f"这段程序没跑起来：{_reason_of(stop)}")


def _reason_of(stop) -> str:
    """停下那一支的 `$msg`，就这一句。"""
    given = stop.by_tag(getattr(stop.data, "tag", ""))
    if given is None:
        return "它没说为什么"
    said = given.by_tag("$msg")
    return "" if said is None else str(said.value)


class Handler:
    """一个角色，作为一个处理者：接单、算、把这一手写进进度。"""

    def __init__(self, name: str, role: str, caps: Dict[str, Callable], meeting_dir,
                 viba_path: Optional[list] = None, programs=()) -> None:
        self.name = name
        self.role = role
        self.caps: Dict[str, Callable] = dict(caps)
        self.meeting_dir = meeting_dir
        # 表里哪几个名字是"一整段程序"（单上那次调用），不是一个算子（见 `Binding.programs`）。
        self.programs = set(programs)
        # 跑这次调用时按哪儿找模块；不给就看单上写的（`$VIBA_PATH`）。
        self.viba_path = viba_path
        # 这一次接单里真干过的事（哪一步算了、哪一步不是它的事）。
        self.binding: Optional[Binding] = None
        # 这一次接单里答下的那些步：(数据路径, 算子)。回放的不算它干的活。
        self.did: List[Tuple[str, str]] = []

    # ------------------------------------------------------------------
    # 接单
    # ------------------------------------------------------------------

    def takes(self) -> List[Ticket]:
        """现在轮到它接哪几张单：没有 `$ok`，而且最后那一篇不是它写的。"""
        out: List[Ticket] = []
        for uid in ticket_uids(self.meeting_dir):
            if answered(self.meeting_dir, uid):
                continue
            last = latest(self.meeting_dir, uid)
            if last is not None and last.who == self.name:
                continue
            one = read_ticket(self.meeting_dir, uid)
            if one is not None:
                out.append(one)
        return out

    def attempt(self, ticket: Ticket) -> Stage:
        """接一张单：跑单上那次调用，把这次跑的答复写进它的进度。"""
        binding = Binding(self.name, self.caps, ticket.storage_path,
                          self.viba_path if self.viba_path is not None
                          else _path_string(ticket.viba_path),
                          programs=self.programs)
        self.binding = binding
        result = exec(ticket.call, binding.env())
        stage = a_stage(ticket.uid, self.name, result)
        write_stage(self.meeting_dir, stage)
        for module_path, func_name, value in binding.did:
            if value != "replayed":
                self.did.append((module_path, func_name))
        return stage

    def tick(self) -> List[Stage]:
        """接一轮单：现在轮到它的都接掉，一张一篇进度。"""
        self.did = []
        return [self.attempt(one) for one in self.takes()]

    # ------------------------------------------------------------------
    # 这一手的账
    # ------------------------------------------------------------------

    def line(self, stage: Stage) -> str:
        """一篇进度写成一句话，给人看。"""
        return line_of(self.name, self.role, stage)

    def answered_line(self, module_path: str, func_name: str) -> str:
        """答下的一步写成一句话，给人看，也记进这场会的记录。"""
        where = module_path or "这个案子"
        return f"- {self.name}（{self.role}）答了 {where} + {func_name}"


def line_of(who: str, role: str, stage: Stage) -> str:
    """一篇进度写成一句话：谁、哪张单、做完了没有。"""
    called = f"{who}（{role}）"
    if stage.answered():
        return f"{called}：{stage.uid} 做完了"
    return f"{called}：{stage.uid} 停在这儿——{stage.stop()}"


def _path_string(paths) -> str:
    """几个目录写成一串（`Environment.viba_path` 要的那种写法）。"""
    import os
    seen: List[str] = []
    for one in paths or ():
        if one and one not in seen:
            seen.append(one)
    return os.pathsep.join(seen)


def stages_of(meeting_dir, uid: str) -> List[Stage]:
    """一张单的全部尝试，按写的次序。"""
    return read_stages(meeting_dir, uid)


__all__ = ["Handler", "program_caps", "the_program", "the_value", "stages_of",
           "answer_of", "failure_in", "finished", "stop_kind", "unimplemented_step"]
