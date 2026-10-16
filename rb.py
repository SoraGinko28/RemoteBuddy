#!/usr/bin/env python3
"""rb：两个子命令，一个开一场会，一个开一个网页看它。

    rb cli "…"     给一句话就开一场会，跑到它自己停下；有人伸手就把它停掉
    rb web         开一个网页：看工作区里所有的会，包括已经跑完的那些

两个子命令都在 `RemoteBuddy-core` 里，用的是同一份会议数据（`<工作区>/.meeting/`）：`rb cli`
驱动一场会，`rb web` 看它 —— 一边跑一边看，跑完之后再看，看到的是同一场会。

`rb cli` 不是 TUI，也不等人输入：命令行上那句话说出去，它把这场会跑到停下。除那句话，它只认
命令行上那几样──规范仓库和它要的那一版、干涉规则和它要的那一版、（接着跑时）那一场会的数据
目录。它的终端输出只有一行：看这场会该跑的那条 `rb web` 命令。

**`rb cli` 跑起来之后，除驱动它的这一趟自己，谁碰这场会都算伸手，当场停会**：人往这场会里
说话（从哪个进程说进来的都算）；另一个进程往这场会的 journal 里写；另一个服务把驱动权拿走。
哪一下算伸手由一份 viba 程序判（`--no-interference-rule-path`）：每观测到一下就把这一下交给它，
它答一句话（或者答 `true`）就是有人伸手，答空串（或者答 `false`）就是这一下不算。不给规则就
用内置的那一条：不是驱动这一趟自己做的，就算有人伸手。

这一趟读的规范、用的规则、谁伸了手、为什么停的，都记在会议的数据目录里（`rb.json`）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from meeting import MeetingStore, utc_now
from web import (
    PROJECT_ROOT,
    MeetingWebService,
    build_server,
)

from tickets import _viba  # noqa: F401  (让 `viba` 这个包能被 import 到)

from viba import viba_ast
from viba.interpret import (
    Environment,
    EnvironmentCompute,
    EnvironmentStorage,
    get_snapshot,
    interpret,
    put_snapshot,
)
from tickets.binding import answer_of, failure_in, finished
from tickets.mgmt_critic import the_flow_verdict
from tickets.builtins import the_table as the_builtin_table
from tickets.workflow import workflow_root

VERSION = "0.2.0"
HUMAN = "human"
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_RUN_FAILED = 2
# 流程那一关没过：`mgmt_critic` 说这场会没走完（它说的那些问题印在标准错误上）。
# 这一条就是它的那个返回值——过了是 0，没过是这个数。
EXIT_FLOW_INCOMPLETE = 3
EXIT_INTERFERED = 4

# 看一眼这一场会隔多久：干涉要靠这个看出来，不能太慢（一场对话里的每一下都在这个快慢上）。
WATCH_INTERVAL = 0.2
# 建会之后等第一件活起来的宽限：超过这么久还没有任何活起来，就当这场会没开成。
START_GRACE = 60.0


# ----------------------------------------------------------------------
# 那一份"不是驱动自己做的就算伸手"的规则（不给规则时用它）
# ----------------------------------------------------------------------

DEFAULT_RULE = '''__decl__ =
    bool
  <- $env Env

args = __get_args__ << __decl__

from_the_drive =
    bool
  <- $env Env
  <- { 这一下是不是驱动这场会的那一趟自己做的 }

# 驱动这一趟自己做的不算伸手；别的都算。答 true 就是"有人伸手"。
__impl__ = builtin.not
  << $env args.env
  << $x (from_the_drive << $env args.env)
'''


def _git(where: Path, *args: str) -> str:
    """在那个仓库里跑一条 git；跑不起来就当场报出来（带了它自己说的话）。"""
    done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True)
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        raise RuntimeError(f"git -C {where} {' '.join(args)}：{said[-1] if said else '失败了'}")
    return done.stdout


def materialize_norm(repo_path: Optional[str], commit_id: Optional[str]) -> Tuple[Optional[Path], str]:
    """这一趟读哪份规范：`(那个目录, 读的是哪一版)`。

    给了 `commit_id` 就照那一版把仓库的树取出来（`git archive`），取到的是一个临时目录──
    读的是那一版的字节，不是工作树现在的样子，也不是那个仓库里的工作树。没给就是 `repo_path`
    这个目录现在的样子，这一版记空串。
    """
    if not repo_path:
        if commit_id:
            raise RuntimeError("给了 --norm-commit-id 就得给 --norm-repo-path")
        return None, ""
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"规范仓库不在：{root}")
    if not commit_id:
        return root, ""
    sha = _git(root, "rev-parse", "--verify", f"{commit_id}^{{commit}}").strip()
    # 取到哪儿是按这一版的号定的：同一个提交永远落在同一条路径上。这样一场会记下"我读的是哪儿"之后，
    # 接着跑（`--resume`）算出来的还是那条路径 —— 每一步都会照它再比一次（`the_norm_of`）。
    into = Path(tempfile.gettempdir()) / f"rb-norm-{sha[:12]}"
    if not (into.with_suffix(".ready")).is_file():
        into.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(["git", "-C", str(root), "archive", sha], capture_output=True)
        if done.returncode != 0:
            raise RuntimeError(
                f"取不出 {root} 的 {sha}：{(done.stderr or b'').decode('utf-8', 'replace').strip()}")
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
            try:
                archive.extractall(into, filter="data")
            except TypeError:                  # 3.12 以前没有 filter 这个参数
                archive.extractall(into)
        into.with_suffix(".ready").write_text(sha + "\n", encoding="utf-8")
    return into, sha


def read_rule(path: Optional[str], commit_id: Optional[str]) -> Tuple[Optional[str], str, str]:
    """这一趟用哪条规则：`(规则源码, 它是哪儿来的, 是哪一版)`；没给就是内置那一条。

    给了 `commit_id` 就从那个仓库的那一版里取这份文件（不是工作树里现在这一份）。
    """
    if not path:
        if commit_id:
            raise RuntimeError("给了 --no-interference-rule-commit-id 就得给 --no-interference-rule-path")
        return DEFAULT_RULE, "<内置>", ""
    where = Path(path).expanduser().resolve()
    if not where.is_file():
        raise RuntimeError(f"干涉规则不在：{where}")
    if not commit_id:
        return where.read_text(encoding="utf-8"), str(where), ""
    repo = _git(where.parent, "rev-parse", "--show-toplevel").strip()
    repo_path = Path(repo).resolve()
    try:
        relative = where.relative_to(repo_path)
    except ValueError as bad:
        raise RuntimeError(f"{where} 不在 {repo_path} 里") from bad
    sha = _git(repo_path, "rev-parse", "--verify", f"{commit_id}^{{commit}}").strip()
    return _git(repo_path, "show", f"{sha}:{relative.as_posix()}"), str(where), sha


# ----------------------------------------------------------------------
# 观测到的那一下：交给规则判
# ----------------------------------------------------------------------


@dataclass
class Observation:
    """有人可能伸了一下手：哪一路、谁、干了什么。"""

    what: str            # "event"（journal 里多了一条不是这一趟写的）/ "driver"（驱动权被拿走）
    actor: str = ""
    action: str = ""
    phase: str = ""
    content: str = ""
    seq: int = 0
    from_the_drive: bool = False

    def said(self) -> str:
        """一句话说清这一下是什么（印出来、记下来都用它）。"""
        if self.what == "event":
            return f"journal 里多了一条：{self.action}（{self.actor or '没写谁'}）第 {self.seq} 条"
        return f"驱动权被拿走了（{self.actor or '不知道是谁'}）"


class InterferenceRule:
    """那份 viba 程序：把一下观测交给它，它答"这一下算不算伸手"。

    它能调到的算子就是这几个（都是 `<- $env Env`）：`the_what` / `the_actor` / `the_action` /
    `the_phase` / `the_content` / `the_seq` / `from_the_drive` / `the_meeting_id`。答一句话就是
    那句理由，答空串就是没有伸手；答 bool 就是"伸手了没有"，答 `nil` 就是这一下不管。
    """

    def __init__(self, source: str, where: str, commit: str, meeting_id: str = "") -> None:
        self.source = source
        self.where = where
        self.commit = commit
        self.meeting_id = meeting_id
        self.workdir = Path(tempfile.mkdtemp(prefix="rb-rule-"))
        self.program = self.workdir / "no_interference_rule.viba"
        self.program.write_text(source, encoding="utf-8")
        self.case = self.workdir / "case"
        self.case.mkdir()
        self.table = self._the_table()

    def _the_table(self) -> Dict[str, Callable]:
        """规则能调到的算子：viba 自带那一套，加上这一下的那几个字段。"""
        table = dict(the_builtin_table())
        for name in ("the_what", "the_actor", "the_action", "the_phase",
                     "the_content", "the_seq", "from_the_drive", "the_meeting_id"):
            table[name] = (lambda field_name: lambda env: get_snapshot(env, field_name))(name)
        return table

    def check(self) -> None:
        """这条规则现在能不能读起来：读不起来就当场报出来，别等到有人伸了手才发现。"""
        try:
            viba_ast.parse(self.source)
        except SyntaxError as bad:
            raise RuntimeError(f"干涉规则读不起来（{self.where}）：{bad}") from bad

    def judge(self, one: Observation) -> Optional[str]:
        """这一下算不算伸手：算了就是那句理由，不算就是 None。"""
        case = Environment(
            EnvironmentStorage("", None, str(self.case)),
            EnvironmentCompute(lambda _module, name: self.table.get(name)),
            str(self.workdir),
        )
        put_snapshot(case, one.what, "the_what")
        put_snapshot(case, one.actor, "the_actor")
        put_snapshot(case, one.action, "the_action")
        put_snapshot(case, one.phase, "the_phase")
        put_snapshot(case, one.content, "the_content")
        put_snapshot(case, one.seq, "the_seq")
        put_snapshot(case, one.from_the_drive, "from_the_drive")
        put_snapshot(case, self.meeting_id, "the_meeting_id")
        answer = interpret(str(self.program), case)
        if not finished(answer):
            raise RuntimeError(f"干涉规则没跑完：{failure_in(answer) or '不知道停在哪儿'}")
        piece = getattr(answer_of(answer), "data", None)
        if piece is None or isinstance(piece, viba_ast.Nil):
            return None
        value = getattr(piece, "value", None)
        if isinstance(value, bool):
            return one.said() if value else None
        if isinstance(value, str):
            return value or None
        raise RuntimeError(f"干涉规则答的不是 bool、也不是一句话：{piece!r}")


# ----------------------------------------------------------------------
# 这一趟跑：驱动权、journal、页面上的那几下，都在这儿看着
# ----------------------------------------------------------------------


@dataclass
class Interference:
    """伸手的那一下，和规则给的那句话。"""

    at: str
    observation: Observation
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "what": self.observation.what,
            "actor": self.observation.actor,
            "action": self.observation.action,
            "content": self.observation.content,
            "seq": self.observation.seq,
            "reason": self.reason,
        }


class DriveWatch:
    """看着这一场会：journal、驱动权、页面上打过来的那几下。

    驱动这一趟自己写进 journal 的每一条，都在 `own_seqs` 里（`MeetingStore` 落每一条
    journal 的那一处被记了一下）；不在这本账上的，就是别人写的。
    """

    def __init__(self, service: MeetingWebService, store: MeetingStore, rule: InterferenceRule,
                 on_interference: Optional[Callable[[Interference], None]] = None) -> None:
        self.service = service
        self.store = store
        self.meeting_id = store.meeting.meeting_id
        self.rule = rule
        self.rule.meeting_id = self.meeting_id
        self.on_interference = on_interference
        self.interferences: List[Interference] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.own_seqs: set = set()
        self.thread: Optional[threading.Thread] = None
        self.offset = 0
        self.carry = b""
        self.start_seq = store.last_event_sequence()
        self._original_append = None
        self._own_dirs = {str(store.run_dir)}

    # -- 记着"这一条是驱动自己写的" --------------------------------

    def _remember_own_writes(self) -> None:
        """在 `MeetingStore` 落 journal 的那一处记一笔：这一条是这一趟自己写的。

        journal 只从那一处落（`append_event` / `append_chat` / `append_screen` 都走它），
        所以不在这一本账上的序号，就是别的进程写进来的。
        """
        watch = self
        original = MeetingStore._append_event_unlocked

        def patched(store, kind, actor, phase, **rest):
            if str(store.run_dir) not in watch._own_dirs:
                return original(store, kind, actor, phase, **rest)
            with store._exclusive_state():                 # 同一线程里再进一次，不复取
                expected = store._last_event_sequence_unlocked() + 1
                watch.own_seqs.add((str(store.run_dir), expected))
                return original(store, kind, actor, phase, **rest)

        self._original_append = original
        MeetingStore._append_event_unlocked = patched

    def _restore_own_writes(self) -> None:
        if self._original_append is not None:
            MeetingStore._append_event_unlocked = self._original_append
            self._original_append = None

    # -- 看着 ------------------------------------------------------

    def start(self) -> None:
        self._remember_own_writes()
        self.offset = 0
        self.carry = b""
        self.thread = threading.Thread(target=self._loop, name=f"rb-watch-{self.meeting_id}",
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self._restore_own_writes()

    def _loop(self) -> None:
        while not self.stop.wait(WATCH_INTERVAL):
            try:
                self._once()
            except Exception:                              # noqa: BLE001 - 看错了不能把这一趟带倒
                continue

    def _once(self) -> None:
        for one in self._new_events():
            self._judge(one)
        stolen = self._stolen_driver()
        if stolen is not None:
            self._judge(stolen)

    def _new_events(self) -> List[Observation]:
        """journal 里这一趟还没看过的那几条，哪些不是驱动自己写的。"""
        path = self.store.events_path
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return []
        self.offset += len(chunk)
        block = self.carry + chunk
        lines = block.split(b"\n")
        self.carry = lines.pop()
        out: List[Observation] = []
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line.decode("utf-8"))
            seq = int(event.get("seq") or 0)
            actor = str(event.get("actor") or "")
            kind = str(event.get("kind") or "")
            own = (str(self.store.run_dir), seq) in self.own_seqs
            if kind == "chat" and actor == HUMAN:
                # 人说的话：不管它从哪个进程写进来，都算人伸了手。
                own = False
            if own or seq <= self.start_seq:
                continue
            out.append(Observation(what="event", actor=actor, action=kind,
                                   phase=str(event.get("phase") or ""),
                                   content=str(event.get("content") or ""),
                                   seq=seq, from_the_drive=False))
        return out

    def _stolen_driver(self) -> Optional[Observation]:
        """驱动权还是不是这一趟的：写着别人的、而且那个人还活着并且心跳还新，就是被拿走了。"""
        state = self.service._driver_state(self.store)
        if not state:
            return None
        pid = state.get("pid")
        if pid == os.getpid():
            return None
        if not self.service._driver_alive(state):
            return None
        return Observation(what="driver", actor=str(state.get("url") or f"pid {pid}"),
                           action="driver", phase=self.store.meeting.phase, from_the_drive=False)

    def _judge(self, one: Observation) -> None:
        try:
            reason = self.rule.judge(one)
        except Exception as bad:                            # noqa: BLE001 - 规则跑不起来就当有人伸手
            reason = f"干涉规则跑不起来：{type(bad).__name__}: {bad}"
        if not reason:
            return
        found = Interference(at=utc_now(), observation=one, reason=reason)
        with self.lock:
            self.interferences.append(found)
        if self.on_interference is not None:
            self.on_interference(found)

    def reasons(self) -> List[Interference]:
        with self.lock:
            return list(self.interferences)


# ----------------------------------------------------------------------
# 这一趟跑的记录：读的哪份规范、用的哪条规则、谁伸了手
# ----------------------------------------------------------------------


class RunRecord:
    """`<会议目录>/rb.json`：这一趟读的是什么、谁在驱、谁伸了手。"""

    def __init__(self, store: MeetingStore, **fields: Any) -> None:
        self.path = Path(store.run_dir) / "rb.json"
        self.data: Dict[str, Any] = {"meeting_id": store.meeting.meeting_id,
                                     "driver_pid": os.getpid(),
                                     "started_at": utc_now(),
                                     "interference": []}
        self.data.update(fields)
        self.write()

    def write(self) -> None:
        self.data["updated_at"] = utc_now()
        text = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)

    def interfered(self, found: Interference) -> None:
        self.data["interference"].append(found.as_dict())
        self.write()

    def ended(self, status: str, phase: str) -> None:
        self.data["ended_at"] = utc_now()
        self.data["end"] = {"status": status, "phase": phase}
        self.write()


# ----------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------

HARNESSES = {
    "claude": ("claude-code", "deepseek"),
    "claude-code": ("claude-code", "deepseek"),
    "responses": ("responses", "openai"),
    "codex": ("codex-app-server", "openai"),
    "codex-app-server": ("codex-app-server", "openai"),
    "trae": ("trae-agent", "openai"),
    "trae-agent": ("trae-agent", "openai"),
    "trae-acp": ("trae-acp", "openai"),
}


def build_parser() -> argparse.ArgumentParser:
    """命令行：`rb cli` 开一场会，`rb web` 开一个网页看会。"""
    parser = argparse.ArgumentParser(
        prog="rb",
        description="开一场会（rb cli），或者开一个网页看工作区里所有的会（rb web）。",
        epilog=(
            "例：\n"
            "  rb cli \"把 tests/ 里那几个红都修了\"\n"
            "  rb cli --norm-repo-path ~/workspace/RbNormViba2 --norm-commit-id 978366b \"…\"\n"
            "  rb cli --no-interference-rule-path ~/rules/no_interference.viba \"…\"\n"
            "  rb cli --resume ~/work/.meeting/20261012-101010-abc123 \"接着做\"\n"
            "  rb web --workspace ~/work\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"rb {VERSION}")
    commands = parser.add_subparsers(dest="command", metavar="<命令>")

    cli = commands.add_parser(
        "cli", prog="rb cli", help="给一句话就开一场会，跑到它自己停下；有人伸手就把它停掉",
        description="给一句话就开一场会，跑到它自己停下；有人伸手就把它停掉。"
                    "终端输出只有一行：看这场会该跑的那条 `rb web` 命令。")
    cli.add_argument("prompt", nargs="*", metavar="PROMPT",
                     help="交给这场会的那句话；多个词用空格接起来，`-` 表示从标准输入读")
    cli.add_argument("--resume", default=None, metavar="MEETING",
                     help="接着跑这一场会：给它的数据目录（`<工作区>/.meeting/<会议号>`）")
    cli.add_argument("--meeting-id", default=None, metavar="ID",
                     help="这场会叫什么；不给自己起一个")
    cli.add_argument("--workspace", default=None, metavar="DIR",
                     help="这场会在哪个工作区里跑；不给就是现在这个目录")
    cli.add_argument("--harness", default=None, choices=sorted(HARNESSES), metavar="NAME",
                     help="新会话默认用哪套：claude、responses、codex、trae（别名也认）")
    cli.add_argument("--norm-repo-path", default=None, metavar="DIR", help="规范仓库在哪儿")
    cli.add_argument("--norm-commit-id", default=None, metavar="SHA",
                     help="读规范仓库的哪一版；不给就是那个目录现在的样子")
    cli.add_argument("--no-interference-rule-path", default=None, metavar="FILE",
                     help="干涉规则那份 viba 程序在哪儿；不给就用内置那一条")
    cli.add_argument("--no-interference-rule-commit-id", default=None, metavar="SHA",
                     help="干涉规则读哪一版；不给就是那个文件现在的样子")
    cli.add_argument("--json", action="store_true",
                     help="那一行命令写成一条 JSON（`{\"type\": \"web\", \"command\": …}`）")

    web = commands.add_parser(
        "web", prog="rb web", help="开一个网页看工作区里所有的会，包括已经跑完的那些",
        description="开一个网页看工作区里所有的会（一边跑的、已经跑完的都在里面）。"
                    "这一条一直开着，Ctrl-C 收摊；它印出来的就是那个链接。")
    web.add_argument("--workspace", default=None, metavar="DIR",
                     help="看哪个工作区；不给就是现在这个目录")
    web.add_argument("--host", default="127.0.0.1", help="听哪个地址（只认本机）")
    web.add_argument("--port", type=int, default=0,
                     help="听哪个端口；0 就是让系统挑一个没人用的")
    web.add_argument("--harness", default=None, choices=sorted(HARNESSES), metavar="NAME",
                     help="新会话默认用哪套：claude、responses、codex、trae（别名也认）")
    return parser


def read_prompt(words: Sequence[str], stdin=None) -> str:
    """命令行上那句话：`-` 就是从标准输入读一整份。"""
    if list(words) == ["-"]:
        handle = stdin if stdin is not None else sys.stdin
        return handle.read().strip()
    return " ".join(words).strip()


def web_command(workspace: Path) -> str:
    """看这个工作区该跑的那条命令（`rb cli` 的终端输出就是它）。"""
    import shlex
    return f"rb web --workspace {shlex.quote(str(workspace))}"


class Reporter:
    """`rb cli` 印给外面看的那一行；`--json` 就是一行一条 JSON。"""

    def __init__(self, as_json: bool = False, out=None) -> None:
        self.as_json = as_json
        self.out = out if out is not None else sys.stdout

    def web(self, workspace: Path, meeting_id: str) -> None:
        command = web_command(workspace)
        if self.as_json:
            self.out.write(json.dumps({"type": "web", "command": command,
                                       "meeting_id": meeting_id,
                                       "workspace": str(workspace)},
                                      ensure_ascii=False) + "\n")
        else:
            self.out.write(command + "\n")
        self.out.flush()


# ----------------------------------------------------------------------
# 这一趟
# ----------------------------------------------------------------------


class Run:
    """一场会从建起来（或者接着跑）到停下。"""

    def __init__(self, args: argparse.Namespace, reporter: Reporter,
                 service_factory: Optional[Callable[..., MeetingWebService]] = None) -> None:
        self.args = args
        self.reporter = reporter
        self.service_factory = service_factory or (lambda **kwargs: MeetingWebService(**kwargs))
        self.service: Optional[MeetingWebService] = None
        self.store: Optional[MeetingStore] = None
        self.watch: Optional[DriveWatch] = None
        self.record: Optional[RunRecord] = None
        self.rule: Optional[InterferenceRule] = None
        self.prompt = ""
        self.workspace = Path.cwd()
        self.exit_code = EXIT_OK
        self._stop_said = False
        self._saw_work = False
        self._started_at = time.monotonic()

    # -- 建起来 ----------------------------------------------------

    def prepare(self) -> None:
        args = self.args
        norm_root, norm_commit = materialize_norm(args.norm_repo_path, args.norm_commit_id)
        if norm_root is not None:
            os.environ["REMOTEBUDDY_NORM"] = str(norm_root)
            # 这一份得真像一份规范：不像就当场说，别等会议开起来才发现
            workflow_root(project_root=PROJECT_ROOT)
        source, where, commit = read_rule(args.no_interference_rule_path,
                                          args.no_interference_rule_commit_id)
        self.rule = InterferenceRule(source, where, commit)
        self.rule.check()
        self.prompt = read_prompt(args.prompt)
        if args.resume is None and not self.prompt:
            raise RuntimeError("没说要它做什么：给一句话（`-` 就是从标准输入读）")
        backend, provider = _harness_pair(args.harness)
        self.workspace = self._workspace()
        self.service = self.service_factory(
            project_root=PROJECT_ROOT,
            workspace=self.workspace,
            default_backend=backend,
            default_provider=provider,
        )

    def _workspace(self) -> Path:
        """这一场会开在哪个目录：接着跑就从那一场会的数据目录推出来。"""
        if self.args.resume is None:
            return Path(self.args.workspace or Path.cwd()).expanduser().resolve()
        meeting_dir = meeting_path(self.args.resume)
        if self.args.workspace:
            asked = Path(self.args.workspace).expanduser().resolve()
            if asked != meeting_dir.parent.parent:
                raise RuntimeError(f"这场会在 {meeting_dir.parent.parent} 里，不是 {asked}")
        return meeting_dir.parent.parent

    # -- 起会 ------------------------------------------------------

    def open_meeting(self) -> MeetingStore:
        assert self.service is not None
        args = self.args
        if args.resume is None:
            payload: Dict[str, Any] = {"message": self.prompt,
                                       "workspace": str(self.workspace)}
            if args.meeting_id:
                payload["meeting_id"] = args.meeting_id
            store = self.service.start(payload)
        else:
            meeting_dir = meeting_path(args.resume)
            meeting_id = meeting_dir.name
            try:
                store = self.service.load_store(meeting_id)
            except Exception:                              # noqa: BLE001 - 服务认不出就自己读
                store = MeetingStore.load_at(meeting_dir, meeting_id)
            if self.prompt:
                # 人说的那一句：`message` 回的是 (这场会, agent 起没起来的那个原因)
                store, _agent_error = self.service.message(meeting_id, {"message": self.prompt})
            else:
                store = self._carry_on(store, meeting_id)
        self.store = store
        return store

    def _carry_on(self, store: MeetingStore, meeting_id: str) -> MeetingStore:
        """接着跑这一场：先把没跑完的那几趟从单的进度上接起来，接不上再看是不是要叫醒 agent。

        只动这一场：工作区里别的会一个字都不碰。
        """
        assert self.service is not None
        unfinished = self.service.unfinished_tickets(store)
        if unfinished is not None:
            case, words, target = unfinished
            # 从单读到哪儿了接着跑，是这一处的私事（页面上没有这一颗按钮：那是 assistant 的活），
            # 所以这里用的是服务自己那一手 `_start_tickets`。
            self.service._start_tickets(store, meeting_id, words, target, case=case)
            return self.service.load_store(meeting_id)
        if store.meeting.phase in {"api_error", "cycle_timeout"}:
            return self.service.resume(meeting_id)
        return store

    # -- 跑到停下 --------------------------------------------------

    def wait_until_done(self) -> None:
        assert self.service is not None and self.store is not None and self.watch is not None
        service = self.service
        meeting_id = self.store.meeting.meeting_id
        while True:
            if self.watch.reasons():
                self.exit_code = EXIT_INTERFERED
                return
            store = service.load_store(meeting_id)
            self.store = store
            status = store.meeting.status
            phase = store.meeting.phase
            if status in {"completed", "failed", "interrupted"}:
                return
            running = self._live_work()
            self._saw_work = self._saw_work or running
            if not running:
                if phase in {"created", "starting"}:
                    if not self._saw_work and time.monotonic() - self._started_at > START_GRACE:
                        self.exit_code = EXIT_RUN_FAILED
                        return
                else:
                    return
            time.sleep(WATCH_INTERVAL)

    def _live_work(self) -> bool:
        """这一场会现在还有没有活没干完（这一场自己的那几个线程）。"""
        assert self.service is not None and self.store is not None
        meeting_id = self.store.meeting.meeting_id
        service = self.service
        with service.lock:
            running = any(worker.is_alive() for worker in (
                service.jobs.get(meeting_id),
                service.ticket_jobs.get(meeting_id),
                service.capture_jobs.get(meeting_id),
            ) if worker is not None)
            queued = meeting_id in service.queued_jobs
        return running or queued

    # -- 停下 ------------------------------------------------------

    def stop_everything(self) -> None:
        if self.watch is not None:
            self.watch.close()
            self.watch = None
        if self.rule is not None:
            import shutil
            shutil.rmtree(self.rule.workdir, ignore_errors=True)

    def run(self) -> int:
        try:
            self.prepare()
            store = self.open_meeting()
            self.record = RunRecord(store,
                                    workspace=str(self.workspace),
                                    prompt=self.prompt,
                                    norm_root=os.environ.get("REMOTEBUDDY_NORM", ""),
                                    norm_commit=self.args.norm_commit_id or "",
                                    rule_where=self.rule.where,
                                    rule_commit=self.rule.commit)
            self.watch = DriveWatch(self.service, store, self.rule,
                                    on_interference=self._interfered)
            self.watch.start()
            # 终端输出就这一行：看这场会该跑哪条命令。这一趟自己不开网页服务。
            self.reporter.web(self.workspace, store.meeting.meeting_id)
            self.wait_until_done()
            return self._finish()
        except KeyboardInterrupt:
            self._stop_meeting("操作的人按了中断")
            self.exit_code = EXIT_RUN_FAILED
            return self.exit_code
        except Exception as bad:                            # noqa: BLE001 - 起不来的原因要印出来
            sys.stderr.write(f"rb cli: {type(bad).__name__}: {bad}\n")
            self.exit_code = EXIT_RUN_FAILED
            return self.exit_code
        finally:
            self.stop_everything()

    def _interfered(self, found: Interference) -> None:
        if self.record is not None:
            self.record.interfered(found)
        self._stop_meeting(found.reason)
        self._note_in_the_journal(found)

    def _note_in_the_journal(self, found: Interference) -> None:
        """伸手这回事也写一条进这场会的 journal：跑完之后在 `rb web` 上也看得见。"""
        if self.service is None or self.store is None:
            return
        try:
            self.service.load_store(self.store.meeting.meeting_id).append_event(
                "state", "rb", "interfered",
                content=found.reason,
                data={"status": "interfered", "what": found.observation.said(),
                      "actor": found.observation.actor, "action": found.observation.action})
        except Exception:                                   # noqa: BLE001 - 记一笔失败不挡停会
            pass

    def _stop_meeting(self, reason: str) -> None:
        """停会：先写停止请求让它自己收尾，写不进去（或者它没收）就直接杀掉那一场。"""
        if self.service is None or self.store is None:
            return
        meeting_id = self.store.meeting.meeting_id
        try:
            self.service.stop(meeting_id)
        except Exception:                                   # noqa: BLE001 - 收尾失败就硬停
            try:
                self.service.stop(meeting_id, force=True)
            except Exception as worse:                      # noqa: BLE001
                sys.stderr.write(f"rb cli: 停不下来 {meeting_id}：{worse}\n")

    def _wait_for_own_work(self, seconds: float = 15.0) -> None:
        """等这一场自己在做的那几趟收尾：它收尾时会把阶段写回去，所以最后那一停要等它写完。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._live_work():
                return
            time.sleep(WATCH_INTERVAL)

    def _finish(self) -> int:
        assert self.service is not None and self.store is not None
        found = self.watch.reasons() if self.watch is not None else []
        if found:
            self.exit_code = EXIT_INTERFERED
            # 伸了手就当场停：先把停止请求写下去、把那一轮打断。在做的那一趟收尾时还会把
            # 阶段写回"收尾提议"，所以等它利索收完，再停一次——盘上留下的就是"停了"。
            self._stop_meeting(found[0].reason)
            self._note_in_the_journal(found[0])
            self._wait_for_own_work()
            self._stop_meeting(found[0].reason)
        store = self.service.load_store(self.store.meeting.meeting_id) if self.service else self.store
        self.store = store
        status, phase = store.meeting.status, store.meeting.phase
        if self.record is not None:
            self.record.ended(status, phase)
        # 流程那一关的判决就是这一趟的判决：`mgmt_critic` 说没走完，退出码就是那一个，它点出的
        # 问题印在标准错误上（这一趟的终端输出照旧只有 stdout 那一行）。判过、或者这一场根本没
        # 走到那一关（起不来、被人伸手停了），才轮到下面那几个通用退出码。
        verdict = (the_flow_verdict(Path(store.run_dir))
                   if self.exit_code == EXIT_OK else None)
        if self.exit_code == EXIT_OK and verdict is not None and not verdict[0]:
            self.exit_code = EXIT_FLOW_INCOMPLETE
            problems = verdict[1] or "mgmt_critic 说这一场的流程没走完"
            sys.stderr.write(f"rb cli: 流程那一关没过：{problems}\n")
        elif self.exit_code == EXIT_OK and status in {"failed", "interrupted"}:
            self.exit_code = EXIT_RUN_FAILED
        elif self.exit_code == EXIT_OK and phase in {"api_error", "cycle_timeout"}:
            self.exit_code = EXIT_RUN_FAILED
        return self.exit_code


def _free_port(host: str) -> int:
    """让系统挑一个现在没人用的端口（`--port 0` 就是"随便挑一个"）。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _harness_pair(name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`--harness` 说的是哪一套后端：`(backend, provider)`；没给就是两个 None。"""
    if not name:
        return None, None
    return HARNESSES[name]


def run_web(args: argparse.Namespace) -> int:
    """`rb web`：开一个网页看工作区里所有的会（一边跑的、已经跑完的都在里面）。"""
    workspace = Path(args.workspace or Path.cwd()).expanduser()
    if not workspace.is_dir():
        sys.stderr.write(f"rb web: 工作区不在：{workspace}\n")
        return EXIT_RUN_FAILED
    backend, provider = _harness_pair(args.harness)
    service = MeetingWebService(
        project_root=PROJECT_ROOT,
        workspace=workspace,
        default_backend=backend,
        default_provider=provider,
    )
    server = build_server(args.host, args.port or _free_port(args.host), service=service)
    print(f"RemoteBuddy workspace: http://{args.host}:{server.server_address[1]}/", flush=True)
    print(f"工作区：{service.workspace}（会议数据在 {service.workspace / MeetingStore.WORKSPACE_DIR}/）",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


def meeting_path(asked: str) -> Path:
    """`--resume` 给的那条路径：它得是一场会的数据目录（`<工作区>/.meeting/<会议号>`）。"""
    where = Path(asked).expanduser().resolve()
    if not where.is_dir():
        raise RuntimeError(f"这一场会的数据目录不在：{where}")
    if where.parent.name != ".meeting":
        raise RuntimeError(f"这不是一场会的数据目录（它不在某个工作区的 .meeting/ 下）：{where}")
    return where


def main(argv: Optional[Sequence[str]] = None,
         service_factory: Optional[Callable[..., MeetingWebService]] = None) -> int:
    """`rb` 这一条命令：`cli` 开一场会，`web` 开一个网页看会。"""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as done:                              # argparse 自己的出口（--help / 参数不对）
        return int(done.code or 0)
    if args.command == "web":
        return run_web(args)
    if args.command == "cli":
        return Run(args, Reporter(as_json=args.json), service_factory=service_factory).run()
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
