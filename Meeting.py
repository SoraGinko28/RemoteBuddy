from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
TURN_COMPLETE_MARKER = "<!-- REMOTEBUDDY_TURN_COMPLETE -->"
MAX_BRIEF_BYTES = 1024 * 1024
MAX_TURN_BYTES = 64 * 1024
MEETING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


class MeetingError(RuntimeError):
    pass


class InvalidTurnError(MeetingError):
    pass


class IntegrityError(MeetingError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise MeetingError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return events


@dataclass
class Participant:
    participant_id: str
    role: str
    pane_id: Optional[str] = None


@dataclass
class TurnRecord:
    turn_id: str
    speaker: str
    objective: str
    output_path: str
    status: str = "pending"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    chat_event_id: Optional[str] = None


@dataclass
class ChatMessage:
    event_id: str
    turn_id: str
    speaker: str
    content: str
    captured_at: str
    source_path: str
    source_sha256: str
    stream: str = "chat"


@dataclass
class ScreenCapture:
    event_id: str
    participant: str
    pane_id: str
    content: str
    captured_at: str
    content_sha256: str
    previous_sha256: Optional[str]
    phase: str
    stream: str


@dataclass
class Meeting:
    meeting_id: str
    status: str
    created_at: str
    updated_at: str
    session_name: str
    session_state: str
    brief: Dict[str, Any]
    participants: Dict[str, Participant]
    streams: Dict[str, str]
    turns: List[TurnRecord]
    settings: Dict[str, Any]
    hashes: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    failure: Optional[str] = None
    session_stopped_at: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["participants"] = {
            key: asdict(value) for key, value in self.participants.items()
        }
        payload["turns"] = [asdict(value) for value in self.turns]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Meeting":
        participants = {
            key: Participant(**value)
            for key, value in payload.get("participants", {}).items()
        }
        turns = [TurnRecord(**value) for value in payload.get("turns", [])]
        return cls(
            meeting_id=payload["meeting_id"],
            status=payload["status"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            session_name=payload["session_name"],
            session_state=payload.get("session_state", "unknown"),
            brief=dict(payload["brief"]),
            participants=participants,
            streams=dict(payload["streams"]),
            turns=turns,
            settings=dict(payload.get("settings", {})),
            hashes=dict(payload.get("hashes", {})),
            warnings=list(payload.get("warnings", [])),
            failure=payload.get("failure"),
            session_stopped_at=payload.get("session_stopped_at"),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
        )


class MeetingStore:
    """Owns the durable, filesystem-backed representation of one meeting."""

    def __init__(self, run_dir: Path, meeting: Meeting):
        self.run_dir = run_dir.resolve()
        self.meeting = meeting
        self.manifest_path = self.run_dir / "meeting.json"
        self._screen_hashes: Dict[str, Optional[str]] = {}
        self._screen_sequences: Dict[str, int] = {}

    @staticmethod
    def _safe_run_dir(project_root: Path, meeting_id: str) -> Path:
        """Resolve a run directory without following a runs symlink outside the project."""
        root = project_root.expanduser().resolve()
        runs = root / "runs"
        if runs.exists() and runs.is_symlink():
            raise MeetingError("runs directory must not be a symlink")
        runs.mkdir(parents=True, exist_ok=True)
        run_dir = (runs / meeting_id).resolve()
        if root not in run_dir.parents:
            raise MeetingError("meeting directory escapes project root")
        return run_dir

    @staticmethod
    def _validate_settings(turn_timeout: float, capture_interval: float) -> None:
        import math

        if not math.isfinite(float(turn_timeout)) or float(turn_timeout) <= 0:
            raise MeetingError("turn timeout must be a finite positive number")
        if not math.isfinite(float(capture_interval)) or float(capture_interval) <= 0:
            raise MeetingError("capture interval must be a finite positive number")

    @classmethod
    def create(
        cls,
        project_root: Path,
        meeting_id: str,
        brief_source: Path,
        skill_source: Path,
        session_name: str,
        turn_specs: Sequence[Tuple[str, str, str]],
        model: Optional[str],
        turn_timeout: float,
        capture_interval: float,
    ) -> "MeetingStore":
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise MeetingError(
                "meeting id must use lowercase letters, digits, and hyphens (max 48 chars)"
            )
        cls._validate_settings(turn_timeout, capture_interval)

        brief_source = brief_source.expanduser().resolve()
        skill_source = skill_source.expanduser().resolve()
        if not brief_source.is_file():
            raise MeetingError(f"brief is not a file: {brief_source}")
        if brief_source.stat().st_size > MAX_BRIEF_BYTES:
            raise MeetingError(f"brief exceeds {MAX_BRIEF_BYTES} bytes")
        if not skill_source.is_file():
            raise MeetingError(f"meeting skill not found: {skill_source}")

        try:
            brief_text = brief_source.read_text(encoding="utf-8")
            skill_text = skill_source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MeetingError("brief and skill files must be UTF-8 text") from exc

        return cls._create_with_text(
            project_root,
            meeting_id,
            brief_text,
            str(brief_source),
            skill_text,
            session_name,
            turn_specs,
            model,
            turn_timeout,
            capture_interval,
        )

    @classmethod
    def create_from_text(
        cls,
        project_root: Path,
        meeting_id: str,
        brief_text: str,
        skill_source: Path,
        session_name: str,
        turn_specs: Sequence[Tuple[str, str, str]],
        model: Optional[str],
        turn_timeout: float,
        capture_interval: float,
    ) -> "MeetingStore":
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise MeetingError(
                "meeting id must use lowercase letters, digits, and hyphens (max 48 chars)"
            )
        cls._validate_settings(turn_timeout, capture_interval)
        if not isinstance(brief_text, str) or not brief_text.strip():
            raise MeetingError("brief must be a non-empty UTF-8 text string")
        try:
            brief_bytes = brief_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MeetingError("brief must be UTF-8 text") from exc
        if len(brief_bytes) > MAX_BRIEF_BYTES:
            raise MeetingError(f"brief exceeds {MAX_BRIEF_BYTES} bytes")
        skill_source = skill_source.expanduser().resolve()
        if not skill_source.is_file():
            raise MeetingError(f"meeting skill not found: {skill_source}")
        try:
            skill_text = skill_source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MeetingError("meeting skill file must be UTF-8 text") from exc
        return cls._create_with_text(
            project_root,
            meeting_id,
            brief_text,
            "<web brief>",
            skill_text,
            session_name,
            turn_specs,
            model,
            turn_timeout,
            capture_interval,
        )

    @classmethod
    def _create_with_text(
        cls,
        project_root: Path,
        meeting_id: str,
        brief_text: str,
        brief_source_label: str,
        skill_text: str,
        session_name: str,
        turn_specs: Sequence[Tuple[str, str, str]],
        model: Optional[str],
        turn_timeout: float,
        capture_interval: float,
    ) -> "MeetingStore":
        run_dir = cls._safe_run_dir(project_root, meeting_id)
        if run_dir.exists():
            raise MeetingError(f"meeting already exists: {meeting_id}")

        (run_dir / "turns").mkdir(parents=True)
        (run_dir / "streams").mkdir()
        runtime_skill = run_dir / ".agents" / "skills" / "meeting-participant" / "SKILL.md"
        runtime_skill.parent.mkdir(parents=True)

        _atomic_write_text(run_dir / "brief.md", brief_text)
        _atomic_write_text(runtime_skill, skill_text)
        for stream_path in (
            run_dir / "streams" / "chat.jsonl",
            run_dir / "streams" / "screen.proposer.jsonl",
            run_dir / "streams" / "screen.reviewer.jsonl",
        ):
            _atomic_write_text(stream_path, "")

        now = utc_now()
        turns = [
            TurnRecord(
                turn_id=turn_id,
                speaker=speaker,
                objective=objective,
                output_path=f"turns/{turn_id}.md",
            )
            for turn_id, speaker, objective in turn_specs
        ]
        meeting = Meeting(
            meeting_id=meeting_id,
            status="created",
            created_at=now,
            updated_at=now,
            session_name=session_name,
            session_state="not-started",
            brief={
                "path": "brief.md",
                "source_path": brief_source_label,
                "sha256": sha256_text(brief_text),
            },
            participants={
                "proposer": Participant("proposer", "proposer"),
                "reviewer": Participant("reviewer", "reviewer"),
            },
            streams={
                "chat": "streams/chat.jsonl",
                "screen:proposer": "streams/screen.proposer.jsonl",
                "screen:reviewer": "streams/screen.reviewer.jsonl",
            },
            turns=turns,
            settings={
                "model": model,
                "turn_timeout_seconds": turn_timeout,
                "capture_interval_seconds": capture_interval,
            },
            hashes={
                "brief.md": sha256_text(brief_text),
                ".agents/skills/meeting-participant/SKILL.md": sha256_text(skill_text),
                "streams/chat.jsonl": sha256_file(run_dir / "streams" / "chat.jsonl"),
                "streams/screen.proposer.jsonl": sha256_file(
                    run_dir / "streams" / "screen.proposer.jsonl"
                ),
                "streams/screen.reviewer.jsonl": sha256_file(
                    run_dir / "streams" / "screen.reviewer.jsonl"
                ),
            },
        )
        store = cls(run_dir, meeting)
        store.render_conversation()
        store._write_manifest()
        return store

    @classmethod
    def load(cls, project_root: Path, meeting_id: str) -> "MeetingStore":
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise MeetingError(f"invalid meeting id: {meeting_id}")
        run_dir = cls._safe_run_dir(project_root, meeting_id)
        manifest_path = run_dir / "meeting.json"
        if not manifest_path.is_file():
            raise MeetingError(f"meeting not found: {meeting_id}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeetingError(f"invalid meeting manifest: {manifest_path}") from exc
        try:
            meeting = Meeting.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise MeetingError(f"invalid meeting manifest structure: {manifest_path}") from exc
        if meeting.meeting_id != meeting_id:
            raise MeetingError("meeting manifest id does not match requested id")
        if meeting.schema_version != SCHEMA_VERSION:
            raise MeetingError(
                f"unsupported meeting schema {meeting.schema_version}; expected {SCHEMA_VERSION}"
            )
        store = cls(run_dir, meeting)
        # Manifest-controlled paths are always relative files inside this run.
        for relative in meeting.streams.values():
            path = (run_dir / relative).resolve()
            if run_dir not in path.parents:
                raise MeetingError("meeting stream path escapes meeting directory")
        for turn in meeting.turns:
            store.turn_path(turn.turn_id)
        return store

    def _write_manifest(self) -> None:
        self.meeting.updated_at = utc_now()
        hash_paths = {
            "brief.md": self.run_dir / "brief.md",
            ".agents/skills/meeting-participant/SKILL.md": self.run_dir
            / ".agents"
            / "skills"
            / "meeting-participant"
            / "SKILL.md",
            "streams/chat.jsonl": self.stream_path("chat"),
            "streams/screen.proposer.jsonl": self.stream_path("screen:proposer"),
            "streams/screen.reviewer.jsonl": self.stream_path("screen:reviewer"),
        }
        for turn in self.meeting.turns:
            turn_file = self.run_dir / turn.output_path
            if turn_file.is_file():
                hash_paths[turn.output_path] = turn_file
        self.meeting.hashes = {
            relative: sha256_file(path)
            for relative, path in hash_paths.items()
            if path.is_file()
        }
        _atomic_write_json(self.manifest_path, self.meeting.to_dict())

    def stream_path(self, stream_name: str) -> Path:
        try:
            relative = self.meeting.streams[stream_name]
        except KeyError as exc:
            raise MeetingError(f"unknown stream: {stream_name}") from exc
        path = (self.run_dir / relative).resolve()
        if self.run_dir not in path.parents:
            raise MeetingError("meeting stream path escapes meeting directory")
        return path

    def turn(self, turn_id: str) -> TurnRecord:
        for turn in self.meeting.turns:
            if turn.turn_id == turn_id:
                return turn
        raise MeetingError(f"unknown turn: {turn_id}")

    def turn_path(self, turn_id: str) -> Path:
        path = (self.run_dir / self.turn(turn_id).output_path).resolve()
        if self.run_dir not in path.parents:
            raise MeetingError(f"turn path escapes meeting directory: {path}")
        return path

    def set_panes(self, panes: Dict[str, str]) -> None:
        expected = set(self.meeting.participants)
        if set(panes) != expected:
            raise MeetingError(
                "pane mapping must contain exactly: " + ", ".join(sorted(expected))
            )
        for participant, pane_id in panes.items():
            self.meeting.participants[participant].pane_id = pane_id
        self.meeting.session_state = "running"
        self._write_manifest()

    def set_status(self, status: str, failure: Optional[str] = None) -> None:
        if status not in {"created", "running", "completed", "failed", "interrupted"}:
            raise MeetingError(f"invalid meeting status: {status}")
        self.meeting.status = status
        self.meeting.failure = failure
        self._write_manifest()

    def refresh(self) -> None:
        """Reload state written by another CLI process, such as `stop`."""
        refreshed = self.load(self.run_dir.parent.parent, self.meeting.meeting_id)
        self.meeting = refreshed.meeting

    def mark_session_retained(self) -> None:
        if self.meeting.session_state != "stopped":
            self.meeting.session_state = "retained"
            self._write_manifest()

    def mark_session_stopped(self) -> None:
        self.meeting.session_state = "stopped"
        self.meeting.session_stopped_at = utc_now()
        if self.meeting.status in {"created", "running"}:
            self.meeting.status = "interrupted"
            self.meeting.failure = "stopped by user"
        self._write_manifest()

    @property
    def stop_request_path(self) -> Path:
        return self.run_dir / "stop.request"

    def request_stop(self) -> None:
        _atomic_write_text(self.stop_request_path, utc_now() + "\n")

    def stop_requested(self) -> bool:
        return self.stop_request_path.is_file()

    def start_turn(self, turn_id: str) -> None:
        turn = self.turn(turn_id)
        if turn.status != "pending":
            raise MeetingError(f"turn {turn_id} is {turn.status}, expected pending")
        if self.turn_path(turn_id).exists():
            raise MeetingError(f"turn output already exists: {turn_id}")
        turn_index = self.meeting.turns.index(turn)
        incomplete = [
            previous.turn_id
            for previous in self.meeting.turns[:turn_index]
            if previous.status != "completed"
        ]
        if incomplete:
            raise MeetingError(
                f"turn {turn_id} cannot start before: {', '.join(incomplete)}"
            )
        turn.status = "running"
        turn.started_at = utc_now()
        self._write_manifest()

    def complete_turn(self, turn_id: str, chat_event_id: str) -> None:
        turn = self.turn(turn_id)
        if turn.status != "running":
            raise MeetingError(f"turn {turn_id} is {turn.status}, expected running")
        turn.status = "completed"
        turn.completed_at = utc_now()
        turn.chat_event_id = chat_event_id
        self._write_manifest()

    def add_warning(self, warning: str) -> None:
        self.meeting.warnings.append(warning)
        self._write_manifest()

    def read_completed_turn_snapshot(self, turn_id: str) -> Optional[Tuple[str, str]]:
        path = self.turn_path(turn_id)
        if not path.exists():
            return None
        # Read at most one byte beyond the protocol limit, so a growing file
        # cannot make the harness allocate unbounded memory.
        with path.open("rb") as handle:
            data = handle.read(MAX_TURN_BYTES + 1)
        if len(data) > MAX_TURN_BYTES:
            raise InvalidTurnError(f"turn {turn_id} exceeds {MAX_TURN_BYTES} bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidTurnError(f"turn {turn_id} is not UTF-8") from exc

        lines = text.splitlines()
        if not lines or lines[-1] != TURN_COMPLETE_MARKER:
            return None
        if text.count(TURN_COMPLETE_MARKER) != 1:
            raise InvalidTurnError(f"turn {turn_id} has multiple completion markers")
        body = "\n".join(lines[:-1]).rstrip()
        if not body:
            raise InvalidTurnError(f"turn {turn_id} has no message body")
        return body, sha256_bytes(data)

    def read_completed_turn(self, turn_id: str) -> Optional[str]:
        snapshot = self.read_completed_turn_snapshot(turn_id)
        return snapshot[0] if snapshot is not None else None

    def append_chat(
        self,
        turn_id: str,
        speaker: str,
        content: str,
        source_sha256: Optional[str] = None,
    ) -> ChatMessage:
        turn = self.turn(turn_id)
        if turn.status != "running":
            raise MeetingError(f"turn {turn_id} is {turn.status}, expected running")
        if turn.speaker != speaker:
            raise MeetingError(
                f"turn {turn_id} belongs to {turn.speaker}, not {speaker}"
            )
        source_path = self.turn_path(turn_id)
        messages = self.chat_messages()
        if any(message.turn_id == turn_id for message in messages):
            raise MeetingError(f"turn already exists in chat stream: {turn_id}")
        actual_sha256 = sha256_file(source_path)
        if source_sha256 is not None and source_sha256 != actual_sha256:
            raise IntegrityError(f"turn {turn_id} changed while being accepted")
        event = ChatMessage(
            event_id=f"chat-{len(messages) + 1:06d}",
            turn_id=turn_id,
            speaker=speaker,
            content=content,
            captured_at=utc_now(),
            source_path=str(source_path.relative_to(self.run_dir)),
            source_sha256=actual_sha256,
        )
        _append_jsonl(self.stream_path("chat"), asdict(event))
        self.render_conversation()
        return event

    def chat_messages(self) -> List[ChatMessage]:
        return [ChatMessage(**payload) for payload in read_jsonl(self.stream_path("chat"))]

    def append_screen(
        self,
        participant: str,
        pane_id: str,
        content: str,
        phase: str,
    ) -> Optional[ScreenCapture]:
        if not content.strip():
            return None
        stream_name = f"screen:{participant}"
        if participant not in self._screen_hashes:
            previous_events = read_jsonl(self.stream_path(stream_name))
            self._screen_sequences[participant] = len(previous_events)
            self._screen_hashes[participant] = (
                previous_events[-1]["content_sha256"] if previous_events else None
            )

        content_hash = sha256_text(content)
        previous_hash = self._screen_hashes[participant]
        if content_hash == previous_hash:
            return None

        sequence = self._screen_sequences[participant] + 1
        event = ScreenCapture(
            event_id=f"screen-{participant}-{sequence:06d}",
            participant=participant,
            pane_id=pane_id,
            content=content,
            captured_at=utc_now(),
            content_sha256=content_hash,
            previous_sha256=previous_hash,
            phase=phase,
            stream=stream_name,
        )
        _append_jsonl(self.stream_path(stream_name), asdict(event))
        self._screen_sequences[participant] = sequence
        self._screen_hashes[participant] = content_hash
        return event

    def render_conversation(self) -> None:
        lines = [
            f"# RemoteBuddy Meeting: {self.meeting.meeting_id}",
            "",
            "Source brief: `brief.md`",
            "",
        ]
        for message in self.chat_messages():
            lines.extend(
                [
                    f"## {message.turn_id} · {message.speaker}",
                    "",
                    message.content,
                    "",
                ]
            )
        _atomic_write_text(self.run_dir / "conversation.md", "\n".join(lines).rstrip() + "\n")

    def protected_snapshot(self, active_turn_id: Optional[str] = None) -> Dict[str, str]:
        protected = [
            self.run_dir / "meeting.json",
            self.run_dir / "brief.md",
            self.run_dir / "conversation.md",
            self.stream_path("chat"),
            self.run_dir / ".agents" / "skills" / "meeting-participant" / "SKILL.md",
        ]
        active_relative = None
        if active_turn_id is not None:
            active_relative = str(Path(self.turn(active_turn_id).output_path))
            inventory_key = f"__turn_inventory__:{active_relative}"
        else:
            inventory_key = "__turn_inventory__"
        snapshot = {
            str(path.relative_to(self.run_dir)): sha256_file(path)
            for path in protected
            if path.is_file()
        }
        snapshot[inventory_key] = self._turn_inventory_hash(active_relative)
        return snapshot

    def _turn_inventory_hash(self, active_relative: Optional[str]) -> str:
        inventory: List[str] = []
        turns_dir = self.run_dir / "turns"
        for path in turns_dir.rglob("*"):
            relative = str(path.relative_to(self.run_dir))
            if relative == active_relative:
                continue
            if path.is_file():
                inventory.append(f"{relative}:{sha256_file(path)}")
            else:
                inventory.append(f"{relative}:<non-file>")
        return sha256_text("\n".join(sorted(inventory)))

    def assert_protected_unchanged(self, snapshot: Dict[str, str]) -> None:
        changed: List[str] = []
        for relative, expected_hash in snapshot.items():
            if relative.startswith("__turn_inventory__"):
                _, _, active_relative = relative.partition(":")
                current_hash = self._turn_inventory_hash(active_relative)
                if current_hash != expected_hash:
                    changed.append("turns/")
                continue
            path = self.run_dir / relative
            if not path.is_file() or sha256_file(path) != expected_hash:
                changed.append(relative)
        if changed:
            raise IntegrityError("protected meeting inputs changed: " + ", ".join(changed))

    def stream_event_count(self, stream_name: str) -> int:
        return len(read_jsonl(self.stream_path(stream_name)))
