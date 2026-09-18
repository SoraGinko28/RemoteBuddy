"""meeting_sdk — meeting_sdk.viba 的 Python 实现：从一场会议里取数的只读接口。

设计（签名）在 MEETING_ADT.md 的「取数据接口」那一章（模块名 meeting_sdk）：每个函数带泛型
参数 ``Ctx``、吃一个 ``$ctx``，返回 ``Result``，签名里只有稳定类型。这一层是它的实现。

这一版的绑定（协议没规定 Ctx / Data 长什么样，实现方自己绑）：

* ``Ctx`` = :class:`meeting_adt.MeetingData`：一份会议记录。
* ``Result[T]`` = viba 的 ``Ok`` / ``Err``（meeting_core_reflect 能把它们读成 Result 的两支）。
* 有名字的稳定类型用 meeting_adt 里同名的 dataclass：``FileId`` / ``VersionedFile`` /
  ``FileKvStore`` / ``ChatMessage`` / ``ScoreMessage`` / 各种 Reference。
* 设计里的匿名积（屏幕那一份状态、文件那一份状态）用 ``dict``，键就是设计里的 tag 名：
  ``{"text", "segments", "cursor_x", "cursor_y", "width", "height", "captured_at"}``、
  ``{"name", "meta", "content"}``。

只有读：没有写入、修改、删除。取数函数一律返回 ``Result``——查不到是 ``Err``，不是异常。
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import meeting_adt as adt

# viba 是子模块，包在它的根目录下面；把子模块根加进 sys.path，import 才找得到。
_VENDORED_VIBA = Path(__file__).resolve().parent / "viba"
if (_VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file() and str(_VENDORED_VIBA) not in sys.path:
    sys.path.insert(0, str(_VENDORED_VIBA))

from viba.type import Err, Ok, Result  # noqa: E402

Ctx = adt.MeetingData

# 接口吃进 / 吐出的那些类型，从这一份里拿，调用方不必去碰记录层。
MeetingData = adt.MeetingData
AgentRole = adt.AgentRole
DataStatus = adt.DataStatus
TextRegion = adt.TextRegion
FileId = adt.FileId
VersionedFile = adt.VersionedFile
FileKvStore = adt.FileKvStore
ChatMessage = adt.ChatMessage
ScoreMessage = adt.ScoreMessage
ScreenReference = adt.ScreenReference
ChatReference = adt.ChatReference
ScoreReference = adt.ScoreReference
TerminalControlReference = adt.TerminalControlReference
VersionedFileReference = adt.VersionedFileReference
VersionedFileContentReference = adt.VersionedFileContentReference


# ----------------------------------------------------------------------
# 取数：时间与流
# ----------------------------------------------------------------------


def chunk_times(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> Result:
    """这段时间里每条 chunk 的时刻。"""
    moments = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is not None and _within(moment, start, end):
            moments.append(moment)
    return Ok(sorted(moments))


def streams_between(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> Result:
    """这段时间里出现过的流名。"""
    names = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is None or not _within(moment, start, end):
            continue
        if getattr(chunk, "stream_name", "") and chunk.stream_name not in names:
            names.append(chunk.stream_name)
    return Ok(names)


# ----------------------------------------------------------------------
# 取数：聊天与评分
# ----------------------------------------------------------------------


def chat_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> Result:
    return Ok([m for m in _records(ctx, adt.ChatStreamChunk) if _within(m.time, start, end)])


def score_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> Result:
    return Ok([m for m in _records(ctx, adt.ScoreStreamChunk) if _within(m.time, start, end)])


def chat_message_at(ctx: Ctx, time: _dt.datetime, user: str) -> Result:
    """一个人在一个时刻只有一条。"""
    for message in _records(ctx, adt.ChatStreamChunk):
        if message.time == time and message.user_name == user:
            return Ok(message)
    return Err(f"聊天流里没有 {user!r} 在 {time} 的那条")


def resolve_chat_reference(ctx: Ctx, ref: "adt.ChatReference") -> Result:
    return chat_message_at(ctx, ref.msg_time, ref.user_name)


def resolve_chat_content_reference(ctx: Ctx, ref: "adt.ChatReference") -> Result:
    """引用那一段的原文。"""
    given = resolve_chat_reference(ctx, ref)
    if isinstance(given, Err):
        return given
    return Ok(_region_of(_chat_message_text(given.value), ref.region))


def resolve_score_reference(ctx: Ctx, ref: "adt.ScoreReference") -> Result:
    for message in _records(ctx, adt.ScoreStreamChunk):
        if message.time == ref.judge_time and message.judge_name == ref.judge_name:
            return Ok(message)
    return Err(f"评分流里没有 {ref.judge_name!r} 在 {ref.judge_time} 的那条")


# ----------------------------------------------------------------------
# 取数：屏幕
# ----------------------------------------------------------------------


def screen_at(ctx: Ctx, time: _dt.datetime) -> Result:
    """这一时刻那一屏（内容，不是抓屏记录本身）。"""
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == time:
            return Ok(_screen_state(capture))
    return Err(f"屏幕流里没有 {time} 那一屏")


def resolve_screen_reference(ctx: Ctx, ref: "adt.ScreenReference") -> Result:
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == ref.capture_time:
            return Ok(_screen_state(capture))
    return Err(f"屏幕流里没有 {ref.capture_time} 那一屏")


# ----------------------------------------------------------------------
# 取数：人
# ----------------------------------------------------------------------


def participants(ctx: Ctx) -> Result:
    return Ok(sorted(adt.user_names_of_meeting_data(ctx)))


def roles(ctx: Ctx) -> Result:
    return Ok(dict(ctx.agent_name2role))


def owner_at(ctx: Ctx, time: _dt.datetime) -> Result:
    """哪一刻谁拿着终端；nil = 此刻无人控制。"""
    return Ok(adt.owner_at(time, _records(ctx, adt.TerminalControlStreamChunk)))


def control_owner_of(ctx: Ctx, ref: "adt.TerminalControlReference") -> Result:
    """那一次接管 / 交还的接管人；nil = 交还之后没人接手。"""
    for control in _records(ctx, adt.TerminalControlStreamChunk):
        if control.start_time == ref.start_time and (control.user_name or None) == ref.user_name:
            return Ok(control.user_name or None)
    return Err(f"控制流里没有 {ref.start_time} 那一条")


# ----------------------------------------------------------------------
# 取数：文件层
# ----------------------------------------------------------------------


def file_id_by_path(ctx: Ctx, path: str) -> Result:
    """/workspace_name/entry_name 或 /workspace_name/fold_name/entry_name。

    每层名字唯一（见记录层 FileDesktop 的 Assert），所以路径定得到一条。
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2:
        workspace_name, entry_name = parts
        for workspace in ctx.file_desktop.workspaces:
            if workspace.workspace_name != workspace_name:
                continue
            for member in workspace.member_files:
                if isinstance(member, adt.FileId) and member.entry_name == entry_name:
                    return Ok(member)
        return Err(f"没有 {path!r} 这个文件")
    if len(parts) == 3:
        workspace_name, fold_name, entry_name = parts
        for workspace in ctx.file_desktop.workspaces:
            if workspace.workspace_name != workspace_name:
                continue
            for member in workspace.member_files:
                if not isinstance(member, adt.FileFold) or member.fold_name != fold_name:
                    continue
                for file_id in member.member_file_ids:
                    if file_id.entry_name == entry_name:
                        return Ok(file_id)
        return Err(f"没有 {path!r} 这个文件")
    return Err(f"路径要么 /workspace/entry，要么 /workspace/fold/entry：{path!r}")


def file_kv_store(ctx: Ctx) -> Result:
    return Ok(ctx.file_kv_store)


def files_at(ctx: Ctx, time: _dt.datetime) -> Result:
    """某一时刻每个文件是什么（那一刻还没有的版本，这个文件就不在里面）。"""
    out: Dict[str, dict] = {}
    for file_id, versioned_file in ctx.file_kv_store.file_id2file.items():
        state = _file_state_at(versioned_file, time)
        if state is not None:
            out[file_id] = state
    return Ok(out)


def latest_file(ctx: Ctx, file: "adt.FileId") -> Result:
    """最新的那一份状态。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(file.file_id)
    if versioned_file is None:
        return Err(f"文件库里没有 {file.file_id!r}")
    return Ok(_file_state(versioned_file))


def resolve_versioned_file_reference(ctx: Ctx, ref: "adt.VersionedFileReference") -> Result:
    """引用那一刻的文件状态。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(ref.file_id.file_id)
    if versioned_file is None:
        return Err(f"文件库里没有 {ref.file_id.file_id!r}")
    state = _file_state_at(versioned_file, ref.version_time)
    if state is None:
        return Err(f"{ref.file_id.file_id!r} 在 {ref.version_time} 还没有这一版")
    return Ok(state)


def resolve_versioned_file_content_reference(
        ctx: Ctx, ref: "adt.VersionedFileContentReference") -> Result:
    """引用那一段的原文。"""
    given = resolve_versioned_file_reference(ctx, ref.versioned_file_ref)
    if isinstance(given, Err):
        return given
    return Ok(_region_of(given.value["content"] or "", ref.region))


# ----------------------------------------------------------------------
# 私有小工具（这一层认识 meeting_adt 的记录）
# ----------------------------------------------------------------------


def _records(ctx: Ctx, chunk_type) -> List:
    out: List = []
    for chunk in ctx.sorted_stream_chunks:
        if isinstance(chunk, chunk_type):
            out.extend(getattr(chunk, "chunks", []))
    return out


def _chunk_time(chunk) -> Optional[_dt.datetime]:
    if isinstance(chunk, adt.ScreenStreamChunk):
        return chunk.chunks[0].captured_at if chunk.chunks else None
    if isinstance(chunk, (adt.ChatStreamChunk, adt.ScoreStreamChunk)):
        return chunk.chunks[0].time if chunk.chunks else None
    if isinstance(chunk, adt.TerminalControlStreamChunk):
        return chunk.chunks[0].start_time if chunk.chunks else None
    return None


def _within(moment: Optional[_dt.datetime], start: _dt.datetime,
            end: _dt.datetime) -> bool:
    if moment is None:
        return False
    if start is not None and moment < start:
        return False
    if end is not None and moment > end:
        return False
    return True


def _chat_message_text(message: "adt.ChatMessage") -> str:
    parts = []
    for part in message.message:
        if isinstance(part, adt.RawMessage):
            parts.append(part.text)
        elif isinstance(part, adt.AtUserMessage):
            parts.append(f"@{part.user_name}")
    return "".join(parts)


def _region_of(text: str, region: Optional["adt.TextRegion"]) -> str:
    if region is None:
        return text
    return text[region.start:region.end]


def _screen_state(capture: "adt.ScreenCapture") -> dict:
    """设计里那个匿名积：$text * $segments * $cursor_x * $cursor_y * $width * $height * $captured_at"""
    return {
        "text": capture.screen_data,
        "segments": list(capture.terminal_text_content),
        "cursor_x": capture.cursor_x,
        "cursor_y": capture.cursor_y,
        "width": capture.screen_width,
        "height": capture.screen_height,
        "captured_at": capture.captured_at,
    }


def _file_state(versioned_file: "adt.VersionedFile") -> dict:
    """设计里那个匿名积：$name * $meta * $content"""
    return {
        "name": versioned_file.file_name.data,
        "meta": versioned_file.file_meta.data,
        "content": versioned_file.file_content.data,
    }


def _file_state_at(versioned_file: "adt.VersionedFile",
                   moment: _dt.datetime) -> Optional[dict]:
    """某一时刻那一份状态；那一刻还没有的版本 -> None（这个文件在那一刻不存在）。

    文件库里每个部件只留一版，所以 as-of 只问"那一版赶上了没有"。
    """
    state = {}
    for key, part in (("name", versioned_file.file_name),
                      ("meta", versioned_file.file_meta),
                      ("content", versioned_file.file_content)):
        if part.time is not None and moment is not None and part.time > moment:
            return None
        state[key] = part.data
    return state


__all__ = [
    "AgentRole",
    "ChatMessage",
    "ChatReference",
    "Ctx",
    "DataStatus",
    "Err",
    "FileId",
    "FileKvStore",
    "MeetingData",
    "Ok",
    "Result",
    "ScoreMessage",
    "ScoreReference",
    "ScreenReference",
    "TerminalControlReference",
    "TextRegion",
    "VersionedFile",
    "VersionedFileContentReference",
    "VersionedFileReference",
    "chat_message_at",
    "chat_messages",
    "chunk_times",
    "control_owner_of",
    "file_id_by_path",
    "file_kv_store",
    "files_at",
    "latest_file",
    "owner_at",
    "participants",
    "resolve_chat_content_reference",
    "resolve_chat_reference",
    "resolve_score_reference",
    "resolve_screen_reference",
    "resolve_versioned_file_content_reference",
    "resolve_versioned_file_reference",
    "roles",
    "screen_at",
    "score_messages",
    "streams_between",
]
