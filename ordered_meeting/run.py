"""人类说一句话，剩下的是工单：assistant 收下来、发单，服务接单，一直到头。

这是会议那一侧要用的那个入口——一条消息进来，跑一趟，报结局：

    python -m ordered_meeting.run --store <store> --order <工单> \\
        --workflow workflow --words "写一个快速排序，放到 quicksort.py" --writer 模块:函数
    python -m ordered_meeting.run --meeting <会议目录> --order <工单> --workflow workflow \\
        --words "…" --meeting-url http://127.0.0.1:PORT --meeting-id <会议号> \\
        --workspace <agent 干活的目录>

给了 `--meeting` 就用 `<会议目录>/ordered/` 那一份 store 和记录；案子名、agent 要交的文件名都从 `--words`
里那个文件名来，不用另外给。

消息**先到 assistant**：它把人类那句话放进 store，再读 store 决定发哪一张；接着一轮一步，它把下一步的
`$call` 固定成一张工单，会接那个算子的服务照单算掉。一张单跑完了再读一遍 store，看下一张该发哪张，一直
到没有该发的单为止。谁都不会的那一步没人接，当轮就走不动了，账上是 `stuck`——不假装跑完
（`--actor-only` 就是这样：写完之后判语那张没人接）。

结局只有 `ok` 退出码是 0。
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ticket_centered import _viba  # noqa: F401

from .actor import caps_for
from .critic import critic_service
from .meeting import case_for, record_path, store_root as meeting_store_root, target_for
from .plan import workflow_of
from .session import OK, Session


def run_message(*, store_root, case: str, order: str, workflow, words: Optional[str] = None,
                services: Sequence[Tuple[str, str, Dict[str, Callable]]] = (),
                record_path=None, max_rounds: int = 1024,
                out: Optional[Callable[[str], None]] = None):
    """一条人类的消息进来：assistant 收下、发单，服务接单，跑到头。"""
    session = Session(store_root, case, order, workflow=workflow, record_path=record_path)
    if words:
        session.state_the_task(words)
    return session.run(list(services), max_rounds=max_rounds, out=out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一句话进来：assistant 发单，actor 接单")
    parser.add_argument("--store", default=None,
                        help="共用的 store（给了 --meeting 就不用给这个）")
    parser.add_argument("--meeting", default=None,
                        help="会议目录：这一侧用 <会议目录>/ordered/ 那一份 store 和记录")
    parser.add_argument("--case", default=None,
                        help="这张工单跑在哪个案子下（不给就从 --words 里那个文件名来）")
    parser.add_argument("--order", required=True, help="哪张工单（`orders/<工单>/`）")
    parser.add_argument("--workflow", required=True, help="这份流程在哪")
    parser.add_argument("--words", default=None, help="人类刚说的话")
    parser.add_argument("--name", default="actor", help="这个 actor 服务叫什么")
    parser.add_argument("--critic-name", default="critic", help="这个 critic 服务叫什么")
    parser.add_argument("--actor-only", action="store_true",
                        help="只有 actor 接单（判语那一张没人接，账上是 stuck）")
    parser.add_argument("--writer", default=None, help="写这一步从哪来：`模块:函数`")
    parser.add_argument("--meeting-url", default=None, help="真会议的服务地址")
    parser.add_argument("--meeting-id", default=None, help="真会议的会议号")
    parser.add_argument("--workspace", default=None, help="agent 干活的目录")
    parser.add_argument("--target", default=None,
                        help="agent 要交的文件（不给就从 --words 里那个文件名来）")
    parser.add_argument("--rounds", type=int, default=1024, help="轮数上限")
    args = parser.parse_args(argv)

    if not args.store and not args.meeting:
        parser.error("要么给 --store，要么给 --meeting")
    store = args.store or meeting_store_root(args.meeting)
    record = record_path(args.meeting) if args.meeting else None
    case = args.case or case_for(args.words or "")
    target = args.target or target_for(args.words or "")

    if args.writer:
        caps = caps_for(writer=args.writer, name=args.name)
    else:
        for needed in ("meeting_url", "meeting_id", "workspace"):
            if not getattr(args, needed):
                parser.error(f"要么给 --writer，要么给真会议那几项（缺 --{needed.replace('_', '-')}）")
        caps = caps_for(meeting_url=args.meeting_url, meeting_id=args.meeting_id,
                        workspace=args.workspace, target=target, name=args.name)

    services = [(args.name, "actor", caps)]
    if not args.actor_only:
        services.append((args.critic_name, "critic", critic_service(workflow_of(args.workflow))))

    outcome = run_message(store_root=store, case=case, order=args.order,
                          workflow=args.workflow, words=args.words, services=services,
                          max_rounds=args.rounds, out=print, record_path=record)
    return 0 if outcome.outcome == OK else 1


if __name__ == "__main__":
    sys.exit(main())
