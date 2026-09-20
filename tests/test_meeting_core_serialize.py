"""Tests for the commutative diagram, written by viba.serialize:

    rule_reflect ∘ serialize ≡ meeting_core_reflect

Each case does two things:

1. The written viba code is a resident of that definition (``is_compliant``);
2. On the same path, both sides read exactly the same thing — one side is
   viba.reflect reading the serialized result, the other is
   meeting_core_reflect reading the Python data directly.

Point 2 does not hand-pick a few paths; it walks the whole tree on the Python
side and compares every path that reaches a leaf.
"""

import datetime as _dt

import pytest

import meeting_adt as adt
import meeting_core_reflect as reflect
from viba import serialize  # noqa: E402
from viba import viba_ast  # noqa: E402
from viba.rule import reflect as rule_reflect  # noqa: E402
from viba.rule.is_compliant import is_compliant  # noqa: E402
from viba.type import AstNodeType, Err, Ok, custom_module  # noqa: E402

CORE_MODULE = custom_module(reflect._design_source())
CORE_DEFS = {definition.name: definition for definition in CORE_MODULE.module.body}


def _moment(microsecond: int = 7) -> _dt.datetime:
    return _dt.datetime(2026, 9, 18, 12, 0, 0, microsecond, tzinfo=_dt.timezone.utc)


def _samples():
    """(definition name, data): those binding in the "Stable types" chapter."""
    v = _moment()
    file_id = adt.FileId("f1", "a.py")
    file_ref = adt.VersionedFileReference(file_id=file_id, version_time=v)
    return [
        ("datetime", v),
        ("TextRegion", adt.TextRegion(0, 2)),
        ("UserName", "alice"),
        ("AgentRole", adt.AgentRole.GENERATOR),
        ("DataStatus", adt.DataStatus.READ_ONLY),
        ("Style", "bold"),
        ("Color", adt.Rgb(1, 2, 3)),
        ("TerminalTextSegment", adt.TerminalTextSegment(
            text="x", fg=adt.Rgb(1, 2, 3), bg=None, styles={"bold"})),
        ("FileState", {"name": "a.py", "meta": None, "content": "print(1)"}),
        ("FileState", {"name": "a.py", "meta": adt.DataStatus.READ_ONLY,
                       "content": "print(1)"}),
        ("FileId", file_id),
        ("VersionedFile", adt.VersionedFile(
            file_name=[adt.Versioned("a.py", v, "alice")],
            file_meta=[adt.Versioned(adt.DataStatus.READ_ONLY, v, "alice")],
            file_content=[adt.Versioned("print(1)", v, "alice"),
                          adt.Versioned("print(1)\nprint(2)", _moment(8), "bob")])),
        ("ScreenReference", adt.ScreenReference(
            capture_time=v, stream_name="screen", region=adt.TextRegion(0, 2))),
        ("ChatReference", adt.ChatReference(
            msg_time=v, user_name="alice", region=adt.TextRegion(0, 2))),
        ("ScoreReference", adt.ScoreReference(judge_time=v, judge_name="bob")),
        ("TerminalControlReference", adt.TerminalControlReference(
            start_time=v, user_name="alice", region=adt.TextRegion(0, 3))),
        ("VersionedFileReference", file_ref),
        ("VersionedFileContentReference", adt.VersionedFileContentReference(
            versioned_file_ref=file_ref, region=adt.TextRegion(0, 5))),
        ("ContentReference", adt.ChatReference(
            msg_time=v, user_name="alice", region=adt.TextRegion(0, 2))),
        ("ScoreMessage", adt.ScoreMessage(
            tag="correctness", score=1.0, comment="ok", judge_name="bob", time=v,
            reference=adt.ScoreReference(judge_time=v, judge_name="bob"))),
    ]


def _node(name: str, data):
    """The Python data bound into a node: the caller's side of the writer."""
    given = reflect.access.root(reflect._definition(name), reflect._Record(data))
    assert isinstance(given, Ok), given
    return given.ok_value


def _source(name: str, data):
    """serialize -> the viba source for that data."""
    return serialize.serialize(name, _node(name, data))


def _witness(name: str, data):
    """serialize -> the parsed-back witness body (the spelling of that data)."""
    given = _source(name, data)
    assert isinstance(given, Ok), given
    module = viba_ast.parse(given.ok_value)
    body = [definition for definition in module.body if definition.name == name]
    assert len(body) == 1
    return body[0].body


def _same_step(step):
    """Translate a step on the Python side into the same step on the
    viba.reflect side."""
    return {"by_tag": rule_reflect.by_tag, "by_field_index": rule_reflect.by_field_index,
            "at_index": rule_reflect.at_index, "at_key": rule_reflect.at_key}[step.kind](
        step.value)


def _leaf_of(given):
    """(kind, value): Err and Ok are told apart, the messages are not compared."""
    if isinstance(given, Err):
        return ("err", None)
    return ("ok", given.ok_value)


@pytest.mark.parametrize("name,data", _samples(), ids=lambda x: None)
def test_the_written_code_is_a_resident_of_the_definition(name, data) -> None:
    body = _witness(name, data)
    given = is_compliant(AstNodeType(body, CORE_MODULE),
                         AstNodeType(CORE_DEFS[name].body, CORE_MODULE))
    assert isinstance(given, Ok) and given.ok_value is True, given


@pytest.mark.parametrize("name,data", _samples(), ids=lambda x: None)
def test_both_readers_read_the_same_thing(name, data) -> None:
    """Walk the whole tree: every path must read the same leaf on both sides."""
    assert _leaves_read_alike(name, data) > 0, f"{name}: not one leaf matched"


def _leaves_read_alike(name: str, data) -> int:
    """Walk both sides and compare every path; how many leaves were read."""
    body = _witness(name, data)
    definition = reflect._definition(name)
    access = reflect.access
    root = access.root(definition, reflect._Record(data)).ok_value
    witness_root = rule_reflect.access.root(definition, rule_reflect.Witness(body)).ok_value
    compared = 0
    for node in access._walk(root):
        ours = access.leaf(node)
        theirs = rule_reflect.access.get_by_path(
            witness_root, [_same_step(step) for step in node.path])
        assert _leaf_of(ours) == _leaf_of(theirs), (
            f"{name}: path {[step.value for step in node.path]} "
            f"here {ours!r}, there {theirs!r}")
        compared += _leaf_of(ours)[0] == "ok"
    return compared


def test_an_empty_container_is_written_as_an_empty_literal() -> None:
    """A literal with no members is still a literal: ListLiteral[] and the
    other two. The design leans on this — an empty $styles set means "no extra
    styles"."""
    for type_expression, empty, written in (
            ("list[str]", [], "ListLiteral[]"),
            ("set[str]", set(), "SetLiteral[]"),
            ("dict[str, str]", {}, "DictLiteral[]")):
        access, definition = reflect.access_for(type_expression).ok_value
        root = access.root(definition, reflect._Record(empty))
        assert isinstance(root, Ok), root
        given = serialize.serialize("entry", root.ok_value)
        assert isinstance(given, Ok), given
        assert written in given.ok_value, given.ok_value
        assert viba_ast.parse(given.ok_value)            # and it reads back


def test_an_empty_list_among_the_chains_still_reads_the_same() -> None:
    """VersionedFile with no $file_meta: the empty chain is written, and both
    readers walk the rest of the record the same way."""
    data = adt.VersionedFile(
        file_name=[adt.Versioned("a.py", _moment(), "alice")],
        file_meta=[],
        file_content=[adt.Versioned("print(1)", _moment(), "alice")])
    given = _source("VersionedFile", data)
    assert isinstance(given, Ok), given
    assert "ListLiteral[]" in given.ok_value
    assert _leaves_read_alike("VersionedFile", data) > 0


def test_a_chat_part_that_does_not_bind_is_refused() -> None:
    """The design's chat part is $raw_msg str; the record layer hands over a
    RawMessage. Nothing is written rather than a forced nil."""
    given = _source("ChatMessage", adt.ChatMessage(
        user_name="alice", time=_moment(), message=[adt.RawMessage("hi")]))
    assert isinstance(given, Err)
    assert "no branch" in given.err_msg


def test_serialize_refuses_a_missing_step() -> None:
    given = _source("datetime", {})
    assert isinstance(given, Err)
