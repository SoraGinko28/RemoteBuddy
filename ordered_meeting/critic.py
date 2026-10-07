"""critic 这一侧：也是一个服务，也在一份共用的 store 上接单。

    python -m ordered_meeting.critic --store <store> --case <案子> --order <这一趟> --workflow <规范目录>

能力表（`critic_caps`）就是 api 里那一件事 `judge`，加上跑规则要用的那几个算子；规则是规范里
`rules/*.viba` 那几份，用 `interpret` 跑——规则自己给门槛、自己答 bool，这里不替它判。

它和 actor 是同一套：工单里写着哪一步、要哪个算子、`$call` 是什么；它会的就照 `$call` 算，结论落在
这一步自己的数据路径下（`judge` 是那个 bool，`objection` 是说不合格的那句话）。谁都不会的那一步没人接，
当轮就走不动了。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable, Dict, Optional

from ticket_centered import _viba  # noqa: F401

from ticket_centered.critic import critic_caps

from .actor import report_line
from .plan import workflow_of
from .service import answer_service, run_service


def critic_service(workflow) -> Dict[str, Callable]:
    """critic 这张能力表：`judge` 加上规则要用的那几个算子。"""
    return critic_caps(workflow)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="critic 的服务进程：接单、判案")
    parser.add_argument("--store", required=True, help="共用的 store")
    parser.add_argument("--case", required=True, help="这张工单跑在哪个案子下")
    parser.add_argument("--order", required=True, help="哪张工单（`orders/<工单>/`）")
    parser.add_argument("--workflow", required=True, help="规范在哪（工单和规则在那里面）")
    parser.add_argument("--name", default="critic", help="这个服务叫什么（账里用的名字）")
    parser.add_argument("--phase", choices=("answer", "run"), default="answer",
                        help="接单（answer，一轮补上欠着的那一步）还是自己跑一遍整张单（run）")
    args = parser.parse_args(argv)

    workflow = workflow_of(args.workflow)
    caps = critic_service(workflow)
    if args.phase == "answer":
        report = answer_service(args.name, "critic", caps, store_root=args.store,
                                order=args.order, case=args.case, workflow=workflow)
    else:
        report = run_service(args.name, "critic", caps, store_root=args.store,
                             order=args.order, case=args.case, workflow=workflow)
    print(report_line(report))
    return 1 if report.failure else 0


if __name__ == "__main__":
    sys.exit(main())
