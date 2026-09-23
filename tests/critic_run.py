#!/usr/bin/env python3
"""The meeting critic runs the second-order critic's rules.

    .venv/bin/python -m tests.critic_run

This is a demo of the rule layer, not the product's critic: the demo lives under
``tests/``, the rules it runs (``critic/*.viba``), the host
(``underlying_viba_ops.py``) and the judgment (``critic_rules.py``) are product
code — the real critic runs the same ones.

One process, no fixtures:

1. a real meeting is started in ``runs/`` by the harness's own writer. The
   generator edits ``batch.py`` in a real ``vi`` in the pane — it opens the
   editor and types the file one line at a time, so what the pane shows is the
   edit itself, and the frames of the edit are journaled as they happen. Three
   versions go by, each one captured as a terminal capture is: the pane's own
   text, its parameters (row count, width, cursor) and the moment, all checked
   by ``adt``'s own screen asserts. The first version has a function running
   past 15 lines, the second has a short function taking five parameters, the
   third has neither fault;
2. the critic takes the evidence for each moment it wants judged: a
   ``mc.ScreenReference`` — the moment as the design's datetime (one
   ``$microsecond``), the screen stream, and the region of the pane it read —
   written into the store under that case's address. The rules are two files
   and never change: the case is material the critic hands in;
3. each rule runs: ``prepare_run`` measures through the host (reading the
   meeting is the one impure step, so it becomes a Prepare in the backup), then
   ``is_compliant`` replays that evidence and answers the verdict;
4. the critic says so in the meeting: a chat message from ``critic`` naming the
   rule, the function that broke it, the number it measured, and the pane it
   read. The length rule objects to the first version, the parameter rule to
   the second, and neither to the third — that is what a second rule buys.

Honest about its own limits: the *speaking* here is scripted. The demo writes
that chat message itself, under the critic's name, from the rule's Prepares
(``judge``/``cite``); the generator's three versions are constants too. What is
not scripted is the rule, the measurement and the verdict — those are the
product's own layers (``viba.compliance``, ``meeting_sdk``, ``viba.reflect``).
The participant that would read the meeting and decide to speak for real is
``meeting_reviewer``/``discriminator_harness``, and this demo does not use it.

The rules are viba source; the host is ``underlying_viba_ops``, which reaches
the meeting through ``meeting_sdk`` only and reads what it gets through
``viba.reflect``.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from pathlib import Path

import critic_rules
import meeting_adt as adt
import meeting_loader
import meeting_v2 as m2
import references
from tmux_transport import TmuxTransport
from workspace_capture import capture_stable_workspace, capture_workspace

HERE = Path(__file__).resolve().parent            # tests/
REPO = HERE.parent                                # the checkout
RULE = critic_rules.RULES[0]
PARAMETER_RULE = critic_rules.RULES[1]
FOLDED_RULE = critic_rules.RULES[2]
RULES = critic_rules.RULES
STORE = HERE / "critic" / "store"                 # the demo's own prepared evidence

LONG_CODE = '''import json


def load_rows(path):
    with open(path) as handle:
        return json.load(handle)


def total(rows):
    return sum(row["amount"] for row in rows)


def process_batch(path):
    rows = load_rows(path)
    kept = []
    for row in rows:
        if row.get("amount") is None:
            continue
        if row.get("id") is None:
            continue
        if row["amount"] < 0:
            continue
        if row["amount"] > 100:
            continue
        kept.append({"id": row["id"], "amount": row["amount"]})
    if not kept:
        return {"count": 0, "total": 0}
    return {"count": len(kept), "total": total(kept)}
'''

WIDE_CODE = '''import json


def load_rows(path):
    with open(path) as handle:
        return json.load(handle)


def total(rows):
    return sum(row["amount"] for row in rows)


def keep_row(row, low, high, allow_zero, strict):
    amount = row.get("amount")
    if amount is None:
        return False
    if strict and not allow_zero:
        return low < amount < high
    return low <= amount <= high


def process_batch(path):
    kept = [row for row in load_rows(path) if keep_row(row, 0, 100, False, True)]
    return {"count": len(kept), "total": total(kept)}
'''

FOLDED_CODE = '''import json


def load_rows(path):
    with open(path) as handle:
        return json.load(handle)


def total(rows):
    return sum(row["amount"] for row in rows)


def process_batch(path):
    rows = load_rows(path)
    kept = []
    for row in rows:
        amount = row.get("amount")
        if amount is None:
            continue
        if 0 <= amount <= 100:
            kept.append(row)
    kept.sort(key=lambda row: row["id"]); return {"count": len(kept), "total": total(kept)}
'''

SHORT_CODE = '''import json


def load_rows(path):
    with open(path) as handle:
        return json.load(handle)


def total(rows):
    return sum(row["amount"] for row in rows)


def process_batch(path):
    kept = [row for row in load_rows(path) if 0 <= row.get("amount", -1) <= 100]
    return {"count": len(kept), "total": total(kept)}
'''


# ----------------------------------------------------------------------
# 1. the meeting, with the code in it
# ----------------------------------------------------------------------


def start_meeting(root: Path, meeting_id: str, live: bool = True):
    """A real meeting: a live session, and frames captured from it.

    ``live=False`` writes the same kind of frames without a terminal: the
    workspace page has nothing to take over then, but the frames still satisfy
    the design's screen asserts, which is what the tests need.

    The harness's own writer and transport do the work: the workspace session is
    started (`TmuxTransport.create_workspace_session`), the store is told which
    pane it owns, and every frame comes from the pane itself
    (`workspace_capture.capture_workspace`) — plain text, ANSI segments, and the
    cursor and size the terminal reports, not numbers written down here.

    The generator's turn is scripted (edit the file in vi, print it), but the
    session is real: the workspace page can take the terminal over, and the
    frames are what the terminal actually showed.
    """
    # the harness only reads the skill text; it is kept out of the checkout
    skill = Path(tempfile.mkdtemp(prefix="critic-skill-")) / "SKILL.md"
    skill.write_text("---\nname: workspace-collaborator\n---\n", encoding="utf-8")
    store = m2.MeetingStore.create(root, meeting_id,
                                   "写一个把批次里的小额记录挑出来的函数",
                                   skill, f"critic-{meeting_id}", "test-model")
    store.append_chat(m2.DEFAULT_AGENT_NAME, "先写一版", "progress")

    transport = None
    if live:
        binary = shutil.which("tmux")
        if binary is None:
            raise RuntimeError("tmux is not installed: a live workspace session needs it")
        transport = TmuxTransport(binary)
        store.update(status="starting", phase="starting", control_owner="none", failure=None)
        pane_id = transport.create_workspace_session(store.meeting.session_name,
                                                     store.workspace_path)
        store.set_session(pane_id)
        transport.send_keys(pane_id, "clear")      # the shell's banner off the frame
        capture_workspace(store, transport)        # the opening frame

    captures = []
    for code in (LONG_CODE, WIDE_CODE, FOLDED_CODE, SHORT_CODE):
        if live:
            # the generator edits in the terminal, and the pane shows it: the
            # frames of the edit are journaled as they happen, then the file is
            # printed for a clean look at what it now says
            _edit_with_vi(pane_id, transport, "batch.py", code,
                          lambda: _capture(store, transport))
            transport.send_keys(pane_id, "clear && cat batch.py")
            frame = _capture(store, transport)
            region = _region_of(frame, code)
        else:
            (store.workspace_path / "batch.py").write_text(code, encoding="utf-8")
            frame, region = _scripted_frame(store, code)
        store.append_chat(m2.DEFAULT_AGENT_NAME, "batch.py 更新了", "final")
        captures.append((frame, region))
    if live:
        store.update(status="running", phase="waiting_for_user", control_owner="none")
    return store, captures, transport


def _edit_with_vi(pane_id: str, transport, name: str, code: str, look=None) -> None:
    """Produce the file the way a generator would: in vi, one line at a time.

    No heredoc and no `cat >`: the pane runs a real editor, and the file is what
    the typing left behind. Whatever the file said before is taken out first
    (`ggdG`), so each version is an edit of the file rather than an append to
    it. There is no newline after the last line: the editor writes it.

    ``look`` is called while the edit is on screen — mid-typing, and again once
    the last line is down — so the frames that show the editing come from the
    pane at the time it happened, still in insert mode, before anything is saved.
    """
    transport.send_keys(pane_id, f"vi {name}")       # a real editor, not a heredoc
    transport.send_literal(pane_id, "ggdG")          # the old text goes
    transport.send_literal(pane_id, "i")             # insert mode: typing is writing
    lines = code.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line:
            transport.send_keys(pane_id, line)
        else:
            transport.send_enter(pane_id)            # a blank line is an empty line
        if look is not None and index + 1 == len(lines) // 2:
            look()                                   # the edit itself, mid-typing
    if lines[-1]:
        transport.send_literal(pane_id, lines[-1])   # the last line, no newline after it
    if look is not None:
        look()                                       # the whole file, still in the editor
    transport.send_special(pane_id, "Escape")        # command mode: the file is what it says
    transport.send_keys(pane_id, ":wq")              # save and quit


def _capture(store, transport):
    """One frame from the pane, journaled even when it repeats."""
    frame = capture_stable_workspace(store, transport)
    return frame if frame is not None else capture_workspace(store, transport)


def _scripted_frame(store, code: str):
    """A frame written by hand, with the parameters a capture needs."""
    prefix, suffix = "$ cat batch.py\n", "\n$ "
    pane = prefix + code + suffix
    rows = [row.rstrip(" ") for row in pane.split("\n")]
    frame = store.append_screen(pane, "cat batch.py", str(store.workspace_path),
                                metadata={"pane_width": max(adt.cell_width(r) for r in rows) + 1,
                                          "pane_height": len(rows),
                                          "cursor_x": len(rows[-1]),
                                          "cursor_y": len(rows) - 1})
    return frame, (len(prefix), len(prefix) + len(code))


def _region_of(frame, code: str):
    """Where that code sits in the plain text of the frame the pane gave."""
    start = frame["content"].index(code)
    return (start, start + len(code))


# ----------------------------------------------------------------------
# 2. the critic's evidence, and 3. the judgment
# ----------------------------------------------------------------------


def judge(store, screen, region, store_dir: Path, rule: Path = RULE):
    """Judge one moment with one rule, then say so in the meeting.

    The judgment is the product's (`critic_rules.judge_moment`): the rule runs,
    the case is the material handed in, the verdict is the rule's. The *saying*
    is this demo's, and it is scripted — see the module docstring.
    """
    result = critic_rules.judge_moment(store, screen, region, store_dir, rule)
    if result["complaint"]:
        microsecond, region = result["case"]
        result["complaint"] += (
            f"地址：microsecond={microsecond} 的屏幕 region {region[0]}..{region[1]}"
            f"（{store.meeting.meeting_id}）。")
        cite(store, screen, result["complaint"])
    return result


def cite(store, screen, complaint: str):
    """The demo puts the complaint in the meeting under ``critic``'s name.

    A scripted participant, not a model: it appends the chat message straight to
    the journal, with a screen reference to the pane the rule's evidence came
    from. A real reviewer would take this turn itself.
    """
    content = screen.get("content") or ""
    reference = references.reference_from_event(
        screen, {"kind": "screen", "event_id": screen["event_id"],
                 "region": {"start": 0, "end": len(content)}})
    store.append_chat(m2.DEFAULT_DISCRIMINATOR_NAME, complaint, "request",
                      references=[reference])


# ----------------------------------------------------------------------


def main() -> int:
    meeting_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-critic-rules"
    store, captures, transport = start_meeting(REPO, meeting_id)
    print(f"meeting: {meeting_id}  ({store.run_dir})")
    _check_captures(store)

    complaints = 0
    for screen, region in captures:
        microsecond = critic_rules.captured_microsecond(screen)
        for rule in RULES:
            result = judge(store, screen, region,
                           STORE / f"{microsecond}-{rule.stem}", rule)
            print(f"\nmoment microsecond={microsecond}  region {region[0]}..{region[1]}"
                  f"  rule {result['rule']}")
            print(f"  prepared: {result.get('prepared')!r}  "
                  f"measuring {result.get('measured')} time(s)")
            print(f"  judged:   {result['verdict']!r}  "
                  f"({'replayed, no new measurement' if result['replayed_without_measuring'] else 'measured again'})"
                  f", compared {result.get('judged')}")
            if result["complaint"]:
                complaints += 1
                print(f"  critic: {result['complaint']}")
            else:
                print("  critic: 没有违规，不说话")

    print(f"\n{len(captures)} 个时刻 × {len(RULES)} 条规则 → {complaints} 次发言")
    print()
    print((store.run_dir / "conversation.md").read_text(encoding="utf-8").strip())
    print()
    print(f"the session stays up, so the workspace page can take it over:")
    print(f"  {TmuxTransport.attach_command(store.meeting.session_name)}")
    print(f"  python demo_v2.py stop {meeting_id}   # when you are done")
    return 0


def _check_captures(store) -> None:
    """The design's own screen asserts over the captures this run prepared."""
    data, _summary = meeting_loader.load_meeting_data_from_run(store.run_dir)
    captures = [chunk for chunk in data.sorted_stream_chunks
                if isinstance(chunk, adt.ScreenStreamChunk)][-1].chunks
    for capture in captures:
        adt.assert_screen_plain_matches(capture)
        adt.assert_screen_rows_match_height(capture)
        adt.assert_screen_row_widths_fit(capture)
        adt.assert_cursor_in_bounds(capture)
        adt.assert_segments_valid(capture)
    print("captures pass the design's screen asserts: "
          + "; ".join(f"{c.screen_width}x{c.screen_height} cursor {c.cursor_x},{c.cursor_y}"
                      for c in captures))


if __name__ == "__main__":
    raise SystemExit(main())
