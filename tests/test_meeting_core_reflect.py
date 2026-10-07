"""Tests for meeting_core_reflect: the access side of viba-reflect.md on MEETING_ADT's stable types.

Design = the "Stable types" chapter of MEETING_ADT.md; data = the Python record in meeting_adt.py.
The starting point does not check the version (viba-reflect.md: root only pairs the
descriptor with the data), and this layer provides no checking either.
"""

import datetime as _dt

import pytest

import meeting_adt as adt
import meeting_core_reflect as reflect
from viba.type import VibaProgramErr, Ok  # noqa: E402  (Ok / VibaProgramErr in the protocol)


from viba.viba_type_descriptor import pool_find_definition  # noqa: E402  (description-side query)


def _definition_result(name: str):
    """The description-side protocol query PoolFindDefinition: fetch a
    definition descriptor by full name."""
    full_name = name if "." in name else f"meeting_core.{name}"
    return pool_find_definition(reflect._design_pool(), full_name)


def _definition(name: str):
    given = _definition_result(name)
    assert isinstance(given, Ok), given
    return given.ok_value


def _open(record, name: str):
    """Start a node per the protocol: access(definition).root(_Record(record))."""
    accessor = reflect.access
    given = accessor.root(_definition(name), reflect._Record(record))
    assert isinstance(given, Ok), given
    return given.ok_value


def _moment() -> _dt.datetime:
    return _dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _file_id(file_id: str = "f1") -> adt.FileId:
    return adt.FileId(file_id=file_id)


def _versioned_file() -> adt.VersionedFile:
    return adt.VersionedFile(
        file_name=[adt.Versioned("a.py", _moment(), "alice")],
        file_meta=[adt.Versioned(adt.DataStatus.READ_ONLY, _moment(), "alice")],
        file_content=[adt.Versioned("print(1)", _moment(), "bob")],
    )


# =====================================================================
# Design: compiled out of the "Stable types" chapter
# =====================================================================

class TestDesign:
    def test_source_is_the_stable_chapter(self) -> None:
        source = reflect._design_source()
        assert "FileId =" in source and "VersionedFile =" in source
        # the record layer chapter is not part of this design
        assert "MeetingData =" not in source
        assert "FileDesktop =" not in source

    def test_hash_is_sha256_of_the_source(self) -> None:
        import hashlib
        assert reflect._design_hash() == hashlib.sha256(reflect._design_source().encode("utf-8")).hexdigest()
        assert len(reflect._design_hash()) == 64

    def test_pool_carries_the_stable_definitions(self) -> None:
        names = [d.def_name for d in reflect._design_pool().files[0].definitions]
        assert len(names) == 29
        assert "FileId" in names and "ContentReference" in names and "Result" in names
        assert "ImageAttachment" in names                  # 随消息交出去的图片（ChatMessage 的一部分）
        assert "ScreenContent" in names and "FileState" in names
        assert "FileKvStore" not in names                 # the record layer types are not in the stable chapter
        assert {"Assert", "Appendix", "Hint"} <= set(names)   # Assert / Appendix / Hint are definitions too (units)

    def test_definition_by_short_and_full_name(self) -> None:
        assert _definition("FileId").full_name == "meeting_core.FileId"
        assert _definition("meeting_core.FileId").full_name == "meeting_core.FileId"

    def test_unknown_definition_is_an_err(self) -> None:
        assert isinstance(_definition_result("FileDesktop"), VibaProgramErr)


# =====================================================================
# Starting point: only pairs (descriptor, data); the version is left to the
# layer above
# =====================================================================

class TestRoot:
    def test_current_design_opens(self) -> None:
        node = _open(_file_id(), "FileId")
        assert isinstance(node, reflect.VibaNode)
        assert node.path == ()

    def test_root_pairs_the_descriptor_with_the_data(self) -> None:
        accessor = reflect.access
        given = accessor.root(_definition("FileId"), reflect._Record(_file_id()))
        assert given.ok_value.get_file_id().value == "f1"



# =====================================================================
# Six cells: has / get / leaf / length / keys
# =====================================================================

class TestSevenCells:
    def test_get_returns_a_node_that_remembers_the_address(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        inner = node.by_tag("file_name")
        assert [step.value for step in inner.path] == ["$file_name"]

    def test_leaf_kinds(self) -> None:
        """UserName is a sum: the value hangs under the $agent_name branch, so
        you have to walk in to reach a leaf."""
        node = _open(_versioned_file(), "VersionedFile")
        name = node.get_file_name().at_index(0).get_author()
        with pytest.raises(reflect.VibaReflectError, match="not a leaf"):
            name.leaf
        author = name.by_tag("agent_name")
        assert author.leaf == "alice"
        assert author.value == "alice"

    def test_time_is_a_python_datetime_and_reads_through_the_design(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        moment = node.get_file_name().at_index(0).get_time()
        assert isinstance(moment.data, _dt.datetime)
        assert moment.get_microsecond().value == _moment().microsecond

    def test_length_and_keys_only_for_containers(self) -> None:
        accessor = reflect.access
        node = accessor._unwrap(accessor.root(_definition("VersionedFile"),
                                             reflect._Record(_versioned_file())))
        chain = node.get_file_content()
        assert accessor.length(chain).ok_value == 1
        assert accessor.keys(chain).msg.startswith("this piece is not a dict")
        assert accessor.length(node).msg.startswith("this piece is not a container")

    def test_leaf_on_a_record_is_an_error(self) -> None:
        accessor = reflect.access
        node = accessor._unwrap(accessor.root(_definition("VersionedFile"),
                                             reflect._Record(_versioned_file())))
        assert "not a leaf" in accessor.leaf(node).msg


# =====================================================================
# Addresses: by_tag / by_field_index / at_index / at_key
# =====================================================================

class TestAddressing:
    def test_by_tag_and_at_index_walk_a_chain(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        content = node.by_tag("file_content").at_index(0).get_data()
        assert content.value == "print(1)"

    def test_at_index_walks_a_version_chain(self) -> None:
        history = adt.VersionedFile(
            file_name=[adt.Versioned("a.py")],
            file_meta=[adt.Versioned(None)],
            file_content=[adt.Versioned("body"), adt.Versioned("body\nmore")],
        )
        node = _open(history, "VersionedFile")
        assert node.get_file_content().at_index(0).get_data().value == "body"
        assert node.get_file_content().at_index(1).get_data().value == "body\nmore"

    def test_by_field_index_walks_an_untagged_branch(self) -> None:
        segment = adt.TerminalTextSegment(text="x", fg=adt.Rgb(1, 2, 3))
        fg = _open(segment, "TerminalTextSegment").get_fg()
        assert fg.by_field_index(0).by_tag("truecolor").get_r().value == 1
        assert fg.has_field_0() is True

    def test_unknown_coordinate_is_an_error(self) -> None:
        node = _open(_file_id(), "FileId")
        with pytest.raises(reflect.VibaReflectError, match="no such address"):
            node.by_tag("nope")

    def test_at_index_on_a_non_container_is_an_error(self) -> None:
        with pytest.raises(reflect.VibaReflectError):
            _open(_file_id(), "FileId").at_index(0)


# =====================================================================
# Sum types: class name vs definition name, enum value vs tag
# =====================================================================

class TestSumBranches:
    def test_data_status_enum_matches_its_tag(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        status = node.get_file_meta().at_index(0).get_data()
        assert status.has_read_only() is True
        assert status.has_deleted() is False

    def test_enum_with_a_dollar_value_matches_its_tag(self) -> None:
        node = _open(adt.AgentRole.ACTOR, "AgentRole")
        assert node.has_actor() is True
        assert node.has_critic() is False

    def test_nil_valued_optional_field_is_present_with_nil(self) -> None:
        """The field is present and the value is nil: has is true, and reading
        gives nil (not "this piece is missing")."""
        segment = adt.TerminalTextSegment(text="x", bg=None)
        node = _open(segment, "TerminalTextSegment")
        assert node.has_bg() is True
        assert reflect.access.leaf(node.get_bg()).ok_value is None
        assert node.has_field_1() is False               # positional members are only fg / bg

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
# The Python landing of section 5.4
# =====================================================================

class TestPythonLanding:
    def test_get_has_try_get_and_in(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        assert node.get_file_name().at_index(0).get_data().value == "a.py"
        assert node.has_file_name() is True
        assert node.try_get_nope().msg.startswith("the design has no such address")
        assert "file_name" in node
        assert "nope" not in node

    def test_missing_value_is_ok_none_not_an_exception(self) -> None:
        node = _open({"time": _moment(), "author": "alice"}, "Versioned")
        assert node.try_get_data().ok_value is None          # the data has no such key
        with pytest.raises(reflect.VibaReflectError, match="has no value"):
            node.get_data()

    def test_dir_lists_the_synthesised_names(self) -> None:
        assert "get_file_content" in dir(_open(_versioned_file(), "VersionedFile"))

    def test_sequence_access_and_iteration(self) -> None:
        node = _open(_versioned_file(), "VersionedFile").get_file_content()
        assert len(node) == 1 and node.is_dict is False
        assert node[0].get_data().value == "print(1)"
        assert [version.get_data().value for version in node] == ["print(1)"]
        styles = _open(adt.TerminalTextSegment(text="x", styles={"bold"}), "TerminalTextSegment").get_styles()
        assert [style.value for style in styles] == ["bold"]

    def test_set_types_are_recognised(self) -> None:
        segment = adt.TerminalTextSegment(text="x", styles={"bold"})
        assert _open(segment, "TerminalTextSegment").get_styles().is_set is True


# =====================================================================
# Convenience functions
# =====================================================================

class TestConvenience:
    def test_resolve_and_read(self) -> None:
        node = _open(_versioned_file(), "VersionedFile")
        path = [reflect.by_tag("file_content"), reflect.at_index(0),
                reflect.by_tag("data")]
        assert reflect.access.resolve(node, path).ok_value.value == "print(1)"
        assert reflect.access.get_by_path(node, path).ok_value == "print(1)"

    def test_a_missing_step_with_more_steps_left_is_an_err(self) -> None:
        """The piece is missing in the data and there are more steps after it:
        VibaProgramErr, None cannot be carried on with."""
        data = {"file_name": [adt.Versioned("a.py", _moment(), "alice")]}
        node = _open(data, "VersionedFile")
        assert node.try_get_file_meta().ok_value is None        # a single step: Ok(nil)
        given = reflect.access.resolve(node, [reflect.by_tag("file_meta"),
                                            reflect.at_index(0)])
        assert isinstance(given, VibaProgramErr) and given.msg == "this step has no value"

    def test_read_reports_a_missing_step(self) -> None:
        node = _open({"time": _moment(), "author": "alice"}, "Versioned")
        assert reflect.access.get_by_path(node, [reflect.by_tag("data")]).msg == "this step has no value"

    def test_resolve_reports_an_unknown_step(self) -> None:
        node = _open(_file_id(), "FileId")
        given = reflect.access.resolve(node, [reflect.by_tag("nope")])
        assert "no such address" in given.msg

    def test_list_fields_keeps_what_is_there(self) -> None:
        node = _open(_file_id(), "FileId")
        given = reflect.access.list_fields(node, _definition("FileId"))
        assert [member.value for member in given.ok_value] == ["f1"]


class TestResult:
    """Result is a stable type too: $ok / $err match by class name."""

    def test_ok_and_err_branches_match(self) -> None:
        from viba.type import VibaProgramErr, Ok

        ok = _open(Ok("body"), "Result")
        assert ok.has_ok() is True
        assert ok.has_err() is False
        err = _open(VibaProgramErr("boom"), "Result")
        assert err.has_err() is True
        assert err.has_ok() is False

    def test_ok_payload_is_the_value_itself(self) -> None:
        """The payload of $ok T is T itself: Ok has only one cell, and what
        comes out is that cell."""
        from viba.type import Ok

        node = _open(Ok("body"), "Result")
        assert node.get_ok().value == "body"

    def test_ok_nil_is_present_with_nil_inside(self) -> None:
        from viba.type import Ok

        node = _open(Ok(None), "Result")
        assert node.has_ok() is True
        assert reflect.access.leaf(node.get_ok()).ok_value is None

    def test_wrong_branch_raises(self) -> None:
        from viba.type import Ok

        node = _open(Ok("body"), "Result")
        with pytest.raises(reflect.VibaReflectError):
            node.by_tag("err")


# =====================================================================
# The whole tree: health check
# =====================================================================

class TestWalk:
    def test_a_versioned_file_walks_down_to_leaves(self) -> None:
        accessor = reflect.access
        root = accessor._unwrap(accessor.root(_definition("VersionedFile"),
                                             reflect._Record(_versioned_file())))
        leaves = []
        for node in accessor._walk(root):
            given = accessor.leaf(node)
            if isinstance(given, Ok):
                leaves.append(given.ok_value)
        assert {"a.py", "alice", "bob", "print(1)"} <= set(leaves)
