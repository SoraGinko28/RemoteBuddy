"""assistant 的进程：把人类说的话放进 store（落在案子自己的数据路径下）、收尾说一句。

    python -m ticket_centered.assistant --meeting <会议目录> --case case/quicksort \\
        --words "人类说的话"
    python -m ticket_centered.assistant --meeting <会议目录> --case case/quicksort \\
        --bundle <发给我的那一包>

能力表（`assistant_caps`）就是 api 里那两件事。**要它发言的工单，就在它的名下写一步**——这一趟的
五张工单里只有收尾那张有它的步骤；发工单是它在程序外头的事（唯一的派单者），不在这里。

`stated_task` 的实参是人类刚说的话：话**不是材料**，不随工单走，由听消息的那一侧交进来（这里就是
`--words`；接消息的服务也一样）。那一步不在任何工单里，所以 `--words` 给的时候不带 `--bundle`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, Optional

from . import _viba  # noqa: F401

from viba.compliance import record_prepare

from .host import read_material, text_of, value_of
from .member import bundle_main
from .store import Store

RECORD_NAME = "meeting_record.md"


def assistant_caps(say: Optional[Callable[[str], None]] = None) -> Dict[str, Callable]:
    """assistant 的两件事。`say(line)` 是它回话的地方（没说就只落进 store）。"""

    def stated_task(env, words: str = ""):
        """人类说了什么：材料落在案子自己的数据路径下。"""
        record_prepare(env, "stated_task", None, words)
        return words

    def closing_note(env, code, verdict):
        """这场会的结果：最后那版有多少行，判语是什么。"""
        written = text_of(env, code)
        judged = value_of(read_material(env, verdict))
        note = (f"这一版 {len(written.splitlines())} 行；"
                f"判语：{'合格' if judged else '不合格'}")
        if say is not None:
            say(note)
        return note

    return {"stated_task": stated_task, "closing_note": closing_note}


def state_the_task(args, _workflow, caps: Dict[str, Callable]) -> None:
    """人类刚说的那句话：先把这一步做掉——它不在工单里，工单只读它的结果。"""
    if not args.words:
        return
    store = Store(Path(args.meeting) / "store", Path(args.meeting) / RECORD_NAME)
    env = store.env(args.case, args.workflow, case=args.case)
    caps["stated_task"](env, args.words)
    store.record(f"- 人类说了一句：{args.words}")


def main(argv=None) -> int:
    def extra(parser):
        parser.add_argument("--words", default=None, help="人类刚说的话（不在任何工单里）")

    def caps_for(_args, _workflow):
        return assistant_caps()

    return bundle_main("assistant", caps_for, argv, extra=extra, setup=state_the_task,
                       description="assistant 的进程：把人类说的话放进 store、收尾说一句")


if __name__ == "__main__":
    sys.exit(main())
