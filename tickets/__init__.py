"""tickets —— 一场会跑在一张一张发出去的工单上。

一件只有三样：**工单**（一次要跑的调用，写在 `<会议目录>/tickets/<工单号>/ticket.viba`）、**进度**
（处理它的每一次，一次一个文件，在 `<会议目录>/ticket_progress/<工单号>/` 下）、**能力表**（谁答哪些
算子）。顺序、回话、判什么、改什么，全在规范里（`RbNorm` 那几类 viba 文件，程序一行都不带）。

消息先到 assistant 手上：它把人类说的话放进案子，再读值算出现在该发哪一张单，把它写下来。然后每个
成员接单——自己会的算子算、案子里已有值的回放、其余答 `$not_implemented`（"这一步不是我的事"），
于是同一张单在几个成员之间轮着往前走，直到它的进度里有一条 `$ok`。

成员进程（也可以由宿主在进程内直接接单）：

    python -m tickets.actor     --meeting <会议目录> --case <案子> --writer 模块:函数
    python -m tickets.critic    --meeting <会议目录> --case <案子>
    python -m tickets.assistant --meeting <会议目录> --case <案子> --words "人类说的话"

名字是**延迟导入**的：包自己不预先拉起那几个模块，所以 `python -m tickets.workflow` 这类入口不会
因为"包里已经导过了"被 runpy 记一句 RuntimeWarning。
"""

from typing import Any

_EXPORTS = {
    "Handler": "handler",
    "program_caps": "handler",
    "line_of": "handler",
    "Ticket": "ticket",
    "read_ticket": "ticket",
    "write_ticket": "ticket",
    "ticket_uids": "ticket",
    "tickets": "ticket",
    "Stage": "progress",
    "read_stages": "progress",
    "write_stage": "progress",
    "answered": "progress",
    "finished": "progress",
    "latest": "progress",
    "the_state": "progress",
    "Store": "values",
    "Binding": "binding",
    "step_address": "binding",
    "read_stored": "binding",
    "text_of": "binding",
    "issue": "issue",
    "issue_for": "issue",
    "Session": "session",
    "Outcome": "session",
    "open_work": "session",
    "run_for_meeting": "meeting",
    "case_for": "meeting",
    "target_for": "meeting",
    "due": "plan",
    "next_ticket": "plan",
    "Workflow": "workflow",
    "load_workflow": "workflow",
    "workflow_root": "workflow",
    "norm_home": "workflow",
    "members_for": "table",
    "members_of": "table",
    "actor_caps": "actor",
    "meeting_actor_caps": "actor",
    "actor_handler": "actor",
    "scripted_handler": "actor",
    "live_handler": "actor",
    "assistant_caps": "assistant",
    "assistant_handler": "assistant",
    "critic_caps": "critic",
    "critic_handler": "critic",
    "AgentChannel": "channel",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f".{module_name}", __name__), name)
