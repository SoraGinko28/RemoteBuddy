"""接口那几个声明：actor、tech_critic、mgmt_critic、assistant 会哪些算子，写在另一个仓库里。

那一份接口两边都要：规范里工单和规则 `import` 它，程序把它实现出来。所以它既不放在规范那个仓库里，
也不放在程序这个仓库里，单独一个仓库（`RbApiDeclare`），**两边都以 submodule 依赖它**：

```
RbNorm/RbApiDeclare/rb_api/             规范那一份
RemoteBuddy-core/RbApiDeclare/rb_api/   程序这一份
```

"按哪一版写"不再写在一个文件里：submodule 指着哪个 commit，就是哪一版（git 的 gitlink 就是那个记录）。
读的时候用程序自己那一份（`declare_home()`），核两件事：

* 规范自己那份 submodule 和它指着同一个 commit（规范边上没有那份 submodule 就没有可核的：临时目录、
  别人的目录）；
* 这一份底下没有没提交的改动——不然读进去的不是那个 commit 里的东西。

这一版记在每一张单上（`$handler_name2api_declare_repo_commit_id`），那个目录也在单上的 `$VIBA_PATH` 里，
跑的时候按它找得到 `actor_api` 这些名字。

**程序自己那些数据写成什么不在这里**：一张单、它的进度，都是程序自己的东西，写在程序仓库的
`tickets/ticket.viba` 里，由下面那个 `Definition` 读。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import _viba  # noqa: F401

from viba import viba_ast
from viba.type import AstNodeType, VibaProgramErr, custom_module

DECLARE_ENV = "REMOTEBUDDY_API_DECLARE"
API_SUFFIX = "_api"
VIBA_SUFFIX = ".viba"
# 这张单和它的进度写成什么：程序仓库里那份文件，和里面那几个定义的名字
TICKET_FILE = f"ticket{VIBA_SUFFIX}"
TICKET_NAME = "Ticket"
STAGE_NAME = "TicketStage"
PROGRESS_NAME = "TicketProgress"
# 程序仓库里那份定义就在这个包目录下（`tickets/ticket.viba`）
DEFINITIONS_HOME = Path(__file__).resolve().parent
# 接口那个 submodule 叫什么，它里面的声明放在哪个包目录下
DECLARE_REPO = "RbApiDeclare"
DECLARE_PACKAGE = Path(DECLARE_REPO) / "rb_api"
DEFAULT_DECLARE = Path(__file__).resolve().parent.parent / DECLARE_PACKAGE


def declare_home() -> Path:
    """接口那份声明在哪儿：程序仓库里那个 submodule；`$REMOTEBUDDY_API_DECLARE` 可以另指一个。"""
    given = os.environ.get(DECLARE_ENV)
    return Path(given).expanduser() if given else DEFAULT_DECLARE


def declared_apis(root, home=None) -> Tuple[Dict[str, str], str, Optional[Path]]:
    """这份规范跑起来用的接口：`({名字: 原文}, commit, 目录)`。

    `home` 不给就是程序自己那份 submodule。`root` 是规范目录：它自己那份 submodule 要是也在，就核一下
    两边指着同一个 commit——两边都依赖同一个仓库，指的不是同一个东西就不该跑。
    """
    where = Path(home) if home is not None else declare_home()
    files = {path.stem: path.read_text(encoding="utf-8")
             for path in sorted(where.glob(f"*{API_SUFFIX}{VIBA_SUFFIX}"))}
    if not files:
        raise FileNotFoundError(f"{where} 里一份 *{API_SUFFIX}{VIBA_SUFFIX} 都没有：接口仓库读不到了")
    ours = head_of(where)
    theirs = the_norms_own(root)
    if theirs is not None:
        other = head_of(theirs)
        if other != ours:
            raise ValueError(
                f"接口对不上：{theirs} 在 {other}，程序这边是 {ours}。"
                f"两个 submodule 要指着同一个 commit（`git submodule update`）")
    uncommitted = uncommitted_in(where)
    if uncommitted:
        raise ValueError(
            f"{where} 里有没提交的改动（{'、'.join(uncommitted)}）：读进去的就不是 {ours} 那一版了")
    return files, ours, where


def the_norms_own(root) -> Optional[Path]:
    """规范自己那份 submodule 里那几份声明在哪儿；边上没有（临时目录、别人的目录）就是 None。"""
    root = Path(root)
    for candidate in (root / DECLARE_REPO, root.parent / DECLARE_REPO):
        if (candidate / ".git").exists():
            return candidate / "rb_api"
    return None


def head_of(where) -> str:
    """这一份当前在哪一版：`git rev-parse HEAD`。"""
    done = _git(where, "rev-parse", "HEAD")
    if done.returncode != 0:
        raise ValueError(
            f"{where} 不是一个 git 仓库（读不出 HEAD）：{(done.stderr or '').strip()[-200:]}")
    return done.stdout.strip()


def uncommitted_in(where) -> list:
    """这一份底下有没有没提交的改动，一项一个文件名。"""
    done = _git(where, "status", "--porcelain", "--", ".")
    if done.returncode != 0:
        raise ValueError(f"读不出 {where} 的改动：{(done.stderr or '').strip()[-200:]}")
    out = []
    for line in done.stdout.splitlines():
        text = line.strip()
        if text:
            out.append(text.split(maxsplit=1)[-1])
    return out


def _git(where, *arguments):
    """在 `where` 里跑一条 git 命令。"""
    return subprocess.run(["git", "-C", str(where), *arguments],
                          capture_output=True, text=True, check=False)


# ----------------------------------------------------------------------
# 程序自己那份数据写成什么：`tickets/ticket.viba`
# ----------------------------------------------------------------------


class Definition:
    """程序仓库那份 `ticket.viba` 里一个定义读出来的样子：成员的名字、次序、类型，和判法。

    成员的名字、次序、类型都是这份定义说的：程序那一侧只往里填值、照它一个一个读回来。填出来的东西
    合不合这份定义，写的时候判一遍（`check`）；这边多一个成员、少一个成员，`the_fields` 当场说清楚。
    所以"一张单上有什么"只有这一处——改它改的是 `.viba`，不是 Python。
    """

    def __init__(self, name: str, where=None) -> None:
        self.name = name
        self.home = Path(where) if where is not None else DEFINITIONS_HOME
        self.module, self.path = _the_module(self.home)
        definition = next((one for one in self.module.module.body
                           if getattr(one, "name", "") == name), None)
        if definition is None:
            raise ValueError(f"{self.path} 里没有 {name} 这个定义")
        self.body = definition.body
        self.tags = tuple(_tags_of(self.body))

    def the_fields(self, values: Dict[str, Any]) -> Tuple[Tuple[str, Any], ...]:
        """这几个值按定义里的次序摆好；多一个少一个都当场说清楚。"""
        missing = [tag for tag in self.tags if tag not in values]
        unknown = [tag for tag in values if tag not in self.tags]
        if missing or unknown:
            raise ValueError(
                f"{self.path} 里 {self.name} 是 {'、'.join(self.tags)}；"
                f"这一边少 {'、'.join(missing) or '没有'}，多 {'、'.join(unknown) or '没有'}")
        return tuple((tag, values[tag]) for tag in self.tags)

    def check(self, node) -> None:
        """这一段数据是不是这个定义说的那种；不是就当场说清楚差在哪儿。

        `node` 可以是一段拼好的 viba 数据（AST），也可以是读回来的一份（`VObject`）。
        """
        from viba.is_sub_type import is_sub_type
        from viba.reflect import language_config

        piece = node if isinstance(node, viba_ast.AST) else getattr(node, "data", node)
        judged = is_sub_type(AstNodeType(piece, self.module),
                             AstNodeType(self.body, self.module), config=language_config)
        if isinstance(judged, VibaProgramErr):
            raise ValueError(f"{self.path} 那份 {self.name} 判不了这一段：{judged.msg}")
        if judged.ok_value is not True:
            raise ValueError(f"这一段不合 {self.path} 那份 {self.name}：成员或类型对不上")


def ticket_definition(where=None) -> Definition:
    """一张单写成什么：那份 `ticket.viba` 里的 `Ticket`。"""
    return _a_definition(TICKET_NAME, where)


def stage_definition(where=None) -> Definition:
    """一次尝试写成什么：那份 `ticket.viba` 里的 `TicketStage`。"""
    return _a_definition(STAGE_NAME, where)


def progress_definition(where=None) -> Definition:
    """一张单的全部进度写成什么：那份 `ticket.viba` 里的 `TicketProgress`。"""
    return _a_definition(PROGRESS_NAME, where)


def _a_definition(name: str, where=None) -> Definition:
    """一个定义，一个目录读一次，往后照旧的答。"""
    home = Path(where) if where is not None else DEFINITIONS_HOME
    if (home, name) not in _DEFINITIONS:
        _DEFINITIONS[(home, name)] = Definition(name, home)
    return _DEFINITIONS[(home, name)]


_DEFINITIONS: Dict[Tuple[Path, str], Definition] = {}
_MODULES: Dict[Path, Tuple[Any, Path]] = {}


def _the_module(home: Path) -> Tuple[Any, Path]:
    """那个目录里的 `ticket.viba` 读成一个模块，答 `(模块, 那份文件在哪儿)`。

    viba 自己那份 `interpret_result.viba` 拼在前面：声明里写得出 `InterpretResult`，就是靠它在同一个
    模块里（`viba/interpret.py` 判一次跑的结果时用的是同一份声明）。一个目录读一次。
    """
    if home not in _MODULES:
        path = home / TICKET_FILE
        if not path.is_file():
            raise FileNotFoundError(f"没有 {path}：一张单和它的进度写成什么，是程序仓库里那份文件说的")
        source = _result_source() + "\n" + path.read_text(encoding="utf-8")
        _MODULES[home] = (custom_module(source), path)
    return _MODULES[home]


def _result_source() -> str:
    """viba 自己那份结果声明（`viba/interpret_result.viba`）的原文。"""
    import viba.interpret as the_interpreter

    return Path(the_interpreter.__file__).with_name(
        f"{the_interpreter.RESULT_MODULE_NAME}{VIBA_SUFFIX}").read_text(encoding="utf-8")


def _tags_of(node) -> List[str]:
    """一段类型上那几个成员的名字，按写下来的次序（`A * $x t * $y t` 这种链）。"""
    out: List[str] = []
    _collect_tags(node, out)
    return out


def _collect_tags(node, out: List[str]) -> None:
    """走那条乘积链，把一个一个 tag 收下来；成员自己的类型不再往里走。"""
    if isinstance(node, viba_ast.Tagged):
        if node.tag not in out:
            out.append(node.tag)
        return
    for attribute in ("left", "right", "function", "argument"):
        inner = getattr(node, attribute, None)
        if inner is not None:
            _collect_tags(inner, out)


__all__ = ["declare_home", "declared_apis", "the_norms_own", "head_of", "uncommitted_in",
           "Definition", "ticket_definition", "stage_definition", "progress_definition",
           "DECLARE_ENV", "DEFAULT_DECLARE", "TICKET_FILE",
           "TICKET_NAME", "STAGE_NAME", "PROGRESS_NAME"]
