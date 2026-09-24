"""ticket_centered —— 一场会跑在一叠事先设计好的工单上。

一套只有三件事：一个 store（数据路径下面挂材料）、一个"该发哪一张"的判据、几张能力表。顺序、
回话、判什么、改什么，全在流程里（`workflow/` 那五类 viba 文件）；进程只做：自己会的算子算、
store 里已有答案的回放、其余递延——那一步交给别人。

三个角色各有自己的文件和自己的进程（`--bundle` 指到发给它的那一份）：

    python -m ticket_centered.actor     --meeting <会议目录> --case <案子> --bundle <包> \\
        --meeting-url http://… --meeting-id … --workspace …
    python -m ticket_centered.critic    --meeting <会议目录> --case <案子> --bundle <包>
    python -m ticket_centered.assistant --meeting <会议目录> --case <案子> --words "人类说的话"

发工单那一侧拿 `Meeting`：`due` / `next_ticket` 决定该发哪一张，`publish` 把一张发给每个成员。

名字是**延迟导入**的：包自己不预先拉起那几个模块，所以 `python -m ticket_centered.workflow` 这类入口
不会因为"包里已经导过了"被 runpy 记一句 RuntimeWarning。
"""

from typing import Any

_EXPORTS = {
    "Meeting": "meeting",
    "Member": "host",
    "Runner": "host",
    "read_material": "host",
    "capability_digest": "host",
    "Store": "store",
    "Workflow": "workflow",
    "Step": "workflow",
    "ROLES": "workflow",
    "ROLE_OF_API": "workflow",
    "SHIPPED_WORKFLOW": "workflow",
    "load_workflow": "workflow",
    "workflow_root": "workflow",
    "config_home": "workflow",
    "install": "workflow",
    "actor_caps": "actor",
    "meeting_actor_caps": "actor",
    "assistant_caps": "assistant",
    "critic_caps": "critic",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f".{module_name}", __name__), name)
