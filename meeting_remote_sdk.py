"""meeting_remote_sdk -- the remote binding of the meeting_sdk.viba contract.

Ctx is a meeting uid. Every api is backed by its own url on a RemoteBuddy web
server:

    POST {base_url}/api/meetings/{meeting_uid}/sdk/{api}
    body  {"args": {...}}   ->  {"script": "entry = ..."}

The answer is a viba script whose entry is the api's own result: a failure is the
$err branch of that result, so the script is always a resident of the api's
return type in the data-interface chapter. This binding checks that before it
hands the script back (``Ok(script)``); read it with viba.reflect, the way a
local answer is read with meeting_core_reflect. Arguments travel through
meeting_sdk_json. The server answers by running the same api of
meeting_local_sdk over the record it holds, so the two bindings agree by
construction. Call configure() once (the default is the local web server); the
csrf token is fetched from /api/session on the first call.

Nothing writes: the data interface is read-only. The make_* group builds values
and never goes over the wire.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import meeting_local_sdk as local
import meeting_sdk_viba
from meeting_sdk_json import decode, encode
from viba.type import VibaProgramErr as _Err, Ok as _Ok, Result as _Result

CSRF_HEADER = "X-RemoteBuddy-CSRF"          # what the server expects on a POST
DEFAULT_BASE_URL = "http://127.0.0.1:8765"  # the local harness web server
DEFAULT_TIMEOUT = 15.0

Ctx = str          # this binding: Ctx is a meeting uid

_BASE_URL = DEFAULT_BASE_URL
_TOKEN: Optional[str] = None
_TIMEOUT = DEFAULT_TIMEOUT


def configure(base_url: str = DEFAULT_BASE_URL, token: Optional[str] = None,
              timeout: float = DEFAULT_TIMEOUT) -> None:
    """Point the binding at a server; call it once, before any api."""
    global _BASE_URL, _TOKEN, _TIMEOUT
    _BASE_URL = base_url.rstrip("/")
    _TOKEN = token
    _TIMEOUT = timeout


def server_url() -> str:
    """Where the binding is pointed right now."""
    return _BASE_URL


# ----------------------------------------------------------------------
# the wire
# ----------------------------------------------------------------------


def _url(meeting_uid: str, api: str) -> str:
    return f"{_BASE_URL}/api/meetings/{urllib.parse.quote(str(meeting_uid))}/sdk/{api}"


def _csrf_token(refresh: bool = False) -> str:
    """The server's csrf token: fetched from /api/session and cached."""
    global _TOKEN
    if _TOKEN is not None and not refresh:
        return _TOKEN
    request = urllib.request.Request(f"{_BASE_URL}/api/session")
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    _TOKEN = str(payload.get("csrf_token") or "")
    return _TOKEN


def _call(api: str, meeting_uid: str, **args) -> _Result:
    """One api, one POST to its own url."""
    body = json.dumps({"args": encode(args)}).encode("utf-8")
    for attempt in (0, 1):
        request = urllib.request.Request(
            _url(meeting_uid, api), data=body, method="POST",
            headers={"Content-Type": "application/json", CSRF_HEADER: _csrf_token()})
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code == 403 and attempt == 0:      # a stale token: ask again
                _csrf_token(refresh=True)
                continue
            return _Err(_error_message(error))
        except OSError as error:
            return _Err(f"cannot reach the meeting server: {error}")
    else:
        return _Err("the meeting server refused the call")
    script = payload.get("script")
    if not isinstance(script, str):
        return _Err("the meeting server did not answer with a viba script")
    checked = meeting_sdk_viba.is_a_valid_answer(api, script)
    if isinstance(checked, _Err):
        return _Err(checked.err_msg)
    return _Ok(script)


def _error_message(error: urllib.error.HTTPError) -> str:
    detail = error.read().decode("utf-8", "replace")
    try:
        payload = json.loads(detail)
    except ValueError:
        return f"{error.code}: {detail.strip()}"
    answer = payload.get("error") or payload.get("message") or "no message"
    return f"{error.code}: {answer}"


# ----------------------------------------------------------------------
# the 33 apis of the contract
# ----------------------------------------------------------------------


def now(ctx: Ctx) -> _Result:
    """The moment this process is at; never hands out a microsecond twice."""
    return _call("now", ctx)


def chunk_times(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """The moment of every chunk in this span."""
    return _call("chunk_times", ctx, start=start, end=end)


def streams_between(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    """The stream names seen in this span."""
    return _call("streams_between", ctx, start=start, end=end)


def chat_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _call("chat_messages", ctx, start=start, end=end)


def score_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _call("score_messages", ctx, start=start, end=end)


def chat_message_at(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """One person has one message at a moment."""
    return _call("chat_message_at", ctx, time=time, user=user)


def resolve_chat_reference(ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
    return _call("resolve_chat_reference", ctx, ref=ref)


def resolve_chat_content_reference(ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
    """The text the reference points at."""
    return _call("resolve_chat_content_reference", ctx, ref=ref)


def resolve_score_reference(ctx: Ctx, ref: 'adt.ScoreReference') -> _Result:
    return _call("resolve_score_reference", ctx, ref=ref)


def chat_message_references(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    """The references this message carries, in order."""
    return _call("chat_message_references", ctx, time=time, user=user)


def score_message_reference(ctx: Ctx, time: _dt.datetime, judge: str) -> _Result:
    """The reference this score carries."""
    return _call("score_message_reference", ctx, time=time, judge=judge)


def first_chunk_time(ctx: Ctx) -> _Result:
    """The moment of the meeting's first chunk."""
    return _call("first_chunk_time", ctx)


def last_chunk_time(ctx: Ctx) -> _Result:
    """The last one."""
    return _call("last_chunk_time", ctx)


def screen_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """The content of that screen at this moment (not the capture record)."""
    return _call("screen_at", ctx, time=time)


def resolve_screen_reference(ctx: Ctx, ref: 'adt.ScreenReference') -> _Result:
    return _call("resolve_screen_reference", ctx, ref=ref)


def participants(ctx: Ctx) -> _Result:
    return _call("participants", ctx)


def roles(ctx: Ctx) -> _Result:
    return _call("roles", ctx)


def owner_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """Who holds the terminal at a moment; nil = nobody holds it then."""
    return _call("owner_at", ctx, time=time)


def control_owner_of(ctx: Ctx, ref: 'adt.TerminalControlReference') -> _Result:
    """The taker of that take-over / hand-back; nil = nobody took over then."""
    return _call("control_owner_of", ctx, ref=ref)


def file_id_by_path(ctx: Ctx, path: str) -> _Result:
    """/workspace_name/entry_name or /workspace_name/fold_name/entry_name."""
    return _call("file_id_by_path", ctx, path=path)


def file_datetimes(ctx: Ctx, file: 'adt.FileId') -> _Result:
    """The moments this file changed: the union of all version times on the"""
    return _call("file_datetimes", ctx, file=file)


def files_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    """What each file is at a moment (a file whose version does not exist yet at"""
    return _call("files_at", ctx, time=time)


def latest_file(ctx: Ctx, file: 'adt.FileId') -> _Result:
    """The latest state."""
    return _call("latest_file", ctx, file=file)


def resolve_versioned_file_reference(ctx: Ctx, ref: 'adt.VersionedFileReference') -> _Result:
    """The file state at the moment cited."""
    return _call("resolve_versioned_file_reference", ctx, ref=ref)


def resolve_versioned_file_content_reference(ctx: Ctx, ref: 'adt.VersionedFileContentReference') -> _Result:
    """The text the reference points at."""
    return _call("resolve_versioned_file_content_reference", ctx, ref=ref)


def read_text(ctx: Ctx, file: 'adt.FileId') -> _Result:
    """read_text = latest_file (the design's spelling is an alias)."""
    return _call("read_text", ctx, file=file)


make_text_region = local.make_text_region       # a value, not data: built here


make_screen_reference = local.make_screen_reference       # a value, not data: built here


make_chat_reference = local.make_chat_reference       # a value, not data: built here


make_score_reference = local.make_score_reference       # a value, not data: built here


make_terminal_control_reference = local.make_terminal_control_reference       # a value, not data: built here


make_versioned_file_reference = local.make_versioned_file_reference       # a value, not data: built here


make_versioned_file_content_reference = local.make_versioned_file_content_reference       # a value, not data: built here

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
