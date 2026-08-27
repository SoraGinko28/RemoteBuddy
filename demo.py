#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from Meeting import IntegrityError, InvalidTurnError, MeetingError, MeetingStore
from tmux_transport import TmuxError, TmuxTransport


PROJECT_ROOT = Path(__file__).resolve().parent
SKILL_PATH = PROJECT_ROOT / ".agents" / "skills" / "meeting-participant" / "SKILL.md"
TURN_SPECS: Tuple[Tuple[str, str, str], ...] = (
    (
        "001-proposer",
        "proposer",
        "Propose a concrete approach for the brief, including assumptions, scope, and evidence needed.",
    ),
    (
        "002-reviewer",
        "reviewer",
        "Challenge the proposal's most important risks and identify the evidence needed to resolve them.",
    ),
    (
        "003-proposer",
        "proposer",
        "Revise the proposal in response to the review, explicitly resolving or accepting each major concern.",
    ),
)
TRUST_PROMPT_TEXT = "Do you trust the contents of this"
UPDATE_PROMPT_TEXT = "Update available!"
STARTUP_GATE_TEXT = "Press enter to continue"
READY_PROMPT_TEXT = "Ask Codex to do anything"
INPUT_SETTLE_SECONDS = 5.0


class HarnessError(RuntimeError):
    pass


class MeetingStopped(HarnessError):
    pass


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def generated_meeting_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{secrets.token_hex(3)}"


def doctor_checks() -> List[DoctorCheck]:
    version = sys.version_info
    checks = [
        DoctorCheck(
            "python",
            version >= (3, 9),
            f"{version.major}.{version.minor}.{version.micro}",
        )
    ]
    binaries: Dict[str, Optional[str]] = {
        name: shutil.which(name) for name in ("codex", "vim", "tmux")
    }
    for name in ("codex", "vim", "tmux"):
        path = binaries[name]
        checks.append(DoctorCheck(name, path is not None, path or "not found"))

    codex_path = binaries["codex"]
    if codex_path:
        try:
            result = subprocess.run(
                [codex_path, "login", "status"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            checks.append(
                DoctorCheck(
                    "codex-login",
                    result.returncode == 0,
                    "authenticated" if result.returncode == 0 else "not authenticated",
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            checks.append(DoctorCheck("codex-login", False, "status check failed"))
    else:
        checks.append(DoctorCheck("codex-login", False, "codex not found"))
    return checks


def print_doctor(checks: Sequence[DoctorCheck]) -> bool:
    for check in checks:
        label = "OK" if check.ok else "FAIL"
        print(f"[{label}] {check.name}: {check.detail}")
    return all(check.ok for check in checks)


def require_runtime() -> Dict[str, str]:
    checks = doctor_checks()
    failures = [check.name for check in checks if not check.ok]
    if failures:
        print_doctor(checks)
        raise HarnessError("doctor checks failed: " + ", ".join(failures))
    paths = {name: shutil.which(name) for name in ("codex", "tmux")}
    return {name: path for name, path in paths.items() if path is not None}


class MeetingHarness:
    def __init__(
        self,
        store: MeetingStore,
        transport: TmuxTransport,
        panes: Dict[str, str],
        turn_timeout: float,
        capture_interval: float,
        startup_timeout: float = 30.0,
        input_settle_seconds: float = INPUT_SETTLE_SECONDS,
    ) -> None:
        self.store = store
        self.transport = transport
        self.panes = panes
        self.turn_timeout = turn_timeout
        self.capture_interval = capture_interval
        self.startup_timeout = startup_timeout
        self.input_settle_seconds = input_settle_seconds

    def _check_external_stop(self) -> None:
        if self.store.stop_requested():
            raise MeetingStopped("meeting stopped by user")
        try:
            payload = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("meeting manifest became unreadable") from exc
        if payload.get("session_state") == "stopped":
            raise MeetingStopped("meeting stopped by user")

    def _assert_inputs_unchanged(self, snapshot: Dict[str, str]) -> None:
        self._check_external_stop()
        self.store.assert_protected_unchanged(snapshot)

    def _assert_panes_alive(self) -> None:
        dead = [
            participant
            for participant, pane_id in self.panes.items()
            if not self.transport.pane_alive(pane_id)
        ]
        if dead:
            raise HarnessError("Codex pane exited: " + ", ".join(dead))

    def capture_all(self, phase: str) -> Dict[str, str]:
        snapshots: Dict[str, str] = {}
        for participant, pane_id in self.panes.items():
            content = self.transport.capture(pane_id)
            snapshots[participant] = content
            self.store.append_screen(participant, pane_id, content, phase)
        return snapshots

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        handled_gates = set()
        while True:
            self._check_external_stop()
            self._assert_panes_alive()
            snapshots = self.capture_all("startup")
            for participant, content in snapshots.items():
                update_gate = (participant, "update")
                if (
                    self._current_startup_gate(content, UPDATE_PROMPT_TEXT)
                    and update_gate not in handled_gates
                ):
                    self.transport.send_menu_choice(self.panes[participant], "2")
                    handled_gates.add(update_gate)
                elif self._current_startup_gate(content, TRUST_PROMPT_TEXT):
                    raise HarnessError(
                        "Codex showed an unexpected trust prompt despite the per-run trust override"
                    )
            if all(self._pane_is_ready(content) for content in snapshots.values()):
                return
            if time.monotonic() >= deadline:
                raise HarnessError(
                    "Codex panes did not clear a startup prompt before startup timeout"
                )
            remaining = deadline - time.monotonic()
            time.sleep(min(self.capture_interval, max(0.0, remaining)))

    @staticmethod
    def _pane_is_ready(content: str) -> bool:
        last_ready_prompt = content.rfind(READY_PROMPT_TEXT)
        last_startup_gate = content.rfind(STARTUP_GATE_TEXT)
        return last_ready_prompt >= 0 and last_ready_prompt > last_startup_gate

    @staticmethod
    def _current_startup_gate(content: str, gate_text: str) -> bool:
        return content.rfind(gate_text) > content.rfind(READY_PROMPT_TEXT)

    @staticmethod
    def turn_prompt(turn_id: str, role: str, output_path: str, objective: str) -> str:
        return (
            "$meeting-participant Execute the assigned RemoteBuddy meeting turn. "
            f"role={role}; turn_id={turn_id}; output_path={output_path}; "
            f"objective={objective} Use only pwd, cd, and non-interactive Vim Ex "
            "commands that exit by themselves. Follow the skill protocol exactly."
        )

    def _execute_turn(self, turn_id: str) -> None:
        turn = self.store.turn(turn_id)
        self.store.start_turn(turn_id)
        protected = self.store.protected_snapshot(turn_id)
        pane_id = self.panes[turn.speaker]
        prompt = self.turn_prompt(
            turn.turn_id, turn.speaker, turn.output_path, turn.objective
        )
        deadline = time.monotonic() + self.turn_timeout
        self.transport.send_literal(pane_id, prompt)
        settle_deadline = min(
            deadline, time.monotonic() + self.input_settle_seconds
        )
        while time.monotonic() < settle_deadline:
            self._check_external_stop()
            self._assert_panes_alive()
            self.capture_all(f"turn:{turn.turn_id}:input")
            self._assert_inputs_unchanged(protected)
            remaining = settle_deadline - time.monotonic()
            time.sleep(min(self.capture_interval, max(0.0, remaining)))
        if time.monotonic() >= deadline:
            raise HarnessError(f"turn timed out before prompt submission: {turn.turn_id}")
        self.transport.send_enter(pane_id)
        acknowledgement = f"TURN_COMPLETE {turn.turn_id}"

        while True:
            self._check_external_stop()
            self._assert_panes_alive()
            snapshots = self.capture_all(f"turn:{turn.turn_id}")
            self._assert_inputs_unchanged(protected)
            turn_snapshot = self.store.read_completed_turn_snapshot(turn.turn_id)
            body = turn_snapshot[0] if turn_snapshot is not None else None
            source_sha256 = turn_snapshot[1] if turn_snapshot is not None else None
            acknowledged = self._has_exact_ack(
                snapshots[turn.speaker], acknowledgement
            )
            if acknowledged and body is None:
                raise InvalidTurnError(
                    f"turn {turn.turn_id} acknowledged completion without a valid turn file"
                )
            if body is not None and acknowledged:
                event = self.store.append_chat(
                    turn.turn_id,
                    turn.speaker,
                    body,
                    source_sha256=source_sha256,
                )
                self.store.complete_turn(turn.turn_id, event.event_id)
                return
            if time.monotonic() >= deadline:
                raise HarnessError(f"turn timed out without both completion signals: {turn.turn_id}")
            remaining = deadline - time.monotonic()
            time.sleep(min(self.capture_interval, max(0.0, remaining)))

    @staticmethod
    def _has_exact_ack(content: str, acknowledgement: str) -> bool:
        """Accept one complete visible line, allowing common TUI bullets/prefixes."""
        for line in content.splitlines():
            normalized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
            normalized = normalized.strip(" \t•›>│")
            if normalized == acknowledgement:
                return True
        return False

    def _final_capture(self, phase: str) -> None:
        try:
            self.capture_all(phase)
        except (HarnessError, MeetingError, TmuxError, OSError):
            pass

    def run(self) -> None:
        try:
            self.store.set_status("running")
            self._wait_until_ready()
            for turn in self.store.meeting.turns:
                self._execute_turn(turn.turn_id)
            self._final_capture("completed")
            self.store.set_status("completed")
            self.store.mark_session_retained()
        except KeyboardInterrupt:
            self._final_capture("interrupted")
            self.store.set_status("interrupted", "interrupted by user")
            self.store.mark_session_retained()
            raise
        except MeetingStopped:
            self._final_capture("stopped")
            try:
                self.store.refresh()
            except MeetingError:
                pass
            if self.store.meeting.status in {"created", "running"}:
                self.store.mark_session_stopped()
            raise
        except Exception as exc:
            if self.store.stop_requested():
                self._final_capture("stopped")
                try:
                    self.store.refresh()
                except MeetingError:
                    pass
                if self.store.meeting.status in {"created", "running"}:
                    self.store.mark_session_stopped()
                raise MeetingStopped("meeting stopped by user") from exc
            self._final_capture("failed")
            self.store.set_status("failed", str(exc))
            self.store.mark_session_retained()
            raise


class MeetingRunner:
    """Create tmux panes and run the shared three-turn Harness."""

    def __init__(
        self,
        store: MeetingStore,
        runtime: Dict[str, str],
        model: Optional[str],
        turn_timeout: float,
        capture_interval: float,
        transport: Optional[TmuxTransport] = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.model = model
        self.turn_timeout = turn_timeout
        self.capture_interval = capture_interval
        self.transport = transport or TmuxTransport(runtime["tmux"])
        self.panes: Optional[Dict[str, str]] = None

    def run(self) -> None:
        session_name = self.store.meeting.session_name
        try:
            if self.store.stop_requested() or self.store.meeting.session_state == "stopped":
                if self.store.meeting.status in {"created", "running"}:
                    self.store.mark_session_stopped()
                raise MeetingStopped("meeting stopped by user")
            self.panes = self.transport.create_two_agent_session(
                session_name,
                self.store.run_dir,
                self.runtime["codex"],
                self.model,
            )
            if self.store.stop_requested():
                try:
                    self.transport.kill_session(session_name)
                finally:
                    self.store.mark_session_stopped()
                raise MeetingStopped("meeting stopped by user")
            self.store.set_panes(self.panes)
            MeetingHarness(
                self.store,
                self.transport,
                self.panes,
                self.turn_timeout,
                self.capture_interval,
            ).run()
        except MeetingStopped:
            if self.store.meeting.status in {"created", "running"}:
                self.store.mark_session_stopped()
            raise
        except KeyboardInterrupt:
            if self.store.meeting.status in {"created", "running"}:
                self.store.set_status("interrupted", "interrupted by user")
            raise
        except Exception as exc:
            try:
                if self.store.meeting.status == "created":
                    self.store.set_status("failed", str(exc))
                if self.transport.session_exists(session_name):
                    self.store.mark_session_retained()
            except (MeetingError, TmuxError, OSError):
                pass
            raise


def request_stop(store: MeetingStore, tmux_binary: Optional[str] = None) -> bool:
    """Signal the runner before killing panes; safe to call repeatedly."""
    if store.meeting.status in {"completed", "failed", "interrupted"} and store.meeting.session_state not in {
        "running",
        "retained",
    }:
        return False
    store.request_stop()
    binary = tmux_binary or shutil.which("tmux")
    removed = False
    if binary:
        removed = TmuxTransport(binary).kill_session(store.meeting.session_name)
    store.mark_session_stopped()
    return removed


def print_session_commands(store: MeetingStore) -> None:
    print(f"Run directory: {store.run_dir}")
    print(f"Attach: {TmuxTransport.attach_command(store.meeting.session_name)}")
    print(f"Cleanup: python3 {PROJECT_ROOT / 'demo.py'} stop {store.meeting.meeting_id}")


def command_doctor(_args: argparse.Namespace) -> int:
    return 0 if print_doctor(doctor_checks()) else 2


def command_run(args: argparse.Namespace) -> int:
    brief_argument = Path(args.brief).expanduser()
    if not brief_argument.is_absolute():
        raise HarnessError("--brief must be an absolute path")
    runtime = require_runtime()
    meeting_id = args.meeting_id or generated_meeting_id()
    session_name = f"remotebuddy-{meeting_id}"
    store = MeetingStore.create(
        PROJECT_ROOT,
        meeting_id,
        brief_argument,
        SKILL_PATH,
        session_name,
        TURN_SPECS,
        args.model,
        args.turn_timeout,
        args.capture_interval,
    )
    transport = TmuxTransport(runtime["tmux"])
    try:
        MeetingRunner(
            store,
            runtime,
            args.model,
            args.turn_timeout,
            args.capture_interval,
            transport=transport,
        ).run()
    except KeyboardInterrupt:
        print(f"Meeting interrupted: {meeting_id}", file=sys.stderr)
        print_session_commands(store)
        return 130
    except MeetingStopped as exc:
        print(f"Meeting stopped: {exc}", file=sys.stderr)
        print_session_commands(store)
        return 130
    except (HarnessError, IntegrityError, MeetingError, TmuxError) as exc:
        print(f"Meeting failed: {exc}", file=sys.stderr)
        print_session_commands(store)
        return 1

    print(f"Meeting completed: {meeting_id}")
    print_session_commands(store)
    return 0


def command_status(args: argparse.Namespace) -> int:
    store = MeetingStore.load(PROJECT_ROOT, args.meeting_id)
    meeting = store.meeting
    print(f"Meeting: {meeting.meeting_id}")
    print(f"Status: {meeting.status}")
    print(f"Session: {meeting.session_name} ({meeting.session_state})")
    print(f"Turns: {sum(turn.status == 'completed' for turn in meeting.turns)}/{len(meeting.turns)}")
    print(f"Chat events: {store.stream_event_count('chat')}")
    print(f"Proposer screen events: {store.stream_event_count('screen:proposer')}")
    print(f"Reviewer screen events: {store.stream_event_count('screen:reviewer')}")
    if meeting.failure:
        print(f"Failure: {meeting.failure}")
    print(f"Run directory: {store.run_dir}")
    if meeting.session_state != "stopped":
        print(f"Attach: {TmuxTransport.attach_command(meeting.session_name)}")
    return 0


def command_stop(args: argparse.Namespace) -> int:
    store = MeetingStore.load(PROJECT_ROOT, args.meeting_id)
    tmux_path = shutil.which("tmux")
    if not tmux_path and store.meeting.session_state in {"running", "retained"}:
        raise HarnessError("tmux not found; cannot stop the retained session")
    removed = request_stop(store, tmux_path)
    outcome = "stopped" if removed else "already absent"
    print(f"Session {outcome}: {store.meeting.session_name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a filesystem-backed two-agent Codex meeting demo."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check local prerequisites")
    doctor.set_defaults(handler=command_doctor)

    run = subparsers.add_parser("run", help="start a three-turn meeting")
    run.add_argument("--brief", required=True, help="absolute path to a UTF-8 brief")
    run.add_argument("--meeting-id", help="lowercase letters, digits, and hyphens")
    run.add_argument("--model", help="Codex model override")
    run.add_argument("--turn-timeout", type=positive_float, default=300.0)
    run.add_argument("--capture-interval", type=positive_float, default=1.0)
    run.set_defaults(handler=command_run)

    status = subparsers.add_parser("status", help="show persisted meeting state")
    status.add_argument("meeting_id")
    status.set_defaults(handler=command_status)

    stop = subparsers.add_parser("stop", help="stop and remove a retained tmux session")
    stop.add_argument("meeting_id")
    stop.set_defaults(handler=command_stop)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (HarnessError, MeetingError, TmuxError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


from demo_v2 import (  # noqa: E402,F401
    PROJECT_ROOT as PROJECT_ROOT,
    build_parser as build_parser,
    doctor as doctor,
    main as main,
)


if __name__ == "__main__":
    raise SystemExit(main())
