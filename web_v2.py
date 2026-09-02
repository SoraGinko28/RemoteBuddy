#!/usr/bin/env python3
"""RemoteBuddy v2 local workspace service."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from agent_protocol import AgentBackend
from codex_app_server_client import (
    CodexAppServerBackend,
    CodexAppServerError,
    probe_codex_backend,
)
from claude_code_client import (
    ClaudeCodeBackend,
    ClaudeCodeError,
    probe_claude_backend,
)
from interactive_runner import (
    InteractiveRunner,
    RunnerError,
    capture_workspace,
    resolve_workspace_path,
    send_tokens_with_captures,
)
from meeting_v2 import (
    MAX_EVENT_CONTENT_BYTES,
    MAX_CHAT_REFERENCES,
    MAX_MESSAGE_BYTES,
    MEETING_ID_RE,
    SCHEMA_VERSION,
    VALID_SCORE_TAGS,
    MeetingError,
    MeetingStore,
    UnsupportedMeetingError,
    sha256_text,
    utc_now,
)
from meetingV6 import reference_from_event
from model_api_client import (
    AgentAction,
    MODEL_RE,
    SUPPORTED_PROVIDERS,
    ModelApiConfig,
    ModelApiError,
    ModelConfigError,
    OpenAIResponsesClient,
    _default_auth_key,
    _select_auth_key,
    _validate_base_url,
    validate_key_tokens,
)
from tmux_transport import TmuxError, TmuxTransport, libtmux_version


PROJECT_ROOT = Path(__file__).resolve().parent
SKILL_PATH = PROJECT_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
MODEL_API_PATH = PROJECT_ROOT / "model_api"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CSRF_HEADER = "X-RemoteBuddy-CSRF"
MAX_REQUEST_BYTES = MAX_MESSAGE_BYTES + 32 * 1024
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
EVENT_TYPES = {"command", "vim_keys"}
# Human terminal input is intentionally disabled while the shared workspace
# screen is being validated. Agent actions still use the internal transport.
HUMAN_TERMINAL_READ_ONLY = True
DEFAULT_JUDGE_NAME = "human"
MAX_JUDGE_NAME_BYTES = 200
LOCAL_SETTINGS_PATH = ".remotebuddy/settings.local.json"
SCORE_IGNORED_FIELDS = frozenset(
    {
        "score",
        "value",
        "numeric_score",
        "rating",
        "judge_name",
        "judge",
        "excerpt",
        "source_sha256",
        "source_hash",
    }
)
MAX_SCORE_COMMENT_BYTES = MAX_MESSAGE_BYTES
BACKEND_COMBINATIONS = {
    ("responses", "openai"): "Responses API",
    ("codex-app-server", "openai"): "Codex App Server",
    ("claude-code", "deepseek"): "Claude Code + DeepSeek",
}
OVERRIDES_FILENAME = "overrides.json"
MAX_API_KEY_CHARS = 4096
AUTH_FILENAME = "auth.json"


class WebError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _settings_path(project_root: Path) -> Path:
    # Check the lexical path before resolving it so a local symlink cannot
    # redirect settings writes outside the project.
    lexical = project_root / LOCAL_SETTINGS_PATH
    root = project_root.resolve()
    if root not in lexical.resolve().parents:
        raise WebError(500, "settings path escapes project root")
    settings_dir = lexical.parent
    if settings_dir.exists() and settings_dir.is_symlink():
        raise WebError(500, "local settings directory must not be a symlink")
    if lexical.exists() and lexical.is_symlink():
        raise WebError(500, "local settings file must not be a symlink")
    path = lexical.resolve()
    if root not in path.parents:
        raise WebError(500, "settings path escapes project root")
    return path


def _validate_judge_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebError(400, "judge_name must be a non-empty string")
    value = value.strip()
    if len(value.encode("utf-8")) > MAX_JUDGE_NAME_BYTES:
        raise WebError(413, "judge_name is too large")
    return value


def _read_judge_name(project_root: Path) -> str:
    path = _settings_path(project_root)
    if not path.exists():
        return DEFAULT_JUDGE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebError(500, "local settings are invalid") from exc
    if not isinstance(payload, dict):
        raise WebError(500, "local settings are invalid")
    value = payload.get("judge_name", DEFAULT_JUDGE_NAME)
    try:
        return _validate_judge_name(value)
    except WebError as exc:
        raise WebError(500, "local settings contain an invalid judge_name") from exc


def _write_judge_name(project_root: Path, judge_name: str) -> None:
    path = _settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".settings.local.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"judge_name": judge_name}, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _atomic_write_json(path: Path, payload: Dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


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


def _event_revision(store: MeetingStore) -> str:
    paths = [store.manifest_path, store.events_path, store.run_dir / "conversation.md"]
    signature: List[str] = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            signature.append(f"{path.name}:missing")
    return sha256_text("\n".join(signature))[:24]


def _public_manifest(store: MeetingStore) -> Dict[str, Any]:
    payload = store.meeting.to_dict()
    payload["run_path"] = f"runs/{store.meeting.meeting_id}"
    payload["event_count"] = store.event_count()
    return payload


class MeetingWebService:
    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        model_api_path: Optional[Path] = None,
        skill_path: Optional[Path] = None,
        runner_factory: Callable[..., InteractiveRunner] = InteractiveRunner,
        client_factory: Optional[Callable[[ModelApiConfig], AgentBackend]] = None,
        transport_factory: Callable[[str], TmuxTransport] = TmuxTransport,
        default_backend: Optional[str] = None,
        default_provider: Optional[str] = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.model_api_path = (model_api_path or self.project_root / "model_api").expanduser().resolve()
        self.skill_path = (
            skill_path
            or self.project_root / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
        ).expanduser().resolve()
        self.runner_factory = runner_factory
        self.client_factory = client_factory
        self.transport_factory = transport_factory
        self.default_backend = default_backend
        self.default_provider = default_provider
        self.csrf_token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs: Dict[str, threading.Thread] = {}
        self.queued_jobs: Dict[str, Tuple[MeetingStore, ModelApiConfig, bool, bool]] = {}
        self.meeting_locks: Dict[str, threading.RLock] = {}
        self.capture_jobs: Dict[str, threading.Thread] = {}
        self.active_backends: Dict[str, AgentBackend] = {}
        self._backend_check_cache: Optional[Tuple[float, str, bool, str]] = None
        self._claude_check_cache: Optional[Tuple[float, str, bool, str]] = None

    def _meeting_lock(self, meeting_id: str) -> threading.RLock:
        with self.lock:
            return self.meeting_locks.setdefault(meeting_id, threading.RLock())

    def _job_alive(self, meeting_id: str) -> bool:
        with self.lock:
            worker = self.jobs.get(meeting_id)
            return bool((worker and worker.is_alive()) or meeting_id in self.queued_jobs)

    def _make_backend(self, config: ModelApiConfig) -> AgentBackend:
        if self.client_factory is not None:
            return self.client_factory(config)
        if config.agent_backend == "codex-app-server":
            return CodexAppServerBackend(config)
        if config.agent_backend == "claude-code":
            return ClaudeCodeBackend(config)
        return OpenAIResponsesClient(config)

    def _interrupt_backend(self, meeting_id: str) -> None:
        with self.lock:
            backend = self.active_backends.get(meeting_id)
        if backend is None:
            return
        try:
            backend.interrupt()
        except Exception:
            # The runner owns failure classification; control operations stay
            # idempotent even if the model process has already exited.
            pass

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
        except UnsupportedMeetingError as exc:
            raise WebError(404, "meeting not found") from exc
        except MeetingError as exc:
            message = str(exc)
            raise WebError(404 if "not found" in message else 422, message) from exc

    def _load_config(self) -> ModelApiConfig:
        try:
            return self._apply_overrides(ModelApiConfig.load(self.model_api_path))
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc

    def _overrides_path(self) -> Path:
        path = self.model_api_path / OVERRIDES_FILENAME
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise WebError(500, "model_api/overrides.json must be a regular file")
        return path

    def _read_overrides(self) -> Dict[str, Dict[str, str]]:
        path = self._overrides_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(500, "model_api/overrides.json is invalid") from exc
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            raise WebError(500, "model_api/overrides.json is invalid")
        result: Dict[str, Dict[str, str]] = {}
        for provider, fields in providers.items():
            if provider not in SUPPORTED_PROVIDERS or not isinstance(fields, dict):
                raise WebError(500, "model_api/overrides.json is invalid")
            base_url = fields.get("base_url")
            if base_url is not None and not isinstance(base_url, str):
                raise WebError(500, "model_api/overrides.json is invalid")
            entry = {key: value for key, value in fields.items() if key == "base_url" and isinstance(value, str) and value}
            if entry:
                result[provider] = entry
        return result

    def _apply_overrides(self, config: ModelApiConfig) -> ModelApiConfig:
        overrides = self._read_overrides()
        if not overrides:
            return config
        profiles: Dict[str, Dict[str, Any]] = {
            name: dict(profile) if isinstance(profile, Mapping) else {}
            for name, profile in config.provider_profiles.items()
        }
        changed = False
        for provider, fields in overrides.items():
            base_url = fields.get("base_url")
            if base_url:
                profiles.setdefault(provider, {})["base_url"] = base_url
                changed = True
                if provider == config.agent_provider:
                    config = replace(config, base_url=base_url.rstrip("/"))
        return replace(config, provider_profiles=profiles) if changed else config

    def _auth_key_status(self, config: ModelApiConfig, provider: str) -> Tuple[str, bool]:
        profile = config.provider_profiles.get(provider, {})
        auth_name = profile.get("auth_key") if isinstance(profile, Mapping) else None
        if not isinstance(auth_name, str) or not auth_name:
            auth_name = _default_auth_key(provider)
        return auth_name, bool(_select_auth_key(config.auth_keys, auth_name))

    def credentials_status(self) -> Dict[str, Any]:
        """Describe stored credentials without ever returning secret values."""
        overrides = self._read_overrides()
        try:
            config: Optional[ModelApiConfig] = ModelApiConfig.load(self.model_api_path)
        except ModelConfigError:
            config = None
        providers: Dict[str, Any] = {}
        for provider in sorted(SUPPORTED_PROVIDERS):
            if config is not None:
                auth_name, has_key = self._auth_key_status(config, provider)
            else:
                auth_name, has_key = _default_auth_key(provider), False
            providers[provider] = {
                "auth_key_name": auth_name,
                "has_api_key": has_key,
                "base_url_override": overrides.get(provider, {}).get("base_url"),
            }
        return {"providers": providers}

    def update_credentials(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an API key and optional base URL for one provider."""
        if not isinstance(payload, dict) or not set(payload).issubset({"provider", "api_key", "base_url"}):
            raise WebError(400, "credentials request contains unsupported fields")
        provider = payload.get("provider")
        if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
            raise WebError(400, "provider must be one of: " + ", ".join(sorted(SUPPORTED_PROVIDERS)))
        api_key = payload.get("api_key")
        base_url = payload.get("base_url")
        if api_key is None and base_url is None:
            raise WebError(400, "credentials request requires api_key or base_url")
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key.strip():
                raise WebError(400, "api_key must be a non-empty string")
            api_key = api_key.strip()
            if len(api_key) > MAX_API_KEY_CHARS:
                raise WebError(413, "api_key is too large")
            if any(character.isspace() or not character.isprintable() for character in api_key):
                raise WebError(400, "api_key contains unsupported characters")
        if base_url is not None:
            if not isinstance(base_url, str):
                raise WebError(400, "base_url must be a string")
            base_url = base_url.strip()
            if base_url:
                try:
                    _validate_base_url(base_url)
                except ModelConfigError as exc:
                    raise WebError(400, str(exc)) from exc
        with self.lock:
            if api_key is not None:
                self._write_auth_key(provider, api_key)
            if base_url is not None:
                self._write_base_url_override(provider, base_url)
            self._backend_check_cache = None
            self._claude_check_cache = None
        return self.credentials_status()

    def _write_auth_key(self, provider: str, api_key: str) -> None:
        auth_path = self.model_api_path / AUTH_FILENAME
        if auth_path.exists() and (auth_path.is_symlink() or not auth_path.is_file()):
            raise WebError(500, "model_api/auth.json must be a regular file")
        existing: Dict[str, str] = {}
        if auth_path.exists():
            try:
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebError(500, "model_api/auth.json must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise WebError(500, "model_api/auth.json must be a JSON object")
            existing = {
                key: value
                for key, value in payload.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }
        name = _default_auth_key(provider)
        legacy = existing.pop("api_key", None)
        if legacy and name != "OPENAI_API_KEY" and "OPENAI_API_KEY" not in existing:
            # The legacy alias historically carried the OpenAI key; promote it
            # to the dedicated key so both providers can coexist.
            existing["OPENAI_API_KEY"] = legacy
        existing[name] = api_key
        _atomic_write_json(auth_path, existing, mode=0o600)

    def _write_base_url_override(self, provider: str, base_url: str) -> None:
        overrides = self._read_overrides()
        entry = dict(overrides.get(provider, {}))
        if base_url:
            entry["base_url"] = base_url.rstrip("/")
        else:
            entry.pop("base_url", None)
        if entry:
            overrides[provider] = entry
        else:
            overrides.pop(provider, None)
        _atomic_write_json(self._overrides_path(), {"providers": overrides}, mode=0o600)

    def settings(self) -> Dict[str, Any]:
        """Return machine-local page settings that are safe to expose."""
        with self.lock:
            return {"judge_name": _read_judge_name(self.project_root)}

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"judge_name"}:
            raise WebError(400, "settings request must contain only judge_name")
        judge_name = _validate_judge_name(payload.get("judge_name"))
        with self.lock:
            _write_judge_name(self.project_root, judge_name)
        return {"judge_name": judge_name}

    def _probe_codex_config(self, config: ModelApiConfig) -> Tuple[bool, str]:
        cache_key = f"{config.model}:{config.reasoning_effort}:{config.codex_dynamic_tools}"
        now = time.monotonic()
        cached = self._backend_check_cache
        if cached and cached[1] == cache_key and now - cached[0] < 10.0:
            return cached[2], cached[3]
        try:
            detail = probe_codex_backend(config)
            ok = True
        except CodexAppServerError as exc:
            ok, detail = False, str(exc)
        self._backend_check_cache = (time.monotonic(), cache_key, ok, detail)
        return ok, detail

    def _probe_claude_config(self, config: ModelApiConfig) -> Tuple[bool, str]:
        cache_key = f"{config.model}:{config.base_url}:{config.claude_binary}"
        now = time.monotonic()
        cached = self._claude_check_cache
        if cached and cached[1] == cache_key and now - cached[0] < 10.0:
            return cached[2], cached[3]
        try:
            detail = probe_claude_backend(config)
            ok = True
        except ClaudeCodeError as exc:
            ok, detail = False, str(exc)
        self._claude_check_cache = (time.monotonic(), cache_key, ok, detail)
        return ok, detail

    @staticmethod
    def _validate_backend_pair(backend: Any, provider: Any) -> Tuple[str, str]:
        if not isinstance(backend, str) or not isinstance(provider, str):
            raise WebError(400, "agent_backend and agent_provider must be strings")
        if (backend, provider) not in BACKEND_COMBINATIONS:
            raise WebError(400, "agent backend/provider combination is not supported")
        return backend, provider

    def _resolve_config(
        self,
        config: ModelApiConfig,
        backend: str,
        provider: str,
        model: Optional[str],
    ) -> ModelApiConfig:
        self._validate_backend_pair(backend, provider)
        try:
            return config.resolve(backend, provider, model)
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc

    def _probe_selected_config(self, config: ModelApiConfig) -> None:
        if self.client_factory is not None:
            return
        if config.agent_backend == "codex-app-server":
            ok, detail = self._probe_codex_config(config)
        elif config.agent_backend == "claude-code":
            ok, detail = self._probe_claude_config(config)
        else:
            return
        if not ok:
            raise WebError(503, detail)

    def _config_for_store(
        self, store: MeetingStore, config: Optional[ModelApiConfig] = None
    ) -> ModelApiConfig:
        resolved = self._resolve_config(
            config or self._load_config(),
            store.meeting.agent_backend,
            store.meeting.agent_provider,
            store.meeting.model,
        )
        self._probe_selected_config(resolved)
        return resolved

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
            manifest = directory / "meeting.json"
            if MeetingStore.peek_schema(manifest) != SCHEMA_VERSION:
                continue
            try:
                store = MeetingStore.load(self.project_root, directory.name)
                summaries.append(_public_manifest(store))
            except (MeetingError, OSError, ValueError) as exc:
                warnings.append(f"{directory.name}: {exc}")
        summaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return {"meetings": summaries, "warnings": warnings}

    def snapshot(self, meeting_id: str) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        if store.meeting.control_owner == "human":
            self._ensure_capture_worker(store)
        conversation_path = store.run_dir / "conversation.md"
        conversation = conversation_path.read_text(encoding="utf-8") if conversation_path.is_file() else ""
        events = store.events()
        return {
            "meeting": _public_manifest(store),
            "conversation": conversation,
            "chat": [event for event in events if event.get("kind") == "chat"],
            "scores": [event for event in events if event.get("kind") == "score"],
            "latest_screen": store.latest_screen(),
            "revision": _event_revision(store),
            "cursor": int(events[-1]["seq"]) if events else 0,
        }

    def _validated_reference(self, store: MeetingStore, payload: Any) -> Optional[Dict[str, Any]]:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise WebError(400, "reference must be an object")
        event_id = payload.get("event_id")
        event = next((item for item in store.events() if item.get("event_id") == event_id), None)
        if event is None:
            raise WebError(422, "reference source event was not found")
        try:
            return reference_from_event(event, payload)
        except (TypeError, ValueError) as exc:
            raise WebError(422, str(exc)) from exc

    def _validated_references(
        self,
        store: MeetingStore,
        payload: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Validate the V6 reference list while accepting the V2 singular alias."""
        has_list = "references" in payload
        has_single = "reference" in payload
        if has_list and payload.get("references") is not None and not isinstance(payload.get("references"), list):
            raise WebError(400, "references must be an array")
        candidates: List[Any] = []
        if has_list and isinstance(payload.get("references"), list):
            candidates.extend(payload["references"])
        if has_single and payload.get("reference") is not None:
            # Some V6 callers kept the old field name while changing its
            # value to a list.  Accept that spelling alongside ``references``
            # so the migration does not force a flag-day client update.
            singular_value = payload.get("reference")
            if isinstance(singular_value, list):
                candidates.extend(singular_value)
            else:
                candidates.append(singular_value)
        if len(candidates) > MAX_CHAT_REFERENCES:
            raise WebError(413, f"at most {MAX_CHAT_REFERENCES} references are allowed")
        validated: List[Dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            reference = self._validated_reference(store, candidate)
            if reference is None:
                raise WebError(422, "reference must be an object")
            region = reference.get("region") if isinstance(reference.get("region"), Mapping) else {}
            key = (
                reference.get("kind"),
                reference.get("event_id"),
                region.get("start"),
                region.get("end"),
            )
            if key in seen:
                continue
            seen.add(key)
            validated.append(reference)
        return validated

    def events(self, meeting_id: str, after: Any = 0) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        try:
            sequence = int(after)
        except (TypeError, ValueError) as exc:
            raise WebError(400, "after must be an integer") from exc
        if sequence < 0:
            raise WebError(400, "after must be at least zero")
        all_events = store.events()
        return {
            "meeting_id": meeting_id,
            "events": [event for event in all_events if int(event.get("seq", 0)) > sequence],
            "cursor": int(all_events[-1]["seq"]) if all_events else sequence,
            "revision": _event_revision(store),
        }

    def _new_id(self) -> str:
        for _ in range(20):
            value = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            if not (self.project_root / "runs" / value).exists():
                return value
        raise WebError(500, "could not allocate a meeting id")

    def start(self, payload: Dict[str, Any]) -> MeetingStore:
        if not isinstance(payload, dict):
            raise WebError(400, "request must be a JSON object")
        unexpected = set(payload) - {
            "message",
            "meeting_id",
            "model",
            "typing_profile",
            "agent_backend",
            "agent_provider",
        }
        if unexpected:
            raise WebError(400, "unexpected meeting fields: " + ", ".join(sorted(unexpected)))
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise WebError(400, "message must be a non-empty string")
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise WebError(413, "message is too large")
        meeting_id = payload.get("meeting_id") or self._new_id()
        if not isinstance(meeting_id, str) or not MEETING_ID_RE.fullmatch(meeting_id):
            raise WebError(400, "invalid meeting id")
        config = self._load_config()
        backend = payload.get("agent_backend", self.default_backend or config.agent_backend)
        provider = payload.get("agent_provider", self.default_provider or config.agent_provider)
        backend, provider = self._validate_backend_pair(backend, provider)
        model = payload.get("model") or None
        if model is not None and (not isinstance(model, str) or not MODEL_RE.fullmatch(model)):
            raise WebError(400, "model must be a short string")
        config = self._resolve_config(config, backend, provider, model)
        self._probe_selected_config(config)
        model = config.model
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise WebError(400, "model must be a short string")
        typing_profile = payload.get("typing_profile", "balanced")
        if typing_profile not in {"demo", "balanced", "fast"}:
            raise WebError(400, "typing_profile must be demo, balanced, or fast")
        session_name = f"remotebuddy-{meeting_id}"
        try:
            store = MeetingStore.create(
                self.project_root,
                meeting_id,
                message,
                self.skill_path,
                session_name,
                model,
                agent_backend=config.agent_backend,
                typing_profile=typing_profile,
                agent_provider=config.agent_provider,
            )
        except (MeetingError, OSError) as exc:
            raise WebError(400, str(exc)) from exc
        self._ensure_worker(store, config, resume=False, fresh_conversation=False)
        return store

    def _ensure_worker(
        self,
        store: MeetingStore,
        config: ModelApiConfig,
        *,
        resume: bool,
        fresh_conversation: bool,
    ) -> None:
        meeting_id = store.meeting.meeting_id
        try:
            config = config.resolve(
                store.meeting.agent_backend,
                store.meeting.agent_provider,
                store.meeting.model,
            )
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc
        with self.lock:
            current = self.jobs.get(meeting_id)
            if current and current.is_alive():
                previous = self.queued_jobs.get(meeting_id)
                if previous:
                    fresh_conversation = fresh_conversation or previous[3]
                self.queued_jobs[meeting_id] = (store, config, resume, fresh_conversation)
                return
            client = self._make_backend(config)
            transport = self.transport_factory(shutil.which("tmux") or "tmux")
            meeting_lock = self._meeting_lock(meeting_id)
            worker = threading.Thread(
                target=self._worker,
                args=(store, client, transport, meeting_lock, resume, fresh_conversation),
                name=f"remotebuddy-{meeting_id}",
                daemon=True,
            )
            self.jobs[meeting_id] = worker
            self.active_backends[meeting_id] = client
            worker.start()

    def _worker(
        self,
        store: MeetingStore,
        client: AgentBackend,
        transport: TmuxTransport,
        meeting_lock: threading.RLock,
        resume: bool,
        fresh_conversation: bool,
    ) -> None:
        pending: Optional[Tuple[MeetingStore, ModelApiConfig, bool, bool]] = None
        try:
            runner = self.runner_factory(store, transport, client, lock=meeting_lock)
            if resume:
                runner.resume(fresh_conversation=fresh_conversation)
            else:
                runner.start()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            with self.lock:
                meeting_id = store.meeting.meeting_id
                if self.active_backends.get(meeting_id) is client:
                    self.active_backends.pop(meeting_id, None)
                if self.jobs.get(meeting_id) is threading.current_thread():
                    self.jobs.pop(meeting_id, None)
                pending = self.queued_jobs.pop(meeting_id, None)
            if pending:
                next_store, next_config, next_resume, next_fresh = pending
                self._ensure_worker(
                    next_store,
                    next_config,
                    resume=next_resume,
                    fresh_conversation=next_fresh,
                )

    def _ensure_capture_worker(self, store: MeetingStore) -> None:
        meeting_id = store.meeting.meeting_id
        with self.lock:
            current = self.capture_jobs.get(meeting_id)
            if current and current.is_alive():
                return
            worker = threading.Thread(
                target=self._capture_human_control,
                args=(meeting_id,),
                name=f"remotebuddy-capture-{meeting_id}",
                daemon=True,
            )
            self.capture_jobs[meeting_id] = worker
            worker.start()

    def _capture_human_control(self, meeting_id: str) -> None:
        transport = self.transport_factory(shutil.which("tmux") or "tmux")
        try:
            while True:
                with self._meeting_lock(meeting_id):
                    store = self.load_store(meeting_id)
                    if store.meeting.status in TERMINAL_STATUSES:
                        return
                    if store.meeting.control_owner == "human":
                        try:
                            capture_workspace(store, transport)
                        except (RunnerError, TmuxError, OSError) as exc:
                            store.append_event("error", "harness", "failed", content=str(exc))
                            try:
                                session_state = (
                                    "retained"
                                    if transport.session_exists(store.meeting.session_name)
                                    else "stopped"
                                )
                            except (TmuxError, OSError):
                                session_state = store.meeting.session_state
                            store.update(
                                status="failed",
                                phase="failed",
                                control_owner="none",
                                failure=str(exc),
                                session_state=session_state,
                            )
                            return
                    elif store.meeting.control_requested != "human":
                        return
                time.sleep(0.25)
        except (WebError, MeetingError, OSError):
            return
        finally:
            with self.lock:
                if self.capture_jobs.get(meeting_id) is threading.current_thread():
                    self.capture_jobs.pop(meeting_id, None)

    def message(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if not isinstance(payload, dict) or not set(payload).issubset(
                {"message", "delivery", "reference", "references"}
            ):
                raise WebError(400, "message request contains unsupported fields")
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, str) or not message.strip():
                raise WebError(400, "message must be a non-empty string")
            if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise WebError(413, "message is too large")
            delivery = payload.get("delivery", "next_action")
            if delivery not in {"next_action", "interrupt"}:
                raise WebError(400, "delivery must be next_action or interrupt")
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            references = self._validated_references(store, payload)
            starting_fresh = store.meeting.phase == "created"
            orphaned_agent = (
                store.meeting.phase in {"agent_thinking", "agent_acting"}
                and not self._job_alive(meeting_id)
            )
            wake_agent = (
                store.meeting.phase in {"waiting_for_user", "completion_proposed", "api_error", "cycle_timeout", "created"}
                or orphaned_agent
            )
            fresh_conversation = (
                store.meeting.phase in {"api_error", "cycle_timeout"}
                or store.meeting.recovery_required
                or orphaned_agent
            )
            if "references" in payload or isinstance(payload.get("reference"), list):
                store.append_chat("human", message, "request", references=references)
            else:
                # Preserve the legacy singular journal shape when an older
                # client sends ``reference``; V6 callers use ``references``.
                store.append_chat(
                    "human",
                    message,
                    "request",
                    reference=references[0] if references else None,
                )
            if delivery == "interrupt" or wake_agent:
                store.bump_request_generation(
                    failure=None,
                    cycle_action_count=0,
                    cycle_started_at=utc_now(),
                )
            else:
                store.update(failure=None)
            if delivery == "interrupt":
                self._interrupt_backend(meeting_id)
            if wake_agent or delivery == "interrupt":
                config = self._config_for_store(store)
                store.update(status="running", phase="agent_thinking", control_owner="agent")
                self._ensure_worker(
                    store,
                    config,
                    resume=not starting_fresh,
                    fresh_conversation=fresh_conversation,
                )
        return self.load_store(meeting_id)

    def score(self, meeting_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append a validated score without waking or mutating Agent state."""
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            allowed_fields = {"tag", "reference", "comment"} | set(SCORE_IGNORED_FIELDS)
            if not isinstance(payload, dict) or not set(payload).issubset(allowed_fields):
                raise WebError(400, "score request contains unsupported fields")
            tag = payload.get("tag")
            if not isinstance(tag, str) or tag not in VALID_SCORE_TAGS:
                raise WebError(400, "tag must be competent or incompetent")
            if "reference" not in payload:
                raise WebError(400, "score reference is required")
            reference = self._validated_reference(store, payload.get("reference"))
            if reference is None:
                raise WebError(422, "score reference is required")
            comment = payload.get("comment")
            if comment is not None:
                if not isinstance(comment, str):
                    raise WebError(400, "comment must be a string or null")
                comment = comment.strip() or None
                if comment is not None and len(comment.encode("utf-8")) > MAX_SCORE_COMMENT_BYTES:
                    raise WebError(413, "comment is too large")
            judge_name = _read_judge_name(self.project_root)
            try:
                return store.append_score(tag, reference, judge_name, score=None, comment=comment)
            except MeetingError as exc:
                raise WebError(422, str(exc)) from exc

    def acquire(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            if HUMAN_TERMINAL_READ_ONLY:
                raise WebError(409, "terminal is temporarily read-only for human users")
            if store.meeting.control_owner == "human":
                return store
            orphaned_agent = (
                store.meeting.phase in {"agent_thinking", "agent_acting"}
                and not self._job_alive(meeting_id)
            )
            store.bump_request_generation(
                control_requested="human",
                recovery_required=store.meeting.recovery_required or orphaned_agent,
            )
            self._interrupt_backend(meeting_id)
            if store.meeting.phase in {"waiting_for_user", "completion_proposed", "api_error", "cycle_timeout"} or orphaned_agent:
                store.update(phase="human_control", control_owner="human", control_requested=None)
                store.append_event("control", "human", "human_control", data={"status": "acquired"})
            self._ensure_capture_worker(store)
        return self.load_store(meeting_id)

    def release(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.control_owner != "human":
                return store
            config = self._config_for_store(store)
            fresh_conversation = store.meeting.recovery_required
            store.bump_request_generation(
                phase="agent_thinking",
                control_owner="agent",
                control_requested=None,
                cycle_action_count=0,
                cycle_started_at=utc_now(),
                status="running",
            )
            store.append_event("control", "human", "agent_thinking", data={"status": "released"})
            self._ensure_worker(
                store,
                config,
                resume=True,
                fresh_conversation=fresh_conversation,
            )
        return self.load_store(meeting_id)

    def terminal_input(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if HUMAN_TERMINAL_READ_ONLY:
                raise WebError(403, "terminal is temporarily read-only for human users")
            if store.meeting.control_owner != "human":
                raise WebError(409, "human must acquire terminal control first")
            pane_id = store.meeting.pane_id
            if not pane_id:
                raise WebError(409, "workspace pane is not available")
            kind = payload.get("type") if isinstance(payload, dict) else None
            transport = self.transport_factory(shutil.which("tmux") or "tmux")
            action_id = "human-action-" + secrets.token_hex(6)
            try:
                info = transport.pane_info(pane_id)
                command = ""
                path = ""
                keys: Sequence[Dict[str, str]] = []
                if kind == "command":
                    if set(payload) != {"type", "command", "path"}:
                        raise WebError(400, "command input has unexpected fields")
                    command = payload.get("command")
                    path = payload.get("path", "")
                    if command not in {"pwd", "cd", "vim", "python"} or not isinstance(path, str):
                        raise WebError(400, "invalid workspace command")
                    if info["command"] not in {"bash", "zsh", "sh"}:
                        raise WebError(409, "workspace commands require an active shell")
                    if command in {"cd", "vim", "python"}:
                        path = str(resolve_workspace_path(store, info["path"], path, command))
                elif kind in {"vim_keys", "terminal_keys"}:
                    if set(payload) != {"type", "keys"}:
                        raise WebError(400, "terminal input has unexpected fields")
                    keys = payload.get("keys")
                    vim_active = info["command"] in {"vim", "vi"}
                    python_active = info["command"].lower().startswith("python")
                    if kind == "vim_keys" and not vim_active:
                        raise WebError(409, "vim keys require an active Vim process")
                    if kind == "terminal_keys" and not python_active:
                        raise WebError(409, "terminal keys require an active Python process")
                    if not isinstance(keys, list):
                        raise WebError(400, "keys must be an array")
                    validate_key_tokens(keys)
                else:
                    raise WebError(400, "terminal input type is not supported")

                store.append_event(
                    "action",
                    "human",
                    "human_control",
                    data={"action_id": action_id, "status": "received", "input": payload},
                )
                store.append_event(
                    "action", "human", "human_control", data={"action_id": action_id, "status": "started"}
                )
                if kind == "command":
                    transport.send_workspace_command(pane_id, command, path)
                else:
                    send_tokens_with_captures(store, transport, pane_id, keys)
                time.sleep(0.15)
                capture_workspace(store, transport)
                store.append_event(
                    "action", "human", "human_control", data={"action_id": action_id, "status": "completed"}
                )
            except WebError:
                raise
            except ModelApiError as exc:
                raise WebError(400, str(exc)) from exc
            except (RunnerError, TmuxError, OSError) as exc:
                store.append_event(
                    "action",
                    "human",
                    "human_control",
                    content=str(exc),
                    data={"action_id": action_id, "status": "rejected"},
                )
                raise WebError(422, str(exc)) from exc
        return self.load_store(meeting_id)

    def complete(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status == "completed":
                return store
            if store.meeting.status in {"failed", "interrupted"}:
                raise WebError(409, "meeting cannot be completed from its current state")
            if not store.meeting.pane_id or store.meeting.session_state not in {"running", "retained"}:
                raise WebError(409, "workspace session has not started")
            store.bump_request_generation(
                status="completed",
                phase="completed",
                control_owner="none",
                control_requested=None,
                session_state="retained",
            )
            self._interrupt_backend(meeting_id)
            store.append_event("state", "human", "completed", data={"status": "completed"})
            with self.lock:
                self.queued_jobs.pop(meeting_id, None)
        return self.load_store(meeting_id)

    def stop(self, meeting_id: str) -> MeetingStore:
        # Publish the durable interrupt before contending on meeting/state
        # locks. The typing loop checks this file at every burst boundary.
        initial = self.load_store(meeting_id)
        if initial.meeting.session_state == "stopped" and initial.meeting.status == "interrupted":
            return initial
        if initial.meeting.status in {"completed", "failed"}:
            raise WebError(409, "use cleanup for a finished meeting")
        initial.request_stop()
        self._interrupt_backend(meeting_id)
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.session_state == "stopped" and store.meeting.status == "interrupted":
                return store
            if store.meeting.status in {"completed", "failed"}:
                raise WebError(409, "use cleanup for a finished meeting")
            store.bump_request_generation(
                status="interrupted",
                phase="interrupted",
                control_owner="none",
                control_requested=None,
                pending_action_id=None,
                failure="stopped by user",
            )
            try:
                self.transport_factory(shutil.which("tmux") or "tmux").kill_session(
                    store.meeting.session_name
                )
            except (TmuxError, OSError) as exc:
                store.append_event("error", "harness", "interrupted", content=str(exc))
                raise WebError(502, "meeting stopped, but tmux cleanup failed") from exc
            store.mark_session_stopped()
            store.append_event("state", "human", "interrupted", data={"status": "interrupted"})
            with self.lock:
                self.queued_jobs.pop(meeting_id, None)
        return self.load_store(meeting_id)

    def cleanup(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status not in TERMINAL_STATUSES:
                raise WebError(409, "meeting must be finished before cleanup")
            if store.meeting.session_state == "stopped":
                return store
            try:
                self.transport_factory(shutil.which("tmux") or "tmux").kill_session(
                    store.meeting.session_name
                )
            except (TmuxError, OSError) as exc:
                store.append_event("error", "harness", store.meeting.phase, content=str(exc))
                raise WebError(502, "tmux cleanup failed") from exc
            store.mark_session_stopped()
            store.append_event("control", "human", store.meeting.phase, data={"status": "cleaned"})
        return self.load_store(meeting_id)

    def resume(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            orphaned_agent = (
                store.meeting.phase in {"agent_thinking", "agent_acting"}
                and not self._job_alive(meeting_id)
            )
            if store.meeting.phase not in {"waiting_for_user", "completion_proposed", "api_error", "cycle_timeout"} and not orphaned_agent:
                raise WebError(409, "Agent can resume only while waiting or after an API error")
            config = self._config_for_store(store)
            fresh_conversation = (
                store.meeting.phase in {"api_error", "cycle_timeout", "completion_proposed"}
                or store.meeting.recovery_required
                or orphaned_agent
            )
            store.bump_request_generation(
                status="running",
                phase="agent_thinking",
                control_owner="agent",
                failure=None,
                cycle_action_count=0,
                cycle_started_at=utc_now(),
            )
            self._ensure_worker(
                store,
                config,
                resume=True,
                fresh_conversation=fresh_conversation,
            )
        return self.load_store(meeting_id)

    def health(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        python_ok = sys.version_info[:2] == (3, 12)
        checks.append({"name": "python", "ok": python_ok, "detail": ".".join(map(str, sys.version_info[:3]))})
        for name in ("tmux", "vim"):
            path = shutil.which(name)
            checks.append({"name": name, "ok": path is not None, "detail": path or "not found"})
        tmux_library_version = libtmux_version()
        checks.append(
            {
                "name": "libtmux",
                "ok": tmux_library_version is not None,
                "detail": tmux_library_version or "not installed",
            }
        )
        backends: Dict[str, Dict[str, Any]] = {}
        selected_key: Optional[str] = None
        config: Optional[ModelApiConfig] = None
        try:
            config = self._apply_overrides(ModelApiConfig.load(self.model_api_path))
            if self.default_backend:
                config = config.resolve(
                    self.default_backend,
                    self.default_provider or config.agent_provider,
                )
            selected_key = f"{config.agent_backend}/{config.agent_provider}"
            checks.append(
                {
                    "name": "model-api",
                    "ok": True,
                    "detail": f"{config.model} ({selected_key})",
                }
            )
            for (backend, provider), label in BACKEND_COMBINATIONS.items():
                key = f"{backend}/{provider}"
                auth_key_name, has_api_key = self._auth_key_status(config, provider)
                try:
                    resolved = config.resolve(backend, provider)
                    ok, detail = True, resolved.model
                    if backend == "codex-app-server":
                        ok, detail = self._probe_codex_config(resolved)
                    elif backend == "claude-code":
                        ok, detail = self._probe_claude_config(resolved)
                    backends[key] = {
                        "ok": ok,
                        "detail": detail,
                        "label": label,
                        "agent_backend": backend,
                        "agent_provider": provider,
                        "default_model": resolved.model,
                        "base_url": resolved.base_url,
                        "auth_key_name": auth_key_name,
                        "has_api_key": has_api_key,
                        "needs_api_key": not has_api_key,
                        "required": key == selected_key,
                    }
                except (ModelConfigError, ClaudeCodeError, CodexAppServerError) as exc:
                    backends[key] = {
                        "ok": False,
                        "detail": str(exc),
                        "label": label,
                        "agent_backend": backend,
                        "agent_provider": provider,
                        "default_model": (
                            "deepseek-v4-pro[1m]" if provider == "deepseek" else None
                        ),
                        "base_url": (
                            "https://api.deepseek.com/anthropic" if provider == "deepseek" else None
                        ),
                        "auth_key_name": auth_key_name,
                        "has_api_key": has_api_key,
                        "needs_api_key": not has_api_key,
                        "required": key == selected_key,
                    }
            selected = backends.get(selected_key)
            if selected and config.agent_backend != "responses":
                checks.append(
                    {
                        "name": config.agent_backend,
                        "ok": selected["ok"],
                        "detail": selected["detail"],
                    }
                )
        except (ModelConfigError, WebError) as exc:
            checks.append({"name": "model-api", "ok": False, "detail": str(exc)})
        core_names = {"python", "tmux", "vim", "libtmux", "model-api"}
        core_ok = all(check["ok"] for check in checks if check["name"] in core_names)
        selected_ok = bool(selected_key and backends.get(selected_key, {}).get("ok"))
        with self.lock:
            workers = list(self.jobs.values())
        return {
            "ok": core_ok and selected_ok,
            "core_ok": core_ok,
            "python": ".".join(map(str, sys.version_info[:3])),
            "checks": checks,
            "backends": backends,
            "active_jobs": sum(worker.is_alive() for worker in workers),
            "agent_backend": config.agent_backend if config is not None else None,
            "agent_provider": config.agent_provider if config is not None else None,
            "service": "running",
        }


class RemoteBuddyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], service: MeetingWebService):
        self.service = service
        super().__init__(server_address, RemoteBuddyRequestHandler)


class RemoteBuddyRequestHandler(BaseHTTPRequestHandler):
    server_version = "RemoteBuddy/0.2"

    _LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}

    @property
    def service(self) -> MeetingWebService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _guard(self) -> None:
        """Reject requests that did not originate from this loopback service.

        The loopback bind alone does not stop DNS rebinding: a page on an
        attacker domain whose DNS re-resolves to 127.0.0.1 becomes same-origin
        with this service. Pinning the Host header (and any Origin header) to
        the local addresses closes that path.
        """
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in {f"{name}:{port}" for name in self._LOOPBACK_HOSTNAMES} | self._LOOPBACK_HOSTNAMES:
            raise WebError(403, "request host is not the local workspace")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {f"http://{name}:{port}" for name in self._LOOPBACK_HOSTNAMES}:
            raise WebError(403, "cross-origin requests are not allowed")

    def _headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _json(self, payload: Dict[str, Any], status: int = 200, etag: Optional[str] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(status)
            self._headers()
            self.send_header("Content-Length", str(len(body)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status=status)

    def _internal_error(self, exc: Exception) -> None:
        # Exception text can carry absolute filesystem paths; keep it out of
        # the response and log it server-side instead.
        print(f"[web_v2] internal error on {self.command} {self.path}: {exc!r}", file=sys.stderr, flush=True)
        self._error(500, "internal error")

    def _read_json(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise WebError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise WebError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise WebError(413, "request body is too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise WebError(400, "incomplete request body")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(400, "request body must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise WebError(400, "request body must be a JSON object")
        return payload

    def _read_empty_json(self) -> None:
        if self._read_json():
            raise WebError(400, "request body must be an empty JSON object")

    def _csrf(self) -> None:
        if not hmac.compare_digest(self.headers.get(CSRF_HEADER, ""), self.service.csrf_token):
            raise WebError(403, "missing or invalid CSRF token")

    def _static(self, relative: str) -> None:
        frontend_root = self.service.project_root / "frontend"
        if frontend_root.is_symlink():
            raise WebError(404, "static file not found")
        root = frontend_root.resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise WebError(404, "static file not found")
        data = candidate.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:24] + '"'
        try:
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self._headers(mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._guard()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/":
                self._static("workspace.html")
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
            if path == "/api/settings":
                self._json(self.service.settings())
                return
            if path == "/api/credentials":
                self._json(self.service.credentials_status())
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
                after = query.get("after", ["0"])[-1]
                payload = self.service.events(unquote(match.group(1)), after)
                etag = '"' + sha256_text(payload["revision"] + str(after))[:24] + '"'
                self._json(payload, etag=etag)
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, ValueError) as exc:
            self._internal_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._guard()
            self._csrf()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/meetings":
                store = self.service.start(self._read_json())
                self._json({"meeting_id": store.meeting.meeting_id, "meeting": _public_manifest(store)}, status=201)
                return
            if path == "/api/settings":
                self._json(self.service.update_settings(self._read_json()))
                return
            if path == "/api/credentials":
                self._json(self.service.update_credentials(self._read_json()))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/scores", path)
            if match:
                meeting_id = unquote(match.group(1))
                event = self.service.score(meeting_id, self._read_json())
                self._json({"event": event, "meeting": _public_manifest(self.service.load_store(meeting_id))})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/messages", path)
            if match:
                store = self.service.message(unquote(match.group(1)), self._read_json())
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/control/(acquire|release)", path)
            if match:
                self._read_empty_json()
                store = getattr(self.service, match.group(2))(unquote(match.group(1)))
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/terminal/input", path)
            if match:
                store = self.service.terminal_input(unquote(match.group(1)), self._read_json())
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/agent/resume", path)
            if match:
                self._read_empty_json()
                store = self.service.resume(unquote(match.group(1)))
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/(complete|stop|cleanup)", path)
            if match:
                self._read_empty_json()
                store = getattr(self.service, match.group(2))(unquote(match.group(1)))
                self._json({"meeting": _public_manifest(store)})
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, TmuxError, ModelApiError, ValueError) as exc:
            self._internal_error(exc)


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, service: Optional[MeetingWebService] = None) -> RemoteBuddyHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("RemoteBuddy web server only listens on loopback")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return RemoteBuddyHTTPServer((host, port), service or MeetingWebService())


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the RemoteBuddy v2 workspace service")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--harness",
        default=None,
        help="Default harness for new sessions: claude, responses, or codex",
    )
    args = parser.parse_args(argv)

    default_backend: Optional[str] = None
    default_provider: Optional[str] = None
    if args.harness:
        aliases = {
            "claude": ("claude-code", "deepseek"),
            "claude-code": ("claude-code", "deepseek"),
            "responses": ("responses", "openai"),
            "codex": ("codex-app-server", "openai"),
            "codex-app-server": ("codex-app-server", "openai"),
        }
        pair = aliases.get(args.harness)
        if pair is None:
            parser.error("--harness must be one of: claude, responses, codex")
        default_backend, default_provider = pair

    service = MeetingWebService(
        default_backend=default_backend,
        default_provider=default_provider,
    )
    server = build_server(args.host, args.port, service=service)
    print(f"RemoteBuddy workspace: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
