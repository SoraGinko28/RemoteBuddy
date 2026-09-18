"""Tests for stable_adt_reflect: the access side of viba-reflect.md on MEETING_ADT's stable types.

设计 = MEETING_ADT.md 的「稳定类型」章；数据 = meeting_adt.py 那份 Python 记录。
"""

import datetime as _dt

import pytest

import meeting_adt as adt
import stable_adt_reflect as ref


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
        source = ref.design_source()
        assert "FileId :=" in source and "VersionedFile :=" in source
        # 记录层那一章不在这份设计里
        assert "MeetingData :=" not in source
        assert "FileDesktop :=" not in source

    def test_hash_is_sha256_of_the_source(self) -> None:
        import hashlib
        assert ref.design_hash() == hashlib.sha256(ref.design_source().encode("utf-8")).hexdigest()
        assert len(ref.design_hash()) == 64

    def test_pool_carries_the_stable_definitions(self) -> None:
        names = [d.def_name for d in ref.pool().files[0].definitions]
        assert len(names) == 25
        assert "FileId" in names and "ContentReference" in names

    def test_definition_by_short_and_full_name(self) -> None:
        assert ref.definition("FileId").full_name == "stable_adt.FileId"
        assert ref.definition("stable_adt.FileId").full_name == "stable_adt.FileId"

    def test_unknown_definition_raises(self) -> None:
        with pytest.raises(ref.StableReflectError):
            ref.definition("FileDesktop")


# =====================================================================
# 起点：先核版本
# =====================================================================

class TestRoot:
    def test_current_design_opens(self) -> None:
        node = ref.open_record(_file_id(), "FileId")
        assert isinstance(node, ref.StableNode)
        assert node.path == ()

    def test_data_without_hashes_is_refused(self) -> None:
        given = ref.access(ref.definition("FileId")).root(ref.StableData(_file_id()))
        assert given.message == "数据没有声明版本，拒绝给起点"

    def test_wrong_version_is_refused(self) -> None:
        data = ref.StableData(_file_id(), ["deadbeef"])
        given = ref.access(ref.definition("FileId")).root(data)
        assert "版本对不上" in given.message

    def test_version_matches_is_the_other_half(self) -> None:
        assert ref.stable_version_matches(ref.pool(), ref.StableData.current(_store())).value is True
        assert ref.stable_version_matches(ref.pool(), ref.StableData(_store(), ["nope"])).value is False


# =====================================================================
# 七格：has / get / leaf / length / keys
# =====================================================================

class TestSevenCells:
    def test_get_returns_a_node_that_remembers_the_address(self) -> None:
        node = ref.open_record(_versioned_file(), "VersionedFile")
        inner = node.by_tag("file_name")
        assert [step.value for step in inner.path] == ["$file_name"]

    def test_leaf_kinds(self) -> None:
        node = ref.open_record(_versioned_file(), "VersionedFile")
        author = node.get_file_name().get_author()
        assert author.leaf.kind == "str"
        assert author.value == "alice"
        assert author.leaf.value == "alice"

    def test_time_is_a_python_datetime_and_reads_through_the_design(self) -> None:
        node = ref.open_record(_versioned_file(), "VersionedFile")
        moment = node.get_file_name().get_time()
        assert isinstance(moment.data, _dt.datetime)
        assert moment.get_microsecond().value == _moment().microsecond

    def test_length_and_keys_only_for_containers(self) -> None:
        accessor = ref.access(ref.definition("FileKvStore"))
        node = accessor._unwrap(accessor.root(ref.StableData.current(_store())))
        inner = node.get_file_id2file()
        assert accessor.length(inner).value == 1
        assert accessor.keys(inner).value == ["f1"]
        assert accessor.length(node).message.startswith("这一段不是容器")
        assert accessor.keys(node).message.startswith("这一段不是 dict")

    def test_leaf_on_a_record_is_an_error(self) -> None:
        accessor = ref.access(ref.definition("VersionedFile"))
        node = accessor._unwrap(accessor.root(ref.StableData.current(_versioned_file())))
        assert "不是叶子" in accessor.leaf(node).message


# =====================================================================
# 地址：by_tag / by_field_index / at_index / at_key
# =====================================================================

class TestAddressing:
    def test_by_tag_and_at_key_walk_the_store(self) -> None:
        node = ref.open_record(_store(), "FileKvStore")
        content = node.by_tag("file_id2file").at_key("f1").get_file_content().get_data()
        assert content.value == "print(1)"

    def test_at_index_walks_a_list(self) -> None:
        history = adt.VersionedFile(
            file_name=adt.Versioned("a.py"),
            file_meta=adt.Versioned(None),
            file_content=adt.Versioned("body"),
        )
        node = ref.open_record(history, "VersionedFile")
        assert node.get_file_content().get_data().value == "body"

    def test_by_field_index_walks_an_untagged_branch(self) -> None:
        segment = adt.TerminalTextSegment(text="x", fg=adt.Rgb(1, 2, 3))
        fg = ref.open_record(segment, "TerminalTextSegment").get_fg()
        assert fg.by_field_index(0).by_tag("truecolor").get_r().value == 1
        assert fg.has_field_0() is True

    def test_unknown_coordinate_is_an_error(self) -> None:
        node = ref.open_record(_file_id(), "FileId")
        with pytest.raises(ref.StableReflectError, match="设计里没有这个坐标"):
            node.by_tag("nope")

    def test_at_index_on_a_non_container_is_an_error(self) -> None:
        with pytest.raises(ref.StableReflectError):
            ref.open_record(_file_id(), "FileId").at_index(0)


# =====================================================================
# 和类型：类名对定义名，枚举值对 tag
# =====================================================================

class TestSumBranches:
    def test_data_status_enum_matches_its_tag(self) -> None:
        node = ref.open_record(_versioned_file(), "VersionedFile")
        status = node.get_file_meta().get_data()
        assert status.has_read_only() is True
        assert status.has_deleted() is False

    def test_absent_optional_field_is_false(self) -> None:
        segment = adt.TerminalTextSegment(text="x", bg=None)
        node = ref.open_record(segment, "TerminalTextSegment")
        assert node.has_bg() is False
        assert node.has_field_1() is False

    def test_content_reference_branch_matches_by_class_name(self) -> None:
        message = adt.ChatMessage(
            user_name=adt.HUMAN_USER_NAME, time=_moment(),
            message=[adt.ScreenReference(
                capture_time=_moment(), stream_name="screen", region=adt.TextRegion(0, 3))])
        part = ref.open_record(message, "ChatMessage").get_message().at_index(0)
        reference = part.by_tag("reference")
        assert reference.has_screen_ref() is True
        assert reference.has_chat_ref() is False

    def test_wrong_branch_does_not_match(self) -> None:
        message = adt.ChatMessage(
            user_name=adt.HUMAN_USER_NAME, time=_moment(),
            message=[adt.ChatReference(
                msg_time=_moment(), user_name=adt.HUMAN_USER_NAME,
                region=adt.TextRegion(0, 3))])
        part = ref.open_record(message, "ChatMessage").get_message().at_index(0)
        assert part.has_reference() is True
        reference = part.by_tag("reference")
        assert reference.has_screen_ref() is False
        with pytest.raises(ref.StableReflectError):
            reference.by_tag("screen_ref")


# =====================================================================
# 第 5.4 节的 Python 落点
# =====================================================================

class TestPythonLanding:
    def test_get_has_try_get_and_in(self) -> None:
        node = ref.open_record(_versioned_file(), "VersionedFile")
        assert node.get_file_name().get_data().value == "a.py"
        assert node.has_file_name() is True
        assert node.try_get_nope().message.startswith("设计里没有这个坐标")
        assert "file_name" in node
        assert "nope" not in node

    def test_missing_value_is_ok_none_not_an_exception(self) -> None:
        node = ref.open_record(adt.Versioned(data=None), "Versioned")
        assert node.try_get_data().value is None
        with pytest.raises(ref.StableReflectError, match="没有值"):
            node.get_data()

    def test_dir_lists_the_synthesised_names(self) -> None:
        assert "get_file_id2file" in dir(ref.open_record(_store(), "FileKvStore"))

    def test_dict_access_and_sequence_iteration(self) -> None:
        node = ref.open_record(_store(), "FileKvStore").get_file_id2file()
        assert len(node) == 1 and node.is_dict is True
        assert node.keys() == ["f1"]
        assert node["f1"].get_file_name().get_data().value == "a.py"
        assert [value.get_file_name().get_data().value for _, value in node.items()] == ["a.py"]
        styles = ref.open_record(adt.TerminalTextSegment(text="x", styles={"bold"}),
                                 "TerminalTextSegment").get_styles()
        assert [style.value for style in styles] == ["bold"]

    def test_set_shapes_are_recognised(self) -> None:
        segment = adt.TerminalTextSegment(text="x", styles={"bold"})
        assert ref.open_record(segment, "TerminalTextSegment").get_styles().is_set is True


# =====================================================================
# 便利函数
# =====================================================================

class TestConvenience:
    def test_resolve_and_read(self) -> None:
        node = ref.open_record(_store(), "FileKvStore")
        path = [ref.by_tag("file_id2file"), ref.at_key("f1"),
                ref.by_tag("file_content"), ref.by_tag("data")]
        assert ref.stable_resolve(node, path).value.value == "print(1)"
        assert ref.stable_read(node, path).value.value == "print(1)"

    def test_read_reports_a_missing_step(self) -> None:
        node = ref.open_record(adt.Versioned(data=None), "Versioned")
        assert ref.stable_read(node, [ref.by_tag("data")]).message == "这一步没有值"

    def test_resolve_reports_an_unknown_step(self) -> None:
        node = ref.open_record(_file_id(), "FileId")
        given = ref.stable_resolve(node, [ref.by_tag("nope")])
        assert "设计里没有这个坐标" in given.message

    def test_list_fields_keeps_what_is_there(self) -> None:
        node = ref.open_record(_file_id(), "FileId")
        given = ref.stable_list_fields(node, ref.definition("FileId"))
        assert [member.value for member in given.value] == ["f1", "a.py"]


# =====================================================================
# 整棵树：体检
# =====================================================================

class TestWalk:
    def test_store_walks_down_to_leaves(self) -> None:
        accessor = ref.access(ref.definition("FileKvStore"))
        root = accessor._unwrap(accessor.root(ref.StableData.current(_store())))
        leaves = []
        for node in accessor._walk(root):
            given = accessor.leaf(node)
            if isinstance(given, ref.Ok):
                leaves.append(given.value.value)
        assert {"a.py", "alice", "bob", "print(1)"} <= set(leaves)
