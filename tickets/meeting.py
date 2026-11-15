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

要交的那个文件由规范定（`intake/` 里那几份程序答出来的名字），这一处只把它接进来：案子按要交的那个
名字起（`case_for`），agent 要交的也是它——两样都只有那一个来源，页面上不另设一遍。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .handler import Handler
from .layout import CASE_PREFIX, STORE_DIR, case_root, record_path
from .session import Outcome, Session
from .values import Store

FILE_IN_WORDS = re.compile(r"[\w./-]+\.[A-Za-z0-9]+")


def a_file_in(words: str) -> str:
    """这句话里像文件名的那一个词；没点就是空。

    这是给规范用的一个**算子**（`intake/` 里那几份程序能问的其中一样）：哪一句算一份活由规范说了算，
    这一处只管照写法认出那个词。
    """
    for token in FILE_IN_WORDS.findall(words or ""):
        if not token.startswith("http"):
            return token
    return ""


# 一张路径：一段连着写的字，前面不是一个字的尾巴、也不是网址的一部分。
PATH_IN_WORDS = re.compile(r"(?<![A-Za-z0-9._~:/-])[A-Za-z0-9._~-]*(?:/[A-Za-z0-9._~-]+)+")


def a_path_in(words: str) -> str:
    """这句话里像路径的那一个词：带斜杠、不是网址；没有就是空。

    跟 `a_file_in` 一样是给规范用的一个算子。立法那几份受理程序问的是"要立的那份法在哪儿"，
    那是一个目录，不是文件名——所以认的是斜杠，不是点。
    一条路径是一段连着写的字，不是拿空格分开的一个词：中文那句话里路径前后常常没有空格，
    所以照写法认（`re`），不照分词认。
    """
    for token in PATH_IN_WORDS.findall(words or ""):
        if not token.startswith("http"):
            return token
    return ""


def case_for(target: str, fallback: str = "meeting", tag=None) -> str:
    """这一趟跑在哪个案子下：`case/<要交的那个名字>`，没名字就用 fallback。

    同一场会里再说一句就再来一趟：`tag` 给这一趟一个序号（`case/<名字>-2`）。每一张单的存储根都指向
    案子，所以另一趟有自己的那份值——不然第二句会被"这几步都答过了"挡回去，什么也不做。
    """
    stem = Path(target).stem if target else ""
    name = stem or fallback
    return f"{CASE_PREFIX}/{name}" if tag is None else f"{CASE_PREFIX}/{name}-{tag}"


def case_for_pass(meeting_dir, target: str, fallback: str = "meeting") -> str:
    """这一趟跑在哪个案子下：数着盘上已经有的那些，取头一个还没用过的号。

    头一趟是 `case/<名字>`，再用一次就是 `case/<名字>-2`，依此类推。数的是**盘上已经有的案子目录**，
    不是进程里记着的次数：服务重起过、或者别的服务起过这一趟，都不该让第二句话被"这几步都答过了"
    挡回去而什么也不做。
    """
    where = Path(meeting_dir) / STORE_DIR / CASE_PREFIX
    used = {one.name for one in where.iterdir()} if where.is_dir() else set()
    stem = Path(target).stem if target else ""
    name = stem or fallback
    if name not in used:
        return case_for(target, fallback)
    tag = 2
    while f"{name}-{tag}" in used:
        tag += 1
    return case_for(target, fallback, tag)


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
                    target: str = "", max_rounds: int = 64, record=None,
                    out: Optional[Callable[[str, str], None]] = None) -> Outcome:
    """一条人类的消息进来，在这个会议目录里跑一趟（`target` 是这一趟要交的那个名字）。

    `out(谁, 说了什么)` 给一行一行看的人（见 `session.Session.run`）。
    """
    handlers = list(handlers)
    session = Session(meeting_dir, case, workflow=workflow, record=record)
    session.stated_task(words, the_assistant(handlers, issuer), target)
    return session.run(handlers, max_rounds=max_rounds, out=out)


def values_of(meeting_dir, case: str, record=None) -> Store:
    """这个案子的值。"""
    return Store(case_root(meeting_dir, case), record or record_path(meeting_dir))


def cases_of(meeting_dir) -> List[str]:
    """这场会开过哪几个案子。"""
    from .layout import cases

    return cases(meeting_dir)


__all__ = ["run_for_meeting", "the_assistant", "a_file_in", "a_path_in", "case_for", "values_of",
           "cases_of", "record_path", "case_root"]
