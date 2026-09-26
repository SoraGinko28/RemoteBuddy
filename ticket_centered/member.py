"""一个成员的进程：有人把一张工单的一份副本交到它手上，它在自己那份副本里跑一遍。

设计里是这么说的：**这一包发给会上的每个成员；各人拿一份自己的副本，在包里跑一遍**——自己会的算子
算、store 里有答案的回放、其余递延；**答下的值抄回会议**。

一个成员的进程拿到三样：

* `--meeting <会议目录>`：会议那一份共享的数据（`store/`、`meeting_record.md`）在这儿；
* `--bundle <包>`：**发给我的那一份**。包在哪就说明发给谁、是哪张工单：`tickets/<工单>/<成员>/`；
  `program/` 是程序（三个 api 的声明、几张工单、规则），`store/` 是这一步要读的数据；
* `--case <案子>`：这一包的数据路径跑在哪个案子下——**案子名由发出去的那一侧给**，工单里只有相对路径。

然后：跑一遍、答下的值抄回会议、退出。工单名和成员名都在包的路径里，不用另外带一封信。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from . import _viba  # noqa: F401

from viba.interpret import interpret
from viba.type import UnderlyingVibaOpFailed, VibaProgramErr

from .host import Member
from .store import Store
from .workflow import Workflow, load_workflow, workflow_root

PROGRAM_DIR = "program"
STORE_DIR = "store"
RECORD_NAME = "meeting_record.md"
VIBA_SUFFIX = ".viba"


def run_bundle(bundle, *, meeting, case: str, caps: Dict[str, Callable],
               out: Callable[[str], None] = print) -> List[Tuple[str, str]]:
    """在"发给我的"那份副本里跑一遍工单程序，答下的值抄回会议。

    返回这一次**真答了**哪几步：store 里已经有答案的是回放，不是这个成员干的活。
    """
    bundle = Path(bundle)
    ticket = bundle.parent.name
    name = bundle.name                                  # 发给谁：包的目录名
    store = Store(bundle / STORE_DIR, Path(meeting) / RECORD_NAME)
    runner = Member(name, name, caps).at(store, case, str(bundle / PROGRAM_DIR))
    answer = interpret(str(bundle / PROGRAM_DIR / f"{ticket}{VIBA_SUFFIX}"), runner.env(""))
    answered = [(path, op) for path, op, value in runner.did if value != "replayed"]
    role = runner.member.role
    _hand_back(bundle, Path(meeting) / STORE_DIR)
    # 自己的账写进会议记录：谁答了哪条数据路径下的哪个算子。写权是"每个角色只写自己名下那条
    # 数据路径"，这一行就是事后查它用的凭据（meeting.audit 读的就是这些行）。
    for path, op in answered:
        store.record(f"- {name}（{role}）答了 {path} + {op}")
    out(_line(name, ticket, answered, runner.deferred, _failure(answer)))
    return answered


def _hand_back(bundle: Path, store_root: Path) -> None:
    """答下的值抄回会议（每个角色只写自己名下那条数据路径）。"""
    source = bundle / STORE_DIR
    if source.is_dir():
        shutil.copytree(source, store_root, dirs_exist_ok=True)


def _failure(answer) -> str:
    if isinstance(answer, VibaProgramErr):
        return answer.err_msg
    if isinstance(answer, UnderlyingVibaOpFailed):
        return answer.msg
    return ""


def _line(name: str, ticket: str, answered, deferred, failure: str) -> str:
    said = [f"{name}：{ticket} 跑完了"]
    said.append("答了 " + "；".join(f"{path} + {op}" for path, op in answered)
                if answered else "没有它的事（整个过程都递延）")
    if deferred and not answered:
        said.append("递延在 " + "；".join(f"{path} + {op}" for path, op in deferred[:1]))
    if failure:
        said.append(f"没跑完：{failure}")
    return "，".join(said)


# ----------------------------------------------------------------------
# 每个角色进程共用的那点命令行
# ----------------------------------------------------------------------


def bundle_main(role: str,
                caps_for: Callable[[argparse.Namespace, Workflow], Dict[str, Callable]],
                argv=None, *, extra: Callable[[argparse.ArgumentParser], None] = None,
                setup: Callable[[argparse.Namespace, Workflow, Dict[str, Callable]], None] = None,
                description: str = "") -> int:
    """一个角色进程的命令行：会议在哪、发给我的那一包在哪、跑在哪个案子下。"""
    parser = argparse.ArgumentParser(description=description or f"{role} 的进程")
    parser.add_argument("--meeting", required=True, help="会议目录（store/ 和 tickets/ 在那儿）")
    parser.add_argument("--bundle", default=None, help="发给我的那一包（不给就是不走工单的那一步）")
    parser.add_argument("--case", required=True, help="这一包跑在哪个案子下")
    parser.add_argument("--workflow", default=None,
                        help="这份流程在哪（不给就按 项目 → $REMOTEBUDDY_HOME/workflow → 程序默认 找）")
    if extra is not None:
        extra(parser)
    args = parser.parse_args(argv)

    workflow = load_workflow(args.workflow)
    caps = caps_for(args, workflow)
    if setup is not None:
        setup(args, workflow, caps)
    if args.bundle:
        run_bundle(args.bundle, meeting=args.meeting, case=args.case, caps=caps)
    return 0
