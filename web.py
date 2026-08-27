#!/usr/bin/env python3
"""Local RemoteBuddy meeting workbench.

The service exposes durable JSONL streams and starts the same meeting runner
in a background thread. Terminal access is centralized through libtmux.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from Meeting import (
    MAX_BRIEF_BYTES,
    MAX_TURN_BYTES,
    MEETING_ID_RE,
    TURN_COMPLETE_MARKER,
    InvalidTurnError,
    MeetingError,
    MeetingStore,
    read_jsonl,
    sha256_file,
    sha256_text,
)
from demo import (
    PROJECT_ROOT,
    SKILL_PATH,
    TURN_SPECS,
    HarnessError,
    MeetingRunner,
    doctor_checks,
    generated_meeting_id,
    require_runtime,
    request_stop,
)
from tmux_transport import TmuxError


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CSRF_HEADER = "X-RemoteBuddy-CSRF"
MAX_REQUEST_BYTES = MAX_BRIEF_BYTES + 16 * 1024
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
EVENT_ID_RE = re.compile(r"-(\d+)$")
TURN_PHASE_RE = re.compile(r"(?:^|:)turn:([^:]+)")


class WebError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise WebError(400, f"{label} must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise WebError(400, f"{label} must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise WebError(400, f"{label} must be a finite positive number")
    return parsed


def _seq(event_id: str) -> int:
    match = EVENT_ID_RE.search(str(event_id))
    return int(match.group(1)) if match else 0


def _timestamp_key(value: Any) -> str:
    return str(value or "")


def _timeline_key(event: Dict[str, Any]) -> Tuple[str, int, int]:
    # Chat is rendered after the screen capture made in the same millisecond.
    return (
        _timestamp_key(event.get("captured_at")),
        0 if event.get("kind") == "screen" else 1,
        int(event.get("seq", 0)),
    )


def _turn_from_phase(phase: Any) -> Optional[str]:
    match = TURN_PHASE_RE.search(str(phase or ""))
    return match.group(1) if match else None


def _bounded_text(path: Path, limit: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise WebError(413, f"file exceeds {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebError(422, f"file is not UTF-8: {path.name}") from exc


def _event_revision(store: MeetingStore) -> str:
    paths: List[Path] = [
        store.manifest_path,
        store.run_dir / "brief.md",
        store.run_dir / "conversation.md",
        store.stream_path("chat"),
        store.stream_path("screen:proposer"),
        store.stream_path("screen:reviewer"),
        store.stop_request_path,
    ]
    for turn in store.meeting.turns:
        paths.append(store.turn_path(turn.turn_id))
    signature: List[str] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            signature.append(f"{path.name}:missing")
        else:
            signature.append(f"{path.relative_to(store.run_dir)}:{stat.st_size}:{stat.st_mtime_ns}")
    return sha256_text("\n".join(signature))[:24]


class MeetingWebService:
    """Application service used by both the HTTP handler and unit tests."""

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        skill_path: Path = SKILL_PATH,
        runner_factory: Callable[..., Any] = MeetingRunner,
        runtime_provider: Callable[[], Dict[str, str]] = require_runtime,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.skill_path = skill_path.expanduser().resolve()
        self.runner_factory = runner_factory
        self.runtime_provider = runtime_provider
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs: Dict[str, threading.Thread] = {}
        self._stream_cache: Dict[Tuple[str, str], Tuple[Tuple[int, int], List[Dict[str, Any]]]] = {}
        self._health_cache: Optional[Tuple[float, Dict[str, Any]]] = None

    def _runs_root(self) -> Path:
        runs = self.project_root / "runs"
        if runs.exists() and runs.is_symlink():
            raise WebError(500, "runs directory must not be a symlink")
        runs.mkdir(parents=True, exist_ok=True)
        return runs

    def load_store(self, meeting_id: str) -> MeetingStore:
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise WebError(400, "invalid meeting id")
        try:
            return MeetingStore.load(self.project_root, meeting_id)
        except MeetingError as exc:
            message = str(exc)
            status = 404 if "not found" in message else 422
            raise WebError(status, message) from exc

    @staticmethod
    def _public_manifest(store: MeetingStore) -> Dict[str, Any]:
        payload = store.meeting.to_dict()
        # Do not disclose arbitrary source paths from a local filesystem.
        payload.get("brief", {}).pop("source_path", None)
        completed = sum(turn.status == "completed" for turn in store.meeting.turns)
        current = next(
            (turn.turn_id for turn in store.meeting.turns if turn.status == "running"),
            None,
        )
        payload["completed_turns"] = completed
        payload["total_turns"] = len(store.meeting.turns)
        payload["current_turn"] = current
        payload["run_path"] = f"runs/{store.meeting.meeting_id}"
        return payload

    def _stream_events(self, store: MeetingStore, stream_name: str) -> List[Dict[str, Any]]:
        path = store.stream_path(stream_name)
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            signature = (0, 0)
        key = (store.meeting.meeting_id, stream_name)
        with self.lock:
            cached = self._stream_cache.get(key)
            if cached and cached[0] == signature:
                return list(cached[1])
        try:
            raw = read_jsonl(path)
        except MeetingError as exc:
            raise WebError(422, str(exc)) from exc
        events: List[Dict[str, Any]] = []
        for payload in raw:
            event = dict(payload)
            event["seq"] = _seq(event.get("event_id", ""))
            if stream_name == "chat":
                event.update(
                    {
                        "kind": "chat",
                        "participant": event.get("speaker"),
                        "phase": f"turn:{event.get('turn_id')}",
                    }
                )
            else:
                event.update(
                    {
                        "kind": "screen",
                        "participant": event.get("participant"),
                        "turn_id": _turn_from_phase(event.get("phase")),
                    }
                )
            events.append(event)
        with self.lock:
            self._stream_cache[key] = (signature, list(events))
        return events

    def _all_events(self, store: MeetingStore) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "chat": self._stream_events(store, "chat"),
            "proposer": self._stream_events(store, "screen:proposer"),
            "reviewer": self._stream_events(store, "screen:reviewer"),
        }

    def list_meetings(self) -> Dict[str, Any]:
        summaries: List[Dict[str, Any]] = []
        warnings: List[str] = []
        try:
            children = list(self._runs_root().iterdir())
        except OSError as exc:
            raise WebError(500, f"cannot read runs directory: {exc}") from exc
        for directory in children:
            if not directory.is_dir() or not MEETING_ID_RE.fullmatch(directory.name):
                continue
            try:
                store = MeetingStore.load(self.project_root, directory.name)
                summary = self._public_manifest(store)
                summary["revision"] = _event_revision(store)
                summaries.append(summary)
            except (WebError, MeetingError, OSError, ValueError) as exc:
                warnings.append(f"{directory.name}: {exc}")
                summaries.append(
                    {
                        "meeting_id": directory.name,
                        "status": "invalid",
                        "session_state": "unknown",
                        "failure": str(exc),
                        "completed_turns": 0,
                        "total_turns": 0,
                        "updated_at": "",
                        "warnings": [str(exc)],
                    }
                )
        summaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return {"meetings": summaries, "warnings": warnings}

    def snapshot(self, meeting_id: str) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        try:
            brief = _bounded_text(store.run_dir / "brief.md", MAX_BRIEF_BYTES)
            conversation = _bounded_text(store.run_dir / "conversation.md", 4 * MAX_BRIEF_BYTES)
        except WebError:
            raise
        events = self._all_events(store)
        latest = {
            participant: (items[-1] if items else None)
            for participant, items in (
                ("proposer", events["proposer"]),
                ("reviewer", events["reviewer"]),
            )
        }
        chat = events["chat"]
        return {
            "meeting": self._public_manifest(store),
            "brief": brief,
            "conversation": conversation,
            "turns": [dict(turn) for turn in self._public_manifest(store)["turns"]],
            "chat": chat,
            "latest_screens": latest,
            "revision": _event_revision(store),
            "cursors": {
                "chat_after": chat[-1]["seq"] if chat else -1,
                "proposer_after": events["proposer"][-1]["seq"] if events["proposer"] else -1,
                "reviewer_after": events["reviewer"][-1]["seq"] if events["reviewer"] else -1,
            },
        }

    def events(self, meeting_id: str, cursors: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        cursors = cursors or {}
        after: Dict[str, int] = {}
        for key in ("chat_after", "proposer_after", "reviewer_after"):
            raw = cursors.get(key, -1)
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise WebError(400, f"{key} must be an integer") from exc
            if value < -1:
                raise WebError(400, f"{key} must be at least -1")
            after[key] = value
        streams = self._all_events(store)
        chat = [event for event in streams["chat"] if event["seq"] > after["chat_after"]]
        proposer = [event for event in streams["proposer"] if event["seq"] > after["proposer_after"]]
        reviewer = [event for event in streams["reviewer"] if event["seq"] > after["reviewer_after"]]
        timeline = sorted(chat + proposer + reviewer, key=_timeline_key)
        return {
            "meeting_id": meeting_id,
            "events": {
                "chat": chat,
                "proposer": proposer,
                "reviewer": reviewer,
            },
            "timeline": timeline,
            "cursors": {
                "chat_after": streams["chat"][-1]["seq"] if streams["chat"] else after["chat_after"],
                "proposer_after": streams["proposer"][-1]["seq"] if streams["proposer"] else after["proposer_after"],
                "reviewer_after": streams["reviewer"][-1]["seq"] if streams["reviewer"] else after["reviewer_after"],
            },
            "revision": _event_revision(store),
        }

    def turn_detail(self, meeting_id: str, turn_id: str) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        try:
            turn = store.turn(turn_id)
        except MeetingError as exc:
            raise WebError(404, str(exc)) from exc
        path = store.turn_path(turn_id)
        raw = ""
        marker_valid = False
        marker_error: Optional[str] = None
        source_sha256: Optional[str] = None
        if path.is_file():
            try:
                raw = _bounded_text(path, MAX_TURN_BYTES)
                source_sha256 = sha256_file(path)
                snapshot = store.read_completed_turn_snapshot(turn_id)
                marker_valid = snapshot is not None
            except (WebError, InvalidTurnError, OSError) as exc:
                marker_error = str(exc)
                try:
                    raw = _bounded_text(path, MAX_TURN_BYTES)
                except Exception:
                    raw = ""
        events = self._all_events(store)
        chat = next((event for event in events["chat"] if event.get("turn_id") == turn_id), None)
        related = [
            event
            for participant in ("proposer", "reviewer")
            for event in events[participant]
            if event.get("turn_id") == turn_id
        ]
        related.sort(key=_timeline_key)
        before: Dict[str, Optional[Dict[str, Any]]] = {"proposer": None, "reviewer": None}
        after: Dict[str, Optional[Dict[str, Any]]] = {"proposer": None, "reviewer": None}
        for participant in before:
            participant_events = events[participant]
            indices = [
                index for index, event in enumerate(participant_events)
                if event.get("turn_id") == turn_id
            ]
            if indices:
                first, last = indices[0], indices[-1]
                if first > 0:
                    before[participant] = participant_events[first - 1]
                if last + 1 < len(participant_events):
                    after[participant] = participant_events[last + 1]
        return {
            "meeting_id": meeting_id,
            "turn": dict(turn.__dict__),
            "raw": raw,
            "marker": {
                "expected": TURN_COMPLETE_MARKER,
                "valid": marker_valid,
                "error": marker_error,
            },
            "source_sha256": source_sha256,
            "manifest_sha256": store.meeting.hashes.get(turn.output_path),
            "chat_event": chat,
            "screen_events": related,
            "screen_before": before,
            "screen_after": after,
        }

    def _new_id(self) -> str:
        for _ in range(10):
            candidate = generated_meeting_id()
            if not (self.project_root / "runs" / candidate).exists():
                return candidate
        raise WebError(500, "could not allocate a meeting id")

    def start(self, payload: Dict[str, Any]) -> MeetingStore:
        if not isinstance(payload, dict):
            raise WebError(400, "request must be a JSON object")
        brief = payload.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise WebError(400, "brief must be a non-empty string")
        try:
            if len(brief.encode("utf-8")) > MAX_BRIEF_BYTES:
                raise WebError(413, f"brief exceeds {MAX_BRIEF_BYTES} bytes")
        except UnicodeEncodeError as exc:
            raise WebError(400, "brief must be UTF-8 text") from exc
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or len(model) > 200):
            raise WebError(400, "model must be a short string or null")
        timeout = _finite_positive(payload.get("turn_timeout_seconds", 300), "turn_timeout_seconds")
        interval = _finite_positive(payload.get("capture_interval_seconds", 1), "capture_interval_seconds")
        meeting_id = payload.get("meeting_id") or self._new_id()
        if not isinstance(meeting_id, str) or not MEETING_ID_RE.fullmatch(meeting_id):
            raise WebError(400, "invalid meeting id")
        session_name = f"remotebuddy-{meeting_id}"
        try:
            store = MeetingStore.create_from_text(
                self.project_root,
                meeting_id,
                brief,
                self.skill_path,
                session_name,
                TURN_SPECS,
                model,
                timeout,
                interval,
            )
        except MeetingError as exc:
            raise WebError(400, str(exc)) from exc
        with self.lock:
            worker = threading.Thread(
                target=self._run_worker,
                args=(store, model, timeout, interval),
                name=f"remotebuddy-{meeting_id}",
                daemon=True,
            )
            self.jobs[meeting_id] = worker
            worker.start()
        return store

    def _run_worker(self, store: MeetingStore, model: Optional[str], timeout: float, interval: float) -> None:
        try:
            runtime = self.runtime_provider()
            self.runner_factory(store, runtime, model, timeout, interval).run()
        except Exception as exc:
            try:
                current = MeetingStore.load(self.project_root, store.meeting.meeting_id)
                if current.meeting.status in {"created", "running"}:
                    if current.stop_requested():
                        current.mark_session_stopped()
                    else:
                        current.set_status("failed", str(exc))
                        current.mark_session_retained()
            except Exception:
                pass
        finally:
            with self.lock:
                self.jobs.pop(store.meeting.meeting_id, None)

    def stop(self, meeting_id: str) -> MeetingStore:
        store = self.load_store(meeting_id)
        if store.meeting.session_state != "stopped":
            try:
                request_stop(store, shutil.which("tmux"))
            except (HarnessError, MeetingError, TmuxError, OSError) as exc:
                # The durable signal is still enough for a runner to exit; make
                # the user-visible state deterministic even if tmux vanished.
                try:
                    store.mark_session_stopped()
                except MeetingError:
                    raise WebError(500, str(exc)) from exc
        return self.load_store(meeting_id)

    def health(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            if self._health_cache and now - self._health_cache[0] < 5:
                return dict(self._health_cache[1])
        checks = doctor_checks()
        payload = {
            "ok": all(check.ok for check in checks),
            "python": ".".join(str(part) for part in __import__("sys").version_info[:3]),
            "checks": [dict(name=check.name, ok=check.ok, detail=check.detail) for check in checks],
            "active_jobs": len(self.jobs),
            "service": "running",
        }
        with self.lock:
            self._health_cache = (now, payload)
        return payload


class RemoteBuddyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], service: MeetingWebService):
        self.service = service
        super().__init__(server_address, RemoteBuddyRequestHandler)


class RemoteBuddyRequestHandler(BaseHTTPRequestHandler):
    server_version = "RemoteBuddy/0.1"

    @property
    def service(self) -> MeetingWebService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the local CLI readable; errors are still returned in JSON.
        return

    def _json(self, payload: Dict[str, Any], status: int = 200, etag: Optional[str] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status=status)

    def _read_json(self) -> Dict[str, Any]:
        header = self.headers.get("Content-Length")
        if header is None:
            raise WebError(411, "Content-Length is required")
        try:
            length = int(header)
        except ValueError as exc:
            raise WebError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise WebError(413, "request body is too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise WebError(400, "incomplete request body")
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(400, "request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise WebError(400, "request body must be a JSON object")
        return value

    def _require_csrf(self) -> None:
        supplied = self.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(supplied, self.service.csrf_token):
            raise WebError(403, "missing or invalid CSRF token")

    def _static(self, relative: str) -> None:
        root = (self.service.project_root / "frontend").resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise WebError(404, "static file not found")
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        etag = '"' + hashlib.sha256(data).hexdigest()[:24] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/":
                self._static("index.html")
                return
            if path.startswith("/assets/"):
                relative = unquote(path[len("/assets/"):])
                if "/" in relative or relative.startswith("."):
                    raise WebError(404, "static file not found")
                self._static(relative)
                return
            if path == "/api/session":
                self._json({"csrf_token": self.service.csrf_token, "csrf_header": CSRF_HEADER})
                return
            if path == "/api/health":
                self._json(self.service.health())
                return
            if path == "/api/meetings":
                self._json(self.service.list_meetings())
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/snapshot", path)
            if match:
                payload = self.service.snapshot(unquote(match.group(1)))
                self._json(payload, etag='"' + payload["revision"] + '"')
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/events", path)
            if match:
                query = parse_qs(parsed.query, keep_blank_values=False)
                cursors = {key: values[-1] for key, values in query.items() if key in {
                    "chat_after", "proposer_after", "reviewer_after"
                }}
                payload = self.service.events(unquote(match.group(1)), cursors)
                etag = '"' + sha256_text(payload["revision"] + parsed.query)[:24] + '"'
                self._json(payload, etag=etag)
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/turns/([^/]+)", path)
            if match:
                payload = self.service.turn_detail(unquote(match.group(1)), unquote(match.group(2)))
                self._json(payload)
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, ValueError) as exc:
            self._error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._require_csrf()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/meetings":
                store = self.service.start(self._read_json())
                self._json(
                    {
                        "meeting_id": store.meeting.meeting_id,
                        "status": store.meeting.status,
                        "meeting": self.service._public_manifest(store),
                    },
                    status=201,
                )
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/stop", path)
            if match:
                store = self.service.stop(unquote(match.group(1)))
                self._json(
                    {
                        "meeting_id": store.meeting.meeting_id,
                        "status": store.meeting.status,
                        "session_state": store.meeting.session_state,
                    }
                )
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, TmuxError, HarnessError, ValueError) as exc:
            self._error(500, str(exc))


def build_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    service: Optional[MeetingWebService] = None,
) -> RemoteBuddyHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("RemoteBuddy web server only listens on loopback")
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return RemoteBuddyHTTPServer((host, port), service or MeetingWebService())


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the local RemoteBuddy meeting workbench")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    server = build_server(args.host, args.port)
    print(f"RemoteBuddy web workbench: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# Schema v2 is implemented in a separate module so old run files and their
# reader remain untouched.  These aliases keep the public `web.py` entrypoint
# and imports stable while routing all new traffic through the workspace app.
from web_v2 import (  # noqa: E402,F401
    CSRF_HEADER as CSRF_HEADER,
    MeetingWebService as MeetingWebService,
    RemoteBuddyHTTPServer as RemoteBuddyHTTPServer,
    WebError as WebError,
    build_server as build_server,
    main as main,
)


if __name__ == "__main__":
    raise SystemExit(main())
