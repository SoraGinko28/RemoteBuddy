"""人类说一句话，剩下的是工单：assistant 收下来、发单，服务接单，一直到头。

这是会议那一侧要用的那个入口——一条消息进来，跑一趟，报结局：

    python -m ordered_meeting.run --store <store> --case <案子> --order <工单> \\
        --workflow workflow --words "写一个快速排序，放到 quicksort.py" \\
        --writer 模块:函数
    python -m ordered_meeting.run --store <store> --case <案子> --order <工单> \\
        --workflow workflow --words "…" --meeting-url http://127.0.0.1:PORT \\
        --meeting-id <会议号> --workspace <agent 干活的目录> --target quicksort.py

消息**先到 assistant**：它把人类那句话放进 store，再读 store 决定发哪一张；接着一轮一步，它把下一步的
`$call` 固定成一张工单，会接那个算子的服务照单算掉。谁都不会的那一步没人接，当轮就走不动了，账上是
`stuck`——不假装跑完。

结局只有 `ok` 退出码是 0。
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ticket_centered import _viba  # noqa: F401

from .actor import caps_for
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
    parser.add_argument("--store", required=True, help="共用的 store")
    parser.add_argument("--case", required=True, help="这张工单跑在哪个案子下")
    parser.add_argument("--order", required=True, help="哪张工单（`orders/<工单>/`）")
    parser.add_argument("--workflow", required=True, help="这份流程在哪")
    parser.add_argument("--words", default=None, help="人类刚说的话")
    parser.add_argument("--name", default="actor", help="这个 actor 服务叫什么")
    parser.add_argument("--writer", default=None, help="写这一步从哪来：`模块:函数`")
    parser.add_argument("--meeting-url", default=None, help="真会议的服务地址")
    parser.add_argument("--meeting-id", default=None, help="真会议的会议号")
    parser.add_argument("--workspace", default=None, help="agent 干活的目录")
    parser.add_argument("--target", default="quicksort.py", help="agent 要交的文件")
    parser.add_argument("--rounds", type=int, default=1024, help="轮数上限")
    args = parser.parse_args(argv)

    if args.writer:
        caps = caps_for(writer=args.writer, name=args.name)
    else:
        for needed in ("meeting_url", "meeting_id", "workspace"):
            if not getattr(args, needed):
                parser.error(f"要么给 --writer，要么给真会议那几项（缺 --{needed.replace('_', '-')}）")
        caps = caps_for(meeting_url=args.meeting_url, meeting_id=args.meeting_id,
                        workspace=args.workspace, target=args.target, name=args.name)

    outcome = run_message(store_root=args.store, case=args.case, order=args.order,
                          workflow=args.workflow, words=args.words,
                          services=[(args.name, "actor", caps)],
                          max_rounds=args.rounds, out=print)
    return 0 if outcome.outcome == OK else 1


if __name__ == "__main__":
    sys.exit(main())
