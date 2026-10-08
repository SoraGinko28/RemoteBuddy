"""一张工单的进度：**一次尝试一个文件**，只增不删。

```
<会议目录>/ticket_progress/<工单号>/<写入时刻>-<谁>.viba
```

一篇进度写成什么，不是这一处写的：成员的名字、次序、类型都在接口仓库那份 `ticket.viba` 里
（`TicketStage`，见 `declare.py`），整份进度是它的那一列（`TicketProgress = AppendOnlyList[TicketStage]`）。
这一处只做三件事：把值按 `TicketStage` 排好、写成一份快照；照它一个一个读回来；要整份的时候，把那些
文件读成那一列（`progress_node`）。

文件名带写入时刻和是谁写的，所以两个人同时写也是两个文件，**只增不删不需要锁**。一张单做完了没有，
读的是它这一整份进度里**有没有**一条 `$ok`（`answered`）：这是那个只从"没有"变成"有"的答案，
后来写下的停下记录顶不掉它。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.interpret import (Environment, EnvironmentStorage, read_snapshot,
                            write_snapshot)
from viba.reflect import access as reflect_access
from viba.type import VibaProgramErr

from .declare import progress_definition, stage_definition
from .layout import PROGRESS_DIR, VIBA_SUFFIX
from .ticket import a_time, ticket_uids


def stage_name(who: str, when: Optional[datetime] = None) -> str:
    """一次尝试的文件名：`<写入时刻>-<谁>`，时刻是世界时。

    写成能按名字排序的那种（`20261002T030405.123456Z`），名字序就是写的次序；时刻里没有冒号
    （文件名在哪个系统上都得是一段路径）。
    """
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{who}"


@dataclass
class Stage:
    """谁在某一张单上试了一次，那次跑交回了什么。"""

    uid: str
    who: str
    result: Any                # 那次跑的答复，viba 数据（`InterpretResult`）
    time: str

    def answered(self) -> bool:
        """这一次跑答下了一个值没有（答复上是 `$ok`）。"""
        from .binding import finished

        return finished(self.result)

    def stop(self) -> str:
        """这一次跑停下的那句话；答下了值就是空串。"""
        from .binding import failure_in, unimplemented_step

        if self.answered():
            return ""
        step = unimplemented_step(self.result)
        if step is not None:
            # `$full_qualified_func_name` 就是那一步的整名（`actor_api.acknowledge`），队伍就在它里面。
            return f"不是它的事：{step[1] or step[0]}"
        return failure_in(self.result)

    def the_values(self) -> dict:
        """这一次尝试那几个成员的值，按名字给（次序由那份定义说）。"""
        return {"$ticket_uid": self.uid,
                "$handler_name": self.who,
                "$interpret_result": self.result,
                "$time": self.time}

    def as_node(self):
        """这一篇写成的那一段 viba 数据：写完照 `TicketStage` 核一遍。"""
        definition = stage_definition()
        node = viba_ast.ProductChain([
            viba_ast.Tagged(tag, _piece(value))
            for tag, value in definition.the_fields(self.the_values())])
        definition.check(node)
        return node


# ----------------------------------------------------------------------
# 盘上那几个动作
# ----------------------------------------------------------------------


def progress_dir(meeting_dir, uid: str) -> Path:
    """这一张单的进度放在哪儿。"""
    return Path(meeting_dir) / PROGRESS_DIR / uid


def write_stage(meeting_dir, stage: "Stage") -> Path:
    """把这一次尝试追加到这份进度里，答它落在哪儿。

    名字里带写入时刻和是谁写的，所以同一张单被同一个人试两次也是两个文件。
    """
    name = stage_name(stage.who)
    write_snapshot(_environ(meeting_dir, stage.uid), stage.as_node(), name)
    return progress_dir(meeting_dir, stage.uid) / f"{name}{VIBA_SUFFIX}"


def read_stages(meeting_dir, uid: str) -> List["Stage"]:
    """这张单被处理的每一次，按写的次序。

    次序就是文件名的次序，而名字以写入时刻开头，所以进度读出来就是它被追加的次序。读不出来的那一篇
    跳过：一篇写坏了的记录不是丢掉它周围那些记录的理由。
    """
    where = progress_dir(meeting_dir, uid)
    if not where.is_dir():
        return []
    out: List[Stage] = []
    for path in sorted(where.glob(f"*{VIBA_SUFFIX}")):
        try:
            stored = read_snapshot(_environ(meeting_dir, uid), path.stem)
        except Exception:                 # 这一篇读不出来
            continue
        if stored is None:
            continue
        try:
            out.append(stage_of_node(stored))
        except Exception:                 # 这一篇不是一个阶段
            continue
    return out


def stage_of_node(node) -> "Stage":
    """这一段 viba 数据当成一篇进度来读：成员照那份 `TicketStage` 一个一个取。"""
    fields = {}
    for tag in stage_definition().tags:
        got = node.by_tag(tag)
        if tag == "$interpret_result":
            fields["result"] = got
        else:
            fields[tag[1:]] = _leaf(got)
    return Stage(uid=fields["ticket_uid"], who=fields["handler_name"],
                 result=fields["result"], time=fields["time"])


def a_stage(uid: str, who: str, result, when: Optional[datetime] = None) -> "Stage":
    """一篇写好、等着落盘的进度。"""
    return Stage(uid=uid, who=who, result=result, time=a_time(when))


def progress_node(meeting_dir, uid: str):
    """这张单的全部进度当成**一份** viba 数据：那一列 `TicketProgress`。

    盘上是一行一项（一个文件），这里读成它本来那样：一列，按写的次序。读不出来的那一篇不在列里
    （`read_stages` 同上）。列本身照 `TicketProgress` 核一遍——它是那份定义说的那种才交出去。
    """
    definition = progress_definition()
    node = viba_ast.TypeApp("ListLiteral", [stage.as_node() for stage in read_stages(meeting_dir, uid)])
    definition.check(node)
    return node


# ----------------------------------------------------------------------
# 这份进度说了什么
# ----------------------------------------------------------------------


def latest(meeting_dir, uid: str) -> Optional["Stage"]:
    """这张单最后被谁试过，没人试过就是 None。"""
    stages = read_stages(meeting_dir, uid)
    return stages[-1] if stages else None


def answered(meeting_dir, uid: str) -> bool:
    """这张单的进度里有一条 `$ok` 吗？这就是"做完了"。

    是整个系统停下来的条件，而且它只从"没有"变成"有"：进度只增，一条答下了值的记录就在那儿了，
    后来某个人读到"还没做完"、动手时别人已经做完，它写下的那条停下记录落在那条 `$ok` 之后，
    顶不掉那个答案。
    """
    return any(stage.answered() for stage in read_stages(meeting_dir, uid))


def finished(meeting_dir) -> bool:
    """这场会发过的每一张单都做完了吗。"""
    return all(answered(meeting_dir, uid) for uid in ticket_uids(meeting_dir))


def unfinished(meeting_dir) -> List[str]:
    """还没做完的那几张单，按号排。"""
    return [uid for uid in ticket_uids(meeting_dir) if not answered(meeting_dir, uid)]


def the_state(meeting_dir) -> List[dict]:
    """一行一张单：它的进度到这会儿是什么样。给人看的清点。"""
    out = []
    for uid in ticket_uids(meeting_dir):
        stages = read_stages(meeting_dir, uid)
        last = stages[-1] if stages else None
        first = stages[0] if stages else None
        out.append({
            "ticket": uid,
            "tries": len(stages),
            "first_time": first.time if first else None,
            "answered": answered(meeting_dir, uid),
            "who": [one.who for one in stages],
            "last_who": last.who if last else None,
            "last_time": last.time if last else None,
            "last_answered": last.answered() if last else None,
            "last_stop": last.stop() if last else None,
        })
    return out


# ----------------------------------------------------------------------
# 它写在哪儿
# ----------------------------------------------------------------------


def _environ(meeting_dir, uid: str) -> Environment:
    """这份进度的存储：根是 `ticket_progress/`，路径是这张单的号。"""
    return Environment(
        EnvironmentStorage(uid, None, str(Path(meeting_dir) / PROGRESS_DIR)), None)


def _piece(value) -> viba_ast.AST:
    """一个值写成 viba 数据的那一段。"""
    if isinstance(value, viba_ast.AST):
        return value
    data = getattr(value, "data", value)
    if isinstance(data, viba_ast.AST):
        return data
    return viba_ast.Constant(value)


def _leaf(node):
    """一个成员上那个值。"""
    given = reflect_access.leaf(node)
    if isinstance(given, VibaProgramErr):
        raise RuntimeError(f"这个成员不是一个值：{given.msg}")
    return given.ok_value
