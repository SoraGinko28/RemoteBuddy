"""真会议那一侧：工单派活时，怎么把话说进会议、又不说成"人说的"。

actor 把题面发进会议走的是会议自己的消息接口——和人类说话是同一个接口。会议这一侧因此要认得出
哪一句是**工单在派活**：它只该把 agent 叫起来干活，不该再从头跑一遍工单（否则自己套自己）。

认出来只有一个凭据：这一句是**正在跑的那趟工单说出去的**。`MeetingChannel` 在把话发出去的那一小段
时间里先声明这件事（`speaking`），会议收到这句就照旧叫醒 agent、不再发新单。除此之外它和
`AgentChannel` 一模一样：等 agent 干完、从它的工作区把文件读回来。
"""

from __future__ import annotations

from typing import Callable, Optional

from ticket_centered import _viba  # noqa: F401

from ticket_centered.actor import AgentChannel


class MeetingChannel(AgentChannel):
    """真会议里 actor 那条线：`say` 之前先声明"这一句是工单派活"。"""

    def __init__(self, url: str, meeting_id: str, workspace, *,
                 speaking: Optional[Callable[[], object]] = None, agent_name: str = "actor",
                 **kwargs) -> None:
        super().__init__(url, meeting_id, workspace, agent_name=agent_name, **kwargs)
        self._speaking = speaking

    def say(self, message: str) -> dict:
        if self._speaking is None:
            return super().say(message)
        with self._speaking():
            return super().say(message)
