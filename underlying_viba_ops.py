"""underlying_viba_ops — the host side of the critic's rules.

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
import io
import re
import tokenize
from typing import Any, Dict, List, Tuple

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
# the first rule: a function may not run past 15 lines
LINES_NAME = "max_function_lines"
NAME_NAME = "worst_function_name"
LIMIT_NAME = "within_limit"
# the second rule: a function may not take more than 4 parameters
PARAMETERS_NAME = "max_function_parameters"
WIDEST_NAME = "widest_function_name"
PARAMETER_LIMIT_NAME = "within_parameter_limit"
# the third rule: one statement per line, nothing folded with `;`
FOLDED_NAME = "max_folded_statements"
WORST_FOLDED_NAME = "worst_folded_line"
FOLD_LIMIT_NAME = "within_fold_limit"
# the assistant's rule: the member that owes an answer has gone quiet
CHAT_REFERENCE_NAME = "cited_critic_message"
CHAT_SNAPSHOT = "cited_critic_message"     # what the assistant leaves there
UNANSWERED_NAME = "max_unanswered_seconds"
SILENT_NAME = "unanswered_member"
GRACE_NAME = "within_answer_grace"
UNREVIEWED_NAME = "unreviewed_replies"
REVIEW_LIMIT_NAME = "within_review_limit"


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
            PARAMETERS_NAME: self.max_function_parameters,
            WIDEST_NAME: self.widest_function_name,
            PARAMETER_LIMIT_NAME: self.within_parameter_limit,
            FOLDED_NAME: self.max_folded_statements,
            WORST_FOLDED_NAME: self.worst_folded_line,
            FOLD_LIMIT_NAME: self.within_fold_limit,
            CHAT_REFERENCE_NAME: self.cited_critic_message,
            UNANSWERED_NAME: self.max_unanswered_seconds,
            SILENT_NAME: self.unanswered_member,
            GRACE_NAME: self.within_answer_grace,
            UNREVIEWED_NAME: self.unreviewed_replies,
            REVIEW_LIMIT_NAME: self.within_review_limit,
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

    def max_function_parameters(self, env, subject):
        """How many parameters the widest function in that region takes (measured).

        Its own name, so its own Prepare: the second rule's number, like the
        first rule's, is the thing a complaint has to quote.
        """
        def compute(prepared):
            self.measured += 1
            return _widest(_functions(_code(self.ctx, prepared)))[1]
        return measure(env, PARAMETERS_NAME, subject, compute)

    def widest_function_name(self, env, subject):
        """Which function that is — measured as well, so the complaint can name it."""
        def compute(prepared):
            self.measured += 1
            return _widest(_functions(_code(self.ctx, prepared)))[0]
        return measure(env, WIDEST_NAME, subject, compute)

    def within_parameter_limit(self, env, count, name, limit):
        """Is that function's parameter count within the limit? Pure."""
        self.judged += 1
        return count.value <= limit.value

    def max_folded_statements(self, env, subject):
        """How many statements the worst `;`-folded line holds (measured)."""
        def compute(prepared):
            self.measured += 1
            return _worst_folding(_code(self.ctx, prepared))[1]
        return measure(env, FOLDED_NAME, subject, compute)

    def worst_folded_line(self, env, subject):
        """Where that line is, and which function it is in — measured as well."""
        def compute(prepared):
            self.measured += 1
            return _worst_folding(_code(self.ctx, prepared))[0]
        return measure(env, WORST_FOLDED_NAME, subject, compute)

    def within_fold_limit(self, env, count, name, limit):
        """Does one line hold at most the allowed number of statements? Pure."""
        self.judged += 1
        return count.value <= limit.value

    def cited_critic_message(self, env):
        """The chat message the assistant cited: what the critic just said.

        Material, like the critic's screen reference: the assistant writes it
        under the case's own address.
        """
        cited = read_snapshot(env, CHAT_SNAPSHOT)
        if cited is None:
            raise ValueError(f"no cited message at {CHAT_SNAPSHOT!r} "
                             f"under this case's address")
        return cited

    def max_unanswered_seconds(self, env, subject):
        """How long the member that owes an answer has been quiet (measured)."""
        def compute(prepared):
            self.measured += 1
            return _unanswered(prepared, self.ctx)[0]
        return measure(env, UNANSWERED_NAME, subject, compute)

    def unanswered_member(self, env, subject):
        """Who owes it — measured as well, so the complaint can name them."""
        def compute(prepared):
            self.measured += 1
            return _unanswered(prepared, self.ctx)[1]
        return measure(env, SILENT_NAME, subject, compute)

    def within_answer_grace(self, env, seconds, who, limit):
        """Has the wait stayed inside the grace the rule allows? Pure."""
        self.judged += 1
        return seconds.value <= limit.value

    def unreviewed_replies(self, env, subject):
        """How many replies the actor has made since that message (measured).

        An answer nobody has looked at is not an answer yet: the critic owes it
        a re-check, and this is how many it owes.
        """
        def compute(prepared):
            self.measured += 1
            return _answer_state(prepared, self.ctx)[2]
        return measure(env, UNREVIEWED_NAME, subject, compute)

    def within_review_limit(self, env, replies, who, limit):
        """Have the replies been reviewed yet? Pure."""
        self.judged += 1
        return replies.value <= limit.value


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
    # What the pane shows is not always the file: a viewer prints line numbers.
    return editor_text(text[start:end])


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


def _functions(code: str) -> List[Tuple[str, int, int]]:
    """(name, lines, parameters) for every function in this code."""
    tree = _parse(code)
    if tree is None:
        return []
    return [(node.name, _body_lines(node), _parameters(node))
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _body_lines(node) -> int:
    """How many lines the function's body spans, its `def` included."""
    return (node.end_lineno or node.lineno) - node.lineno + 1


def _parameters(node) -> int:
    """Every named slot the function takes: positional, keyword-only, `*args`,
    `**kwargs` — the width of its door."""
    args = node.args
    return (len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
            + (1 if args.vararg else 0) + (1 if args.kwarg else 0))


def _parse(source: str):
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _longest(found: List[Tuple[str, int, int]]) -> Tuple[str, int]:
    """The function with the most lines, or nothing if there is none."""
    best = max(found, key=lambda item: item[1], default=("", 0, 0))
    return best[0], best[1]


def _widest(found: List[Tuple[str, int, int]]) -> Tuple[str, int]:
    """The function with the most parameters, or nothing if there is none."""
    best = max(found, key=lambda item: item[2], default=("", 0, 0))
    return best[0], best[2]


# `cat -n` and vim's `number` both write a line as `  10<TAB>items = ...`.
GUTTER = re.compile(r"^[ \t]*(\d+)[ \t](.*)$")


def editor_text(text: str) -> str:
    """The code as the screen shows it, with a line-number gutter taken off.

    The numbers a viewer prints are not part of the file, so a region read off
    the pane goes through here before anything parses it. Only a block where
    every non-empty line is numbered, with the numbers ascending by one, counts
    as a gutter; anything else is left alone.
    """
    lines = text.split("\n")
    matched = [GUTTER.match(line) for line in lines]
    numbered = [found for found in matched if found]
    if not numbered or any(found is None and line.strip()
                           for found, line in zip(matched, lines)):
        return text
    numbers = [int(found.group(1)) for found in numbered]
    if any(second - first != 1 for first, second in zip(numbers, numbers[1:])):
        return text
    return "\n".join(found.group(2) if found else line
                     for found, line in zip(matched, lines))


def _semicolon_lines(code: str) -> Dict[int, int]:
    """Line -> how many `;` statement separators it holds.

    The tokenizer is what knows a `;` is a separator and not text: a `;` inside
    a string or a comment is part of that token, never an `OP`.
    """
    counts: Dict[int, int] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type == tokenize.OP and token.string == ";":
                counts[token.start[0]] = counts.get(token.start[0], 0) + 1
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {}
    return counts


def _worst_folding(code: str) -> Tuple[str, int]:
    """Where the worst `;`-folding is, and how many statements that line holds."""
    counts = _semicolon_lines(code)
    if not counts:
        return "", 0
    line = max(counts, key=lambda number: (counts[number], -number))
    where = f"第 {line} 行"
    inside = _enclosing_function(code, line)
    # Named like the other rules' offenders (the function first), with the line
    # after it: "quick_sort 第 5 行".
    return (f"{inside} {where}" if inside else where), counts[line] + 1


# ----------------------------------------------------------------------
# the chat side: who owes an answer, and how long they have been quiet
# ----------------------------------------------------------------------


def _unanswered(subject, ctx) -> Tuple[int, str]:
    """(seconds overdue, who owes it) — see ``_answer_state``."""
    seconds, who, _replies = _answer_state(subject, ctx)
    return seconds, who


def _answer_state(subject, ctx) -> Tuple[int, str, int]:
    """(seconds overdue, who owes an answer, replies awaiting review).

    The cited message is found by its identity — the `$microsecond` the design
    gives a moment — and the wait is the distance between two moments the
    meeting itself handed out; the members and their roles come from
    `meeting_sdk` as well. Nothing here rebuilds a datetime, a name or a role
    out of the record it was handed.
    """
    cited_us = _given(subject.get_msg_time(), "microsecond")
    actor = _actor_name(ctx)
    window = _chat_window(ctx)
    cited_at = next((when for when, _ in window if _moment_us(when) == cited_us), None)
    if cited_at is None:
        return 0, "", 0
    replies = [when for when, author in window if actor and author == actor and when > cited_at]
    if replies:
        # The actor answered: nothing is overdue, and how much of it is
        # unreviewed is the count of those answers.
        return 0, actor, len(replies)
    now_at = _ok_root("Result[datetime]", sdk.now(ctx)).data
    return max(0, int((now_at - cited_at).total_seconds())), actor, 0


def _chat_window(ctx) -> List[Tuple[Any, str]]:
    """(moment, author) for every chat message the meeting has, oldest first."""
    start = _ok_root("Result[datetime]", sdk.first_chunk_time(ctx)).data
    end = _ok_root("Result[datetime]", sdk.now(ctx)).data
    found = _ok_root("Result[list[ChatMessage]]", sdk.chat_messages(ctx, start, end))
    return [(found[index].get_time().data, _message_author(found[index]))
            for index in range(len(found))]


def _moment_us(value) -> int:
    """The design's `$microsecond` for a moment the meeting handed out."""
    return _record_leaf(_root("datetime", value).get_microsecond())


def _message_author(message) -> str:
    """The agent that wrote a message, or "" when the human did.

    The record layer carries a user name as one string: an agent's name, or the
    human sentinel. `$human` is the sentinel — a member named "human" is not a
    member at all.
    """
    name = str(_record_leaf(message.get_user_name().get_agent_name()))
    return "" if name in {"$human", "human"} else name


def _actor_name(ctx) -> str:
    """The member that holds the terminal: the one who owes the answers."""
    roles = _ok_root("Result[dict[str, AgentRole]]", sdk.roles(ctx))
    for name in roles.keys():
        if roles.at_key(name).has_actor():
            return str(name)
    return ""


def _ok_root(type_expression: str, answer):
    """`$ok` of a meeting_sdk answer, rooted at the type the api declares."""
    return _root(type_expression, answer).get_ok()


def _enclosing_function(code: str, line: int) -> str:
    """The innermost function that line is inside, or "" at module level."""
    tree = _parse(code)
    if tree is None:
        return ""
    found = [((node.end_lineno or node.lineno) - node.lineno, node.name)
             for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.lineno <= line <= (node.end_lineno or node.lineno)]
    return min(found)[1] if found else ""


__all__ = ["MeetingOps", "REFERENCE_NAME", "REFERENCE_SNAPSHOT", "LINES_NAME",
           "NAME_NAME", "LIMIT_NAME", "PARAMETERS_NAME", "WIDEST_NAME",
           "PARAMETER_LIMIT_NAME", "FOLDED_NAME", "WORST_FOLDED_NAME",
           "FOLD_LIMIT_NAME", "CHAT_REFERENCE_NAME", "CHAT_SNAPSHOT",
           "UNANSWERED_NAME", "SILENT_NAME", "GRACE_NAME", "UNREVIEWED_NAME",
           "REVIEW_LIMIT_NAME", "SCREEN_STREAM"]
