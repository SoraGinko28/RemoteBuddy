"""mgmt_critic 这一侧：流程走完了没有。代码好不好不归它管（那是 tech_critic 的事）。

它答两样，和 tech_critic 一样：`judge` 是这个 bool，`objection` 是"哪里不完整"的那句话——
答 false 的时候一句一条，答 true 的时候是空串。判在哪条数据路径下由单给（那条 `$env`）。

它读的是这场会自己的账，不是别人转述：

* `tickets/` 里发过哪几张单、`ticket_progress/` 里每一张试过没有、有没有一个 `$ok`；
* 案子那份值里要交的那个名字（受理答出来的 `target`），和它在工作区里有没有落下来。

**它不看自己那一张单**：一张单的进度是它做完才写的，"试过但没做完"数的因此是别人——正在跑的
那一张（包括这一张）还没有进度，不会被算成缺口。别的流程缺口都是明确的：有人试过却没做成，
或者要交的那份东西根本不在。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba.interpret import put_snapshot

from .binding import NO_IMPLEMENTATION_TAG, stop_kind
from .handler import Handler, program_caps
from .layout import case_root, record_path
from .progress import answered as progress_answered, read_stages
from .ticket import ticket_uids
from .workflow import role_of_api
from .values import Store

FLOW_DIR = "flow"
# 这一步答下的那一份（bool）叫什么：和 tech_critic 那一步一样，名字就是这一步的名字
# ——`steps_of` 从单上读出来的"答了哪一步"是 (flow, check)，值也必须落在这一条上，
# 不然规划器会一直以为这一步没做过。
CHECKED = "check"
REASON = "objection"


def _tried_and_broke(meeting_dir, uid: str) -> bool:
    """这一张单有没有人真试过、却没做成。

    只数"没跑起来"那一种：轮换留下的那一篇（"这一步不是它的事"，别人接着做）不算——那是分工在走，
    不是缺口。一张单的进度是它做完才写的，所以正在跑的那一张（包括问流程这一张）不会被算进来。
    """
    for stage in read_stages(meeting_dir, uid):
        if stage.answered():
            return False
        if stop_kind(stage.result) not in ("", NO_IMPLEMENTATION_TAG):
            return True
    return False


def judgements_without_evidence(workflow, root: Path) -> List[str]:
    """判过、却没说判的是哪一份的那些判决，一句一条。

    判代码那一侧（tech_critic）判之前会把判的那一份存下来（`<那条数据路径>/judgment/under_judgment.viba`，
    见 `tickets/critic.py` 的 UNDER_JUDGMENT）。账面上一句"合格"背后什么都没有，就是流程上的缺口：
    谁也说不出它判的是哪一份。只会说"合格"的替身正是这样——判过的每一步都查得到，这一条就抓得住。
    """
    out: List[str] = []
    for api in workflow.apis():
        if role_of_api(api) != "tech_critic":
            continue
        for op in workflow.ops_of(api):
            for path in sorted(root.rglob(f"{op}.viba")):
                if "judgment" in path.parts:
                    continue
                if not (path.parent / "judgment" / "under_judgment.viba").is_file():
                    out.append(f"{path.parent.relative_to(root)} 判过了，"
                               f"却没留下判的是哪一份")
    return out


def the_flow_verdict(meeting_dir) -> Optional[Tuple[bool, str]]:
    """最新那个案子里，流程那一关判下来的是什么：`(过了没有, 哪里不完整)`；还没判过就是 None。

    它判完把两样都留在这个案子自己的数据路径下（`flow/check` 与 `flow/objection`），这一处照那两处读。
    谁要拿这个结论当自己的结论（`rb cli` 的退出码就是它），照这里取。
    """
    from .layout import cases

    for case in reversed(cases(meeting_dir)):
        values = Store(case_root(meeting_dir, case), record_path(meeting_dir))
        verdict, answered = values.read(FLOW_DIR, CHECKED)
        if not answered:
            continue
        said = values.value(FLOW_DIR, REASON)
        return (bool(verdict), "" if said is None else str(said))
    return None


def what_is_missing(workflow, meeting_dir, case: str, workspace=None) -> List[str]:
    """流程上还缺什么，一句一条；一条都不缺就是空的那一列。

    四样：有人试过却没做成的单、要交的那份东西没落下来、判过却没有凭据的判决、这个案子里一步都没答过。
    """
    problems: List[str] = []
    root = case_root(meeting_dir, case)
    values = Store(root, record_path(meeting_dir))
    for uid in ticket_uids(meeting_dir):
        if _tried_and_broke(meeting_dir, uid):
            problems.append(f"{uid} 试过但没做成")
    problems.extend(judgements_without_evidence(workflow, root))
    target = values.value("", "target")
    if target and workspace is not None and not (Path(workspace) / str(target)).exists():
        problems.append(f"要交的东西不在工作区里：{target}")
    if not values.stored():
        problems.append("这个案子里还没有人答过一步")
    return problems


def mgmt_critic_caps(workflow, meeting_dir, case: str,
                     workspace=None, judgment: str = FLOW_DIR) -> Dict[str, Callable]:
    """mgmt_critic 的能力表：就一件事 `check` —— 流程完整吗，actor 的活落了吗。

    `meeting_dir` 是这场会的目录（单和进度都在它底下），`case` 是这一趟的案子，
    `workspace` 是 actor 干活的那个目录（要交的东西照它找），`judgment` 是结论记在哪条数据路径下。
    """

    def check(env, judged=None):
        del judged                      # 单把 tech_critic 最后那次判决递进来：它只用来定"什么时候该问"
        problems = what_is_missing(workflow, meeting_dir, case, workspace)
        put_snapshot(env, "；".join(problems), REASON)
        put_snapshot(env, not problems, CHECKED)
        return not problems

    return {"check": check}


# ----------------------------------------------------------------------
# 它也可以是一个成员（表里 `mgmt_critic` 那一行）
# ----------------------------------------------------------------------


def mgmt_critic_handler(meeting_dir, workflow, *, case: str = "", name: str = "mgmt_critic",
                        workspace=None) -> Handler:
    """一个 mgmt_critic 处理者：`check`，加上规范里那几张单的程序。"""
    return Handler(name, "mgmt_critic",
                   {**program_caps(workflow),
                    **mgmt_critic_caps(workflow, meeting_dir, case, workspace=workspace)},
                   meeting_dir, programs=workflow.tickets())


__all__ = ["mgmt_critic_caps", "mgmt_critic_handler", "what_is_missing",
           "judgements_without_evidence", "the_flow_verdict", "FLOW_DIR",
           "CHECKED", "JUDGED", "REASON", "main"]


def main(argv=None) -> int:
    """mgmt_critic 的进程：接一轮单，把这一手印成一行 JSON（表里那行写成 `process` 时用）。"""
    from .member import member_main

    def caps_for(args, workflow):
        return mgmt_critic_caps(workflow, args.meeting, args.case, workspace=args.workdir)

    return member_main("mgmt_critic", caps_for, argv, extra=_more_arguments,
                       description="mgmt_critic 的进程：看这场会走完没有，缺什么点出来")


def _more_arguments(parser) -> None:
    """多一样：actor 干活的那个目录（要交的东西照它找）。"""
    parser.add_argument("--workdir", default=None, help="actor 干活的目录（要交的东西照它找）")


if __name__ == "__main__":
    import sys

    sys.exit(main())

