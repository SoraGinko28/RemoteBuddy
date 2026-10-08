"""assistant 发一张单：把这个案子和一段程序写成一张单，落在会议目录下面。

发单就是写文件：`<会议目录>/tickets/<工单号>/ticket.viba`。单上那几样（`ticket.Ticket`）都由这一处
定：**号**从案子的名字来（`<程序的名字>-<案子>`，一眼看出是哪张单、哪个案子），**名字**是规范里要跑的
那一段程序，**调用**是 `__dyn_call__ << "<名字>"`，**存储根**是这个案子的值那一层，**找模块的路**是
这一份规范给的（接口那个仓库的目录也在里面），**接口按哪一版**是这一份规范说的（
`$handler_name2api_declare_repo_commit_id`），**时刻**是写下它的那一刻。

发出去的单谁接、谁答，这一处一句话都不说——那是读进度的人的事。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .layout import CASE_PREFIX, case_of
from .ticket import Ticket, a_time, the_call, write_ticket


def ticket_uid(name: str, values_root) -> str:
    """这张单的号：`<程序的名字>-<案子>`。

    号就是它在 `tickets/` 和 `ticket_progress/` 两个目录下的目录名，所以要一眼看得出是哪张单、哪个
    案子。案子名取最里面那一段（`case/<名字>-2` → `<名字>-2`）。
    """
    parts = [part for part in str(values_root).replace("\\", "/").rstrip("/").split("/") if part]
    case = ""
    if CASE_PREFIX in parts:
        case = parts[parts.index(CASE_PREFIX) + 1] if len(parts) > parts.index(CASE_PREFIX) + 1 else ""
    return f"{name}-{case}" if case else name


def issue(meeting_dir, name: str, *, values_root, workflow,
          when: Optional[datetime] = None) -> Ticket:
    """发出这一张单，答它落在哪儿。

    单上那次调用不带环境：给它一个环境就是跑它。跑它的是规范里 `tickets/<名字>.viba` 那一段程序。
    找模块的路是这一份规范给的（接口那个仓库的目录也在里面），接口按的是哪一版记在单上
    （`$handler_name2api_declare_repo_commit_id`）——接口不在规范里，事后要说得清读的是哪一版。
    """
    ticket = Ticket(uid=ticket_uid(name, values_root), name=name, call=the_call(name),
                    storage_path=str(values_root), viba_path=workflow.search_dirs(),
                    handler_name2api_declare_repo_commit_id=workflow.declares(),
                    time=a_time(when))
    write_ticket(meeting_dir, ticket)
    return ticket


def issue_for(meeting_dir, name: str, *, meeting, case: str, workflow,
              when: Optional[datetime] = None) -> Ticket:
    """照这场会、这个案子发一张单（`meeting` 给的是会议目录）。"""
    from .layout import case_root

    return issue(meeting_dir, name, values_root=case_root(meeting_dir, case),
                 workflow=workflow, when=when)


def case_of_ticket(meeting_dir, ticket: Ticket) -> str:
    """这张单跑在哪个案子下：从它的 `$storage_path` 读回来。"""
    return case_of(ticket.storage_path, meeting_dir)


__all__ = ["issue", "issue_for", "ticket_uid", "case_of_ticket", "Path"]
