"""Tests for meeting_remote_sdk: the same contract, over the wire.

The two bindings are driven with the same arguments and must answer the same
thing: meeting_local_sdk evaluates in this process, meeting_remote_sdk posts to
a real RemoteBuddyHTTPServer whose record is the same one (the fixture the local
tests use, handed over `sdk_meeting_data` instead of a journal).

`now` is left out of the comparison (two clocks), and the two writers are
checked separately: the server refuses them.
"""

import datetime as _dt
import tempfile
import threading
from pathlib import Path

import pytest

import meeting_local_sdk as local
import meeting_remote_sdk as remote
from tests.test_meeting_local_sdk import prepare_meeting_data_as_ctx
from web_v2 import MeetingWebService, RemoteBuddyHTTPServer


class _SdkService(MeetingWebService):
    """A service whose record is handed to it, no journal involved."""

    def __init__(self, data) -> None:
        super().__init__(project_root=Path(tempfile.gettempdir()))
        self.data = data

    def sdk_meeting_data(self, meeting_id: str):
        return self.data


@pytest.fixture(name="wired")
def _wired():
    """A running server, a fixture record, and both bindings pointed at it."""
    data = prepare_meeting_data_as_ctx()
    service = _SdkService(data)
    server = RemoteBuddyHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    before = remote.server_url()
    remote.configure(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield data
    finally:
        remote.configure(before)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _chunk_moments(data):
    """The moments the fixture's chunks sit at, from the local sdk."""
    return [local.first_chunk_time(data).value, local.last_chunk_time(data).value]


def _answers():
    """(api, args): every read api with arguments the fixture can answer."""
    data = prepare_meeting_data_as_ctx()
    first, last = _chunk_moments(data)
    file_id = local.file_id_by_path(data, "/ws/a.py").value
    file_ref = local.make_versioned_file_reference(file_id, first)
    content_ref = local.make_versioned_file_content_reference(file_ref,
                                                              local.make_text_region(0, 5))
    chat_ref = local.make_chat_reference(last, "bob", local.make_text_region(0, 2))
    score_ref = local.make_score_reference(last, "bob")
    screen_ref = local.make_screen_reference(first, "screen", local.make_text_region(0, 2))
    control_ref = local.make_terminal_control_reference(first, "alice",
                                                        local.make_text_region(0, 3))
    return [
        ("chunk_times", (data, first, last)),
        ("streams_between", (data, first, last)),
        ("chat_messages", (data, first, last)),
        ("score_messages", (data, first, last)),
        ("chat_message_at", (data, last, "bob")),
        ("resolve_chat_reference", (data, chat_ref)),
        ("resolve_chat_content_reference", (data, chat_ref)),
        ("resolve_score_reference", (data, score_ref)),
        ("chat_message_references", (data, first, "alice")),
        ("score_message_reference", (data, last, "bob")),
        ("first_chunk_time", (data,)),
        ("last_chunk_time", (data,)),
        ("screen_at", (data, first)),
        ("resolve_screen_reference", (data, screen_ref)),
        ("participants", (data,)),
        ("roles", (data,)),
        ("owner_at", (data, first)),
        ("control_owner_of", (data, control_ref)),
        ("file_id_by_path", (data, "/ws/a.py")),
        ("file_datetimes", (data, file_id)),
        ("files_at", (data, last)),
        ("latest_file", (data, file_id)),
        ("resolve_versioned_file_reference", (data, file_ref)),
        ("resolve_versioned_file_content_reference", (data, content_ref)),
        ("read_text", (data, file_id)),
    ]


def _payload(given):
    """Ok -> its value, Err -> the sentence; so the two sides can be compared."""
    if isinstance(given, local._Ok):
        return ("ok", given.value)
    return ("err", given.message)


@pytest.mark.parametrize("api,args", _answers(), ids=lambda item: None)
def test_both_bindings_answer_the_same(api, args, wired) -> None:
    data, rest = args[0], args[1:]
    ours = getattr(local, api)(data, *rest)
    theirs = getattr(remote, api)(data.meeting_uid, *rest)
    assert _payload(ours) == _payload(theirs)


def test_errors_travel_as_errors(wired) -> None:
    data = wired
    moment = local.last_chunk_time(data).value
    assert _payload(remote.chat_message_at(data.meeting_uid, moment, "alice"))[0] == "err"
    assert _payload(remote.file_id_by_path(data.meeting_uid, "/ws/nope.py"))[0] == "err"


def test_the_server_refuses_the_writers(wired) -> None:
    data = wired
    moment = local.now(data).value
    for api, args in (("write_text", ("f1", "more", moment, "alice")),
                      ("open_file", ("/ws/c.py", moment, "alice"))):
        given = getattr(remote, api)(data.meeting_uid, *args)
        assert isinstance(given, local._Err)
        assert "read-only over the wire" in given.message


def test_an_unknown_api_is_a_404(wired) -> None:
    given = remote._call("no_such_api", wired.meeting_uid)
    assert isinstance(given, local._Err)
    assert "404" in given.message or "no such meeting sdk api" in given.message


def test_now_comes_from_the_server(wired) -> None:
    data = wired
    moment = remote.now(data.meeting_uid)
    assert isinstance(moment, local._Ok)
    assert isinstance(moment.value, _dt.datetime)
    assert abs(moment.value - local.now(data).value) < _dt.timedelta(minutes=1)


def test_the_make_group_is_local_and_identical() -> None:
    """Building a value is not data access: the remote binding aliases them."""
    for name in remote.__all__:
        if name.startswith("make_"):
            assert getattr(remote, name) is getattr(local, name)


def test_the_contract_is_the_same_35() -> None:
    assert remote.__all__ == local.__all__
    assert len(remote.__all__) == 35
