"""assistant 这一侧：消息先到它手上，它再发单。

两件事：

1. **把人类说的话放进案子**（`stated_task`，落在案子自己的数据路径下）——消息只经过它这一道；
2. **发一张单**：读这个案子的值，算出现在该发哪一张，把它写成
   `<会议目录>/tickets/<工单号>/ticket.viba`。它做不了的那些步骤由接单的人留成进度。

发单这件事因此有一个可查的凭据：那几个文件（`tickets/` 那一层）。assistant 说"我发了这张单"的时候，
指的就是它；它不必知道谁会接。

`closing_note` 是它在会议里说的最后一句（收尾那张单里有它的步骤）。**要它发言的单，就在它的名下写
一步**——发单本身是它在程序外头的事（唯一的派单者）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from . import _viba  # noqa: F401

from viba.interpret import write_snapshot

from .binding import read_stored, text_of, value_of
from .handler import Handler, program_caps


def assistant_caps(say: Optional[Callable[[str], None]] = None) -> Dict[str, Callable]:
    """assistant 的两件事。`say(line)` 是它回话的地方（没说就只落进案子）。"""

    def stated_task(env, words: str = ""):
        """人类说了什么：落在案子自己的数据路径下。"""
        write_snapshot(env, words, "stated_task")
        return words

    def closing_note(env, code, verdict):
        """这场会的结果：最后那版有多少行，判语是什么。"""
        written = text_of(env, code)
        judged = value_of(read_stored(env, verdict))
        note = (f"这一版 {len(written.splitlines())} 行；"
                f"判语：{'合格' if judged else '不合格'}")
        if say is not None:
            say(note)
        return note

    return {"stated_task": stated_task, "closing_note": closing_note}


def assistant_handler(meeting_dir, workflow, *, name: str = "assistant",
                      say: Optional[Callable[[str], None]] = None) -> Handler:
    """一个 assistant 处理者：它那两件事，加上规范里那几张单的程序。

    发单的人和接单的人是同一个角色，所以它自己也接单——收尾那张单里只有它的步骤。
    """
    return Handler(name, "assistant",
                   {**program_caps(workflow), **assistant_caps(say)}, meeting_dir,
                   programs=workflow.tickets())


def main(argv=None) -> int:
    """assistant 的进程：把人类说的话放进案子，再接一轮单。"""
    from .member import member_main, the_values

    def extra(parser):
        parser.add_argument("--words", default=None, help="人类刚说的话（不在任何单里）")

    def caps_for(_args, _workflow):
        return assistant_caps()

    def setup(args, workflow, caps):
        """人类那句话先落进案子：它不在任何单里，单只读它的结果。"""
        if not args.words:
            return
        store = the_values(args)
        caps["stated_task"](store.env("", workflow.search_path()), args.words)
        store.record(f"- 人类说了一句：{args.words}")

    return member_main("assistant", caps_for, argv, extra=extra, setup=setup,
                       description="assistant 的进程：把人类说的话放进案子、接单")


if __name__ == "__main__":
    sys.exit(main())
