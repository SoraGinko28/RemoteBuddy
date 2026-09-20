"""Tests for meeting_validate: firing and passing of every Assert,
cross-chunk checks, ordering, and summaries."""

import datetime as _dt
from datetime import timezone as _dt_timezone

import pytest

import meeting_adt as adt
import meeting_validate as validator


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)


def _later(seconds: int) -> _dt.datetime:
    return _now() + _dt.timedelta(seconds=seconds)


def _valid_screen(text: str = "ab\ncd", moment=None,
                  width: int = 2, height: int = 2) -> adt.ScreenCapture:
    return adt.ScreenCapture(
        screen_data=text,
        terminal_text_content=[adt.TerminalTextSegment(text=text)],
        cursor_x=0, cursor_y=0, screen_width=width, screen_height=height,
        captured_at=moment or _now(),
    )


def _meeting(chunks: list) -> adt.MeetingData:
    return adt.MeetingData(meeting_uid="m", meeting_name="x",
                           sorted_stream_chunks=chunks)


class TestReportBasics:
    def test_ok_when_no_chunks(self) -> None:
        report = validator.validate_meeting_data(_meeting([]))
        assert report.ok
        assert report.count == 0

    def test_ok_on_valid_screen_chunk(self) -> None:
        chunk = adt.ScreenStreamChunk(owner_user_name="alice",
                                      chunks=[_valid_screen()])
        meeting = _meeting([chunk])
        meeting.agent_name2role = {"alice": adt.AgentRole.GENERATOR}
        report = validator.validate_meeting_data(meeting)
        assert report.ok, report.violations

    def test_uid_carried(self) -> None:
        report = validator.validate_meeting_data(
            adt.MeetingData(meeting_uid="m-9"))
        assert report.meeting_uid == "m-9"


class TestScreenAsserts:
    def _report_for(self, capture: adt.ScreenCapture, owner: str = "alice"):
        chunk = adt.ScreenStreamChunk(owner_user_name=owner, chunks=[capture])
        meeting = _meeting([chunk])
        meeting.agent_name2role = {"alice": adt.AgentRole.GENERATOR}
        return validator.validate_meeting_data(meeting)

    def test_plain_mismatch_recorded(self) -> None:
        capture = _valid_screen(text="ab")
        capture.screen_data = "xx"
        report = self._report_for(capture)
        assert "plain_of_matches" in report.by_assert()

    def test_width_overflow_recorded(self) -> None:
        capture = _valid_screen(text="abc", width=2)
        report = self._report_for(capture)
        assert "row_widths_fit" in report.by_assert()

    def test_cjk_width_overflow_recorded(self) -> None:
        capture = _valid_screen(text="\u4e2d\u6587", width=3)
        report = self._report_for(capture)
        assert "row_widths_fit" in report.by_assert()

    def test_height_mismatch_recorded(self) -> None:
        capture = _valid_screen(text="ab\ncd", height=3)
        report = self._report_for(capture)
        assert "rows_match_height" in report.by_assert()

    def test_cursor_out_of_bounds_recorded(self) -> None:
        capture = _valid_screen(width=2, height=2)
        capture.cursor_x = 2
        report = self._report_for(capture)
        assert "cursor_in_bounds" in report.by_assert()

    def test_bad_segments_recorded(self) -> None:
        capture = _valid_screen(text="ab")
        capture.terminal_text_content = [
            adt.TerminalTextSegment(text="a", fg=adt.PaletteColor(1)),
            adt.TerminalTextSegment(text="b", fg=adt.PaletteColor(1)),
        ]
        capture.screen_data = "ab"
        report = self._report_for(capture)
        assert "segments_valid" in report.by_assert()

    def test_violation_carries_stream_and_time(self) -> None:
        moment = _later(5)
        capture = _valid_screen(text="abc", width=2, moment=moment)
        report = self._report_for(capture)
        violation = [v for v in report.violations if v.assert_name == "row_widths_fit"][0]
        assert violation.stream_name == "screen"
        assert str(moment) in violation.at

    def test_multiple_bad_records_counted(self) -> None:
        bad1 = _valid_screen(text="abc", width=2, moment=_later(1))
        bad2 = _valid_screen(text="xyz", width=2, moment=_later(2))
        chunk = adt.ScreenStreamChunk(owner_user_name="alice", chunks=[bad1, bad2])
        meeting = _meeting([chunk])
        meeting.agent_name2role = {"alice": adt.AgentRole.GENERATOR}
        report = validator.validate_meeting_data(meeting)
        assert report.by_assert()["row_widths_fit"] == 2


class TestTerminalControl:
    def _controls(self) -> list:
        return [adt.TerminalControl(user_name=adt.HUMAN_USER_NAME, start_time=_now())]

    def test_passive_cannot_own_terminal(self) -> None:
        chunk = adt.TerminalControlStreamChunk(chunks=[
            adt.TerminalControl(user_name="pas", start_time=_now())])
        meeting = _meeting([chunk])
        meeting.agent_name2role = {"pas": adt.AgentRole.PASSIVE_DISCRIMINATOR}
        report = validator.validate_meeting_data(meeting)
        assert "terminal_control_capable" in report.by_assert()

    def test_human_can_own_terminal(self) -> None:
        meeting = _meeting([adt.TerminalControlStreamChunk(chunks=self._controls())])
        report = validator.validate_meeting_data(meeting)
        assert report.ok


class TestScoreSummon:
    def _score(self, judge: str, reference=None) -> adt.ScoreMessage:
        return adt.ScoreMessage(tag="t", judge_name=judge, time=_later(3),
                                reference=reference)

    def _chat_with_mention(self, judge: str) -> adt.ChatMessage:
        return adt.ChatMessage(
            user_name="human", time=_later(2),
            message=[adt.AtUserMessage(user_name=judge)])

    def _meeting_with(self, chunks: list, roles: dict) -> adt.MeetingData:
        meeting = _meeting(chunks)
        meeting.agent_name2role = roles
        return meeting

    def test_passive_without_summon_flagged(self) -> None:
        roles = {"pas": adt.AgentRole.PASSIVE_DISCRIMINATOR}
        score_chunk = adt.ScoreStreamChunk(chunks=[self._score("pas")])
        meeting = self._meeting_with([score_chunk], roles)
        report = validator.validate_meeting_data(meeting)
        assert "score_summon_chained" in report.by_assert()

    def test_generator_score_never_flagged(self) -> None:
        roles = {"gen": adt.AgentRole.GENERATOR}
        score_chunk = adt.ScoreStreamChunk(chunks=[self._score("gen")])
        meeting = self._meeting_with([score_chunk], roles)
        report = validator.validate_meeting_data(meeting)
        assert report.ok, report.violations

    def test_passive_summoned_by_chat_reference_ok(self) -> None:
        roles = {"pas": adt.AgentRole.PASSIVE_DISCRIMINATOR}
        chat = self._chat_with_mention("pas")
        ref = adt.ChatReference(msg_time=chat.time, user_name=chat.user_name,
                                region=adt.TextRegion(0, 1))
        score_chunk = adt.ScoreStreamChunk(chunks=[self._score("pas", ref)])
        chat_chunk = adt.ChatStreamChunk(chunks=[chat])
        # sorted_stream_chunks must be ascending in time: chat (07:00:02)
        # precedes score (07:00:03)
        meeting = self._meeting_with([chat_chunk, score_chunk], roles)
        report = validator.validate_meeting_data(meeting)
        assert report.ok, report.violations

    def test_passive_ref_missing_from_stream_flagged(self) -> None:
        roles = {"pas": adt.AgentRole.PASSIVE_DISCRIMINATOR}
        ref = adt.ChatReference(msg_time=_later(2), user_name="nobody",
                                region=adt.TextRegion(0, 1))
        score_chunk = adt.ScoreStreamChunk(chunks=[self._score("pas", ref)])
        meeting = self._meeting_with([score_chunk], roles)
        report = validator.validate_meeting_data(meeting)
        assert "score_summon_chained" in report.by_assert()


class TestMeetingLevel:
    def test_role_map_unknown_agent_flagged(self) -> None:
        chunk = adt.ScreenStreamChunk(owner_user_name="alice",
                                      chunks=[_valid_screen()])
        meeting = _meeting([chunk])
        meeting.agent_name2role = {"alice": adt.AgentRole.GENERATOR,
                                   "ghost": adt.AgentRole.GENERATOR}
        report = validator.validate_meeting_data(meeting)
        assert "role_map_keys_in_user_names" in report.by_assert()

    def test_role_map_human_not_required(self) -> None:
        chunk = adt.ScreenStreamChunk(owner_user_name=adt.HUMAN_USER_NAME,
                                      chunks=[_valid_screen()])
        meeting = _meeting([chunk])
        report = validator.validate_meeting_data(meeting)
        assert report.ok, report.violations


class TestOrdering:
    def _chats(self, times: list) -> adt.ChatStreamChunk:
        return adt.ChatStreamChunk(chunks=[
            adt.ChatMessage(user_name="a", time=t) for t in times])

    def test_chunk_records_out_of_order_flagged(self) -> None:
        chunk = self._chats([_later(2), _later(1)])
        meeting = _meeting([chunk])
        report = validator.validate_meeting_data(meeting)
        assert "records_time_ascending" in report.by_assert()

    def test_chunk_records_in_order_ok(self) -> None:
        chunk = self._chats([_later(1), _later(2)])
        report = validator.validate_meeting_data(_meeting([chunk]))
        assert report.ok

    def test_equal_timestamps_allowed(self) -> None:
        chunk = self._chats([_later(1), _later(1)])
        report = validator.validate_meeting_data(_meeting([chunk]))
        assert report.ok

    def test_chunks_list_out_of_order_flagged(self) -> None:
        early = self._chats([_later(1)])
        late = self._chats([_later(5)])
        # deliberately out of order: late before early
        meeting = _meeting([late, early])
        report = validator.validate_meeting_data(meeting)
        assert "chunks_start_time_ascending" in report.by_assert()


class TestValidateRunDir:
    def test_missing_dir_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            validator.validate_run_dir(tmp_path / "no-such-run")


def _entry(file_id: str, entry_name: str) -> adt.FileEntry:
    return adt.FileEntry(entry_name=entry_name, file_id=adt.FileId(file_id=file_id))


def _file_store() -> adt.FileKvStore:
    return adt.FileKvStore(file_id2file={"f1": adt.VersionedFile(), "f2": adt.VersionedFile()})


def _file_meeting(workspace: adt.FileWorkspace) -> adt.MeetingData:
    desktop = adt.FileDesktop(workspaces=[workspace])
    desktop.reorder_indexes = adt.Versioned(adt.identity_desktop_order(desktop))
    meeting = _meeting([])
    meeting.file_kv_store = _file_store()
    meeting.file_desktop = desktop
    return meeting


class TestFileLayer:
    def _workspace(self) -> adt.FileWorkspace:
        return adt.FileWorkspace(member_files=[
            _entry("f1", "a.py"),
            adt.FileFold(fold_name="pkg", member_file_ids=[_entry("f2", "b.py")]),
        ], workspace_name="ws")

    def test_valid_desktop_passes(self) -> None:
        report = validator.validate_meeting_data(_file_meeting(self._workspace()))
        assert report.ok, report.violations

    def test_duplicate_names_flagged(self) -> None:
        workspace = adt.FileWorkspace(member_files=[
            _entry("f1", "pkg"), adt.FileFold(fold_name="pkg"),
        ], workspace_name="ws")
        report = validator.validate_meeting_data(_file_meeting(workspace))
        assert "file_desktop_unique_names" in report.by_assert()

    def test_missing_file_id_flagged(self) -> None:
        workspace = adt.FileWorkspace(member_files=[_entry("gone", "a.py")],
                                      workspace_name="ws")
        report = validator.validate_meeting_data(_file_meeting(workspace))
        assert "file_id_in_store" in report.by_assert()

    def test_bad_member_order_flagged(self) -> None:
        meeting = _file_meeting(self._workspace())
        meeting.file_desktop.reorder_indexes.data.workspace_indexes[0].file_indexes = []
        report = validator.validate_meeting_data(meeting)
        assert "file_desktop_member_order" in report.by_assert()
