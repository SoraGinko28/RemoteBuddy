from __future__ import annotations

import ast
import json
import math
import os
import random
import re
import socket
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
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
    "agent_provider",
    "codex_dynamic_tools",
    "fast_model",
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
    "agent_provider",
    "codex_dynamic_tools",
}
CODEX_PROVIDER_KEYS = {"name", "base_url", "wire_api", "requires_openai_auth"}
CANONICAL_TOP_LEVEL_KEYS = {
    "default_backend",
    "default_provider",
    "model",
    "base_url",
    "request_timeout_seconds",
    "reasoning_effort",
    "store_responses",
    "agent_backend",
    "agent_provider",
    "codex_dynamic_tools",
}
CANONICAL_PROVIDER_KEYS = {
    "name",
    "base_url",
    "wire_api",
    "auth_key",
    "model",
    "fast_model",
    "requires_auth",
    "requires_openai_auth",
}
CANONICAL_HARNESS_KEYS = {"binary", "min_version", "restricted"}
SUPPORTED_BACKENDS = {"responses", "codex-app-server", "claude-code"}
SUPPORTED_PROVIDERS = {"openai", "deepseek"}
# DeepSeek's long-context model uses a literal ``[1m]`` suffix.
MODEL_RE = re.compile(r"^(?=.{1,200}$)[A-Za-z0-9][A-Za-z0-9._-]*(?:\[1m\])?$")


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
    agent_provider: str = "openai"
    fast_model: Optional[str] = None
    provider_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    harness_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)
    auth_keys: Mapping[str, str] = field(default_factory=dict, repr=False)
    claude_binary: str = "claude"

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
        agent_provider = values.get("agent_provider", "openai")
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
        if agent_backend not in SUPPORTED_BACKENDS:
            raise ModelConfigError("agent_backend is not supported")
        if not isinstance(agent_provider, str) or agent_provider not in SUPPORTED_PROVIDERS:
            raise ModelConfigError("agent_provider is not supported")
        if not isinstance(dynamic_tools, bool):
            raise ModelConfigError("codex_dynamic_tools must be boolean")
        if dynamic_tools and agent_backend != "codex-app-server":
            raise ModelConfigError("codex_dynamic_tools requires agent_backend=codex-app-server")

        provider_profiles = values.get("provider_profiles", {})
        harness_profiles = values.get("harness_profiles", {})
        if not isinstance(provider_profiles, dict) or not isinstance(harness_profiles, dict):
            raise ModelConfigError("provider or harness profiles are invalid")

        warnings: List[str] = []
        # A missing auth.json is tolerated at load time so key-less installs
        # (for example codex-app-server via the local CLI login) stay usable;
        # resolve() enforces the key for the combination actually selected.
        auth = _read_auth_file(auth_path, required=False)
        if auth_path.is_file() and os.name == "posix" and stat.S_IMODE(auth_path.stat().st_mode) & 0o077:
            warnings.append("model_api/auth.json should be readable only by its owner (chmod 600)")
        auth_keys = dict(auth)
        auth_name = str(values.get("auth_key") or _default_auth_key(agent_provider))
        api_key = _select_auth_key(auth_keys, auth_name)
        if (agent_backend == "responses" or agent_provider == "deepseek") and not api_key:
            warnings.append(
                f"auth.json is missing {auth_name}; the {agent_backend} backend cannot start until it is saved"
            )
        harness_profile = harness_profiles.get(agent_backend, {})
        if not isinstance(harness_profile, dict):
            harness_profile = {}
        claude_binary = harness_profile.get("binary", "claude")
        if not isinstance(claude_binary, str) or not claude_binary.strip():
            raise ModelConfigError("claude-code binary must be a non-empty string")
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
            agent_provider,
            values.get("fast_model"),
            provider_profiles,
            harness_profiles,
            auth_keys,
            claude_binary.strip(),
        )

    def resolve(
        self,
        agent_backend: Optional[str] = None,
        agent_provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "ModelApiConfig":
        """Resolve a stored backend/provider pair without changing legacy callers."""
        backend = agent_backend or self.agent_backend
        provider = agent_provider or self.agent_provider
        if backend not in SUPPORTED_BACKENDS:
            raise ModelConfigError("agent_backend is not supported")
        if provider not in SUPPORTED_PROVIDERS:
            raise ModelConfigError("agent_provider is not supported")
        if backend == "claude-code" and provider != "deepseek":
            raise ModelConfigError("claude-code currently requires agent_provider=deepseek")
        profile = self.provider_profiles.get(provider, {})
        if not isinstance(profile, Mapping):
            profile = {}
        # An explicit empty model on codex-app-server means "let the Codex CLI
        # use its own configured default" — required for ChatGPT-plan logins
        # that may not have access to a hard-coded model id.
        if backend == "codex-app-server" and model == "":
            selected_model = ""
        else:
            selected_model = model or profile.get("model") or (self.model if provider == self.agent_provider else None)
        selected_base_url = profile.get("base_url") or (self.base_url if provider == self.agent_provider else None)
        if provider == "deepseek":
            selected_model = selected_model or "deepseek-v4-pro[1m]"
            selected_base_url = selected_base_url or "https://api.deepseek.com/anthropic"
        if not (backend == "codex-app-server" and selected_model == ""):
            if not isinstance(selected_model, str) or not MODEL_RE.fullmatch(selected_model):
                raise ModelConfigError("model must be a short model identifier")
        if not isinstance(selected_base_url, str):
            raise ModelConfigError("provider requires base_url")
        _validate_base_url(selected_base_url)
        auth_name = str(profile.get("auth_key") or _default_auth_key(provider))
        api_key = _select_auth_key(self.auth_keys, auth_name)
        if (backend == "responses" or provider == "deepseek") and not api_key:
            raise ModelConfigError(f"auth.json is missing {auth_name}")
        dynamic = self.codex_dynamic_tools if backend == "codex-app-server" else False
        harness = self.harness_profiles.get(backend, {})
        binary = harness.get("binary", self.claude_binary if backend == "claude-code" else "claude") if isinstance(harness, Mapping) else "claude"
        return replace(
            self,
            model=selected_model,
            base_url=selected_base_url.rstrip("/"),
            api_key=api_key,
            agent_backend=backend,
            agent_provider=provider,
            fast_model=profile.get("fast_model") if isinstance(profile, Mapping) else None,
            codex_dynamic_tools=dynamic,
            claude_binary=str(binary),
        )


def _default_auth_key(provider: str) -> str:
    return "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"


def _read_auth_file(path: Path, *, required: bool) -> Dict[str, str]:
    if not path.exists():
        if required:
            raise ModelConfigError("model_api/auth.json is missing or not a regular file")
        return {}
    if not path.is_file() or path.is_symlink():
        raise ModelConfigError("model_api/auth.json is missing or not a regular file")
    try:
        payload = json.loads(_bounded_text(path, AUTH_LIMIT, "auth.json"))
    except json.JSONDecodeError as exc:
        raise ModelConfigError("model_api/auth.json must be valid JSON") from exc
    if not isinstance(payload, dict) or not set(payload) <= {"api_key", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"}:
        raise ModelConfigError("auth.json must contain only api_key, OPENAI_API_KEY, or DEEPSEEK_API_KEY")
    if "api_key" in payload and len(payload) > 1:
        # Keep the legacy alias unambiguous; dedicated provider keys may coexist.
        raise ModelConfigError("auth.json must contain only api_key or dedicated provider keys")
    result: Dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ModelConfigError(f"auth.json {key} must be a non-empty string")
        result[key] = value.strip()
    return result


def _select_auth_key(auth: Mapping[str, str], name: str) -> str:
    return str(auth.get(name) or auth.get("api_key") or "")


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
    """Parse the bounded TOML subset used by legacy and provider profiles."""
    values: Dict[str, Any] = {}
    assignment = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")
    table: Optional[str] = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_toml_comment(raw_line).strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = re.fullmatch(r"\[(model_providers|providers|harnesses)\.([A-Za-z0-9_.-]+)\]", line)
            if not match:
                raise ModelConfigError(f"unsupported config.toml syntax on line {line_number}")
            table = match.group(1) + "." + match.group(2)
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
    """Convert legacy, Codex-style, or canonical provider profiles."""
    if any(key.startswith("providers.") or key.startswith("harnesses.") for key in values) or any(
        key in values for key in ("default_backend", "default_provider")
    ):
        return _normalise_canonical_config(values)
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
        result.setdefault("agent_provider", "openai")
        result.setdefault("codex_dynamic_tools", False)
        result["provider_profiles"] = {
            "openai": {
                "model": result["model"],
                "base_url": result["base_url"],
                "wire_api": "responses",
                "auth_key": "OPENAI_API_KEY",
            }
        }
        result["harness_profiles"] = {}
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
        "agent_provider": values.get("agent_provider", "openai"),
        "codex_dynamic_tools": values.get("codex_dynamic_tools", False),
        "provider_profiles": {
            "openai": {
                "model": model,
                "base_url": provider_values["base_url"],
                "wire_api": "responses",
                "auth_key": "OPENAI_API_KEY",
            }
        },
        "harness_profiles": {},
    }


def _normalise_canonical_config(values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and flatten the provider/harness catalog format."""
    unknown = {
        key for key in values
        if "." not in key and key not in CANONICAL_TOP_LEVEL_KEYS
    }
    if unknown:
        raise ModelConfigError("unknown config keys: " + ", ".join(sorted(unknown)))

    provider_profiles: Dict[str, Dict[str, Any]] = {}
    for key, value in values.items():
        if not key.startswith("providers."):
            continue
        parts = key.split(".", 2)
        if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            raise ModelConfigError("provider profile identifier is invalid")
        provider_profiles.setdefault(parts[1], {})[parts[2]] = value
    for provider, profile in provider_profiles.items():
        unknown_profile = set(profile) - CANONICAL_PROVIDER_KEYS
        if unknown_profile:
            raise ModelConfigError(
                f"unknown provider keys: {', '.join(sorted(unknown_profile))}"
            )
        if "name" in profile and profile["name"] != provider:
            raise ModelConfigError("provider name does not match its table")
        if "base_url" in profile:
            if not isinstance(profile["base_url"], str):
                raise ModelConfigError("provider base_url must be a string")
            _validate_base_url(profile["base_url"])
        if "wire_api" in profile and profile["wire_api"] not in {"responses", "anthropic"}:
            raise ModelConfigError("provider wire_api is not supported")
        for model_key in ("model", "fast_model"):
            if model_key in profile and (
                not isinstance(profile[model_key], str)
                or not MODEL_RE.fullmatch(profile[model_key])
            ):
                raise ModelConfigError(f"provider {model_key} must be a short model identifier")
        if "auth_key" in profile and (
            not isinstance(profile["auth_key"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", profile["auth_key"])
        ):
            raise ModelConfigError("provider auth_key is invalid")
        for bool_key in ("requires_auth", "requires_openai_auth"):
            if bool_key in profile and not isinstance(profile[bool_key], bool):
                raise ModelConfigError(f"provider {bool_key} must be boolean")

    harness_profiles: Dict[str, Dict[str, Any]] = {}
    for key, value in values.items():
        if not key.startswith("harnesses."):
            continue
        parts = key.split(".", 2)
        if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            raise ModelConfigError("harness profile identifier is invalid")
        harness_profiles.setdefault(parts[1], {})[parts[2]] = value
    for harness, profile in harness_profiles.items():
        unknown_profile = set(profile) - CANONICAL_HARNESS_KEYS
        if unknown_profile:
            raise ModelConfigError(
                f"unknown harness keys: {', '.join(sorted(unknown_profile))}"
            )
        if "binary" in profile and (
            not isinstance(profile["binary"], str) or not profile["binary"].strip()
        ):
            raise ModelConfigError("harness binary must be a non-empty string")
        if "restricted" in profile and not isinstance(profile["restricted"], bool):
            raise ModelConfigError("harness restricted must be boolean")

    backend = values.get("default_backend", values.get("agent_backend", "responses"))
    provider = values.get("default_provider", values.get("agent_provider"))
    if provider is None:
        provider = "deepseek" if backend == "claude-code" else "openai"
    if backend not in SUPPORTED_BACKENDS:
        raise ModelConfigError("agent_backend is not supported")
    if provider not in SUPPORTED_PROVIDERS:
        raise ModelConfigError("agent_provider is not supported")
    selected = provider_profiles.get(provider, {})
    model = values.get("model", selected.get("model"))
    base_url = values.get("base_url", selected.get("base_url"))
    if provider == "deepseek":
        model = model or "deepseek-v4-pro[1m]"
        base_url = base_url or "https://api.deepseek.com/anthropic"
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise ModelConfigError("model must be a short model identifier")
    if not isinstance(base_url, str):
        raise ModelConfigError("provider requires base_url")
    _validate_base_url(base_url)
    timeout = values.get("request_timeout_seconds", 120)
    effort = values.get("reasoning_effort", "low")
    if isinstance(effort, str):
        effort = effort.strip()
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ModelConfigError("request_timeout_seconds must be numeric")
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ModelConfigError("reasoning_effort is not supported")
    if "store_responses" in values and not isinstance(values["store_responses"], bool):
        raise ModelConfigError("store_responses must be boolean")
    dynamic = values.get("codex_dynamic_tools", False)
    if not isinstance(dynamic, bool):
        raise ModelConfigError("codex_dynamic_tools must be boolean")
    if dynamic and backend != "codex-app-server":
        raise ModelConfigError("codex_dynamic_tools requires agent_backend=codex-app-server")
    # Expose selected profile fields in the flattened runtime object.
    selected_profile = dict(selected)
    selected_profile.setdefault("model", model)
    selected_profile.setdefault("base_url", base_url)
    selected_profile.setdefault("auth_key", _default_auth_key(provider))
    provider_profiles[provider] = selected_profile
    return {
        "model": model,
        "base_url": base_url,
        "request_timeout_seconds": timeout,
        "reasoning_effort": effort,
        "store_responses": values.get("store_responses", True),
        "agent_backend": backend,
        "agent_provider": provider,
        "codex_dynamic_tools": dynamic,
        "fast_model": selected_profile.get("fast_model"),
        "provider_profiles": provider_profiles,
        "harness_profiles": harness_profiles,
        "auth_key": selected_profile.get("auth_key"),
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


class _RetryableApiError(ModelApiError):
    """Internal marker for transient transport failures worth one more try."""

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _retry_after_seconds(headers: Any) -> Optional[float]:
    try:
        value = headers.get("Retry-After") if headers is not None else None
        seconds = float(value)
        return seconds if 0 <= seconds <= 120 else None
    except (TypeError, ValueError):
        return None


# Reasoning tokens share the output budget, so higher effort needs headroom.
_EFFORT_OUTPUT_BUDGET = {
    "none": 4096,
    "low": 8192,
    "medium": 16384,
    "high": 32768,
    "xhigh": 49152,
    "max": 65536,
}


class OpenAIResponsesClient:
    backend_name = "responses"
    stateful = False

    # HTTP statuses worth a bounded retry: throttling and gateway hiccups.
    RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
    MAX_TRANSPORT_RETRIES = 2

    def __init__(
        self,
        config: ModelApiConfig,
        opener: Any = urllib.request.urlopen,
        retry_base_seconds: float = 0.5,
    ):
        self.config = config
        self._opener = opener
        self._retry_base_seconds = retry_base_seconds
        self._cycle_instructions = ""

    def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        request_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        timeout = request_timeout_seconds or self.config.request_timeout_seconds
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            attempt_timeout = timeout if attempt == 1 else max(0.1, deadline - time.monotonic())
            try:
                return self._post_once(path, payload, attempt_timeout)
            except _RetryableApiError as exc:
                remaining = deadline - time.monotonic()
                if attempt > self.MAX_TRANSPORT_RETRIES or remaining <= 0.5:
                    raise ModelApiError(str(exc)) from exc
                delay = exc.retry_after_seconds
                if delay is None:
                    delay = self._retry_base_seconds * (2 ** (attempt - 1))
                    delay += random.uniform(0, self._retry_base_seconds / 2)
                time.sleep(max(0.0, min(delay, remaining - 0.25)))

    def _post_once(self, path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
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
            message = f"Responses API returned HTTP {exc.code}{suffix}"
            if exc.code in self.RETRYABLE_HTTP_STATUSES:
                raise _RetryableApiError(message, _retry_after_seconds(exc.headers)) from exc
            raise ModelApiError(message) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                # The timeout consumed the request budget; retrying with the
                # remaining slice would only produce a second, shorter timeout.
                raise ModelApiError(
                    f"Responses API request timed out (configured timeout {timeout:g}s)"
                ) from exc
            detail = _safe_transport_detail(reason, self.config.api_key)
            raise _RetryableApiError(f"Responses API connection failed: {detail}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelApiError(
                f"Responses API request timed out (configured timeout {timeout:g}s)"
            ) from exc
        except OSError as exc:
            detail = _safe_transport_detail(exc, self.config.api_key)
            raise _RetryableApiError(f"Responses API connection failed: {detail}") from exc
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
        effort = self.config.reasoning_effort
        payload = {
            "model": model or self.config.model,
            "instructions": instructions,
            # A plain input string is accepted by both the official Responses
            # API and smaller compatible gateways.
            "input": observation,
            "max_output_tokens": _EFFORT_OUTPUT_BUDGET.get(effort, 4096),
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
        if effort != "none":
            payload["reasoning"] = {"effort": effort}
        # Do not send a Conversation field: the configured gateway only
        # implements Responses. The caller supplies durable local context in
        # the observation packet instead.
        value = self._post("responses", payload, request_timeout_seconds)
        response_status = value.get("status")
        if response_status == "incomplete":
            details = value.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            raise ModelApiError(
                "Responses API did not complete the requested action "
                f"(incomplete: {reason or 'unknown reason'})"
            )
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
