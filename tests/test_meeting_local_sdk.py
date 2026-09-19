"""Test cases for meeting_local_sdk: every test_case_* only takes (ctx, sdk, reflect).

The style is practice for future metric getters:

    import meeting_local_sdk
    import meeting_core_reflect

    def prepare_meeting_data_as_ctx():
        return ...

    test_case_xxx(prepare_meeting_data_as_ctx(), sdk, reflect)
    test_case_yyy(prepare_meeting_data_as_ctx(), sdk, reflect)

Three rules:

1. **The whole file touches the record layer only in
   prepare_meeting_data_as_ctx** (meeting_adt); values like references are
   always built with the ``make_*`` constructors on ``sdk`` (they are the
   interface in meeting_local_sdk.viba).
2. **Test cases must not know the details of meeting data** — data can only be
   fetched with ``sdk`` (meeting_local_sdk), and whatever comes back can only be read
   through ``reflect`` (meeting_core_reflect) per the design.
3. **Results are read through the design's interface**: Result is a type in
   meeting_core, the SDK does not expose its own Ok / Err; always go through
   ``result.has_ok()`` / ``result.get_ok().value`` (on failure ``has_err()`` /
   ``get_err().value``). When the payload is a sequence or a record, take the
   node's ``$data`` and then read it piece by piece with reflection — the T of
   ``$ok T`` is empty at Result, so only the caller knows which type it wants.

The three fixtures below let pytest drive these cases with the same signature.
"""

import datetime as _dt

import pytest

import meeting_core_reflect
import meeting_local_sdk
import meeting_adt  # only prepare_meeting_data_as_ctx uses this
from viba.type import Ok  # noqa: E402  (Ok / Err in the protocol)
from viba.viba_type_descriptor import pool_find_definition  # noqa: E402

# In the design datetime is in "microseconds" ($microsecond int), so these
# moments all differ in microsecond count and the cases can tell them apart.
_T0 = _dt.datetime(2026, 9, 18, 12, 0, 0, 1, tzinfo=_dt.timezone.utc)
_T1 = _dt.datetime(2026, 9, 18, 12, 0, 1, 2, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 9, 18, 12, 0, 2, 3, tzinfo=_dt.timezone.utc)
_ALICE = "alice"
_BOB = "bob"


def prepare_meeting_data_as_ctx():
    """A meeting record, handed to the cases as ctx."""
    adt = meeting_adt
    text_region = meeting_local_sdk.make_text_region

    file_id = adt.FileId(file_id="f1", entry_name="a.py")
    file_ref = meeting_local_sdk.make_versioned_file_reference(file_id, _T0)
    # one that is too early: that version of a.py only exists from _T0
    stale_file_ref = meeting_local_sdk.make_versioned_file_reference(
        file_id, _T0 - _dt.timedelta(seconds=1))
    controls = [meeting_local_sdk.make_terminal_control_reference(_T0, _ALICE, text_region(0, 3)),
                meeting_local_sdk.make_terminal_control_reference(_T2, None, text_region(0, 3))]

    # alice's first message carries all six kinds of reference; later cases
    # pull them out of the data
    first_message = adt.ChatMessage(user_name=_ALICE, time=_T1, message=[
        adt.RawMessage("hi"),
        meeting_local_sdk.make_screen_reference(_T1, "screen", text_region(0, 2)),
        meeting_local_sdk.make_chat_reference(_T2, _BOB, text_region(0, 2)),
        meeting_local_sdk.make_score_reference(_T2, _BOB),
        controls[0],
        controls[1],
        file_ref,
        stale_file_ref,
        meeting_local_sdk.make_versioned_file_content_reference(file_ref, text_region(0, 5)),
    ])

    controls_chunk = adt.TerminalControlStreamChunk(chunks=[
        adt.TerminalControl(tmux_window_name="w", tmux_pane=0, user_name=_ALICE,
                            start_time=_T0),
        adt.TerminalControl(tmux_window_name="w", tmux_pane=0, user_name="", start_time=_T2),
    ])
    screens = adt.ScreenStreamChunk(owner_user_name=_ALICE, chunks=[
        adt.ScreenCapture(screen_data="ab\ncd", terminal_text_content=[], cursor_x=0,
                          cursor_y=0, screen_width=2, screen_height=2, captured_at=_T1),
    ])
    chats = adt.ChatStreamChunk(chunks=[
        first_message,
        adt.ChatMessage(user_name=_BOB, time=_T2, message=[adt.RawMessage("yo")]),
    ])
    scores = adt.ScoreStreamChunk(chunks=[
        adt.ScoreMessage(tag="correctness", score=1.0, comment="good", judge_name=_BOB,
                         time=_T2,
                         reference=meeting_local_sdk.make_chat_reference(
                             _T1, _ALICE, text_region(0, 2))),
    ])

    fold = adt.FileFold(fold_name="pkg",
                        member_file_ids=[adt.FileId(file_id="f2", entry_name="b.py")])
    workspace = adt.FileWorkspace(member_files=[file_id, fold], workspace_name="ws",
                                  workspace_status=None)          # default = writable
    locked = adt.FileWorkspace(member_files=[], workspace_name="locked",
                               workspace_status=adt.DataStatus.READ_ONLY)
    desktop = adt.FileDesktop(workspaces=[workspace, locked])
    desktop.reorder_indexes = adt.Versioned(adt.identity_desktop_order(desktop), _T0, _ALICE)
    store = adt.FileKvStore(file_id2file={
        "f1": adt.VersionedFile(
            file_name=[adt.Versioned("a.py", _T0, _ALICE)],
            file_meta=[adt.Versioned(adt.DataStatus.READ_ONLY, _T0, _ALICE)],
            file_content=[adt.Versioned("print(1)", _T0, _ALICE)]),
        "f2": adt.VersionedFile(
            file_name=[adt.Versioned("b.py", _T2, _BOB)],
            file_meta=[adt.Versioned(adt.DataStatus.DELETED, _T2, _BOB)],
            file_content=[adt.Versioned("print(2)", _T2, _BOB)]),
    })

    return adt.MeetingData(
        meeting_uid="m1",
        meeting_name="case",
        agent_name2role={_ALICE: adt.AgentRole.GENERATOR,
                         _BOB: adt.AgentRole.PASSIVE_DISCRIMINATOR},
        sorted_stream_chunks=[screens, chats, scores, controls_chunk],
        file_kv_store=store,
        file_desktop=desktop,
    )


# ----------------------------------------------------------------------
# Small helpers for reading and fetching input: only sdk + reflect
# ----------------------------------------------------------------------


def _definition(name: str):
    """The description-side protocol query PoolFindDefinition: fetch a
    definition descriptor by full name."""
    full_name = name if "." in name else f"meeting_core.{name}"
    given = pool_find_definition(meeting_core_reflect._design_pool(), full_name)
    assert isinstance(given, Ok), given
    return given.value


def _node(value, name, reflect):
    """Start a node per the protocol: access(definition).root(_Record(value))."""
    given = reflect.access(_definition(name)).root(reflect._Record(value))
    assert isinstance(given, Ok), "cannot open the root"
    return given.value


def _result(value, reflect):
    return _node(value, "Result", reflect)


def _ok_node(value, reflect):
    """The node of the $ok branch."""
    result = _result(value, reflect)
    assert result.has_ok() is True, "this is not $ok"
    return result.get_ok()


def _ok_value(value, reflect):
    """Scalar payload: result.get_ok().value."""
    return _ok_node(value, reflect).value


def _ok_data(value, reflect):
    """When the payload is a datetime / sequence / record, take $data from the
    node of the $ok branch."""
    return _ok_node(value, reflect).data


def _err_message(value, reflect):
    result = _result(value, reflect)
    assert result.has_err() is True, "this is not $err"
    return result.get_err().value


_REFERENCE_KINDS = ("screen_ref", "chat_ref", "score_ref", "terminal_control_ref",
                    "versioned_file_ref", "versioned_file_content_ref")


def _reference_kind(reference, reflect):
    """Which of the six branches a reference is."""
    node = _node(reference, "ContentReference", reflect)
    for kind in _REFERENCE_KINDS:
        if getattr(node, f"has_{kind}")():
            return kind
    raise AssertionError("not one of the reference kinds")


def _references_of_kind(references, kind, reflect):
    return [r for r in references if _reference_kind(r, reflect) == kind]


def _one_reference_of_kind(references, kind, reflect):
    found = _references_of_kind(references, kind, reflect)
    assert len(found) == 1, f"{kind}: found {len(found)}"
    return found[0]


def _message_time(message, reflect):
    return _node(message, "ChatMessage", reflect).get_time().data


def _messages(ctx, sdk, reflect):
    """The chat messages in the meeting (interval endpoints come from sdk,
    no hard-coded moments)."""
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    return _ok_node(sdk.chat_messages(ctx, first, last), reflect).data


def _message_of(messages, user_name, reflect):
    for message in messages:
        node = _node(message, "ChatMessage", reflect)
        if node.get_user_name().by_tag("agent_name").value == user_name:
            return message
    raise AssertionError(f"no message from {user_name} in the data")


@pytest.fixture(name="sdk")
def _sdk_module():
    return meeting_local_sdk


@pytest.fixture(name="reflect")
def _reflect_module():
    return meeting_core_reflect


@pytest.fixture
def ctx():
    return prepare_meeting_data_as_ctx()


# =====================================================================
# Time and streams
# =====================================================================

def test_case_now_is_a_moment_and_never_repeats(ctx, sdk, reflect):
    first = _node(_ok_data(sdk.now(ctx), reflect), "datetime", reflect)
    second = _node(_ok_data(sdk.now(ctx), reflect), "datetime", reflect)
    assert first.get_microsecond().value >= 0
    assert second.data > first.data


def test_case_first_and_last_chunk_time_bound_the_meeting(ctx, sdk, reflect):
    first = _node(_ok_data(sdk.first_chunk_time(ctx), reflect), "datetime", reflect)
    last = _node(_ok_data(sdk.last_chunk_time(ctx), reflect), "datetime", reflect)
    assert first.get_microsecond().value == 1
    assert last.get_microsecond().value == 3


def test_case_chunk_times_follow_the_streams(ctx, sdk, reflect):
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    payload = _ok_node(sdk.chunk_times(ctx, first, last), reflect).data
    moments = [_node(m, "datetime", reflect).get_microsecond().value for m in payload]
    # control starts at _T0; one screen and one chat are both at _T1;
    # the score is at _T2
    assert moments == [1, 2, 2, 3]


def test_case_chunk_times_can_be_narrowed(ctx, sdk, reflect):
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    payload = _ok_node(sdk.chunk_times(ctx, last, last), reflect).data
    assert len(payload) == 1
    assert _node(payload[0], "datetime", reflect).get_microsecond().value == 3


def test_case_streams_between_lists_the_stream_names(ctx, sdk, reflect):
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    names = _ok_node(sdk.streams_between(ctx, first, last), reflect).data
    assert sorted(names) == ["chat", "score", "screen", "terminal_control"]


# =====================================================================
# Chat and scores
# =====================================================================

def test_case_chat_messages_read_through_the_design(ctx, sdk, reflect):
    messages = _messages(ctx, sdk, reflect)
    assert len(messages) == 2
    first = _node(messages[0], "ChatMessage", reflect)
    assert first.get_user_name().by_tag("agent_name").value == "alice"
    assert first.get_time().get_microsecond().value == 2


def test_case_chat_messages_can_be_narrowed(ctx, sdk, reflect):
    moment = _message_time(_message_of(_messages(ctx, sdk, reflect), _BOB, reflect), reflect)
    narrow = _ok_node(sdk.chat_messages(ctx, moment, moment), reflect).data
    assert len(narrow) == 1
    assert _node(narrow[0], "ChatMessage", reflect).get_user_name().by_tag(
        "agent_name").value == "bob"


def test_case_chat_message_at_is_unique_per_time_and_user(ctx, sdk, reflect):
    moment = _message_time(_message_of(_messages(ctx, sdk, reflect), _BOB, reflect), reflect)
    message = _ok_node(sdk.chat_message_at(ctx, moment, _BOB), reflect).data
    assert _node(message, "ChatMessage", reflect).get_user_name().by_tag(
        "agent_name").value == "bob"
    assert "alice" in _err_message(sdk.chat_message_at(ctx, moment, _ALICE), reflect)


def test_case_message_references_carry_all_six_kinds(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    kinds = sorted(_reference_kind(r, reflect) for r in references)
    assert set(kinds) == set(_REFERENCE_KINDS)


def test_case_resolve_chat_reference(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    ref = _one_reference_of_kind(references, "chat_ref", reflect)
    message = _ok_node(sdk.resolve_chat_reference(ctx, ref), reflect).data
    node = _node(message, "ChatMessage", reflect)
    assert node.get_user_name().by_tag("agent_name").value == "bob"
    assert node.get_time().get_microsecond().value == 3


def test_case_resolve_chat_content_reference_takes_the_region(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    ref = _one_reference_of_kind(references, "chat_ref", reflect)
    assert _ok_value(sdk.resolve_chat_content_reference(ctx, ref), reflect) == "yo"


def test_case_resolve_score_reference(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    ref = _one_reference_of_kind(references, "score_ref", reflect)
    message = _ok_node(sdk.resolve_score_reference(ctx, ref), reflect).data
    node = _node(message, "ScoreMessage", reflect)
    assert node.get_tag().value == "correctness"
    assert node.get_score().value == 1.0


def test_case_score_message_reference_points_at_a_chat_message(ctx, sdk, reflect):
    moment = _message_time(_message_of(_messages(ctx, sdk, reflect), _BOB, reflect), reflect)
    ref = _ok_node(sdk.score_message_reference(ctx, moment, _BOB), reflect).data
    assert _reference_kind(ref, reflect) == "chat_ref"
    message = _ok_node(sdk.resolve_chat_reference(ctx, ref), reflect).data
    assert _node(message, "ChatMessage", reflect).get_user_name().by_tag(
        "agent_name").value == "alice"


def test_case_score_message_reference_without_that_score_is_an_error(ctx, sdk, reflect):
    moment = _message_time(_message_of(_messages(ctx, sdk, reflect), _BOB, reflect), reflect)
    assert _err_message(sdk.score_message_reference(ctx, moment, _ALICE), reflect)


def test_case_make_chat_reference_points_back_at_the_message(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    ref = sdk.make_chat_reference(_message_time(alice, reflect), _ALICE,
                                  sdk.make_text_region(0, 2))
    assert _ok_value(sdk.resolve_chat_content_reference(ctx, ref), reflect) == "hi"


# =====================================================================
# Screen
# =====================================================================

def _screen_reference(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    return _one_reference_of_kind(references, "screen_ref", reflect)


def test_case_screen_at_returns_the_state(ctx, sdk, reflect):
    ref = _screen_reference(ctx, sdk, reflect)
    capture_time = _node(ref, "ScreenReference", reflect).get_capture_time().data
    state = _ok_node(sdk.screen_at(ctx, capture_time), reflect).data
    node = _node(state, "ScreenContent", reflect)
    assert node.get_text().value == "ab\ncd"
    assert node.get_width().value == 2 and node.get_height().value == 2
    assert node.get_captured_at().get_microsecond().value == 2


def test_case_screen_at_without_that_moment_is_an_error(ctx, sdk, reflect):
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    assert "screen stream" in _err_message(sdk.screen_at(ctx, last), reflect)


def test_case_resolve_screen_reference(ctx, sdk, reflect):
    state = _ok_node(sdk.resolve_screen_reference(ctx, _screen_reference(ctx, sdk, reflect)),
                     reflect).data
    assert _node(state, "ScreenContent", reflect).get_text().value == "ab\ncd"


# =====================================================================
# People
# =====================================================================

def test_case_participants(ctx, sdk, reflect):
    names = _ok_node(sdk.participants(ctx), reflect).data
    assert sorted(_node(n, "UserName", reflect).by_tag("agent_name").value
                  for n in names) == ["alice", "bob"]


def test_case_roles(ctx, sdk, reflect):
    roster = _ok_node(sdk.roles(ctx), reflect).data
    assert _node(roster["alice"], "AgentRole", reflect).has_generator() is True
    assert _node(roster["bob"], "AgentRole", reflect).has_passive_discriminator() is True


def test_case_owner_at_reads_the_control_intervals(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    # at the moment alice speaks (_T1) the terminal is still hers
    assert _ok_value(sdk.owner_at(ctx, _message_time(alice, reflect)), reflect) == "alice"
    handback = _control_references(ctx, sdk, reflect)[1]
    assert _ok_value(
        sdk.owner_at(ctx, _node(handback, "TerminalControlReference",
                                reflect).get_start_time().data), reflect) is None


def _control_references(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    controls = _references_of_kind(references, "terminal_control_ref", reflect)
    assert len(controls) == 2
    return controls


def test_case_control_owner_of_a_handover(ctx, sdk, reflect):
    takeover, handback = _control_references(ctx, sdk, reflect)
    assert _ok_value(sdk.control_owner_of(ctx, takeover), reflect) == "alice"
    assert _ok_value(sdk.control_owner_of(ctx, handback), reflect) is None


# =====================================================================
# File layer
# =====================================================================

def test_case_file_id_by_path_finds_a_direct_file(ctx, sdk, reflect):
    file_id = _ok_node(sdk.file_id_by_path(ctx, "/ws/a.py"), reflect).data
    node = _node(file_id, "FileId", reflect)
    assert node.get_entry_name().value == "a.py"


def test_case_file_id_by_path_finds_a_file_in_a_fold(ctx, sdk, reflect):
    file_id = _ok_node(sdk.file_id_by_path(ctx, "/ws/pkg/b.py"), reflect).data
    node = _node(file_id, "FileId", reflect)
    assert node.get_file_id().value == "f2"


def test_case_file_id_by_path_rejects_unknown_paths(ctx, sdk, reflect):
    for path in ("/ws/nope.py", "a.py", "/ws/pkg/x/y.py"):
        assert _err_message(sdk.file_id_by_path(ctx, path), reflect)


def test_case_file_datetimes_lists_the_versions(ctx, sdk, reflect):
    file_id = _file_id_of(ctx, sdk, reflect, "/ws/a.py")
    moments = _ok_node(sdk.file_datetimes(ctx, file_id), reflect).data
    assert [_node(m, "datetime", reflect).get_microsecond().value for m in moments] == [1]


def test_case_latest_file_gives_the_current_state(ctx, sdk, reflect):
    file_id = _ok_node(sdk.file_id_by_path(ctx, "/ws/a.py"), reflect).data
    state = _ok_node(sdk.latest_file(ctx, file_id), reflect).data
    node = _node(state, "FileState", reflect)
    assert node.get_name().value == "a.py"
    assert _node(node.get_meta().data, "DataStatus", reflect).has_read_only() is True
    assert node.get_content().value == "print(1)"


def test_case_files_at_only_lists_what_existed(ctx, sdk, reflect):
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    before = _ok_node(sdk.files_at(ctx, first), reflect).data
    assert sorted(before) == ["f1"]                      # b.py's version is later
    after = _ok_node(sdk.files_at(ctx, last), reflect).data
    assert sorted(after) == ["f1", "f2"]
    assert _node(after["f2"], "FileState", reflect).get_content().value == "print(2)"


def _file_references(ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    files = _references_of_kind(references, "versioned_file_ref", reflect)
    assert len(files) == 2
    return files


def test_case_resolve_versioned_file_reference_is_as_of(ctx, sdk, reflect):
    resolved = [r for r in _file_references(ctx, sdk, reflect)
                if _result(sdk.resolve_versioned_file_reference(ctx, r), reflect).has_ok()]
    # two file references in the data: one caught that version, one is too
    # early
    assert len(resolved) == 1
    state = _ok_node(sdk.resolve_versioned_file_reference(ctx, resolved[0]), reflect).data
    assert _node(state, "FileState", reflect).get_name().value == "a.py"


def test_case_resolve_versioned_file_content_reference_takes_the_region(
        ctx, sdk, reflect):
    alice = _message_of(_messages(ctx, sdk, reflect), _ALICE, reflect)
    references = _ok_node(sdk.chat_message_references(ctx, _message_time(alice, reflect),
                                                      _ALICE), reflect).data
    ref = _one_reference_of_kind(references, "versioned_file_content_ref", reflect)
    given = sdk.resolve_versioned_file_content_reference(ctx, ref)
    assert _ok_value(given, reflect) == "print"


def test_case_make_versioned_file_content_reference_takes_the_region(ctx, sdk, reflect):
    file_id = _ok_node(sdk.file_id_by_path(ctx, "/ws/a.py"), reflect).data
    file_ref = sdk.make_versioned_file_reference(file_id,
                                                 _ok_data(sdk.first_chunk_time(ctx), reflect))
    ref = sdk.make_versioned_file_content_reference(file_ref, sdk.make_text_region(0, 5))
    assert _ok_value(sdk.resolve_versioned_file_content_reference(ctx, ref),
                     reflect) == "print"


# =====================================================================
# Opening a file: the path answers its FileId, or an Err
# =====================================================================


def _file_id_of(ctx, sdk, reflect, path):
    return _ok_node(sdk.file_id_by_path(ctx, path), reflect).data


def test_case_a_path_that_is_not_there_is_an_error(ctx, sdk, reflect):
    for path in ("a.py", "/nope/c.py", "/ws/nofold/c.py", "/ws/pkg", "/ws/c.py"):
        assert _err_message(sdk.file_id_by_path(ctx, path), reflect), path


def test_case_read_text_is_latest_file(ctx, sdk, reflect):
    file_id = _file_id_of(ctx, sdk, reflect, "/ws/a.py")
    read = _ok_node(sdk.read_text(ctx, file_id), reflect).data
    latest = _ok_node(sdk.latest_file(ctx, file_id), reflect).data
    assert read == latest


