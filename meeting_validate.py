"""meeting_validate — 把 MeetingData 上全部 ADT Assert 跑一遍，产出违规清单。

设计初衷：
- 只收集 Assert（数据不变量），违规即「记录不符合 ADT」，一条一条记下来，
  不中断、不修改数据。
- Hint（回应提醒 / 链顶 human 等）是运营策略，不是数据不变量：
  本模块不拦，留 hints 扩展位。
- 每条违规给 [流 / 时间 / 断言名 / 原因]，供报告与 G14 全量审计。

覆盖的 Assert（对齐 MEETING_ADT.md / meeting_adt.py）：
  screen            plain_of==screen_data / 行宽≤width / 行数==height /
                    cursor 界内 / 段非空且相邻属性异
  terminal_control  owner ∈ terminal_capable（human 或 generator）
  keystroke         owner_at(stroke_time)==user_name
  score             summon_chained（passive 须被 @ 召唤）
  meeting           keys(agent_name2role) ⊆ user_names
  ordering          chunk 内记录按时间非降；sorted_stream_chunks 按首记录非降
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
    at: str            # 记录时间（ISO），无则 "n/a"
    assert_name: str   # Assert 的名字
    reason: str        # 违规原因（断言函数抛出的消息）


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

    # ---- 全局：role map keys ⊆ user_names ----
    _check(report, "meeting", None, "role_map_keys_in_user_names",
           lambda: adt.assert_role_map_keys_in_user_names(meeting))

    # ---- 收集 keystroke/control/chat/score 供跨 chunk 判定 ----
    controls: list[adt.TerminalControl] = []
    chat_messages: list[adt.ChatMessage] = []
    score_messages: list[adt.ScoreMessage] = []
    for chunk in meeting.sorted_stream_chunks:
        if isinstance(chunk, adt.TerminalControlStreamChunk):
            controls.extend(chunk.chunks)
        elif isinstance(chunk, adt.ChatStreamChunk):
            chat_messages.extend(chunk.chunks)
        elif isinstance(chunk, adt.ScoreStreamChunk):
            score_messages.extend(chunk.chunks)

    role_of = lambda name: meeting.agent_name2role.get(name)  # noqa: E731

    # ---- 逐流逐记录跑记录级 Assert ----
    for chunk in meeting.sorted_stream_chunks:
        if isinstance(chunk, adt.ScreenStreamChunk):
            _validate_screen_chunk(report, chunk)
        elif isinstance(chunk, adt.TerminalControlStreamChunk):
            for control in chunk.chunks:
                _check(report, "terminal_control", control.start_time,
                       "terminal_control_capable",
                       lambda c=control: adt.assert_terminal_control_capable(c, role_of))
        elif isinstance(chunk, adt.KeystrokeStreamChunk):
            for key in chunk.chunks:
                _check(report, "keystroke", key.stroke_time,
                       "keystroke_owner_matches",
                       lambda k=key: adt.assert_keystroke_owner_matches(k, controls))
        elif isinstance(chunk, adt.ScoreStreamChunk):
            for message in chunk.chunks:
                referenced = _resolve_score_reference(message, chat_messages)
                _check(report, "score", message.time,
                       "score_summon_chained",
                       lambda m=message, r=referenced: adt.assert_score_summon_chained(
                           m, role_of, r))

        # ---- ordering：chunk 内记录时间非降 ----
        _check(report, _chunk_stream_name(chunk), None, "records_time_ascending",
               lambda c=chunk: _assert_records_ascending(c))

    # ---- ordering：sorted_stream_chunks 按首记录时间非降 ----
    _check(report, "meeting", None, "chunks_start_time_ascending",
           lambda: _assert_chunks_ascending(meeting.sorted_stream_chunks))

    return report


def _resolve_score_reference(message: adt.ScoreMessage,
                             chat_messages: "list[adt.ChatMessage]"):
    """summon_chained 判定用的引用解析：只关心 chat_ref 是否指向一条召唤消息。"""
    reference = message.reference
    if reference is None:
        return None
    if isinstance(reference, adt.ChatReference):
        try:
            return adt.resolve_chat_reference(reference, chat_messages)
        except (LookupError, ValueError):
            return None
    return None  # screen/score 引用不能证明被召唤


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
    """加载 + 校验一次，供 G14 全量审计与 CLI 用。"""
    meeting, summary = loader.load_meeting_data_from_run(run_dir)
    report = validate_meeting_data(meeting)
    return report, summary
