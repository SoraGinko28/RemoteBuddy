"""一场会的数据放在会议目录下面的哪几处。

```
<会议目录>/tickets/<工单号>/ticket.viba              一张工单：那次调用本身
<会议目录>/ticket_progress/<工单号>/<时刻>-<谁>.viba  处理它的每一次尝试，一次一个文件
<会议目录>/store/<案子>/<数据路径>/<算子>.viba        每一步答下的值（viba 自己的快照）
<会议目录>/meeting_record.md                          这场会的记录
```

工单、进度、值都在会议目录下面，各自一处，互不嵌套：看工单就看 `tickets/`，看谁试过哪一张就看
`ticket_progress/`，看答下了什么就看 `store/`。案子（`case/<名字>`）下面是这个案子的全部值——一张
工单的 `$storage_path` 指的就是它，所以那次调用写下的每一份快照都落在这个案子里。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

TICKETS_DIR = "tickets"
PROGRESS_DIR = "ticket_progress"
STORE_DIR = "store"
RECORD_NAME = "meeting_record.md"
TICKET_NAME = "ticket"
VIBA_SUFFIX = ".viba"
CASE_PREFIX = "case"


def parts_of(case: str) -> List[str]:
    """一个案子名写成路径上的那几段。"""
    return [part for part in str(case).split("/") if part]


def tickets_root(meeting_dir) -> Path:
    """这场会的工单放在哪儿。"""
    return Path(meeting_dir) / TICKETS_DIR


def progress_root(meeting_dir) -> Path:
    """这场会那些尝试放在哪儿。"""
    return Path(meeting_dir) / PROGRESS_DIR


def store_root(meeting_dir) -> Path:
    """这场会的值放在哪儿（案子一层在它下面）。"""
    return Path(meeting_dir) / STORE_DIR


def case_root(meeting_dir, case: str) -> Path:
    """这个案子的值放在哪儿：一张工单的 `$storage_path` 就是它。"""
    return store_root(meeting_dir).joinpath(*parts_of(case))


def record_path(meeting_dir) -> Path:
    """这场会的记录写在哪儿。"""
    return Path(meeting_dir) / RECORD_NAME


def cases(meeting_dir) -> List[str]:
    """这场会开过哪几个案子，按名字排（`store/case/<名字>` 那几层）。"""
    root = store_root(meeting_dir) / CASE_PREFIX
    if not root.is_dir():
        return []
    return [f"{CASE_PREFIX}/{one.name}" for one in sorted(root.iterdir()) if one.is_dir()]


def case_of(storage_path, meeting_dir) -> str:
    """一条 `$storage_path` 说的是哪个案子；不在 store 底下就是空串。"""
    try:
        inside = Path(storage_path).relative_to(store_root(meeting_dir))
    except ValueError:
        return ""
    return "/".join(inside.parts)
