"""meeting_sdk 的用例：每个 test_case_* 只吃 (ctx, meeting_sdk, meeting_core_reflect)。

写法是给以后的 metric getter 练手的：

    import meeting_sdk
    import meeting_core_reflect

    def prepare_meeting_data_as_ctx():
        return ...

    test_case_xxx(prepare_meeting_data_as_ctx(), meeting_sdk, meeting_core_reflect)
    test_case_yyy(prepare_meeting_data_as_ctx(), meeting_sdk, meeting_core_reflect)

两条规矩：

1. **用例里不许认识 meeting data 的细节**——数据只能用 ``meeting_sdk`` 取，取回来的东西
   只能用 ``meeting_core_reflect`` 按设计读。
2. **结果按设计的接口读**：Result 是 meeting_core 里的类型，SDK 不暴露自己的 Ok / Err；
   一律走 ``result.has_ok()`` / ``result.get_ok().value``（失败走 ``has_err()`` /
   ``get_err().value``）。载荷是序列或记录时，拿节点的 ``$data`` 再用反射逐段读——
   ``$ok T`` 的 T 在 Result 那里是空的，只能靠调用方知道自己要的是什么类型。

底下三个 fixture 是为了让 pytest 按同一签名驱动这些用例（``prepare_meeting_data_as_ctx``
是唯一碰记录的地方，它造数据）。
"""

import datetime as _dt

import pytest

import meeting_core_reflect
import meeting_sdk
import meeting_adt  # 只有 prepare_meeting_data_as_ctx 用它

# 设计里的 datetime 是"微秒"（$microsecond int），所以这几个时刻的微秒各不相同，
# 用例才读得出谁是谁。
_T0 = _dt.datetime(2026, 9, 18, 12, 0, 0, 1, tzinfo=_dt.timezone.utc)
_T1 = _dt.datetime(2026, 9, 18, 12, 0, 1, 2, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 9, 18, 12, 0, 2, 3, tzinfo=_dt.timezone.utc)
_T3 = _dt.datetime(2026, 9, 18, 12, 0, 3, 4, tzinfo=_dt.timezone.utc)


def prepare_meeting_data_as_ctx():
    """一份会议记录，当 ctx 交给用例。"""
    adt = meeting_adt
    alice = "alice"
    bob = "bob"

    controls = adt.TerminalControlStreamChunk(chunks=[
        adt.TerminalControl(tmux_window_name="w", tmux_pane=0, user_name=alice, start_time=_T0),
        adt.TerminalControl(tmux_window_name="w", tmux_pane=0, user_name="", start_time=_T2),
    ])
    screens = adt.ScreenStreamChunk(owner_user_name=alice, chunks=[
        adt.ScreenCapture(screen_data="ab\ncd", terminal_text_content=[], cursor_x=0,
                          cursor_y=0, screen_width=2, screen_height=2, captured_at=_T1),
    ])
    chats = adt.ChatStreamChunk(chunks=[
        adt.ChatMessage(user_name=alice, time=_T1, message=[adt.RawMessage("hi")]),
        adt.ChatMessage(user_name=bob, time=_T2, message=[adt.RawMessage("yo")]),
    ])
    scores = adt.ScoreStreamChunk(chunks=[
        adt.ScoreMessage(tag="correctness", score=1.0, comment="good", judge_name=bob,
                         time=_T2, reference=adt.ScoreReference(judge_time=_T2, judge_name=bob)),
    ])

    fold = adt.FileFold(fold_name="pkg",
                        member_file_ids=[adt.FileId(file_id="f2", entry_name="b.py")])
    workspace = adt.FileWorkspace(member_files=[adt.FileId(file_id="f1", entry_name="a.py"), fold],
                                  workspace_name="ws",
                                  workspace_status=adt.DataStatus.READ_ONLY)
    desktop = adt.FileDesktop(workspaces=[workspace])
    desktop.reorder_indexes = adt.Versioned(adt.identity_desktop_order(desktop), _T0, alice)
    store = adt.FileKvStore(file_id2file={
        "f1": adt.VersionedFile(
            file_name=adt.Versioned("a.py", _T0, alice),
            file_meta=adt.Versioned(adt.DataStatus.READ_ONLY, _T0, alice),
            file_content=adt.Versioned("print(1)", _T0, alice)),
        "f2": adt.VersionedFile(
            file_name=adt.Versioned("b.py", _T3, bob),
            file_meta=adt.Versioned(None, _T3, bob),
            file_content=adt.Versioned("print(2)", _T3, bob)),
    })

    return adt.MeetingData(
        meeting_uid="m1",
        meeting_name="case",
        agent_name2role={alice: adt.AgentRole.GENERATOR, bob: adt.AgentRole.PASSIVE_DISCRIMINATOR},
        sorted_stream_chunks=[screens, chats, scores, controls],
        file_kv_store=store,
        file_desktop=desktop,
    )


# ----------------------------------------------------------------------
# 读结果：只走反射的标准接口
# ----------------------------------------------------------------------


def _result(value, meeting_core_reflect):
    return meeting_core_reflect.open_record(value, "Result")


def _ok_node(value, meeting_core_reflect):
    """$ok 那一支的节点。"""
    result = _result(value, meeting_core_reflect)
    assert result.has_ok() is True, "这不是 $ok"
    return result.get_ok()


def _ok_value(value, meeting_core_reflect):
    """标量载荷：result.get_ok().value。"""
    return _ok_node(value, meeting_core_reflect).value


def _err_message(value, meeting_core_reflect):
    result = _result(value, meeting_core_reflect)
    assert result.has_err() is True, "这不是 $err"
    return result.get_err().value


@pytest.fixture(name="meeting_sdk")
def _meeting_sdk_module():
    return meeting_sdk


@pytest.fixture(name="meeting_core_reflect")
def _meeting_core_reflect_module():
    return meeting_core_reflect


@pytest.fixture
def ctx():
    return prepare_meeting_data_as_ctx()


# =====================================================================
# 时间与流
# =====================================================================

def test_case_chunk_times_follow_the_streams(ctx, meeting_sdk, meeting_core_reflect):
    payload = _ok_node(meeting_sdk.chunk_times(ctx, _T0, _T2), meeting_core_reflect).data
    moments = [meeting_core_reflect.open_record(m, "datetime").get_microsecond().value
               for m in payload]
    # 屏幕一条、聊天一条都在 _T1，评分在 _T2，控制从 _T0 起
    assert moments == [1, 2, 2, 3]


def test_case_chunk_times_can_be_narrowed(ctx, meeting_sdk, meeting_core_reflect):
    payload = _ok_node(meeting_sdk.chunk_times(ctx, _T2, _T2), meeting_core_reflect).data
    assert len(payload) == 1
    assert meeting_core_reflect.open_record(payload[0], "datetime").get_microsecond().value == 3


def test_case_streams_between_lists_the_stream_names(ctx, meeting_sdk, meeting_core_reflect):
    names = _ok_node(meeting_sdk.streams_between(ctx, _T0, _T3), meeting_core_reflect).data
    assert sorted(names) == ["chat", "score", "screen", "terminal_control"]


# =====================================================================
# 聊天与评分
# =====================================================================

def test_case_chat_messages_read_through_the_design(ctx, meeting_sdk, meeting_core_reflect):
    messages = _ok_node(meeting_sdk.chat_messages(ctx, _T0, _T3), meeting_core_reflect).data
    assert len(messages) == 2
    first = meeting_core_reflect.open_record(messages[0], "ChatMessage")
    assert first.get_user_name().by_tag("agent_name").value == "alice"
    assert first.get_time().get_microsecond().value == 2


def test_case_chat_messages_can_be_narrowed(ctx, meeting_sdk, meeting_core_reflect):
    messages = _ok_node(meeting_sdk.chat_messages(ctx, _T2, _T2), meeting_core_reflect).data
    assert len(messages) == 1
    node = meeting_core_reflect.open_record(messages[0], "ChatMessage")
    assert node.get_user_name().by_tag("agent_name").value == "bob"


def test_case_chat_message_at_is_unique_per_time_and_user(ctx, meeting_sdk, meeting_core_reflect):
    message = _ok_node(meeting_sdk.chat_message_at(ctx, _T1, "alice"), meeting_core_reflect).data
    node = meeting_core_reflect.open_record(message, "ChatMessage")
    assert node.get_user_name().by_tag("agent_name").value == "alice"

    assert "bob" in _err_message(meeting_sdk.chat_message_at(ctx, _T1, "bob"),
                                 meeting_core_reflect)


def test_case_resolve_chat_reference(ctx, meeting_sdk, meeting_core_reflect):
    ref = meeting_sdk.ChatReference(msg_time=_T2, user_name="bob",
                                    region=meeting_sdk.TextRegion(0, 2))
    message = _ok_node(meeting_sdk.resolve_chat_reference(ctx, ref), meeting_core_reflect).data
    node = meeting_core_reflect.open_record(message, "ChatMessage")
    assert node.get_user_name().by_tag("agent_name").value == "bob"


def test_case_resolve_chat_content_reference_takes_the_region(ctx, meeting_sdk,
                                                             meeting_core_reflect):
    ref = meeting_sdk.ChatReference(msg_time=_T1, user_name="alice",
                                    region=meeting_sdk.TextRegion(0, 2))
    given = meeting_sdk.resolve_chat_content_reference(ctx, ref)
    assert _ok_value(given, meeting_core_reflect) == "hi"


def test_case_resolve_score_reference(ctx, meeting_sdk, meeting_core_reflect):
    ref = meeting_sdk.ScoreReference(judge_time=_T2, judge_name="bob")
    message = _ok_node(meeting_sdk.resolve_score_reference(ctx, ref), meeting_core_reflect).data
    node = meeting_core_reflect.open_record(message, "ScoreMessage")
    assert node.get_tag().value == "correctness"
    assert node.get_score().value == 1.0


def test_case_unknown_score_reference_is_an_error(ctx, meeting_sdk, meeting_core_reflect):
    ref = meeting_sdk.ScoreReference(judge_time=_T1, judge_name="nobody")
    assert "nobody" in _err_message(meeting_sdk.resolve_score_reference(ctx, ref),
                                    meeting_core_reflect)


# =====================================================================
# 屏幕
# =====================================================================

def test_case_screen_at_returns_the_state(ctx, meeting_sdk, meeting_core_reflect):
    state = _ok_node(meeting_sdk.screen_at(ctx, _T1), meeting_core_reflect).data
    assert state["text"] == "ab\ncd"
    assert state["width"] == 2 and state["height"] == 2
    assert meeting_core_reflect.open_record(
        state["captured_at"], "datetime").get_microsecond().value == 2


def test_case_screen_at_without_that_moment_is_an_error(ctx, meeting_sdk, meeting_core_reflect):
    assert "屏幕流" in _err_message(meeting_sdk.screen_at(ctx, _T3), meeting_core_reflect)


def test_case_resolve_screen_reference(ctx, meeting_sdk, meeting_core_reflect):
    ref = meeting_sdk.ScreenReference(capture_time=_T1, stream_name="screen",
                                      region=meeting_sdk.TextRegion(0, 2))
    state = _ok_node(meeting_sdk.resolve_screen_reference(ctx, ref), meeting_core_reflect).data
    assert state["text"] == "ab\ncd"


# =====================================================================
# 人
# =====================================================================

def test_case_participants(ctx, meeting_sdk, meeting_core_reflect):
    names = _ok_node(meeting_sdk.participants(ctx), meeting_core_reflect).data
    assert names == ["alice", "bob"]


def test_case_roles(ctx, meeting_sdk, meeting_core_reflect):
    roster = _ok_node(meeting_sdk.roles(ctx), meeting_core_reflect).data
    assert roster["alice"] is meeting_sdk.AgentRole.GENERATOR
    assert roster["bob"] is meeting_sdk.AgentRole.PASSIVE_DISCRIMINATOR


def test_case_owner_at_covers_and_leaves_gaps(ctx, meeting_sdk, meeting_core_reflect):
    assert _ok_value(meeting_sdk.owner_at(ctx, _T1), meeting_core_reflect) == "alice"
    assert _ok_value(meeting_sdk.owner_at(ctx, _T2), meeting_core_reflect) is None
    earlier = meeting_sdk.owner_at(ctx, _T0 - _dt.timedelta(seconds=1))
    assert _ok_value(earlier, meeting_core_reflect) is None


def test_case_control_owner_of_a_handover(ctx, meeting_sdk, meeting_core_reflect):
    takeover = meeting_sdk.TerminalControlReference(
        start_time=_T0, user_name="alice", region=meeting_sdk.TextRegion(0, 3))
    assert _ok_value(meeting_sdk.control_owner_of(ctx, takeover), meeting_core_reflect) == "alice"
    handback = meeting_sdk.TerminalControlReference(
        start_time=_T2, user_name=None, region=meeting_sdk.TextRegion(0, 3))
    assert _ok_value(meeting_sdk.control_owner_of(ctx, handback), meeting_core_reflect) is None


# =====================================================================
# 文件层
# =====================================================================

def test_case_file_id_by_path_finds_a_direct_file(ctx, meeting_sdk, meeting_core_reflect):
    file_id = _ok_node(meeting_sdk.file_id_by_path(ctx, "/ws/a.py"), meeting_core_reflect).data
    node = meeting_core_reflect.open_record(file_id, "FileId")
    assert node.get_entry_name().value == "a.py"


def test_case_file_id_by_path_finds_a_file_in_a_fold(ctx, meeting_sdk, meeting_core_reflect):
    file_id = _ok_node(meeting_sdk.file_id_by_path(ctx, "/ws/pkg/b.py"),
                       meeting_core_reflect).data
    node = meeting_core_reflect.open_record(file_id, "FileId")
    assert node.get_file_id().value == "f2"


def test_case_file_id_by_path_rejects_unknown_paths(ctx, meeting_sdk, meeting_core_reflect):
    for path in ("/ws/nope.py", "a.py", "/ws/pkg/x/y.py"):
        assert _err_message(meeting_sdk.file_id_by_path(ctx, path), meeting_core_reflect)


def test_case_file_kv_store_is_readable_through_the_design(ctx, meeting_sdk,
                                                           meeting_core_reflect):
    store = _ok_node(meeting_sdk.file_kv_store(ctx), meeting_core_reflect).data
    node = meeting_core_reflect.open_record(store, "FileKvStore")
    assert node.get_file_id2file().keys() == ["f1", "f2"]


def test_case_latest_file_gives_the_current_state(ctx, meeting_sdk, meeting_core_reflect):
    file_id = _ok_node(meeting_sdk.file_id_by_path(ctx, "/ws/a.py"), meeting_core_reflect).data
    state = _ok_node(meeting_sdk.latest_file(ctx, file_id), meeting_core_reflect).data
    assert state["name"] == "a.py"
    assert state["meta"] is meeting_sdk.DataStatus.READ_ONLY
    assert state["content"] == "print(1)"


def test_case_files_at_only_lists_what_existed(ctx, meeting_sdk, meeting_core_reflect):
    before = _ok_node(meeting_sdk.files_at(ctx, _T2), meeting_core_reflect).data
    assert sorted(before) == ["f1"]                      # f2 那一版是 _T3 才有的
    after = _ok_node(meeting_sdk.files_at(ctx, _T3), meeting_core_reflect).data
    assert sorted(after) == ["f1", "f2"]
    assert after["f2"]["content"] == "print(2)"


def test_case_resolve_versioned_file_reference_is_as_of(ctx, meeting_sdk, meeting_core_reflect):
    file_id = _ok_node(meeting_sdk.file_id_by_path(ctx, "/ws/pkg/b.py"),
                       meeting_core_reflect).data
    ref = meeting_sdk.VersionedFileReference(file_id=file_id, version_time=_T3)
    state = _ok_node(meeting_sdk.resolve_versioned_file_reference(ctx, ref),
                     meeting_core_reflect).data
    assert state["name"] == "b.py"

    too_early = meeting_sdk.VersionedFileReference(file_id=file_id, version_time=_T2)
    assert _err_message(meeting_sdk.resolve_versioned_file_reference(ctx, too_early),
                        meeting_core_reflect)


def test_case_resolve_versioned_file_content_reference_takes_the_region(
        ctx, meeting_sdk, meeting_core_reflect):
    file_id = _ok_node(meeting_sdk.file_id_by_path(ctx, "/ws/a.py"), meeting_core_reflect).data
    ref = meeting_sdk.VersionedFileContentReference(
        versioned_file_ref=meeting_sdk.VersionedFileReference(file_id=file_id, version_time=_T0),
        region=meeting_sdk.TextRegion(0, 5))
    given = meeting_sdk.resolve_versioned_file_content_reference(ctx, ref)
    assert _ok_value(given, meeting_core_reflect) == "print"
