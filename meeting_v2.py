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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from typing_policy import TYPING_POLICY_VERSION, TYPING_PROFILES
from references import reference_from_event


SCHEMA_VERSION = 2
MEETING_MODE = "human-agent-workspace"
MEETING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_EVENT_CONTENT_BYTES = MAX_MESSAGE_BYTES
VALID_STATUSES = {"created", "starting", "running", "completed", "failed", "interrupted"}
TERMINAL_MEETING_STATUSES = {"completed", "failed", "interrupted"}
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
# Agent roles follow the Meeting ADT (agent_name2role). "human" is reserved
# and never appears as an agent name. A meeting always has exactly one
# generator agent that owns the shared terminal (multi-agent slices keep
# this invariant; additional agents join as discriminators).
VALID_AGENT_ROLES = {"generator", "active_discriminator", "passive_discriminator"}
HUMAN_USER = "human"
DEFAULT_AGENT_NAME = "agent"
VALID_EVENT_KINDS = {"chat", "screen", "score", "action", "control", "state", "error"}
VALID_SCORE_TAGS = {"competent", "incompetent"}
VALID_AGENT_BACKENDS = {"responses", "codex-app-server", "claude-code", "trae-agent"}
VALID_AGENT_PROVIDERS = {"openai", "deepseek"}
# Mirrors the effort levels accepted by model_api_client; None on a meeting
# means "follow the config.toml default".
VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}
MAX_CHAT_REFERENCES = 32


class MeetingError(RuntimeError):
    pass


class UnsupportedMeetingError(MeetingError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- agent identity helpers -------------------------------------------------

def normalize_agent_role_map(
    role_map: Optional[Mapping[str, str]],
    *,
    require_generator: bool = True,
) -> Dict[str, str]:
    """Validate and normalize the ADT agent_name2role map.

    Rules:
    - keys are non-empty agent names; ``"human"`` is reserved and may not be
      an agent name;
    - values must be a member of VALID_AGENT_ROLES;
    - a meeting keeps at least one ``generator`` (the shared-terminal owner).
    An empty/None map falls back to the legacy single agent
    ``{DEFAULT_AGENT_NAME: "generator"}`` so pre-identity meetings behave
    exactly as before.
    """
    if not role_map:
        return {DEFAULT_AGENT_NAME: "generator"}
    if not isinstance(role_map, Mapping):
        raise MeetingError("agent_name2role must be a mapping of name -> role")
    normalized: Dict[str, str] = {}
    for name, role in role_map.items():
        if not isinstance(name, str) or not name.strip():
            raise MeetingError("agent names must be non-empty strings")
        if name == HUMAN_USER:
            raise MeetingError('"human" is reserved and cannot be an agent name')
        if not isinstance(role, str) or role not in VALID_AGENT_ROLES:
            raise MeetingError(f"invalid agent role {role!r} for {name!r}")
        normalized[name.strip()] = role
    if require_generator and "generator" not in set(normalized.values()):
        raise MeetingError("meeting requires exactly one generator agent")
    return normalized


# -- @-mention (ADT: $at_user_name) ---------------------------------------

_AT_NAME_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_.-]*)")


def mentioned_agent_names(content: str, role_map: Mapping[str, str]) -> list:
    """Names mentioned with a leading ``@`` that are members of role_map.

    Order follows the message text; duplicates are removed. ``@human`` or an
    unknown name is simply not a mention of any member.
    """
    if not isinstance(content, str):
        return []
    seen = set()
    result: list = []
    for match in _AT_NAME_RE.finditer(content):
        name = match.group(1)
        if name in role_map and name not in seen:
            seen.add(name)
            result.append(name)
    return result


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
        lines = handle.readlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if line_number == len(lines) and not line.endswith("\n"):
                # The writer terminates every record with a newline, so a torn
                # final line is a concurrent append in progress, not corruption.
                break
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
    agent_provider: str = "openai"
    agent_session_id: Optional[str] = None
    agent_turn_id: Optional[str] = None
    agent_protocol_version: Optional[str] = None
    typing_profile: str = "balanced"
    typing_policy_version: str = TYPING_POLICY_VERSION
    agent_ordinal: int = 0
    # ADT agent_name2role: single source of who is in the meeting and with
    # which role. Append-only at join time; see MeetingStore.add_agent_role.
    # The legacy default ({DEFAULT_AGENT_NAME: "generator"}) keeps every
    # pre-identity meeting behaving exactly as before.
    agent_name2role: Dict[str, str] = field(default_factory=dict)
    reasoning_effort: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    mode: str = MEETING_MODE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def effective_role_map(self) -> Dict[str, str]:
        """agent_name2role with the legacy fallback applied when empty."""
        return self.agent_name2role or {DEFAULT_AGENT_NAME: "generator"}

    def agent_names(self) -> Set[str]:
        return set(self.effective_role_map)

    def role_of(self, agent_name: str) -> Optional[str]:
        return self.effective_role_map.get(agent_name)

    def is_agent_name(self, agent_name: str) -> bool:
        return agent_name in self.agent_names()

    def generator_name(self) -> str:
        """Name of the generator agent (the shared-terminal owner/worker).

        Today every meeting has exactly one generator; the default role map
        names it DEFAULT_AGENT_NAME ("agent"), keeping legacy behavior.
        """
        for name, role in self.effective_role_map.items():
            if role == "generator":
                return name
        return DEFAULT_AGENT_NAME  # unreachable under validation; safe fallback

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
        if values["agent_backend"] not in VALID_AGENT_BACKENDS:
            raise ValueError("invalid agent backend")
        if values["agent_provider"] not in VALID_AGENT_PROVIDERS:
            raise ValueError("invalid agent provider")
        if values["agent_backend"] == "claude-code" and values["agent_provider"] != "deepseek":
            raise ValueError("claude-code requires deepseek provider")
        if values["agent_backend"] == "trae-agent" and values["agent_provider"] != "openai":
            raise ValueError("trae-agent requires openai provider")
        if values["typing_profile"] not in TYPING_PROFILES:
            raise ValueError("invalid typing profile")
        if values["reasoning_effort"] is not None and values["reasoning_effort"] not in VALID_REASONING_EFFORTS:
            raise ValueError("invalid reasoning effort")
        return cls(**values)


class MeetingStore:
    """Durable schema-v2 meeting state and one globally ordered event journal."""

    def __init__(self, run_dir: Path, meeting: Meeting):
        self.run_dir = run_dir.resolve()
        self.meeting = meeting
        self.manifest_path = self.run_dir / "meeting.json"
        self._thread_lock = threading.RLock()
        self._state_depth = 0
        self._events_cache: List[Dict[str, Any]] = []
        self._events_cache_signature: Optional[tuple] = None
        self._events_cache_offset = 0

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
        agent_provider: str = "openai",
        agent_ordinal: int = 0,
        reasoning_effort: Optional[str] = None,
        agent_name2role: Optional[Mapping[str, str]] = None,
    ) -> "MeetingStore":
        if not isinstance(initial_message, str) or not initial_message.strip():
            raise MeetingError("initial message must be non-empty")
        if len(initial_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise MeetingError(f"initial message exceeds {MAX_MESSAGE_BYTES} bytes")
        roles = normalize_agent_role_map(agent_name2role)
        if agent_backend not in VALID_AGENT_BACKENDS:
            raise MeetingError("invalid agent backend")
        if agent_provider not in VALID_AGENT_PROVIDERS:
            raise MeetingError("invalid agent provider")
        if agent_backend == "claude-code" and agent_provider != "deepseek":
            raise MeetingError("claude-code requires deepseek provider")
        if agent_backend == "trae-agent" and agent_provider != "openai":
            raise MeetingError("trae-agent requires openai provider")
        if typing_profile not in TYPING_PROFILES:
            raise MeetingError("invalid typing profile")
        if not isinstance(agent_ordinal, int) or isinstance(agent_ordinal, bool) or agent_ordinal < 0:
            raise MeetingError("invalid agent ordinal")
        if reasoning_effort is not None and reasoning_effort not in VALID_REASONING_EFFORTS:
            raise MeetingError("invalid reasoning effort")
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
            agent_provider=agent_provider,
            typing_profile=typing_profile,
            agent_ordinal=agent_ordinal,
            agent_name2role=roles,
            reasoning_effort=reasoning_effort,
            cycle_started_at=now,
        )
        store = cls(run_dir, meeting)
        store._write_manifest(hash_events=True)
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
        # Legacy manifests have no role map: fall back to the single-agent
        # default so pre-identity meetings keep their exact behavior.
        if not meeting.agent_name2role:
            meeting.agent_name2role = {DEFAULT_AGENT_NAME: "generator"}
        else:
            meeting.agent_name2role = normalize_agent_role_map(meeting.agent_name2role)
        store = cls(run_dir, meeting)
        store._contained_path(meeting.workspace)
        for relative in meeting.streams.values():
            store._contained_path(relative)
        return store

    def add_agent_role(self, agent_name: str, role: str) -> "Meeting":
        """Join an agent to the meeting (ADT: role map is append-only).

        Rules:
        - ``agent_name`` must be non-empty and never ``"human"``;
        - ``role`` must be a member of VALID_AGENT_ROLES;
        - an existing agent's role is immutable: re-joining with the same
          role is a no-op, re-joining with a different role raises.
        """
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise MeetingError("agent name must be non-empty")
        if agent_name == HUMAN_USER:
            raise MeetingError('"human" is reserved and cannot be an agent name')
        if not isinstance(role, str) or role not in VALID_AGENT_ROLES:
            raise MeetingError(f"invalid agent role {role!r}")
        with self._exclusive_state():
            current = self.meeting.effective_role_map
            existing = current.get(agent_name)
            if existing is not None:
                if existing != role:
                    raise MeetingError(
                        f"role of agent {agent_name!r} is immutable (already {existing!r})"
                    )
                return self.meeting
            self.meeting.agent_name2role = {**current, agent_name: role}
            self._write_manifest()
        return self.meeting

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

    def _write_manifest(self, *, hash_events: bool = False) -> None:
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
        with self._thread_lock:
            if self._state_depth:
                # flock is per open file description, so a nested acquisition
                # in the same thread would deadlock on a second open. The
                # outer context already holds the exclusive lock; run nested
                # blocks inside it without reloading over in-flight changes.
                self._state_depth += 1
                try:
                    yield
                finally:
                    self._state_depth -= 1
                return
            lock_path = self.run_dir / ".state.lock"
            with lock_path.open("a+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                self._state_depth = 1
                try:
                    self._reload_manifest_unlocked()
                    yield
                finally:
                    self._state_depth = 0

    def refresh(self) -> None:
        """Reload the manifest in place without rebuilding the whole store."""
        with self._thread_lock:
            self._reload_manifest_unlocked()

    def events(self) -> List[Dict[str, Any]]:
        with self._thread_lock:
            return list(self._cached_events())

    def _cached_events(self) -> List[Dict[str, Any]]:
        """Parse only appended journal bytes; the journal is append-only.

        The cache is invalidated by (size, mtime_ns), so appends from other
        processes are picked up. A torn final line (concurrent append in
        progress) is left unparsed and re-checked on the next call.
        """
        path = self.events_path
        try:
            status = path.stat()
        except OSError:
            self._events_cache = []
            self._events_cache_signature = None
            self._events_cache_offset = 0
            return self._events_cache
        signature = (status.st_size, status.st_mtime_ns)
        if signature == self._events_cache_signature:
            return self._events_cache
        if status.st_size < self._events_cache_offset:
            self._events_cache = []
            self._events_cache_offset = 0
        with path.open("rb") as handle:
            handle.seek(self._events_cache_offset)
            chunk = handle.read()
        last_newline = chunk.rfind(b"\n")
        complete = chunk[: last_newline + 1] if last_newline >= 0 else b""
        for raw_line in complete.splitlines():
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._events_cache = []
                self._events_cache_signature = None
                self._events_cache_offset = 0
                raise MeetingError(f"invalid JSONL at {path}") from exc
            if not isinstance(value, dict):
                self._events_cache = []
                self._events_cache_signature = None
                self._events_cache_offset = 0
                raise MeetingError(f"invalid JSONL object at {path}")
            self._events_cache.append(value)
        self._events_cache_offset += len(complete)
        # Mark the cache clean only when the whole file was consumed; a torn
        # tail must be re-read once the writer finishes the line.
        self._events_cache_signature = (
            signature if self._events_cache_offset == status.st_size else None
        )
        return self._events_cache

    def last_event_sequence(self) -> int:
        """Return the tail sequence with a 4 KB read instead of a full parse."""
        with self._thread_lock:
            return self._last_event_sequence_unlocked()

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
        # Hashing the whole journal per append is O(file size) and grows with
        # the session; the integrity hash is refreshed on terminal updates.
        self._write_manifest()
        return event

    def append_chat(
        self,
        actor: str,
        content: str,
        message_type: str,
        reference: Optional[Dict[str, Any]] = None,
        references: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not (actor == HUMAN_USER or actor in self.meeting.agent_names()):
            raise MeetingError("chat actor must be human or a meeting agent")
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
        # Scores judge the Agent's observable work. A human-authored chat
        # message must never be scoreable — neither by the human who wrote it
        # nor by anyone else — while screen output and Agent chat remain fair
        # targets.
        if source_event.get("kind") == "chat" and source_event.get("actor") == "human":
            raise MeetingError("human chat messages cannot be scored")
        try:
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

    def note_terminal_control(self, user_name: str) -> None:
        """Record that ``user_name`` started controlling the shared pane.

        ADT TerminalControl: there is no end_time -- the next controller's
        start_time marks the boundary. Stored as a ``control`` event so the
        Agent-visible journal shape does not grow new kinds.
        """
        # ADT terminal_capable: only the human or the generator agent may
        # own the shared pane; discriminators never hold the terminal.
        if not (user_name == HUMAN_USER or self.meeting.role_of(user_name) == "generator"):
            raise MeetingError("terminal control user_name must be human or the generator agent")
        pane = self.meeting.pane_id or ""
        digits = pane.lstrip("%")
        tmux_pane: Optional[int] = int(digits) if digits.isdigit() else None
        self.append_event(
            "control",
            user_name,
            self.meeting.phase,
            data={
                "status": "terminal_control",
                "user_name": user_name,
                "tmux_window_name": self.meeting.session_name,
                "tmux_pane": tmux_pane,
                "start_time": utc_now(),
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
        segments: Optional[list] = None,
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
            if segments is not None:
                # Color payload for the frontend (ScreenCapture
                # terminal_text_content). ``content`` stays the plain
                # projection (screen_data) the model consumes; ``ansi`` is
                # the derived SGR encoding a terminal emulator can write
                # directly.
                from terminal_text import encode_segments

                data["segments"] = segments
                data["ansi"] = encode_segments(segments)
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
            self._apply_changes_unlocked(changes)
            self._write_manifest(hash_events=changes.get("status") in TERMINAL_MEETING_STATUSES)

    def bump_request_generation(self, **changes: Any) -> int:
        """Atomically increment request_generation together with other changes.

        The new value is computed after the manifest reload inside the state
        lock, so a concurrent process can never make an increment vanish —
        important because request_generation is the interrupt mechanism.
        """
        with self._exclusive_state():
            value = self.meeting.request_generation + 1
            self.meeting.request_generation = value
            self._apply_changes_unlocked(changes)
            self._write_manifest(hash_events=changes.get("status") in TERMINAL_MEETING_STATUSES)
            return value

    def _apply_changes_unlocked(self, changes: Mapping[str, Any]) -> None:
        for key, value in changes.items():
            if key not in self.meeting.__dataclass_fields__:
                raise MeetingError(f"unknown meeting field: {key}")
            if key == "status" and value not in VALID_STATUSES:
                raise MeetingError(f"invalid meeting status: {value}")
            if key == "phase" and value not in VALID_PHASES:
                raise MeetingError(f"invalid meeting phase: {value}")
            if key == "control_owner" and not (
                value in {"human", "none"} or self.meeting.is_agent_name(value)
            ):
                raise MeetingError(f"invalid control owner: {value}")
            if key == "typing_profile" and value not in TYPING_PROFILES:
                raise MeetingError("invalid typing profile")
            if key == "reasoning_effort" and value is not None and value not in VALID_REASONING_EFFORTS:
                raise MeetingError("invalid reasoning effort")
            setattr(self.meeting, key, value)

    def set_session(self, pane_id: str) -> None:
        self.update(pane_id=pane_id, session_state="running")

    def request_stop(self) -> None:
        _atomic_write_text(self.stop_request_path, utc_now() + "\n")

    def stop_requested(self) -> bool:
        return self.stop_request_path.is_file()

    def mark_session_stopped(self) -> None:
        self.update(session_state="stopped", session_stopped_at=utc_now())

    def event_count(self) -> int:
        # seq is assigned monotonically from 1, so the tail sequence equals
        # the count without parsing the journal.
        return self.last_event_sequence()
