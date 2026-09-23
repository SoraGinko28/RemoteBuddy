"""e2e_two_agents — live two-process meeting: actor + @-summoned reviewer.

Creates a meeting under the REPO runs/ directory (so the uid is browsable),
drives a real actor claude-code process to finish a task, then sends an
@critic message that summons a SECOND real claude-code process. Asserts:

- the reviewer's final comment is recorded under its own name ("critic"),
- the actor's final is recorded under its own name ("agent"),
- the two are distinguishable actors in the same journal,
- only human/actor ever appear in terminal-control records,
- the recorded journal validates with 0 violations.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent
CSRF_HEADER = "X-RemoteBuddy-CSRF"
ACTOR_TASK = ("List the files in the current directory with ls -la, "
                  "then print DONE_GEN_7E3. Do not write files.")
REVIEWER_ASK = "@critic please review what the actor just did. One short paragraph."


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Live:
    def __init__(
        self,
        backend: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        task: str = ACTOR_TASK,
    ) -> None:
        self.backend = backend
        self.provider = provider
        self.model = model
        self.task = task
        import web_v2
        service = web_v2.MeetingWebService(
            project_root=REPO,
            model_api_path=REPO / "model_api",
            skill_path=REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md",
        )
        self.service = service
        server = web_v2.build_server("127.0.0.1", _free_port(), service=service)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        self.port = server.server_address[1]
        self.csrf = ""
        self.meeting_id = ""

    def _req(self, method: str, path: str, payload=None, timeout: float = 60.0) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if method == "POST":
            headers[CSRF_HEADER] = self.csrf
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} {path} -> {response.status}: {raw[:200]}")
        if response.status >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status}: {parsed}")
        return parsed

    def connect(self) -> None:
        self.csrf = self._req("GET", "/api/session")["csrf_token"]

    def join(self, meeting_id: str) -> None:
        """Attach to a meeting already on disk (its actor has parked)."""
        self.connect()
        self.meeting_id = meeting_id
        print(f"joined meeting {meeting_id} on :{self.port}", flush=True)

    def start(self) -> None:
        self.connect()
        meeting_id = "multi-" + uuid.uuid4().hex[:8]
        payload = {
            "meeting_id": meeting_id,
            "message": self.task,
            "typing_profile": "fast",
            # No role map: the meeting's own default team, which names the
            # members actor / critic / assistant. A driver that invents its own
            # names makes the meeting show "agent" where the actor is.
        }
        # Only send the harness fields the caller asked for; otherwise the
        # meeting keeps the configured default (claude-code/deepseek).
        if self.backend is not None:
            payload["agent_backend"] = self.backend
        if self.provider is not None:
            payload["agent_provider"] = self.provider
        if self.model is not None:
            payload["model"] = self.model
        result = self._req("POST", "/api/meetings", payload)
        self.meeting_id = result["meeting_id"]
        print(f"meeting {self.meeting_id} on :{self.port}", flush=True)

    def events(self) -> list:
        return self._req("GET", f"/api/meetings/{self.meeting_id}/events?after=0")["events"]

    def send(self, message: str) -> None:
        self._req("POST", f"/api/meetings/{self.meeting_id}/messages", {"message": message})

    def park(self) -> None:
        """Park a meeting this process was driving, if it is mid-turn."""
        try:
            store = self.service.load_store(self.meeting_id)
            if store.meeting.status in {"completed", "failed", "interrupted"}:
                return
            if store.meeting.phase == "waiting_for_user" and store.meeting.control_owner == "none":
                return
            store.update(phase="waiting_for_user", control_owner="none",
                         pending_action_id=None, recovery_required=True,
                         failure="the driver that owned this run exited mid-turn; nothing is running")
        except Exception as exc:  # noqa: BLE001
            print(f"park warning: {exc}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        try:
            self._req("POST", f"/api/meetings/{self.meeting_id}/stop", {}, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            print(f"stop warning: {exc}", file=sys.stderr, flush=True)

    def shutdown(self) -> None:
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _wait(predicate, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1.0)
    raise TimeoutError(f"timeout waiting for {what}")


def _finals(events, actor: str) -> list:
    return [e for e in events
            if e.get("kind") == "chat" and e.get("actor") == actor
            and e.get("message_type") == "final"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="RemoteBuddy live two-agent E2E")
    parser.add_argument("--stop", action="store_true",
                        help="freeze (archive) the meeting instead of keeping it live")
    parser.add_argument("--backend", default=None,
                        help="agent_backend for the actor (default: config)")
    parser.add_argument("--provider", default=None,
                        help="agent_provider for the actor (default: config)")
    parser.add_argument("--model", default=None,
                        help="model for the actor; pass '' for the CLI default")
    parser.add_argument("--task", default=ACTOR_TASK,
                        help="what the actor is asked to do")
    parser.add_argument("--join", default=None,
                        help="skip phase 1 and review this existing meeting id")
    parser.add_argument("--wait-answer", type=float, default=0.0,
                        help="after the critic speaks, wait this long for the actor "
                             "to answer it (the assistant reminds it to)")
    parser.add_argument("--ask", default=REVIEWER_ASK,
                        help="the @critic message that summons the reviewer; "
                             "empty means nobody asks and the assistant decides")
    options = parser.parse_args()

    freeze = options.stop
    live = _Live(backend=options.backend, provider=options.provider,
                 model=options.model, task=options.task)
    summary = {"backend": options.backend, "provider": options.provider,
               "model": options.model, "joined": options.join}
    failed = []
    try:
        if options.join:
            live.join(options.join)
        else:
            live.start()
        live._req("GET", f"/api/meetings/{live.meeting_id}/snapshot")

        # Phase 1: real actor works until the meeting is idle again. A
        # actor that has finished its work parks in completion_proposed
        # (waiting for the human to accept), which is idle too.
        def actor_idle() -> dict:
            snap = live._req("GET", f"/api/meetings/{live.meeting_id}/snapshot")
            meeting = snap.get("meeting", {})
            if meeting.get("phase") in ("waiting_for_user", "completion_proposed") \
                    and meeting.get("control_owner") in ("none", None):
                return meeting
            return {}

        if not options.join:
            idle = _wait(actor_idle, 420, "actor to reach idle")
            actor_chats = [e for e in live.events()
                               if e.get("kind") == "chat" and e.get("actor") == "agent"]
            summary["actor_chat_count"] = len(actor_chats)
            summary["actor_phase"] = idle.get("phase")
            print("actor idle; chats:", len(actor_chats), flush=True)

        # Phase 2: @critic summons the second process — unless the human says
        # nothing, and the assistant is the one that decides to wake it.
        if options.ask:
            live.send(options.ask)
        critic_finals = _wait(lambda: _finals(live.events(), "critic") or None,
                              400, "reviewer final chat")
        summary["critic_final"] = critic_finals[-1]["content"][:160]
        print("critic final recorded", flush=True)

        # Phase 3 (optional): the critic has spoken; does the actor answer it?
        # Nobody asks by hand — the assistant watches for exactly this.
        if options.wait_answer:
            spoke_at = critic_finals[-1].get("captured_at") or ""

            def actor_answered() -> list:
                answers = [e for e in live.events()
                           if e.get("kind") == "chat" and e.get("actor") == "agent"
                           and (e.get("captured_at") or "") > spoke_at]
                return answers or None

            answers = _wait(actor_answered, options.wait_answer,
                            "the actor to answer the critic")
            summary["actor_answer"] = answers[-1]["content"][:160]
            print("actor answered:", answers[-1]["content"][:80], flush=True)

        events = live.events()
        summary["meeting_id"] = live.meeting_id
        summary["chat_actors"] = sorted({e.get("actor") for e in events if e.get("kind") == "chat"})
        controls = [e for e in events if e.get("kind") == "control"]
        control_owners = sorted({(e.get("data") or {}).get("user_name") for e in controls})
        summary["control_user_names"] = control_owners

        if "agent" not in summary["chat_actors"] or "critic" not in summary["chat_actors"]:
            failed.append("both actor and critic must have chat records")
        if not (set(control_owners) <= {"human", "agent"}):
            failed.append(f"reviewer must never own terminal: {control_owners}")
        if not critic_finals:
            failed.append("missing critic final chat")

        # Validator on the real record.
        import meeting_validate as validator
        report, _ = validator.validate_run_dir(REPO / "runs" / live.meeting_id)
        summary["validator_violations"] = report.count
        if not report.ok:
            failed.append(f"validator violations: {report.count}")
    finally:
        # The worker and the assistant's watch live in this process: leaving
        # without parking the meeting leaves it claiming a turn nobody is
        # running (the page shows that member as 异常).
        live.park()
        if freeze:
            live.stop()
            time.sleep(1.0)
        # keep-live (default): leave the meeting running/waiting so the user
        # can keep chatting and taking over; freeze only with --stop.
        live.shutdown()

    summary["passed"] = not failed
    summary["failures"] = failed
    summary["run_dir"] = f"runs/{summary.get('meeting_id','')}"
    summary["kept_live"] = not freeze
    summary["hint"] = "kept live: open it in the UI to keep chatting; use --stop to freeze (archive)."
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
