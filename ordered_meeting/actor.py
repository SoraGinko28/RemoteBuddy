"""actor 这一侧：一个服务进程，一张能力表，在共用的 store 上接单。

    python -m ordered_meeting.actor --store <store> --case <案子> --order <工单> \\
        --meeting-url http://127.0.0.1:PORT --meeting-id <会议号> \\
        --workspace <agent 干活的目录> --target quicksort.py
    python -m ordered_meeting.actor --store <store> --case <案子> --order <工单> \\
        --writer <模块>:<函数>

第一种是真会议：写这一步交给一个会写代码的 agent（`AgentChannel` 说话进会议、等它干完、从它的工作区
读回文件）。第二种是脚本化的：`--writer` 指一个 `write(prompt) -> code`，测试用它替代那个 agent。
两种都只做一件事：跑一遍这张工单，把它会的算子答掉，报一行账。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Callable, Dict, Optional

from ticket_centered import _viba  # noqa: F401

from ticket_centered.actor import actor_caps, meeting_actor_caps

from .service import Report, run_service


def actor_service(write: Callable[[str], str], name: str = "actor",
                  say: Optional[Callable[[str, object], None]] = None) -> Dict[str, Callable]:
    """actor 这个服务的能力表：它会的那几个算子。"""
    return actor_caps(write, say)


def writer_from(spec: str) -> Callable[[str], str]:
    """`模块:函数` 里的那个 `write(prompt) -> code`。"""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"--writer 要写成 模块:函数，收到的是 {spec!r}")
    module = importlib.import_module(module_name)
    return getattr(module, attribute)


def meeting_writer(args: argparse.Namespace) -> Callable[[str], str]:
    """真会议那一侧：说话进会议、等 agent 干完、从工作区读回文件。"""
    from ticket_centered.actor import AgentChannel

    channel = AgentChannel(args.meeting_url, args.meeting_id, args.workspace)
    return meeting_actor_caps(channel, args.target)["write_code"]


def report_line(report: Report) -> str:
    """一行 JSON 的账：谁、哪张工单、答了哪几步、还欠着哪一步。"""
    return json.dumps({
        "service": report.service,
        "role": report.role,
        "order": report.order,
        "ticket": report.ticket,
        "answered": [list(pair) for pair in report.answered],
        "replayed": [list(pair) for pair in report.replayed],
        "deferred": [list(pair) for pair in report.deferred],
        "failure": report.failure,
    }, ensure_ascii=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="actor 的服务进程：接单、答它会的算子")
    parser.add_argument("--store", required=True, help="共用的 store")
    parser.add_argument("--case", required=True, help="这张工单跑在哪个案子下")
    parser.add_argument("--order", required=True, help="哪张工单（`orders/<工单>/`）")
    parser.add_argument("--name", default="actor", help="这个服务叫什么（账里用的名字）")
    parser.add_argument("--writer", default=None, help="写这一步从哪来：`模块:函数`")
    parser.add_argument("--meeting-url", default=None, help="真会议的服务地址")
    parser.add_argument("--meeting-id", default=None, help="真会议的会议号")
    parser.add_argument("--workspace", default=None, help="agent 干活的目录")
    parser.add_argument("--target", default="quicksort.py", help="agent 要交的文件")
    args = parser.parse_args(argv)

    if args.writer:
        write = writer_from(args.writer)
    else:
        for needed in ("meeting_url", "meeting_id", "workspace"):
            if not getattr(args, needed):
                parser.error(f"要么给 --writer，要么给真会议那几项（缺 --{needed.replace('_', '-')}）")
        write = meeting_writer(args)

    report = run_service(args.name, "actor", actor_service(write, args.name),
                         store_root=args.store, order=args.order, case=args.case)
    print(report_line(report))
    return 1 if report.failure else 0


if __name__ == "__main__":
    sys.exit(main())
