"""agent 不碰终端：它交命令，harness 去操作（`AgentCmd` -> `AgentCmdResult`）。

分两种命令（ADT 里写的 `AgentCmd = $cmd str | $appliable_diff str`）：

- `run_command` / `ensure_shell`：`$cmd` 那条路——先确认终端前台是 shell，再执行、
  把**整个缓存**截下来，最后 `echo $?` 取返回码（`TmuxCmdResult`）。
- `tmux_apply_diff_by_vim`：`$appliable_diff` 那条路——一份 unified diff 变成一串
  vim 动作，逐步执行（每步之间可以被打断、要问过 actor 才继续），最后从工作区取回
  新 diff 和它要求的那份对比，不一致就再来一轮。

这份文件现在只有接口：测试先写（见 `tests/test_tmux_vim.py`），实现按测试一条条补。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

# 一次 vim 动作里的按键 token，和现有 keystroke token 同形：
# {"kind": "text"|"ctrl"|"rest", "value": ...}
KeyToken = Dict[str, Any]
Runner = Callable[..., Any]


class DiffError(RuntimeError):
    """这份 diff 读不出来（不是 unified diff、hunk 头坏掉、行前缀不认识）。"""


class DiffNotAppliable(RuntimeError):
    """这份 diff 在当前工作区上应用不了（上下文对不上、文件不存在、路径越界）。"""


class Interrupted(RuntimeError):
    """执行过程中被打断（人说话了、critic 提问了、页面上收回了控制）。"""


@dataclass(frozen=True)
class TmuxCmdResult:
    """在共享终端里跑一条命令的结果。"""

    retcode: str
    output_on_screen: str


@dataclass(frozen=True)
class ShellCmdResult:
    """不走共享终端的结果（harness 自己在工作区里跑的那种）。"""

    retcode: str
    stdout: str
    stderr: str


AgentCmdResult = Union[TmuxCmdResult, ShellCmdResult]


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    # 原始行，带前缀：' ' 上下文、'-' 删、'+' 增；'\' 是 "No newline at end of file"
    lines: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FilePatch:
    old_path: str
    new_path: str
    hunks: Tuple[Hunk, ...] = ()
    is_new: bool = False
    is_delete: bool = False
    is_rename: bool = False


@dataclass(frozen=True)
class VimStep:
    """vim 里的一步：一串按键 + 一句"这一步在干什么"（写进 journal，给人和 critic 看）。"""

    keys: Tuple[KeyToken, ...]
    label: str = ""


@dataclass(frozen=True)
class Snapshot:
    """动手之前给整个工作区拍的那张快照（`git worktree`）。"""

    workspace: Path
    branch: str
    path: Path
    commit: str


@dataclass
class ApplyOutcome:
    """一轮 apply 的收尾账：新 diff、对不对、用了几轮。"""

    result: Optional[AgentCmdResult] = None
    wanted_diff: str = ""
    got_diff: str = ""
    rounds: int = 0
    steps_taken: list = field(default_factory=list)


def parse_unified_diff(text: str) -> list:
    """把一份 unified diff 读成 `[FilePatch]`；读不出来就抛 `DiffError`。"""
    raise NotImplementedError("parse_unified_diff")


def vim_steps_for(patch: FilePatch) -> list:
    """一个文件的补丁变成一串 vim 动作（同一文件多个 hunk 要**从后往前**改，行号才不会串）。"""
    raise NotImplementedError("vim_steps_for")


def vim_steps_for_diff(text: str) -> list:
    """整份 diff 变成一串 vim 动作。"""
    raise NotImplementedError("vim_steps_for_diff")


def ensure_shell(transport: Any, pane_id: str, *, restarter: Optional[Callable[[], str]] = None) -> list:
    """确认终端前台是 shell：不是就想办法退出来，实在不行重启会话。返回做过哪些动作。"""
    raise NotImplementedError("ensure_shell")


def run_command(
    transport: Any,
    store: Any,
    pane_id: str,
    command: str,
    *,
    interrupt_check: Optional[Callable[[], bool]] = None,
    restarter: Optional[Callable[[], str]] = None,
    prompt: str = "",
) -> TmuxCmdResult:
    """`$cmd`：确认是 shell、执行、整屏抓下来、`echo $?` 取返回码。"""
    raise NotImplementedError("run_command")


def snapshot_worktree(workspace: Path, *, run: Optional[Runner] = None) -> Snapshot:
    """`git worktree` 给整个工作区拍快照（连被忽略的文件一起）。"""
    raise NotImplementedError("snapshot_worktree")


def worktree_diff(snapshot: Snapshot, *, run: Optional[Runner] = None, base: str = "HEAD") -> str:
    """从快照那边取"这次编辑对文件系统造成的新 diff"。"""
    raise NotImplementedError("worktree_diff")


def check_appliable(workspace: Path, diff: str, *, run: Optional[Runner] = None) -> ShellCmdResult:
    """这份 diff 能不能应用（`git apply --check`，不用进终端）。"""
    raise NotImplementedError("check_appliable")


def diffs_match(wanted: str, got: str) -> bool:
    """两份 diff 是不是同一件事（按落到文件上的结果比，不按文本逐字节比）。"""
    raise NotImplementedError("diffs_match")


def apply_vim_steps(
    transport: Any,
    store: Any,
    pane_id: str,
    steps: Sequence[VimStep],
    *,
    interrupt_check: Optional[Callable[[], bool]] = None,
    ask: Optional[Callable[[str], str]] = None,
) -> list:
    """一步步执行 vim 动作：每步之间检查打断，必要时问 actor 要不要继续。返回执行过的步骤。"""
    raise NotImplementedError("apply_vim_steps")


def tmux_apply_diff_by_vim(
    transport: Any,
    store: Any,
    pane_id: str,
    diff: str,
    *,
    ask: Optional[Callable[[str], str]] = None,
    interrupt_check: Optional[Callable[[], bool]] = None,
    max_rounds: int = 3,
    run: Optional[Runner] = None,
    restarter: Optional[Callable[[], str]] = None,
) -> ApplyOutcome:
    """一份 diff 通过 tmux 里的 vim 落到工作区，然后核对新 diff 是否与要求的一致。"""
    raise NotImplementedError("tmux_apply_diff_by_vim")
