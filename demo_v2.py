#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

from codex_app_server_client import CodexAppServerError, probe_codex_backend
from claude_code_client import ClaudeCodeError, probe_claude_backend
from meeting_v2 import MeetingError, MeetingStore
from model_api_client import ModelApiConfig, ModelConfigError
from tmux_transport import TmuxError, TmuxTransport, libtmux_version


PROJECT_ROOT = Path(__file__).resolve().parent


def doctor() -> bool:
    checks = [("python", sys.version_info[:2] == (3, 12), ".".join(map(str, sys.version_info[:3])))]
    for name in ("tmux", "vim"):
        path = shutil.which(name)
        checks.append((name, path is not None, path or "not found"))
    version = libtmux_version()
    checks.append(("libtmux", version is not None, version or "not installed"))
    try:
        config = ModelApiConfig.load(PROJECT_ROOT / "model_api")
        key_missing = (
            config.agent_backend == "responses" or config.agent_provider == "deepseek"
        ) and not config.api_key
        if key_missing:
            checks.append(("model-api", False, "auth.json is missing the selected provider's API key"))
        else:
            checks.append(("model-api", True, f"{config.model} ({config.agent_backend}/{config.agent_provider})"))
        for warning in config.warnings:
            print(f"[WARN] model-api: {warning}")
        if config.agent_backend == "codex-app-server":
            try:
                version = probe_codex_backend(config)
                checks.append(("codex-app-server", True, version))
            except CodexAppServerError as exc:
                checks.append(("codex-app-server", False, str(exc)))
        elif config.agent_backend == "claude-code":
            try:
                version = probe_claude_backend(config)
                checks.append(("claude-code", True, version))
            except ClaudeCodeError as exc:
                checks.append(("claude-code", False, str(exc)))
    except ModelConfigError as exc:
        checks.append(("model-api", False, str(exc)))
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    return all(ok for _name, ok, _detail in checks)


def command_status(args: argparse.Namespace) -> int:
    store = MeetingStore.load(PROJECT_ROOT, args.meeting_id)
    meeting = store.meeting
    print(f"Meeting: {meeting.meeting_id}")
    print(f"Status: {meeting.status} / {meeting.phase}")
    print(f"Control: {meeting.control_owner}")
    print(f"Agent backend: {meeting.agent_backend}")
    print(f"Agent provider: {meeting.agent_provider}")
    if meeting.agent_session_id:
        print(f"Agent session: {meeting.agent_session_id}")
    print(f"Session: {meeting.session_name} ({meeting.session_state})")
    print(f"Events: {store.event_count()}")
    if meeting.failure:
        print(f"Failure: {meeting.failure}")
    if meeting.session_state != "stopped":
        print(f"Attach: {TmuxTransport.attach_command(meeting.session_name)}")
    return 0


def command_stop(args: argparse.Namespace) -> int:
    store = MeetingStore.load(PROJECT_ROOT, args.meeting_id)
    if store.meeting.status in {"completed", "failed"}:
        raise MeetingError("use cleanup for a finished meeting")
    store.request_stop()
    store.update(
        status="interrupted",
        phase="interrupted",
        control_owner="none",
        control_requested=None,
        failure="stopped by user",
        request_generation=store.meeting.request_generation + 1,
    )
    binary = shutil.which("tmux")
    if store.meeting.session_state in {"running", "retained"} and not binary:
        raise MeetingError("tmux not found; the workspace session could not be cleaned up")
    if binary and store.meeting.session_state != "stopped":
        TmuxTransport(binary).kill_session(store.meeting.session_name)
    store.mark_session_stopped()
    print(f"Stopped: {store.meeting.meeting_id}")
    return 0


def command_cleanup(args: argparse.Namespace) -> int:
    store = MeetingStore.load(PROJECT_ROOT, args.meeting_id)
    if store.meeting.status not in {"completed", "failed", "interrupted"}:
        raise MeetingError("meeting must be finished before cleanup")
    binary = shutil.which("tmux")
    if store.meeting.session_state in {"running", "retained"} and not binary:
        raise MeetingError("tmux not found; the workspace session could not be cleaned up")
    if binary and store.meeting.session_state != "stopped":
        TmuxTransport(binary).kill_session(store.meeting.session_name)
    store.mark_session_stopped()
    print(f"Cleaned: {store.meeting.meeting_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage RemoteBuddy v2 workspace sessions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="check local prerequisites")
    doctor_parser.set_defaults(handler=lambda _args: 0 if doctor() else 2)
    status = subparsers.add_parser("status", help="show a v2 workspace session")
    status.add_argument("meeting_id")
    status.set_defaults(handler=command_status)
    stop = subparsers.add_parser("stop", help="interrupt and remove a workspace session")
    stop.add_argument("meeting_id")
    stop.set_defaults(handler=command_stop)
    cleanup = subparsers.add_parser("cleanup", help="remove a retained tmux session")
    cleanup.add_argument("meeting_id")
    cleanup.set_defaults(handler=command_cleanup)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (MeetingError, TmuxError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
