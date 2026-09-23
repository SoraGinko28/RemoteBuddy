"""critic_rules — the rules a meeting's critic enforces, run over its screens.

A rule is a viba program (``critic/*.viba``): environ in, bool out. It bakes in
no meeting and no moment. The case is material the critic hands in — a
``mc.ScreenReference``: which moment, which screen stream, and which region of
the pane — written under the case's own address. Reading the meeting is the one
impure step, so it goes through ``measure``: the answer becomes that case's
Prepare and every later judgment replays it.

The host is ``underlying_viba_ops``: it reaches the meeting through
``meeting_sdk`` only and reads what comes back through ``viba.reflect``.

This module is the critic's own side of that: which frame to judge, the judgment
itself, and how to say what a rule answered — in words a critic can quote.
"""

from __future__ import annotations

import ast
import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

import meeting_core_reflect as reflect
import meeting_loader
import references
import underlying_viba_ops as ops
from viba import viba_ast
from viba.compliance import (PreparedStorage, is_compliant, measured_of,
                             prepare_run, read_prepare)
from viba.interpret import Environment, EnvironmentCompute, write_snapshot
from viba.type import Ok

REPO = Path(__file__).resolve().parent
RULES = (REPO / "critic" / "function_length_le_15.viba",
         REPO / "critic" / "parameter_count_le_4.viba",
         REPO / "critic" / "statements_per_line_le_1.viba")
DESIGN = REPO / "critic" / "design"     # generated from meeting_core_reflect

# The case's environment, as the rules declare it: its path is the case's
# address, and the place the cited reference and its evidence live.
CASE_ENV = "screen_reference_under_judgment"

# The newest screen is often just the prompt that follows the code, and an actor
# that keeps working after printing it pushes that frame further back: walk a
# window, not the last frame or two.
FRAMES_TO_SEARCH = 64

# Where a meeting keeps the evidence of its rule checks.
EVIDENCE_DIR = "critic_rules"


class Case(NamedTuple):
    """How one family of rules makes its cases: the address, and the material.

    A rule never carries a case; the reviewer writes one under the address the
    rule reads, and the material is that case as viba data.
    """

    case_env: str                                        # where the case lands
    snapshot: str                                        # the name the host reads it under
    material: Callable[[Any, Tuple[int, int]], Any]      # event + region -> viba data


class RuleWording(NamedTuple):
    """One rule in words: what it is about, and how to say what it answered."""

    title: str      # what the rule is about, for whoever quotes the verdict
    offender: str   # what the rule names its offender as: a function, or a line
    value: str      # the Prepare holding the number the rule objected to
    name: str       # the Prepare holding the name of that offender
    wording: str    # `{value}` filled in from those two; the limit stays in the rule


RULE_WORDINGS = {
    "function_length_le_15": RuleWording(
        "no function may run past 15 lines", "函数",
        ops.LINES_NAME, ops.NAME_NAME, "有 {value} 行，超过 15 行的上限"),
    "parameter_count_le_4": RuleWording(
        "no function may take more than 4 parameters", "函数",
        ops.PARAMETERS_NAME, ops.WIDEST_NAME, "有 {value} 个参数，超过 4 个参数的上限"),
    "statements_per_line_le_1": RuleWording(
        "no line may put several statements on it with `;`", "位置",
        ops.FOLDED_NAME, ops.WORST_FOLDED_NAME,
        "把 {value} 条语句用 ; 压在一行里，每行只允许一条"),
}


# ----------------------------------------------------------------------
# the judgment: one rule, one moment
# ----------------------------------------------------------------------


def screen_case(event, region: Tuple[int, int]):
    """The critic's case: the moment, the stream, the region of the pane."""
    return screen_reference_material(captured_microsecond(event), region)


# The critic's rules judge a screen; their case is a screen reference.
SCREEN_CASE = Case(CASE_ENV, ops.REFERENCE_SNAPSHOT, screen_case)


def judge_moment(store, case_event, region, store_dir, rule: Path, *,
                 case: Case = SCREEN_CASE,
                 wordings: Dict[str, "RuleWording"] = None) -> Dict[str, Any]:
    """Run one rule over one case: the case is material the reviewer wrote.

    Answers what the rule said (``Ok(True)`` holds, ``Ok(False)`` objects, a
    ``VibaProgramErr`` is a rule that did not run), how often the impure step
    really measured, and — when it objects — the sentence to repeat.
    """
    wordings = RULE_WORDINGS if wordings is None else wordings
    wording = wordings[Path(rule).stem]
    store_dir = Path(store_dir)
    if store_dir.exists():
        shutil.rmtree(store_dir)
    backup = store_dir / "backup"
    storage = PreparedStorage("root", store_root_dir=str(store_dir / "run"),
                              prepare_root_dir=str(backup))
    design = DESIGN / "meeting_core.viba"
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text(reflect._design_source(), encoding="utf-8")

    ctx, _summary = meeting_loader.load_meeting_data_from_run(store.run_dir)
    host = ops.MeetingOps(ctx)
    environ = Environment(storage, EnvironmentCompute(host.get_func),
                          viba_path=f"{design.parent}:{Path(rule).parent}")

    # the evidence: the cited thing, under the case's address
    write_snapshot(environ.sub_env(case.case_env), case.material(case_event, region),
                   case.snapshot)

    result: Dict[str, Any] = {"rule": Path(rule).stem,
                              "case": (str(case_event.get("event_id") or ""), region),
                              "complaint": None, "backup": backup,
                              "replayed_without_measuring": None}
    verdict = prepare_run(str(rule), environ)
    result["prepared"] = verdict
    result["measured"] = host.measured
    if not isinstance(verdict, Ok):
        result["verdict"] = verdict
        return result
    measured_once = host.measured

    verdict = is_compliant(str(rule), environ)
    result["verdict"] = verdict
    result["judged"] = host.judged
    result["replayed_without_measuring"] = host.measured == measured_once

    if isinstance(verdict, Ok):
        case_env = environ.sub_env(case.case_env)
        name = measured_value(read_prepare(case_env, wording.name))
        value = measured_value(read_prepare(case_env, wording.value))
        # What was measured, and who it is about, is data whether or not the
        # rule objected: a reader of the meeting can tell a case that is inside
        # the limit from one that is still being watched.
        result["measured_value"] = {"name": name, "value": value}
        if verdict.ok_value is False:
            result["offender"] = {"name": name, "value": value}
            # In words: a rule's name is a file name, and nobody says a file name
            # out loud. The id stays in the journal, where it is read by code.
            result["complaint"] = (f"{wording.title}：{name} "
                                  f"{wording.wording.format(value=value)}。")
    return result


def verdict_word(result: Dict[str, Any]) -> str:
    """What a rule answered, in one word."""
    verdict = result.get("verdict")
    if not isinstance(verdict, Ok):
        return "did not run"
    return "holds" if verdict.ok_value else "VIOLATED"


def screen_reference_material(microsecond: int, region: Tuple[int, int]):
    """The cited reference as material: `$capture_time` + `$stream_name` + `$region`."""
    def tags(pairs):
        return viba_ast.ProductChain([viba_ast.Tagged(tag, viba_ast.Constant(value))
                                      for tag, value in pairs])
    return viba_ast.ProductChain([
        viba_ast.Tagged("$capture_time", tags([("$microsecond", microsecond)])),
        viba_ast.Tagged("$stream_name", viba_ast.Constant(ops.SCREEN_STREAM)),
        viba_ast.Tagged("$region", tags([("$start", region[0]), ("$end", region[1])])),
    ])


def measured_value(prepare):
    """What a Prepare measured: the value it holds, through viba.reflect."""
    value, recorded = measured_of(prepare)
    return value if recorded else None


def captured_microsecond(screen) -> int:
    """The moment of a screen event, as the design's datetime: one `$microsecond`."""
    text = str(screen.get("captured_at") or "")
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).microsecond


# ----------------------------------------------------------------------
# the frame to judge: where the Python is
# ----------------------------------------------------------------------


def code_region(text: str) -> Optional[Tuple[int, int]]:
    """Where the code is on this screen, or None when the screen shows none.

    A pane holds more than code — the command that printed it, the prompt after
    it, a program's output. The rules are about functions and lines, so a block
    only counts as code when it *defines* something: a screen full of test
    output (`[1, 2, 3]` parses as well as `items = [1, 2, 3]` does) is not a
    file, and judging it would answer about the wrong thing. Among the blocks
    that define something, the longest wins; top-level code starts in column 0.
    """
    lines = text.split("\n")
    starts = _line_starts(text)
    best: Optional[Tuple[int, int]] = None
    for first in range(len(lines)):
        # The pane is what the viewer printed; a candidate block is that with
        # any line-number gutter taken off, which is what the rules must judge.
        head = ops.editor_text(lines[first])
        if not head or head[0].isspace():
            continue                       # top-level code does not start indented
        for last in range(len(lines), first, -1):
            if best is not None and last - first <= best[1] - best[0]:
                break                      # this block cannot beat the best one
            block = ops.editor_text("\n".join(lines[first:last]))
            if _parses(block) and _defines(block):
                best = (first, last)
                break
    if best is None:
        return None
    first, last = best
    return starts[first], starts[last - 1] + len(lines[last - 1])


def _defines(block: str) -> bool:
    """Whether this block is a piece of a file rather than a value or output."""
    try:
        tree = ast.parse(block)
    except (SyntaxError, ValueError):
        return False
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Import, ast.ImportFrom))
               for node in tree.body)


def _line_starts(text: str) -> List[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _parses(block: str) -> bool:
    try:
        ast.parse(block)
    except (SyntaxError, ValueError):
        return False
    return True


# ----------------------------------------------------------------------
# the whole check: every rule, over the newest screen that shows Python
# ----------------------------------------------------------------------


def review(store, store_dir: Optional[Path] = None,
           rules=RULES) -> Tuple[str, Dict[str, Any]]:
    """Run every rule over the newest screen that shows Python.

    Returns ``(section, report)``: the block to put in a critic's context, and
    the same thing as data, so the meeting can keep what was checked. Never
    raises — a critic's turn must go on even when a frame cannot be read.
    """
    try:
        return _review(store, store_dir, rules)
    except Exception as exc:  # noqa: BLE001 — the check is not the critic's job to debug
        section = ("=== rule check (the meeting's rules) ===\n"
                   f"the rules could not be run over this meeting: {exc!r}\n"
                   "Say that, do not guess.")
        return section, {"error": repr(exc), "rules": []}


def _review(store, store_dir, rules) -> Tuple[str, Dict[str, Any]]:
    screens = [event for event in store.events()
               if event.get("kind") == "screen" and (event.get("content") or "").strip()]
    found = _frame_to_judge(screens[-FRAMES_TO_SEARCH:])
    if found is None:
        return (_NO_CODE_SECTION, {"frame": None, "citation": None, "rules": []})
    screen, region = found
    results = []
    for rule in rules:
        where = Path(store_dir) if store_dir else Path(store.run_dir) / EVIDENCE_DIR
        results.append(judge_moment(store, screen, region, where / rule.stem, rule,
                                    case=SCREEN_CASE))
    report = {"frame": {"event_id": screen.get("event_id"),
                        "captured_at": screen.get("captured_at"),
                        "region": list(region)},
              "citation": frame_reference(screen, region),
              "rules": [{"rule": result["rule"], "verdict": verdict_word(result),
                         "offender": (result.get("offender") or {}).get("name"),
                         "complaint": result.get("complaint")} for result in results]}
    return _section(screen, region, results), report


def _frame_to_judge(screens):
    """The newest frame the rules should judge: the edit, not the output.

    A frame where the code was typed is the first scene, and it wins even when
    a newer frame still shows the same code next to what a test run printed: a
    citation into the output scene answers about the wrong place.
    """
    for editor_only in (True, False):
        for screen in reversed(list(screens)):
            if editor_only and not _is_editor_view(screen["content"]):
                continue
            region = code_region(screen["content"])
            if region is not None:
                return screen, region
    return None


def _is_editor_view(text: str) -> bool:
    """Whether the pane shows the file in an editor rather than printed out.

    vim fills the rows after the file with `~` and writes its mode on the last
    line (`-- INSERT (paste) --`); a printed file leaves a shell prompt and the
    command that printed it. The difference matters because the rules are about
    the edit: judging the screen where the output was read answers about the
    wrong scene, even when the same code is on it.
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and set(stripped) == {"~"}:
            return True
        if stripped.startswith("--") and stripped.endswith("--") and len(stripped) > 4:
            return True
    return False


def frame_reference(screen, region: Tuple[int, int]) -> Dict[str, Any]:
    """The frame a check judged, as a reference: that event, that region of it.

    A critic cites what it read: the pane at one moment and the span the rules
    were run over. Whoever quotes the verdict carries this, so the claim points
    at the frame instead of describing where it is.
    """
    return references.reference_from_event(
        screen, {"kind": "screen", "event_id": screen["event_id"],
                 "region": {"start": region[0], "end": region[1]}})


def citation(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The reference a report carries, or None when no frame was judged."""
    found = report.get("citation")
    return found if isinstance(found, dict) else None


_NO_CODE_SECTION = (
    "=== rule check (the meeting's rules) ===\n"
    "no screen in this meeting shows a block of Python, so no rule could run.\n"
    "Say that, do not guess."
)


def _section(screen, region, results) -> str:
    lines = ["=== rule check (the meeting's rules, run for you over the newest screen "
             "that shows Python) ===",
             f"screen {screen.get('event_id')} at {screen.get('captured_at')}, "
             f"region {region[0]}..{region[1]} of the pane"]
    for result in results:
        wording = RULE_WORDINGS[result["rule"]]
        line = f"- {wording.title}: {verdict_word(result)}"
        if result.get("complaint"):
            line += f" — {result['complaint']}"
        lines.append(line)
    offenders = [result for result in results
                 if (result.get("offender") or {}).get("name")]
    if offenders:
        named = "；".join(
            f"{RULE_WORDINGS[result['rule']].title}——{result['offender']['name']}"
            for result in offenders)
        lines.append(f"要在回复里说清是这几件事：{named}（说事，不要念规则的文件名）。")
    else:
        lines.append("没有规则被违反：直说没有违规，不要自己另外找毛病。")
    lines.append("These verdicts are the rules' answers, computed for you: name the rule "
                 "and the function exactly as written above, and do not judge the code "
                 "yourself. The frame they were run over is cited as a reference on your "
                 "reply, so point at it with that citation instead of describing where it is.")
    return "\n".join(lines)
