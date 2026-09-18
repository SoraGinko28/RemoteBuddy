"""Tests for meeting_core_reflect: the access side of viba-reflect.md on MEETING_ADT's stable types.

设计 = MEETING_ADT.md 的「稳定类型」章；数据 = meeting_adt.py 那份 Python 记录。
起点不核版本（viba-reflect.md：root 只把描述符和数据配成对），这一层也不提供核对。
"""

import datetime as _dt

import pytest

import meeting_adt as adt
import meeting_core_reflect as reflect
from viba.type import Err, Ok  # noqa: E402  （协议里的 Ok / Err）


from viba.viba_type_descriptor import pool_find_definition  # noqa: E402  （描述侧的协议查询）


def _definition_result(name: str):
    """描述侧的协议查询 PoolFindDefinition：按全名取定义描述符。"""
    full_name = name if "." in name else f"meeting_core.{name}"
    return pool_find_definition(reflect._design_pool(), full_name)


def _definition(name: str):
    given = _definition_result(name)
    assert isinstance(given, Ok), given
    return given.value


def _open(record, name: str):
    """按协议起一个节点：access(definition).root(_Record(record))。"""
    accessor = reflect.access(_definition(name))
    given = accessor.root(reflect._Record(record))
    assert isinstance(given, Ok), given
    return given.value


def _moment() -> _dt.datetime:
    return _dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _file_id(file_id: str = "f1", entry_name: str = "a.py") -> adt.FileId:
    return adt.FileId(file_id=file_id, entry_name=entry_name)


def _versioned_file() -> adt.VersionedFile:
    return adt.VersionedFile(
        file_name=adt.Versioned("a.py", _moment(), "alice"),
        file_meta=adt.Versioned(adt.DataStatus.READ_ONLY, _moment(), "alice"),
        file_content=adt.Versioned("print(1)", _moment(), "bob"),
    )


def _store() -> adt.FileKvStore:
    return adt.FileKvStore(file_id2file={"f1": _versioned_file()})


# =====================================================================
# 设计：从 MEETING_ADT.md 的「稳定类型」章编译出来
# =====================================================================

class TestDesign:
    def test_source_is_the_stable_chapter(self) -> None:
        source = reflect._design_source()
        assert "FileId :=" in source and "VersionedFile :=" in source
        # 记录层那一章不在这份设计里
        assert "MeetingData :=" not in source
        assert "FileDesktop :=" not in source

    def test_hash_is_sha256_of_the_source(self) -> None:
        import hashlib
        assert reflect._design_hash() == hashlib.sha256(reflect._design_source().encode("utf-8")).hexdigest()
        assert len(reflect._design_hash()) == 64

    def test_pool_carries_the_stable_definitions(self) -> None:
        names = [d.def_name for d in reflect._design_pool().files[0].definitions]
        assert len(names) == 28
        assert "FileId" in names and "ContentReference" in names and "Result" in names
        assert "ScreenContent" in names and "FileState" in names

    def test_definition_by_short_and_full_name(self) -> None:
        assert _definition("FileId").full_name == "meeting_core.FileId"
        assert _definition("meeting_core.FileId").full_name == "meeting_core.FileId"

    def test_unknown_definition_is_an_err(self) -> None:
        assert isinstance(_definition_result("FileDesktop"), Err)


# =====================================================================
# 起点：只配 (描述符, 数据)，版本交给上层
# =====================================================================

class TestRoot:
    def test_current_design_opens(self) -> None:
        node = _open(_file_id(), "FileId")
        assert isinstance(node, reflect.VibaNode)
        assert node.path == ()

    def test_root_pairs_the_descriptor_with_the_data(self) -> None:
        accessor = reflect.access(_definition("FileId"))
        given = accessor.root(reflect._Record(_file_id()))
        assert given.value.get_file_id().value == "f1"



# =====================================================================
# 六格：has / get / leaf / length / keys
# =====================================================================

class TestSevenCells:
    def test_get_returns_a_node_that_remembers_the_address(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        inner = node.by_tag("file_name")
        assert [step.value for step in inner.path] == ["$file_name"]

    def test_leaf_kinds(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        author = node.get_file_name().get_author()
        assert author.leaf.kind == "str"
        assert author.value == "alice"
        assert author.leaf.value == "alice"

    def test_time_is_a_python_datetime_and_reads_through_the_design(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        moment = node.get_file_name().get_time()
        assert isinstance(moment.data, _dt.datetime)
        assert moment.get_microsecond().value == _moment().microsecond

    def test_length_and_keys_only_for_containers(self) -> None:
        accessor = reflect.access(_definition("FileKvStore"))
        node = accessor._unwrap(accessor.root(reflect._Record(_store())))
        inner = node.get_file_id2file()
        assert accessor.length(inner).value == 1
        assert accessor.keys(inner).value == ["f1"]
        assert accessor.length(node).message.startswith("这一段不是容器")
        assert accessor.keys(node).message.startswith("这一段不是 dict")

    def test_leaf_on_a_record_is_an_error(self) -> None:
        accessor = reflect.access(_definition("VersionedFile"))
        node = accessor._unwrap(accessor.root(reflect._Record(_versioned_file())))
        assert "不是叶子" in accessor.leaf(node).message


# =====================================================================
# 地址：by_tag / by_field_index / at_index / at_key
# =====================================================================

class TestAddressing:
    def test_by_tag_and_at_key_walk_the_store(self) -> None:
        node = _open(_store(), "FileKvStore")
        content = node.by_tag("file_id2file").at_key("f1").get_file_content().get_data()
        assert content.value == "print(1)"

    def test_at_index_walks_a_list(self) -> None:
        history = adt.VersionedFile(
            file_name=adt.Versioned("a.py"),
            file_meta=adt.Versioned(None),
            file_content=adt.Versioned("body"),
        )
        node = _open(history, "VersionedFile")
        assert node.get_file_content().get_data().value == "body"

    def test_by_field_index_walks_an_untagged_branch(self) -> None:
        segment = adt.TerminalTextSegment(text="x", fg=adt.Rgb(1, 2, 3))
        fg = _open(segment, "TerminalTextSegment").get_fg()
        assert fg.by_field_index(0).by_tag("truecolor").get_r().value == 1
        assert fg.has_field_0() is True

    def test_unknown_coordinate_is_an_error(self) -> None:
        node = _open(_file_id(), "FileId")
        with pytest.raises(reflect.VibaReflectError, match="设计里没有这个坐标"):
            node.by_tag("nope")

    def test_at_index_on_a_non_container_is_an_error(self) -> None:
        with pytest.raises(reflect.VibaReflectError):
            _open(_file_id(), "FileId").at_index(0)


# =====================================================================
# 和类型：类名对定义名，枚举值对 tag
# =====================================================================

class TestSumBranches:
    def test_data_status_enum_matches_its_tag(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        status = node.get_file_meta().get_data()
        assert status.has_read_only() is True
        assert status.has_deleted() is False

    def test_enum_with_a_dollar_value_matches_its_tag(self) -> None:
        node = _open(adt.AgentRole.GENERATOR, "AgentRole")
        assert node.has_generator() is True
        assert node.has_passive_discriminator() is False

    def test_absent_optional_field_is_false(self) -> None:
        segment = adt.TerminalTextSegment(text="x", bg=None)
        node = _open(segment, "TerminalTextSegment")
        assert node.has_bg() is False
        assert node.has_field_1() is False

    def test_content_reference_branch_matches_by_class_name(self) -> None:
        message = adt.ChatMessage(
            user_name=adt.HUMAN_USER_NAME, time=_moment(),
            message=[adt.ScreenReference(
                capture_time=_moment(), stream_name="screen", region=adt.TextRegion(0, 3))])
        part = _open(message, "ChatMessage").get_message().at_index(0)
        reference = part.by_tag("reference")
        assert reference.has_screen_ref() is True
        assert reference.has_chat_ref() is False

    def test_wrong_branch_does_not_match(self) -> None:
        message = adt.ChatMessage(
            user_name=adt.HUMAN_USER_NAME, time=_moment(),
            message=[adt.ChatReference(
                msg_time=_moment(), user_name=adt.HUMAN_USER_NAME,
                region=adt.TextRegion(0, 3))])
        part = _open(message, "ChatMessage").get_message().at_index(0)
        assert part.has_reference() is True
        reference = part.by_tag("reference")
        assert reference.has_screen_ref() is False
        with pytest.raises(reflect.VibaReflectError):
            reference.by_tag("screen_ref")


# =====================================================================
# 第 5.4 节的 Python 落点
# =====================================================================

class TestPythonLanding:
    def test_get_has_try_get_and_in(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        assert node.get_file_name().get_data().value == "a.py"
        assert node.has_file_name() is True
        assert node.try_get_nope().message.startswith("设计里没有这个坐标")
        assert "file_name" in node
        assert "nope" not in node

    def test_missing_value_is_ok_none_not_an_exception(self) -> None:
        node = _open(adt.Versioned(data=None), "Versioned")
        assert node.try_get_data().value is None
        with pytest.raises(reflect.VibaReflectError, match="没有值"):
            node.get_data()

    def test_dir_lists_the_synthesised_names(self) -> None:
        assert "get_file_id2file" in dir(_open(_store(), "FileKvStore"))

    def test_dict_access_and_sequence_iteration(self) -> None:
        node = _open(_store(), "FileKvStore").get_file_id2file()
        assert len(node) == 1 and node.is_dict is True
        assert node.keys() == ["f1"]
        assert node["f1"].get_file_name().get_data().value == "a.py"
        assert [value.get_file_name().get_data().value for _, value in node.items()] == ["a.py"]
        styles = _open(adt.TerminalTextSegment(text="x", styles={"bold"}), "TerminalTextSegment").get_styles()
        assert [style.value for style in styles] == ["bold"]

    def test_set_shapes_are_recognised(self) -> None:
        segment = adt.TerminalTextSegment(text="x", styles={"bold"})
        assert _open(segment, "TerminalTextSegment").get_styles().is_set is True


# =====================================================================
# 便利函数
# =====================================================================

class TestConvenience:
    def test_resolve_and_read(self) -> None:
        node = _open(_store(), "FileKvStore")
        path = [reflect.by_tag("file_id2file"), reflect.at_key("f1"),
                reflect.by_tag("file_content"), reflect.by_tag("data")]
        assert reflect.viba_resolve(node, path).value.value == "print(1)"
        assert reflect.viba_get_by_path(node, path).value.value == "print(1)"

    def test_read_reports_a_missing_step(self) -> None:
        node = _open(adt.Versioned(data=None), "Versioned")
        assert reflect.viba_get_by_path(node, [reflect.by_tag("data")]).message == "这一步没有值"

    def test_resolve_reports_an_unknown_step(self) -> None:
        node = _open(_file_id(), "FileId")
        given = reflect.viba_resolve(node, [reflect.by_tag("nope")])
        assert "设计里没有这个坐标" in given.message

    def test_list_fields_keeps_what_is_there(self) -> None:
        node = _open(_file_id(), "FileId")
        given = reflect.viba_list_fields(node, _definition("FileId"))
        assert [member.value for member in given.value] == ["f1", "a.py"]


class TestResult:
    """Result 也在稳定类型里：$ok / $err 两个分支按类名对位。"""

    def test_ok_and_err_branches_match(self) -> None:
        from viba.type import Err, Ok

        ok = _open(Ok("body"), "Result")
        assert ok.has_ok() is True
        assert ok.has_err() is False
        err = _open(Err("boom"), "Result")
        assert err.has_err() is True
        assert err.has_ok() is False

    def test_ok_payload_is_the_value_itself(self) -> None:
        """$ok T 的载荷就是 T：Ok 只有一格，取出来的是那一格。"""
        from viba.type import Ok

        node = _open(Ok("body"), "Result")
        assert node.get_ok().value == "body"

    def test_ok_nil_is_present_with_nil_inside(self) -> None:
        from viba.type import Ok

        node = _open(Ok(None), "Result")
        assert node.has_ok() is True
        assert node.get_ok().value is None

    def test_wrong_branch_raises(self) -> None:
        from viba.type import Ok

        node = _open(Ok("body"), "Result")
        with pytest.raises(reflect.VibaReflectError):
            node.by_tag("err")


# =====================================================================
# 整棵树：体检
# =====================================================================

class TestWalk:
    def test_store_walks_down_to_leaves(self) -> None:
        accessor = reflect.access(_definition("FileKvStore"))
        root = accessor._unwrap(accessor.root(reflect._Record(_store())))
        leaves = []
        for node in accessor._walk(root):
            given = accessor.leaf(node)
            if isinstance(given, Ok):
                leaves.append(given.value.value)
        assert {"a.py", "alice", "bob", "print(1)"} <= set(leaves)
