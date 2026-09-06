"""Tests for meeting_loader: projecting events.jsonl kinds onto the five
streams."""

import datetime as _dt
import json

import pytest

import meeting_adt as adt
import meeting_loader as loader


def _write_run(tmp_path, events, manifest=None) -> object:
    run_dir = tmp_path / "20260902-153537-7f1bea"
    (run_dir / "streams").mkdir(parents=True)
    manifest = manifest or {"meeting_id": "20260902-153537-7f1bea",
                            "session_name": "demo"}
    (run_dir / "meeting.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "streams" / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")
    return run_dir


def _ev(seq, kind, captured_at, content=None, actor="agent", phase="x",
         data=None) -> dict:
    event = {"seq": seq, "event_id": f"e-{seq:08d}", "captured_at": captured_at,
             "kind": kind, "actor": actor, "phase": phase,
             "content": content or "", "message_type": None, "data": data or {}}
    return event


def _t(iso: str) -> object:
    return adt._iso_parse(iso)


class TestMissingFiles:
    def test_missing_meeting_json(self, tmp_path) -> None:
        run_dir = tmp_path / "r"
        run_dir.mkdir()
        (run_dir / "streams").mkdir()
        (run_dir / "streams" / "events.jsonl").write_text("", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="meeting.json"):
            loader.load_meeting_data_from_run(run_dir)

    def test_missing_events_jsonl(self, tmp_path) -> None:
        run_dir = tmp_path / "r"
        run_dir.mkdir()
        (run_dir / "meeting.json").write_text("{}", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="events.jsonl"):
            loader.load_meeting_data_from_run(run_dir)


class TestMapping:
    def test_chat_human_agent_mapping(self, tmp_path) -> None:
        events = [
            _ev(1, "chat", "2026-09-02T07:35:37.684Z", content="hi",
                actor="human", phase="created"),
            _ev(2, "chat", "2026-09-02T07:35:38.000Z", content="ok",
                actor="agent", phase="ack"),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)

        chat_chunks = [c for c in meeting.sorted_stream_chunks
                       if isinstance(c, adt.ChatStreamChunk)]
        assert len(chat_chunks) == 1
        messages = chat_chunks[0].chunks
        assert [m.user_name for m in messages] == [adt.HUMAN_USER_NAME, "agent"]
        assert messages[0].message == [adt.RawMessage(text="hi")]
        assert summary.mapped["chat"] == 2

    def test_screen_mapping_fields(self, tmp_path) -> None:
        content = "ab\ncd"
        events = [_ev(1, "screen", "2026-09-02T07:35:37.890Z", content=content,
                      data={"cursor_x": 1, "cursor_y": 1, "pane_width": 80,
                            "pane_height": 24})]
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)

        screen_chunks = [c for c in meeting.sorted_stream_chunks
                         if isinstance(c, adt.ScreenStreamChunk)]
        assert len(screen_chunks) == 1
        capture = screen_chunks[0].chunks[0]
        # normalization: when content has fewer than height rows, blank rows
        # are padded to a full frame of 24 rows
        assert len(capture.screen_data.split("\n")) == 24
        assert capture.screen_data.startswith("ab\ncd\n")
        assert capture.cursor_x == 1
        assert capture.cursor_y == 1
        assert capture.screen_width == 80
        assert capture.screen_height == 24
        assert capture.captured_at == _t("2026-09-02T07:35:37.890Z")
        assert summary.mapped["screen"] == 1

    def test_screen_normalized_rows_right_trimmed(self, tmp_path) -> None:
        # normalization: trailing spaces are stripped from rows; when the
        # content rows fall short of height, blank rows are padded at the
        # bottom to a full frame
        content = "ab  \ncd "
        events = [_ev(1, "screen", "2026-09-02T07:35:37.890Z", content=content,
                      data={"cursor_x": 0, "cursor_y": 0, "pane_width": 4,
                            "pane_height": 3})]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        capture = [c for c in meeting.sorted_stream_chunks
                   if isinstance(c, adt.ScreenStreamChunk)][0].chunks[0]
        assert capture.screen_data == "ab\ncd\n"
        assert capture.screen_height == 3
        # after normalization the ADT frame asserts must hold
        adt.assert_screen_rows_match_height(capture)
        adt.assert_screen_plain_matches(capture)
        adt.assert_screen_row_widths_fit(capture)

    def test_harness_control_acquired_not_mapped(self, tmp_path) -> None:
        events = [
            _ev(1, "control", "2026-09-02T13:22:06.860Z", actor="harness",
                phase="human_control", data={"status": "acquired"}),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        assert summary.mapped.get("terminal_control", 0) == 0
        assert all(not isinstance(c, adt.TerminalControlStreamChunk)
                   for c in meeting.sorted_stream_chunks)

    def test_noise_kinds_skipped(self, tmp_path) -> None:
        events = [
            _ev(1, "state", "2026-09-02T07:35:38.000Z"),
            _ev(2, "action", "2026-09-02T07:35:38.100Z"),
            _ev(3, "error", "2026-09-02T07:35:38.200Z"),
            _ev(4, "weird", "2026-09-02T07:35:38.300Z"),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        assert meeting.sorted_stream_chunks == []
        assert summary.skipped_noise == 3
        assert summary.skipped_unrecognized == 1

    def test_control_acquired_becomes_terminal_control(self, tmp_path) -> None:
        events = [
            _ev(1, "control", "2026-09-02T13:22:06.860Z", actor="human",
                phase="human_control", data={"status": "acquired"}),
            _ev(2, "control", "2026-09-02T13:22:22.703Z", actor="human",
                phase="agent_thinking", data={"status": "released"}),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        tc_chunks = [c for c in meeting.sorted_stream_chunks
                     if isinstance(c, adt.TerminalControlStreamChunk)]
        assert len(tc_chunks) == 1
        assert tc_chunks[0].chunks[0].user_name == adt.HUMAN_USER_NAME
        assert tc_chunks[0].chunks[0].start_time == _t("2026-09-02T13:22:06.860Z")
        assert summary.mapped["terminal_control"] == 1

    def test_control_release_only_creates_nothing(self, tmp_path) -> None:
        events = [_ev(1, "control", "2026-09-02T13:22:22.703Z", actor="human",
                      phase="agent_thinking", data={"status": "released"})]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        assert all(not isinstance(c, adt.TerminalControlStreamChunk)
                   for c in meeting.sorted_stream_chunks)

    def test_role_map_infers_generator_for_agents_only(self, tmp_path) -> None:
        events = [
            _ev(1, "chat", "2026-09-02T07:35:37.684Z", content="hi", actor="human"),
            _ev(2, "chat", "2026-09-02T07:35:38.000Z", content="ok", actor="agent"),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        assert meeting.agent_name2role == {"agent": adt.AgentRole.GENERATOR}

    def test_role_map_assert_holds_on_loaded_data(self, tmp_path) -> None:
        events = [
            _ev(1, "chat", "2026-09-02T07:35:37.684Z", content="hi", actor="human"),
            _ev(2, "chat", "2026-09-02T07:35:38.000Z", content="ok", actor="agent"),
            _ev(3, "screen", "2026-09-02T07:35:39.000Z", content="frame",
                data={"cursor_x": 0, "cursor_y": 0, "pane_width": 80,
                      "pane_height": 24}),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        # every agent that appeared is in the role map -> the assert must pass
        adt.assert_role_map_keys_in_user_names(meeting)

    def test_screen_owner_splits_on_human_takeover(self, tmp_path) -> None:
        events = [
            _ev(1, "chat", "2026-09-02T13:20:00.000Z", content="start", actor="agent"),
            _ev(2, "screen", "2026-09-02T13:21:00.000Z", content="a",
                actor="workspace", data={"cursor_x": 0, "cursor_y": 0,
                                         "pane_width": 80, "pane_height": 24}),
            _ev(3, "control", "2026-09-02T13:22:06.860Z", actor="human",
                phase="human_control", data={"status": "acquired"}),
            _ev(4, "screen", "2026-09-02T13:22:10.000Z", content="b",
                actor="workspace", data={"cursor_x": 0, "cursor_y": 0,
                                         "pane_width": 80, "pane_height": 24}),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        owners = [(c.owner_user_name, len(c.chunks)) for c in meeting.sorted_stream_chunks
                  if isinstance(c, adt.ScreenStreamChunk)]
        assert owners == [("agent", 1), (adt.HUMAN_USER_NAME, 1)]

    def test_events_out_of_seq_order_are_sorted(self, tmp_path) -> None:
        events = [
            _ev(2, "chat", "2026-09-02T07:35:38.000Z", content="later", actor="agent"),
            _ev(1, "chat", "2026-09-02T07:35:37.684Z", content="first", actor="agent"),
        ]
        run_dir = _write_run(tmp_path, events)
        meeting, _ = loader.load_meeting_data_from_run(run_dir)
        chats = [c for c in meeting.sorted_stream_chunks
                 if isinstance(c, adt.ChatStreamChunk)][0]
        assert [m.message[0].text for m in chats.chunks] == ["first", "later"]

    def test_bad_json_line_skipped(self, tmp_path) -> None:
        run_dir = tmp_path / "r"
        (run_dir / "streams").mkdir(parents=True)
        (run_dir / "meeting.json").write_text("{}", encoding="utf-8")
        good = json.dumps(_ev(1, "chat", "2026-09-02T07:35:37.684Z", content="hi",
                              actor="human"))
        (run_dir / "streams" / "events.jsonl").write_text(
            "not-json\n" + good + "\n", encoding="utf-8")
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        assert summary.event_total == 2
        assert len(meeting.sorted_stream_chunks) == 1

    def test_empty_events_file(self, tmp_path) -> None:
        events: list = []
        run_dir = _write_run(tmp_path, events)
        meeting, summary = loader.load_meeting_data_from_run(run_dir)
        assert meeting.sorted_stream_chunks == []
        assert summary.event_total == 0
