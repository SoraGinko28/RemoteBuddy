"""underlying_viba_ops — the host side of the critic's first rule.

Two rules hold for everything in this module:

1. **data is fetched through meeting_sdk only**: `meeting_local_sdk` is one
   binding of that interface, and it is the only thing here that reaches the
   meeting. No journal paths, no store, no loader, no `meeting_adt` record.
2. **what comes back is read through viba.reflect**: address by address, on the
   node the protocol hands out. The subject is a `meeting_core` material (the
   interpreter hands it over as viba data, which `viba.reflect` reads), and a
   meeting record is read with `meeting_core_reflect` — the same protocol over
   this layer's own records.

The subject is a `mc.ScreenReference`: the moment, the stream, and the region of
the pane the critic read. The moment travels as the design's `datetime` — one
`$microsecond`, the time identity inside a meeting — and the datetime that
microsecond belongs to is asked of the meeting itself; nothing here rebuilds one.

The one step that is not pure — reading the meeting — goes through `measure`, so
its answer is written as this case's Prepare and replayed on every later run
(`viba-compliance.md`). The comparison is pure and runs on every judgment.
"""

from __future__ import annotations

import ast
from typing import List, Tuple

import meeting_core_reflect as reflect
import meeting_local_sdk as sdk
from viba.compliance import measure
from viba.interpret import read_snapshot
from viba.type import VibaProgramErr

# The screen stream's name, as the record layer spells it; the api this module
# calls (screen_at) is that stream's, so a reference naming another stream is a
# reference this rule cannot read.
SCREEN_STREAM = "screen"

REFERENCE_NAME = "cited_screen_reference"
REFERENCE_SNAPSHOT = "cited_screen_reference"   # what the critic leaves there
LINES_NAME = "max_function_lines"
NAME_NAME = "worst_function_name"
LIMIT_NAME = "within_limit"


class MeetingOps:
    """`get_func` for one meeting: the implementations the rule reaches."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.measured = 0        # how often the impure step actually ran
        self.judged = 0          # how often the pure step ran

    # ---- the host side the interpreter asks ----

    def get_func(self, module_path: str, func_name: str):
        return {
            REFERENCE_NAME: self.cited_screen_reference,
            LINES_NAME: self.max_function_lines,
            NAME_NAME: self.worst_function_name,
            LIMIT_NAME: self.within_limit,
        }.get(func_name)

    # ---- the rule's functions ----

    def cited_screen_reference(self, env):
        """The screen reference the critic cited for this judgment.

        It is material, not source: the critic writes it into the store under the
        case's own address and the rule reads it from there — which is what lets
        one rule judge whatever moment the critic picks.
        """
        cited = read_snapshot(env, REFERENCE_SNAPSHOT)
        if cited is None:
            raise ValueError(f"no screen reference at {REFERENCE_SNAPSHOT!r} "
                             f"under this case's address")
        return cited

    def max_function_lines(self, env, subject):
        """How many lines the longest function in that region takes (measured).

        The call runs in the case's environment, so `measure` keeps the answer
        under that case's own address — the same path on every run.
        """
        def compute(prepared):
            self.measured += 1
            return _longest(_functions(_code(self.ctx, prepared)))[1]
        return measure(env, LINES_NAME, subject, compute)

    def worst_function_name(self, env, subject):
        """Which function that is — measured as well, under its own name, so it
        is its own Prepare (one call has one name)."""
        def compute(prepared):
            self.measured += 1
            return _longest(_functions(_code(self.ctx, prepared)))[0]
        return measure(env, NAME_NAME, subject, compute)

    def within_limit(self, env, lines, name, limit):
        """Is that function within the limit? Pure: every judgment runs it."""
        self.judged += 1
        return lines.value <= limit.value


# ----------------------------------------------------------------------
# the subject: a meeting_core ScreenReference, read through reflect
# ----------------------------------------------------------------------


def _code(ctx, subject) -> str:
    """The code the critic cited: that region of the pane at that moment.

    `$capture_time` is the design's datetime — one `$microsecond`; the datetime
    it belongs to is looked up through meeting_sdk, the pane comes back through
    reflect (`$ok`, then `$text`), and the cited `$region` slices it.
    """
    capture_time = subject.get_capture_time()
    microsecond = _given(capture_time, "microsecond")
    stream_name = _given(subject, "stream_name")
    if stream_name != SCREEN_STREAM:
        raise ValueError(f"this reference is about {stream_name!r}, not the screen stream")
    region = subject.get_region()
    start = _given(region, "start")
    end = _given(region, "end")

    answer = sdk.screen_at(ctx, _moment(ctx, microsecond))
    if isinstance(answer, VibaProgramErr):
        raise ValueError(answer.err_msg)
    text = _record_leaf(_screen_content(answer).get_text())
    return text[start:end]


def _moment(ctx, microsecond: int):
    """The datetime this meeting has for that microsecond, or an error.

    The moments come from meeting_sdk itself (`screen_times`, the moments the
    screen stream captured): nothing here rebuilds a datetime out of a
    microsecond, it is looked up — so a critic can name any moment the meeting
    has, not just the first one.
    """
    answer = sdk.screen_times(ctx)
    if isinstance(answer, VibaProgramErr):
        raise ValueError(answer.err_msg)
    root = _root("Result[list[datetime]]", answer)
    for node in root.get_ok():
        if _record_leaf(node.get_microsecond()) == microsecond:
            # the identity was matched through reflect; what is handed back is
            # the value the meeting itself gave for it
            return node.data
    raise ValueError(f"this meeting has no moment with microsecond {microsecond}")


def _screen_content(result):
    """A `Result[ScreenContent]` read through reflect: enter `$ok`."""
    return _root("Result[ScreenContent]", result).get_ok()


def _given(node, tag: str):
    """A piece of the rule's material: the interpreter hands it over as viba
    data, so `viba.reflect` is the landing that reads it."""
    return node.by_tag(tag).value


def _record_leaf(node):
    """A piece of a meeting record: this layer's own viba.reflect binding reads
    it, so `meeting_core_reflect` is the landing that does."""
    given = reflect.access.leaf(node)
    if isinstance(given, VibaProgramErr):
        raise ValueError(given.err_msg)
    return given.ok_value


def _root(type_expression: str, data):
    """The node of a meeting_sdk answer, rooted at the type the api declares."""
    found = reflect.access_for(type_expression)
    if isinstance(found, VibaProgramErr):
        raise ValueError(found.err_msg)
    access, definition = found.ok_value
    given = access.root(definition, reflect._Record(data))
    if isinstance(given, VibaProgramErr):
        raise ValueError(given.err_msg)
    return given.ok_value


# ----------------------------------------------------------------------
# the code itself: functions and their line counts
# ----------------------------------------------------------------------


def _functions(code: str) -> List[Tuple[str, int]]:
    """(name, lines) for every function in this code, longest first."""
    tree = _parse(code)
    if tree is None:
        return []
    found = [(node.name, (node.end_lineno or node.lineno) - node.lineno + 1)
             for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return sorted(found, key=lambda item: item[1], reverse=True)


def _parse(source: str):
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _longest(found: List[Tuple[str, int]]) -> Tuple[str, int]:
    return max(found, key=lambda item: item[1]) if found else ("", 0)


__all__ = ["MeetingOps", "REFERENCE_NAME", "REFERENCE_SNAPSHOT", "LINES_NAME",
           "NAME_NAME", "LIMIT_NAME", "SCREEN_STREAM"]
