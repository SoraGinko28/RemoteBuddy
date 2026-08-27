from __future__ import annotations

import ast
import json
import math
import os
import re
import socket
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from agent_protocol import (
    AgentAction,
    AgentBackendError,
    AgentProtocolError,
    AgentResponse,
    action_json_schema,
    decode_action_text,
    validate_key_tokens,
)


CONFIG_LIMIT = 64 * 1024
AUTH_LIMIT = 16 * 1024
CLIENT_USER_AGENT = "RemoteBuddy/0.2"
REQUIRED_CONFIG_KEYS = {
    "model",
    "base_url",
    "request_timeout_seconds",
    "reasoning_effort",
}
ALLOWED_CONFIG_KEYS = {
    *REQUIRED_CONFIG_KEYS,
    "agent_backend",
    "codex_dynamic_tools",
}
CODEX_CONFIG_KEYS = {
    "model_provider",
    "model",
    "review_model",
    "model_reasoning_effort",
    "disable_response_storage",
    "network_access",
    "windows_wsl_setup_acknowledged",
    "notify",
    "request_timeout_seconds",
    "agent_backend",
    "codex_dynamic_tools",
}
CODEX_PROVIDER_KEYS = {"name", "base_url", "wire_api", "requires_openai_auth"}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


ModelApiError = AgentBackendError
ModelResponseError = AgentProtocolError


class ModelConfigError(ModelApiError):
    pass


@dataclass(frozen=True)
class ModelApiConfig:
    model: str
    base_url: str
    request_timeout_seconds: float
    reasoning_effort: str
    api_key: str = field(repr=False)
    warnings: Sequence[str]
    store_responses: bool = True
    agent_backend: str = "responses"
    codex_dynamic_tools: bool = False

    @classmethod
    def load(cls, directory: Path) -> "ModelApiConfig":
        root = directory.expanduser().resolve()
        config_path = root / "config.toml"
        auth_path = root / "auth.json"
        if not config_path.is_file() or config_path.is_symlink():
            raise ModelConfigError("model_api/config.toml is missing or not a regular file")

        config_text = _bounded_text(config_path, CONFIG_LIMIT, "config.toml")
        values = _parse_toml_document(config_text)
        values = _normalise_config(values)

        model = values["model"]
        base_url = values["base_url"]
        timeout = values["request_timeout_seconds"]
        effort = values["reasoning_effort"]
        agent_backend = values.get("agent_backend", "responses")
        dynamic_tools = values.get("codex_dynamic_tools", False)
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise ModelConfigError("model must be a short model identifier")
        if not isinstance(base_url, str):
            raise ModelConfigError("base_url must be a string")
        _validate_base_url(base_url)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ModelConfigError("request_timeout_seconds must be numeric")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 1 or timeout > 300:
            raise ModelConfigError("request_timeout_seconds must be between 1 and 300")
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ModelConfigError("reasoning_effort is not supported")
        if agent_backend not in {"responses", "codex-app-server"}:
            raise ModelConfigError("agent_backend must be responses or codex-app-server")
        if not isinstance(dynamic_tools, bool):
            raise ModelConfigError("codex_dynamic_tools must be boolean")
        if dynamic_tools and agent_backend != "codex-app-server":
            raise ModelConfigError("codex_dynamic_tools requires agent_backend=codex-app-server")

        warnings: List[str] = []
        api_key = ""
        if auth_path.exists() or agent_backend == "responses":
            if not auth_path.is_file() or auth_path.is_symlink():
                raise ModelConfigError("model_api/auth.json is missing or not a regular file")
            try:
                auth = json.loads(_bounded_text(auth_path, AUTH_LIMIT, "auth.json"))
            except json.JSONDecodeError as exc:
                raise ModelConfigError("model_api/auth.json must be valid JSON") from exc
            if not isinstance(auth, dict) or set(auth) not in ({"api_key"}, {"OPENAI_API_KEY"}):
                raise ModelConfigError("auth.json must contain only api_key or OPENAI_API_KEY")
            api_key = auth.get("api_key", auth.get("OPENAI_API_KEY"))
            if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
                raise ModelConfigError("auth.json api_key must be a non-empty string")
            api_key = api_key.strip()
            if os.name == "posix" and stat.S_IMODE(auth_path.stat().st_mode) & 0o077:
                warnings.append("model_api/auth.json should be readable only by its owner (chmod 600)")
        return cls(
            model,
            base_url.rstrip("/"),
            timeout,
            effort,
            api_key,
            tuple(warnings),
            bool(values.get("store_responses", True)),
            agent_backend,
            dynamic_tools,
        )


def _bounded_text(path: Path, limit: int, label: str) -> str:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ModelConfigError(f"model_api/{label} is too large")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelConfigError(f"model_api/{label} must be UTF-8") from exc


def _parse_toml_document(text: str) -> Dict[str, Any]:
    """Parse the small TOML subset needed by the legacy and Codex profiles."""
    values: Dict[str, Any] = {}
    assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")
    table: Optional[str] = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_toml_comment(raw_line).strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = re.fullmatch(r"\[model_providers\.([A-Za-z0-9_.-]+)\]", line)
            if not match:
                raise ModelConfigError(f"unsupported config.toml syntax on line {line_number}")
            table = "model_providers." + match.group(1)
            continue
        match = assignment.fullmatch(line)
        if not match:
            raise ModelConfigError(f"unsupported config.toml syntax on line {line_number}")
        key, raw_value = match.groups()
        full_key = f"{table}.{key}" if table else key
        if full_key in values:
            raise ModelConfigError(f"duplicate config key: {full_key}")
        try:
            value = _parse_toml_value(raw_value)
        except (SyntaxError, ValueError) as exc:
            raise ModelConfigError(f"invalid config value for {key}") from exc
        if not _valid_config_value(value):
            raise ModelConfigError(f"unsupported config value for {key}")
        values[full_key] = value
    return values


def _strip_toml_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, char in enumerate(line):
        if char == "\\" and quoted and not escaped:
            escaped = True
            continue
        if char == '"' and not escaped:
            quoted = not quoted
        if char == "#" and not quoted:
            return line[:index]
        escaped = False
    return line


def _parse_toml_value(raw_value: str) -> Any:
    value = raw_value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    return ast.literal_eval(value)


def _valid_config_value(value: Any) -> bool:
    if isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(isinstance(item, str) for item in value)
    return False


def _normalise_config(values: Dict[str, Any]) -> Dict[str, Any]:
    """Convert either supported on-disk profile into the runtime profile."""
    if "model_provider" not in values and not any(
        key.startswith("model_providers.") for key in values
    ):
        unknown = set(values) - ALLOWED_CONFIG_KEYS
        if unknown:
            raise ModelConfigError("unknown config keys: " + ", ".join(sorted(unknown)))
        missing = REQUIRED_CONFIG_KEYS - set(values)
        if missing:
            raise ModelConfigError("missing config keys: " + ", ".join(sorted(missing)))
        result = dict(values)
        result["store_responses"] = True
        result.setdefault("agent_backend", "responses")
        result.setdefault("codex_dynamic_tools", False)
        return result

    unknown = {
        key
        for key in values
        if "." not in key and key not in CODEX_CONFIG_KEYS
    }
    if unknown:
        raise ModelConfigError("unknown config keys: " + ", ".join(sorted(unknown)))
    provider = values.get("model_provider")
    if not isinstance(provider, str) or not provider or not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
        raise ModelConfigError("model_provider must be a provider identifier")
    prefix = "model_providers." + provider + "."
    provider_values = {
        key[len(prefix) :]: value
        for key, value in values.items()
        if key.startswith(prefix)
    }
    other_provider_tables = {
        key.split(".", 2)[1]
        for key in values
        if key.startswith("model_providers.") and not key.startswith(prefix)
    }
    if other_provider_tables:
        raise ModelConfigError("config contains an unused model provider")
    unknown_provider = set(provider_values) - CODEX_PROVIDER_KEYS
    if unknown_provider:
        raise ModelConfigError(
            "unknown provider keys: " + ", ".join(sorted(unknown_provider))
        )
    if "base_url" not in provider_values or "wire_api" not in provider_values:
        raise ModelConfigError("model provider requires base_url and wire_api")
    if provider_values["wire_api"] != "responses":
        raise ModelConfigError("model provider wire_api must be responses")
    if "name" in provider_values and provider_values["name"] != provider:
        raise ModelConfigError("model provider name does not match its table")
    if "requires_openai_auth" in provider_values and not isinstance(
        provider_values["requires_openai_auth"], bool
    ):
        raise ModelConfigError("requires_openai_auth must be boolean")

    model = values.get("model")
    effort = values.get("model_reasoning_effort")
    if isinstance(model, str):
        model = model.strip()
    if isinstance(effort, str):
        effort = effort.strip()
    if not isinstance(model, str) or not isinstance(effort, str):
        raise ModelConfigError("Codex config requires model and model_reasoning_effort")
    if "review_model" in values and (
        not isinstance(values["review_model"], str)
        or not MODEL_RE.fullmatch(values["review_model"].strip())
    ):
        raise ModelConfigError("review_model must be a short model identifier")
    if "disable_response_storage" in values and not isinstance(
        values["disable_response_storage"], bool
    ):
        raise ModelConfigError("disable_response_storage must be boolean")
    if "network_access" in values and not isinstance(values["network_access"], str):
        raise ModelConfigError("network_access must be a string")
    if "windows_wsl_setup_acknowledged" in values and not isinstance(
        values["windows_wsl_setup_acknowledged"], bool
    ):
        raise ModelConfigError("windows_wsl_setup_acknowledged must be boolean")
    if "notify" in values and not isinstance(values["notify"], list):
        raise ModelConfigError("notify must be an array of strings")
    return {
        "model": model,
        "base_url": provider_values["base_url"],
        "request_timeout_seconds": values.get("request_timeout_seconds", 60),
        "reasoning_effort": effort,
        "store_responses": not bool(values.get("disable_response_storage", False)),
        "agent_backend": values.get("agent_backend", "responses"),
        "codex_dynamic_tools": values.get("codex_dynamic_tools", False),
    }


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelConfigError("base_url must not include credentials, query, or fragment")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ModelConfigError("base_url must use HTTPS (HTTP is allowed only for loopback tests)")
    if not parsed.netloc:
        raise ModelConfigError("base_url must include a host")


def _safe_transport_detail(error: Any, api_key: str) -> str:
    label = type(error).__name__
    detail = " ".join(str(error).split())
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    detail = detail[:240]
    return f"{label}: {detail}" if detail else label


class OpenAIResponsesClient:
    backend_name = "responses"
    stateful = False

    def __init__(self, config: ModelApiConfig, opener: Any = urllib.request.urlopen):
        self.config = config
        self._opener = opener
        self._cycle_instructions = ""

    def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url + "/" + path.lstrip("/"),
            data=encoded,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Some gateways reject Python's default Python-urllib user agent.
                # Identify the application without pretending to be a browser.
                "User-Agent": CLIENT_USER_AGENT,
            },
        )
        try:
            timeout = request_timeout_seconds or self.config.request_timeout_seconds
            response = self._opener(request, timeout=timeout)
            with response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read(64 * 1024).decode("utf-8"))
                detail = _http_error_detail(body, self.config.api_key)
            except Exception:
                pass
            suffix = f": {detail}" if detail else ""
            raise ModelApiError(f"Responses API returned HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ModelApiError(
                    f"Responses API request timed out (configured timeout {timeout:g}s)"
                ) from exc
            detail = _safe_transport_detail(reason, self.config.api_key)
            raise ModelApiError(f"Responses API connection failed: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelApiError(
                f"Responses API request timed out (configured timeout {timeout:g}s)"
            ) from exc
        except OSError as exc:
            detail = _safe_transport_detail(exc, self.config.api_key)
            raise ModelApiError(f"Responses API connection failed: {detail}") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise ModelApiError("Responses API response is too large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelApiError("Responses API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ModelApiError("Responses API returned an invalid object")
        return value
    def create_conversation(self, meeting_id: str) -> str:
        """Return a local context handle without requiring /conversations.

        Some Responses-compatible gateways expose only POST /responses. The
        harness keeps the durable context in events.jsonl and sends it with
        every request, so a remote Conversation resource is optional.
        """
        if not isinstance(meeting_id, str) or not meeting_id:
            raise ModelApiError("meeting id is required for a local response context")
        return "local-responses-" + meeting_id

    def start_cycle(
        self,
        meeting_id: str,
        instructions: str,
        initial_context: str,
        *,
        model: Optional[str] = None,
        event_callback: Any = None,
    ) -> str:
        del initial_context, model
        self._cycle_instructions = instructions
        session_id = self.create_conversation(meeting_id)
        if event_callback:
            event_callback(
                {
                    "backend": self.backend_name,
                    "event": "cycle_started",
                    "session_id": session_id,
                    "status": "ready",
                }
            )
        return session_id

    def next_action(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Any = None,
    ) -> AgentResponse:
        del action_handler
        return self.create_action(
            session_id,
            self._cycle_instructions,
            observation,
            model=model,
            allowed_types=allowed_types,
            allowed_message_types=allowed_message_types,
            request_timeout_seconds=request_timeout_seconds,
        )

    def interrupt(self) -> None:
        # The standard-library Responses request is not cancellable. The
        # runner still discards its result using the durable generation check.
        return None

    def close(self) -> None:
        return None

    def create_action(
        self,
        conversation_id: str,
        instructions: str,
        observation: str,
        *,
        model: Optional[str] = None,
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
        request_timeout_seconds: Optional[float] = None,
    ) -> AgentResponse:
        schema = action_json_schema(allowed_types, allowed_message_types)
        payload = {
            "model": model or self.config.model,
            "instructions": instructions,
            # A plain input string is accepted by both the official Responses
            # API and smaller compatible gateways.
            "input": observation,
            "max_output_tokens": 4096,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "remotebuddy_action",
                    "strict": True,
                    "schema": schema,
                }
            },
            "store": self.config.store_responses,
        }
        # Do not send a Conversation field: the configured gateway only
        # implements Responses. The caller supplies durable local context in
        # the observation packet instead.
        value = self._post("responses", payload, request_timeout_seconds)
        response_status = value.get("status")
        if response_status not in {None, "completed"}:
            raise ModelApiError("Responses API did not complete the requested action")
        response_id = value.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ModelApiError("Responses API did not return a response id")
        text = _response_output_text(value)
        # Decode the action structurally here. The Runner validates it against
        # the latest terminal phase immediately before execution; treating a
        # stale phase choice as an API failure would make a recoverable model
        # decision end the entire collaboration cycle.
        action = decode_action_text(text)
        return AgentResponse(response_id, action)


def _http_error_detail(body: Any, api_key: str) -> str:
    """Extract common gateway error shapes without exposing credentials."""
    if not isinstance(body, dict):
        return ""
    nested = body.get("error")
    candidates = []
    if isinstance(nested, dict):
        candidates.extend([nested.get("code"), nested.get("message")])
    candidates.extend([body.get("code"), body.get("message")])
    parts = [str(value).strip() for value in candidates if isinstance(value, str) and value.strip()]
    detail = ": ".join(dict.fromkeys(parts))
    return detail[:500].replace(api_key, "[redacted]")


def _response_output_text(value: Dict[str, Any]) -> str:
    direct = value.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    pieces: List[str] = []
    for item in value.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    pieces.append(text)
    if not pieces:
        raise ModelResponseError("Responses API returned no action text")
    return "".join(pieces)
