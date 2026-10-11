"""这场会有哪几个成员：一份表，一行一个。

一个成员是三样东西：**名字**、**它实现哪一份 api**、**怎么找到它**。表就在规范目录里
（`<规范>/members`），一行一个成员，`#` 开头的是说明：

```
# 名字       它实现哪份 api         怎么找到它
actor       actor_api             agent
critic      critic_api            critic
assistant   assistant_api         assistant
checker     static_code_api       process python -m static_check
```

**加一个成员不用改 Python**：写一份 `<角色>_api.viba` 放进规范目录（它声明这个角色会哪些算子），再往
这张表里加一行。基础那三个角色（actor、critic、assistant）的声明不在这儿，在 `RbApiDeclare` 那个仓库
里，程序和规范两边都读它（见 `declare.py`）；规范里另写一份同名的是不行的。`怎么找到它` 有五种写法：

* `agent` —— 会议里那个 agent（真会议是 `channel.AgentChannel`，演练和测试里塞一份脚本化的能力表）；
* `critic` —— 在本地跑验收条件的那张 critic 能力表；
* `assistant` —— 收消息、发单的那张 assistant 能力表；
* `scripted <模块>:<函数>` —— 本进程里一张能力表，做那一步是 `do(prompt) -> 成果`；
* `process <命令…>` —— 另起一个进程。宿主每一次把它跑一遍，接上 `--meeting / --case / --name`；那个
  进程自己照单算、自己写进度（`tickets.actor` 就是这个约定的一份实现）。`python` 这个词换成当前
  解释器。

表里写着的 api 必须真的有一份（规范自己带的，或者 `RbApiDeclare` 里那三个基础角色）；写错名字当场
说清楚，不要留到跑的时候才发现没人接单。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import _viba  # noqa: F401

from .handler import Handler, line_of, program_caps
from .progress import Stage, read_stages, unfinished
from .workflow import is_api_file, role_of_api

TABLE_NAME = "members"
COMMENT = "#"
KINDS = ("agent", "critic", "assistant", "scripted", "process")
# 表里没写、规范目录里也没有那份表时用的：这场会就是这三个成员。
FALLBACK_ROWS = ("actor actor_api agent",
                 "critic critic_api critic",
                 "assistant assistant_api assistant")


@dataclass
class Row:
    """表里的一行：谁、实现哪份 api、怎么找到它。"""

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
    """这份规范里那张表；规范不是从目录读出来的就没有表。"""
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
        raise ValueError(f"{name!r} 那一行写的不是一份 api：{api!r}（要写成 <角色>_api）")
    if kind in ("agent", "critic", "assistant") and len(words) > 3:
        raise ValueError(f"{name!r} 那一行是 {kind}，后面不该再有别的：{text!r}")
    if kind in ("scripted", "process") and len(words) < 4:
        raise ValueError(f"{name!r} 那一行是 {kind}，后面得写上怎么起：{text!r}")
    return Row(name=name, api=api, kind=kind, arguments=tuple(words[3:]))


def rows_of(source: Any = None) -> List[Row]:
    """表里那几个成员。`source` 可以是那份表、一个规范目录，也可以是没给（那就是没有表）。"""
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


def members_of(workflow) -> List[Row]:
    """这一场有哪几个成员：读规范里那张表；没有那份表就用默认那三个。"""
    return rows_of(table_path(workflow)) or [row_of(text) for text in FALLBACK_ROWS]


def write_table(path, rows: Iterable[Any]) -> Path:
    """把表写下来（一行一个成员）。给人改的那份表也照这个写法；`#` 开头的那几行原样留着。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{COMMENT} 名字 它实现哪份 api 怎么找到它"]
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
# 表项 → 成员
# ----------------------------------------------------------------------


class Process:
    """一个成员是另一个进程：每一次把它跑一遍，它自己接单、自己写进度。

    宿主的约定只有两条：命令后面接上 `--meeting / --case / --name / --rounds`；它把这一手写成进度。
    账不从它嘴里读，读的是它写下的那些进度——进度才是"谁做了什么"的凭据。
    """

    def __init__(self, name: str, role: str, command: Sequence[str], meeting_dir, case: str,
                 cwd=None) -> None:
        self.name = name
        self.role = role
        self.command = list(command)
        self.meeting_dir = meeting_dir
        self.case = case
        self.cwd = cwd
        # 它自己说它答了哪几步（就是它印出来的那行 JSON）：账记在这场会的记录里。
        self.did: List[Tuple[str, str]] = []

    def tick(self) -> List[Stage]:
        """把这一手交给它：跑一遍，然后读它写下的那些进度。"""
        before = {uid: len(read_stages(self.meeting_dir, uid))
                  for uid in unfinished(self.meeting_dir)}
        command = [sys.executable if word == "python" else word for word in self.command]
        command += ["--meeting", str(self.meeting_dir), "--case", str(self.case),
                    "--name", self.name, "--rounds", "1"]
        done = subprocess.run(command, cwd=str(self.cwd) if self.cwd else None,
                              env=_child_env(self.cwd), capture_output=True, text=True,
                              check=False)
        written: List[Stage] = []
        for uid, count in before.items():
            for stage in read_stages(self.meeting_dir, uid)[count:]:
                if stage.who == self.name:
                    written.append(stage)
        if done.returncode != 0 and not written:
            raise RuntimeError(f"{self.name} 没跑起来：{(done.stderr or '').strip()[-200:]}")
        self.did = _answered_of(done.stdout)
        return written

    def line(self, stage: Stage) -> str:
        """一篇进度写成一句话。"""
        return line_of(self.name, self.role, stage)

    def answered_line(self, module_path: str, func_name: str) -> str:
        """答下的一步写成一句话，给人看，也记进这场会的记录。"""
        where = module_path or "这个案子"
        return f"- {self.name}（{self.role}）答了 {where} + {func_name}"


def members_for(rows: Sequence[Row], *, workflow, meeting_dir, case: str,
                agent_caps: Optional[Dict[str, Callable]] = None,
                agent_name: Optional[str] = None,
                assistant_say: Optional[Callable[[str], None]] = None,
                cwd=None) -> List[Any]:
    """照这张表把这一趟接单的成员装配出来。

    `agent_caps` 是 `agent` 那一行用的那张能力表（真会议里是会议里那个 agent，演练和测试里是脚本化的
    写手）；`agent_name` 是它在会议里的名字——表里写的是角色位，真名字以会议为准。
    """
    from .actor import actor_caps, writer_from
    from .assistant import assistant_caps
    from .critic import critic_caps

    # （`assistant_caps` 在收尾那一处也用到：表里没写 assistant 时补一个，见函数末尾。）

    apis = set(workflow.apis())
    members: List[Any] = []
    for row in rows:
        if apis and row.api not in apis:
            raise ValueError(f"{row.name} 那一行实现 {row.api}，这份规范里没有这份 api")
        role = row.role
        if row.kind == "agent":
            if agent_caps is None:
                raise ValueError(f"{row.name} 那一行要会议里那个 agent，这一趟没有给它")
            caps = agent_caps
            name = agent_name or row.name
            one: Any = Handler(name, role, {**program_caps(workflow), **caps}, meeting_dir,
                               programs=workflow.tickets())
        elif row.kind == "critic":
            one = Handler(row.name, role,
                          {**program_caps(workflow), **critic_caps(workflow, workspace=cwd)},
                          meeting_dir, programs=workflow.tickets())
        elif row.kind == "assistant":
            one = Handler(row.name, role,
                          {**program_caps(workflow), **assistant_caps(assistant_say)},
                          meeting_dir, programs=workflow.tickets())
        elif row.kind == "scripted":
            one = Handler(row.name, role,
                          {**program_caps(workflow), **actor_caps(writer_from(row.arguments[0]))},
                          meeting_dir, programs=workflow.tickets())
        else:
            one = Process(row.name, role, command_for(row), meeting_dir, case, cwd=cwd)
        members.append(one)
    if not any(getattr(one, "role", "") == "assistant" for one in members):
        # 表里没写 assistant 也一定有它：人类那句话总得有人收、单总得有人发。
        # 它做哪两件事写在规范里那份 `assistant_api.viba` 上，不由这一处说。
        members.append(Handler("assistant", "assistant",
                               {**program_caps(workflow), **assistant_caps(assistant_say)},
                               meeting_dir, programs=workflow.tickets()))
    return members


def command_for(row: Row) -> List[str]:
    """一个进程成员那条命令（`--meeting` 那几项由 `Process.tick` 接上）。"""
    return list(row.arguments)


def _answered_of(output: str) -> List[Tuple[str, str]]:
    """那个进程印的最后那行 JSON 里"答了哪几步"。"""
    import json

    for line in reversed((output or "").splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            said = json.loads(text)
        except ValueError:
            continue
        return [(str(one[0]), str(one[1])) for one in said.get("answered") or ()]
    return []


def _child_env(cwd=None) -> Dict[str, str]:
    """那个进程的环境：照跑起来的这个解释器能 import 到的那几个目录，加上它自己的目录。

    这样它才 import 得到 `tickets` 这个包，也 import 得到规范里那几个 api 用的东西。
    """
    env = dict(os.environ)
    seen: List[str] = []
    for entry in [*sys.path, str(cwd) if cwd else ""]:
        if entry and Path(entry).is_dir() and entry not in seen:
            seen.append(entry)
    for one in env.get("PYTHONPATH", "").split(os.pathsep):
        if one and one not in seen:
            seen.append(one)
    env["PYTHONPATH"] = os.pathsep.join(seen)
    return env


def main(argv=None) -> int:
    """`python -m tickets.table`：看这场会有哪几个成员，或者加一个。"""
    parser = argparse.ArgumentParser(description="成员表：一个成员一行")
    parser.add_argument("--table", default=None, help="那张表（不给就是 <规范>/members）")
    parser.add_argument("--workflow", default=None, help="规范目录：表就在它旁边")
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
