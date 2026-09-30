"""meeting_validate — run every ADT Assert over a MeetingData and produce a
violation list.

Design intent:
- Collect only Asserts (data invariants); a violation means a record does not
  conform to the ADT. Record them one by one, without interrupting or
  modifying data.
- Hints (reply reminders / chain-head human, etc.) are operational policy, not
  data invariants: this module does not block on them and keeps a hints
  extension slot.
- Each violation carries [stream / time / assert name / reason], for reports
  and the G14 full audit.

Asserts covered (aligned with MEETING_ADT.md / meeting_adt.py):
  screen            plain_of == screen_data / row width <= width /
                    row count == height / cursor within bounds / segments
                    non-empty and adjacent attributes distinct
  terminal_control  owner in terminal_capable (human or actor)
  meeting           keys(agent_name2role) subset of user_names
  file_desktop      names unique per level (so a path resolves to one entry);
                    member order is a permutation at every level;
                    every file id in the tree exists in FileKvStore
  ordering          records within a chunk are non-decreasing in time;
                    sorted_stream_chunks are non-decreasing by first record
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import meeting_adt as adt
import meeting_loader as loader


@dataclass
class Violation:
    stream_name: str
    at: str            # record time (ISO); "n/a" when absent
    assert_name: str   # name of the Assert
    reason: str        # violation reason (the message raised by the assert function)


@dataclass
class ValidationReport:
    meeting_uid: str = ""
    violations: list = field(default_factory=list)  # list[Violation]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def count(self) -> int:
        return len(self.violations)

    def by_assert(self) -> dict:
        counts: dict = {}
        for v in self.violations:
            counts[v.assert_name] = counts.get(v.assert_name, 0) + 1
        return counts

    def by_stream(self) -> dict:
        counts: dict = {}
        for v in self.violations:
            counts[v.stream_name] = counts.get(v.stream_name, 0) + 1
        return counts


def _time_str(moment: Optional[object]) -> str:
    if moment is None:
        return "n/a"
    return str(moment)


def _record_time(record: object) -> Optional[object]:
    for attr in ("captured_at", "time", "start_time", "stroke_time"):
        if hasattr(record, attr) and getattr(record, attr) is not None:
            return getattr(record, attr)
    return None


def _chunk_stream_name(chunk: object) -> str:
    return getattr(chunk, "stream_name", "?")


def _check(report: ValidationReport, stream_name: str, moment: Optional[object],
           assert_name: str, check: Callable[[], None]) -> None:
    try:
        check()
    except AssertionError as exc:
        report.violations.append(Violation(
            stream_name=stream_name,
            at=_time_str(moment),
            assert_name=assert_name,
            reason=str(exc),
        ))


def validate_meeting_data(meeting: adt.MeetingData) -> ValidationReport:
    report = ValidationReport(meeting_uid=meeting.meeting_uid)

    # ---- global: role map keys ⊆ user_names ----
    _check(report, "meeting", None, "role_map_keys_in_user_names",
           lambda: adt.assert_role_map_keys_in_user_names(meeting))

    # ---- file layer: unique names, permutation order, file ids present in the store ----
    _check(report, "file_desktop", None, "file_desktop_unique_names",
           lambda: adt.assert_file_desktop_unique_names(meeting.file_desktop))
    _check(report, "file_desktop", None, "file_desktop_distinct_file_ids",
           lambda: adt.assert_file_desktop_distinct_file_ids(meeting.file_desktop))
    _check(report, "file_desktop", None, "file_desktop_member_order",
           lambda: adt.assert_file_desktop_member_order(meeting.file_desktop))
    for file_id in adt.file_ids_of_desktop(meeting.file_desktop):
        _check(report, "file_desktop", None, "file_id_in_store",
               lambda f=file_id: adt.assert_file_id_in_store(f, meeting.file_kv_store))

    # ---- collect control/chat records for cross-chunk checks ----
    controls: list[adt.TerminalControl] = []
    chat_messages: list[adt.ChatMessage] = []
    for chunk in meeting.sorted_stream_chunks:
        if isinstance(chunk, adt.TerminalControlStreamChunk):
            controls.extend(chunk.chunks)
        elif isinstance(chunk, adt.ChatStreamChunk):
            chat_messages.extend(chunk.chunks)

    role_of = lambda name: meeting.agent_name2role.get(name)  # noqa: E731

    # ---- run record-level Asserts over each stream, record by record ----
    for chunk in meeting.sorted_stream_chunks:
        if isinstance(chunk, adt.ScreenStreamChunk):
            _validate_screen_chunk(report, chunk)
        elif isinstance(chunk, adt.TerminalControlStreamChunk):
            for control in chunk.chunks:
                _check(report, "terminal_control", control.start_time,
                       "terminal_control_capable",
                       lambda c=control: adt.assert_terminal_control_capable(c, role_of))

        # ---- ordering: records within a chunk are non-decreasing in time ----
        _check(report, _chunk_stream_name(chunk), None, "records_time_ascending",
               lambda c=chunk: _assert_records_ascending(c))

    # ---- ordering: sorted_stream_chunks are non-decreasing by first-record time ----
    _check(report, "meeting", None, "chunks_start_time_ascending",
           lambda: _assert_chunks_ascending(meeting.sorted_stream_chunks))

    return report


def _validate_screen_chunk(report: ValidationReport, chunk: adt.ScreenStreamChunk) -> None:
    for capture in chunk.chunks:
        checks = [
            ("plain_of_matches", lambda c=capture: adt.assert_screen_plain_matches(c)),
            ("row_widths_fit", lambda c=capture: adt.assert_screen_row_widths_fit(c)),
            ("rows_match_height", lambda c=capture: adt.assert_screen_rows_match_height(c)),
            ("cursor_in_bounds", lambda c=capture: adt.assert_cursor_in_bounds(c)),
            ("segments_valid", lambda c=capture: adt.assert_segments_valid(c)),
        ]
        for assert_name, check in checks:
            _check(report, "screen", capture.captured_at, assert_name, check)


def _assert_records_ascending(chunk: object) -> None:
    records = getattr(chunk, "chunks", [])
    previous = None
    for record in records:
        moment = _record_time(record)
        if moment is not None:
            if previous is not None and moment < previous:
                raise AssertionError(
                    f"{_chunk_stream_name(chunk)} chunk records out of order: "
                    f"{previous} then {moment}"
                )
            previous = moment


def _assert_chunks_ascending(chunks: list) -> None:
    previous = None
    for chunk in chunks:
        records = getattr(chunk, "chunks", [])
        if not records:
            continue
        first = _record_time(records[0])
        if first is not None:
            if previous is not None and first < previous:
                raise AssertionError(
                    f"sorted_stream_chunks out of order: chunk start {first} "
                    f"before previous {previous}"
                )
            previous = first


def validate_run_dir(run_dir: Path) -> tuple[ValidationReport, loader.LoadSummary]:
    """Load and validate once, for the G14 full audit and the CLI."""
    meeting, summary = loader.load_meeting_data_from_run(run_dir)
    report = validate_meeting_data(meeting)
    return report, summary
