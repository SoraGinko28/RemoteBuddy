"""这场会的那一份 store：会议目录下面放什么、案子叫什么、记录写在哪。

会议（`web.py` 那一侧）给的是**一个目录**：`<会议目录>/`，里面是这场会的 manifest、journal、工作区。
工单这一侧在它下面另起一份：`<会议目录>/ordered/`：

```
<会议目录>/ordered/store/                     共用的那一份 store：**只有数据**（值、工单）
<会议目录>/ordered/store/orders/<这一趟>/ticket   这一趟发的是哪张单（程序不发副本，在规范里）
<会议目录>/ordered/store/<案子>/<数据路径>/<算子>.viba       这一步答下的值
<会议目录>/ordered/store/<案子>/<数据路径>/tickets/<算子>/    这一步那张工单，和它被处理的每一次
<会议目录>/ordered/meeting_record.md          这场会的记录：谁说了什么、发了哪张单、谁答了哪一步
```

一条人类的消息进来就跑一趟（`run_for_meeting`）：assistant 收下、发单，actor / critic 接单，一直到没有
该发的单。案子名从人类那句话里点名的文件名来（`case_for`），agent 要交的那个文件也是（`target_for`）——
两样都只有那一个来源，页面上不另设一遍。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .session import Outcome, Session

ROOT_NAME = "ordered"
STORE_DIR = "store"
RECORD_NAME = "meeting_record.md"
CASE_PREFIX = "case"
FILE_IN_WORDS = re.compile(r"[\w./-]+\.[A-Za-z0-9]+")


def root_of(meeting_dir, root_name: str = ROOT_NAME) -> Path:
    """工单这一侧在会议目录下面的那一层。"""
    return Path(meeting_dir) / root_name


def store_root(meeting_dir, root_name: str = ROOT_NAME) -> Path:
    """共用的那份 store。"""
    return root_of(meeting_dir, root_name) / STORE_DIR


def record_path(meeting_dir, root_name: str = ROOT_NAME) -> Path:
    """这场会的记录。"""
    return root_of(meeting_dir, root_name) / RECORD_NAME


def target_for(words: str, fallback: str = "quicksort.py") -> str:
    """人类那句话里点名的那个文件名：agent 要交的就是它。"""
    for token in FILE_IN_WORDS.findall(words or ""):
        if not token.startswith("http"):
            return token
    return fallback


def case_for(words: str, fallback: str = "meeting", tag=None) -> str:
    """这张工单跑在哪个案子下：`case/<那个文件的名字>`，没有就用 fallback。

    同一场会里再说一句就再来一趟：`tag` 给这一趟一个序号（`case/<名字>-2`）。五张单的数据路径都
    相对案子，所以另一趟有自己的那几条路——不然第二句会被"这几步都答过了"挡回去，什么也不做。
    """
    stem = Path(target_for(words, "")).stem if words else ""
    name = stem or fallback
    return f"{CASE_PREFIX}/{name}" if tag is None else f"{CASE_PREFIX}/{name}-{tag}"


def run_for_meeting(meeting_dir, *, case: str, words: str, workflow,
                    services: Sequence[Tuple[str, str, Dict[str, Callable]]] = (),
                    root_name: str = ROOT_NAME, max_rounds: int = 1024,
                    out: Optional[Callable[[str], None]] = None,
                    session: Optional[Session] = None) -> Outcome:
    """一条人类的消息进来，在这个会议目录里跑一趟。"""
    session = session or Session(store_root(meeting_dir, root_name), case, "meeting",
                                 workflow=workflow, record_path=record_path(meeting_dir, root_name))
    session.state_the_task(words)
    return session.run(list(services), max_rounds=max_rounds, out=out)
