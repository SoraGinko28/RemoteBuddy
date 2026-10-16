"""actor 这一侧：做活这件事交给一个会做活的 agent。

能力表（`actor_caps`）就是 api 里那四件事：回一句"知道了"、照题面做一版、回一句"做好了"、照它说过的
地方再做一版。做成什么由给它的那份题面定，这一处不假定是一段代码。
"做"落在谁身上由调用者给：`actor_caps(do)` 里是 `do(prompt) -> 成果`；真会议里用
`meeting_actor_caps(channel, target)`，`channel.AgentChannel` 把话发进真会议（会议服务的 HTTP 面）、
等那个 agent 干完、从它的工作区把交出来的东西读回来。

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


def actor_caps(do: Callable[[str], str],
               say: Optional[Callable[[str, Any], None]] = None) -> Dict[str, Callable]:
    """actor 的四个算子。`do(prompt) -> 成果` 就是那个会做活的 agent。"""

    def acknowledge(env, about):
        return _said(say, "知道了，这就照题目做一版", _read_or(env, about))

    def do_step(env, task, after):
        return do(_first_prompt(text_of(env, task)))

    def report_done(env, result, after):
        return _said(say, "做好了", _read_or(env, result))

    def redo_step(env, result, objection):
        previous = text_of(env, result)
        what = text_of(env, objection)
        if not what:
            return previous                      # 它没说不合格：照原样，不必再做一版
        return do(_revision_prompt(previous, what))

    return {"acknowledge": acknowledge, "do_step": do_step,
            "report_done": report_done, "redo_step": redo_step}


def meeting_actor_caps(channel: AgentChannel, target: str) -> Dict[str, Callable]:
    """真会议里 actor 的能力表：写交给那个 agent；`target` 是要交的那个文件（规范说的）。

    这一处一个文件名都不带：交什么由规范定，谁跑这一趟谁把它交进来。

    `acknowledge` 是**叫醒 agent 的那一下**：题面由它说进会议（它拿得到 `$about` 指的那份题面），
    agent 因此开始干活——工单驱动的会议里没有别人负责叫醒它。`do_step` 接着等它把东西交出来。
    """

    # 这一趟开工的时候那个东西是什么时候的：上一次留下的那一版不算这一趟的活。
    opened: Dict[str, object] = {}

    def acknowledge(env, about):
        # 题面说进会议就是叫醒 agent 的那一下：说完就回，不等它回话（它接着就去做，
        # 下一步 `do_step` 会等它把东西交出来）。
        opened["before"] = channel.stamp(target)
        channel.say(_writing_prompt(text_of(env, about), target))
        return "知道了，这就照题目做一版"

    def do_step(env, task, after):
        before = opened.pop("before", None)
        return channel.ensure_work(_writing_prompt(text_of(env, task), target), target,
                                   since=before, changed=_must_change(channel, target, before))

    def report_done(env, result, after):
        # 它这一轮说过的那句收尾话；没说过就用这一句（别在这儿空等）。
        return channel.wait_for_chat("final", timeout=10) or "做好了"

    def redo_step(env, result, objection):
        previous = text_of(env, result)
        what = text_of(env, objection)
        if not what:
            return previous                      # 它没说不合格：照原样，不必再叫一次 agent
        return channel.ensure_work(_revising_prompt(previous, what, target), target,
                                   force=True, changed=_must_change(channel, target, None))

    return {"acknowledge": acknowledge, "do_step": do_step,
            "report_done": report_done, "redo_step": redo_step}


def actor_handler(meeting_dir, workflow, *, caps: Dict[str, Callable],
                  name: str = "actor") -> Handler:
    """一个 actor 处理者：它那四个算子，加上规范里那几张单的程序。"""
    return Handler(name, "actor", {**program_caps(workflow), **caps}, meeting_dir,
                   programs=workflow.tickets())


# ----------------------------------------------------------------------
# 那几句话，和几个小工具
# ----------------------------------------------------------------------


def _first_prompt(task: str) -> str:
    return f"照这份题面做一版：\n{task}"


def _revision_prompt(previous: str, objection: str) -> str:
    if objection:
        return f"上一版被指出不合格：{objection}\n照它说的地方再做一版：\n{previous}"
    return f"没人说不合格（照原样再交一版）：\n{previous}"


def _must_change(channel, target: str, before) -> bool:
    """这一趟得真把它改了吗？

    一份文件：上一版已经在，就得看见它这一趟真改过。**一棵树**不这么看——它没有"哪一刻算改过"，
    只有"这一版比上一版好吗"，那是**下一次判决**说的话（一份法改没改好，由验收条件判，不由时刻判）。
    树这边不设这一条，免得改了的那一版因为在上一轮落的盘而被当成没改。
    """
    if channel.is_a_tree(target):
        return False
    return before is not None


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


def writer_from(spec: str, workspace=None) -> Callable[[str], str]:
    """`模块:函数` 里的那个 `do(prompt) -> 成果`。

    那个函数也可以写成 `do(prompt, workspace)`：给它工作区，它就能像真 actor 一样把东西落成文件
    （工单要交的往往是一个文件，不只是一段文字）。给了 `workspace`、而那个函数只收一个实参的，
    就照只收一个那么调。
    """
    import importlib
    import inspect

    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"--doer 要写成 模块:函数，收到的是 {spec!r}")
    module = importlib.import_module(module_name)
    writer = getattr(module, attribute)
    if workspace is None:
        return writer
    try:
        wanted = len([one for one in inspect.signature(writer).parameters.values()
                      if one.kind in (one.POSITIONAL_ONLY, one.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        return writer
    if wanted < 2:
        return writer
    return lambda prompt: writer(prompt, workspace)


def scripted_handler(meeting_dir, workflow, spec: str, *, name: str = "actor") -> Handler:
    """脚本化的那个 actor：做只是 `do(prompt) -> 成果`（演练和测试用它替掉真 agent）。"""
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
        parser.add_argument("--writer", default=None, help="做这一步从哪来：`模块:函数`")
        parser.add_argument("--meeting-url", default=None, help="真会议的服务地址")
        parser.add_argument("--meeting-id", default=None, help="真会议的会议号")
        parser.add_argument("--workspace", default=None, help="agent 干活的目录")
        parser.add_argument("--target", default="", help="agent 要交的那个文件（规范说的）")

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
