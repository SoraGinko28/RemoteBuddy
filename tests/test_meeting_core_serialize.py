"""Tests for meeting_core_serialize: the commutative diagram.

    rule_reflect ∘ serialize ≡ meeting_core_reflect

Each case does two things:

1. The written viba code is a resident of that definition (``is_compliant``);
2. On the same path, both sides read exactly the same thing — one side is
   viba.rule.reflect reading the serialized result, the other is
   meeting_core_reflect reading the Python data directly.

Point 2 does not hand-pick a few paths; it walks the whole tree on the Python
side and compares every path that reaches a leaf.
"""

import datetime as _dt

import pytest

import meeting_adt as adt
import meeting_core_reflect as reflect
import meeting_core_serialize as serialize
from viba import viba_ast  # noqa: E402
from viba.rule import reflect as rule_reflect  # noqa: E402
from viba.rule.is_compliant import is_compliant  # noqa: E402
from viba.type import AstNodeType, Err, Ok, custom_module  # noqa: E402

# Known gap on the viba side: a generic alias (AppendOnlyList[T] := list[T]) does
# not accept a ListLiteral resident, while writing list[T] directly is. Once viba
# is fixed this table goes empty.
KNOWN_VIBA_GAPS = {
    "VersionedFile": "AppendOnlyList[T] := list[T] 展开后不认 ListLiteral 居民",
}

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


def _witness(name: str, data):
    """serialize -> the parsed-back witness body (the spelling of that data)."""
    given = serialize.serialize(name, data)
    assert isinstance(given, Ok), given
    module = viba_ast.parse(given.value)
    body = [definition for definition in module.body if definition.name == name]
    assert len(body) == 1
    return body[0].body


def _same_step(step):
    """Translate a step on the Python side into the same step on the
    viba.rule.reflect side."""
    return {"by_tag": rule_reflect.by_tag, "by_field_index": rule_reflect.by_field_index,
            "at_index": rule_reflect.at_index, "at_key": rule_reflect.at_key}[step.kind](
        step.value)


def _leaf_of(given):
    if isinstance(given, Err):
        return None
    return (given.value.kind, given.value.value)


@pytest.mark.parametrize("name,data", _samples(), ids=lambda x: None)
def test_the_written_code_is_a_resident_of_the_definition(name, data) -> None:
    body = _witness(name, data)
    given = is_compliant(AstNodeType(body, CORE_MODULE),
                         AstNodeType(CORE_DEFS[name].body, CORE_MODULE))
    if name in KNOWN_VIBA_GAPS:
        pytest.xfail(KNOWN_VIBA_GAPS[name])
    assert isinstance(given, Ok) and given.value is True, given


@pytest.mark.parametrize("name,data", _samples(), ids=lambda x: None)
def test_both_readers_read_the_same_thing(name, data) -> None:
    """Walk the whole tree: every path must read the same leaf on both sides."""
    body = _witness(name, data)
    definition = reflect._definition(name)
    access = reflect.access(definition)
    root = access.root(reflect._Record(data)).value
    witness_root = rule_reflect.access(definition).root(rule_reflect.Witness(body)).value
    compared = 0
    for node in access._walk(root):
        ours = access.leaf(node)
        theirs = rule_reflect.viba_get_by_path(
            witness_root, [_same_step(step) for step in node.path])
        assert _leaf_of(ours) == _leaf_of(theirs), (
            f"{name}: 路径 {[step.value for step in node.path]} "
            f"这边 {ours!r}，那边 {theirs!r}")
        compared += _leaf_of(ours) is not None
    assert compared > 0, f"{name}: 一条叶子都没对上"


def test_an_empty_container_has_no_viba_spelling(ctx=None) -> None:
    """Known gap: a literal needs at least one element, so an empty
    list / set / dict cannot be written yet."""
    given = serialize.serialize("VersionedFile", adt.VersionedFile(
        file_name=[adt.Versioned("a.py", _moment(), "alice")],
        file_meta=[],
        file_content=[adt.Versioned("print(1)", _moment(), "alice")]))
    assert isinstance(given, Err)
    assert "空的" in given.message


def test_a_chat_part_that_does_not_bind_is_refused() -> None:
    """Known gap: $raw_msg does not match RawMessage; better to write nothing
    than to force a nil."""
    given = serialize.serialize("ChatMessage", adt.ChatMessage(
        user_name="alice", time=_moment(), message=[adt.RawMessage("hi")]))
    assert isinstance(given, Err)
    assert "对得上的支" in given.message


def test_serialize_refuses_a_missing_step() -> None:
    given = serialize.serialize("datetime", {})
    assert isinstance(given, Err)
