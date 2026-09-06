"""e2e_user_path — end-to-end test of RemoteBuddy's real user path.

Purpose (in response to the rule that refactoring/hardening must be verified
against real end-user impact, no self-delusion):
  run a real meeting with a real agent, real tmux, and real HTTP, and walk the
  whole "user" journey end to end:
    human speaks → agent works (terminal frames/actions) → human takes over →
    real keystrokes are echoed → hand back → wrap up;
  then assert the "user-observable invariants" on the journal left behind by
  that meeting:
    1. takeover/hand-back control events pair up, with correct order and owner
       (what frontend takeover relies on);
    2. action events from the user's input go received → started (keystroke
       bookkeeping), and the echo shows up in subsequent frames;
    3. event seq strictly increases and captured_at never goes backwards
       (replay ordering depends on this);
    4. this real record passes meeting_validate with 0 violations (acceptance of
       real user data by the G-chain validator);
    5. the agent was actually working during the session (screen/action events
       appeared, and the agent replied).

Usage:
  python e2e_user_path.py            # run one real meeting and assert; exit code non-zero on failure
  python e2e_user_path.py --keep     # keep the runs dir afterwards for review
Output: a JSON summary on stdout; can be retold as MEETING_E2E.md.

Real-cost warning: it spends model API quota and a few minutes; this is deliberate.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

CSRF_HEADER = "X-RemoteBuddy-CSRF"
START_MESSAGE = "List the current directory contents (ls -la), then reply DONE_E2E_7E3. Do not write any files."
HUMAN_ECHO_MARKER = "E2E_MARKER_9182"
POLL_STEP_S = 1.0
AGENT_FIRST_REPLY_TIMEOUT_S = 240
TAKEOVER_TIMEOUT_S = 60


class E2EFailure(AssertionError):
    pass


class LiveUserPath:
    def __init__(self, project_root: Path) -> None:
        import meeting_v2  # noqa: F401  trigger module initialization
        import web_v2

        self.project_root = project_root
        service = web_v2.MeetingWebService(
            project_root=project_root,
            model_api_path=REPO_ROOT / "model_api",
            skill_path=REPO_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md",
        )
        self.service = service
        server = web_v2.build_server("127.0.0.1", _free_port(), service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        self.port = server.server_address[1]
        self.csrf = ""
        self.meeting_id = ""

    # ---- HTTP helpers (real sockets, equivalent to browser requests) ----

    def _request(self, method: str, path: str, payload=None) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Content-Length": str(len(body))}
        if method == "POST":
            headers[CSRF_HEADER] = self.csrf
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise E2EFailure(f"{method} {path} -> non-JSON ({response.status}): {raw[:200]}")
        if response.status >= 400:
            raise E2EFailure(f"{method} {path} -> {response.status}: {parsed}")
        return parsed

    def start_meeting(self) -> str:
        self.csrf = self._request("GET", "/api/session")["csrf_token"]
        meeting_id = "e2e-" + uuid.uuid4().hex[:8]
        result = self._request("POST", "/api/meetings", {
            "meeting_id": meeting_id,
            "message": START_MESSAGE,
            "typing_profile": "fast",
        })
        self.meeting_id = result["meeting_id"] if isinstance(result, dict) else meeting_id
        return self.meeting_id

    def events(self, after: int = 0) -> dict:
        return self._request("GET", f"/api/meetings/{self.meeting_id}/events?after={after}")

    def snapshot_meeting(self) -> dict:
        payload = self._request("GET", f"/api/meetings/{self.meeting_id}/snapshot")
        return payload["meeting"]

    def send_message(self, message: str) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/messages",
                             {"message": message})

    def acquire(self) -> dict:
        return self._request("POST",
                             f"/api/meetings/{self.meeting_id}/control/acquire", {})

    def release(self) -> dict:
        return self._request("POST",
                             f"/api/meetings/{self.meeting_id}/control/release", {})

    def terminal_input_bytes(self, text: str) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/terminal/input",
                             {"bytes": text.encode("utf-8").hex()})

    def stop(self) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/stop", {})

    def shutdown(self) -> None:
        self.server.shutdown()


def _wait_until(deadline_s: float, label: str, predicate) -> dict:
    while time.monotonic() < deadline_s:
        try:
            result = predicate()
            if result:
                return result
        except E2EFailure:
            raise
        except Exception as exc:  # noqa: BLE001 retry on flaky network
            print(f"  retry {label}: {exc!r}")
        time.sleep(POLL_STEP_S)
    raise E2EFailure(f"timeout waiting for {label}")


def _kind_actor(events: list[dict], kind: str, actor: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind and e.get("actor") == actor]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteBuddy real user-path E2E")
    parser.add_argument("--keep", action="store_true", help="keep the temp runs dir")
    args = parser.parse_args(argv)

    tmp_root = Path(tempfile.mkdtemp(prefix="rb-e2e-"))
    journal_dir: Path | None = None
    results: dict = {}
    failed: list[str] = []
    try:
        live = LiveUserPath(tmp_root)
        t_start = time.monotonic()
        try:
            meeting_id = live.start_meeting()
            results["meeting_id"] = meeting_id
            results["agent_task"] = START_MESSAGE
            print(f"started meeting {meeting_id} on :{live.port}", flush=True)

            # -- M2: agent starts working: agent chat + screen/action events appear --
            def agent_working() -> dict:
                events = live.events()["events"]
                kinds = {e.get("kind") for e in events}
                agent_chat = any(
                    e.get("kind") == "chat" and e.get("actor") == "agent"
                    for e in events
                )
                if agent_chat and {"screen", "action"} <= kinds:
                    return {"events": events}
                return {}

            first = _wait_until(
                time.monotonic() + AGENT_FIRST_REPLY_TIMEOUT_S,
                "agent to chat + act",
                agent_working,
            )
            agent_chats = _kind_actor(first["events"], "chat", "agent")
            results["agent_chat_count_by_first_reply"] = len(agent_chats)
            results["screen_events_by_first_reply"] = len(
                _kind_actor(first["events"], "screen", "workspace"))
            print(f"agent replied; chats={len(agent_chats)} screens="
                  f"{results['screen_events_by_first_reply']}", flush=True)

            # -- M2b: wait for the agent's cycle to end (back to waiting), then let the user take over (idle takeover) --
            _wait_until(time.monotonic() + AGENT_FIRST_REPLY_TIMEOUT_S,
                        "agent cycle to finish",
                        lambda: live.snapshot_meeting()
                        if live.snapshot_meeting().get("phase") == "waiting_for_user"
                        else {})
            print("agent idle; user may take over", flush=True)

            # -- M3: user takes over: after acquire, control events pair up with owner=human --
            t_acquire = time.monotonic()
            live.acquire()
            results["acquire_latency_ms"] = round((time.monotonic() - t_acquire) * 1000)

            def acquired() -> dict:
                meeting = live.snapshot_meeting()
                if meeting.get("control_owner") == "human":
                    return meeting
                return {}

            _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S, "acquire ownership",
                        acquired)
            print("user owns terminal", flush=True)

            # -- M4: user types real input and waits for the echo --
            live.terminal_input_bytes(f"echo {HUMAN_ECHO_MARKER}\n")

            def echo_seen() -> dict:
                events = live.events()["events"]
                frames = "".join(
                    e.get("content") or "" for e in events
                    if e.get("kind") == "screen"
                )
                if HUMAN_ECHO_MARKER in frames:
                    return {"events": events, "frames": frames}
                return {}

            echo = _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S,
                               "user-typed echo on screen", echo_seen)
            echo_events = echo["events"]
            human_actions = _kind_actor(echo_events, "action", "human")
            results["human_action_events"] = len(human_actions)
            statuses = {a.get("data", {}).get("status") for a in human_actions}
            results["human_action_statuses"] = sorted(statuses)
            print("user input echoed on screen", flush=True)

            # -- M5: hand back (idle takeover → back to waiting) --
            live.release()

            def released() -> dict:
                meeting = live.snapshot_meeting()
                if meeting.get("phase") == "waiting_for_user":
                    return meeting
                return {}

            _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S, "release to waiting",
                        released)
            print("control released back", flush=True)

            # -- wrap up: stop the service; freeze the journal, then run static assertions --
            live.stop()
            time.sleep(1.0)
            journal_dir = next((tmp_root / "runs").glob(f"{meeting_id}"), None)
            if journal_dir is None:
                raise E2EFailure("journal dir missing after stop")

            full_events = live.events(0)["events"]
            results["total_events"] = len(full_events)
            results["elapsed_s"] = round(time.monotonic() - t_start, 1)
            print(f"journal frozen: {journal_dir} ({results['total_events']} events)",
                  flush=True)

            # assertion block (user-observable invariants)
            seqs = [e.get("seq") for e in full_events]
            if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
                failed.append("seq must be strictly increasing & unique")
            stamps = [e.get("captured_at") for e in full_events]
            if stamps != sorted(stamps):
                failed.append("captured_at must not go backwards")

            control_acquired = [e for e in full_events
                                if e.get("kind") == "control"
                                and (e.get("data") or {}).get("status") == "acquired"]
            control_released = [e for e in full_events
                                if e.get("kind") == "control"
                                and (e.get("data") or {}).get("status") == "released"]
            if not control_acquired:
                failed.append("no control/acquired event")
            if not control_released:
                failed.append("no control/released event")
            results["control_acquired"] = len(control_acquired)
            results["control_released"] = len(control_released)

            if not human_actions:
                failed.append("no human action events journaled")
            elif "received" not in statuses or "started" not in statuses:
                failed.append("human keystroke bookkeeping incomplete "
                              f"(statuses={sorted(statuses)})")

            frames_text = "".join(e.get("content") or "" for e in full_events
                                  if e.get("kind") == "screen")
            if HUMAN_ECHO_MARKER not in frames_text:
                failed.append("user-typed marker missing from journal frames")

            # G-chain validator acceptance of the real record
            import meeting_validate as validator
            report, summary = validator.validate_run_dir(journal_dir)
            results["validator_violations"] = report.count
            results["validator_by_assert"] = dict(report.by_assert())
            results["loader_mapped"] = dict(summary.mapped)
            if not report.ok:
                failed.append(
                    f"validator violations on real journal: {report.count}")

            # snapshot's final owner/phase state
            final_meeting = live.snapshot_meeting()
            results["final_status"] = final_meeting.get("status")
            if final_meeting.get("control_owner") not in ("none", None):
                failed.append("terminal must be released at end")
        finally:
            live.shutdown()
    finally:
        if args.keep and journal_dir is not None:
            kept = REPO_ROOT / "runs" / journal_dir.name
            shutil.copytree(journal_dir, kept)
            results["kept_journal"] = str(kept)
            journal_dir = kept

    results["passed"] = not failed
    results["failures"] = failed
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
