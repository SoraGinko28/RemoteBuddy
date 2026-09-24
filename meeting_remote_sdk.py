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
construction.

The binding is a ``Client``: one kept connection, one token, one timeout, and
no module-level mutable state. ``configure(url)`` only swaps the default client
the module-level functions use, so the chapter's hint still reads

    import meeting_remote_sdk as sdk
    sdk.chat_messages(meeting_uid, start, end)

Nothing writes: the data interface is read-only. The make_* group builds values
and never goes over the wire.
"""

from __future__ import annotations

import datetime as _dt
import http.client
import json
import urllib.parse
from typing import Any, Optional

import meeting_local_sdk as local
import meeting_sdk_viba
from meeting_sdk_json import decode, encode
from viba.type import VibaProgramErr as _Err, Ok as _Ok, Result as _Result

CSRF_HEADER = "X-RemoteBuddy-CSRF"          # what the server expects on a POST
DEFAULT_BASE_URL = "http://127.0.0.1:8765"  # the local harness web server
DEFAULT_TIMEOUT = 15.0

Ctx = str          # this binding: Ctx is a meeting uid


class Client:
    """One server: a kept connection, a token, a timeout."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"base_url must be an http(s) url: {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.timeout = timeout
        self.token = token
        self.calls = 0
        self._connection: Optional[http.client.HTTPConnection] = None

    # ---- the connection ----

    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            self._connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _send(self, method: str, path: str, body: Optional[bytes] = None,
              headers: Optional[dict] = None) -> tuple:
        """One request over the kept connection; a broken one is replaced once."""
        for attempt in (0, 1):
            connection = self._connect()
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw else {})
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt:
                    raise
        raise AssertionError("unreachable")

    # ---- the token ----

    def _csrf_token(self, refresh: bool = False) -> str:
        """The server's csrf token: fetched from /api/session and kept."""
        if self.token is not None and not refresh:
            return self.token
        status, payload = self._send("GET", "/api/session")
        if status >= 400:
            raise RuntimeError(f"cannot read the session: {status}")
        self.token = str(payload.get("csrf_token") or "")
        return self.token

    # ---- the wire ----

    def call(self, api: str, ctx: Ctx, **args: Any) -> _Result:
        """One api, one POST to its own url."""
        path = f"/api/meetings/{urllib.parse.quote(str(ctx))}/sdk/{api}"
        body = json.dumps({"args": encode(args)}).encode("utf-8")
        for attempt in (0, 1):
            headers = {"Content-Type": "application/json",
                       CSRF_HEADER: self._csrf_token(refresh=attempt > 0)}
            try:
                status, payload = self._send("POST", path, body, headers)
            except Exception as error:              # noqa: BLE001 - 报给调用者，不抛
                return _Err(f"cannot reach the meeting server: {error}")
            if status == 403 and attempt == 0:      # a stale token: ask again
                continue
            if status >= 400:
                answer = payload.get("error") or payload.get("message") or "no message"
                return _Err(f"{status}: {answer}")
            break
        self.calls += 1
        script = payload.get("script")
        if not isinstance(script, str):
            return _Err("the meeting server did not answer with a viba script")
        checked = meeting_sdk_viba.is_a_valid_answer(api, script)
        if isinstance(checked, _Err):
            return _Err(checked.err_msg)
        return _Ok(script)

    # ------------------------------------------------------------------
    # the apis of the contract
    # ------------------------------------------------------------------

    def now(self, ctx: Ctx) -> _Result:
        """The moment this process is at; never hands out a microsecond twice.
        """
        return self.call('now', ctx)

    def chunk_times(self, ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
        """The moment of every chunk in this span.
        """
        return self.call('chunk_times', ctx, start=start, end=end)

    def streams_between(self, ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
        """The stream names seen in this span.
        """
        return self.call('streams_between', ctx, start=start, end=end)

    def chat_messages(self, ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
        return self.call('chat_messages', ctx, start=start, end=end)

    def score_messages(self, ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
        return self.call('score_messages', ctx, start=start, end=end)

    def chat_message_at(self, ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
        """One person has one message at a moment.
        """
        return self.call('chat_message_at', ctx, time=time, user=user)

    def resolve_chat_reference(self, ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
        return self.call('resolve_chat_reference', ctx, ref=ref)

    def resolve_chat_content_reference(self, ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
        """The text the reference points at.
        """
        return self.call('resolve_chat_content_reference', ctx, ref=ref)

    def resolve_score_reference(self, ctx: Ctx, ref: 'adt.ScoreReference') -> _Result:
        return self.call('resolve_score_reference', ctx, ref=ref)

    def chat_message_references(self, ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
        """The references this message carries, in order.
        """
        return self.call('chat_message_references', ctx, time=time, user=user)

    def score_message_reference(self, ctx: Ctx, time: _dt.datetime, judge: str) -> _Result:
        """The reference this score carries.
        """
        return self.call('score_message_reference', ctx, time=time, judge=judge)

    def first_chunk_time(self, ctx: Ctx) -> _Result:
        """The moment of the meeting's first chunk.
        """
        return self.call('first_chunk_time', ctx)

    def last_chunk_time(self, ctx: Ctx) -> _Result:
        """The last one.
        """
        return self.call('last_chunk_time', ctx)

    def screen_times(self, ctx: Ctx) -> _Result:
        """The moments the screen stream captured, ascending.
        """
        return self.call('screen_times', ctx)

    def screen_at(self, ctx: Ctx, time: _dt.datetime) -> _Result:
        """The content of that screen at this moment (not the capture record).
        """
        return self.call('screen_at', ctx, time=time)

    def resolve_screen_reference(self, ctx: Ctx, ref: 'adt.ScreenReference') -> _Result:
        return self.call('resolve_screen_reference', ctx, ref=ref)

    def participants(self, ctx: Ctx) -> _Result:
        return self.call('participants', ctx)

    def roles(self, ctx: Ctx) -> _Result:
        return self.call('roles', ctx)

    def owner_at(self, ctx: Ctx, time: _dt.datetime) -> _Result:
        """Who holds the terminal at a moment; nil = nobody holds it then.
        """
        return self.call('owner_at', ctx, time=time)

    def control_owner_of(self, ctx: Ctx, ref: 'adt.TerminalControlReference') -> _Result:
        """The taker of that take-over / hand-back; nil = nobody took over then.
        """
        return self.call('control_owner_of', ctx, ref=ref)

    def file_id_by_path(self, ctx: Ctx, path: str) -> _Result:
        """/workspace_name/entry_name or /workspace_name/fold_name/entry_name.
        """
        return self.call('file_id_by_path', ctx, path=path)

    def file_datetimes(self, ctx: Ctx, file: 'adt.FileId') -> _Result:
        """The moments this file changed: the union of all version times on the
        """
        return self.call('file_datetimes', ctx, file=file)

    def files_at(self, ctx: Ctx, time: _dt.datetime) -> _Result:
        """What each file is at a moment (a file whose version does not exist yet at
        """
        return self.call('files_at', ctx, time=time)

    def latest_file(self, ctx: Ctx, file: 'adt.FileId') -> _Result:
        """The latest state.
        """
        return self.call('latest_file', ctx, file=file)

    def resolve_versioned_file_reference(self, ctx: Ctx, ref: 'adt.VersionedFileReference') -> _Result:
        """The file state at the moment cited.
        """
        return self.call('resolve_versioned_file_reference', ctx, ref=ref)

    def resolve_versioned_file_content_reference(self, ctx: Ctx, ref: 'adt.VersionedFileContentReference') -> _Result:
        """The text the reference points at.
        """
        return self.call('resolve_versioned_file_content_reference', ctx, ref=ref)

    def read_text(self, ctx: Ctx, file: 'adt.FileId') -> _Result:
        """read_text = latest_file (the design's spelling is an alias).
        """
        return self.call('read_text', ctx, file=file)


# ----------------------------------------------------------------------
# the default client, and the module-level binding the chapter's hint uses
# ----------------------------------------------------------------------

_DEFAULT = Client()


def configure(base_url: str = DEFAULT_BASE_URL, token: Optional[str] = None,
              timeout: float = DEFAULT_TIMEOUT) -> None:
    """Point the module-level functions at a server; call it before any api."""
    global _DEFAULT
    _DEFAULT.close()
    _DEFAULT = Client(base_url, token, timeout)


def server_url() -> str:
    """Where the module-level functions are pointed right now."""
    return _DEFAULT.base_url


def client() -> Client:
    """The default client itself, when its connection or counters matter."""
    return _DEFAULT


def now(ctx: Ctx) -> _Result:
    return _DEFAULT.now(ctx)

def chunk_times(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _DEFAULT.chunk_times(ctx, start, end)

def streams_between(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _DEFAULT.streams_between(ctx, start, end)

def chat_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _DEFAULT.chat_messages(ctx, start, end)

def score_messages(ctx: Ctx, start: _dt.datetime, end: _dt.datetime) -> _Result:
    return _DEFAULT.score_messages(ctx, start, end)

def chat_message_at(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    return _DEFAULT.chat_message_at(ctx, time, user)

def resolve_chat_reference(ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
    return _DEFAULT.resolve_chat_reference(ctx, ref)

def resolve_chat_content_reference(ctx: Ctx, ref: 'adt.ChatReference') -> _Result:
    return _DEFAULT.resolve_chat_content_reference(ctx, ref)

def resolve_score_reference(ctx: Ctx, ref: 'adt.ScoreReference') -> _Result:
    return _DEFAULT.resolve_score_reference(ctx, ref)

def chat_message_references(ctx: Ctx, time: _dt.datetime, user: str) -> _Result:
    return _DEFAULT.chat_message_references(ctx, time, user)

def score_message_reference(ctx: Ctx, time: _dt.datetime, judge: str) -> _Result:
    return _DEFAULT.score_message_reference(ctx, time, judge)

def first_chunk_time(ctx: Ctx) -> _Result:
    return _DEFAULT.first_chunk_time(ctx)

def last_chunk_time(ctx: Ctx) -> _Result:
    return _DEFAULT.last_chunk_time(ctx)

def screen_times(ctx: Ctx) -> _Result:
    return _DEFAULT.screen_times(ctx)

def screen_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    return _DEFAULT.screen_at(ctx, time)

def resolve_screen_reference(ctx: Ctx, ref: 'adt.ScreenReference') -> _Result:
    return _DEFAULT.resolve_screen_reference(ctx, ref)

def participants(ctx: Ctx) -> _Result:
    return _DEFAULT.participants(ctx)

def roles(ctx: Ctx) -> _Result:
    return _DEFAULT.roles(ctx)

def owner_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    return _DEFAULT.owner_at(ctx, time)

def control_owner_of(ctx: Ctx, ref: 'adt.TerminalControlReference') -> _Result:
    return _DEFAULT.control_owner_of(ctx, ref)

def file_id_by_path(ctx: Ctx, path: str) -> _Result:
    return _DEFAULT.file_id_by_path(ctx, path)

def file_datetimes(ctx: Ctx, file: 'adt.FileId') -> _Result:
    return _DEFAULT.file_datetimes(ctx, file)

def files_at(ctx: Ctx, time: _dt.datetime) -> _Result:
    return _DEFAULT.files_at(ctx, time)

def latest_file(ctx: Ctx, file: 'adt.FileId') -> _Result:
    return _DEFAULT.latest_file(ctx, file)

def resolve_versioned_file_reference(ctx: Ctx, ref: 'adt.VersionedFileReference') -> _Result:
    return _DEFAULT.resolve_versioned_file_reference(ctx, ref)

def resolve_versioned_file_content_reference(ctx: Ctx, ref: 'adt.VersionedFileContentReference') -> _Result:
    return _DEFAULT.resolve_versioned_file_content_reference(ctx, ref)

def read_text(ctx: Ctx, file: 'adt.FileId') -> _Result:
    return _DEFAULT.read_text(ctx, file)


make_text_region = local.make_text_region


make_screen_reference = local.make_screen_reference


make_chat_reference = local.make_chat_reference


make_score_reference = local.make_score_reference


make_terminal_control_reference = local.make_terminal_control_reference


make_versioned_file_reference = local.make_versioned_file_reference


make_versioned_file_content_reference = local.make_versioned_file_content_reference

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
