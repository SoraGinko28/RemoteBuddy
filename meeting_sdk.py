"""meeting_sdk — meeting_sdk.viba 的 Python 实现：从一场会议里取数的只读接口。

设计（签名）在 MEETING_ADT.md 的「取数据接口」那一章（模块名 meeting_sdk）：每个函数带泛型
参数 ``Ctx``、吃一个 ``$ctx``，返回 Result，签名里只有稳定类型。这一层是它的实现。

**对外的接口就是 meeting_sdk.viba 里的那 34 个**：名字落 snake_case，参数、返回都照设计，不多导出
任何东西——签名里那些类型（``mc.ChatReference``、``mc.AgentRole``…）属于 meeting_core，不从这里拿。

这一版的绑定（协议说 Ctx / Data 由实现方绑）：

* ``Ctx`` = :class:`meeting_adt.MeetingData`：一份会议记录。
* ``Result[T]`` = viba 的 ``Ok`` / ``Err``（meeting_core_reflect 能把它们读成 Result 的两支）。
* 有名字的稳定类型用 meeting_adt 里同名的 dataclass：``FileId`` / ``VersionedFile`` /
  ``ChatMessage`` / ``ScoreMessage`` / 各种 Reference（`FileKvStore` 是记录层的类型）。
* 屏幕那一份状态（``mc.ScreenContent``）与文件那一份状态（``mc.FileState``）用 ``dict``，
  键就是设计里的 tag 名：``{"text", "segments", "cursor_x", "cursor_y", "width", "height",
  "captured_at"}``、``{"name", "meta", "content"}``。

读是主：取数函数一律返回 Result——查不到是 ``Err``，不是异常。写只有文件层那两个
（``open_file`` / ``write_text``），而且只往版本链上追加：旧版永远留着，as-of 照旧读得到。
末尾那组 ``make_*`` 不吃 ``ctx``，只把引用这类值造出来。
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

from viba.type import Err as _Err, Ok as _Ok, Result as _Result  # noqa: E402

_REFERENCE_TYPES = (adt.ScreenReference, adt.ChatReference, adt.ScoreReference,
                    adt.TerminalControlReference, adt.VersionedFileReference,
                    adt.VersionedFileContentReference)

Ctx = adt.MeetingData  # 这一版的绑定：Ctx 就是一份会议记录（协议说 Ctx 由实现方绑）


# ----------------------------------------------------------------------
# 取数：时间与流
# ----------------------------------------------------------------------


def chunk_times(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """这段时间里每条 chunk 的时刻。"""
    moments = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is not None and _within(moment, start, end):
            moments.append(moment)
    return _Ok(sorted(moments))


def streams_between(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """这段时间里出现过的流名。"""
    names = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is None or not _within(moment, start, end):
            continue
        if getattr(chunk, "stream_name", "") and chunk.stream_name not in names:
            names.append(chunk.stream_name)
    return _Ok(names)


# ----------------------------------------------------------------------
# 取数：聊天与评分
# ----------------------------------------------------------------------


def chat_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _Ok([m for m in _records(ctx, adt.ChatStreamChunk) if _within(m.time, start, end)])


def score_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _Ok([m for m in _records(ctx, adt.ScoreStreamChunk) if _within(m.time, start, end)])


def chat_message_at(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """一个人在一个时刻只有一条。"""
    for message in _records(ctx, adt.ChatStreamChunk):
        if message.time == time and message.user_name == user:
            return _Ok(message)
    return _Err(f"聊天流里没有 {user!r} 在 {time} 的那条")


def resolve_chat_reference(ctx: Ctx, ref: "adt.ChatReference") -> _Result:
    return chat_message_at(ctx, ref.msg_time, ref.user_name)


def resolve_chat_content_reference(ctx: Ctx, ref: "adt.ChatReference") -> _Result:
    """引用那一段的原文。"""
    given = resolve_chat_reference(ctx, ref)
    if isinstance(given, _Err):
        return given
    return _Ok(_region_of(_chat_message_text(given.value), ref.region))


def resolve_score_reference(ctx: Ctx, ref: "adt.ScoreReference") -> _Result:
    for message in _records(ctx, adt.ScoreStreamChunk):
        if message.time == ref.judge_time and message.judge_name == ref.judge_name:
            return _Ok(message)
    return _Err(f"评分流里没有 {ref.judge_name!r} 在 {ref.judge_time} 的那条")


def chat_message_references(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """这条消息带的引用，按出现顺序。"""
    given = chat_message_at(ctx, time, user)
    if isinstance(given, _Err):
        return given
    return _Ok([part for part in given.value.message if isinstance(part, _REFERENCE_TYPES)])


def score_message_reference(ctx: Ctx, time: _dt.datetime, judge: str) -> _Result:
    """这条评分带的引用。"""
    given = resolve_score_reference(ctx, adt.ScoreReference(judge_time=time, judge_name=judge))
    if isinstance(given, _Err):
        return given
    if given.value.reference is None:
        return _Err(f"{judge!r} 在 {time} 那条评分没有引用")
    return _Ok(given.value.reference)


def first_chunk_time(ctx: Ctx) -> _Result:
    """会议里第一条 chunk 的时刻。"""
    moments = [m for m in (_chunk_time(c) for c in ctx.sorted_stream_chunks) if m is not None]
    if not moments:
        return _Err("这场会议没有任何 chunk")
    return _Ok(min(moments))


def last_chunk_time(ctx: Ctx) -> _Result:
    """最后一条。"""
    moments = [m for m in (_chunk_time(c) for c in ctx.sorted_stream_chunks) if m is not None]
    if not moments:
        return _Err("这场会议没有任何 chunk")
    return _Ok(max(moments))


# ----------------------------------------------------------------------
# 取数：屏幕
# ----------------------------------------------------------------------


def screen_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """这一时刻那一屏（内容，不是抓屏记录本身）。"""
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == time:
            return _Ok(_screen_state(capture))
    return _Err(f"屏幕流里没有 {time} 那一屏")


def resolve_screen_reference(ctx: Ctx, ref: "adt.ScreenReference") -> _Result:
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == ref.capture_time:
            return _Ok(_screen_state(capture))
    return _Err(f"屏幕流里没有 {ref.capture_time} 那一屏")


# ----------------------------------------------------------------------
# 取数：人
# ----------------------------------------------------------------------


def participants(ctx: Ctx) -> _Result:
    return _Ok(sorted(adt.user_names_of_meeting_data(ctx)))


def roles(ctx: Ctx) -> _Result:
    return _Ok(dict(ctx.agent_name2role))


def owner_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """哪一刻谁拿着终端；nil = 此刻无人控制。"""
    return _Ok(adt.owner_at(time, _records(ctx, adt.TerminalControlStreamChunk)))


def control_owner_of(ctx: Ctx, ref: "adt.TerminalControlReference") -> _Result:
    """那一次接管 / 交还的接管人；nil = 交还之后没人接手。"""
    for control in _records(ctx, adt.TerminalControlStreamChunk):
        if control.start_time == ref.start_time and (control.user_name or None) == ref.user_name:
            return _Ok(control.user_name or None)
    return _Err(f"控制流里没有 {ref.start_time} 那一条")


# ----------------------------------------------------------------------
# 取数：文件层
# ----------------------------------------------------------------------


def file_id_by_path(ctx: Ctx, path: str) -> _Result:
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
                    return _Ok(member)
        return _Err(f"没有 {path!r} 这个文件")
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
                        return _Ok(file_id)
        return _Err(f"没有 {path!r} 这个文件")
    return _Err(f"路径要么 /workspace/entry，要么 /workspace/fold/entry：{path!r}")


def file_datetimes(ctx: Ctx, file: "adt.FileId") -> _Result:
    """这个文件改过的那些时刻：三条链上所有版本时刻的并集，去重升序。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(file.file_id)
    if versioned_file is None:
        return _Err(f"文件库里没有 {file.file_id!r}")
    moments = {version.time for _, chain in _chains(versioned_file)
               for version in chain if version.time is not None}
    return _Ok(sorted(moments))


def files_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """某一时刻每个文件是什么（那一刻还没有的版本，这个文件就不在里面）。"""
    out: Dict[str, dict] = {}
    for file_id, versioned_file in ctx.file_kv_store.file_id2file.items():
        state = _file_state_at(versioned_file, time)
        if state is not None:
            out[file_id] = state
    return _Ok(out)


def latest_file(ctx: Ctx, file: "adt.FileId") -> _Result:
    """最新的那一份状态。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(file.file_id)
    if versioned_file is None:
        return _Err(f"文件库里没有 {file.file_id!r}")
    return _Ok(_file_state(versioned_file))


def resolve_versioned_file_reference(ctx: Ctx, ref: "adt.VersionedFileReference") -> _Result:
    """引用那一刻的文件状态。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(ref.file_id.file_id)
    if versioned_file is None:
        return _Err(f"文件库里没有 {ref.file_id.file_id!r}")
    state = _file_state_at(versioned_file, ref.version_time)
    if state is None:
        return _Err(f"{ref.file_id.file_id!r} 在 {ref.version_time} 还没有这一版")
    return _Ok(state)


def resolve_versioned_file_content_reference(
        ctx: Ctx, ref: "adt.VersionedFileContentReference") -> _Result:
    """引用那一段的原文。"""
    given = resolve_versioned_file_reference(ctx, ref.versioned_file_ref)
    if isinstance(given, _Err):
        return given
    return _Ok(_region_of(given.value["content"] or "", ref.region))


# ----------------------------------------------------------------------
# 写：只写文件层，而且只加版本（旧版永远留着）
# ----------------------------------------------------------------------


def open_file(ctx: Ctx, file_path: str, time: _dt.datetime, user_name: str) -> _Result:
    """按路径打开；路径上没有就新建。同一路径来回都是同一个 FileId。

    新建的文件：name = 路径末段、content = ""（各记一版），状态链空着（可写）；
    成员多了一个，$reorder_indexes 跟着记一版。
    """
    parts = [p for p in file_path.split("/") if p]
    if len(parts) not in (2, 3):
        return _Err(f"路径要么 /workspace/entry，要么 /workspace/fold/entry：{file_path!r}")
    workspace_name = parts[0]
    for workspace in ctx.file_desktop.workspaces:
        if workspace.workspace_name != workspace_name:
            continue
        if workspace.workspace_status == adt.DataStatus.READ_ONLY:
            return _Err(f"工作区 {workspace_name!r} 只读，不能新建文件")
        if len(parts) == 2:
            entry_name = parts[1]
            for member in workspace.member_files:
                if isinstance(member, adt.FileFold):
                    if member.fold_name == entry_name:
                        return _Err(f"{file_path!r} 那一层已经有同名的 fold")
                elif member.entry_name == entry_name:
                    return _Ok(member)          # 已经有：打开
            opened = adt.FileId(file_id=_new_file_id(ctx), entry_name=entry_name)
            workspace.member_files.append(opened)
            _mount_new_file(ctx, opened, time, user_name)
            return _Ok(opened)
        fold_name, entry_name = parts[1], parts[2]
        for member in workspace.member_files:
            if not isinstance(member, adt.FileFold) or member.fold_name != fold_name:
                continue
            for existing in member.member_file_ids:
                if existing.entry_name == entry_name:
                    return _Ok(existing)        # 已经有：打开
            opened = adt.FileId(file_id=_new_file_id(ctx), entry_name=entry_name)
            member.member_file_ids.append(opened)
            _mount_new_file(ctx, opened, time, user_name)
            return _Ok(opened)
        return _Err(f"工作区 {workspace_name!r} 里没有 {fold_name!r} 这个 fold")
    return _Err(f"没有 {workspace_name!r} 这个 workspace")


def write_text(ctx: Ctx, file_id: "adt.FileId", text_content: str, time: _dt.datetime,
               user_name: str) -> _Result:
    """往文件的内容链上加一版：新版 = 旧全文 + 这段，旧的版本原样留着。"""
    versioned_file = ctx.file_kv_store.file_id2file.get(file_id.file_id)
    if versioned_file is None:
        return _Err(f"文件库里没有 {file_id.file_id!r}")
    status = _latest_of(versioned_file.file_meta)
    if status == adt.DataStatus.READ_ONLY:
        return _Err(f"{file_id.file_id!r} 只读")
    if status == adt.DataStatus.DELETED:
        return _Err(f"{file_id.file_id!r} 已删除")
    old_content = _latest_of(versioned_file.file_content) or ""
    versioned_file.file_content.append(adt.Versioned(old_content + text_content, time, user_name))
    return _Ok(None)


def read_text(ctx: Ctx, file: "adt.FileId") -> _Result:
    """read_text := latest_file（设计里的写法就是别名）。"""
    return latest_file(ctx, file)


# ----------------------------------------------------------------------
# 构造：造引用这类值（只造值，不碰记录）
# ----------------------------------------------------------------------


def make_text_region(start: int, end: int) -> "adt.TextRegion":
    return adt.TextRegion(start=start, end=end)


def make_screen_reference(capture_time: _dt.datetime, stream_name: str,
                          region: "adt.TextRegion") -> "adt.ScreenReference":
    return adt.ScreenReference(capture_time=capture_time, stream_name=stream_name,
                               region=region)


def make_chat_reference(msg_time: _dt.datetime, user_name: str,
                        region: "adt.TextRegion") -> "adt.ChatReference":
    return adt.ChatReference(msg_time=msg_time, user_name=user_name, region=region)


def make_score_reference(judge_time: _dt.datetime, judge_name: str) -> "adt.ScoreReference":
    return adt.ScoreReference(judge_time=judge_time, judge_name=judge_name)


def make_terminal_control_reference(
        start_time: _dt.datetime, user_name: Optional[str],
        region: "adt.TextRegion") -> "adt.TerminalControlReference":
    return adt.TerminalControlReference(start_time=start_time, user_name=user_name,
                                        region=region)


def make_versioned_file_reference(
        file_id: "adt.FileId",
        version_time: _dt.datetime) -> "adt.VersionedFileReference":
    return adt.VersionedFileReference(file_id=file_id, version_time=version_time)


def make_versioned_file_content_reference(
        versioned_file_ref: "adt.VersionedFileReference",
        region: "adt.TextRegion") -> "adt.VersionedFileContentReference":
    return adt.VersionedFileContentReference(versioned_file_ref=versioned_file_ref,
                                             region=region)


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
    """设计里那个 FileState：$name * $meta * $content，各取链尾。"""
    return {key: _latest_of(chain) for key, chain in _chains(versioned_file)}


def _file_state_at(versioned_file: "adt.VersionedFile",
                   moment: _dt.datetime) -> Optional[dict]:
    """某一刻那一份状态：每个部件取链上 ≤ moment 的最后一版。

    name / content 那一刻还没有 -> 这个文件在那一刻不存在（None）；
    meta 那一刻还没有 -> 缺省，可写（$meta nil）。
    """
    state = {}
    for key, chain in _chains(versioned_file):
        version = _version_at(chain, moment)
        if version is None and key != "meta":
            return None
        state[key] = version.data if version is not None else None
    return state


def _chains(versioned_file: "adt.VersionedFile"):
    """三个部件各自的版本链，键就是设计里的 tag 名。"""
    return (("name", versioned_file.file_name), ("meta", versioned_file.file_meta),
            ("content", versioned_file.file_content))


def _latest_of(chain: List) -> object:
    """链尾就是最新的那一版的值；空链给 None。"""
    return chain[-1].data if chain else None


def _version_at(chain: List, moment: Optional[_dt.datetime]):
    """链上 time ≤ moment 的最后一版；一版都没有就是 None。"""
    found = None
    for version in chain:
        if moment is not None and version.time is not None and version.time > moment:
            break
        found = version
    return found


def _new_file_id(ctx: Ctx) -> str:
    """文件库里没用过的内部 id。"""
    taken = set(ctx.file_kv_store.file_id2file.keys())
    number = 1
    while f"f{number}" in taken:
        number += 1
    return f"f{number}"


def _mount_new_file(ctx: Ctx, file_id: "adt.FileId", time: _dt.datetime,
                    user_name: str) -> None:
    """新建文件：三条链各一版；成员多了一个，桌面顺序快照跟着记一版。"""
    ctx.file_kv_store.file_id2file[file_id.file_id] = adt.VersionedFile(
        file_name=[adt.Versioned(file_id.entry_name, time, user_name)],
        file_meta=[],                                 # 状态链空着 = 可写
        file_content=[adt.Versioned("", time, user_name)],
    )
    desktop = ctx.file_desktop
    desktop.reorder_indexes = adt.Versioned(adt.identity_desktop_order(desktop), time,
                                            user_name)


__all__ = [
    "chunk_times",
    "streams_between",
    "chat_messages",
    "score_messages",
    "chat_message_at",
    "resolve_chat_reference",
    "resolve_chat_content_reference",
    "resolve_score_reference",
    "chat_message_references",
    "score_message_reference",
    "first_chunk_time",
    "last_chunk_time",
    "screen_at",
    "resolve_screen_reference",
    "participants",
    "roles",
    "owner_at",
    "control_owner_of",
    "file_id_by_path",
    "file_datetimes",
    "files_at",
    "latest_file",
    "resolve_versioned_file_reference",
    "resolve_versioned_file_content_reference",
    "open_file",
    "write_text",
    "read_text",
    "make_text_region",
    "make_screen_reference",
    "make_chat_reference",
    "make_score_reference",
    "make_terminal_control_reference",
    "make_versioned_file_reference",
    "make_versioned_file_content_reference",
]
