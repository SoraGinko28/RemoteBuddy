"""The second-order critic's rules, run over a real meeting.

The demo is ``tests/critic_run.py``; what it runs is product code — the rules
(`critic/function_length_le_15.viba`, `critic/parameter_count_le_4.viba`), the
host (`underlying_viba_ops.py`) and the judgment (`critic_rules.py`), the same
ones the real critic runs before it speaks. The ``critic`` who speaks *here* is
scripted: the demo writes that chat message itself. Each rule judges every
moment the critic hands in — the case is material the critic writes into the
store, not source baked into the rule — and these cases pin the verdicts, the
complaint, the replay, the way the file gets written, and the two constraints on
the host.
"""

from __future__ import annotations

import ast
from pathlib import Path

import critic_rules
import meeting_adt as adt
import meeting_loader
import meeting_v2 as m2
import underlying_viba_ops as ops
from tests import critic_run
from viba.type import Ok


def _meeting(tmp_path: Path, name: str):
    """A meeting with four captures: a long function, a wide one, a folded one,
    and a clean one.

    No terminal: the frames are written the way a capture would be, which is
    what the design's screen asserts are about. `critic_run.py` starts a live
    session for the workspace page.
    """
    store, captures, _transport = critic_run.start_meeting(tmp_path, f"test-{name}",
                                                           live=False)
    return store, captures


def _judge(tmp_path: Path, store, capture, store_dir: Path = None,
           rule: Path = critic_run.RULE):
    screen, region = capture
    return critic_run.judge(store, screen, region, store_dir or tmp_path / "store", rule)


def test_each_rule_judges_every_moment_the_critic_hands_in(tmp_path) -> None:
    """The rules never change; the case does. Both rules judge all three
    versions, and they object to different ones: the length rule to the long
    function only, the parameter rule to the wide one only."""
    store, captures = _meeting(tmp_path, "every-moment")

    def judged(rule, name):
        return [_judge(tmp_path, store, capture, tmp_path / f"{name}-{index}", rule)
                for index, capture in enumerate(captures)]

    length = judged(critic_run.RULE, "length")
    params = judged(critic_run.PARAMETER_RULE, "params")
    folded = judged(critic_run.FOLDED_RULE, "folded")

    assert [result["verdict"].ok_value for result in length] == [False, True, True, True]
    assert [result["verdict"].ok_value for result in params] == [True, False, True, True]
    assert [result["verdict"].ok_value for result in folded] == [True, True, False, True]

    assert "process_batch" in length[0]["complaint"]
    assert "16 行" in length[0]["complaint"]      # the function's real length
    assert "keep_row" in params[1]["complaint"]
    assert "5 个参数" in params[1]["complaint"]   # its real parameter count
    assert "process_batch" in folded[2]["complaint"]
    assert "2 条语句" in folded[2]["complaint"]   # what that line folded
    assert all(result["complaint"] is None
               for result in (length[1], length[2], length[3],
                              params[0], params[2], params[3],
                              folded[0], folded[1], folded[3]))


def test_the_complaint_lands_in_the_meeting_chat_with_the_pane_it_read(tmp_path) -> None:
    store, captures = _meeting(tmp_path, "complaint")
    result = _judge(tmp_path, store, captures[0])

    chats = [event for event in store.events() if event.get("kind") == "chat"]
    complaint = chats[-1]
    assert complaint["actor"] == "critic"
    assert complaint["content"] == result["complaint"]
    reference = complaint["data"]["references"][0]
    assert reference["kind"] == "screen"
    assert reference["event_id"] == captures[0][0]["event_id"]
    # the short moment was judged as well, and the critic stayed quiet about it
    assert [event["actor"] for event in chats].count("critic") == 1


def test_the_capture_is_prepared_the_way_the_design_demands(tmp_path) -> None:
    """The frame's parameters: segments match the text, rows == height, widths
    fit, the cursor is inside, no segment is empty — and the cited region is the
    code inside that pane."""
    store, captures = _meeting(tmp_path, "capture")
    data, _summary = meeting_loader.load_meeting_data_from_run(store.run_dir)
    screens = [chunk for chunk in data.sorted_stream_chunks
               if isinstance(chunk, adt.ScreenStreamChunk)][-1].chunks
    assert len(screens) == 4
    for capture in screens:
        adt.assert_screen_plain_matches(capture)
        adt.assert_screen_rows_match_height(capture)
        adt.assert_screen_row_widths_fit(capture)
        adt.assert_cursor_in_bounds(capture)
        adt.assert_segments_valid(capture)
    for capture, code, screen in zip(captures, (critic_run.LONG_CODE,
                                                critic_run.WIDE_CODE,
                                                critic_run.FOLDED_CODE,
                                                critic_run.SHORT_CODE), screens):
        start, end = capture[1]
        assert screen.screen_data[start:end] == code


def test_the_second_judgment_replays_the_recorded_measurement(tmp_path) -> None:
    """Prepare once, judge often: the meeting is read in the first run only."""
    store, captures = _meeting(tmp_path, "replay")
    screen, region = captures[0]
    store_dir = tmp_path / "store"
    result = _judge(tmp_path, store, captures[0], store_dir)
    assert result["measured"] == 2          # the two measurements: lines, name
    assert result["replayed_without_measuring"] is True
    assert result["judged"] == 2            # the pure question ran both times

    from viba.compliance import is_compliant
    from viba.interpret import Environment, EnvironmentCompute
    from viba.compliance import PreparedStorage

    storage = PreparedStorage("root", store_root_dir=str(store_dir / "run"),
                              prepare_root_dir=str(store_dir / "backup"))
    host = ops.MeetingOps(meeting_loader.load_meeting_data_from_run(store.run_dir)[0])
    environ = Environment(storage, EnvironmentCompute(host.get_func),
                          viba_path=str(store_dir))
    again = is_compliant(str(critic_run.RULE), environ)
    assert again.ok_value is False and host.measured == 0

    prepared = sorted(path.name for path in
                      (result["backup"] / "root" / critic_rules.CASE_ENV
                       / "prepare").glob("*.viba"))
    assert prepared == ["max_function_lines.viba", "worst_function_name.viba"]


def test_the_case_is_material_and_travels_as_the_designs_datetime(tmp_path) -> None:
    """The evidence's `$call` is the `mc.ScreenReference` the critic wrote: the
    moment is one `$microsecond`, not a string we invented."""
    store, captures = _meeting(tmp_path, "subject")
    result = _judge(tmp_path, store, captures[0])
    microsecond = critic_rules.captured_microsecond(captures[0][0])
    prepared = (result["backup"] / "root" / critic_rules.CASE_ENV / "prepare"
                / "max_function_lines.viba").read_text(encoding="utf-8")
    assert "$capture_time" in prepared and f"$microsecond {microsecond}" in prepared
    assert '$stream_name "screen"' in prepared and "$region" in prepared


def test_a_moment_this_meeting_never_had_is_no_verdict(tmp_path) -> None:
    """The moment is looked up in the meeting's screen stream: an invented one
    is a broken step, not a verdict."""
    store, captures = _meeting(tmp_path, "invented")
    screen, region = captures[0]
    invented = dict(screen)
    invented["captured_at"] = "2026-01-01T00:00:00.000Z"
    result = _judge(tmp_path, store, (invented, region), tmp_path / "store")
    assert not isinstance(result["verdict"], Ok)


def test_the_actor_writes_the_file_in_vi_line_by_line() -> None:
    """The live meeting's file is written by editing in vi, not by a heredoc.

    The pane is told to open the editor, the code is typed one line at a time
    (blank lines as real returns, the last line without a newline after it), and
    the edit ends with Escape and `:wq`. No `cat >`, no `<<` anywhere in it.
    """
    transport = _Recorder()
    critic_run._edit_with_vi("%1", transport, "batch.py", critic_run.WIDE_CODE)
    calls = transport.calls
    lines = critic_run.WIDE_CODE.splitlines()

    assert calls[0] == ("keys", "vi batch.py")          # a real editor, first thing
    assert calls[-1] == ("keys", ":wq")                 # and the edit is saved by it
    assert calls.index(("literal", "ggdG")) < calls.index(("literal", "i"))
    assert ("special", "Escape") in calls[:-1]

    typed = [text for kind, text in calls
             if kind == "keys" and text not in ("vi batch.py", ":wq")]
    assert typed == [line for line in lines if line][:-1]     # every line but the last
    assert [text for kind, text in calls if kind == "literal"
            and text not in ("i", "ggdG")] == [lines[-1]]     # which is typed, unentered
    assert [kind for kind, _ in calls].count("enter") == sum(1 for line in lines if not line)
    assert not any("<<" in text or "cat >" in text for _, text in calls)


class _Recorder:
    """A transport that only writes down what it was told to do."""

    def __init__(self) -> None:
        self.calls = []

    def send_keys(self, pane_id, text):
        self.calls.append(("keys", text))

    def send_literal(self, pane_id, text):
        self.calls.append(("literal", text))

    def send_enter(self, pane_id):
        self.calls.append(("enter", ""))

    def send_special(self, pane_id, key):
        self.calls.append(("special", key))


def test_a_numbered_view_is_read_and_a_dump_of_values_is_not() -> None:
    """The pane shows what a viewer printed: `cat -n` numbers every line, and a
    program's output parses as well as code does. The region is the code."""
    numbered = "\n".join(f"{index:6d}\t{line}"
                         for index, line in enumerate(critic_run.SHORT_CODE.split("\n"), start=1))
    text = "$ cat -n batch.py\n" + numbered + "\n$ "
    start, end = critic_rules.code_region(text)
    assert critic_rules.ops.editor_text(text[start:end]) == critic_run.SHORT_CODE

    dump = "$ python3 batch.py\n[1, 2, 3, 4, 5] [1, 2, 3, 4, 5]\n[5, 4, 3, 2, 1]\n$ "
    assert critic_rules.code_region(dump) is None


def test_the_critic_finds_the_code_on_the_screen() -> None:
    """A pane holds more than code — the command, the prompt after it. The region
    the critic cites is the longest block of lines the Python parser accepts."""
    text = "$ cat batch.py\n" + critic_run.SHORT_CODE + "\n$ "
    start, end = critic_rules.code_region(text)
    assert text[start:end] == critic_run.SHORT_CODE
    assert critic_rules.code_region("$ ls -la\ntotal 0\n$ ") is None


def test_the_edit_is_judged_before_the_screen_that_printed_it(tmp_path) -> None:
    """The rules are about the edit: when the code is still on an editor frame,
    that frame is judged — a newer frame that printed the file next to a test
    run's output is not where the problem is, even though the code is on it."""
    store, _captures = _meeting(tmp_path, "first-scene")
    code = critic_run.SHORT_CODE

    def append(pane, command):
        rows = [row.rstrip(" ") for row in pane.split("\n")]
        return store.append_screen(pane, command, str(store.workspace_path),
                                   metadata={"pane_width": max(adt.cell_width(r) for r in rows) + 1,
                                             "pane_height": len(rows),
                                             "cursor_x": 0, "cursor_y": len(rows) - 1})

    editor = append(code + "\n" + "\n".join("~" for _ in range(6)) + "\n-- INSERT --",
                    "vim quicksort.py")
    printed = append("$ cat -n quicksort.py\n" + code + "\n$ python3 quicksort.py\n"
                     "[1, 2, 3] [1, 2, 3]\n$ ", "cat -n quicksort.py")

    _section, report = critic_rules.review(store, store_dir=tmp_path / "store")

    assert report["frame"]["event_id"] == editor["event_id"]
    assert report["frame"]["event_id"] != printed["event_id"]
    assert report["citation"]["event_id"] == editor["event_id"]
    assert report["citation"]["excerpt"].startswith("import json")


def test_the_rule_check_reads_the_newest_screen_that_shows_python(tmp_path) -> None:
    """What a real critic is handed before it speaks: the rules' verdicts, with
    the frame they were run over. The newest frame here is a bare prompt, so the
    check walks back to the one that shows the code."""
    store, _captures = _meeting(tmp_path, "review")
    critic_run._scripted_frame(store, critic_run.LONG_CODE)
    store.append_screen("bash-3.2$ ", "echo", str(store.workspace_path),
                        metadata={"pane_width": 12, "pane_height": 1,
                                  "cursor_x": 10, "cursor_y": 0})

    section, report = critic_rules.review(store, store_dir=tmp_path / "review-store")

    assert "rule check" in section
    assert "no function may run past 15 lines" in section and "VIOLATED" in section
    assert "process_batch" in section and "16 行" in section   # the rule's own measurement
    assert "no function may take more than 4 parameters" in section
    assert [rule["verdict"] for rule in report["rules"]] == ["VIOLATED", "holds", "holds"]
    assert report["rules"][0]["offender"] == "process_batch"   # which function, as data
    assert report["rules"][1]["offender"] is None               # the rule that held names nobody
    assert "要在回复里说清是这几件事" in section and "process_batch" in section
    assert report["frame"]["event_id"]                          # the frame it judged
    assert report["frame"]["region"][1] > report["frame"]["region"][0]
    # and it is cited: the reference points at that frame and that region
    assert report["citation"]["kind"] == "screen"
    assert report["citation"]["event_id"] == report["frame"]["event_id"]
    assert [report["citation"]["region"]["start"], report["citation"]["region"]["end"]] == \
        report["frame"]["region"]


def test_the_rule_check_on_a_pane_without_python_runs_nothing(tmp_path) -> None:
    """No code on any screen is not a violation and not a guess: it is said."""
    store = m2.MeetingStore.create(tmp_path, "no-code", "talk", Path(__file__), "s",
                                   model="test-model")
    store.append_screen("bash-3.2$ ls\nbash-3.2$ ", "ls", str(store.workspace_path),
                        metadata={"pane_width": 16, "pane_height": 2,
                                  "cursor_x": 10, "cursor_y": 1})
    section, report = critic_rules.review(store, store_dir=tmp_path / "no-code-store")
    assert "no rule could run" in section
    assert report == {"frame": None, "citation": None, "rules": []}


def test_the_ops_reach_data_through_meeting_sdk_only() -> None:
    """What the host may import and call, read off its own source."""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "ast", "io", "re", "tokenize", "typing",
                        "meeting_core_reflect", "meeting_local_sdk", "viba"}

    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "sdk"}
    assert {"screen_times", "screen_at"} <= called
    assert any(isinstance(node, ast.Attribute) and node.attr == "access"
               and isinstance(node.value, ast.Name) and node.value.id == "reflect"
               for node in ast.walk(tree))
