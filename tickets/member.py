"""一个成员进程的入口：接一轮单，把这一手印成一行 JSON。

    python -m tickets.actor --meeting <会议目录> --case <案子> [--name actor] [--rounds 1]
    python -m tickets.critic --meeting <会议目录> --case <案子>          # tech_critic
    python -m tickets.mgmt_critic --meeting <会议目录> --case <案子>     # mgmt_critic
    python -m tickets.assistant --meeting <会议目录> --case <案子> --words "人类说的话"

一个成员进程只做一件事：照这个案子发过的单，把**轮到它**的那些接掉（`handler.Handler.tick`），
一张单写一篇进度。它算完了就退出，账印在标准输出上（一行 JSON）——宿主按那些进度看它做了什么，
不信它在 stdout 上说了什么。

`--rounds` 是"多接几轮"（一张单要它做两步时用得上：接一手、下一手还轮得到它）；默认一轮。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Dict, Optional

from . import _viba  # noqa: F401

from .handler import Handler, program_caps
from .layout import case_root, record_path
from .plan import workflow_of
from .values import Store


def member_main(role: str, caps_for: Callable[[argparse.Namespace, object], Dict],
                argv=None, *, extra: Optional[Callable[[argparse.ArgumentParser], None]] = None,
                setup: Optional[Callable[[argparse.Namespace, object, Dict], None]] = None,
                description: str = "") -> int:
    """一个成员进程的全部：收参数、问角色要能力表、接单、把账印出来。"""
    parser = argparse.ArgumentParser(description=description or f"{role} 的成员进程")
    parser.add_argument("--meeting", required=True, help="这场会的目录")
    parser.add_argument("--case", required=True, help="跑在哪个案子下（`case/<名字>`）")
    parser.add_argument("--name", default=role, help="这个成员叫什么（进度里用的名字）")
    parser.add_argument("--workflow", default=None, help="规范在哪（不给就是规范目录）")
    parser.add_argument("--rounds", type=int, default=1, help="接几轮（默认一轮）")
    if extra is not None:
        extra(parser)
    args = parser.parse_args(argv)

    workflow = workflow_of(args.workflow)
    caps = caps_for(args, workflow)
    if setup is not None:
        setup(args, workflow, caps)
    handler = Handler(args.name, role, {**program_caps(workflow), **caps}, args.meeting,
                      programs=workflow.tickets())

    stages = []
    for _round in range(max(1, args.rounds)):
        written = handler.tick()
        stages.extend(written)
        if not written:
            break
    print(json.dumps({
        "handler": handler.name,
        "role": role,
        "case": args.case,
        "answered": [[where, name] for where, name in handler.did],
        "stages": [{"ticket": one.uid, "answered": one.answered(), "stop": one.stop()}
                   for one in stages],
    }, ensure_ascii=False))
    return 0


def the_values(args) -> Store:
    """这个成员看到的案子那份值。"""
    return Store(case_root(args.meeting, args.case), record_path(args.meeting))


if __name__ == "__main__":
    sys.exit(member_main("member", lambda _args, _workflow: {}))
