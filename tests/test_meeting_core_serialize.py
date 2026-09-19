"""meeting_core_serialize 的用例：那份交换图。

    rule_reflect ∘ serialize ≡ meeting_core_reflect

每个 case 干两件事：

1. 写出来的那段 viba 代码是那个定义的居民（``is_compliant``）；
2. 同一条路径，两边读出来一模一样——一边是 viba.rule.reflect 读序列化结果，
   一边是 meeting_core_reflect 直接读 Python 数据。

第 2 条不是挑几条路径手写，而是把 Python 那一侧的整棵树走一遍，
每条能读到叶子的路径都拿去两边对。
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

# viba 那边的已知缺口：泛型别名（AppendOnlyList[T] := list[T]）不认 ListLiteral 居民，
# 直接写 list[T] 就认。等 viba 修好，这张表就空掉。
KNOWN_VIBA_GAPS = {
    "VersionedFile": "AppendOnlyList[T] := list[T] 展开后不认 ListLiteral 居民",
}

CORE_MODULE = custom_module(reflect._design_source())
CORE_DEFS = {definition.name: definition for definition in CORE_MODULE.module.body}


def _moment(microsecond: int = 7) -> _dt.datetime:
    return _dt.datetime(2026, 9, 18, 12, 0, 0, microsecond, tzinfo=_dt.timezone.utc)


def _samples():
    """(定义名, 数据)：稳定类型章里绑得上的那些。"""
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
    """序列化 -> 解析回来的 witness 体（就是那份数据的写法）。"""
    given = serialize.serialize(name, data)
    assert isinstance(given, Ok), given
    module = viba_ast.parse(given.value)
    body = [definition for definition in module.body if definition.name == name]
    assert len(body) == 1
    return body[0].body


def _same_step(step):
    """把 Python 侧的一步换成 viba.rule.reflect 侧的同一步。"""
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
    """整棵树走一遍：每条路径两边读出来的叶子必须一样。"""
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
    """已知缺口：字面量至少要一个元素，空 list / set / dict 现在写不出来。"""
    given = serialize.serialize("VersionedFile", adt.VersionedFile(
        file_name=[adt.Versioned("a.py", _moment(), "alice")],
        file_meta=[],
        file_content=[adt.Versioned("print(1)", _moment(), "alice")]))
    assert isinstance(given, Err)
    assert "空的" in given.message


def test_a_chat_part_that_does_not_bind_is_refused() -> None:
    """已知缺口：$raw_msg 跟 RawMessage 对不上，宁可不写，也不硬凑 nil。"""
    given = serialize.serialize("ChatMessage", adt.ChatMessage(
        user_name="alice", time=_moment(), message=[adt.RawMessage("hi")]))
    assert isinstance(given, Err)
    assert "对得上的支" in given.message


def test_serialize_refuses_a_missing_step() -> None:
    given = serialize.serialize("datetime", {})
    assert isinstance(given, Err)
