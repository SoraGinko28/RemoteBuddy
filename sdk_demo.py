#!/usr/bin/env python3
"""Run the sdk end to end, and read its answers with viba.reflect.

    python3 sdk_demo.py

All in this one process, no fixtures from the test suite:

1. write a small meeting journal under a temporary runs/ directory
   (meeting.json + streams/events.jsonl, the files meeting_loader reads);
2. start the harness web server on a free local port over that journal;
3. point meeting_sdk at it and drive the apis of the data-interface
   chapter -- every answer is a viba script (``entry = ...``);
4. read those scripts with viba.reflect -- the landing's sugar:
   ``get_ok()`` / ``get_microsecond()`` / ``node["agent"]`` / ``has_err()`` /
   ``node.value`` -- and check what came out.

Step 4 is the point: this program never calls meeting_core_reflect. A remote
consumer only ever sees viba code, so the rule layer is all it needs.

Exit code 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
from pathlib import Path

import meeting_sdk as remote
import meeting_sdk_viba as viba_sdk
from viba.reflect import (VibaData, access as viba_access, at_index as viba_at_index,
                         at_key as viba_at_key, by_field_index as viba_by_field_index,
                         by_tag as viba_by_tag)
from viba.type import VibaProgramErr, Ok
from meeting import MeetingStore
from web import MeetingWebService, RemoteBuddyHTTPServer

MEETING_ID = "20260919-120000-remote"
PORT = 0                      # let the os pick a free port
CHAT_ASKED = "please add a note"
CHAT_DONE = "done"
SCREEN_TEXT = "hello from the pane"


# ----------------------------------------------------------------------
# 1. a meeting journal on disk
# ----------------------------------------------------------------------


def write_journal(root: Path) -> dt.datetime:
    """A real meeting: MeetingStore writes it, the server reads it back.

    The store is the harness's own writer (a manifest plus a journal under
    runs/<id>), so nothing here is a fixture the loader does not see in
    production.
    """
    skill = root / "SKILL.md"
    skill.write_text("---\nname: workspace-collaborator\n---\n", encoding="utf-8")
    store = MeetingStore.create(root, MEETING_ID, CHAT_ASKED, skill, "remote-demo",
                                "test-model", agent_name2role={"agent": "actor"},
                                workspace=root)
    store.append_chat("agent", CHAT_DONE, "final")
    time.sleep(0.01)      # the journal stamps milliseconds: keep the two apart
    screen = store.append_screen(SCREEN_TEXT, "bash", str(store.workspace_path),
                                 metadata={"pane_width": 80, "pane_height": 1,
                                           "cursor_x": 0, "cursor_y": 0})
    # the demo wrote that event, so it knows the exact moment the capture is at
    return dt.datetime.fromisoformat(str(screen["captured_at"]).replace("Z", "+00:00"))


# ----------------------------------------------------------------------
# 2. reading a script with the rule layer's pythonic landing
# ----------------------------------------------------------------------


def leaf_value(node) -> object:
    """The python value under a node (the landing puts it on ``node.value``)."""
    return node.value


def answer(api: str, *args):
    """Call one api over the wire; give back (the script, the entry's node)."""
    given = getattr(remote, api)(MEETING_ID, *args)
    if isinstance(given, VibaProgramErr):
        raise SystemExit(f"{api}: {given.msg}")
    entry = viba_sdk.is_a_valid_answer(api, given.ok_value).ok_value
    _access, definition = viba_sdk.access_of(api).ok_value
    node = viba_access.root(definition, VibaData(entry)).ok_value
    return given.ok_value, node


# ----------------------------------------------------------------------
# 3. the checks
# ----------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.gaps = 0

    def that(self, label: str, ok: bool, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        self.failed += 0 if ok else 1
        print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""))

    def note(self, label: str, detail: str) -> None:
        self.gaps += 1
        print(f"  [gap ] {label}: {detail}")

    def done(self) -> int:
        print()
        if self.failed:
            print(f"{self.failed} check(s) failed" +
                  (f", {self.gaps} known gap(s)" if self.gaps else ""))
            return 1
        print(f"every check passed" + (f", {self.gaps} known gap(s) above" if self.gaps else ""))
        return 0


def run_checks(report: Report, capture_time: dt.datetime) -> None:
    print("remote sdk, read back with viba.reflect")
    print(f"  meeting: {MEETING_ID} at {remote.server_url()}")

    now = dt.datetime.now(dt.timezone.utc)
    script, node = answer("chunk_times", now - dt.timedelta(minutes=5),
                          now + dt.timedelta(minutes=5))
    report.that("chunk_times", len(node.get_ok()) == 2,
                f"{len(node.get_ok())} moments in the range")

    _script, node = answer("participants")
    names = [leaf_value(name.get_agent_name()) for name in node.get_ok()]
    report.that("participants", names == ["$human", "agent"], f"{names}")

    _script, first = answer("first_chunk_time")
    _script, last = answer("last_chunk_time")
    first_us = leaf_value(first.get_ok().get_microsecond())
    last_us = leaf_value(last.get_ok().get_microsecond())
    report.that("first_chunk_time", first_us is not None, f"microsecond={first_us}")
    report.that("last_chunk_time", last_us > first_us, f"microsecond={last_us} > {first_us}")

    _script, node = answer("now")
    report.that("now (the server's clock)", node.has_ok() is True,
                f"microsecond={leaf_value(node.get_ok().get_microsecond())}")

    _script, node = answer("roles")
    role = node.get_ok()["agent"]
    report.that("roles", role.has_actor() is True, "agent -> actor")

    script, node = answer("file_id_by_path", "/ws/a.py")
    report.that("file_id_by_path (no file layer in a journal)", node.has_err() is True,
                f"$err = {leaf_value(node.get_err())!r}")

    _script, node = answer("screen_at", capture_time)
    content = node.get_ok()
    segments = content.get_segments()
    report.that("screen_at",
                leaf_value(content.get_text()) == SCREEN_TEXT
                and len(segments) == 1
                and leaf_value(segments[0].get_text()) == SCREEN_TEXT
                and len(segments[0].get_styles()) == 0,
                f"{leaf_value(content.get_text())!r}, {len(segments)} segment(s), "
                f"{len(segments[0].get_styles())} styles")

    given = remote.chat_messages(MEETING_ID, now - dt.timedelta(minutes=5),
                                 now + dt.timedelta(minutes=5))
    report.note("chat_messages", given.msg if isinstance(given, VibaProgramErr) else "served")
    print()
    print("one answer, as the server sent it:")
    for line in script.rstrip("\n").splitlines():
        print("    " + line)


def main() -> int:
    report = Report()
    with tempfile.TemporaryDirectory(prefix="remote-sdk-demo-") as temporary:
        root = Path(temporary)
        capture_time = write_journal(root)
        service = MeetingWebService(project_root=root, workspace=root)
        server = RemoteBuddyHTTPServer(("127.0.0.1", PORT), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before = remote.server_url()
        remote.configure(f"http://127.0.0.1:{server.server_address[1]}")
        try:
            run_checks(report, capture_time)
        finally:
            remote.configure(before)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return report.done()


if __name__ == "__main__":
    raise SystemExit(main())
