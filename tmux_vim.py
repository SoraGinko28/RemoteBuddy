"""agent 不碰终端：它交命令，harness 去操作（`AgentCmd` -> `AgentCmdResult`）。

分两种命令（ADT 里写的 `AgentCmd = $cmd str | $appliable_diff str`）：

- `run_command` / `ensure_shell`：`$cmd` 那条路——先确认终端前台是 shell，再执行、
  把**整个缓存**截下来，最后 `echo $?` 取返回码（`TmuxCmdResult`）。
- `tmux_apply_diff_by_vim`：`$appliable_diff` 那条路——一份 unified diff 变成一串
  vim 动作，逐步执行（每步之间可以被打断、要问过 actor 才继续），落盘之后再核对
  "工作区现在是什么样"和"这份 diff 要求的什么样"是否一致，不一致就再来一轮。

核对不比对 diff 文本，而是比对文件树：动手前用一份**临时索引**把整个工作区写成一棵树
（`Snapshot`），再用另一份临时索引把 actor 要的那份 diff 应用到这棵树上，得到"要求的样子"
（`expected tree`）。每轮结束把工作区现状也写成一棵树，两者只在这份 diff 涉及的文件上比较，
差出来的那一小节就是下一轮要补的改动。这样上下文行的多少、hunk 怎么分组、行号是不是对的
都不影响判断，判据始终是文件里的内容。

这份文件里的 vim 只做这几件事：定位（`{n}G` 或按上下文行搜索）、删若干行、插若干行、
保存退出。一次 vim 动作最多写 `MAX_LINES_PER_STEP` 行，为的是每步之间都能被人打断。
"""
from __future__ import annotations

import difflib
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# 一次 vim 动作里的按键 token，和现有 keystroke token 同形：
# {"kind": "text"|"ctrl", "value": ...}
KeyToken = Dict[str, Any]
Runner = Callable[..., Any]

# 每一步写几行文本。一行一行的：人在屏上看得见每一行的到来，也能在两行之间叫停。
MAX_LINES_PER_STEP = 1

# vim 用一套干净的配置跑：不读用户的 vimrc（autoindent/expandtab 这类设置会把写进去的
# 行改样），不写 viminfo，nocompatible。
VIM_PROGRAM = ("vim", "-u", "NONE", "-N", "-i", "NONE")

SHELL_PROGRAMS = {"bash", "zsh", "sh", "dash", "ksh", "fish", "csh", "tcsh"}
VIM_PROGRAMS = {"vim", "vi", "nvim", "vim.basic", "vim.tiny"}
PAGER_PROGRAMS = {"less", "more", "most", "man"}
# 起会话时壳是被 `env`（或别的"把参数转交给真正程序"的进程）拉起来的，程序刚起落的那
# 一瞬间前台可能是它们，要给它时间换成壳，不能当成"不认识的程序"就重启会话。
EXEC_WRAPPER_PROGRAMS = {
    "env",
    "exec",
    "sudo",
    "doas",
    "nice",
    "ionice",
    "stdbuf",
    "timeout",
    "time",
    "command",
    "builtin",
    "xargs",
    "login",
}

REPL_PROGRAMS = {
    "python",
    "python2",
    "python3",
    "ipython",
    "ipython3",
    "node",
    "nodejs",
    "deno",
    "bun",
    "irb",
    "ruby",
    "perl",
    "php",
    "lua",
    "julia",
    "Rscript",
    "psql",
    "mysql",
    "sqlite3",
    "ghci",
    "iex",
    "cargo",
    "go",
}

# 命令跑完要等多久（构建/测试可能很久），取返回码的轮询间隔，vim 起落要等多久。
COMMAND_TIMEOUT_SECONDS = 900.0
POLL_SECONDS = 0.02
FOREGROUND_TIMEOUT_SECONDS = 20.0
SETTLE_TIMEOUT_SECONDS = 2.0


class DiffError(RuntimeError):
    """这份 diff 读不出来（不是 unified diff、hunk 头坏掉、行前缀不认识、路径越界）。"""


class DiffNotAppliable(RuntimeError):
    """这份 diff 在当前工作区上应用不了（上下文对不上、文件不存在、不是 git 仓库）。"""


class Interrupted(RuntimeError):
    """执行过程中被打断（人说话了、critic 提问了、页面上收回了控制）。"""


class CommandTimeout(RuntimeError):
    """等不到预期的事情发生（命令不返回、vim 起不来、前台程序退不出去）。"""


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

    @property
    def path(self) -> str:
        """这份补丁落到哪个文件上（删掉的文件看旧路径，别的看新路径）。"""
        return self.old_path if self.is_delete else self.new_path

    @property
    def paths(self) -> Tuple[str, ...]:
        """这份补丁牵到的所有路径（改名时新旧都算）。"""
        seen: List[str] = []
        for one in (self.old_path, self.new_path):
            if one and one != "/dev/null" and one not in seen:
                seen.append(one)
        return tuple(seen)


@dataclass(frozen=True)
class VimStep:
    """一步：要么是一串按键（vim 里），要么是一条 shell 命令（改名/删文件这类 vim 干不了的）。

    `label` 说清这一步在哪个文件的哪一段干什么——写进 journal，给人、critic 和 actor 看；
    打断之后要问 actor 要不要继续，问的就是这些标签。`path` 是这一步落到哪个文件上。
    `expect_foreground` 说这一步做完前台该是谁：`"editor"` 是刚打开 vim，`"shell"` 是回到
    shell，空串表示不管（一串按键）。
    """

    keys: Tuple[KeyToken, ...] = ()
    label: str = ""
    command: str = ""
    path: str = ""
    expect_foreground: str = ""

    @property
    def kind(self) -> str:
        return "shell" if self.command else "keys"


@dataclass(frozen=True)
class ShellReady:
    """确认过前台之后的结果：可能换成重启出来的新窗格，所以要把 pane_id 还回去。"""

    pane_id: str
    actions: Tuple[str, ...] = ()


@dataclass
class ApplyOutcome:
    """一轮 apply 的收尾账：新 diff、对不对、用了几轮、哪些文件还对不上。"""

    result: Optional[AgentCmdResult] = None
    wanted_diff: str = ""
    got_diff: str = ""
    rounds: int = 0
    steps_taken: List[VimStep] = field(default_factory=list)
    mismatched_files: List[str] = field(default_factory=list)
    # 干跑时发现的小事（行号写偏了多少…）：跟着结果一起交给模型，免得它照幻想写下一版。
    notes: List[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 读 diff
# ----------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_METADATA_PREFIXES = (
    "diff --git ",
    "diff --cc ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
    "binary files ",
    "Binary files ",
    "GIT binary patch",
    "\\ No newline at end of file",
)


_CODEX_WRAPPERS = ("*** Begin Patch", "*** End Patch")
_CODEX_FILE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$")


def _normalize_diff_lines(text: str) -> List[str]:
    """把常见的手写变体归一到标准 unified diff，再交给下面那段严格的读法。

    - Codex 的 `*** Begin Patch` / `*** End Patch` 包裹，和 `*** Update File: <路径>` /
      `*** Add File:` / `*** Delete File:` 这样的文件头：换成 `---` / `+++` 那两行。
    """
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.strip() in _CODEX_WRAPPERS:
            continue
        match = _CODEX_FILE.match(line)
        if match:
            kind, path = match.group(1), _strip_path_prefix(match.group(2).strip())
            if kind == "Update":
                lines.extend([f"--- a/{path}", f"+++ b/{path}"])
            elif kind == "Add":
                lines.extend(["--- /dev/null", f"+++ b/{path}"])
            else:
                lines.extend([f"--- a/{path}", "+++ /dev/null"])
            continue
        lines.append(line)
    return lines


def parse_unified_diff(text: str) -> List[FilePatch]:
    """把一份 unified diff 读成 `[FilePatch]`；读不出来就抛 `DiffError`。

    路径按 git 的习惯去掉 `a/`、`b/` 前缀（`--no-prefix` 的 diff 本来就没有前缀），
    `/dev/null` 原样留着，表示这个文件是新建或删掉。手写 diff 常有的几处走样（上下文行
    漏了开头那个空格、空行写成彻底的空行、hunk 头的行数数错）都会被顺着 hunk 头的计数
    补回来 —— 落地对不对最终由"文件内容"那一关说了算，这里不必替它把关。
    """
    if text is None:
        raise DiffError("diff 是空的")
    lines = _normalize_diff_lines(text)
    patches: List[FilePatch] = []
    index = 0
    rename_from = ""
    rename_to = ""
    while index < len(lines):
        line = lines[index]
        if line.startswith("rename from "):
            rename_from = line[len("rename from ") :]
            index += 1
            continue
        if line.startswith("rename to "):
            rename_to = line[len("rename to ") :]
            index += 1
            continue
        if line.startswith("diff --git ") or line.startswith("diff --cc "):
            # 纯改名的补丁没有 --- / +++ 这两行，块到这里就结束了。
            if rename_from and rename_to:
                patches.append(_rename_patch(rename_from, rename_to))
                rename_from = rename_to = ""
            index += 1
            continue
        if line.startswith(_METADATA_PREFIXES):
            index += 1
            continue
        if line.startswith("--- "):
            old_raw = line[4:]
            if index + 1 >= len(lines) or not lines[index + 1].startswith(("+++ ", "--- ")):
                raise DiffError(f"文件头后面不是 +++ 那一行: {line!r}")
            # 手写的 diff 里常见的一种走样：新侧那一行也写成 `---`（`--- b/x.py`）。
            # 位置都摆在那儿了，一眼就知道它是新侧头，不必为难。
            new_raw = lines[index + 1][4:]
            index += 2
            hunks: List[Hunk] = []
            while index < len(lines) and lines[index].startswith("@@"):
                hunk, index = _parse_hunk(lines, index)
                hunks.append(hunk)
            old_path = _header_path(old_raw)
            new_path = _header_path(new_raw)
            if rename_to:
                new_path = _header_path(rename_to)
            if rename_from:
                old_path = _header_path(rename_from)
            is_rename = bool(rename_from and rename_to and rename_from != rename_to)
            rename_from = rename_to = ""
            patches.append(
                FilePatch(
                    old_path=old_path,
                    new_path=new_path,
                    hunks=tuple(hunks),
                    is_new=old_path == "/dev/null",
                    is_delete=new_path == "/dev/null",
                    is_rename=is_rename,
                )
            )
            continue
        if not line.strip():
            index += 1
            continue
        raise DiffError(f"这一段不属于任何文件: {line!r}")
    if rename_from and rename_to:
        patches.append(_rename_patch(rename_from, rename_to))
    elif rename_from or rename_to:
        raise DiffError("只有 rename from / rename to 中的一半")
    return patches


def _rename_patch(old_raw: str, new_raw: str) -> FilePatch:
    old_path = _header_path(old_raw)
    new_path = _header_path(new_raw)
    return FilePatch(
        old_path=old_path,
        new_path=new_path,
        is_rename=old_path != new_path,
    )


def _recount(body: Sequence[str], old_count: int, new_count: int) -> Tuple[int, int]:
    """hunk 头的行数按正文重数：手写的 diff 里这个数常错，正文才是真的。"""
    counted_old = sum(1 for raw in body if raw[:1] in {" ", "-"})
    counted_new = sum(1 for raw in body if raw[:1] in {" ", "+"})
    if (counted_old, counted_new) == (old_count, new_count):
        return old_count, new_count
    return counted_old, counted_new


def _parse_hunk(lines: Sequence[str], index: int) -> Tuple[Hunk, int]:
    header = lines[index]
    match = _HUNK_HEADER.match(header)
    if match is None:
        raise DiffError(f"hunk 头读不出来: {header!r}")
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    body: List[str] = []
    old_seen = new_seen = 0
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.startswith("\\"):
            body.append(line)
            cursor += 1
            continue
        if old_seen == old_count and new_seen == new_count:
            break
        # 下一个文件的头（或者 `diff --git`）：这一段 hunk 到头了，行数按正文重数。
        if line.startswith(("--- ", "+++ ", "diff --git ", "diff --cc ")):
            break
        if not line:
            # 空白上下文行被写成了彻底的空行
            if old_seen < old_count and new_seen < new_count:
                body.append(" ")
                old_seen += 1
                new_seen += 1
                cursor += 1
                continue
            raise DiffError(f"hunk 里出现了空行: {header!r}")
        prefix = line[0]
        text = line[1:]
        if prefix not in " +-":
            # 上下文行漏了开头那个空格：hunk 头还等着旧行+新行各一条，那就按上下文算
            if old_seen < old_count and new_seen < new_count:
                prefix, text = " ", line
            else:
                raise DiffError(
                    f"hunk 里这一行既不是上下文（要以一个空格开头）也不是增删（+ / -）: {line!r}"
                )
        if prefix == " ":
            old_seen += 1
            new_seen += 1
        elif prefix == "-":
            old_seen += 1
        else:
            new_seen += 1
        body.append(prefix + text)
        cursor += 1
    # 手写的 diff 行数常常数错：正文到下一个头为止就是这一段，按正文重数，不跟它较真。
    old_count, new_count = _recount(body, old_count, new_count)
    return (
        Hunk(old_start, old_count, new_start, new_count, tuple(body)),
        cursor,
    )


def _header_path(raw: str) -> str:
    value = raw.rstrip("\n")
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = _decode_git_quoted(value[1:-1])
    value = _strip_path_prefix(value)
    if value == "/dev/null":
        return value
    return _safe_path(value)


def _decode_git_quoted(value: str) -> str:
    """git 把特殊路径写成 C 风格转义（`"a/\\344\\270\\255.py"`），这里还原成文字。"""
    out = bytearray()
    index = 0
    mapping = {"t": "\t", "n": "\n", "r": "\r", '"': '"', "\\": "\\"}
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            follower = value[index + 1]
            if follower in "01234567":
                digits = ""
                cursor = index + 1
                while cursor < len(value) and len(digits) < 3 and value[cursor] in "01234567":
                    digits += value[cursor]
                    cursor += 1
                out.append(int(digits, 8))
                index = cursor
                continue
            if follower in mapping:
                out.extend(mapping[follower].encode("utf-8"))
                index += 2
                continue
        out.extend(character.encode("utf-8"))
        index += 1
    return out.decode("utf-8", "replace")


def _strip_path_prefix(value: str) -> str:
    for prefix in ("a/", "b/"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _safe_path(path: str) -> str:
    if not path:
        raise DiffError("diff 里的文件路径是空的")
    if path.startswith("/") or path.startswith("~"):
        raise DiffError(f"diff 里的路径不能是绝对路径: {path!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise DiffError(f"diff 里的路径带控制字符: {path!r}")
    if any(part == ".." for part in PurePosixPath(path).parts):
        raise DiffError(f"diff 里的路径跑到工作区外面去了: {path!r}")
    return path


# ----------------------------------------------------------------------
# 一份补丁变成 vim 动作
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    """hunk 里一段连续改动。"""

    anchor_line: int  # 定位到这一行（1 起；纯插入时是"插在它后面"的那一行，0 表示文件开头）
    above: bool  # 插入写在 anchor_line 上面还是下面
    removed: Tuple[str, ...]
    added: Tuple[str, ...]
    line_number_known: bool  # 光看行号站不站得住（hunk 头里给了行号就是站得住的）


def vim_steps_for(patch: FilePatch) -> List[VimStep]:
    """一个文件的补丁变成一串 vim 动作（同一文件多个 hunk 要从后往前改，行号才不会串）。"""
    path = patch.path if patch.path != "/dev/null" else ""
    steps: List[VimStep] = []
    if patch.is_rename:
        old = _safe_path(patch.old_path)
        new = _safe_path(patch.new_path)
        steps.append(
            VimStep(
                command=f"mv -- {shlex.quote(old)} {shlex.quote(new)}",
                path=new,
                label=f"{new}: 从 {old} 改名过来",
                expect_foreground="shell",
            )
        )
    if patch.is_delete:
        steps.append(
            VimStep(
                command=f"rm -f -- {shlex.quote(path)}",
                path=path,
                label=f"{path}: 删掉这个文件",
                expect_foreground="shell",
            )
        )
        return steps
    if not patch.hunks:
        return steps
    steps.append(
        VimStep(
            command=f"{' '.join(VIM_PROGRAM)} -- {shlex.quote(path)}",
            path=path,
            label=f"{path}: 打开",
            expect_foreground="editor",
        )
    )
    for hunk in sorted(patch.hunks, key=lambda one: one.old_start, reverse=True):
        for segment in reversed(_hunk_segments(hunk)):
            steps.extend(_steps_for_segment(segment, hunk, path))
    steps.append(
        VimStep(
            keys=(_text(":wq"), _ctrl("Enter")),
            path=path,
            label=f"{path}: 写盘退出",
            expect_foreground="shell",
        )
    )
    return steps


def vim_steps_for_patches(patches: Sequence[FilePatch]) -> List[VimStep]:
    steps: List[VimStep] = []
    for patch in patches:
        steps.extend(vim_steps_for(patch))
    return steps


def vim_steps_for_diff(text: str) -> List[VimStep]:
    """整份 diff 变成一串 vim 动作。"""
    return vim_steps_for_patches(parse_unified_diff(text))


def _text(value: str) -> KeyToken:
    return {"kind": "text", "value": value}


def _ctrl(value: str) -> KeyToken:
    return {"kind": "ctrl", "value": value}


def _hunk_segments(hunk: Hunk) -> List[_Segment]:
    """把 hunk 的正文切成一段段连续改动，行号按 unified diff 的算法走一遍。

    行号的约定（unified diff 自己的写法）：`old_start` 是旧文件里 hunk 覆盖的第一行；
    但 `old_count` 为 0 时它表示**插在它后面**的那一行（0 表示插在文件最前面）。
    `anchor_line` 一律是"改动落在旧文件的哪一行上"：要删的段落是第一个被删行的行号，
    纯插入是"插在它后面"的那一行（0 表示插在第一行之前）。
    """
    segments: List[_Segment] = []
    old_line = hunk.old_start
    removed: List[str] = []
    added: List[str] = []
    consumed_before = 0
    started = False

    def finish() -> None:
        nonlocal removed, added, started, consumed_before
        if not started:
            return
        if removed:
            anchor = hunk.old_start + consumed_before
            above = True
        else:
            insert_after = hunk.old_start + consumed_before - (1 if hunk.old_count > 0 else 0)
            above = insert_after <= 0
            anchor = max(insert_after, 0)
        segments.append(
            _Segment(
                anchor_line=anchor,
                above=above,
                removed=tuple(removed),
                added=tuple(added),
                line_number_known=hunk.old_start > 0,
            )
        )
        removed, added, started = [], [], False

    for raw in hunk.lines:
        if raw.startswith("\\"):
            continue
        prefix = raw[0]
        text = raw[1:]
        if prefix == " ":
            finish()
            old_line += 1
            continue
        if not started:
            started = True
            consumed_before = old_line - hunk.old_start
            removed, added = [], []
        if prefix == "-":
            removed.append(text)
            old_line += 1
        elif prefix == "+":
            added.append(text)
    finish()
    return segments


def _steps_for_segment(segment: _Segment, hunk: Hunk, path: str) -> List[VimStep]:
    steps: List[VimStep] = []
    anchor_label = _anchor_label(segment, hunk)
    steps.append(
        VimStep(
            keys=tuple(_anchor_keys(segment, hunk)),
            path=path,
            label=f"{path}: 定位到{anchor_label}",
        )
    )
    chunks = _chunks(segment.added, MAX_LINES_PER_STEP)
    for position, chunk in enumerate(chunks):
        last_chunk = position + 1 == len(chunks)
        steps.append(
            VimStep(
                keys=tuple(
                    _insert_keys(
                        chunk,
                        mode=_insert_mode(segment),
                        first=position == 0,
                        trailing_enter=(not last_chunk) or _has_content_before(hunk),
                    )
                ),
                path=path,
                label=f"{path}: 在{anchor_label}写 {len(chunk)} 行",
            )
        )
    if segment.removed:
        count = len(segment.removed)
        target = max(segment.anchor_line + len(segment.added), 1)
        steps.append(
            VimStep(
                keys=(_text(f"{target}G"), _text(f"{count}dd")),
                path=path,
                label=f"{path}: 删掉原来的 {count} 行",
            )
        )
    return steps


def _chunks(lines: Sequence[str], size: int) -> List[List[str]]:
    return [list(lines[start : start + size]) for start in range(0, len(lines), size)]


def _has_content_before(hunk: Hunk) -> bool:
    """这个 hunk 里旧文件那边有没有行——有就说明文件本来不是空的，写在最前面时要空出一行。"""
    return hunk.old_count > 0


def _insert_mode(segment: _Segment) -> str:
    if segment.anchor_line == 0:
        return "start"
    return "above" if segment.above else "below"


def _insert_keys(
    chunk: Sequence[str],
    *,
    mode: str,
    first: bool,
    trailing_enter: bool = False,
) -> List[KeyToken]:
    """写一组行：先进入插入（`i` 插在光标处、`O` 开上面一行、`o` 开下面一行），
    行与行之间用 Enter 分开，最后 Esc 回到正常模式。

    "在文件最前面写"这一类用 `i` 而不是 `O`：`O` 在空文件里会多留下一个空行，`i` 加一个
    结尾的 Enter 能把原来的第一行顶到下面去。文件本来就是空的时候（新建的、或者本来就没有
    内容），那个结尾的 Enter 反而会多写一个空行，所以 hunk 里旧文件那边一行都没有时不用它。
    """
    if mode == "start":
        opener = "i"
    elif mode == "above":
        opener = "O" if first else "o"
    else:
        opener = "o"
    keys: List[KeyToken] = [_text(opener)]
    for offset, line in enumerate(chunk):
        if line:
            keys.append(_text(line))
        if offset + 1 < len(chunk) or (mode == "start" and trailing_enter):
            keys.append(_ctrl("Enter"))
    # 用 C-c 而不是 Esc 回到正常模式：Esc 是一个"前缀字节"，紧接着又来一个按键时终端和 vim
    # 可能把它当成一个组合键吃掉，人这边看到的就是"上一步没生效、下一句写进了缓冲区"。
    # C-c 是一个字节，没有这种歧义。
    keys.append(_ctrl("C-c"))
    return keys


def _anchor_keys(segment: _Segment, hunk: Hunk) -> List[KeyToken]:
    """定位：hunk 开头有上下文行就按整行搜索过去，没有就报行号。"""
    head = _head_context(hunk)
    if head and segment.line_number_known:
        moves = segment.anchor_line - hunk.old_start
        if moves >= 0:
            keys: List[KeyToken] = [_text("gg"), _text("/" + _search_pattern(head)), _ctrl("Enter")]
            if moves:
                keys.append(_text(f"{moves}j"))
            return keys
    return [_text(f"{max(segment.anchor_line, 1)}G")]


def _anchor_label(segment: _Segment, hunk: Hunk) -> str:
    head = _head_context(hunk)
    if head and segment.line_number_known:
        return f"上下文行 {head[:40]!r} 下面"
    return f"第 {max(segment.anchor_line, 1)} 行"


def _head_context(hunk: Hunk) -> str:
    for raw in hunk.lines:
        if raw.startswith("\\"):
            continue
        if raw.startswith(" "):
            text = raw[1:]
            return text if text.strip() else ""
        return ""
    return ""


def _search_pattern(line: str) -> str:
    """整行匹配的搜索式子：把 vim 认作特殊字符的都转义掉。"""
    escaped = re.sub(r"([\\^$.*\[\]~/])", r"\\\1", line)
    return f"^{escaped}$"


# ----------------------------------------------------------------------
# 在共享终端里干活
# ----------------------------------------------------------------------


def ensure_shell(
    transport: Any,
    pane_id: str,
    *,
    restarter: Optional[Callable[[], str]] = None,
    timeout: float = FOREGROUND_TIMEOUT_SECONDS,
) -> ShellReady:
    """确认终端前台是 shell：不是就想办法退出来，实在不行重启会话。"""
    actions: List[str] = []
    info = _pane_info(transport, pane_id)
    if info.get("dead", "0") != "0":
        pane_id = _restart(transport, pane_id, restarter, actions)
        return ShellReady(pane_id=pane_id, actions=tuple(actions))
    program = _program_name(info.get("command", ""))
    if not program or program in EXEC_WRAPPER_PROGRAMS:
        # tmux 在程序刚起落的那一瞬间会报空的命令名，或者报 `env` 这种"转交参数"的进程，
        # 那时候前台还在换，等一下再看，不要急着下判断。
        if _wait_for(transport, pane_id, _foreground_is_shell, SETTLE_TIMEOUT_SECONDS):
            actions.append("settle")
            return ShellReady(pane_id=pane_id, actions=tuple(actions))
        program = program or "?"
    if program in SHELL_PROGRAMS:
        transport.send_enter(pane_id)
        actions.append("enter")
        return ShellReady(pane_id=pane_id, actions=tuple(actions))
    if program in VIM_PROGRAMS:
        transport.send_tokens(pane_id, (_text(":q!"), _ctrl("Enter")))
        actions.append("quit-vim")
    elif program in PAGER_PROGRAMS:
        transport.send_tokens(pane_id, (_text("q"),))
        actions.append("quit-pager")
    elif program in REPL_PROGRAMS:
        transport.send_special(pane_id, "C-c")
        actions.append("interrupt")
    else:
        pane_id = _restart(transport, pane_id, restarter, actions, program=program)
        return ShellReady(pane_id=pane_id, actions=tuple(actions))
    if _wait_for(transport, pane_id, _foreground_is_shell, timeout):
        return ShellReady(pane_id=pane_id, actions=tuple(actions))
    if restarter is None:
        raise CommandTimeout(f"退不出前台程序 {program!r}，也没有重启会话的办法")
    pane_id = _restart(transport, pane_id, restarter, actions, program=program)
    return ShellReady(pane_id=pane_id, actions=tuple(actions))


def _restart(
    transport: Any,
    pane_id: str,
    restarter: Optional[Callable[[], str]],
    actions: List[str],
    *,
    program: str = "?",
) -> str:
    if restarter is None:
        raise CommandTimeout(f"共享终端的前台是 {program!r}，退不出来，也没有重启会话的办法")
    new_pane = restarter()
    if not new_pane:
        raise CommandTimeout("重启会话没有给出新的窗格")
    actions.append("restart")
    if not _wait_for(transport, new_pane, _foreground_is_shell, FOREGROUND_TIMEOUT_SECONDS):
        raise CommandTimeout("重启之后的窗格前台还不是 shell")
    return new_pane


def run_command(
    transport: Any,
    store: Any,
    pane_id: str,
    command: str,
    *,
    interrupt_check: Optional[Callable[[], bool]] = None,
    restarter: Optional[Callable[[], str]] = None,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    type_line: Optional[Callable[[str, str], None]] = None,
) -> TmuxCmdResult:
    """`$cmd`：确认是 shell、执行、整屏抓下来、`echo $?` 取返回码。

    `type_line(pane_id, text)` 是"把这一行敲进去"的执行器：给了就用它（按人的节奏一个字符
    一个字符敲，人在屏上看得见），没给就直接 `send_keys` 一次发完。
    """
    ready = ensure_shell(transport, pane_id, restarter=restarter)
    pane_id = ready.pane_id
    if interrupt_check is not None and interrupt_check():
        raise Interrupted("还没开始跑命令就被打断了")
    if type_line is not None:
        type_line(pane_id, command)
    else:
        transport.send_keys(pane_id, command)
    retcode = _read_retcode(transport, pane_id, interrupt_check, timeout=timeout)
    screen = transport.capture(pane_id)
    _record(transport, store, pane_id, change_kind="command")
    return TmuxCmdResult(retcode=retcode, output_on_screen=screen)


RETCODE_MARKER = "remotebuddy-retcode-"


def _retcode_command(token: str) -> str:
    return f"echo {RETCODE_MARKER}{token} $?"


def _read_retcode(
    transport: Any,
    pane_id: str,
    interrupt_check: Optional[Callable[[], bool]],
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """再敲一行带随机标记的 `echo`，从整屏缓存里把返回码读出来。

    标记必须每次都不一样：命令刚敲下去、壳还没轮到执行它的那一瞬间，整屏缓存里只有上一次
    那个标记，拿它去解析就会把上一条命令的返回码当成本次的，等于没等命令跑完。
    """
    token = uuid.uuid4().hex[:12]
    transport.send_keys(pane_id, _retcode_command(token))
    needle = f"{RETCODE_MARKER}{token}"
    deadline = time.monotonic() + timeout
    while True:
        screen = transport.capture(pane_id)
        retcode = _retcode_after_marker(screen, needle)
        if retcode is not None:
            return retcode
        if interrupt_check is not None and interrupt_check():
            raise Interrupted("等命令返回的时候被打断了")
        if time.monotonic() >= deadline:
            raise CommandTimeout(f"命令跑了超过 {timeout:.0f} 秒还没返回")
        time.sleep(POLL_SECONDS)


def _retcode_after_marker(screen: str, needle: str) -> Optional[str]:
    position = screen.rfind(needle)
    if position < 0:
        return None
    rest = screen[position + len(needle) :].split("\n", 1)[0]
    match = re.search(r"-?\d+", rest)
    return match.group(0) if match else None


def apply_vim_steps(
    transport: Any,
    store: Any,
    pane_id: str,
    steps: Sequence[VimStep],
    *,
    interrupt_check: Optional[Callable[[], bool]] = None,
    ask: Optional[Callable[[str], str]] = None,
    restarter: Optional[Callable[[], str]] = None,
    type_tokens: Optional[Callable[[str, Sequence[KeyToken]], None]] = None,
) -> List[VimStep]:
    """一步步执行 vim 动作：每步之间检查打断，必要时问 actor 要不要继续。返回执行过的步骤。

    一步要么是一串按键（一次交给终端，中途不查打断——所以一步最多写
    `MAX_LINES_PER_STEP` 行），要么是一条 shell 命令。shell 命令是 harness 按 diff 生成的，
    不是 actor 交上来的字符串，所以它不走 `workspace_command_text` 那道校验（删文件这类
    动作本来就在那道校验的禁止名单里）。
    """
    if not steps:
        return []
    executed: List[VimStep] = []
    if not _gate(interrupt_check, ask, steps[0], "动手之前"):
        return executed
    for position, step in enumerate(steps):
        if not _gate(interrupt_check, ask, step, f"第 {position + 1} / {len(steps)} 步"):
            break
        pane_id = _execute(
            transport,
            store,
            pane_id,
            step,
            restarter=restarter,
            interrupt_check=interrupt_check,
            type_tokens=type_tokens,
        )
        executed.append(step)
    return executed


def _gate(
    interrupt_check: Optional[Callable[[], bool]],
    ask: Optional[Callable[[str], str]],
    step: VimStep,
    when: str,
) -> bool:
    if interrupt_check is None or not interrupt_check():
        return True
    if ask is None:
        raise Interrupted(f"{when}被打断了：{step.label or step.command}")
    question = (
        f"{when}有新的消息插进来。下一步是「{step.label or step.command}」。"
        "要接着把这份改动落下去吗？回 continue 就继续，回别的就停在这里。"
    )
    return str(ask(question)).strip().lower() == "continue"


def _execute(
    transport: Any,
    store: Any,
    pane_id: str,
    step: VimStep,
    *,
    restarter: Optional[Callable[[], str]] = None,
    interrupt_check: Optional[Callable[[], bool]] = None,
    type_tokens: Optional[Callable[[str, Sequence[KeyToken]], None]] = None,
) -> str:
    if step.kind == "shell":
        if step.expect_foreground:
            ready = ensure_shell(transport, pane_id, restarter=restarter)
            pane_id = ready.pane_id
        before = ""
        if step.expect_foreground == "editor":
            try:
                before = transport.capture_visible(pane_id)
            except Exception:
                before = ""
        transport.send_keys(pane_id, step.command)
        if step.expect_foreground == "editor":
            if not _wait_for_editor(transport, pane_id, before, FOREGROUND_TIMEOUT_SECONDS):
                raise CommandTimeout(f"vim 没起来，或者起来了还没接管屏幕: {step.command}")
        elif step.expect_foreground == "shell":
            # 等这条命令自己跑完（`echo $?` 出来的那一行就是"跑完了"），不能等"前台是
            # shell"——刚敲下去的时候前台还没换成子进程，那一等就白等了。
            _read_retcode(transport, pane_id, interrupt_check)
    else:
        if type_tokens is not None:
            type_tokens(pane_id, list(step.keys))
        else:
            transport.send_tokens(pane_id, list(step.keys))
        if step.expect_foreground == "shell" and not _wait_for(
            transport, pane_id, _foreground_is_shell, FOREGROUND_TIMEOUT_SECONDS
        ):
            # 写盘退出没成功（编辑器还开着）。先把前台收回来，文件对不对下一轮的比对会说话。
            ready = ensure_shell(transport, pane_id, restarter=restarter)
            pane_id = ready.pane_id
    _record(transport, store, pane_id, change_kind="vim")
    return pane_id


def _record(transport: Any, store: Any, pane_id: str, *, change_kind: str) -> None:
    """每一步之后留一屏：给 journal 的用真 store，测试里就用那次截屏数一数。"""
    if store is not None:
        from workspace_capture import capture_workspace

        capture_workspace(
            store,
            transport,
            change_kind=change_kind,
            metadata={"agent_cmd": change_kind},
        )
    else:
        transport.capture_visible(pane_id)


def _pane_info(transport: Any, pane_id: str) -> Dict[str, str]:
    """读窗格状态；窗格或会话已经没了的时候当作"死的"，交给 restarter 去重开。"""
    try:
        info = transport.pane_info(pane_id)
    except Exception:
        return {"dead": "1", "command": "", "path": "", "cursor_x": "0", "cursor_y": "0",
                "pane_width": "0", "pane_height": "0"}
    if not isinstance(info, dict):
        return {"dead": "1", "command": "", "path": ""}
    return info


def _program_name(command: str) -> str:
    name = (command or "").strip().split()[0] if (command or "").strip() else ""
    return name.rsplit("/", 1)[-1]


def _foreground_is_shell(info: Dict[str, str]) -> bool:
    return _program_name(info.get("command", "")) in SHELL_PROGRAMS


def _foreground_is_editor(info: Dict[str, str]) -> bool:
    return _program_name(info.get("command", "")) in VIM_PROGRAMS


def _wait_for_editor(transport: Any, pane_id: str, before: str, timeout: float) -> bool:
    """等编辑器真的把屏幕接管过去：前台是编辑器，而且画面已经不是刚才那一屏了。

    只看前台进程名不够。从 vim 起来到它把终端设成 raw 模式之间有一小段：那段时间里终端
    还是按行输入，敲进去的按键会被当成行内容（`C-c` 更是直接变成信号，把还在初始化的 vim
    打断，剩下的按键就落到 shell 手里）。所以还要等它把画面换掉 —— 换掉了才说明它接管了。
    """
    deadline = time.monotonic() + timeout
    while True:
        if _foreground_is_editor(_pane_info(transport, pane_id)):
            try:
                screen = transport.capture_visible(pane_id)
            except Exception:
                screen = ""
            if screen and screen != before:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _wait_for(
    transport: Any,
    pane_id: str,
    predicate: Callable[[Dict[str, str]], bool],
    timeout: float = FOREGROUND_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate(_pane_info(transport, pane_id)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


# ----------------------------------------------------------------------
# 工作区快照与比对：harness 自己读文件、自己算，不占共享终端，也不要求这里是 git 仓库
# ----------------------------------------------------------------------
#
# 判据是"文件里的内容"，不是 diff 文本：动手之前把这份 diff 牵到的文件先读下来
# （`Snapshot`），把这份 diff 在内存里应用一遍得到"要求的样子"，落盘之后再把同样的文件
# 读一遍，逐文件比。差出来的那一小节（`difflib` 出来的 unified diff）就是下一轮要补的改动。
# 全程不用 git：`git apply` 比我们严（行数写错、上下文行少一个空格就"补丁损坏"），报错
# 也没法照着改；而模型手写的 diff 恰恰常常是那样。


@dataclass(frozen=True)
class Snapshot:
    """动手之前给这份 diff 牵到的文件拍的那张快照。

    `contents` 是每个文件当时的内容（None = 那时还不存在），比对照着它算。
    """

    workspace: Path
    contents: Dict[str, Optional[str]]


def _read_contents(workspace: Path, paths: Sequence[str]) -> Dict[str, Optional[str]]:
    """读下这些文件现在是什么；不存在的记 None（新文件就是这样）。"""
    contents: Dict[str, Optional[str]] = {}
    for path in paths:
        try:
            contents[path] = (workspace / path).read_text(
                encoding="utf-8", errors="surrogateescape"
            )
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            contents[path] = None
    return contents


def snapshot_workspace(workspace: Path, patches: Sequence[FilePatch]) -> Snapshot:
    """动手之前给这份 diff 牵到的文件拍一张快照（连没被跟踪、被忽略的文件一起）。"""
    resolved = Path(workspace).expanduser().resolve()
    if not resolved.is_dir():
        raise DiffNotAppliable(f"工作区目录不在了: {resolved}")
    paths = [path for patch in patches for path in patch.paths]
    return Snapshot(workspace=resolved, contents=_read_contents(resolved, paths))


def _snippet(lines: Sequence[str], around: int, span: int = 3) -> str:
    """文件里那一段的真内容，带行号 —— 退回给模型时它就照着这个改，不用再猜。"""
    start = max(0, around - span)
    end = min(len(lines), around + span + 1)
    return "\n".join(f"{number + 1:>6}  {lines[number]}" for number in range(start, end))


def _locate(lines: Sequence[str], block: Sequence[str], start: int) -> Optional[int]:
    """这个 hunk 要改的那一段在文件的哪一行。

    先看它自己说的位置；对不上就按上下文找最近的一处 —— 模型手写的行号常偏，上下文才是真的，
    落盘那一步（vim 里也是按上下文行搜索）就是这么找的，预检不该比它更严。
    """
    if not block:
        return start if 0 <= start <= len(lines) else None

    def matches(at: int) -> bool:
        return (
            0 <= at
            and at + len(block) <= len(lines)
            and all(lines[at + offset] == text for offset, text in enumerate(block))
        )

    if matches(start):
        return start
    candidates = [at for at in range(len(lines) - len(block) + 1) if matches(at)]
    if not candidates:
        return None
    return min(candidates, key=lambda at: (abs(at - start), at))


def _apply_patches(
    patches: Sequence[FilePatch],
    contents: Dict[str, Optional[str]],
    notes: Optional[List[str]] = None,
) -> Dict[str, Optional[str]]:
    """把补丁应用在这些内容上；对不上就抛 `DiffNotAppliable`，并说清"补丁要什么、文件里是什么"。

    报错要能照着改：模型手上只有这份 diff 和它刚才读过的文件，光说"损坏"它只能再猜一遍。
    """
    after = dict(contents)
    for patch in patches:
        if patch.is_rename:
            after[patch.new_path] = after.get(patch.old_path)
            after[patch.old_path] = None
        name = patch.path
        if patch.is_delete:
            after[name] = None
            continue
        body = after.get(name)
        lines = body.splitlines() if body is not None else []
        expects_content = any(
            raw[:1] in {" ", "-"} for hunk in patch.hunks for raw in hunk.lines
        )
        if body is None and not patch.is_new and expects_content:
            # 头里没写 /dev/null、可又要拿已有行来对上下文：那它想改的是个不存在的文件。
            # （纯 `@@ -0,0 +1,n @@` 那种创建，头写成 `a/<路径>` 也是创建，别为难。）
            raise DiffNotAppliable(
                f"{name}: 这个文件不存在 —— 要新建就写成 `--- /dev/null`，要改就把文件读准了再交"
            )
        result: List[str] = []
        cursor = 0
        for hunk in patch.hunks:
            start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
            block = [raw[1:] for raw in hunk.lines if raw[:1] in {" ", "-"}]
            located = _locate(lines, block, start)
            if located is None:
                wanted = block[0] if block else "（空 hunk）"
                raise DiffNotAppliable(
                    f"{name}: 找不到这个 hunk 要改的那一段 —— 补丁的第一行是 {wanted!r}，"
                    f"文件里没有。这个文件现在（第 {max(start + 1, 1)} 行附近）是这样：\n"
                    f"{_snippet(lines, start)}\n"
                    f"照这些行重写这一版，别照你上一版补丁里的行写。"
                )
            if located < cursor:
                raise DiffNotAppliable(f"{name}: 这一份 diff 里两个 hunk 叠在一起了")
            if notes is not None and block and located != start:
                # 干跑通过了，但它把行号写偏了：告诉它偏了多少、我按上下文放到了哪一行 ——
                # 不说的话它以为自己那一版原样落进去了，下一版就会对着幻想出来的内容写。
                notes.append(
                    f"{name}: 补丁说改动在第 {start + 1} 行，按上下文实际落在第 {located + 1} 行"
                    f"（偏 {located - start:+d} 行）"
                )
            result.extend(lines[cursor:located])
            position = located
            for raw in hunk.lines:
                if raw.startswith("\\"):
                    continue
                prefix, text = raw[0], raw[1:]
                if prefix == " ":
                    if position >= len(lines) or lines[position] != text:
                        actual = lines[position] if position < len(lines) else "（文件到头了）"
                        raise DiffNotAppliable(
                            f"{name}: 第 {position + 1} 行的上下文对不上 —— 补丁写的是 {text!r}，"
                            f"文件里是 {actual!r}。这一段现在是：\n{_snippet(lines, position)}\n"
                            f"照这些行重写这一版，别照你上一版补丁里的行写。"
                        )
                    result.append(text)
                    position += 1
                elif prefix == "-":
                    if position >= len(lines) or lines[position] != text:
                        actual = lines[position] if position < len(lines) else "（文件到头了）"
                        raise DiffNotAppliable(
                            f"{name}: 要删的第 {position + 1} 行对不上 —— 补丁写的是 {text!r}，"
                            f"文件里是 {actual!r}。这一段现在是：\n{_snippet(lines, position)}\n"
                            f"照这些行重写这一版，别照你上一版补丁里的行写。"
                        )
                    position += 1
                else:
                    result.append(text)
            cursor = position
        result.extend(lines[cursor:])
        after[name] = "".join(line + "\n" for line in result)
    return after


def _unified_diff(before: Optional[str], after: Optional[str], path: str) -> str:
    """两个内容之间的 unified diff；哪边没有就是新建/删除。"""
    if before == after:
        return ""
    old_lines = (before or "").splitlines(keepends=True)
    new_lines = (after or "").splitlines(keepends=True)
    old_name = "/dev/null" if before is None else f"a/{path}"
    new_name = "/dev/null" if after is None else f"b/{path}"
    body = [
        line
        for line in difflib.unified_diff(
            old_lines, new_lines, fromfile=old_name, tofile=new_name, n=3
        )
        if not line.startswith(("--- ", "+++ "))
    ]
    if not body:
        return ""
    return f"--- {old_name}\n+++ {new_name}\n" + "".join(body)


def workspace_diff(snapshot: Snapshot, contents: Dict[str, Optional[str]]) -> str:
    """从快照那边取"这次编辑对这些文件做了什么"。"""
    return "".join(
        _unified_diff(snapshot.contents.get(path), contents.get(path), path)
        for path in snapshot.contents
    )


def check_appliable(workspace: Path, diff: str, *, run: Optional[Runner] = None) -> ShellCmdResult:
    """这份 diff 能不能应用：用我们自己的读法和落盘算法在内存里试一遍。

    不走 `git apply --check`：它比我们严（没有上下文行的、行数对不上的、手写走样的一律毙掉），
    报错还是它那套（"补丁在 <stdin>:8 损坏"），模型拿着没法改。这里把它读到的那份文件和补丁
    摆在一起比，报"哪一行、补丁要什么、文件里是什么"，它照着改就行。
    """
    del run
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        return ShellCmdResult(retcode="1", stdout="", stderr=f"工作区目录不在了: {workspace}")
    try:
        patches = parse_unified_diff(diff)
    except DiffError as exc:
        return ShellCmdResult(retcode="1", stdout="", stderr=str(exc))
    if not patches:
        return ShellCmdResult(retcode="1", stdout="", stderr="这份 diff 里没有文件改动")
    paths = [path for patch in patches for path in patch.paths]
    try:
        _apply_patches(patches, _read_contents(workspace, paths))
    except DiffNotAppliable as exc:
        return ShellCmdResult(retcode="1", stdout="", stderr=str(exc))
    return ShellCmdResult(retcode="0", stdout="", stderr="")


def cleanup_snapshot(snapshot: Snapshot) -> None:
    """按文件拍的快照没有临时目录要收，留着这个函数是为了调用方那句话读得通。"""
    del snapshot


def diffs_match(wanted: str, got: str) -> bool:
    """两份 diff 说的是不是同一件事：按文件里改了哪些行比，不看 hunk 头和上下文行。

    落地成功与否的正式判据是文件内容的比较（`tmux_apply_diff_by_vim` 里逐文件比）；
    这个函数给 journal、提示和调试用：说清"这两份 diff 的改动是不是一回事"。
    """
    return _changes_of(wanted) == _changes_of(got)


def _changes_of(text: str) -> List[Tuple[str, List[Tuple[int, Tuple[str, ...], Tuple[str, ...]]]]]:
    merged: Dict[str, List[Tuple[int, Tuple[str, ...], Tuple[str, ...]]]] = {}
    for patch in parse_unified_diff(text):
        changes = merged.setdefault(patch.path, [])
        for hunk in patch.hunks:
            for segment in _hunk_segments(hunk):
                changes.append((segment.anchor_line, segment.removed, segment.added))
    return sorted((path, sorted(changes)) for path, changes in merged.items())


# ----------------------------------------------------------------------
# 整条路：一份 diff 落到工作区
# ----------------------------------------------------------------------


def tmux_apply_diff_by_vim(
    transport: Any,
    store: Any,
    pane_id: str,
    diff: str,
    *,
    ask: Optional[Callable[[str], str]] = None,
    interrupt_check: Optional[Callable[[], bool]] = None,
    max_rounds: int = 3,
    restarter: Optional[Callable[[], str]] = None,
    type_tokens: Optional[Callable[[str, Sequence[KeyToken]], None]] = None,
) -> ApplyOutcome:
    """一份 diff 通过 tmux 里的 vim 落到工作区，然后核对文件内容是否与要求的一致。"""
    patches = parse_unified_diff(diff)
    if not patches:
        raise DiffError("这份 diff 里没有文件改动")
    workspace = _workspace_of(store, transport, pane_id)
    snapshot = snapshot_workspace(workspace, patches)
    notes: List[str] = []
    try:
        expected = _apply_patches(patches, snapshot.contents, notes)
    except DiffNotAppliable as exc:
        cleanup_snapshot(snapshot)
        raise DiffNotAppliable(str(exc)) from exc
    paths = _ordered_paths(patches)
    outcome = ApplyOutcome(wanted_diff=diff, mismatched_files=[], notes=notes)
    pending = diff
    try:
        while True:
            outcome.rounds += 1
            steps = vim_steps_for_diff(pending)
            executed = apply_vim_steps(
                transport,
                store,
                pane_id,
                steps,
                interrupt_check=interrupt_check,
                ask=ask,
                restarter=restarter,
                type_tokens=type_tokens,
            )
            outcome.steps_taken.extend(executed)
            current = _read_contents(workspace, paths)
            outcome.got_diff = workspace_diff(snapshot, current)
            if len(executed) < len(steps):
                outcome.mismatched_files = [
                    path for path in paths if current.get(path) != expected.get(path)
                ]
                outcome.result = TmuxCmdResult(
                    retcode="130", output_on_screen=transport.capture(pane_id)
                )
                return outcome
            mismatched = [path for path in paths if current.get(path) != expected.get(path)]
            if not mismatched:
                outcome.mismatched_files = []
                outcome.result = TmuxCmdResult(
                    retcode="0", output_on_screen=transport.capture(pane_id)
                )
                return outcome
            outcome.mismatched_files = mismatched
            if outcome.rounds >= max_rounds:
                outcome.result = TmuxCmdResult(
                    retcode="1", output_on_screen=transport.capture(pane_id)
                )
                return outcome
            pending = "".join(
                _unified_diff(current.get(path), expected.get(path), path) for path in mismatched
            )
    finally:
        cleanup_snapshot(snapshot)


def _workspace_of(store: Any, transport: Any, pane_id: str) -> Path:
    path = getattr(store, "workspace_path", None) if store is not None else None
    if path:
        return Path(path)
    info = _pane_info(transport, pane_id)
    return Path(info.get("path") or ".")


def _ordered_paths(patches: Sequence[FilePatch]) -> List[str]:
    ordered: List[str] = []
    for patch in patches:
        for path in patch.paths:
            if path not in ordered:
                ordered.append(path)
    return ordered


def _diff_touches(diff: str, path: str) -> bool:
    try:
        parsed = parse_unified_diff(diff)
    except DiffError:
        return False
    for patch in parsed:
        if path in patch.paths:
            return True
    return False
