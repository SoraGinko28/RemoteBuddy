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
"""

from .actor import actor_caps, meeting_actor_caps
from .assistant import assistant_caps
from .critic import critic_caps
from .host import Member, Runner, read_material
from .meeting import Meeting
from .store import Store
from .workflow import ROLE_OF_API, ROLES, WORKFLOW_DIR, Step, Workflow, load_workflow

__all__ = ["Member", "Meeting", "ROLE_OF_API", "ROLES", "Runner",
           "Step", "Store", "WORKFLOW_DIR", "Workflow", "actor_caps", "assistant_caps",
           "critic_caps", "load_workflow", "meeting_actor_caps", "read_material"]
