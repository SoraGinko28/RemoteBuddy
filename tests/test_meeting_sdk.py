"""meeting_sdk 的用例：每个 test_case_* 只吃 (ctx, sdk, reflect)。

写法是给以后的 metric getter 练手的：

    import meeting_sdk
    import meeting_core_reflect

    def prepare_meeting_data_as_ctx():
        return ...

    test_case_xxx(prepare_meeting_data_as_ctx(), sdk, reflect)
    test_case_yyy(prepare_meeting_data_as_ctx(), sdk, reflect)

三条规矩：

1. **整个文件只有 prepare_meeting_data_as_ctx 一处碰记录层**（meeting_adt）；
   引用这类值一律用 ``sdk`` 上的 ``make_*`` 造（它们是 meeting_sdk.viba 里的接口）。
2. **用例里不许认识 meeting data 的细节**——数据只能用 ``sdk``（meeting_sdk）取，
   取回来的东西只能用 ``reflect``（meeting_core_reflect）按设计读。
3. **结果按设计的接口读**：Result 是 meeting_core 里的类型，SDK 不暴露自己的 Ok / Err；
   一律走 ``result.has_ok()`` / ``result.get_ok().value``（失败走 ``has_err()`` /
   ``get_err().value``）。载荷是序列或记录时，拿节点的 ``$data`` 再用反射逐段读——
   ``$ok T`` 的 T 在 Result 那里是空的，只能靠调用方知道自己要的是什么类型。

底下三个 fixture 是为了让 pytest 按同一签名驱动这些用例。
"""

import datetime as _dt

import pytest

import meeting_core_reflect
import meeting_sdk
import meeting_adt  # 只有 prepare_meeting_data_as_ctx 用它
from viba.type import Ok  # noqa: E402  （协议里的 Ok / Err）
from viba.viba_type_descriptor import pool_find_definition  # noqa: E402

# 设计里的 datetime 是"微秒"（$microsecond int），所以这几个时刻的微秒各不相同，
# 用例才读得出谁是谁。
_T0 = _dt.datetime(2026, 9, 18, 12, 0, 0, 1, tzinfo=_dt.timezone.utc)
_T1 = _dt.datetime(2026, 9, 18, 12, 0, 1, 2, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 9, 18, 12, 0, 2, 3, tzinfo=_dt.timezone.utc)
_ALICE = "alice"
_BOB = "bob"


def prepare_meeting_data_as_ctx():
    """一份会议记录，当 ctx 交给用例。"""
    adt = meeting_adt
    text_region = meeting_sdk.make_text_region

    file_id = adt.FileId(file_id="f1", entry_name="a.py")
    file_ref = meeting_sdk.make_versioned_file_reference(file_id, _T0)
    # 太早的一条：a.py 那一版是 _T0 才有的
    stale_file_ref = meeting_sdk.make_versioned_file_reference(
        file_id, _T0 - _dt.timedelta(seconds=1))
    controls = [meeting_sdk.make_terminal_control_reference(_T0, _ALICE, text_region(0, 3)),
                meeting_sdk.make_terminal_control_reference(_T2, None, text_region(0, 3))]

    # alice 的第一条消息身上带着六种引用，后面的用例从数据里把它们取出来
    first_message = adt.ChatMessage(user_name=_ALICE, time=_T1, message=[
        adt.RawMessage("hi"),
        meeting_sdk.make_screen_reference(_T1, "screen", text_region(0, 2)),
        meeting_sdk.make_chat_reference(_T2, _BOB, text_region(0, 2)),
        meeting_sdk.make_score_reference(_T2, _BOB),
        controls[0],
        controls[1],
        file_ref,
        stale_file_ref,
        meeting_sdk.make_versioned_file_content_reference(file_ref, text_region(0, 5)),
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
                         reference=meeting_sdk.make_chat_reference(
                             _T1, _ALICE, text_region(0, 2))),
    ])

    fold = adt.FileFold(fold_name="pkg",
                        member_file_ids=[adt.FileId(file_id="f2", entry_name="b.py")])
    workspace = adt.FileWorkspace(member_files=[file_id, fold], workspace_name="ws",
                                  workspace_status=None)          # 缺省 = 可写
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
# 读法与取输入的小工具：只走 sdk + reflect
# ----------------------------------------------------------------------


def _definition(name: str):
    """描述侧的协议查询 PoolFindDefinition：按全名取定义描述符。"""
    full_name = name if "." in name else f"meeting_core.{name}"
    given = pool_find_definition(meeting_core_reflect._design_pool(), full_name)
    assert isinstance(given, Ok), given
    return given.value


def _node(value, name, reflect):
    """按协议起一个节点：access(definition).root(_Record(value))。"""
    given = reflect.access(_definition(name)).root(reflect._Record(value))
    assert isinstance(given, Ok), "取不到起点"
    return given.value


def _result(value, reflect):
    return _node(value, "Result", reflect)


def _ok_node(value, reflect):
    """$ok 那一支的节点。"""
    result = _result(value, reflect)
    assert result.has_ok() is True, "这不是 $ok"
    return result.get_ok()


def _ok_value(value, reflect):
    """标量载荷：result.get_ok().value。"""
    return _ok_node(value, reflect).value


def _ok_data(value, reflect):
    """载荷是 datetime / 序列 / 记录时，$ok 那一支的节点再拿 $data。"""
    return _ok_node(value, reflect).data


def _err_message(value, reflect):
    result = _result(value, reflect)
    assert result.has_err() is True, "这不是 $err"
    return result.get_err().value


_REFERENCE_KINDS = ("screen_ref", "chat_ref", "score_ref", "terminal_control_ref",
                    "versioned_file_ref", "versioned_file_content_ref")


def _reference_kind(reference, reflect):
    """一段引用是六支里的哪一支。"""
    node = _node(reference, "ContentReference", reflect)
    for kind in _REFERENCE_KINDS:
        if getattr(node, f"has_{kind}")():
            return kind
    raise AssertionError("不是任何一种引用")


def _references_of_kind(references, kind, reflect):
    return [r for r in references if _reference_kind(r, reflect) == kind]


def _one_reference_of_kind(references, kind, reflect):
    found = _references_of_kind(references, kind, reflect)
    assert len(found) == 1, f"{kind} 有 {len(found)} 条"
    return found[0]


def _message_time(message, reflect):
    return _node(message, "ChatMessage", reflect).get_time().data


def _messages(ctx, sdk, reflect):
    """会议里的聊天消息（区间端点从 sdk 取，不写死时刻）。"""
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    return _ok_node(sdk.chat_messages(ctx, first, last), reflect).data


def _message_of(messages, user_name, reflect):
    for message in messages:
        node = _node(message, "ChatMessage", reflect)
        if node.get_user_name().by_tag("agent_name").value == user_name:
            return message
    raise AssertionError(f"数据里没有 {user_name} 的消息")


@pytest.fixture(name="sdk")
def _sdk_module():
    return meeting_sdk


@pytest.fixture(name="reflect")
def _reflect_module():
    return meeting_core_reflect


@pytest.fixture
def ctx():
    return prepare_meeting_data_as_ctx()


# =====================================================================
# 时间与流
# =====================================================================

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
    # 控制 _T0 起；屏幕一条、聊天一条都在 _T1；评分在 _T2
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
# 聊天与评分
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
# 屏幕
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
    assert "屏幕流" in _err_message(sdk.screen_at(ctx, last), reflect)


def test_case_resolve_screen_reference(ctx, sdk, reflect):
    state = _ok_node(sdk.resolve_screen_reference(ctx, _screen_reference(ctx, sdk, reflect)),
                     reflect).data
    assert _node(state, "ScreenContent", reflect).get_text().value == "ab\ncd"


# =====================================================================
# 人
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
    # alice 发言那一刻（_T1）终端还是她的
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
# 文件层
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
    assert sorted(before) == ["f1"]                      # b.py 那一版是后来才有的
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
    # 数据里两条文件引用：一条赶上了那一版，一条太早
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
# 写：open_file / write_text（只往版本链上追加）
# =====================================================================


def _file_id_of(ctx, sdk, reflect, path):
    return _ok_node(sdk.file_id_by_path(ctx, path), reflect).data


def _version_times(ctx, sdk, reflect, file_id):
    return [_node(m, "datetime", reflect).get_microsecond().value
            for m in _ok_node(sdk.file_datetimes(ctx, file_id), reflect).data]


def test_case_open_file_opens_an_existing_path(ctx, sdk, reflect):
    moment = _ok_data(sdk.last_chunk_time(ctx), reflect)
    opened = _node(_ok_node(sdk.open_file(ctx, "/ws/a.py", moment, _ALICE),
                            reflect).data, "FileId", reflect)
    same = _node(_file_id_of(ctx, sdk, reflect, "/ws/a.py"), "FileId", reflect)
    assert opened.get_file_id().value == same.get_file_id().value


def test_case_open_file_creates_a_new_file_once(ctx, sdk, reflect):
    moment = _ok_data(sdk.last_chunk_time(ctx), reflect)
    first = _ok_node(sdk.open_file(ctx, "/ws/c.py", moment, _ALICE), reflect).data
    again = _ok_node(sdk.open_file(ctx, "/ws/c.py", moment, _ALICE), reflect).data
    assert _node(first, "FileId", reflect).get_file_id().value == \
        _node(again, "FileId", reflect).get_file_id().value
    state = _node(_ok_node(sdk.latest_file(ctx, first), reflect).data, "FileState", reflect)
    assert state.get_name().value == "c.py"
    assert state.get_content().value == ""
    assert state.has_meta() is False                     # 状态链空着 = 可写


def test_case_open_file_creates_inside_a_fold(ctx, sdk, reflect):
    moment = _ok_data(sdk.last_chunk_time(ctx), reflect)
    opened = _ok_node(sdk.open_file(ctx, "/ws/pkg/c.py", moment, _ALICE), reflect).data
    again = _file_id_of(ctx, sdk, reflect, "/ws/pkg/c.py")
    assert _node(opened, "FileId", reflect).get_file_id().value == \
        _node(again, "FileId", reflect).get_file_id().value


def test_case_open_file_shows_up_in_files_at(ctx, sdk, reflect):
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    opened = _node(_ok_node(sdk.open_file(ctx, "/ws/c.py", last, _ALICE), reflect).data,
                   "FileId", reflect)
    key = opened.get_file_id().value
    assert key in _ok_node(sdk.files_at(ctx, last), reflect).data
    assert key not in _ok_node(sdk.files_at(ctx, first), reflect).data


def test_case_open_file_refuses_where_it_cannot_create(ctx, sdk, reflect):
    moment = _ok_data(sdk.last_chunk_time(ctx), reflect)
    for path in ("a.py", "/nope/c.py", "/ws/nofold/c.py", "/locked/c.py", "/ws/pkg"):
        assert _err_message(sdk.open_file(ctx, path, moment, _ALICE), reflect), path


def test_case_write_text_appends_a_version_and_keeps_the_old_one(ctx, sdk, reflect):
    first = _ok_data(sdk.first_chunk_time(ctx), reflect)
    last = _ok_data(sdk.last_chunk_time(ctx), reflect)
    file_id = _ok_node(sdk.open_file(ctx, "/ws/c.py", first, _ALICE), reflect).data
    assert _result(sdk.write_text(ctx, file_id, "print(3)", last, _ALICE),
                   reflect).has_ok()
    assert _version_times(ctx, sdk, reflect, file_id) == [1, 3]      # 新建那一版 + 写这一版

    latest = _node(_ok_node(sdk.read_text(ctx, file_id), reflect).data, "FileState", reflect)
    assert latest.get_content().value == "print(3)"
    ref = sdk.make_versioned_file_reference(file_id, first)
    old = _node(_ok_node(sdk.resolve_versioned_file_reference(ctx, ref), reflect).data,
                "FileState", reflect)
    assert old.get_content().value == ""                 # 旧版原样留着


def test_case_read_text_is_latest_file(ctx, sdk, reflect):
    file_id = _file_id_of(ctx, sdk, reflect, "/ws/a.py")
    read = _ok_node(sdk.read_text(ctx, file_id), reflect).data
    latest = _ok_node(sdk.latest_file(ctx, file_id), reflect).data
    assert read == latest


def test_case_write_text_refuses_read_only_and_deleted(ctx, sdk, reflect):
    moment = _ok_data(sdk.last_chunk_time(ctx), reflect)
    read_only = _file_id_of(ctx, sdk, reflect, "/ws/a.py")
    assert "只读" in _err_message(sdk.write_text(ctx, read_only, "x", moment, _ALICE),
                                  reflect)
    deleted = _file_id_of(ctx, sdk, reflect, "/ws/pkg/b.py")
    assert "已删除" in _err_message(sdk.write_text(ctx, deleted, "x", moment, _ALICE),
                                    reflect)
