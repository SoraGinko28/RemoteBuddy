"""这场会有哪几个服务：一份表，一行一个成员。

一个成员是三样东西：**名字**、**它实现哪个 api**、**怎么找到它**。表就在流程目录旁边
（`<流程>/services`），一行一个成员，`#` 开头的是说明：

```
# 名字      它实现哪个 api              怎么找到它
actor      actor_api                 agent
critic     critic_api                critic
checker    static_code_analysis_api  process python -m static_check
```

**加一个服务不用改 Python**：写一份 `<角色>_api.viba` 放进流程目录（它声明这个角色会哪些算子），
再往这张表里加一行。`怎么找到它` 有四种写法：

* `agent` —— 会议里那个 agent（真会议是 `AgentChannel`，自己那一侧也可以塞一份脚本化的能力表）；
* `critic` —— 在本地跑规则的那张 critic 能力表；
* `scripted <模块>:<函数>` —— 本进程里一张能力表，写这一步是 `write(prompt) -> code`；
* `process <命令…>` —— 另起一个进程。宿主把 `--store / --case / --order --phase answer` 接在命令后面，
  那个进程自己照 store 里的工单算，把自己答了哪几步印成一行 JSON 交回来（`ordered_meeting.actor`
  就是这个约定的一份实现）。`python` 这个词换成当前解释器。

表里写着的 api 必须在流程里真的有一份；写错名字当场说清楚，不要留到跑的时候才发现没人接单。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ticket_centered import _viba  # noqa: F401

from ticket_centered.workflow import is_api_file, role_of_api

from .service import Service

TABLE_NAME = "services"
COMMENT = "#"
KINDS = ("agent", "critic", "scripted", "process")
# 表里没写、流程目录旁边也没有那份表时用的：程序带的那份流程就是这两个成员
FALLBACK_ROWS = ("actor actor_api agent", "critic critic_api critic")


@dataclass
class Row:
    """表里的一行：谁、实现哪个 api、怎么找到它。"""

    name: str
    api: str
    kind: str
    arguments: Tuple[str, ...] = ()

    @property
    def role(self) -> str:
        return role_of_api(self.api)

    def line(self) -> str:
        return " ".join([self.name, self.api, self.kind, *self.arguments])

    def __str__(self) -> str:
        return self.line()


def table_path(workflow) -> Optional[Path]:
    """这份流程旁边那张表；流程不是从目录读出来的就没有表。"""
    root = getattr(workflow, "root", None)
    if root is None:
        if isinstance(workflow, (str, Path)) and Path(workflow).is_dir():
            return Path(workflow) / TABLE_NAME
        return None
    return Path(root) / TABLE_NAME


def row_of(text: str) -> Row:
    """一行表项。名字、api、怎么找到它三样都得有。"""
    words = text.split()
    if len(words) < 3:
        raise ValueError(f"表项要写成「名字 api 怎么找到它」：{text!r}")
    name, api, kind = words[0], words[1], words[2]
    if kind not in KINDS:
        raise ValueError(f"{name!r} 那一行说不知道怎么找到它：{kind!r}（只有 {'、'.join(KINDS)}）")
    if not is_api_file(api):
        raise ValueError(f"{name!r} 那一行写的不是一个 api：{api!r}（要写成 <角色>_api）")
    if kind in ("agent", "critic") and len(words) > 3:
        raise ValueError(f"{name!r} 那一行是 {kind}，后面不该再有别的：{text!r}")
    if kind in ("scripted", "process") and len(words) < 4:
        raise ValueError(f"{name!r} 那一行是 {kind}，后面得写上怎么起：{text!r}")
    return Row(name=name, api=api, kind=kind, arguments=tuple(words[3:]))


def rows_of(source: Any = None) -> List[Row]:
    """表里那几个成员。`source` 可以是那份表、一个流程，也可以是没给（那就是没有表）。

    没有表、或者表里只有说明——都返回空，交给调用的人决定用什么默认值。
    """
    if source is None:
        return []
    path = Path(source) if isinstance(source, (str, Path)) else table_path(source)
    return read_table(path) if path is not None else []


def read_table(path) -> List[Row]:
    """读那份表；文件不在就是没有表。`#` 开头的那几行是说明。"""
    path = Path(path)
    if not path.is_file():
        return []
    rows: List[Row] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith(COMMENT):
            continue
        try:
            rows.append(row_of(text))
        except ValueError as bad:
            raise ValueError(f"{path}:{number}: {bad}") from None
    return rows


def due_rows(workflow) -> List[Row]:
    """这一场有哪几个成员：读流程旁边那张表；没有那份表就用程序带的那两个。"""
    return rows_of(table_path(workflow)) or [row_of(text) for text in FALLBACK_ROWS]


def write_table(path, rows: Iterable[Any]) -> Path:
    """把表写下来（一行一个成员）。给人改的那份表也照这个写法；`#` 开头的那几行原样留着。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{COMMENT} 名字 它实现哪个 api 怎么找到它"]
    for one in rows:
        text = one.line() if isinstance(one, Row) else str(one).strip()
        lines.append(text if text.startswith(COMMENT) else row_of(text).line())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def add_row(path, text: Any) -> Row:
    """往表里加一行（已经有了同名的不重复加）。"""
    row = text if isinstance(text, Row) else row_of(str(text))
    rows = [one for one in read_table(path) if one.name != row.name]
    rows.append(row)
    write_table(path, rows)
    return row


# ----------------------------------------------------------------------
# 表项 → 服务
# ----------------------------------------------------------------------


def services_for(rows: Sequence[Row], *, workflow, store_root, case: str, order: str,
                 actor_caps: Optional[Dict[str, Callable]] = None,
                 actor_name: Optional[str] = None,
                 workspace=None) -> List[Service]:
    """照这张表把这一趟要跑的服务装配出来。

    `actor_caps` 是 `agent` 那一行用的那张能力表（真会议是会议里那个 agent，测试里是脚本化的写手）；
    `actor_name` 是它在会议里的名字——表里写的是角色位，真名字以会议为准。`workspace` 是进程成员
    干活的目录（`process` 那一行起的进程在它里面跑，它自己写的那份服务就在那儿找得到）。
    """
    if not hasattr(workflow, "apis"):
        from .plan import workflow_of

        workflow = workflow_of(workflow)
    apis = set(workflow.apis())
    services: List[Service] = []
    for row in rows:
        if apis and row.api not in apis:
            raise ValueError(f"{row.name} 那一行实现 {row.api}，这份流程里没有这个 api")
        services.append(_service_of(row, workflow=workflow, store_root=store_root, case=case,
                                    order=order, actor_caps=actor_caps, actor_name=actor_name,
                                    workspace=workspace))
    return services


def _service_of(row: Row, *, workflow, store_root, case: str, order: str,
                actor_caps: Optional[Dict[str, Callable]],
                actor_name: Optional[str], workspace=None) -> Service:
    if row.kind == "agent":
        if actor_caps is None:
            raise ValueError(f"{row.name} 那一行要会议里那个 agent，这一趟没有给它")
        return Service(actor_name or row.name, row.role, caps=actor_caps)
    if row.kind == "critic":
        from .critic import critic_service

        return Service(row.name, row.role, caps=critic_service(workflow))
    if row.kind == "scripted":
        from .actor import actor_service, writer_from

        return Service(row.name, row.role, caps=actor_service(writer_from(row.arguments[0])))
    return Service(row.name, row.role,
                   command=command_for(row, store_root=store_root, case=case, order=order),
                   cwd=str(workspace) if workspace else None)


def command_for(row: Row, *, store_root, case: str, order: str,
                phase: str = "answer") -> List[str]:
    """一个进程成员怎么起：表里那条命令，接上这个约定好的那几项。"""
    command = [sys.executable if word == "python" else word for word in row.arguments]
    return command + ["--store", str(store_root), "--case", str(case), "--order", str(order),
                      "--phase", phase]


def main(argv=None) -> int:
    """`python -m ordered_meeting.members`：看这场会有哪几个服务，或者加一个。"""
    parser = argparse.ArgumentParser(description="服务表：一个成员一行")
    parser.add_argument("--table", default=None, help="那份表（不给就是 <流程>/services）")
    parser.add_argument("--workflow", default=None, help="流程目录：表就在它旁边")
    parser.add_argument("--add", default=None, metavar="一行",
                        help="加一个成员：「名字 api 怎么找到它」")
    args = parser.parse_args(argv)

    if not args.table and not args.workflow:
        parser.error("要么给 --table，要么给 --workflow")
    path = Path(args.table) if args.table else Path(args.workflow) / TABLE_NAME
    if args.add:
        row = add_row(path, args.add)
        print(f"加上了：{row}")
    rows = read_table(path)
    print(f"这张表在：{path}")
    for row in rows:
        print(f"  {row}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
