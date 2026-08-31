from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import MISSING, asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from typing_policy import TYPING_POLICY_VERSION, TYPING_PROFILES


SCHEMA_VERSION = 2
MEETING_MODE = "human-agent-workspace"
MEETING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_EVENT_CONTENT_BYTES = MAX_MESSAGE_BYTES
VALID_STATUSES = {"created", "starting", "running", "completed", "failed", "interrupted"}
VALID_PHASES = {
    "created",
    "starting",
    "agent_thinking",
    "agent_acting",
    "waiting_for_user",
    "completion_proposed",
    "human_control",
    "api_error",
    "cycle_timeout",
    "completed",
    "failed",
    "interrupted",
}
VALID_OWNERS = {"agent", "human", "none"}
VALID_EVENT_KINDS = {"chat", "screen", "score", "action", "control", "state", "error"}
VALID_SCORE_TAGS = {"competent", "incompetent"}
MAX_CHAT_REFERENCES = 32


class MeetingError(RuntimeError):
    pass


class UnsupportedMeetingError(MeetingError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MeetingError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise MeetingError(f"invalid JSONL object at {path}:{line_number}")
            events.append(value)
    return events


@dataclass
class Meeting:
    meeting_id: str
    status: str
    phase: str
    created_at: str
    updated_at: str
    session_name: str
    session_state: str
    pane_id: Optional[str]
    workspace: str
    streams: Dict[str, str]
    model: str
    control_owner: str = "none"
    control_requested: Optional[str] = None
    conversation_id: Optional[str] = None
    last_response_id: Optional[str] = None
    agent_cursor: int = 0
    request_generation: int = 1
    cycle_action_count: int = 0
    cycle_started_at: Optional[str] = None
    pending_action_id: Optional[str] = None
    recovery_required: bool = False
    current_command: str = ""
    current_path: str = ""
    hashes: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    failure: Optional[str] = None
    session_stopped_at: Optional[str] = None
    agent_backend: str = "responses"
    agent_session_id: Optional[str] = None
    agent_turn_id: Optional[str] = None
    agent_protocol_version: Optional[str] = None
    typing_profile: str = "balanced"
    typing_policy_version: str = TYPING_POLICY_VERSION
    schema_version: int = SCHEMA_VERSION
    mode: str = MEETING_MODE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Meeting":
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("mode") != MEETING_MODE:
            raise UnsupportedMeetingError("meeting is not a RemoteBuddy v2 workspace session")
        values: Dict[str, Any] = {}
        for field_name, definition in cls.__dataclass_fields__.items():
            if field_name in payload:
                values[field_name] = payload[field_name]
            elif definition.default is not MISSING:
                values[field_name] = definition.default
            elif definition.default_factory is not MISSING:
                values[field_name] = definition.default_factory()
            else:
                raise KeyError(field_name)
        if values["agent_backend"] not in {"responses", "codex-app-server"}:
            raise ValueError("invalid agent backend")
        if values["typing_profile"] not in TYPING_PROFILES:
            raise ValueError("invalid typing profile")
        return cls(**values)


class MeetingStore:
    """Durable schema-v2 meeting state and one globally ordered event journal."""

    def __init__(self, run_dir: Path, meeting: Meeting):
        self.run_dir = run_dir.resolve()
        self.meeting = meeting
        self.manifest_path = self.run_dir / "meeting.json"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _safe_run_dir(project_root: Path, meeting_id: str, create_runs: bool = True) -> Path:
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise MeetingError("invalid meeting id")
        root = project_root.expanduser().resolve()
        runs = root / "runs"
        if runs.exists() and runs.is_symlink():
            raise MeetingError("runs directory must not be a symlink")
        if create_runs:
            runs.mkdir(parents=True, exist_ok=True)
        run_candidate = runs / meeting_id
        if run_candidate.is_symlink():
            raise MeetingError("meeting directory must not be a symlink")
        run_dir = run_candidate.resolve()
        if root not in run_dir.parents:
            raise MeetingError("meeting directory escapes project root")
        return run_dir

    @classmethod
    def create(
        cls,
        project_root: Path,
        meeting_id: str,
        initial_message: str,
        skill_source: Path,
        session_name: str,
        model: str,
        agent_backend: str = "responses",
        typing_profile: str = "balanced",
    ) -> "MeetingStore":
        if not isinstance(initial_message, str) or not initial_message.strip():
            raise MeetingError("initial message must be non-empty")
        if len(initial_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise MeetingError(f"initial message exceeds {MAX_MESSAGE_BYTES} bytes")
        if agent_backend not in {"responses", "codex-app-server"}:
            raise MeetingError("invalid agent backend")
        if typing_profile not in TYPING_PROFILES:
            raise MeetingError("invalid typing profile")
        skill_source = skill_source.expanduser().resolve()
        if not skill_source.is_file() or skill_source.is_symlink():
            raise MeetingError(f"workspace skill not found: {skill_source}")
        skill_text = skill_source.read_text(encoding="utf-8")
        run_dir = cls._safe_run_dir(project_root, meeting_id)
        if run_dir.exists():
            raise MeetingError(f"meeting already exists: {meeting_id}")

        (run_dir / "workspace").mkdir(parents=True)
        (run_dir / "streams").mkdir()
        runtime_skill = run_dir / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
        runtime_skill.parent.mkdir(parents=True)
        _atomic_write_text(runtime_skill, skill_text)
        _atomic_write_text(run_dir / "streams" / "events.jsonl", "")
        _atomic_write_text(run_dir / "conversation.md", "")

        now = utc_now()
        meeting = Meeting(
            meeting_id=meeting_id,
            status="created",
            phase="created",
            created_at=now,
            updated_at=now,
            session_name=session_name,
            session_state="not-started",
            pane_id=None,
            workspace="workspace",
            streams={"events": "streams/events.jsonl"},
            model=model,
            agent_backend=agent_backend,
            typing_profile=typing_profile,
            cycle_started_at=now,
        )
        store = cls(run_dir, meeting)
        store._write_manifest()
        store.append_event(
            "chat",
            "human",
            "created",
            content=initial_message.strip(),
            message_type="request",
        )
        return store

    @classmethod
    def load(cls, project_root: Path, meeting_id: str) -> "MeetingStore":
        run_dir = cls._safe_run_dir(project_root, meeting_id)
        manifest_path = run_dir / "meeting.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise MeetingError(f"meeting not found: {meeting_id}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            meeting = Meeting.from_dict(payload)
        except UnsupportedMeetingError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MeetingError(f"invalid meeting manifest: {meeting_id}") from exc
        if meeting.meeting_id != meeting_id:
            raise MeetingError("meeting manifest id mismatch")
        store = cls(run_dir, meeting)
        store._contained_path(meeting.workspace)
        for relative in meeting.streams.values():
            store._contained_path(relative)
        return store

    @staticmethod
    def peek_schema(manifest_path: Path) -> Optional[int]:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            return int(payload.get("schema_version", 1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _contained_path(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise MeetingError("meeting path must be relative")
        path = (self.run_dir / relative).resolve()
        if self.run_dir not in path.parents:
            raise MeetingError("meeting path escapes run directory")
        return path

    @property
    def workspace_path(self) -> Path:
        return self._contained_path(self.meeting.workspace)

    @property
    def events_path(self) -> Path:
        return self._contained_path(self.meeting.streams["events"])

    @property
    def stop_request_path(self) -> Path:
        return self.run_dir / "stop.request"

    @property
    def runtime_skill_path(self) -> Path:
        return self.run_dir / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"

    def _write_manifest(self, *, hash_events: bool = True) -> None:
        self.meeting.updated_at = utc_now()
        paths = {
            "conversation.md": self.run_dir / "conversation.md",
            ".agents/skills/workspace-collaborator/SKILL.md": self.runtime_skill_path,
        }
        preserved_screen = self.meeting.hashes.get("latest_screen")
        preserved_events = self.meeting.hashes.get("streams/events.jsonl")
        self.meeting.hashes = {
            relative: sha256_file(path) for relative, path in paths.items() if path.is_file()
        }
        if hash_events and self.events_path.is_file():
            self.meeting.hashes["streams/events.jsonl"] = sha256_file(self.events_path)
        elif preserved_events:
            self.meeting.hashes["streams/events.jsonl"] = preserved_events
        if preserved_screen:
            self.meeting.hashes["latest_screen"] = preserved_screen
        _atomic_write_json(self.manifest_path, self.meeting.to_dict())

    def _last_event_sequence_unlocked(self) -> int:
        if not self.events_path.is_file() or self.events_path.stat().st_size == 0:
            return 0
        with self.events_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            tail = b""
            while position > 0 and tail.count(b"\n") < 2:
                size = min(4096, position)
                position -= size
                handle.seek(position)
                tail = handle.read(size) + tail
        lines = [line for line in tail.splitlines() if line.strip()]
        if not lines:
            return 0
        try:
            return int(json.loads(lines[-1].decode("utf-8"))["seq"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MeetingError("events stream has an invalid final record") from exc

    def _reload_manifest_unlocked(self) -> None:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.meeting = Meeting.from_dict(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MeetingError(f"invalid meeting manifest: {self.meeting.meeting_id}") from exc

    @contextmanager
    def _exclusive_state(self):
        lock_path = self.run_dir / ".state.lock"
        with self._thread_lock, lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self._reload_manifest_unlocked()
            yield

    def refresh(self) -> None:
        refreshed = self.load(self.run_dir.parent.parent, self.meeting.meeting_id)
        self.meeting = refreshed.meeting

    def events(self) -> List[Dict[str, Any]]:
        return read_jsonl(self.events_path)

    def events_after(self, sequence: int) -> List[Dict[str, Any]]:
        return [event for event in self.events() if int(event.get("seq", 0)) > sequence]

    def append_event(
        self,
        kind: str,
        actor: str,
        phase: str,
        *,
        content: str = "",
        message_type: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if kind not in VALID_EVENT_KINDS:
            raise MeetingError(f"invalid event kind: {kind}")
        if len(content.encode("utf-8")) > MAX_EVENT_CONTENT_BYTES:
            raise MeetingError(f"event content exceeds {MAX_EVENT_CONTENT_BYTES} bytes")
        with self._exclusive_state():
            return self._append_event_unlocked(
                kind,
                actor,
                phase,
                content=content,
                message_type=message_type,
                data=data,
            )

    def _append_event_unlocked(
        self,
        kind: str,
        actor: str,
        phase: str,
        *,
        content: str = "",
        message_type: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sequence = self._last_event_sequence_unlocked() + 1
        event = {
            "seq": sequence,
            "event_id": f"event-{sequence:08d}",
            "captured_at": utc_now(),
            "kind": kind,
            "actor": actor,
            "phase": phase,
            "content": content,
            "message_type": message_type,
            "data": data or {},
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if kind == "chat":
            self.render_conversation()
        self._write_manifest(hash_events=kind != "screen")
        return event

    def append_chat(
        self,
        actor: str,
        content: str,
        message_type: str,
        reference: Optional[Dict[str, Any]] = None,
        references: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if actor not in {"human", "agent"}:
            raise MeetingError("chat actor must be human or agent")
        if message_type not in {"request", "ack", "progress", "question", "final"}:
            raise MeetingError("invalid chat message type")
        if not isinstance(content, str) or not content.strip():
            raise MeetingError("chat content must be non-empty")
        # ``reference`` is the v2 single-reference shape.  V6 messages may
        # carry any number of immutable content references, so persist the
        # normalized list while retaining the singular alias for readers that
        # still consume the older journal shape.
        normalized: List[Dict[str, Any]] = []
        legacy_single = references is None and reference is not None
        if reference is not None:
            if not isinstance(reference, Mapping):
                raise MeetingError("chat reference must be an object")
            normalized.append(dict(reference))
        if references is not None:
            if isinstance(references, (str, bytes)):
                raise MeetingError("chat references must be an array")
            for item in references:
                if not isinstance(item, Mapping):
                    raise MeetingError("chat references must contain objects")
                normalized.append(dict(item))
        if len(normalized) > MAX_CHAT_REFERENCES:
            raise MeetingError(f"chat references may contain at most {MAX_CHAT_REFERENCES} items")
        # Deduplicate repeated selections without changing their order.
        unique: List[Dict[str, Any]] = []
        seen = set()
        for item in normalized:
            key = (
                item.get("kind"),
                item.get("event_id"),
                (item.get("region") or {}).get("start") if isinstance(item.get("region"), Mapping) else None,
                (item.get("region") or {}).get("end") if isinstance(item.get("region"), Mapping) else None,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        data: Optional[Dict[str, Any]] = None
        if unique:
            # Keep the exact legacy shape for callers that still pass the
            # singular argument.  The V6 list shape is emitted only when the
            # caller explicitly supplies ``references``.
            data = {"reference": unique[0]} if legacy_single and len(unique) == 1 else {"references": unique}
        return self.append_event(
            "chat", actor, self.meeting.phase, content=content.strip(), message_type=message_type, data=data
        )

    def append_score(
        self,
        tag: str,
        reference: Any = None,
        judge_name: Any = "human",
        score: Optional[float] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an immutable human score without entering the chat flow."""
        # The public shape is ``(tag, reference, judge_name)``.  Keep accepting
        # the earlier ``(tag, judge_name, reference)`` positional order so
        # existing callers can upgrade without rewriting their call sites.
        if isinstance(reference, str) and isinstance(judge_name, Mapping):
            reference, judge_name = judge_name, reference
        if not isinstance(tag, str) or tag not in VALID_SCORE_TAGS:
            raise MeetingError("invalid score tag")
        if not isinstance(judge_name, str) or not judge_name.strip():
            raise MeetingError("judge name must be non-empty")
        if comment is not None:
            if not isinstance(comment, str):
                raise MeetingError("score comment must be text or null")
            comment = comment.strip() or None
            if comment is not None and len(comment.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise MeetingError(f"score comment exceeds {MAX_MESSAGE_BYTES} bytes")
        if not isinstance(reference, Mapping):
            raise MeetingError("score reference must be an object")
        reference = dict(reference)
        source_event = next(
            (event for event in self.events() if event.get("event_id") == reference.get("event_id")),
            None,
        )
        if source_event is None:
            raise MeetingError("score reference source event was not found")
        try:
            # V6 is the current reference contract; the V5 projection still
            # remains available for replaying older journals.
            from meetingV6 import reference_from_event

            reference = reference_from_event(source_event, reference)
        except (TypeError, ValueError) as exc:
            raise MeetingError(str(exc)) from exc
        # V5 scores are categorical annotations.  Keep the field for forward
        # compatibility, but never trust or persist a caller-provided value.
        score = None
        return self.append_event(
            "score",
            "human",
            self.meeting.phase,
            data={
                "tag": tag,
                "score": score,
                "judge_name": judge_name.strip(),
                "reference": dict(reference),
                "comment": comment,
            },
        )

    def append_screen(
        self,
        content: str,
        current_command: str,
        current_path: str,
        phase: Optional[str] = None,
        change_kind: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._exclusive_state():
            metadata_changed = (
                self.meeting.current_command != current_command
                or self.meeting.current_path != current_path
            )
            self.meeting.current_command = current_command
            self.meeting.current_path = current_path
            if not content.strip():
                if metadata_changed:
                    self._write_manifest()
                return None
            digest = sha256_text(content)
            cursor_state = {
                key: metadata[key]
                for key in ("cursor_x", "cursor_y", "pane_width", "pane_height")
                if metadata and key in metadata
            }
            state_digest = digest
            if cursor_state:
                state_digest = sha256_text(
                    digest + "\n" + json.dumps(cursor_state, sort_keys=True, separators=(",", ":"))
                )
            if self.meeting.hashes.get("latest_screen") == state_digest:
                if metadata_changed:
                    self._write_manifest()
                return None
            self.meeting.hashes["latest_screen"] = state_digest
            data = {
                "content_sha256": digest,
                "pane_id": self.meeting.pane_id,
                "current_command": current_command,
                "current_path": current_path,
            }
            if change_kind:
                data["change_kind"] = change_kind
            if metadata:
                reserved = {"content_sha256", "pane_id", "current_command", "current_path"}
                if reserved.intersection(metadata):
                    raise MeetingError("screen metadata contains reserved fields")
                data.update(metadata)
            return self._append_event_unlocked(
                "screen",
                "workspace",
                phase or self.meeting.phase,
                content=content,
                data=data,
            )

    def latest_screen(self) -> Optional[Dict[str, Any]]:
        for event in reversed(self.events()):
            if event.get("kind") == "screen":
                return event
        return None

    def chat_messages(self) -> List[Dict[str, Any]]:
        return [event for event in self.events() if event.get("kind") == "chat"]

    def score_messages(self) -> List[Dict[str, Any]]:
        return [event for event in self.events() if event.get("kind") == "score"]

    # Keep query naming discoverable for callers that use the event-oriented
    # terminology elsewhere in the store API.
    def score_events(self) -> List[Dict[str, Any]]:
        return self.score_messages()

    def scores(self) -> List[Dict[str, Any]]:
        """Short alias for callers that treat scores as a stream."""
        return self.score_messages()

    def render_conversation(self, events: Optional[List[Dict[str, Any]]] = None) -> None:
        source = events if events is not None else self.events()
        lines = [f"# RemoteBuddy Session: {self.meeting.meeting_id}", ""]
        for event in source:
            if event.get("kind") != "chat":
                continue
            actor = "You" if event.get("actor") == "human" else "Agent"
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            references: List[Mapping[str, Any]] = []
            raw_references = data.get("references")
            if isinstance(raw_references, list):
                references.extend(item for item in raw_references if isinstance(item, Mapping))
            elif isinstance(data.get("reference"), Mapping):
                references.append(data["reference"])
            # A journal written by an older client can contain both aliases;
            # collapse those duplicates when rebuilding the markdown view.
            if isinstance(data.get("reference"), Mapping) and not any(
                item == data["reference"] for item in references
            ):
                references.append(data["reference"])
            reference_lines: List[str] = []
            for reference in references:
                reference_lines.extend(
                    [
                        "",
                        f"> Reference: {reference.get('kind')} · {reference.get('event_id')}",
                        f"> {reference.get('excerpt', '')}",
                    ]
                )
            lines.extend(
                [
                    f"## {actor} · {event.get('message_type') or 'message'}",
                    "",
                    str(event.get("content", "")),
                    *reference_lines,
                    "",
                ]
            )
        _atomic_write_text(self.run_dir / "conversation.md", "\n".join(lines).rstrip() + "\n")

    def update(self, **changes: Any) -> None:
        with self._exclusive_state():
            for key, value in changes.items():
                if key not in self.meeting.__dataclass_fields__:
                    raise MeetingError(f"unknown meeting field: {key}")
                if key == "status" and value not in VALID_STATUSES:
                    raise MeetingError(f"invalid meeting status: {value}")
                if key == "phase" and value not in VALID_PHASES:
                    raise MeetingError(f"invalid meeting phase: {value}")
                if key == "control_owner" and value not in VALID_OWNERS:
                    raise MeetingError(f"invalid control owner: {value}")
                if key == "typing_profile" and value not in TYPING_PROFILES:
                    raise MeetingError("invalid typing profile")
                setattr(self.meeting, key, value)
            self._write_manifest()

    def set_session(self, pane_id: str) -> None:
        self.update(pane_id=pane_id, session_state="running")

    def request_stop(self) -> None:
        _atomic_write_text(self.stop_request_path, utc_now() + "\n")

    def stop_requested(self) -> bool:
        return self.stop_request_path.is_file()

    def mark_session_stopped(self) -> None:
        self.update(session_state="stopped", session_stopped_at=utc_now())

    def event_count(self) -> int:
        return len(self.events())
