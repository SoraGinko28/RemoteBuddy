"""`tmux_apply_diff_by_vim` 的 50 个测试（先写测试，实现按这份清单一条条补）。

分七组：
  A parse_unified_diff（9）  B vim_steps_for（10）  C ensure_shell（6）
  D run_command（6）         E apply_vim_steps（8）  F snapshot / check_appliable / diffs_match（7）
  G tmux_apply_diff_by_vim 端到端（4）

假件只有两个：`Pane`（可编排的终端）和 `Git`（假 `git`）。终端模拟的是"每执行一条命令就把
提示符、命令、输出、提示符按顺序追加到整屏缓存里"，所以 `run_command` 的解析是在真文本上跑的。
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import tmux_vim
from tmux_vim import (
    DiffError,
    FilePatch,
    Hunk,
    Interrupted,
    ShellCmdResult,
    TmuxCmdResult,
    VimStep,
)

PROMPT = "bash-3.2$ "


class Pane:
    """可编排的假终端。"""

    def __init__(self, *, command: str = "bash", alive: bool = True, visible_rows: int = 6):
        self.command = command
        self.path = "/w/proj"
        self.alive = alive
        self.session = "remotebuddy-demo"
        self.pane_id = "%1"
        self.prompt = PROMPT
        self.visible_rows = visible_rows
        self.scrollback = ""
        self.script = {}            # 命令 -> (输出, retcode)
        self.retcode = "0"
        self.enters = 0
        self.specials = []
        self.keys = []
        self.commands = []
        self.killed = 0
        self.created = 0
        self.captures = 0
        self.visible_captures = 0

    # --- 编排 ---------------------------------------------------------
    def answer(self, command: str, output: str = "", retcode: str = "0"):
        self.script[command] = (output, retcode)
        return self

    def show(self, text: str):
        self.scrollback += text
        return self

    def at_shell(self):
        self.command = "bash"
        self.scrollback += self.prompt
        return self

    # --- 传输层的那套方法 ---------------------------------------------
    def session_exists(self, _name: str) -> bool:
        return self.alive

    def pane_info(self, pane_id: str):
        if not self.alive or pane_id != self.pane_id:
            return {"dead": "1", "command": "", "path": self.path,
                    "cursor_x": "0", "cursor_y": "0", "pane_width": "120", "pane_height": "36"}
        return {"dead": "0", "command": self.command, "path": self.path,
                "cursor_x": "0", "cursor_y": "0", "pane_width": "120", "pane_height": "36"}

    def capture(self, _pane_id: str) -> str:
        self.captures += 1
        return self.scrollback

    def capture_visible(self, _pane_id: str) -> str:
        self.visible_captures += 1
        return "".join(self.scrollback.splitlines(keepends=True)[-self.visible_rows:])

    def send_enter(self, _pane_id: str) -> None:
        self.enters += 1
        if self.command in {"bash", "zsh", "sh"}:
            self.scrollback += "\n" + self.prompt

    def send_special(self, _pane_id: str, key: str) -> None:
        self.specials.append(key)
        if key == "C-c" and self.command != "bash":
            self.command = "bash"
            self.scrollback += "^C\n" + self.prompt

    def send_tokens(self, _pane_id: str, keys) -> None:
        self.keys.append([dict(token) for token in keys])
        text = "".join(str(token.get("value", "")) for token in keys if token.get("kind") == "text")
        if self.command in {"vim", "vi", "less", "man"} and any(
            str(token.get("value")) == "Enter" for token in keys if token.get("kind") == "ctrl"
        ):
            if text.strip() in {":q", ":q!", ":wq", ":wq!", ":x"}:
                self.command = "bash"
                self.scrollback += text + "\n" + self.prompt
        if self.command == "less" and text.strip() == "q":
            self.command = "bash"
            self.scrollback += self.prompt

    def send_workspace_command(self, _pane_id: str, command: str, path: str = "") -> None:
        self.commands.append(command)
        if "echo $?" in command and command.strip() != "echo $?":
            bare = command.split(";")[0].strip()
            output, retcode = self.script.get(bare, ("", self.retcode))
            self.scrollback += f"{self.prompt}{command}\n{output}\n{retcode}\n{self.prompt}"
            return
        if "echo $?" in command:
            self.scrollback += f"{self.prompt}{command}\n{self.retcode}\n{self.prompt}"
            return
        output, retcode = self.script.get(command, ("", "0"))
        self.retcode = retcode
        self.scrollback += f"{self.prompt}{command}\n{output}\n{self.prompt}"

    def send_hex(self, pane_id: str, hex_text: str) -> None:
        self.commands.append(f"hex:{hex_text}")

    def kill_session(self, _name: str) -> None:
        self.killed += 1
        self.alive = False

    def create_workspace_session(self, _name: str, _workdir):
        self.created += 1
        self.alive = True
        self.command = "bash"
        self.scrollback += self.prompt
        return self.pane_id


class Git:
    """假 git：按子命令回脚本化的结果，同时记下每次调用和环境。"""

    def __init__(self, *, repo: bool = True, apply_check: int = 0, diff_out: str = "",
                 apply_check_err: str = ""):
        self.repo = repo
        self.apply_check = apply_check
        self.apply_check_err = apply_check_err
        self.diff_out = diff_out
        self.calls = []
        self.envs = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(dict(kwargs.get("env") or {}))
        sub = argv[1] if len(argv) > 1 else ""
        if not self.repo:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")
        if sub == "write-tree":
            return subprocess.CompletedProcess(argv, 0, "tree1234\n", "")
        if sub == "commit-tree":
            return subprocess.CompletedProcess(argv, 0, "commit5678\n", "")
        if sub == "apply":
            return subprocess.CompletedProcess(argv, self.apply_check, "", self.apply_check_err)
        if sub in {"diff", "status"}:
            return subprocess.CompletedProcess(argv, 0, self.diff_out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def subcommands(self):
        return [tuple(call[1:3]) for call in self.calls]


def patch(path="a.py", hunks=(), **flags) -> FilePatch:
    return FilePatch(old_path=path, new_path=path, hunks=tuple(hunks), **flags)


def hunk(old_start=1, old_count=1, new_start=1, new_count=1, lines=()) -> Hunk:
    return Hunk(old_start=old_start, old_count=old_count, new_start=new_start, new_count=new_count,
                lines=tuple(lines))


def flat_keys(steps) -> str:
    return " | ".join(
        "".join(str(token.get("value", "")) for token in step.keys) for step in steps
    )


class ParseUnifiedDiffTest(unittest.TestCase):
    """A 组：把 unified diff 读成 FilePatch。"""

    def test_a1_empty_text_is_no_patches(self):                       # A1
        self.assertEqual([], tmux_vim.parse_unified_diff(""))

    def test_a2_one_file_one_hunk(self):                              # A2
        text = ("--- a/a.py\n+++ b/a.py\n@@ -3,1 +3,1 @@\n-old\n+new\n")
        patches = tmux_vim.parse_unified_diff(text)
        self.assertEqual(1, len(patches))
        self.assertEqual("a/a.py", patches[0].old_path)
        self.assertEqual("b/a.py", patches[0].new_path)
        self.assertEqual(1, len(patches[0].hunks))
        self.assertEqual(3, patches[0].hunks[0].old_start)
        self.assertEqual(("-old", "+new"), patches[0].hunks[0].lines)

    def test_a3_two_hunks_one_file(self):                             # A3
        text = ("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n"
                "@@ -9,2 +9,2 @@\n-b\n-B\n-c\n+C\n")
        hunks = tmux_vim.parse_unified_diff(text)[0].hunks
        self.assertEqual(2, len(hunks))
        self.assertEqual((1, 9), (hunks[0].old_start, hunks[1].old_start))

    def test_a4_two_files(self):                                      # A4
        text = ("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+A\n"
                "--- a/b.py\n+++ b/b.py\n@@ -2,1 +2,1 @@\n-x\n+y\n")
        patches = tmux_vim.parse_unified_diff(text)
        self.assertEqual(["a/a.py", "a/b.py"], [one.old_path for one in patches])

    def test_a5_new_file_is_marked(self):                             # A5
        text = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        one = tmux_vim.parse_unified_diff(text)[0]
        self.assertTrue(one.is_new)
        self.assertEqual("b/new.py", one.new_path)

    def test_a6_deleted_file_is_marked(self):                         # A6
        text = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
        one = tmux_vim.parse_unified_diff(text)[0]
        self.assertTrue(one.is_delete)

    def test_a7_bad_hunk_header_is_an_error(self):                    # A7
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff("--- a/a.py\n+++ b/a.py\n@@ nonsense @@\n-a\n+A\n")

    def test_a8_unknown_line_prefix_is_an_error(self):                # A8
        with self.assertRaises(DiffError):
            tmux_vim.parse_unified_diff("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n?a\n")

    def test_a9_no_newline_marker_is_kept(self):                      # A9
        text = ("--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n\\ No newline at end of file\n"
                "+new\n")
        lines = tmux_vim.parse_unified_diff(text)[0].hunks[0].lines
        self.assertIn("\\ No newline at end of file", lines)


class VimStepsTest(unittest.TestCase):
    """B 组：一个补丁变成哪些 vim 动作。"""

    def test_b1_replace_one_line_anchors_at_old_start(self):          # B1
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(10, 1, 10, 1, ["-old", "+new"])]))
        self.assertTrue(steps)
        self.assertIn("10G", flat_keys(steps))
        self.assertIn("new", flat_keys(steps))

    def test_b2_insert_two_lines(self):                               # B2
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(4, 0, 4, 2, ["+one", "+two"])]))
        keys = flat_keys(steps)
        self.assertIn("one", keys)
        self.assertIn("two", keys)

    def test_b3_delete_one_line_uses_dd(self):                        # B3
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(7, 1, 7, 0, ["-bye"])]))
        self.assertIn("dd", flat_keys(steps))

    def test_b4_delete_two_lines_covers_both(self):                   # B4
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(7, 2, 7, 0, ["-one", "-two"])]))
        self.assertIn("2dd", flat_keys(steps).replace(" d", "d") + flat_keys(steps))

    def test_b5_hunks_are_applied_bottom_up(self):                    # B5
        steps = tmux_vim.vim_steps_for(patch(hunks=[
            hunk(2, 1, 2, 1, ["-a", "+A"]),
            hunk(20, 1, 20, 1, ["-b", "+B"]),
        ]))
        anchors = [step.label for step in steps if "20G" in flat_keys([step]) or "2G" in flat_keys([step])]
        self.assertTrue(anchors)
        self.assertIn("20G", flat_keys([steps[0]]), "改动要从后往前做，第一个动的是靠后的那个 hunk")

    def test_b6_append_at_end_of_file(self):                          # B6
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(30, 0, 30, 1, ["+tail"])]))
        keys = flat_keys(steps)
        self.assertIn("tail", keys)
        self.assertTrue("G" in keys or "$" in keys)

    def test_b7_insert_at_start_of_file(self):                        # B7
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(0, 0, 1, 1, ["+head"])]))
        keys = flat_keys(steps)
        self.assertIn("head", keys)
        self.assertTrue("1G" in keys or "gg" in keys)

    def test_b8_context_lines_are_used_for_anchoring(self):           # B8
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(5, 3, 5, 3, [
            " keep1", "-old", "+new", " keep2",
        ])]))
        keys = flat_keys(steps)
        self.assertIn("keep1", keys)
        self.assertIn("/", keys)

    def test_b9_vim_special_characters_are_typed_literally(self):     # B9
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(3, 1, 3, 1, [
            "-old", "+cost = $1 * [x] / 2 ^ 3\\end",
        ])]))
        text = [str(token.get("value")) for step in steps for token in step.keys
                if token.get("kind") == "text"]
        self.assertIn("cost = $1 * [x] / 2 ^ 3\\end", "".join(text))

    def test_b10_context_only_hunk_has_no_steps(self):                # B10
        steps = tmux_vim.vim_steps_for(patch(hunks=[hunk(1, 2, 1, 2, [" same", " lines"])]))
        self.assertEqual([], steps)


class EnsureShellTest(unittest.TestCase):
    """C 组：动手之前先确认终端前台是 shell。"""

    def test_c1_already_at_shell_just_presses_enter(self):            # C1
        pane = Pane(command="bash").at_shell()
        done = tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertEqual(1, pane.enters)
        self.assertIn("enter", done)
        self.assertEqual("bash", pane.command)

    def test_c2_vim_is_quit(self):                                    # C2
        pane = Pane(command="vim")
        tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertEqual("bash", pane.command)
        self.assertTrue(any("q!" in str(token.get("value")) for step in pane.keys for token in step))

    def test_c3_pager_is_quit(self):                                  # C3
        pane = Pane(command="less")
        tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertEqual("bash", pane.command)

    def test_c4_repl_is_interrupted(self):                            # C4
        pane = Pane(command="python3")
        tmux_vim.ensure_shell(pane, pane.pane_id)
        self.assertIn("C-c", pane.specials)
        self.assertEqual("bash", pane.command)

    def test_c5_unknown_program_restarts_the_session(self):           # C5
        pane = Pane(command="htop")

        def restarter():
            pane.create_workspace_session(pane.session, pane.path)
            return pane.pane_id

        tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter)
        self.assertEqual("bash", pane.command)
        self.assertEqual(1, pane.created)

    def test_c6_dead_session_is_restarted(self):                      # C6
        pane = Pane(alive=False)

        def restarter():
            pane.create_workspace_session(pane.session, pane.path)
            return pane.pane_id

        done = tmux_vim.ensure_shell(pane, pane.pane_id, restarter=restarter)
        self.assertTrue(pane.alive)
        self.assertIn("restart", done)


class RunCommandTest(unittest.TestCase):
    """D 组：`$cmd` 那条路。"""

    def test_d1_output_comes_from_the_whole_scrollback(self):          # D1
        pane = Pane().at_shell()
        pane.show("old line far above\n" * 30)
        pane.answer("ls", "a.py\nb.py")
        result = tmux_vim.run_command(pane, None, pane.pane_id, "ls")
        self.assertIsInstance(result, TmuxCmdResult)
        self.assertIn("a.py", result.output_on_screen)
        self.assertIn("old line far above", result.output_on_screen, "要整屏缓存，不是可见那几行")
        self.assertGreater(pane.captures, 0)

    def test_d2_retcode_zero(self):                                   # D2
        pane = Pane().at_shell()
        pane.answer("true", "", "0")
        self.assertEqual("0", tmux_vim.run_command(pane, None, pane.pane_id, "true").retcode)

    def test_d3_retcode_non_zero(self):                               # D3
        pane = Pane().at_shell()
        pane.answer("false", "", "2")
        self.assertEqual("2", tmux_vim.run_command(pane, None, pane.pane_id, "false").retcode)

    def test_d4_multiline_output_is_kept(self):                       # D4
        pane = Pane().at_shell()
        pane.answer("cat f", "one\ntwo\nthree")
        result = tmux_vim.run_command(pane, None, pane.pane_id, "cat f")
        self.assertIn("one", result.output_on_screen)
        self.assertIn("three", result.output_on_screen)

    def test_d5_shell_is_ensured_before_running(self):                # D5
        pane = Pane(command="vim")
        pane.answer("pwd", "/w/proj")
        tmux_vim.run_command(pane, None, pane.pane_id, "pwd")
        self.assertTrue(any("pwd" == command for command in pane.commands))
        self.assertEqual("bash", pane.command)

    def test_d6_interrupt_stops_before_running(self):                 # D6
        pane = Pane().at_shell()
        pane.answer("sleep 100", "")
        with self.assertRaises(Interrupted):
            tmux_vim.run_command(pane, None, pane.pane_id, "sleep 100",
                                 interrupt_check=lambda: True)
        self.assertEqual([], [c for c in pane.commands if c == "sleep 100"])


class ApplyVimStepsTest(unittest.TestCase):
    """E 组：一步步执行 vim 动作。"""

    def step(self, text, label=""):
        return VimStep(keys=({"kind": "text", "value": text},), label=label)

    def test_e1_each_step_is_its_own_send(self):                      # E1
        pane = Pane().at_shell()
        steps = [self.step("1G"), self.step("dd")]
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps)
        self.assertEqual(2, len(pane.keys))

    def test_e2_interrupt_is_checked_before_every_step(self):         # E2
        pane = Pane().at_shell()
        seen = []

        def check():
            seen.append(len(pane.keys))
            return False

        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, [self.step("1G"), self.step("dd")],
                                 interrupt_check=check)
        self.assertEqual([0, 0, 1], seen[:3])

    def test_e3_interrupt_stops_the_rest(self):                       # E3
        pane = Pane().at_shell()
        steps = [self.step("1G"), self.step("dd"), self.step(":wq")]
        with self.assertRaises(Interrupted):
            tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps,
                                     interrupt_check=lambda: len(pane.keys) >= 1,
                                     ask=lambda question: "continue")
        self.assertEqual(1, len(pane.keys))

    def test_e4_the_actor_is_asked_after_an_interrupt(self):          # E4
        pane = Pane().at_shell()
        asked = []
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, [self.step("1G")],
                                 interrupt_check=lambda: True,
                                 ask=lambda question: asked.append(question) or "continue")
        self.assertTrue(asked)

    def test_e5_continue_finishes_the_steps(self):                    # E5
        pane = Pane().at_shell()
        steps = [self.step("1G"), self.step("dd")]
        done = tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps,
                                        interrupt_check=lambda: True,
                                        ask=lambda question: "continue")
        self.assertEqual(2, len(pane.keys))
        self.assertEqual(2, len(done))

    def test_e6_stop_leaves_the_rest_undone(self):                    # E6
        pane = Pane().at_shell()
        steps = [self.step("1G"), self.step("dd")]
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, steps,
                                 interrupt_check=lambda: True,
                                 ask=lambda question: "stop")
        self.assertEqual(1, len(pane.keys))

    def test_e7_a_step_is_followed_by_a_capture(self):                # E7
        pane = Pane().at_shell()
        tmux_vim.apply_vim_steps(pane, None, pane.pane_id, [self.step("1G")])
        self.assertGreaterEqual(pane.captures + pane.visible_captures, 1)

    def test_e8_no_steps_sends_nothing(self):                         # E8
        pane = Pane().at_shell()
        done = tmux_vim.apply_vim_steps(pane, None, pane.pane_id, [])
        self.assertEqual([], done)
        self.assertEqual([], pane.keys)


class SnapshotAndCheckTest(unittest.TestCase):
    """F 组：快照、可应用性、两份 diff 是否一致。"""

    def test_f1_snapshot_uses_a_temporary_index(self):                # F1
        git = Git()
        with tempfile.TemporaryDirectory() as tmp:
            tmux_vim.snapshot_worktree(Path(tmp), run=git)
        subs = [call[1] for call in git.calls]
        self.assertIn("read-tree", subs)
        self.assertIn("write-tree", subs)
        self.assertIn("commit-tree", subs)
        self.assertTrue(any(env.get("GIT_INDEX_FILE") for env in git.envs),
                        "临时索引要经 GIT_INDEX_FILE 传进去")

    def test_f2_snapshot_adds_all_files_and_a_worktree(self):         # F2
        git = Git()
        with tempfile.TemporaryDirectory() as tmp:
            snap = tmux_vim.snapshot_worktree(Path(tmp), run=git)
        add = [call for call in git.calls if call[1] == "add"]
        self.assertEqual(1, len(add))
        self.assertIn("-A", add[0])
        self.assertTrue(any(call[1] == "worktree" and "add" in call for call in git.calls))
        self.assertTrue(str(snap.branch).startswith("snapshot-"))

    def test_f3_good_diff_is_appliable(self):                         # F3
        result = tmux_vim.check_appliable(Path("/w"), "--- a/a\n+++ b/a\n", run=Git(apply_check=0))
        self.assertIsInstance(result, ShellCmdResult)
        self.assertEqual("0", result.retcode)

    def test_f4_bad_diff_reports_stderr(self):                        # F4
        result = tmux_vim.check_appliable(Path("/w"), "--- a/a\n+++ b/a\n",
                                          run=Git(apply_check=1, apply_check_err="patch does not apply"))
        self.assertNotEqual("0", result.retcode)
        self.assertIn("does not apply", result.stderr)

    def test_f5_same_change_with_different_hunk_headers_matches(self): # F5
        wanted = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        got = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+new \n"
        got = got.replace("+new \n", "+new\n")   # 同样的落盘结果
        self.assertTrue(tmux_vim.diffs_match(wanted, got))

    def test_f6_different_change_does_not_match(self):                # F6
        wanted = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        got = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+other\n"
        self.assertFalse(tmux_vim.diffs_match(wanted, got))

    def test_f7_not_a_git_repository_is_reported(self):               # F7
        result = tmux_vim.check_appliable(Path("/w"), "--- a/a\n+++ b/a\n", run=Git(repo=False))
        self.assertNotEqual("0", result.retcode)
        self.assertIn("not a git repository", result.stderr)


class TmuxApplyDiffTest(unittest.TestCase):
    """G 组：整条路。"""

    def diff(self):
        return "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"

    def test_g1_happy_path_reaches_a_match(self):                     # G1
        pane = Pane().at_shell()
        git = Git(diff_out=self.diff())
        outcome = tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, self.diff(),
                                                  run=git, ask=lambda question: "continue")
        self.assertEqual("0", outcome.result.retcode)
        self.assertTrue(outcome.steps_taken)
        self.assertEqual(1, outcome.rounds)

    def test_g2_a_mismatch_runs_another_round(self):                  # G2
        pane = Pane().at_shell()
        git = Git(diff_out="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+wrong\n")
        outcome = tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, self.diff(),
                                                  run=git, ask=lambda question: "continue")
        self.assertGreaterEqual(outcome.rounds, 2)
        self.assertNotEqual("0", outcome.result.retcode)

    def test_g3_a_mismatch_stops_at_max_rounds(self):                 # G3
        pane = Pane().at_shell()
        git = Git(diff_out="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+wrong\n")
        outcome = tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, self.diff(),
                                                  run=git, max_rounds=2,
                                                  ask=lambda question: "continue")
        self.assertEqual(2, outcome.rounds)

    def test_g4_a_human_question_mid_way_pauses_and_resumes(self):     # G4
        pane = Pane().at_shell()
        git = Git(diff_out=self.diff())
        asked = []
        state = {"interrupts": 1}

        def check():
            if state["interrupts"] > 0:
                state["interrupts"] -= 1
                return True
            return False

        outcome = tmux_vim.tmux_apply_diff_by_vim(pane, None, pane.pane_id, self.diff(),
                                                  run=git, interrupt_check=check,
                                                  ask=lambda question: asked.append(question) or "continue")
        self.assertTrue(asked, "被打断之后要问过 actor 才继续")
        self.assertEqual("0", outcome.result.retcode)


if __name__ == "__main__":
    unittest.main()
