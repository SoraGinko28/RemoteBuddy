"""Tests for meeting_sdk: the contract over the wire, answered in viba.

Every api answers with a viba script (``entry = ...``) that is a resident of the
api's return type in the data-interface chapter; the script is read with
viba.reflect and must read exactly what meeting_sdk_server answers here.

The server in these tests is a real RemoteBuddyHTTPServer whose record is the
fixture the local tests use, handed over `sdk_meeting_data` instead of a journal.
"""

import tempfile
import threading
from pathlib import Path

import pytest

import meeting_core_reflect as reflect
import meeting_sdk_server as server
import meeting_sdk as remote
import meeting_sdk_viba as viba_sdk
from tests.test_meeting_sdk_server import prepare_meeting_data_as_ctx
from viba.reflect import (VibaData, access as viba_access, at_index as viba_at_index,
                         at_key as viba_at_key, by_field_index as viba_by_field_index,
                         by_tag as viba_by_tag)
from viba.type import Err, VibaProgramErr, Ok
from web import MeetingWebService, RemoteBuddyHTTPServer


class _SdkService(MeetingWebService):
    """A service whose record is handed to it, no journal involved."""

    def __init__(self, data) -> None:
        super().__init__(project_root=Path(tempfile.gettempdir()))
        self.data = data

    def sdk_meeting_data(self, meeting_id: str):
        return self.data


@pytest.fixture(name="wired")
def _wired():
    """A running server, a fixture record, and the binding pointed at it."""
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


# ----------------------------------------------------------------------
# the same answers, read both ways
# ----------------------------------------------------------------------


def _step(step):
    """One step of a local path, as the rule layer spells it."""
    return {"by_tag": viba_by_tag, "by_field_index": viba_by_field_index,
            "at_index": viba_at_index, "at_key": viba_at_key}[step.kind](
        step.value)


def _leaf(given):
    """(kind, value): Err and Ok are told apart, the messages are not compared."""
    if isinstance(given, Err):
        return ("err", None)
    return ("ok", given.ok_value)


def _reads_the_same(api, local_result, script) -> int:
    """Walk the local answer; the script must read the same at every leaf."""
    access, definition = viba_sdk.access_of(api).ok_value
    local_root = access.root(definition, reflect._Record(local_result)).ok_value
    entry = viba_sdk.is_a_valid_answer(api, script).ok_value
    witness_root = viba_access.root(definition, VibaData(entry)).ok_value
    compared = 0
    for node in access._walk(local_root):
        ours = access.leaf(node)
        theirs = viba_access.get_by_path(witness_root,
                                               [_step(step) for step in node.path])
        assert _leaf(ours) == _leaf(theirs), (
            f"{api}: path {[step.value for step in node.path]} reads {ours!r} here and "
            f"{theirs!r} there")
        compared += _leaf(ours)[0] == "ok"
    return compared


def _api_calls():
    """(api, args): every read api with arguments the fixture can answer."""
    data = prepare_meeting_data_as_ctx()
    first = server.first_chunk_time(data).ok_value
    last = server.last_chunk_time(data).ok_value
    file_id = server.file_id_by_path(data, "/ws/a.py").ok_value
    file_ref = remote.make_versioned_file_reference(file_id, first)
    content_ref = remote.make_versioned_file_content_reference(file_ref,
                                                              remote.make_text_region(0, 5))
    chat_ref = remote.make_chat_reference(last, "bob", remote.make_text_region(0, 2))
    screen_ref = remote.make_screen_reference(first, "screen", remote.make_text_region(0, 2))
    control_ref = remote.make_terminal_control_reference(first, "alice",
                                                        remote.make_text_region(0, 3))
    return [
        ("chunk_times", (data, first, last)),
        ("streams_between", (data, first, last)),
        ("chat_messages", (data, first, last)),
        ("chat_message_at", (data, last, "bob")),
        ("resolve_chat_reference", (data, chat_ref)),
        ("resolve_chat_content_reference", (data, chat_ref)),
        ("chat_message_references", (data, first, "alice")),
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


# The apis whose answers walk down to leaves on both sides. The three chat ones
# cannot be serialized at all yet: the record layer wraps the two chat parts in
# classes of its own (RawMessage / AtUserMessage) and the binding does not land
# them on the design's branches ($raw_msg str / $at_user_name UserName).
WALKABLE = (
    "chunk_times", "streams_between",
    "resolve_chat_content_reference",
    "chat_message_references",
    "first_chunk_time", "last_chunk_time", "screen_at", "resolve_screen_reference",
    "participants", "roles", "owner_at", "control_owner_of", "file_id_by_path",
    "file_datetimes", "files_at", "latest_file",
    "resolve_versioned_file_reference", "resolve_versioned_file_content_reference",
    "read_text",
)
KNOWN_BINDING_GAP = ("chat_messages", "chat_message_at", "resolve_chat_reference")


def _calls(apis):
    return [item for item in _api_calls() if item[0] in apis]


@pytest.mark.parametrize("api,args", _calls({name for name, _ in _api_calls()}
                                            - set(KNOWN_BINDING_GAP)),
                         ids=lambda item: None)
def test_every_answer_is_the_script_for_the_local_answer(api, args, wired) -> None:
    """What the server sends is the script the same actor makes here."""
    data, rest = args[0], args[1:]
    ours = getattr(server, api)(data, *rest)
    given = getattr(remote, api)(data.meeting_uid, *rest)
    assert isinstance(given, Ok), given
    assert given.ok_value == viba_sdk.script_for(api, ours).ok_value


@pytest.mark.parametrize("api,args", _calls(set(WALKABLE)), ids=lambda item: None)
def test_the_walkable_answers_read_the_same(api, args, wired) -> None:
    data, rest = args[0], args[1:]
    ours = getattr(server, api)(data, *rest)
    given = getattr(remote, api)(data.meeting_uid, *rest)
    assert isinstance(given, Ok), given
    assert _reads_the_same(api, ours, given.ok_value) > 0


@pytest.mark.parametrize("api", KNOWN_BINDING_GAP)
def test_the_chat_apis_hit_the_known_binding_gap(api, wired) -> None:
    """The record layer's RawMessage does not land on $raw_msg str, so no
    script yet."""
    data = wired
    moment = server.last_chunk_time(data).ok_value
    calls = {"chat_messages": (data, moment, moment),
             "chat_message_at": (data, moment, "bob"),
             "resolve_chat_reference": (data, remote.make_chat_reference(
                 moment, "bob", remote.make_text_region(0, 2)))}
    given = getattr(remote, api)(data.meeting_uid, *calls[api][1:])
    assert isinstance(given, VibaProgramErr)
    assert "no branch of this sum carries a value" in given.msg


def test_a_failed_api_answers_its_err_branch(wired) -> None:
    data = wired
    moment = server.last_chunk_time(data).ok_value
    given = remote.chat_message_at(data.meeting_uid, moment, "alice")
    assert isinstance(given, Ok)
    entry = viba_sdk.is_a_valid_answer("chat_message_at", given.ok_value).ok_value
    access, definition = viba_sdk.access_of("chat_message_at").ok_value
    witness_root = viba_access.root(definition, VibaData(entry)).ok_value
    assert witness_root.get_err().value        # the sentence rides the $err branch


def test_a_missing_path_is_an_err_inside_the_script(wired) -> None:
    given = remote.file_id_by_path(wired.meeting_uid, "/ws/c.py")
    assert isinstance(given, Ok)
    assert "$err" in given.ok_value


def test_an_unknown_api_is_a_404(wired) -> None:
    given = remote.client().call("no_such_api", wired.meeting_uid)
    assert isinstance(given, VibaProgramErr)
    assert "404" in given.msg


def test_a_value_builder_is_not_served_over_the_wire(wired) -> None:
    """make_* builds a value; asking the server for one is a 400."""
    given = remote.client().call("make_text_region", wired.meeting_uid, start=0, end=2)
    assert isinstance(given, VibaProgramErr)
    assert "builds a value" in given.msg


def test_now_comes_from_the_server(wired) -> None:
    given = remote.now(wired.meeting_uid)
    assert isinstance(given, Ok)
    assert viba_sdk.is_a_valid_answer("now", given.ok_value).ok_value is not None


def test_the_make_group_is_local_and_identical() -> None:
    for name in remote.__all__:
        if name.startswith("make_"):
            assert getattr(remote, name) is getattr(server, name)


def test_the_contract_is_the_same_30() -> None:
    """screen_times arrived for the judge: it needs the screen stream's
    moments, not just the first one."""
    assert remote.__all__ == server.__all__
    assert len(remote.__all__) == 30


def test_every_data_api_has_a_return_type() -> None:
    types = viba_sdk.return_types()
    assert set(types) == {name for name in server.__all__ if not name.startswith("make_")}
    assert viba_sdk.return_type("read_text") == viba_sdk.return_type("latest_file")
