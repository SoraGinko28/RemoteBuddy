#!/usr/bin/env python3
"""Run the remote sdk end to end, and read its answers with viba.rule.reflect.

    python3 remote_sdk_demo.py

All in this one process, no fixtures from the test suite:

1. write a small meeting journal under a temporary runs/ directory
   (meeting.json + streams/events.jsonl, the shape meeting_loader reads);
2. start the harness web server on a free local port over that journal;
3. point meeting_remote_sdk at it and drive the apis of the data-interface
   chapter -- every answer is a viba script (``entry := ...``);
4. read those scripts with viba.rule.reflect -- the landing's sugar:
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
from pathlib import Path

import meeting_remote_sdk as remote
import meeting_sdk_viba as viba_sdk
from viba.rule import reflect as rule_reflect
from viba.type import Err, Ok
from meeting_v2 import MeetingStore
from web_v2 import MeetingWebService, RemoteBuddyHTTPServer

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
                                "test-model", agent_name2role={"agent": "generator"})
    store.append_chat("agent", CHAT_DONE, "final")
    screen = store.append_screen(SCREEN_TEXT, "bash", str(store.workspace_path),
                                 metadata={"pane_width": 80, "pane_height": 1,
                                           "cursor_x": 0, "cursor_y": 0})
    # the demo wrote that event, so it knows the exact moment the capture is at
    return dt.datetime.fromisoformat(str(screen["captured_at"]).replace("Z", "+00:00"))


# ----------------------------------------------------------------------
# 2. reading a script with the rule layer's pythonic landing
# ----------------------------------------------------------------------


def answer(api: str, *args):
    """Call one api over the wire; give back (the script, the entry's node)."""
    given = getattr(remote, api)(MEETING_ID, *args)
    if isinstance(given, Err):
        raise SystemExit(f"{api}: {given.message}")
    entry = viba_sdk.is_a_valid_answer(api, given.value).value
    _access, definition = viba_sdk.access_of(api).value
    node = rule_reflect.access(definition).root(rule_reflect.Witness(entry)).value
    return given.value, node


def scalar(node) -> object:
    """The python value under a node (``node.value`` is the leaf)."""
    return node.value.value


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
    print("remote sdk, read back with viba.rule.reflect")
    print(f"  meeting: {MEETING_ID} at {remote.server_url()}")

    now = dt.datetime.now(dt.timezone.utc)
    script, node = answer("chunk_times", now - dt.timedelta(minutes=5),
                          now + dt.timedelta(minutes=5))
    report.that("chunk_times", len(node.get_ok()) == 2,
                f"{len(node.get_ok())} moments in the range")

    _script, node = answer("participants")
    names = [scalar(name.get_agent_name()) for name in node.get_ok()]
    report.that("participants", names == ["$human", "agent"], f"{names}")

    _script, first = answer("first_chunk_time")
    _script, last = answer("last_chunk_time")
    first_us = scalar(first.get_ok().get_microsecond())
    last_us = scalar(last.get_ok().get_microsecond())
    report.that("first_chunk_time", first_us is not None, f"microsecond={first_us}")
    report.that("last_chunk_time", last_us > first_us, f"microsecond={last_us} > {first_us}")

    _script, node = answer("now")
    report.that("now (the server's clock)", node.has_ok() is True,
                f"microsecond={scalar(node.get_ok().get_microsecond())}")

    _script, node = answer("roles")
    role = node.get_ok()["agent"]
    report.that("roles", role.has_generator() is True, "agent -> generator")

    script, node = answer("file_id_by_path", "/ws/a.py")
    report.that("file_id_by_path (no file layer in a journal)", node.has_err() is True,
                f"$err = {scalar(node.get_err())!r}")

    for api, args in (("chat_messages", (now - dt.timedelta(minutes=5),
                                         now + dt.timedelta(minutes=5))),
                      ("screen_at", (capture_time,))):
        given = getattr(remote, api)(MEETING_ID, *args)
        report.note(api, given.message if isinstance(given, Err) else "served")
    print()
    print("one answer, as the server sent it:")
    for line in script.rstrip("\n").splitlines():
        print("    " + line)


def main() -> int:
    report = Report()
    with tempfile.TemporaryDirectory(prefix="remote-sdk-demo-") as temporary:
        root = Path(temporary)
        capture_time = write_journal(root)
        service = MeetingWebService(project_root=root)
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
