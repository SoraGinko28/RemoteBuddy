"""The second-order critic's rules, run over a real meeting.

A demo of the rule layer, under ``tests/`` with the demo and the rules it uses
(``tests/critic_run.py``, ``tests/critic/``). The rules are
`tests/critic/function_length_le_15.viba` and
`tests/critic/parameter_count_le_4.viba`, the host is
`tests/underlying_viba_ops.py`, and the ``critic`` who speaks is scripted — the
demo writes that chat message itself. Each rule judges every moment the critic
hands in — the case is material the critic writes into the store, not source
baked into the rule — and these cases pin the verdicts, the complaint, the
replay, the way the file gets written, and the two constraints on the host.
"""

from __future__ import annotations

import ast
from pathlib import Path

import meeting_adt as adt
import meeting_loader
import meeting_v2 as m2
from tests import critic_run
from tests import underlying_viba_ops as ops
from viba.type import Ok


def _meeting(tmp_path: Path, name: str):
    """A meeting with three captures: a long function, a wide one, a clean one.

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
    length = [_judge(tmp_path, store, capture, tmp_path / f"length-{index}")
              for index, capture in enumerate(captures)]
    params = [_judge(tmp_path, store, capture, tmp_path / f"params-{index}",
                     critic_run.PARAMETER_RULE)
              for index, capture in enumerate(captures)]

    assert [result["verdict"].ok_value for result in length] == [False, True, True]
    assert [result["verdict"].ok_value for result in params] == [True, False, True]

    assert "process_batch" in length[0]["complaint"]
    assert "16 行" in length[0]["complaint"]      # the function's real length
    assert "keep_row" in params[1]["complaint"]
    assert "5 个参数" in params[1]["complaint"]   # its real parameter count
    assert length[1]["complaint"] is None and length[2]["complaint"] is None
    assert params[0]["complaint"] is None and params[2]["complaint"] is None


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
    assert len(screens) == 3
    for capture in screens:
        adt.assert_screen_plain_matches(capture)
        adt.assert_screen_rows_match_height(capture)
        adt.assert_screen_row_widths_fit(capture)
        adt.assert_cursor_in_bounds(capture)
        adt.assert_segments_valid(capture)
    for capture, code, screen in zip(captures, (critic_run.LONG_CODE,
                                                critic_run.WIDE_CODE,
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
                      (result["backup"] / "root" / critic_run.CASE_ENV
                       / "prepare").glob("*.viba"))
    assert prepared == ["max_function_lines.viba", "worst_function_name.viba"]


def test_the_case_is_material_and_travels_as_the_designs_datetime(tmp_path) -> None:
    """The evidence's `$call` is the `mc.ScreenReference` the critic wrote: the
    moment is one `$microsecond`, not a string we invented."""
    store, captures = _meeting(tmp_path, "subject")
    result = _judge(tmp_path, store, captures[0])
    microsecond = critic_run._captured_at(captures[0][0]["captured_at"]).microsecond
    prepared = (result["backup"] / "root" / critic_run.CASE_ENV / "prepare"
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


def test_the_generator_writes_the_file_in_vi_line_by_line() -> None:
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


def test_the_ops_reach_data_through_meeting_sdk_only() -> None:
    """What the host may import and call, read off its own source."""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "ast", "typing",
                        "meeting_core_reflect", "meeting_local_sdk", "viba"}

    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "sdk"}
    assert {"screen_times", "screen_at"} <= called
    assert any(isinstance(node, ast.Attribute) and node.attr == "access"
               and isinstance(node.value, ast.Name) and node.value.id == "reflect"
               for node in ast.walk(tree))
