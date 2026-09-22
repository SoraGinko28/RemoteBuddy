"""meeting_local_sdk -- the local binding of the meeting_sdk.viba contract.

Ctx is a Python object in this process: a meeting_adt.MeetingData record. Every
getter evaluates in place; nothing goes over a wire. For the remote binding
(Ctx is a meeting uid, every api is backed by a REST URL) see meeting_remote_sdk.

The read-only interface that takes data out of a meeting.

The design (signatures) is the data-interface chapter of MEETING_ADT.md
(module name meeting_sdk): every function carries the generic parameter ``Ctx``,
takes a ``$ctx``, and returns a Result; the signature mentions only stable
types. This layer is its implementation.

**The public interface is exactly the 33 in meeting_sdk.viba**: names fall in
snake_case, parameters and returns follow the design, and nothing else is
exported -- the types in the signatures (``mc.ChatReference``,
``mc.AgentRole``...) belong to meeting_core and are not taken from here.

The bindings of this version (the interface says Ctx / Data are bound by the
implementation):

* ``Ctx`` = :class:`meeting_adt.MeetingData`: a meeting record.
* ``Result[T]`` = viba's ``Ok`` / ``VibaProgramErr`` (meeting_core_reflect can read them as
  Result's two branches).
* Named stable types use the dataclass of the same name in meeting_adt:
  ``FileId`` / ``VersionedFile`` / ``ChatMessage`` / ``ScoreMessage`` / the
  various References (`FileKvStore` is a record-layer type).
* The screen state (``mc.ScreenContent``) and the file state (``mc.FileState``)
  use ``dict``, keyed by the design's tag names:
  ``{"text", "segments", "cursor_x", "cursor_y", "width", "height",
  "captured_at"}``, ``{"name", "meta", "content"}``.

Reading only: getters always return a Result -- a miss is an ``VibaProgramErr``, not an
exception, and no api writes (writes are the harness's job). The ``make_*`` group
at the end takes no ``ctx``; it only builds values such as references.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import meeting_adt as adt

# viba is a submodule, packed under its own root directory; add the submodule
# root to sys.path so the import finds it.
_VENDORED_VIBA = Path(__file__).resolve().parent / "viba"
if (_VENDORED_VIBA / "viba" / "viba_type_descriptor.py").is_file() and str(_VENDORED_VIBA) not in sys.path:
    sys.path.insert(0, str(_VENDORED_VIBA))

from viba.type import VibaProgramErr as _Err, Ok as _Ok, Result as _Result  # noqa: E402

_REFERENCE_TYPES = (adt.ScreenReference, adt.ChatReference, adt.ScoreReference,
                    adt.TerminalControlReference, adt.VersionedFileReference,
                    adt.VersionedFileContentReference)

_LAST_NOW: Optional[_dt.datetime] = None  # the last moment now() handed out

Ctx = adt.MeetingData  # this version's binding: Ctx is a meeting record (the
# interface says Ctx is bound by the implementation)


# ----------------------------------------------------------------------
# Getters: time and streams
# ----------------------------------------------------------------------


def now(ctx: Ctx) -> _Result:
    """The moment this process is at; never hands out a microsecond twice."""
    global _LAST_NOW
    moment = _dt.datetime.now(_dt.timezone.utc)
    if _LAST_NOW is not None and moment <= _LAST_NOW:
        moment = _LAST_NOW + _dt.timedelta(microseconds=1)
    _LAST_NOW = moment
    return _Ok(moment)


def chunk_times(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """The moment of every chunk in this span."""
    moments = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is not None and _within(moment, start, end):
            moments.append(moment)
    return _Ok(sorted(moments))


def streams_between(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """The stream names seen in this span."""
    names = []
    for chunk in ctx.sorted_stream_chunks:
        moment = _chunk_time(chunk)
        if moment is None or not _within(moment, start, end):
            continue
        if getattr(chunk, "stream_name", "") and chunk.stream_name not in names:
            names.append(chunk.stream_name)
    return _Ok(names)


# ----------------------------------------------------------------------
# Getters: chat and scores
# ----------------------------------------------------------------------


def chat_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _Ok([m for m in _records(ctx, adt.ChatStreamChunk) if _within(m.time, start, end)])


def score_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _Ok([m for m in _records(ctx, adt.ScoreStreamChunk) if _within(m.time, start, end)])


def chat_message_at(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """One person has one message at a moment."""
    for message in _records(ctx, adt.ChatStreamChunk):
        if message.time == time and message.user_name == user:
            return _Ok(message)
    return _Err(f"the chat stream has no message from {user!r} at {time}")


def resolve_chat_reference(ctx: Ctx, ref: "adt.ChatReference") -> _Result:
    return chat_message_at(ctx, ref.msg_time, ref.user_name)


def resolve_chat_content_reference(ctx: Ctx, ref: "adt.ChatReference") -> _Result:
    """The text the reference points at."""
    given = resolve_chat_reference(ctx, ref)
    if isinstance(given, _Err):
        return given
    return _Ok(_region_of(_chat_message_text(given.ok_value), ref.region))


def resolve_score_reference(ctx: Ctx, ref: "adt.ScoreReference") -> _Result:
    for message in _records(ctx, adt.ScoreStreamChunk):
        if message.time == ref.judge_time and message.judge_name == ref.judge_name:
            return _Ok(message)
    return _Err(f"the score stream has no score by {ref.judge_name!r} at {ref.judge_time}")


def chat_message_references(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """The references this message carries, in order."""
    given = chat_message_at(ctx, time, user)
    if isinstance(given, _Err):
        return given
    return _Ok([part for part in given.ok_value.message if isinstance(part, _REFERENCE_TYPES)])


def score_message_reference(ctx: Ctx, time: _dt.datetime, judge: str) -> _Result:
    """The reference this score carries."""
    given = resolve_score_reference(ctx, adt.ScoreReference(judge_time=time, judge_name=judge))
    if isinstance(given, _Err):
        return given
    if given.ok_value.reference is None:
        return _Err(f"the score by {judge!r} at {time} carries no reference")
    return _Ok(given.ok_value.reference)


def first_chunk_time(ctx: Ctx) -> _Result:
    """The moment of the meeting's first chunk."""
    moments = [m for m in (_chunk_time(c) for c in ctx.sorted_stream_chunks) if m is not None]
    if not moments:
        return _Err("this meeting has no chunk at all")
    return _Ok(min(moments))


def last_chunk_time(ctx: Ctx) -> _Result:
    """The last one."""
    moments = [m for m in (_chunk_time(c) for c in ctx.sorted_stream_chunks) if m is not None]
    if not moments:
        return _Err("this meeting has no chunk at all")
    return _Ok(max(moments))


# ----------------------------------------------------------------------
# Getters: screen
# ----------------------------------------------------------------------


def screen_times(ctx: Ctx) -> _Result:
    """The moments the screen stream captured, ascending."""
    return _Ok(sorted(capture.captured_at for capture in _records(ctx, adt.ScreenStreamChunk)))


def screen_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """The content of that screen at this moment (not the capture record)."""
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == time:
            return _Ok(_screen_state(capture))
    return _Err(f"the screen stream has no screen at {time}")


def resolve_screen_reference(ctx: Ctx, ref: "adt.ScreenReference") -> _Result:
    for capture in _records(ctx, adt.ScreenStreamChunk):
        if capture.captured_at == ref.capture_time:
            return _Ok(_screen_state(capture))
    return _Err(f"the screen stream has no screen at {ref.capture_time}")


# ----------------------------------------------------------------------
# Getters: people
# ----------------------------------------------------------------------


def participants(ctx: Ctx) -> _Result:
    return _Ok(sorted(adt.user_names_of_meeting_data(ctx)))


def roles(ctx: Ctx) -> _Result:
    return _Ok(dict(ctx.agent_name2role))


def owner_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """Who holds the terminal at a moment; nil = nobody holds it then."""
    return _Ok(adt.owner_at(time, _records(ctx, adt.TerminalControlStreamChunk)))


def control_owner_of(ctx: Ctx, ref: "adt.TerminalControlReference") -> _Result:
    """The taker of that take-over / hand-back; nil = nobody took over then."""
    for control in _records(ctx, adt.TerminalControlStreamChunk):
        if control.start_time == ref.start_time and (control.user_name or None) == ref.user_name:
            return _Ok(control.user_name or None)
    return _Err(f"the control stream has no record at {ref.start_time}")


# ----------------------------------------------------------------------
# Getters: the file layer
# ----------------------------------------------------------------------


def file_id_by_path(ctx: Ctx, path: str) -> _Result:
    """/workspace_name/entry_name or /workspace_name/fold_name/entry_name.

    Names are unique per level (see the record layer's FileDesktop Assert), so a
    path picks exactly one.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2:
        workspace_name, entry_name = parts
        for workspace in ctx.file_desktop.workspaces:
            if workspace.workspace_name != workspace_name:
                continue
            for member in workspace.member_files:
                if isinstance(member, adt.FileEntry) and member.entry_name == entry_name:
                    return _Ok(member.file_id)
        return _Err(f"no file at {path!r}")
    if len(parts) == 3:
        workspace_name, fold_name, entry_name = parts
        for workspace in ctx.file_desktop.workspaces:
            if workspace.workspace_name != workspace_name:
                continue
            for member in workspace.member_files:
                if not isinstance(member, adt.FileFold) or member.fold_name != fold_name:
                    continue
                for entry in member.member_file_ids:
                    if entry.entry_name == entry_name:
                        return _Ok(entry.file_id)
        return _Err(f"no file at {path!r}")
    return _Err(f"a path is either /workspace/entry or /workspace/fold/entry: {path!r}")


def file_datetimes(ctx: Ctx, file: "adt.FileId") -> _Result:
    """The moments this file changed: the union of all version times on the
    three chains, deduplicated and ascending."""
    versioned_file = ctx.file_kv_store.file_id2file.get(file.file_id)
    if versioned_file is None:
        return _Err(f"the file store has no {file.file_id!r}")
    moments = {version.time for _, chain in _chains(versioned_file)
               for version in chain if version.time is not None}
    return _Ok(sorted(moments))


def files_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """What each file is at a moment (a file whose version does not exist yet at
    that moment is not in there)."""
    out: Dict[str, dict] = {}
    for file_id, versioned_file in ctx.file_kv_store.file_id2file.items():
        state = _file_state_at(versioned_file, time)
        if state is not None:
            out[file_id] = state
    return _Ok(out)


def latest_file(ctx: Ctx, file: "adt.FileId") -> _Result:
    """The latest state."""
    versioned_file = ctx.file_kv_store.file_id2file.get(file.file_id)
    if versioned_file is None:
        return _Err(f"the file store has no {file.file_id!r}")
    return _Ok(_file_state(versioned_file))


def resolve_versioned_file_reference(ctx: Ctx, ref: "adt.VersionedFileReference") -> _Result:
    """The file state at the moment cited."""
    versioned_file = ctx.file_kv_store.file_id2file.get(ref.file_id.file_id)
    if versioned_file is None:
        return _Err(f"the file store has no {ref.file_id.file_id!r}")
    state = _file_state_at(versioned_file, ref.version_time)
    if state is None:
        return _Err(f"{ref.file_id.file_id!r} has no version at {ref.version_time}")
    return _Ok(state)


def resolve_versioned_file_content_reference(
        ctx: Ctx, ref: "adt.VersionedFileContentReference") -> _Result:
    """The text the reference points at."""
    given = resolve_versioned_file_reference(ctx, ref.versioned_file_ref)
    if isinstance(given, _Err):
        return given
    return _Ok(_region_of(given.ok_value["content"] or "", ref.region))


# ----------------------------------------------------------------------
# Writes: the file layer only, appending versions (old ones always stay)
# ----------------------------------------------------------------------


def read_text(ctx: Ctx, file: "adt.FileId") -> _Result:
    """read_text = latest_file (the design's spelling is an alias)."""
    return latest_file(ctx, file)


# ----------------------------------------------------------------------
# Construction: making values such as references (never touches a record)
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
# Private helpers (this layer knows meeting_adt's records)
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
    """The anonymous product in the design: $text * $segments * $cursor_x *
    $cursor_y * $width * $height * $captured_at"""
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
    """The FileState in the design: $name * $meta * $content, each taking the
    tail of its chain."""
    return {key: _latest_of(chain) for key, chain in _chains(versioned_file)}


def _file_state_at(versioned_file: "adt.VersionedFile",
                   moment: _dt.datetime) -> Optional[dict]:
    """The state at a moment: each part takes the last version on its chain with
    time <= moment.

    name / content not there yet at that moment -> the file does not exist at
    that moment (None);
    meta not there yet at that moment -> default, writable ($meta nil).
    """
    state = {}
    for key, chain in _chains(versioned_file):
        version = _version_at(chain, moment)
        if version is None and key != "meta":
            return None
        state[key] = version.data if version is not None else None
    return state


def _chains(versioned_file: "adt.VersionedFile"):
    """The version chain of each of the three parts, keyed by the design's tag
    names."""
    return (("name", versioned_file.file_name), ("meta", versioned_file.file_meta),
            ("content", versioned_file.file_content))


def _latest_of(chain: List) -> object:
    """The chain tail is the latest version's value; an empty chain -> None."""
    return chain[-1].data if chain else None


def _version_at(chain: List, moment: Optional[_dt.datetime]):
    """The last version on the chain with time <= moment; otherwise None."""
    found = None
    for version in chain:
        if moment is not None and version.time is not None and version.time > moment:
            break
        found = version
    return found


__all__ = [
    "now",
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
    "screen_times",
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
    "read_text",
    "make_text_region",
    "make_screen_reference",
    "make_chat_reference",
    "make_score_reference",
    "make_terminal_control_reference",
    "make_versioned_file_reference",
    "make_versioned_file_content_reference",
]
