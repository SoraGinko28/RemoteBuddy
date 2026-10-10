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

import tempfile
from typing import Any, Callable, Dict, Optional

from . import _viba  # noqa: F401

from viba.interpret import (Environment, EnvironmentCompute, EnvironmentStorage,
                            interpret, put_snapshot)

from .binding import answer_of, failure_in, finished, read_stored, value_of
from .builtins import the_table as the_builtin_table
from .handler import Handler, program_caps
from .meeting import a_file_in


def assistant_caps(say: Optional[Callable[[str], None]] = None) -> Dict[str, Callable]:
    """assistant 的两件事。`say(line)` 是它回话的地方（没说就只落进案子）。"""

    def stated_task(env, words: str = ""):
        """人类说了什么：落在案子自己的数据路径下。"""
        put_snapshot(env, words, "stated_task")
        return words

    def closing_note(env, result, verdict):
        """这场会的结果：它判下来合格没有。成果是什么由规范给的那一步定，这一处不假定。"""
        del result                                # 成果长什么样是规范的事，这一处不读它
        judged = value_of(read_stored(env, verdict))
        note = f"这场会做完了；判下来：{'合格' if judged else '不合格'}"
        if say is not None:
            say(note)
        return note

    return {"stated_task": stated_task, "closing_note": closing_note}


# ----------------------------------------------------------------------
# 人说的那句算不算一份活：规范里 `intake/` 那几份程序说了算
# ----------------------------------------------------------------------


def decision_caps(words: str) -> Dict[str, Callable]:
    """判断"这一句要不要它做活"时，规范那几份程序能问到的算子。

    只有两样：人刚说的那句（`the_words`），和这句话里像文件名的那一个词（`a_file_in`）。哪一句算一份活
    不在这里定——那是规范里 intake/ 那几份程序的事，这一处只把这两样交给它们。
    """

    def the_words(_env):
        return words

    def a_file_in_op(_env, text):
        return a_file_in(str(value_of(text)))

    def says(_env, text, word):
        return str(value_of(word)) in str(value_of(text))

    return {"the_words": the_words, "a_file_in": a_file_in_op, "says": says}


def what_to_make(workflow, words: str) -> str:
    """规范说这一句要它做的是什么：答出来的是**要交的那个名字**，答不出来就是没有活可派。

    每一份都得答出一个名字来（有一份答不出来，这一句就还没说清要交什么）；用头一份答的那个名字。
    那几份程序跑在一处临时目录上，不留东西——它们是算出来的，不是这一场会的记录。
    """
    names = workflow.intake()
    if not names:
        raise RuntimeError("这份规范里没有 intake/：人说的哪句话算一份活，得由规范说")
    caps = decision_caps(words)
    chosen = ""
    with tempfile.TemporaryDirectory() as where:
        builtins = the_builtin_table()
        environ = Environment(
            EnvironmentStorage("", None, where),
            EnvironmentCompute(lambda module, name: caps.get(name) or builtins.get(name)
                               or builtins.get(f"{module}.{name}" if module else name)),
            workflow.search_path())
        for name in names:
            answer = interpret(str(workflow.intake_file(name)), environ)
            if not finished(answer):
                raise RuntimeError(f"intake/{name} 没跑完：{failure_in(answer)}")
            said = _said_text(answer_of(answer))
            if not said:
                return ""
            chosen = chosen or said
    return chosen


def _said_text(node) -> str:
    """那几份程序答下的那个名字；答下的不是一个字符串就说清楚。"""
    if node is None:
        return ""
    given = value_of(node)
    if not isinstance(given, str):
        raise RuntimeError(f"intake/ 那几份程序答的是一个要交的名字（一段字），收到的是 {given!r}")
    return given.strip()


def assistant_handler(meeting_dir, workflow, *, name: str = "assistant",
                      say: Optional[Callable[[str], None]] = None) -> Handler:
    """一个 assistant 处理者：它那两件事，加上规范里那几张单的程序。

    发单的人和接单的人是同一个角色，所以它自己也接单——收尾那张单里只有它的步骤。
    人说的那句话算不算一份活由 `what_to_make` 问规范，不在这一处。
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
