"""接口那几个声明：actor、critic、assistant 会哪些算子，写在另一个仓库里。

那一份接口两边都要：规范里工单和规则 `import` 它，程序把它实现出来。所以它既不放在规范那个仓库里，
也不放在程序这个仓库里，单独一个仓库（默认 `RbApiDeclare/rb_api/`），两边各自说自己按的是哪一版：

* 规范那一侧：`<规范目录>/api_declare`，一个文件一行，写着那个仓库的 commit id；
* 程序这一侧：发一张单的时候把这一版记在单上（`$handler_name2api_declare_repo_commit_id`），并把
  那个目录放进单上的 `$VIBA_PATH`，跑的时候按它找得到 `actor_api` 这些名字。

那个仓库里还有一份 `ticket.viba`：**程序往会议目录里写的那几份数据写成什么**——一张单（`Ticket`）、
一次尝试（`TicketStage`）、一张单的全部进度（`TicketProgress`）。程序照它拼、照它读，自己那一侧不写死
成员；多一个少一个、类型不对，写的时候当场判一遍（`Definition`）。那几份数据里写 `InterpretResult`
的地方，判的时候读的是 viba 自己那份 `interpret_result.viba`（`_the_module`）。

读的时候核一遍：那个仓库当前的 HEAD 就是 `api_declare` 写着的那个 commit，那份接口下没有没提交的
改动。对不上就当场说清楚——不然读进去的声明和记在单上的那一版不是同一份。

规范里没有 `api_declare` 这份文件，就是这份规范自己带 api（或者一个角色都不带）：这一处一份都不读，
单上那一项也就是空的（`ticket.viba` 照旧从接口仓库读：那几份数据写成什么不属于某一份规范）。
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
DECLARE_FILE = "api_declare"
API_SUFFIX = "_api"
VIBA_SUFFIX = ".viba"
# 一张单和它的进度写成什么：接口仓库里那份文件，和里面那几个定义的名字
TICKET_FILE = f"ticket{VIBA_SUFFIX}"
TICKET_NAME = "Ticket"
STAGE_NAME = "TicketStage"
PROGRESS_NAME = "TicketProgress"
# 接口仓库里，声明放在这个包目录下；程序仓库旁边那一层
DECLARE_PACKAGE = Path("RbApiDeclare") / "rb_api"
DEFAULT_DECLARE = Path(__file__).resolve().parent.parent.parent / DECLARE_PACKAGE
# 写下来的 commit id 至少这么长：太短的没法说它是哪一版
SHORTEST_COMMIT = 7


def declare_home() -> Path:
    """接口那个仓库的声明目录在哪儿：`$REMOTEBUDDY_API_DECLARE` 指到哪儿就是哪儿。"""
    given = os.environ.get(DECLARE_ENV)
    return Path(given).expanduser() if given else DEFAULT_DECLARE


def declared_commit(root) -> str:
    """这份规范说它按哪一版接口写的：`<规范目录>/api_declare` 那一行。

    没有那个文件就是空串（这份规范自己带 api）。文件在、里面却没有一行说出版本，那是写错了。
    """
    path = Path(root) / DECLARE_FILE
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text.split()[0]
    raise ValueError(f"{path} 里没有一行说按哪一版接口写的")


def declared_apis(root, home=None) -> Tuple[Dict[str, str], str, Optional[Path]]:
    """`root` 这一份规范用的接口：`({名字: 原文}, commit, 目录)`。

    规范里没有 `api_declare` 就是 `({}, "", None)`。有它就把那个仓库的声明读一遍，读完核一下版本。
    """
    asked = declared_commit(root)
    if not asked:
        return {}, "", None
    where = Path(home) if home is not None else declare_home()
    files = {path.stem: path.read_text(encoding="utf-8")
             for path in sorted(where.glob(f"*{API_SUFFIX}{VIBA_SUFFIX}"))}
    if not files:
        raise FileNotFoundError(
            f"{Path(root) / DECLARE_FILE} 说了按 {asked[:SHORTEST_COMMIT]} 那一版写，"
            f"但 {where} 里一份 *{API_SUFFIX}{VIBA_SUFFIX} 都没有")
    head = head_of(where)
    if len(asked) < SHORTEST_COMMIT or not head.startswith(asked):
        raise ValueError(
            f"接口对不上：规范按 {asked} 写的，{where} 现在是 {head}。"
            f"把那个仓库切到写的那一版，或者改 {Path(root) / DECLARE_FILE} 这一行")
    uncommitted = uncommitted_in(where)
    if uncommitted:
        raise ValueError(
            f"{where} 里有没提交的改动（{'、'.join(uncommitted)}）：读进去的就不是 {head} 那一版了。"
            f"先提交，或者改 {Path(root) / DECLARE_FILE} 这一行")
    return files, head, where


def head_of(where) -> str:
    """这个仓库当前在哪一版：`git rev-parse HEAD`。"""
    done = _git(where, "rev-parse", "HEAD")
    if done.returncode != 0:
        raise ValueError(
            f"{where} 不是一个 git 仓库（读不出 HEAD）：{(done.stderr or '').strip()[-200:]}")
    return done.stdout.strip()


def uncommitted_in(where) -> list:
    """这份接口下有没有没提交的改动，一项一个文件名。"""
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
# 那张单和它的进度写成什么：接口仓库里那份 `ticket.viba`
# ----------------------------------------------------------------------


class Definition:
    """接口仓库里一个定义读出来的样子：成员的名字、次序、类型，和照它判一次的判法。

    成员的名字、次序、类型都是这份定义说的：程序那一侧只往里填值、照它一个一个读回来。填出来的
    东西合不合这份定义，写的时候判一遍（`check`）；这边多一个成员、少一个成员，`the_fields` 当场
    说清楚。所以"一张单上有什么"只有这一处——改它改的是 `.viba`，不是 Python。
    """

    def __init__(self, name: str, where=None) -> None:
        self.name = name
        self.home = Path(where) if where is not None else declare_home()
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
    """一张单写成什么：接口仓库里那个 `Ticket`。"""
    return _a_definition(TICKET_NAME, where)


def stage_definition(where=None) -> Definition:
    """一次尝试写成什么：接口仓库里那个 `TicketStage`。"""
    return _a_definition(STAGE_NAME, where)


def progress_definition(where=None) -> Definition:
    """一张单的全部进度写成什么：接口仓库里那个 `TicketProgress`。"""
    return _a_definition(PROGRESS_NAME, where)


def _a_definition(name: str, where=None) -> Definition:
    """一个定义，一个目录读一次，往后照旧的答。"""
    home = Path(where) if where is not None else declare_home()
    if (home, name) not in _DEFINITIONS:
        _DEFINITIONS[(home, name)] = Definition(name, home)
    return _DEFINITIONS[(home, name)]


_DEFINITIONS: Dict[Tuple[Path, str], Definition] = {}
_MODULES: Dict[Path, Tuple[Any, Path]] = {}


def _the_module(home: Path) -> Tuple[Any, Path]:
    """接口仓库那份 `ticket.viba` 读成一个模块，答 `(模块, 那份文件在哪儿)`。

    viba 自己那份 `interpret_result.viba` 拼在前面：声明里写得出 `InterpretResult`，就是靠它在同一个
    模块里（`viba/interpret.py` 判一次跑的结果时用的是同一份声明）。一个目录读一次。
    """
    if home not in _MODULES:
        path = home / TICKET_FILE
        if not path.is_file():
            raise FileNotFoundError(
                f"接口里没有 {path}：一张单和它的进度写成什么，是那个仓库说的")
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


__all__ = ["declare_home", "declared_commit", "declared_apis", "head_of", "uncommitted_in",
           "Definition", "ticket_definition", "stage_definition", "progress_definition",
           "DECLARE_ENV", "DECLARE_FILE", "DEFAULT_DECLARE", "TICKET_FILE",
           "TICKET_NAME", "STAGE_NAME", "PROGRESS_NAME"]
