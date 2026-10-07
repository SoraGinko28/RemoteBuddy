"""actor 这一侧：写代码这件事交给一个会写代码的 agent。

能力表（`actor_caps`）就是 api 里那四件事：回一句"知道了"、写一版代码、回一句"做好了"、照判语改一版。
"写"落在谁身上由调用者给：`actor_caps(write)` 里是 `write(prompt) -> code`；真会议里用
`meeting_actor_caps(channel, target)`，`channel.AgentChannel` 把话发进真会议（会议服务的 HTTP 面）、
等那个 agent 干完、从它的工作区把文件读回来。

那两句回话（"知道了"/"做好了"）在真会议那个用法里是从**会议记录里读它自己说的那句**，不是这里编的：
话由说话的人自己说。

这一段是一个**处理者**（`handler.Handler`）：名字、角色 `actor`、和上面那张能力表，加上规范里那几张
单的程序（`handler.program_caps`）——谁接单谁就跑同一段程序，只是各自多带一步自己那半边的实现。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, Optional

from . import _viba  # noqa: F401

from .binding import read_stored, text_of
from .channel import AgentChannel
from .handler import Handler, program_caps


def actor_caps(write: Callable[[str], str],
               say: Optional[Callable[[str, Any], None]] = None) -> Dict[str, Callable]:
    """actor 的四个算子。`write(prompt) -> code` 就是那个会写代码的 agent。"""

    def acknowledge(env, about):
        return _said(say, "知道了，这就照题目写一版", _read_or(env, about))

    def write_code(env, task, after):
        return write(_first_prompt(text_of(env, task)))

    def report_done(env, code, after):
        return _said(say, "做好了", _read_or(env, code))

    def revise_code(env, code, objection):
        previous = text_of(env, code)
        what = text_of(env, objection)
        if not what:
            return previous                      # 判语没说不合格：照原样，不必再写一版
        return write(_revision_prompt(previous, what))

    return {"acknowledge": acknowledge, "write_code": write_code,
            "report_done": report_done, "revise_code": revise_code}


def meeting_actor_caps(channel: AgentChannel, target: str = "quicksort.py") -> Dict[str, Callable]:
    """真会议里 actor 的能力表：写交给那个 agent；它说的那两句从会议记录里读。

    `acknowledge` 是**叫醒 agent 的那一下**：题面由它说进会议（它拿得到 `$about` 指的那份题面），
    agent 因此开始干活——工单驱动的会议里没有别人负责叫醒它。`write_code` 接着等它把文件交出来。
    """

    def acknowledge(env, about):
        # 题面说进会议就是叫醒 agent 的那一下：说完就回，不等它回话（它接着就去写了，
        # 下一步 `write_code` 会等它把文件交出来）。
        channel.say(_writing_prompt(text_of(env, about), target))
        return "知道了，这就照题目写一版"

    def write_code(env, task, after):
        return channel.ensure_work(_writing_prompt(text_of(env, task), target), target)

    def report_done(env, code, after):
        # 它这一轮说过的那句收尾话；没说过就用这一句（别在这儿空等）。
        return channel.wait_for_chat("final", timeout=10) or "做好了"

    def revise_code(env, code, objection):
        previous = text_of(env, code)
        what = text_of(env, objection)
        if not what:
            return previous                      # 判语没说不合格：照原样，不必再叫一次 agent
        return channel.ensure_work(_revising_prompt(previous, what, target), target,
                                   force=True, changed=True)

    return {"acknowledge": acknowledge, "write_code": write_code,
            "report_done": report_done, "revise_code": revise_code}


def actor_handler(meeting_dir, workflow, *, caps: Dict[str, Callable],
                  name: str = "actor") -> Handler:
    """一个 actor 处理者：它那四个算子，加上规范里那几张单的程序。"""
    return Handler(name, "actor", {**program_caps(workflow), **caps}, meeting_dir,
                   programs=workflow.tickets())


# ----------------------------------------------------------------------
# 那几句话，和几个小工具
# ----------------------------------------------------------------------


def _first_prompt(task: str) -> str:
    return f"照这份题面写一版代码：\n{task}"


def _revision_prompt(previous: str, objection: str) -> str:
    if objection:
        return f"判语说这一版不合格：{objection}\n照判语改一版：\n{previous}"
    return f"判语没说不合格（照原样再交一版）：\n{previous}"


def _writing_prompt(task: str, target: str) -> str:
    return f"{_first_prompt(task)}\n结果写进 {target}（就地写文件），写完回复 DONE，不要问我问题。"


def _revising_prompt(previous: str, objection: str, target: str) -> str:
    return f"{_revision_prompt(previous, objection)}\n（结果仍然写进 {target}，写完回复 DONE。）"


def _said(say, words: str, about) -> str:
    if say is not None:
        say(words, about)
    return words


def _read_or(env, address):
    try:
        return read_stored(env, address)
    except RuntimeError:
        return address


def writer_from(spec: str) -> Callable[[str], str]:
    """`模块:函数` 里的那个 `write(prompt) -> code`。"""
    import importlib

    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"--writer 要写成 模块:函数，收到的是 {spec!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def scripted_handler(meeting_dir, workflow, spec: str, *, name: str = "actor") -> Handler:
    """脚本化的那个 actor：写只是 `write(prompt) -> code`（演练和测试用它替掉真 agent）。"""
    return actor_handler(meeting_dir, workflow, caps=actor_caps(writer_from(spec)), name=name)


def live_handler(meeting_dir, workflow, channel: AgentChannel, *, target: str,
                 name: str = "actor") -> Handler:
    """真会议里那个 actor。"""
    return actor_handler(meeting_dir, workflow, caps=meeting_actor_caps(channel, target),
                         name=name)


def main(argv=None) -> int:
    """actor 的进程：接一轮单，把这一手印成一行 JSON。"""
    from .member import member_main

    def extra(parser):
        parser.add_argument("--writer", default=None, help="写这一步从哪来：`模块:函数`")
        parser.add_argument("--meeting-url", default=None, help="真会议的服务地址")
        parser.add_argument("--meeting-id", default=None, help="真会议的会议号")
        parser.add_argument("--workspace", default=None, help="agent 干活的目录")
        parser.add_argument("--target", default="quicksort.py", help="agent 要交的文件")

    def caps_for(args, workflow):
        if args.writer:
            return actor_caps(writer_from(args.writer))
        for needed in ("meeting_url", "meeting_id", "workspace"):
            if not getattr(args, needed):
                raise SystemExit(f"要么给 --writer，要么给真会议那几项（缺 --{needed.replace('_', '-')}）")
        return meeting_actor_caps(AgentChannel(args.meeting_url, args.meeting_id,
                                               args.workspace), args.target)

    return member_main("actor", caps_for, argv, extra=extra,
                       description="actor 的进程：接单、写代码")


if __name__ == "__main__":
    sys.exit(main())
