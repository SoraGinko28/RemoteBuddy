"""一个另起进程的服务，给"服务表里加一行"那套测试用。

它和 `ordered_meeting.actor` 一样只认那个约定：命令后面接着 `--store / --case / --order
--phase answer`，自己照 store 里的工单算，算完了把「答了哪几步」印成一行 JSON。它会的算子只有
`check_static` 一个——不是规范里那份 actor_api 里的任何算子，所以它只能靠规范新写的那份 api 进来。
程序在规范目录里，所以它还得多知道一个 `--workflow`（表里那一行自己写）。

它替的是"RB 自己设计一个特殊服务"那一步：写一个静态检查的算子，包装成进程，接着往服务表里加一行。
"""

from __future__ import annotations

import argparse
import sys

from ordered_meeting.actor import report_line
from ordered_meeting.plan import workflow_of
from ordered_meeting.service import answer_service

from ticket_centered import _viba  # noqa: F401

from ticket_centered.critic import text_of


def check_static(env, code):
    """拿 `$code` 指的那份代码，数它有几个顶层 def：这一版的"静态检查"就这一条。"""
    written = text_of(env, code)
    return written.count("\ndef ") + (1 if written.startswith("def ") else 0)


CAPS = {"check_static": check_static}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="一个另起进程的服务：接单、答它会的算子")
    parser.add_argument("--store", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--order", required=True)
    parser.add_argument("--workflow", required=True, help="规范在哪（工单和 api 在那里面）")
    parser.add_argument("--phase", choices=("answer", "run"), default="answer")
    parser.add_argument("--name", default="checker")
    args = parser.parse_args(argv)

    report = answer_service(args.name, "checker", CAPS, store_root=args.store, order=args.order,
                            case=args.case, workflow=workflow_of(args.workflow))
    print(report_line(report))
    return 1 if report.failure else 0


if __name__ == "__main__":
    sys.exit(main())
