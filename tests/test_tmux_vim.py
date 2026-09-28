"""`tmux_vim` 的测试。

分两半：

- 假终端（`FakePane`，只记账、不跑真程序）管"什么时候发了什么按键、什么时候问了 actor"；
  真 git 管"快照、apply --check、两棵树比出来的差"。
- 真 tmux + 真 vim + 真 git（`RealTmuxTest`）管"这些按键落到文件上到底变成什么"。

每一条测试用的 diff 都是**多文件、多行**改动：用例由 `Case` 生成——给出每个文件的原始内容
和一份编辑清单（在哪一行改几行、删几行、插几行），由测试自己拼出 diff 文本、算出目标内容，
实现只负责把这份 diff 落到文件上。用例表里所有用例都会被 `MultiFileMultiLineGuardTest`
检查一遍，防止有人往里面塞单文件一行的用例。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import tmux_vim
from tmux_vim import (
    CommandTimeout,
    DiffError,
    DiffNotAppliable,
    FilePatch,
    Interrupted,
    ShellCmdResult,
    TmuxCmdResult,
    VimStep,
)

PROMPT = "bash-3.2$ "
SHELLS = {"bash", "zsh", "sh", "dash", "ksh", "fish"}


# ======================================================================
# 用例：每个文件给原始内容 + 一串编辑，diff 文本和目标内容都由用例自己算
# ======================================================================


@dataclass(frozen=True)
class Edit:
    kind: str  # "replace" | "insert" | "delete"
    line: int  # 1 起的行号；insert 是"插在这一行后面"，0 表示插在文件最前面
    count: int = 1  # replace / delete 吃掉几行
    lines: Tuple[str, ...] = ()  # 写进去的新行


@dataclass(frozen=True)
class Case:
    name: str
    files: Tuple[Tuple[str, Tuple[str, ...], Tuple[Edit, ...]], ...]
    context: int = 0  # 生成 diff 时带几行上下文
    new_files: Tuple[str, ...] = ()
    deleted_files: Tuple[str, ...] = ()
    renamed: Tuple[Tuple[str, str], ...] = ()
    note: str = ""

    def base_text(self, path: str) -> str:
        return _text_of(self.base_lines(path))

    def base_lines(self, path: str) -> List[str]:
        for name, lines, _edits in self.files:
            if name == path:
                return list(lines)
        return []

    def edits(self, path: str) -> Tuple[Edit, ...]:
        for name, _lines, edits in self.files:
            if name == path:
                return tuple(edits)
        return ()

    def target_lines(self, path: str) -> List[str]:
        return _apply_edits(self.base_lines(path), self.edits(path))

    def target_text(self, path: str) -> str:
        return _text_of(self.target_lines(path))

    def paths(self) -> List[str]:
        return [name for name, _lines, _edits in self.files]

    def rename_map(self) -> Dict[str, str]:
        return {old: new for old, new in self.renamed}

    def target_path(self, path: str) -> str:
        """这个文件在这份 diff 里最后叫什么名字（改名的看新名字）。"""
        return self.rename_map().get(path, path)

    def step_paths(self) -> List[str]:
        return [self.target_path(name) for name in self.paths()] + [
            new for old, new in self.renamed if old not in self.paths()
        ]


def _text_of(lines: Sequence[str]) -> str:
    return "".join(line + "\n" for line in lines)


def _apply_edits(base: Sequence[str], edits: Sequence[Edit]) -> List[str]:
    out = list(base)
    for edit in sorted(edits, key=lambda one: one.line, reverse=True):
        if edit.kind == "insert":
            out[edit.line : edit.line] = list(edit.lines)
        elif edit.kind == "replace":
            out[edit.line - 1 : edit.line - 1 + edit.count] = list(edit.lines)
        elif edit.kind == "delete":
            del out[edit.line - 1 : edit.line - 1 + edit.count]
        else:
            raise AssertionError(f"unknown edit kind: {edit.kind}")
    return out


def diff_text(case: Case, *, context: Optional[int] = None, with_git_header: bool = False) -> str:
    """按用例拼出 unified diff；行号、上下文都是这里算出来的，不看实现。"""
    width = case.context if context is None else context
    renamed = case.rename_map()
    chunks: List[str] = []
    for name, base, edits in case.files:
        new_name = renamed.get(name, name)
        old_header = "/dev/null" if name in case.new_files else f"a/{name}"
        new_header = "/dev/null" if name in case.deleted_files else f"b/{new_name}"
        if with_git_header or name in renamed:
            chunks.append(f"diff --git a/{name} b/{new_name}\n")
        if name in renamed:
            chunks.append("similarity index 90%\n")
            chunks.append(f"rename from {name}\nrename to {new_name}\n")
        chunks.append(f"--- {old_header}\n+++ {new_header}\n")
        chunks.append(_hunks_for(list(base), list(edits), width))
    for old, new in case.renamed:
        if old in case.paths():
            continue
        chunks.append(
            f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"
        )
    return "".join(chunks)


def _hunks_for(base: Sequence[str], edits: Sequence[Edit], context: int) -> str:
    hunks: List[str] = []
    delta = 0
    previous_end = None
    for edit in sorted(edits, key=lambda one: one.line):
        if edit.kind == "insert":
            old_pos = edit.line
            old_first = max(old_pos - context, 0)
            old_last = min(old_pos + context, len(base))
            body = [" " + line for line in base[old_first:old_pos]]
            body += ["+" + line for line in edit.lines]
            body += [" " + line for line in base[old_pos:old_last]]
        else:
            old_pos = edit.line - 1
            old_first = max(old_pos - context, 0)
            old_last = min(old_pos + edit.count + context, len(base))
            body = [" " + line for line in base[old_first:old_pos]]
            body += ["-" + line for line in base[old_pos : old_pos + edit.count]]
            body += ["+" + line for line in edit.lines]
            body += [" " + line for line in base[old_pos + edit.count : old_last]]
        assert previous_end is None or old_first >= previous_end, "用例里两个 hunk 的上下文叠在一起了"
        previous_end = old_last
        old_count = old_last - old_first
        added = 0 if edit.kind == "delete" else len(edit.lines)
        removed = 0 if edit.kind == "insert" else edit.count
        new_count = old_count + added - removed
        old_start = old_first + 1 if old_count > 0 else old_first
        new_start = old_first + delta + 1 if new_count > 0 else old_first + delta
        hunks.append(
            f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n" + "".join(
                line + "\n" for line in body
            )
        )
        delta += new_count - old_count
    return "".join(hunks)


def expected_anchor_lines(case: Case, path: str) -> List[int]:
    """实现应该按"从后往前"顺序碰到的行号。"""
    anchors = []
    for edit in case.edits(path):
        anchors.append(max(edit.line, 0))
    return sorted(anchors, reverse=True)


def expected_head_context(case: Case, path: str, width: Optional[int] = None) -> List[Optional[str]]:
    """每个 hunk 开头那一行（能不能当搜索锚），顺序和 `expected_anchor_lines` 一样。"""
    base = case.base_lines(path)
    width = case.context if width is None else width
    heads: List[Optional[str]] = []
    for edit in sorted(case.edits(path), key=lambda one: one.line):
        first = max((edit.line if edit.kind != "insert" else edit.line) - width, 0)
        heads.append(base[first] if first < len(base) and first < edit.line else None)
    return list(reversed(heads))


def added_lines(case: Case, path: str) -> List[str]:
    out: List[str] = []
    for edit in sorted(case.edits(path), key=lambda one: one.line):
        if edit.kind != "delete":
            out.extend(edit.lines)
        if edit.kind == "replace":
            out = out
    return out


def count_edits(case: Case, path: str, kind: str) -> int:
    return sum(1 for edit in case.edits(path) if edit.kind == kind)


# ----------------------------------------------------------------------
# 用例表
# ----------------------------------------------------------------------


def _lines(prefix: str, count: int, start: int = 1) -> List[str]:
    return [f"{prefix}{number}" for number in range(start, start + count)]


def _case(name: str, files, **kwargs) -> Case:
    return Case(name=name, files=tuple((path, tuple(lines), tuple(edits)) for path, lines, edits in files),
                **kwargs)


def build_cases() -> List[Case]:
    cases: List[Case] = []
    # 1) 文件数 2..6，每个文件 1..3 个 hunk，三类编辑混着来，行号隔得够开
    for file_count in (2, 3, 4, 5, 6):
        for hunk_count in (1, 2, 3):
            for style in ("replace", "insert", "delete", "mixed"):
                files = []
                for index in range(file_count):
                    prefix = f"f{index + 1}"
                    base = _lines(f"{prefix}-", 60)
                    edits: List[Edit] = []
                    for hunk in range(hunk_count):
                        line = 4 + hunk * 17 + index
                        if style == "replace":
                            edits.append(Edit("replace", line, 2, (f"{prefix}-new-{hunk}a", f"{prefix}-new-{hunk}b")))
                        elif style == "insert":
                            edits.append(Edit("insert", line, 0, (f"{prefix}-ins-{hunk}a", f"{prefix}-ins-{hunk}b", f"{prefix}-ins-{hunk}c")))
                        elif style == "delete":
                            edits.append(Edit("delete", line, 3))
                        else:
                            kind = ("replace", "insert", "delete")[hunk % 3]
                            if kind == "replace":
                                edits.append(Edit("replace", line, 1, (f"{prefix}-mix-{hunk}",)))
                            elif kind == "insert":
                                edits.append(Edit("insert", line, 0, (f"{prefix}-mix-{hunk}", f"{prefix}-mix-{hunk}b")))
                            else:
                                edits.append(Edit("delete", line, 2))
                    files.append((f"{prefix}.py", base, edits))
                cases.append(_case(f"{file_count}files_{hunk_count}hunks_{style}", files, context=3))
    # 2) 不带上下文（纯行号定位）
    for file_count in (2, 3, 4):
        for style in ("replace", "insert", "delete"):
            files = []
            for index in range(file_count):
                base = _lines(f"z{index}-", 30)
                if style == "replace":
                    edits = [Edit("replace", 5, 1, (f"z{index}-new",)), Edit("replace", 20, 2, (f"z{index}-n2", f"z{index}-n3"))]
                elif style == "insert":
                    edits = [Edit("insert", 0, 0, (f"z{index}-top1", f"z{index}-top2")), Edit("insert", 15, 0, (f"z{index}-mid",))]
                else:
                    edits = [Edit("delete", 3, 2), Edit("delete", 25, 1)]
                files.append((f"z{index}.py", base, edits))
            cases.append(_case(f"{file_count}files_{style}_no_context", files, context=0))
    # 3) 边界：文件最前面、最后面、整文件删空、新建
    cases.append(_case("front_and_back", [
        ("a.py", _lines("a", 12), (Edit("insert", 0, 0, ("a-top-1", "a-top-2")), Edit("insert", 12, 0, ("a-end",)))),
        ("b.py", _lines("b", 8), (Edit("insert", 0, 0, ("b-top",)), Edit("replace", 8, 1, ("b-last",)))),
    ], context=3))
    cases.append(_case("front_no_context", [
        ("a.py", _lines("a", 9), (Edit("insert", 0, 0, ("a-first", "a-second")),)),
        ("b.py", _lines("b", 9), (Edit("delete", 1, 2),)),
    ], context=0))
    cases.append(_case("whole_file_gone", [
        ("only.py", _lines("o", 6), (Edit("delete", 1, 6),)),
        ("keep.py", _lines("k", 5), (Edit("replace", 2, 1, ("k-two",)),)),
    ], deleted_files=("only.py",)))
    cases.append(_case("fresh_and_edited", [
        ("fresh.py", (), (Edit("insert", 0, 0, ("fresh-1", "fresh-2", "fresh-3")),)),
        ("edited.py", _lines("e", 7), (Edit("replace", 4, 2, ("e-four", "e-five")),)),
    ], new_files=("fresh.py",), context=3))
    cases.append(_case("fresh_no_context", [
        ("new.py", (), (Edit("insert", 0, 0, ("n1", "n2", "n3", "n4", "n5", "n6")),)),
        ("old.py", _lines("o", 6), (Edit("replace", 6, 1, ("o-last",)),)),
    ], new_files=("new.py",), context=0))
    # 4) 多行：一次插 40 行、一次删 30 行、3 行换 7 行
    cases.append(_case("bulk_insert", [
        ("big.py", _lines("b", 20), (Edit("insert", 3, 0, tuple(f"big-ins-{n}" for n in range(40))),)),
        ("side.py", _lines("s", 10), (Edit("replace", 5, 1, ("s-five",)),)),
    ]))
    cases.append(_case("bulk_delete", [
        ("big.py", _lines("b", 60), (Edit("delete", 5, 30),)),
        ("side.py", _lines("s", 12), (Edit("delete", 2, 3),)),
    ]))
    cases.append(_case("three_to_seven", [
        ("mid.py", _lines("m", 15), (Edit("replace", 4, 3, tuple(f"m-new-{n}" for n in range(7))),)),
        ("other.py", _lines("o", 9), (Edit("replace", 2, 1, ("o-two", "o-two-b")),)),
    ]))
    cases.append(_case("far_apart", [
        ("wide.py", _lines("w", 620), (Edit("replace", 10, 1, ("w-ten",)), Edit("replace", 510, 1, ("w-510",)))),
        ("narrow.py", _lines("n", 30), (Edit("delete", 1, 1), Edit("insert", 29, 0, ("n-tail",)))),
    ], context=3))
    # 5) 特殊内容
    special_cases = {
        "unicode": ("中文 与 🚀 混排", "第二行也有中文"),
        "tabs": ("\tif x:\t\tpass", "\t\treturn x"),
        "trailing_spaces": ("value = 1   ", "other = 2\t "),
        "fake_headers": ("+++ --- @@ 假头", "--- a/看起来像文件头"),
        "quotes_and_dollars": ('say "hi" $HOME `x`', "back\\slash \\n here"),
        "blank_lines": ("", "有内容"),
        "long_line": ("x" * 400, "y" * 399),
    }
    for label, (first, second) in special_cases.items():
        cases.append(_case(f"special_{label}", [
            ("alpha.py", _lines("a", 20), (Edit("replace", 3, 2, (first, second)),)),
            ("beta.py", _lines("b", 20), (Edit("insert", 4, 0, (first, second)), Edit("replace", 16, 1, (second,)))),
        ], context=3))
    # 6) 改名 + 新增 + 删除 + 修改混在一份 diff 里
    cases.append(_case("mixed_kinds", [
        ("keep.py", _lines("k", 10), (Edit("replace", 2, 1, ("k-two",)), Edit("insert", 9, 0, ("k-tail",)))),
        ("gone.py", _lines("g", 4), (Edit("delete", 1, 4),)),
        ("fresh.py", (), (Edit("insert", 0, 0, ("f1", "f2")),)),
        ("old_name.py", _lines("x", 5), ()),
    ], new_files=("fresh.py",), deleted_files=("gone.py",), renamed=(("old_name.py", "new_name.py"),), context=3))
    cases.append(_case("rename_with_edit", [
        ("moved.py", _lines("m", 9), (Edit("replace", 5, 2, ("m-five", "m-six")),)),
        ("peer.py", _lines("p", 6), (Edit("delete", 1, 1),)),
    ], renamed=(("moved.py", "moved_away.py"),), context=3))
    return cases


CASES: List[Case] = build_cases()


def _case_by_name(name: str) -> Case:
    for case in CASES:
        if case.name == name:
            return case
    raise KeyError(name)


# ======================================================================
# 假终端：只记账，不跑真程序
# ======================================================================


class FakePane:
    """可编排的终端。

    前台是什么程序由测试摆好；`send_literal` / `send_enter` / `send_tokens` 都记在账上，
    壳里敲的每一行按测试给的脚本回答（包括 `echo $?`）。
    """

    def __init__(self, *, command: str = "bash", alive: bool = True, path: str = "/w/proj"):
        self.command = command
        self.path = path
        self.alive = alive
        self.session = "remotebuddy-demo"
        self.pane_id = "%1"
        self.prompt = PROMPT
        self.line = ""
        self.pending = ""
        self.scrollback = ""
        self.script: Dict[str, Tuple[str, str]] = {}
        self.retcode = "0"
        self.enters = 0
        self.specials: List[str] = []
        self.tokens: List[List[Dict[str, str]]] = []
        self.literals: List[str] = []
        self.commands: List[str] = []
        self.killed = 0
        self.created = 0
        self.captures = 0
        self.visible_captures = 0
        self.command_sequence: List[str] = []
        self.vim_starts = True
        self.vim_quits = True
        self.answer_marker = True

    # --- 编排 ---------------------------------------------------------
    def answer(self, command: str, output: str = "", retcode: str = "0") -> "FakePane":
        self.script[command] = (output, retcode)
        return self

    def show(self, text: str) -> "FakePane":
        self.scrollback += text
        return self

    def at_shell(self) -> "FakePane":
        self.command = "bash"
        self.scrollback += self.prompt
        return self

    def enter_vim(self) -> "FakePane":
        self.command = "vim"
        return self

    # --- 传输层的那套方法 ---------------------------------------------
    def session_exists(self, _name: str) -> bool:
        return self.alive

    def pane_info(self, pane_id: str) -> Dict[str, str]:
        dead = "0" if (self.alive and pane_id == self.pane_id) else "1"
        command = self.command
        if self.command_sequence:
            command = self.command_sequence.pop(0)
        return {
            "dead": dead,
            "command": command if dead == "0" else "",
            "path": self.path,
            "cursor_x": "0",
            "cursor_y": "0",
            "pane_width": "120",
            "pane_height": "36",
        }

    def capture(self, _pane_id: str) -> str:
        self.captures += 1
        return self.scrollback

    def capture_visible(self, _pane_id: str) -> str:
        self.visible_captures += 1
        return "".join(self.scrollback.splitlines(keepends=True)[-6:])

    def send_literal(self, _pane_id: str, text: str) -> None:
        if not text or "\n" in text or "\r" in text:
            raise AssertionError(f"send_literal 只收一行文字: {text!r}")
        self.literals.append(text)
        if self.command in SHELLS:
            self.line += text
            self.scrollback += text
        else:
            self.pending += text

    def send_enter(self, _pane_id: str) -> None:
        self.enters += 1
        if self.command in SHELLS:
            line = self.line
            self.line = ""
            self.scrollback += "\n"
            self._run_line(line)
            self.scrollback += self.prompt
        else:
            self._feed_vim("\n")

    def send_keys(self, pane_id: str, text: str) -> None:
        self.send_literal(pane_id, text)
        self.send_enter(pane_id)

    def send_special(self, _pane_id: str, key: str) -> None:
        self.specials.append(key)
        if self.command not in SHELLS:
            if key == "C-c":
                self.command = "bash"
                self.scrollback += "^C\n" + self.prompt
            elif key in {"Escape", "Enter"}:
                self._feed_vim("\x1b" if key == "Escape" else "\n")

    def send_tokens(self, _pane_id: str, keys) -> None:
        self.tokens.append([dict(token) for token in keys])
        if self.command not in SHELLS:
            for token in keys:
                if token.get("kind") == "text":
                    self._feed_vim(str(token.get("value", "")))
                elif token.get("value") == "Escape":
                    self._feed_vim("\x1b")
                elif token.get("value") == "Enter":
                    self._feed_vim("\n")

    def send_workspace_command(self, _pane_id: str, command: str, path: str = "") -> None:
        self.send_keys(_pane_id, command)

    def kill_session(self, _name: str) -> None:
        self.killed += 1
        self.alive = False

    def create_workspace_session(self, _name: str, _workdir) -> str:
        self.created += 1
        self.alive = True
        self.command = "bash"
        self.scrollback += self.prompt
        return self.pane_id

    # --- 内部 ---------------------------------------------------------
    def _run_line(self, line: str) -> None:
        """壳里执行一行：命令本身已经在打字时回显过了，这里只补输出和返回码。"""
        if not line.strip():
            return
        self.commands.append(line)
        if line.startswith(f"echo {tmux_vim.RETCODE_MARKER}"):
            if self.answer_marker:
                token = line.split(tmux_vim.RETCODE_MARKER, 1)[1].split()[0]
                self.scrollback += f"{tmux_vim.RETCODE_MARKER}{token} {self.retcode}\n"
            return
        if line.startswith("vim ") or line.startswith("vim\t"):
            if self.vim_starts:
                self.command = "vim"
            return
        output, code = self.script.get(line, ("", self.retcode))
        self.retcode = code
        if output:
            self.scrollback += output + "\n"

    def _feed_vim(self, text: str) -> None:
        """记账用的假 vim：只看它有没有收到"写盘退出"，据此把前台还给 shell。

        普通模式的动作（`1G`、`dd`、`gg`……）发完就完了，只有 ex 命令和搜索会攒到回车。
        """
        if text == "\x1b":
            self.pending = ""
            return
        if text == "\n":
            command = self.pending.strip()
            self.pending = ""
            if command in {":wq", ":wq!", ":x", ":q", ":q!"} and self.vim_quits:
                self.command = "bash"
                self.scrollback += command + "\n" + self.prompt
            return
        if self.command in {"less", "more", "man"} and text == "q":
            self.command = "bash"
            self.pending = ""
            self.scrollback += self.prompt
            return
        if text.startswith(":") or text.startswith("/"):
            self.pending = text
            return
        self.pending = ""

    def opened(self) -> List[str]:
        return [command for command in self.commands if command.startswith("vim ")]


def keys_of(step: VimStep) -> str:
    return "".join(str(token.get("value", "")) for token in step.keys)


def steps_text(steps: Sequence[VimStep]) -> str:
    return " | ".join(keys_of(step) for step in steps)


def text_tokens(steps: Sequence[VimStep]) -> List[str]:
    return [
        str(token.get("value", ""))
        for step in steps
        for token in step.keys
        if token.get("kind") == "text"
    ]


def paths_in(steps: Sequence[VimStep]) -> List[str]:
    seen: List[str] = []
    for step in steps:
        if step.path and step.path not in seen:
            seen.append(step.path)
    return seen


def line_number_anchors(steps: Sequence[VimStep]) -> List[str]:
    anchors = []
    for step in steps:
        text = keys_of(step)
        if text.endswith("G") and text[:-1].isdigit():
            anchors.append(text)
    return anchors


def search_anchors(steps: Sequence[VimStep]) -> List[str]:
    return [token[1:] for token in text_tokens(steps) if token.startswith("/")]


def save_steps(steps: Sequence[VimStep]) -> List[VimStep]:
    return [step for step in steps if "wq" in keys_of(step)]


def delete_steps(steps: Sequence[VimStep]) -> List[str]:
    found = []
    for step in steps:
        for token in text_tokens([step]):
            if token.endswith("dd") and token[:-2].isdigit() or token == "dd":
                found.append(token)
    return found


# ======================================================================
# 用例挂到测试类上：每条用例一个测试方法
# ======================================================================


def install(cls, cases: Sequence[Case], builders: Dict[str, object]) -> None:
    for case in cases:
        for label, builder in builders.items():
            name = f"test_{label}_{case.name}"
            if hasattr(cls, name):
                raise AssertionError(f"重名的测试: {name}")

            def test(self, case=case, builder=builder):
                builder(self, case)

            test.__name__ = name
            test.__doc__ = f"{case.name}: {case.note or label}"
            setattr(cls, name, test)


def apply_patches_to_content(
    patches: Sequence[FilePatch], contents: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    """把读出来的补丁应用到原始内容上——这是测试自己写的一遍，用来核对读得对不对。"""
    out = {name: list(lines) for name, lines in contents.items()}
    for patch in patches:
        if not patch.hunks:
            continue
        name = patch.old_path if patch.is_rename and patch.old_path in out else patch.path
        lines = out.get(name, [])
        result: List[str] = []
        cursor = 0
        for hunk in patch.hunks:
            start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
            result.extend(lines[cursor:start])
            position = start
            for raw in hunk.lines:
                if raw.startswith("\\"):
                    continue
                prefix, text = raw[0], raw[1:]
                if prefix == " ":
                    assert lines[position] == text, f"{name} 的上下文行对不上: {text!r}"
                    result.append(text)
                    position += 1
                elif prefix == "-":
                    assert lines[position] == text, f"{name} 要删的行对不上: {text!r}"
                    position += 1
                else:
                    result.append(text)
            cursor = position
        result.extend(lines[cursor:])
        out[name] = result
    return out


def expected_anchor_plan(case: Case, path: str, width: Optional[int] = None):
    """每个 hunk 的定位方式：能按上下文行搜就（pattern, 往下走几行），否则（行号）。

    顺序和实现要求的一样：同一文件里从后往前。
    """
    lines = case.base_lines(path)
    width = case.context if width is None else width
    plan = []
    for edit in sorted(case.edits(path), key=lambda one: one.line):
        if edit.kind == "insert":
            first = max(edit.line - width, 0)
            lead = edit.line - first - 1 if edit.line > first else 0
            head = lines[first] if edit.line > first and first < len(lines) and lines[first].strip() else None
        else:
            first = max(edit.line - 1 - width, 0)
            lead = edit.line - 1 - first
            head = lines[first] if edit.line - 1 > first and first < len(lines) and lines[first].strip() else None
        if head is None:
            plan.append(("line", max(edit.line, 1), 0))
        else:
            plan.append(("search", "^" + _escape_search(head) + "$", lead))
    return list(reversed(plan))


def _escape_search(line: str) -> str:
    return "".join("\\" + character if character in "\\^$.*[]~/" else character for character in line)


def anchor_plan_of_steps(steps: Sequence[VimStep]):
    plan = []
    for step in steps:
        if step.kind != "keys":
            continue
        texts = text_tokens([step])
        pattern = next((one[1:] for one in texts if one.startswith("/")), None)
        if pattern is not None:
            moves = 0
            for one in texts:
                if one.endswith("j") and one[:-1].isdigit():
                    moves = int(one[:-1])
            plan.append(("search", pattern, moves))
            continue
        plain = keys_of(step)
        if plain.endswith("G") and plain[:-1].isdigit():
            plan.append(("line", int(plain[:-1]), 0))
    return plan


def written_line_counts(steps: Sequence[VimStep]) -> List[int]:
    """每个"进入插入模式写行"的步骤写了多少行：空行没有文字，靠 Enter 数出来。"""
    counts = []
    for step in steps:
        if step.kind != "keys":
            continue
        values = [str(token.get("value", "")) for token in step.keys]
        if not values or values[0] not in {"i", "o", "O"}:
            continue
        enters = sum(1 for value in values if value == "Enter")
        trailing_enter = len(values) >= 2 and values[-2] == "Enter"
        counts.append(enters if trailing_enter else enters + 1)
    return counts


# ======================================================================
# A 组：读 diff
# ======================================================================


class ParseUnifiedDiffTest(unittest.TestCase):
    """diff 文本读成 FilePatch：路径、种类、hunk 行号、正文，最后拿正文把原文件改一遍核对。"""


def _parse_case(self, case: Case) -> None:
    text = diff_text(case)
    patches = tmux_vim.parse_unified_diff(text)
    self.assertEqual(case.step_paths(), [patch.path for patch in patches])
    self.assertEqual(
        len([1 for _name, _base, edits in case.files if edits]),
        len([patch for patch in patches if not (patch.is_rename and not patch.hunks)]),
    )
    for patch, (name, base, edits) in zip(patches, case.files):
        self.assertEqual(name in case.new_files, patch.is_new, name)
        self.assertEqual(name in case.deleted_files, patch.is_delete, name)
        self.assertEqual(name in case.rename_map(), patch.is_rename, name)
        self.assertEqual(len(edits), len(patch.hunks), f"{name} 的 hunk 数")
        for hunk in patch.hunks:
            old_side = sum(1 for raw in hunk.lines if raw[:1] in {" ", "-"})
            new_side = sum(1 for raw in hunk.lines if raw[:1] in {" ", "+"})
            self.assertEqual(hunk.old_count, old_side, f"{name} 旧行数")
            self.assertEqual(hunk.new_count, new_side, f"{name} 新行数")
    contents = {name: list(base) for name, base, _edits in case.files}
    applied = apply_patches_to_content(patches, contents)
    for name, _base, _edits in case.files:
        self.assertEqual(case.target_lines(name), applied[name], f"{name} 应用之后的内容")


def _parse_order_case(self, case: Case) -> None:
    patches = tmux_vim.parse_unified_diff(diff_text(case))
    self.assertEqual(case.step_paths(), [patch.path for patch in patches])
    self.assertGreaterEqual(len(patches), 2, "用例必须是多文件的")
    changed = sum(
        sum(1 for raw in hunk.lines if raw[:1] in {"+", "-"})
        for patch in patches
        for hunk in patch.hunks
    )
    self.assertGreaterEqual(changed, 2, "用例必须改到两行以上")


install(ParseUnifiedDiffTest, CASES, {"parse": _parse_case, "multi": _parse_order_case})


class ParseEdgeTest(unittest.TestCase):
    """读 diff 的边角：越界路径、坏头、`\\ No newline`、git 带的元信息、引号路径。"""

    def test_empty_text_is_no_patches(self):
        self.assertEqual([], tmux_vim.parse_unified_diff(""))

    def test_metadata_lines_are_skipped(self):
        text = (
            "diff --git a/a.py b/a.py\nindex 111..222 100644\n--- a/a.py\n+++ b/a.py\n"
            "@@ -2,2 +2,2 @@\n-a2\n+A2\n a3\n"
            "diff --git a/b.py b/b.py\nnew file mode 100644\nindex 000..333\n--- /dev/null\n"
            "+++ b/b.py\n@@ -0,0 +1,2 @@\n+b1\n+b2\n"
        )
        patches = tmux_vim.parse_unified_diff(text)
        self.assertEqual(["a.py", "b.py"], [patch.path for patch in patches])
        self.assertTrue(patches[1].is_new)

    def test_no_newline_markers_are_kept(self):
        text = (
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n"
            "--- a/b.py\n+++ b/b.py\n@@ -3,1 +3,1 @@\n-x\n+y\n"
        )
        patches = tmux_vim.parse_unified_diff(text)
        markers = [raw for raw in patches[0].hunks[0].lines if raw.startswith("\\")]
        self.assertEqual(2, len(markers))

    def test_bad_hunk_header_is_an_error(self):
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff(
                "--- a/a.py\n+++ b/a.py\n@@ 乱写的 @@\n-a\n+A\n"
                "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
            )

    def test_a_context_line_that_lost_its_leading_space_is_read_as_context(self):
        """手写的 diff 常把上下文行开头那个空格丢掉；hunk 的计数能看出这里该是上下文。"""
        patches = tmux_vim.parse_unified_diff(
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n?a\n+new\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        self.assertEqual((" ?a", "+new"), patches[0].hunks[0].lines)

    def test_a_blank_line_inside_a_hunk_is_a_blank_context_line(self):
        patches = tmux_vim.parse_unified_diff(
            "--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n a1\n\n+middle\n a3\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        self.assertEqual((" a1", " ", "+middle", " a3"), patches[0].hunks[0].lines)

    def test_a_wrong_hunk_count_is_recounted_from_the_body(self):
        """手写的 diff 行数常错：按正文重数，不跟它较真（落地对不对由内容那一关说了算）。"""
        patches = tmux_vim.parse_unified_diff(
            "--- a/a.py\n+++ b/a.py\n@@ -1,99 +1,99 @@\n-a\n+A\n b\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        hunk = patches[0].hunks[0]
        self.assertEqual((2, 2), (hunk.old_count, hunk.new_count))

    def test_a_codex_style_patch_wrapper_is_understood(self):
        """`*** Begin Patch` / `*** Update File:` 这种写法换掉包裹就是标准 diff。"""
        patches = tmux_vim.parse_unified_diff(
            "*** Begin Patch\n*** Update File: pkg/store.py\n@@ -1,2 +1,2 @@\n-old\n+new\n keep\n*** End Patch\n"
        )
        self.assertEqual("pkg/store.py", patches[0].path)
        self.assertEqual(("-old", "+new", " keep"), patches[0].hunks[0].lines)
        added = tmux_vim.parse_unified_diff(
            "*** Begin Patch\n*** Add File: fresh.py\n@@ -0,0 +1,1 @@\n+one\n*** End Patch\n"
        )
        self.assertTrue(added[0].is_new)

    def test_a_new_side_header_written_with_two_minuses_is_understood(self):
        """`--- b/x.py` 是 `+++ b/x.py` 写错了：位置就摆在那儿，认它。"""
        patches = tmux_vim.parse_unified_diff(
            "--- a/a.py\n--- b/a.py\n@@ -1,2 +1,2 @@\n one\n+two\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        self.assertEqual(["a.py", "b.py"], [patch.path for patch in patches])
        self.assertEqual(("-x", "+y"), patches[1].hunks[0].lines)

    def test_prose_without_a_file_header_is_still_an_error(self):
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff(
                "把这两行改掉就行\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            )

    def test_body_longer_than_the_header_is_an_error(self):
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff(
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n+A2\n"
            )

    def test_path_escaping_the_workspace_is_an_error(self):
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff(
                "--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            )

    def test_absolute_path_is_an_error(self):
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff(
                "--- /etc/a.py\n+++ /etc/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            )

    def test_git_quoted_unicode_path_is_decoded(self):
        text = (
            'diff --git "a/\\346\\226\\207 \\344\\273\\266.py" "b/\\346\\226\\207 \\344\\273\\266.py"\n'
            '--- "a/\\346\\226\\207 \\344\\273\\266.py"\n+++ "b/\\346\\226\\207 \\344\\273\\266.py"\n'
            "@@ -1,2 +1,2 @@\n-一\n+二\n 三\n"
            "--- a/plain.py\n+++ b/plain.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        patches = tmux_vim.parse_unified_diff(text)
        self.assertEqual("文 件.py", patches[0].path)

    def test_rename_only_patch_has_no_hunks(self):
        text = (
            "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
            "diff --git a/keep.py b/keep.py\n--- a/keep.py\n+++ b/keep.py\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
        )
        patches = tmux_vim.parse_unified_diff(text)
        self.assertEqual(["new.py", "keep.py"], [patch.path for patch in patches])
        self.assertTrue(patches[0].is_rename)
        self.assertEqual(("old.py", "new.py"), (patches[0].old_path, patches[0].new_path))


# ======================================================================
# B 组：一份 diff 变成哪些 vim 动作
# ======================================================================


class VimStepsTest(unittest.TestCase):
    """按键序列：每个文件一组、从后往前、一步最多写五行、保存那一步带路径。"""


def _steps_structure_case(self, case: Case) -> None:
    steps = tmux_vim.vim_steps_for_diff(diff_text(case))
    self.assertEqual(case.step_paths(), paths_in(steps))
    for name, _base, edits in case.files:
        target = case.target_path(name)
        if not edits:
            continue  # 只改名、没有内容改动的文件不开 vim
        saves = [step for step in steps if step.path == target and "wq" in keys_of(step)]
        if name in case.deleted_files:
            self.assertEqual([], saves, f"{target} 已经删了，不该再保存")
            self.assertTrue(
                any(step.kind == "shell" and "rm" in step.command for step in steps if step.path == target),
                f"{target} 的删除要走 shell",
            )
            continue
        self.assertEqual(1, len(saves), f"{target} 应该正好保存一次")
    opens = [step for step in steps if step.kind == "shell" and " vim " in " " + step.command]
    self.assertEqual(
        [
            case.target_path(name)
            for name, _base, edits in case.files
            if edits and name not in case.deleted_files
        ],
        [step.path for step in opens],
    )
    for step in steps:
        self.assertLessEqual(
            max(written_line_counts([step]) or [0]),
            tmux_vim.MAX_LINES_PER_STEP,
            f"一步写太多行: {step.label}",
        )


def _steps_anchor_case(self, case: Case) -> None:
    steps = tmux_vim.vim_steps_for_diff(diff_text(case))
    expected = []
    for name, _base, _edits in case.files:
        if name in case.deleted_files:
            continue
        expected.extend(expected_anchor_plan(case, name))
    self.assertEqual(expected, anchor_plan_of_steps(steps))


def _steps_content_case(self, case: Case) -> None:
    steps = tmux_vim.vim_steps_for_diff(diff_text(case))
    written = [token for step in steps if step.kind == "keys" for token in text_tokens([step])]
    for name, _base, _edits in case.files:
        for line in case.edits(name):
            for text in line.lines:
                if text:
                    self.assertIn(text, written, f"{name} 里要写进去的这一行没出现在按键里: {text!r}")
    total_added = sum(
        len(edit.lines) for name, _base, _edits in case.files for edit in case.edits(name)
    )
    written_lines = sum(sum(written_line_counts([step])) for step in steps if step.kind == "keys")
    self.assertEqual(total_added, written_lines, "写进去的行数要和 diff 里加的行数一样")


install(
    VimStepsTest,
    CASES,
    {"steps": _steps_structure_case, "anchor": _steps_anchor_case, "content": _steps_content_case},
)


class VimStepShapeTest(unittest.TestCase):
    """几个具体写法的样子：改一行、插两行、删三行、四十行分八步、空行与特殊字符。"""

    def steps_for(self, text):
        return tmux_vim.vim_steps_for_diff(text)

    def two_file_diff(self, first, second):
        return "--- a/a.py\n+++ b/a.py\n" + first + "--- a/b.py\n+++ b/b.py\n" + second

    def file_steps(self, text, path):
        return [step for step in self.steps_for(text) if step.path == path]

    def values(self, step):
        return [str(token.get("value", "")) for token in step.keys]

    def test_replace_one_line_opens_above_then_deletes_the_old_line(self):
        text = self.two_file_diff(
            "@@ -2,3 +2,3 @@\n a1\n-a2\n+A2\n a3\n",
            "@@ -4,3 +4,3 @@\n b3\n-b4\n+B4\n b5\n",
        )
        steps = self.file_steps(text, "a.py")
        self.assertEqual(5, len(steps))
        self.assertEqual("vim -u NONE -N -i NONE -- a.py", steps[0].command)
        self.assertEqual([("search", "^a1$", 1)], anchor_plan_of_steps([steps[1]]))
        self.assertEqual(["O", "A2", "C-c"], self.values(steps[2]))
        self.assertEqual(["4G", "1dd"], self.values(steps[3]))
        self.assertEqual(":wq", keys_of(steps[4])[:3])

    def test_insert_two_lines_writes_them_one_line_per_step(self):
        text = self.two_file_diff(
            "@@ -2,2 +2,4 @@\n a1\n a2\n+x1\n+x2\n",
            "@@ -5,2 +5,4 @@\n b4\n b5\n+y1\n+y2\n",
        )
        steps = self.file_steps(text, "a.py")
        self.assertEqual([("search", "^a1$", 1)], anchor_plan_of_steps([steps[1]]))
        self.assertEqual(["o", "x1", "C-c"], self.values(steps[2]))
        self.assertEqual(["o", "x2", "C-c"], self.values(steps[3]))

    def test_delete_three_lines_uses_3dd(self):
        text = self.two_file_diff(
            "@@ -3,5 +3,2 @@\n a2\n-a3\n-a4\n-a5\n a6\n",
            "@@ -6,4 +6,1 @@\n b5\n-b6\n-b7\n-b8\n",
        )
        steps = self.file_steps(text, "a.py")
        self.assertEqual([("search", "^a2$", 1)], anchor_plan_of_steps([steps[1]]))
        self.assertEqual(["4G", "3dd"], self.values(steps[2]))

    def test_forty_lines_are_forty_steps_of_one_line(self):
        text = self.two_file_diff(
            "@@ -1,1 +1,41 @@\n a1\n" + "".join(f"+row {n}\n" for n in range(40)),
            "@@ -1,1 +1,2 @@\n b1\n+b1-new\n",
        )
        counts = [count for count in written_line_counts(self.steps_for(text)) if count > 1]
        self.assertEqual([], counts, "每一步只写一行")
        a_steps = self.file_steps(text, "a.py")
        writers = [
            step for step in a_steps
            if any(token.get("value") == "o" for token in step.keys)
        ]
        self.assertEqual(40, len(writers))
        self.assertEqual(40, sum(written_line_counts(a_steps)))

    def test_a_blank_added_line_writes_no_text(self):
        text = self.two_file_diff(
            "@@ -1,1 +1,4 @@\n a1\n+\n+ \n+a2\n",
            "@@ -1,1 +1,2 @@\n b1\n+\n",
        )
        a_steps = self.file_steps(text, "a.py")
        self.assertEqual(["o", "C-c"], self.values(a_steps[2]))
        self.assertEqual(["o", " ", "C-c"], self.values(a_steps[3]))
        self.assertEqual(["o", "a2", "C-c"], self.values(a_steps[4]))
        for step in self.steps_for(text):
            self.assertNotIn("", self.values(step))

    def test_regex_metacharacters_in_context_are_escaped(self):
        text = self.two_file_diff(
            "@@ -1,2 +1,2 @@\n a[0] = 1\n-a.b\n+A.B\n",
            "@@ -1,2 +1,2 @@\n b(1) = 2\n-c*d\n+C*D\n",
        )
        self.assertEqual([r"^a\[0\] = 1$", r"^b(1) = 2$"], search_anchors(self.steps_for(text)))

    def test_delete_file_is_one_shell_step(self):
        text = (
            "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
            "--- a/keep.py\n+++ b/keep.py\n@@ -1,2 +1,2 @@\n-one\n+ONE\n two\n"
        )
        steps = self.steps_for(text)
        shells = [step for step in steps if step.kind == "shell"]
        self.assertEqual(["rm -f -- gone.py", "vim -u NONE -N -i NONE -- keep.py"], [step.command for step in shells])

    def test_rename_file_is_a_shell_step_before_the_edit(self):
        text = (
            "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
            "--- a/old.py\n+++ b/new.py\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
            "--- a/peer.py\n+++ b/peer.py\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
        )
        steps = self.steps_for(text)
        self.assertEqual("mv -- old.py new.py", steps[0].command)
        self.assertEqual("new.py", steps[0].path)

    def test_new_file_is_opened_at_its_path_and_saved(self):
        text = (
            "--- /dev/null\n+++ b/pkg/fresh.py\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n"
            "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,2 @@\n-a\n+A\n b\n"
        )
        steps = self.steps_for(text)
        self.assertEqual("vim -u NONE -N -i NONE -- pkg/fresh.py", steps[0].command)
        self.assertEqual(["pkg/fresh.py", "other.py"], paths_in(steps))
        self.assertEqual(["1G"], self.values(steps[1]))
        self.assertEqual(["i", "one", "Enter", "C-c"], self.values(steps[2]))
        self.assertEqual(["i", "two", "Enter", "C-c"], self.values(steps[3]))
        self.assertEqual(["i", "three", "C-c"], self.values(steps[4]))

    def test_special_content_is_typed_verbatim(self):
        text = self.two_file_diff(
            "@@ -1,2 +1,2 @@\n-old\n+\t中文 与 🚀  \n keep\n",
            '@@ -1,2 +1,2 @@\n-old2\n+cost = $1 * [x] / 2 ^ 3\\end "q"\n keep2\n',
        )
        texts = text_tokens(self.steps_for(text))
        self.assertIn("\t中文 与 🚀  ", texts)
        self.assertIn('cost = $1 * [x] / 2 ^ 3\\end "q"', texts)

    def test_hunks_inside_one_file_run_bottom_up(self):
        text = (
            "--- a/a.py\n+++ b/a.py\n@@ -2,1 +2,1 @@\n-a2\n+A2\n"
            "@@ -40,1 +40,1 @@\n-a40\n+A40\n"
            "--- a/b.py\n+++ b/b.py\n@@ -9,1 +9,1 @@\n-b9\n+B9\n"
        )
        self.assertEqual(
            [("line", 40, 0), ("line", 2, 0)],
            anchor_plan_of_steps([step for step in self.steps_for(text) if step.path == "a.py"]),
        )

    def test_no_steps_for_a_patch_without_changes(self):
        self.assertEqual([], tmux_vim.vim_steps_for(FilePatch(old_path="a.py", new_path="a.py")))

    def test_save_step_carries_the_path_and_the_command(self):
        text = self.two_file_diff(
            "@@ -1,2 +1,2 @@\n-a\n+A\n b\n",
            "@@ -1,2 +1,2 @@\n-c\n+C\n d\n",
        )
        saves = save_steps(self.steps_for(text))
        self.assertEqual(["a.py", "b.py"], [step.path for step in saves])
        self.assertEqual([":wq", "Enter", ":wq", "Enter"], [value for step in saves for value in self.values(step)])
        self.assertEqual(["shell", "shell"], [step.expect_foreground for step in saves])


# ======================================================================
# C 组：确认终端前台是 shell
# ======================================================================


class EnsureShellTest(unittest.TestCase):
    """前台是什么程序，收拾完就该是什么样子；收拾不了就重启会话。"""

    def pane(self, command="bash", **kwargs) -> FakePane:
        pane = FakePane(command=command, **kwargs)
        pane.scrollback += pane.prompt
        return pane

    def test_shell_is_confirmed_with_one_quiet_enter(self):
        pane = self.pane()
        ready = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertEqual("enter", ready.actions[-1])
        self.assertEqual(1, pane.enters)

    def test_a_vim_is_quit_with_q_bang(self):
        pane = self.pane()
        pane.enter_vim()
        ready = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertIn("quit-vim", ready.actions)
        self.assertEqual("bash", pane.command)

    def test_a_pager_is_quit_with_q(self):
        pane = self.pane(command="less")
        ready = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertIn("quit-pager", ready.actions)
        self.assertEqual("bash", pane.command)

    def test_a_repl_is_interrupted(self):
        pane = self.pane(command="python3")
        ready = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertIn("interrupt", ready.actions)
        self.assertIn("C-c", pane.specials)

    def test_an_unknown_program_restarts_the_session(self):
        pane = self.pane(command="htop")

        def restarter():
            return pane.create_workspace_session(pane.session, "/w/proj")

        ready = tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter)
        self.assertIn("restart", ready.actions)
        self.assertEqual(1, pane.created)

    def test_an_unknown_program_without_a_restarter_fails_loudly(self):
        pane = self.pane(command="htop")
        with self.assertRaises(CommandTimeout):
            tmux_vim.ensure_shell(pane, pane.pane_id)

    def test_a_dead_pane_is_restarted(self):
        pane = self.pane()

        def restarter():
            return pane.create_workspace_session(pane.session, "/w/proj")

        pane.alive = False
        ready = tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter)
        self.assertIn("restart", ready.actions)
        self.assertTrue(pane.alive)

    def test_an_empty_foreground_name_is_given_time_to_settle(self):
        pane = self.pane()
        pane.command_sequence = ["", "", "bash"]
        ready = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertIn("settle", ready.actions)

    def test_a_vim_that_refuses_to_quit_is_restarted(self):
        pane = self.pane()
        pane.enter_vim()
        pane.vim_quits = False

        def restarter():
            return pane.create_workspace_session(pane.session, "/w/proj")

        ready = tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter, timeout=0.05)
        self.assertIn("restart", ready.actions)
        self.assertEqual(1, pane.created)

    def test_the_new_pane_id_from_a_restart_is_returned(self):
        pane = self.pane(command="htop")

        def restarter():
            pane.pane_id = "%9"
            pane.command = "bash"
            pane.alive = True
            pane.created += 1
            return "%9"

        ready = tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter)
        self.assertEqual("%9", ready.pane_id)

    def test_ensure_shell_quits_the_vim_left_over_from_the_previous_file(self):
        case = _case_by_name("3files_2hunks_mixed")
        pane = self.pane()
        pane.enter_vim()
        steps = tmux_vim.vim_steps_for_diff(diff_text(case))
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps[:2])
        self.assertEqual("vim", pane.command, "第一步是重新打开 vim")
        self.assertIn(":q!", keys_of(VimStep(keys=tuple(pane.tokens[0]))))
        self.assertTrue(pane.opened())


# ======================================================================
# D 组：`$cmd` 那条路
# ======================================================================


class RunCommandTest(unittest.TestCase):
    """跑一条命令：整屏缓存取回来，返回码靠 `echo $?` 读出来。"""

    def pane(self) -> FakePane:
        pane = FakePane()
        pane.at_shell()
        return pane

    def case(self, name="4files_2hunks_mixed") -> Case:
        return _case_by_name(name)

    def test_output_comes_from_the_whole_scrollback_not_the_viewport(self):
        pane = self.pane()
        pane.show("很远以前的一行\n" * 40)
        pane.answer("git diff --stat", " a.py | 3 +++\n b.py | 2 +-\n")
        result = tmux_vim.run_command(pane, None, pane.pane_id, "git diff --stat")
        self.assertIsInstance(result, TmuxCmdResult)
        self.assertIn("很远以前的一行", result.output_on_screen)
        self.assertIn("a.py | 3 +++", result.output_on_screen)
        self.assertEqual("0", result.retcode)

    def test_a_non_zero_retcode_is_reported(self):
        pane = self.pane()
        pane.answer("git apply --check /tmp/x.diff", "error: patch does not apply", "1")
        result = tmux_vim.run_command(pane, None, pane.pane_id, "git apply --check /tmp/x.diff")
        self.assertEqual("1", result.retcode)

    def test_a_multi_file_diff_is_kept_whole(self):
        case = self.case()
        pane = self.pane()
        pane.answer("git diff", diff_text(case))
        result = tmux_vim.run_command(pane, None, pane.pane_id, "git diff")
        for path in case.paths():
            self.assertIn(path, result.output_on_screen)

    def test_the_command_is_typed_as_one_line(self):
        pane = self.pane()
        pane.answer("git status --porcelain", " M a.py\n M b.py\n")
        tmux_vim.run_command(pane, None, pane.pane_id, "git status --porcelain")
        self.assertTrue(pane.commands[0] == "git status --porcelain")
        self.assertTrue(pane.commands[1].startswith(f"echo {tmux_vim.RETCODE_MARKER}"), pane.commands)

    def test_the_shell_is_confirmed_first(self):
        pane = self.pane()
        pane.enter_vim()
        pane.answer("pwd", "/w/proj")
        tmux_vim.run_command(pane, None, pane.pane_id, "pwd")
        self.assertEqual("bash", pane.command)
        self.assertIn("pwd", pane.commands)

    def test_an_interrupt_before_running_sends_nothing(self):
        pane = self.pane()
        pane.answer("git diff", "x")
        with self.assertRaises(Interrupted):
            tmux_vim.run_command(pane, None, pane.pane_id, "git diff", interrupt_check=lambda: True)
        self.assertNotIn("git diff", pane.commands)

    def test_an_interrupt_while_waiting_gives_up_cleanly(self):
        pane = self.pane()
        pane.answer_marker = False
        pane.answer("long build", "")
        checks = {"count": 0}

        def check():
            checks["count"] += 1
            return checks["count"] > 1  # 动手之前那一次不打断，之后开始打断

        with self.assertRaises(Interrupted):
            tmux_vim.run_command(pane, None, pane.pane_id, "long build",
                                 interrupt_check=check, timeout=5.0)
        self.assertIn("long build", pane.commands)

    def test_a_command_that_never_returns_times_out(self):
        pane = self.pane()
        pane.answer_marker = False
        pane.answer("sleep 1000", "")
        with self.assertRaises(CommandTimeout):
            tmux_vim.run_command(pane, None, pane.pane_id, "sleep 1000", timeout=0.05)

    def test_the_retcode_comes_from_the_last_marker(self):
        pane = self.pane()
        pane.answer("first", "one", "0")
        pane.answer("second", "two", "3")
        tmux_vim.run_command(pane, None, pane.pane_id, "first")
        self.assertEqual("3", tmux_vim.run_command(pane, None, pane.pane_id, "second").retcode)

    def test_each_command_leaves_a_capture(self):
        pane = self.pane()
        pane.answer("pwd", "/w/proj")
        tmux_vim.run_command(pane, None, pane.pane_id, "pwd")
        self.assertGreaterEqual(pane.captures, 1)


# ======================================================================
# E 组：一步步执行 vim 动作
# ======================================================================


class ApplyVimStepsTest(unittest.TestCase):
    """步与步之间能被打断、被问、被停下；一条 shell 命令就是一步。"""

    def case(self, name="3files_2hunks_mixed") -> Case:
        return _case_by_name(name)

    def pane(self) -> FakePane:
        pane = FakePane()
        pane.at_shell()
        return pane

    def shell_steps_of(self, pane) -> List[str]:
        return [
            command
            for command in pane.commands
            if not command.startswith(f"echo {tmux_vim.RETCODE_MARKER}")
        ]

    def test_every_step_is_its_own_send(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        executed = tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        self.assertEqual(len(steps), len(executed))
        self.assertEqual(
            [list(step.keys) for step in steps if step.kind == "keys"],
            [[dict(token) for token in keys] for keys in pane.tokens],
        )

    def test_a_capture_follows_every_step(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        self.assertGreaterEqual(pane.captures + pane.visible_captures, len(steps))

    def test_an_interrupt_without_an_ask_raises(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        with self.assertRaises(Interrupted):
            tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps, interrupt_check=lambda: True)

    def test_continue_runs_everything(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        executed = tmux_vim.apply_vim_steps(
            pane, None, pane.pane_id, steps,
            interrupt_check=lambda: True, ask=lambda question: "continue",
        )
        self.assertEqual(len(steps), len(executed))

    def test_stop_before_the_first_step_sends_nothing(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        executed = tmux_vim.apply_vim_steps(
            pane, None, pane.pane_id, steps,
            interrupt_check=lambda: True, ask=lambda question: "stop",
        )
        self.assertEqual([], executed)
        self.assertEqual([], pane.tokens)

    def test_the_actor_is_asked_with_the_step_label(self):
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(self.case()))
        asked: List[str] = []
        tmux_vim.apply_vim_steps(
            pane, None, pane.pane_id, steps,
            interrupt_check=lambda: True,
            ask=lambda question: asked.append(question) or "continue",
        )
        self.assertTrue(asked)
        self.assertIn(steps[0].path, asked[0])

    def test_stopping_half_way_leaves_the_other_files_alone(self):
        case = self.case()
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(case))
        executed = tmux_vim.apply_vim_steps(
            pane, None, pane.pane_id, steps,
            interrupt_check=lambda: len(pane.tokens) >= 2,
            ask=lambda question: "stop",
        )
        self.assertLess(len(executed), len(steps))
        self.assertEqual(1, len(pane.opened()))
        self.assertLess(len(pane.opened()), len(case.paths()))

    def test_the_shell_is_confirmed_before_each_file(self):
        case = self.case()
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(case))
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        self.assertGreaterEqual(pane.enters, len(case.paths()))

    def test_a_shell_step_is_run_in_the_shell(self):
        case = _case_by_name("mixed_kinds")
        pane = self.pane()
        steps = tmux_vim.vim_steps_for_diff(diff_text(case))
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        commands = self.shell_steps_of(pane)
        self.assertTrue(any(command.startswith("rm -f --") for command in commands), commands)
        self.assertTrue(any(command.startswith("mv --") for command in commands), commands)

    def test_a_vim_that_never_starts_is_reported(self):
        pane = self.pane()
        pane.vim_starts = False
        steps = [VimStep(command="vim -- a.py", path="a.py", expect_foreground="editor", label="a.py: 打开")]
        original = tmux_vim.FOREGROUND_TIMEOUT_SECONDS
        tmux_vim.FOREGROUND_TIMEOUT_SECONDS = 0.05
        try:
            with self.assertRaises(CommandTimeout):
                tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        finally:
            tmux_vim.FOREGROUND_TIMEOUT_SECONDS = original

    def test_no_steps_means_no_keys(self):
        pane = self.pane()
        self.assertEqual([], tmux_vim.apply_vim_steps(pane, None, pane.pane_id, []))
        self.assertEqual([], pane.tokens)


# ======================================================================
# F 组：快照、可应用性、两份 diff 是不是一回事（真 git，不开终端）
# ======================================================================


def git(*arguments: str, cwd: Path):
    return subprocess.run(
        ["git", *arguments], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )


def init_repo(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    git("init", "-q", ".", cwd=work)
    git("config", "user.email", "harness@example.com", cwd=work)
    git("config", "user.name", "harness", cwd=work)
    git("config", "core.quotePath", "false", cwd=work)


def write_case_base(case: Case, work: Path) -> None:
    for name, _lines, _edits in case.files:
        if name in case.new_files:
            continue  # 新建的文件在动手之前根本不存在
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(case.base_text(name), encoding="utf-8")


def write_case_target(case: Case, work: Path) -> None:
    for name in case.deleted_files:
        (work / name).unlink(missing_ok=True)
    for old, new in case.renamed:
        target = work / new
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(case.target_text(old), encoding="utf-8")
        (work / old).unlink(missing_ok=True)
    for name, _lines, _edits in case.files:
        if name in case.deleted_files or name in case.rename_map():
            continue
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(case.target_text(name), encoding="utf-8")


def assert_case_target(test: unittest.TestCase, case: Case, work: Path) -> None:
    for name, _lines, _edits in case.files:
        if name in case.deleted_files:
            test.assertFalse((work / name).exists(), f"{name} 应该被删掉")
            continue
        target = case.target_path(name)
        path = work / target
        test.assertTrue(path.exists(), f"{target} 应该存在")
        test.assertEqual(case.target_text(name), path.read_text(encoding="utf-8"), target)
    for name in case.new_files:
        test.assertTrue((work / name).exists(), f"{name} 应该被新建出来")


def restore_base(work: Path) -> None:
    git("checkout", "-q", "--", ".", cwd=work)
    git("clean", "-fdq", cwd=work)


def git_diff_for_case(case: Case, work: Path, *, context: int = 3) -> str:
    write_case_target(case, work)
    git("add", "-A", cwd=work)
    text = git("diff", "--cached", "-M", f"-U{context}", "--no-color", cwd=work).stdout
    git("reset", "-q", cwd=work)
    restore_base(work)
    return text


def without_index_lines(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith("index "))


def repo_workdir(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    work = Path(temporary.name) / "proj"
    init_repo(work)
    return work


def repo_with_case(test: unittest.TestCase, case: Case) -> Path:
    work = repo_workdir(test)
    write_case_base(case, work)
    git("add", "-A", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    return work


class SnapshotTest(unittest.TestCase):
    """快照必须不碰用户的索引和 HEAD，而且真的能当"动手前"的基准。"""

    def test_snapshot_leaves_the_repository_alone(self):
        case = _case_by_name("3files_1hunks_replace")
        work = repo_with_case(self, case)
        head = git("rev-parse", "HEAD", cwd=work).stdout
        before = git("status", "--porcelain", cwd=work).stdout
        snapshot = tmux_vim.snapshot_worktree(work)
        self.addCleanup(lambda: tmux_vim.cleanup_snapshot(snapshot))
        self.assertEqual(head, git("rev-parse", "HEAD", cwd=work).stdout)
        self.assertEqual(before, git("status", "--porcelain", cwd=work).stdout)
        self.assertEqual("", git("stash", "list", cwd=work).stdout)
        self.assertTrue(snapshot.commit)

    def test_the_worktree_diff_matches_what_git_records(self):
        case = _case_by_name("3files_2hunks_mixed")
        work = repo_with_case(self, case)
        snapshot = tmux_vim.snapshot_worktree(work)
        self.addCleanup(lambda: tmux_vim.cleanup_snapshot(snapshot))
        write_case_target(case, work)
        got = tmux_vim.worktree_diff(snapshot)
        git("add", "-A", cwd=work)
        recorded = git("diff", "--cached", "-M", "--no-color", cwd=work).stdout
        self.assertEqual(without_index_lines(recorded), without_index_lines(got))

    def test_untracked_files_are_part_of_the_snapshot(self):
        case = _case_by_name("3files_1hunks_replace")
        work = repo_with_case(self, case)
        (work / "extra.txt").write_text("还没提交的内容\n", encoding="utf-8")
        snapshot = tmux_vim.snapshot_worktree(work)
        self.addCleanup(lambda: tmux_vim.cleanup_snapshot(snapshot))
        (work / "extra.txt").unlink()
        diff = tmux_vim.worktree_diff(snapshot)
        self.assertIn("extra.txt", diff)
        self.assertIn("-还没提交的内容", diff)

    def test_cleanup_removes_the_temporary_index(self):
        case = _case_by_name("3files_1hunks_replace")
        work = repo_with_case(self, case)
        snapshot = tmux_vim.snapshot_worktree(work)
        self.assertTrue(snapshot.scratch.exists())
        tmux_vim.cleanup_snapshot(snapshot)
        self.assertFalse(snapshot.scratch.exists())

    def test_a_directory_that_is_not_a_repository_is_reported(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(DiffNotAppliable):
            tmux_vim.snapshot_worktree(Path(temporary.name))


def _check_case_appliable(self, case: Case) -> None:
    work = repo_with_case(self, case)
    result = tmux_vim.check_appliable(work, git_diff_for_case(case, work))
    self.assertIsInstance(result, ShellCmdResult)
    self.assertEqual("0", result.retcode, result.stderr)
    self.assertGreaterEqual(len(case.files), 2)


def _reject_edited_case(self, case: Case) -> None:
    work = repo_with_case(self, case)
    diff = git_diff_for_case(case, work)
    broken = []
    replaced = False
    for line in diff.splitlines(keepends=True):
        if line.startswith(" ") and not replaced:
            broken.append(" 完全不一样的一行\n")
            replaced = True
            continue
        broken.append(line)
    self.assertTrue(replaced, "带上下文的用例里得有上下文行")
    result = tmux_vim.check_appliable(work, "".join(broken))
    self.assertNotEqual("0", result.retcode)


install(
    SnapshotTest,
    [case for case in CASES[::7] if case.context == 3],
    {"appliable": _check_case_appliable, "reject": _reject_edited_case},
)


class DiffMatchTest(unittest.TestCase):
    """两份 diff 是不是同一件事：同样的改动换个 hunk 分法算同一件，少一行就不算。"""

    def case(self) -> Case:
        return _case_by_name("3files_2hunks_mixed")

    def test_the_same_text_matches(self):
        text = diff_text(self.case())
        self.assertTrue(tmux_vim.diffs_match(text, text))

    def test_context_lines_do_not_change_the_verdict(self):
        case = self.case()
        self.assertTrue(
            tmux_vim.diffs_match(diff_text(case, context=3), diff_text(case, context=0))
        )

    def test_another_grouping_of_the_same_change_matches(self):
        self.assertTrue(
            tmux_vim.diffs_match(
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n@@ -9,1 +9,1 @@\n-b\n+B\n"
                "--- a/b.py\n+++ b/b.py\n@@ -3,1 +3,1 @@\n-c\n+C\n",
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n"
                "--- a/a.py\n+++ b/a.py\n@@ -9,1 +9,1 @@\n-b\n+B\n"
                "--- a/b.py\n+++ b/b.py\n@@ -3,1 +3,1 @@\n-c\n+C\n",
            )
        )

    def test_a_changed_line_does_not_match(self):
        self.assertFalse(
            tmux_vim.diffs_match(
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-c\n+C\n",
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-c\n+OTHER\n",
            )
        )

    def test_a_missing_file_does_not_match(self):
        self.assertFalse(
            tmux_vim.diffs_match(
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-c\n+C\n",
                "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n",
            )
        )

    def test_an_empty_insertion_and_a_missing_insertion_differ(self):
        self.assertFalse(
            tmux_vim.diffs_match(
                "--- a/a.py\n+++ b/a.py\n@@ -1,0 +2,2 @@\n+x\n+y\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-c\n+C\n",
                "--- a/a.py\n+++ b/a.py\n@@ -1,0 +2,1 @@\n+x\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-c\n+C\n",
            )
        )


# ======================================================================
# G 组：整条路（假终端 + 真 git）
# ======================================================================


class FlowTest(unittest.TestCase):
    """假终端不会改文件，所以这一组专门看"对不上"的时候流程怎么走。"""

    def fake_pane(self, work: Path) -> FakePane:
        pane = FakePane(path=str(work))
        pane.at_shell()
        return pane

    def test_an_empty_diff_is_refused(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        with self.assertRaises(DiffError):
            tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, "")

    def test_a_diff_that_does_not_apply_sends_nothing(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        broken = diff_text(case).replace("-f1-4", "-f1-not-here")
        with self.assertRaises(DiffNotAppliable):
            tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, broken)
        self.assertEqual([], pane.tokens)
        self.assertEqual([], pane.commands)

    def test_a_stop_returns_a_non_zero_retcode_and_keeps_the_files(self):
        case = _case_by_name("3files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        outcome = tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff,
            interrupt_check=lambda: True, ask=lambda question: "stop",
        )
        self.assertEqual("130", outcome.result.retcode)
        self.assertEqual([], outcome.steps_taken)
        for name in case.paths():
            self.assertEqual(case.base_text(name), (work / name).read_text(encoding="utf-8"))

    def test_max_rounds_is_respected_and_the_files_are_named(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        outcome = tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=2, ask=lambda question: "continue"
        )
        self.assertEqual(2, outcome.rounds)
        self.assertNotEqual("0", outcome.result.retcode)
        self.assertEqual(case.paths(), outcome.mismatched_files)
        self.assertTrue(outcome.got_diff == "" or "a/" in outcome.got_diff)

    def test_the_rounds_keep_reopening_the_files(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=3, ask=lambda question: "continue"
        )
        opened = pane.opened()
        self.assertEqual(3, opened.count("vim -u NONE -N -i NONE -- f1.py"), opened)
        self.assertEqual(3, opened.count("vim -u NONE -N -i NONE -- f2.py"), opened)

    def test_steps_taken_are_recorded(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        outcome = tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=1, ask=lambda question: "continue"
        )
        self.assertTrue(outcome.steps_taken)
        self.assertEqual(len(outcome.steps_taken), sum(1 for _ in outcome.steps_taken))

    def test_the_snapshot_is_cleaned_up_even_when_it_fails(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        before = sorted(Path(tempfile.gettempdir()).glob("remotebuddy-snapshot-*"))
        tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=1, ask=lambda question: "continue"
        )
        after = sorted(Path(tempfile.gettempdir()).glob("remotebuddy-snapshot-*"))
        self.assertEqual(before, after)

    def test_an_interrupt_with_an_ask_is_questioned(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        asked: List[str] = []
        outcome = tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff,
            interrupt_check=lambda: True,
            ask=lambda question: asked.append(question) or "continue",
            max_rounds=1,
        )
        self.assertTrue(asked)
        self.assertIn("continue", asked[0])
        self.assertNotEqual("", outcome.result.retcode)

    def test_the_pane_from_a_restart_is_used_afterwards(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        pane.command = "htop"
        diff = git_diff_for_case(case, work)

        def restarter():
            pane.command = "bash"
            pane.alive = True
            pane.created += 1
            pane.scrollback += pane.prompt
            return pane.pane_id

        outcome = tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, run=None, restarter=restarter,
            max_rounds=1, ask=lambda question: "continue",
        )
        self.assertGreaterEqual(pane.created, 1)
        self.assertTrue(pane.opened())

    def test_a_new_file_diff_reaches_the_open_step(self):
        case = _case_by_name("fresh_and_edited")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=1, ask=lambda question: "continue"
        )
        self.assertTrue(any("fresh.py" in command for command in pane.opened()), pane.opened())

    def test_a_deleted_file_needs_no_vim(self):
        case = _case_by_name("whole_file_gone")
        work = repo_with_case(self, case)
        pane = self.fake_pane(work)
        diff = git_diff_for_case(case, work)
        tmux_vim.tmux_apply_diff_by_vim(
            pane, None, pane.pane_id, diff, max_rounds=1, ask=lambda question: "continue"
        )
        self.assertFalse(any("only.py" in command for command in pane.opened()), pane.opened())
        self.assertTrue(any(command.startswith("rm -f -- only.py") for command in pane.commands))


# ======================================================================
# H 组：真 tmux + 真 vim + 真 git 的验收层
# ======================================================================

from tmux_transport import TmuxTransport


def wait_for_shell(transport, pane: str, timeout: float = 10.0) -> None:
    """会话刚起来的时候前台可能是拉壳的 `env`，等它换成壳再开始。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if transport.pane_info(pane)["command"] in SHELLS:
            return
        time.sleep(0.05)


def real_cases() -> List[Case]:
    """挑一批进真终端的用例：文件少（跑得快）、种类齐，另加几个特别的。"""
    small = [case for case in CASES if len(case.files) <= 3]
    step = max(1, len(small) // 18)
    chosen = small[::step][:18]
    extra = ["mixed_kinds", "special_unicode", "special_tabs", "far_apart"]
    for name in extra:
        case = _case_by_name(name)
        if case not in chosen:
            chosen.append(case)
    return chosen


@unittest.skipUnless(shutil.which("tmux") and shutil.which("vim"), "需要真 tmux 和真 vim")
class RealTmuxTest(unittest.TestCase):
    """按键到底落到文件上没有，只有真终端说了算。"""

    def kill(self, transport, session) -> None:
        try:
            transport.kill_session(session)
        except Exception:
            pass

    def open_session(self, work: Path):
        session = f"remotubuddy-test-{uuid.uuid4().hex[:8]}"
        transport = TmuxTransport(shutil.which("tmux"))
        pane = transport.create_workspace_session(session, work)
        self.addCleanup(self.kill, transport, session)
        return transport, session, pane

    def apply(self, work: Path, diff: str, **kwargs):
        transport, _session, pane = self.open_session(work)
        kwargs.setdefault("ask", lambda question: "continue")
        return tmux_vim.tmux_apply_diff_by_vim(transport, None, pane, diff, **kwargs)

def _real_git_diff(self, case: Case) -> None:
    work = repo_with_case(self, case)
    diff = git_diff_for_case(case, work, context=3)
    self.assertNotEqual("", diff)
    outcome = self.apply(work, diff)
    self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
    self.assertEqual(1, outcome.rounds, "照着 git 自己的 diff 来，应该一轮就对上")
    assert_case_target(self, case, work)


def _real_zero_context(self, case: Case) -> None:
    work = repo_with_case(self, case)
    diff = git_diff_for_case(case, work, context=0)
    outcome = self.apply(work, diff)
    self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
    assert_case_target(self, case, work)


def _real_actor_text(self, case: Case) -> None:
    """actor 手上那份 diff 长什么样就是什么样（这里是测试自己拼的那份）。"""
    work = repo_with_case(self, case)
    diff = diff_text(case)
    outcome = self.apply(work, diff)
    self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
    assert_case_target(self, case, work)


_real = real_cases()
install(RealTmuxTest, _real, {"git": _real_git_diff})
install(RealTmuxTest, _real[::3], {"zero": _real_zero_context})
install(RealTmuxTest, _real[::2], {"actor": _real_actor_text})


class RealTmuxEdgeTest(unittest.TestCase):
    """真终端上的边角：行号不对、半路停下、会话被杀、快照收尾。"""

    def kill(self, transport, session) -> None:
        try:
            transport.kill_session(session)
        except Exception:
            pass

    def open_session(self, work: Path):
        session = f"remotubuddy-test-{uuid.uuid4().hex[:8]}"
        transport = TmuxTransport(shutil.which("tmux"))
        pane = transport.create_workspace_session(session, work)
        self.addCleanup(self.kill, transport, session)
        wait_for_shell(transport, pane)
        return transport, session, pane

    def apply(self, work: Path, diff: str, **kwargs):
        transport, _session, pane = self.open_session(work)
        kwargs.setdefault("ask", lambda question: "continue")
        return tmux_vim.tmux_apply_diff_by_vim(transport, None, pane, diff, **kwargs)

    def test_wrong_line_numbers_are_repaired_in_a_second_round(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        diff = git_diff_for_case(case, work, context=3)
        shifted = []
        for line in diff.splitlines(keepends=True):
            if line.startswith("@@"):
                head, _, tail = line.partition("@@")
                parts = tail.split()
                numbers = []
                for part in parts[:2]:
                    sign = part[0]
                    body = part[1:]
                    start, _, count = body.partition(",")
                    numbers.append(f"{sign}{int(start) + 1}{',' + count if count else ''}")
                shifted.append(f"{head}@@ {numbers[0]} {numbers[1]} @@\n")
                continue
            shifted.append(line)
        outcome = self.apply(work, "".join(shifted))
        self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
        self.assertGreaterEqual(outcome.rounds, 2, "行号错了要靠再核一轮补回来")
        self.assertEqual([], outcome.mismatched_files, "补上之后不该再留着上一轮的名字")
        assert_case_target(self, case, work)

    def test_a_stop_leaves_the_later_files_untouched(self):
        case = _case_by_name("3files_2hunks_mixed")
        work = repo_with_case(self, case)
        first = case.paths()[0]
        diff = git_diff_for_case(case, work)

        def changed_already() -> bool:
            return (work / first).read_text(encoding="utf-8") != case.base_text(first)

        outcome = self.apply(
            work, diff, interrupt_check=changed_already, ask=lambda question: "stop"
        )
        self.assertEqual("130", outcome.result.retcode)
        self.assertNotEqual(case.base_text(first), (work / first).read_text(encoding="utf-8"))
        for name in case.paths()[1:]:
            self.assertEqual(
                case.base_text(name), (work / name).read_text(encoding="utf-8"), name
            )

    def test_continue_after_a_pause_finishes_the_work(self):
        case = _case_by_name("3files_1hunks_replace")
        work = repo_with_case(self, case)
        first = case.paths()[0]
        diff = git_diff_for_case(case, work)
        asked: List[str] = []

        outcome = self.apply(
            work,
            diff,
            interrupt_check=lambda: (work / first).read_text(encoding="utf-8") != case.base_text(first),
            ask=lambda question: asked.append(question) or "continue",
        )
        self.assertTrue(asked, "停下来之前要问过 actor")
        self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
        assert_case_target(self, case, work)

    def test_a_diff_that_does_not_apply_changes_nothing(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        broken = diff_text(case).replace("-f1-4", "-f1-根本没有这一行")
        with self.assertRaises(DiffNotAppliable):
            self.apply(work, broken)
        for name in case.paths():
            self.assertEqual(case.base_text(name), (work / name).read_text(encoding="utf-8"))

    def test_ensure_shell_quits_a_real_vim(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        transport, _session, pane = self.open_session(work)
        # harness 自己敲编辑器用的是直接送键这条路（不是 actor 交上来的命令行）。
        transport.send_keys(pane, "vim -u NONE -N -i NONE -- a-does-not-matter.txt")
        for _ in range(40):
            if transport.pane_info(pane)["command"] in {"vim", "vi"}:
                break
            time.sleep(0.1)
        self.assertIn(transport.pane_info(pane)["command"], {"vim", "vi"})
        tmux_vim.ensure_shell(transport, pane)
        self.assertNotIn(transport.pane_info(pane)["command"], {"vim", "vi"})

    def test_run_command_reads_a_real_retcode(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        transport, _session, pane = self.open_session(work)
        ok = tmux_vim.run_command(transport, None, pane, "echo hi")
        self.assertEqual("0", ok.retcode)
        self.assertIn("hi", ok.output_on_screen)
        failed = tmux_vim.run_command(transport, None, pane, "sh -c 'exit 7'")
        self.assertEqual("7", failed.retcode)

    def test_run_command_keeps_the_whole_scrollback(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        transport, _session, pane = self.open_session(work)
        result = tmux_vim.run_command(transport, None, pane, "seq 1 300")
        self.assertIn("1", result.output_on_screen)
        self.assertIn("300", result.output_on_screen)

    def test_a_killed_session_is_revived_by_the_restarter(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        transport, session, pane = self.open_session(work)
        transport.kill_session(session)
        revived = {}

        def restarter() -> str:
            revived["pane"] = transport.create_workspace_session(session, work)
            return revived["pane"]

        result = tmux_vim.run_command(transport, None, pane, "pwd", restarter=restarter)
        self.assertIn("pane", revived)
        self.assertEqual("0", result.retcode)
        self.assertIn(work.name, result.output_on_screen)

    def test_no_temporary_snapshot_directories_are_left_behind(self):
        case = _case_by_name("2files_1hunks_replace")
        work = repo_with_case(self, case)
        diff = git_diff_for_case(case, work)
        # 只看这个模块自己开的那三种临时目录：别的进程（跑着的会议、backend 的临时目录）
        # 也在同一个临时目录下过日子，把它们算进来就成了并发噪声。
        prefixes = ("remotebuddy-snapshot-*", "remotebuddy-expected-*", "remotebuddy-current-*")

        def leftovers():
            found = []
            for pattern in prefixes:
                found.extend(Path(tempfile.gettempdir()).glob(pattern))
            return sorted(found)

        before = leftovers()
        self.apply(work, diff)
        self.assertEqual(before, leftovers())

    def test_a_rename_in_a_real_repository_lands(self):
        work = repo_workdir(self)
        (work / "before.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
        (work / "peer.py").write_text("a\nb\nc\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-qm", "base", cwd=work)
        git("mv", "before.py", "after.py", cwd=work)
        (work / "after.py").write_text("one\nTWO\nthree\n", encoding="utf-8")
        (work / "peer.py").write_text("a\nB\nc\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        diff = git("diff", "--cached", "-M", "--no-color", cwd=work).stdout
        git("reset", "-q", cwd=work)
        restore_base(work)
        self.assertIn("rename from before.py", diff)
        outcome = self.apply(work, diff)
        self.assertEqual("0", outcome.result.retcode, outcome.got_diff[-3000:])
        self.assertFalse((work / "before.py").exists())
        self.assertEqual("one\nTWO\nthree\n", (work / "after.py").read_text(encoding="utf-8"))
        self.assertEqual("a\nB\nc\n", (work / "peer.py").read_text(encoding="utf-8"))


# ======================================================================
# 用例表本身：每条都得是多文件、多行的改动
# ======================================================================


class MultiFileMultiLineGuardTest(unittest.TestCase):
    """用例表长歪了（混进单文件或只改一行的用例）就报出来。"""

    def test_every_case_touches_at_least_two_files(self):
        for case in CASES:
            patches = tmux_vim.parse_unified_diff(diff_text(case))
            paths = {patch.path for patch in patches}
            self.assertGreaterEqual(len(paths), 2, case.name)

    def test_every_case_changes_at_least_two_lines(self):
        for case in CASES:
            patches = tmux_vim.parse_unified_diff(diff_text(case))
            changed = sum(
                1
                for patch in patches
                for hunk in patch.hunks
                for raw in hunk.lines
                if raw[:1] in {"+", "-"}
            )
            self.assertGreaterEqual(changed, 2, case.name)

    def test_case_names_are_unique(self):
        names = [case.name for case in CASES]
        self.assertEqual(len(names), len(set(names)))

    def test_the_cases_cover_every_kind_of_change(self):
        kinds = set()
        for case in CASES:
            if case.new_files:
                kinds.add("new")
            if case.deleted_files:
                kinds.add("delete")
            if case.renamed:
                kinds.add("rename")
            if any(edit.kind == "insert" for _n, _l, edits in case.files for edit in edits):
                kinds.add("insert")
            if any(edit.kind == "delete" for _n, _l, edits in case.files for edit in edits):
                kinds.add("drop")
            if any(edit.kind == "replace" for _n, _l, edits in case.files for edit in edits):
                kinds.add("replace")
        self.assertEqual({"new", "delete", "rename", "insert", "drop", "replace"}, kinds)

    def test_multiple_hunks_per_file_are_covered(self):
        for case in CASES:
            for name, _lines, edits in case.files:
                if len(edits) >= 3:
                    return
        self.fail("用例表里得有一份文件带三个以上 hunk 的用例")

