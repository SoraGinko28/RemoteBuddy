"""这场会那一侧：会议目录、案子叫什么、一条消息进来怎么跑一趟。

会议（`web.py` 那一侧）给的是**一个目录**：`<会议目录>/`，里面是这场会的 manifest、journal、工作区。
工单这一侧在它下面另起这几处（见 `layout.py`）：

```
<会议目录>/tickets/<工单号>/ticket.viba
<会议目录>/ticket_progress/<工单号>/<时刻>-<谁>.viba
<会议目录>/store/<案子>/<数据路径>/<算子>.viba
<会议目录>/meeting_record.md
```

一条人类的消息进来就跑一趟（`run_for_meeting`）：**先到 assistant**——它把人类说的话放进案子、接着读
值算出该发哪一张单；发出去之后每个处理者接单，一直到每一张单都做完、也没有该发的下一张。

案子名从人类那句话里点名的文件名来（`case_for`），agent 要交的那个文件也是（`target_for`）——两样都
只有那一个来源，页面上不另设一遍。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .handler import Handler
from .layout import CASE_PREFIX, case_root, record_path
from .session import Outcome, Session
from .values import Store

FILE_IN_WORDS = re.compile(r"[\w./-]+\.[A-Za-z0-9]+")


def target_for(words: str, fallback: str = "quicksort.py") -> str:
    """人类那句话里点名的那个文件名：agent 要交的就是它。"""
    for token in FILE_IN_WORDS.findall(words or ""):
        if not token.startswith("http"):
            return token
    return fallback


def case_for(words: str, fallback: str = "meeting", tag=None) -> str:
    """这张单跑在哪个案子下：`case/<那个文件的名字>`，没有就用 fallback。

    同一场会里再说一句就再来一趟：`tag` 给这一趟一个序号（`case/<名字>-2`）。每一张单的存储根都指向
    案子，所以另一趟有自己的那份值——不然第二句会被"这几步都答过了"挡回去，什么也不做。
    """
    stem = Path(target_for(words, "")).stem if words else ""
    name = stem or fallback
    return f"{CASE_PREFIX}/{name}" if tag is None else f"{CASE_PREFIX}/{name}-{tag}"


def the_assistant(handlers: Sequence[Handler], issuer: Optional[Handler] = None) -> Handler:
    """这一趟派单的那一个：表里那个 assistant（`issuer` 明说了就用它）。"""
    if issuer is not None:
        return issuer
    for one in handlers:
        if one.role == "assistant":
            return one
    raise ValueError("这一趟没有 assistant：谁收消息、谁发单，得有一个")


def run_for_meeting(meeting_dir, *, case: str, words: str, workflow,
                    handlers: Sequence[Handler], issuer: Optional[Handler] = None,
                    max_rounds: int = 64, record=None,
                    out: Optional[Callable[[str], None]] = None) -> Outcome:
    """一条人类的消息进来，在这个会议目录里跑一趟。"""
    handlers = list(handlers)
    session = Session(meeting_dir, case, workflow=workflow, record=record)
    session.stated_task(words, the_assistant(handlers, issuer))
    return session.run(handlers, max_rounds=max_rounds, out=out)


def values_of(meeting_dir, case: str, record=None) -> Store:
    """这个案子的值。"""
    return Store(case_root(meeting_dir, case), record or record_path(meeting_dir))


def cases_of(meeting_dir) -> List[str]:
    """这场会开过哪几个案子。"""
    from .layout import cases

    return cases(meeting_dir)


__all__ = ["run_for_meeting", "the_assistant", "target_for", "case_for", "values_of",
           "cases_of", "record_path", "case_root"]
