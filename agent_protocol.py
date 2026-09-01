from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, runtime_checkable


SPECIAL_KEYS = {
    "Enter",
    "Escape",
    "Tab",
    "Up",
    "Down",
    "Left",
    "Right",
    "Backspace",
    "Delete",
    "C-c",
}
ACTION_TYPES = {"chat", "command", "vim_keys", "terminal_keys", "observe", "done"}
REFERENCE_KINDS = {"chat", "screen"}
# Mirrors meeting_v2.MAX_CHAT_REFERENCES without importing the storage layer.
MAX_ACTION_REFERENCES = 32


class AgentBackendError(RuntimeError):
    pass


class AgentInterrupted(AgentBackendError):
    """The active model turn was deliberately interrupted by the harness."""


class AgentProtocolError(AgentBackendError):
    pass


@dataclass(frozen=True)
class AgentAction:
    type: str
    message_type: str
    content: str
    command: str
    path: str
    keys: Sequence[Dict[str, str]]
    delay_ms: int
    references: Sequence[Dict[str, Any]] = ()

    @classmethod
    def from_dict(
        cls,
        payload: Dict[str, Any],
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
    ) -> "AgentAction":
        required = {"type", "message_type", "content", "command", "path", "keys", "delay_ms"}
        if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"references"}:
            raise AgentProtocolError("model action has unexpected fields")
        action_type = payload["type"]
        if action_type not in ACTION_TYPES:
            raise AgentProtocolError("model action type is invalid")
        if allowed_types is not None and action_type not in set(allowed_types):
            raise AgentProtocolError("model action is not allowed in the current phase")
        message_type = payload["message_type"]
        if message_type not in {"none", "ack", "progress", "question", "final"}:
            raise AgentProtocolError("model message type is invalid")
        if allowed_message_types is not None and message_type not in set(allowed_message_types):
            raise AgentProtocolError("model message type is not allowed in the current phase")
        content = payload["content"]
        command = payload["command"]
        path = payload["path"]
        keys = payload["keys"]
        delay_ms = payload["delay_ms"]
        if not all(isinstance(value, str) for value in (content, command, path)):
            raise AgentProtocolError("model action strings are invalid")
        if len(content.encode("utf-8")) > 64 * 1024 or len(path.encode("utf-8")) > 4096:
            raise AgentProtocolError("model action text is too large")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int):
            raise AgentProtocolError("delay_ms must be an integer")
        if not isinstance(keys, list):
            raise AgentProtocolError("keys must be an array")
        keys = normalize_model_key_tokens(keys)
        references = normalize_reference_tokens(payload.get("references", []))

        if action_type == "chat":
            if message_type not in {"ack", "progress", "question"} or not content.strip():
                raise AgentProtocolError("chat requires ack, progress, or question content")
            _require_unused(command, path, keys, delay_ms)
        elif action_type == "done":
            if message_type != "final" or not content.strip():
                raise AgentProtocolError("done requires final content")
            _require_unused(command, path, keys, delay_ms)
        elif references:
            raise AgentProtocolError("only chat and done actions may carry references")
        if action_type == "command":
            if message_type != "none" or content or command not in {"pwd", "cd", "vim", "python"}:
                raise AgentProtocolError("command action is invalid")
            if command == "pwd" and path:
                raise AgentProtocolError("pwd does not accept a path")
            if command in {"cd", "vim", "python"} and not path:
                raise AgentProtocolError(f"{command} requires a path")
            if keys or delay_ms:
                raise AgentProtocolError("command has unrelated fields")
        elif action_type in {"vim_keys", "terminal_keys"}:
            if message_type != "none" or content or command != "none" or path or delay_ms:
                raise AgentProtocolError(f"{action_type} has unrelated fields")
            validate_key_tokens(keys)
        elif action_type == "observe":
            if message_type != "none" or content or command != "none" or path or keys:
                raise AgentProtocolError("observe has unrelated fields")
            if delay_ms < 100 or delay_ms > 2000:
                raise AgentProtocolError("observe delay must be between 100 and 2000 ms")
        return cls(
            action_type,
            message_type,
            content,
            command,
            path,
            tuple(keys),
            delay_ms,
            tuple(references),
        )


def _require_unused(command: str, path: str, keys: Sequence[Any], delay_ms: int) -> None:
    if command != "none" or path or keys or delay_ms:
        raise AgentProtocolError("chat action has unrelated fields")


def normalize_reference_tokens(raw: Any) -> list:
    """Validate the wire shape of content references on chat/done actions.

    Each reference points at one earlier journal event, mirroring the human
    reference schema: ``kind`` is ``chat`` or ``screen``, the source event is
    selected by its ``seq`` (or ``event_id``), and ``region`` is a half-open
    character range into that event's content — or ``null`` to quote the whole
    content. Resolution against the actual journal happens in the runner.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise AgentProtocolError("references must be an array")
    if len(raw) > MAX_ACTION_REFERENCES:
        raise AgentProtocolError(f"references may contain at most {MAX_ACTION_REFERENCES} items")
    normalized = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"kind", "seq", "event_id", "region"}:
            raise AgentProtocolError("reference has unexpected fields")
        kind = item.get("kind")
        if kind not in REFERENCE_KINDS:
            raise AgentProtocolError("reference kind must be chat or screen")
        seq = item.get("seq")
        event_id = item.get("event_id")
        if seq is None and event_id is None:
            raise AgentProtocolError("reference requires the seq or event_id of an earlier event")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
            raise AgentProtocolError("reference seq must be a positive integer")
        if event_id is not None and (
            not isinstance(event_id, str) or not event_id or len(event_id) > 200
        ):
            raise AgentProtocolError("reference event_id must be a short string")
        region = item.get("region")
        if region is not None:
            if not isinstance(region, dict) or set(region) != {"start", "end"}:
                raise AgentProtocolError("reference region must contain start and end")
            start, end = region.get("start"), region.get("end")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
                raise AgentProtocolError("reference region bounds must be integers")
            if start < 0 or end <= start or end > 1_000_000:
                raise AgentProtocolError("reference region must be a bounded half-open range")
        entry: Dict[str, Any] = {"kind": kind, "region": dict(region) if region else None}
        if seq is not None:
            entry["seq"] = seq
        if event_id is not None:
            entry["event_id"] = event_id
        normalized.append(entry)
    return normalized


def validate_key_tokens(tokens: Sequence[Any]) -> None:
    if not tokens or len(tokens) > 128:
        raise AgentProtocolError("vim_keys must contain 1 to 128 tokens")
    total = 0
    for token in tokens:
        if not isinstance(token, dict) or set(token) != {"kind", "value"}:
            raise AgentProtocolError("invalid key token")
        kind, value = token["kind"], token["value"]
        if not isinstance(value, str):
            raise AgentProtocolError("key token value must be text")
        if kind == "literal":
            if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise AgentProtocolError(
                    "literal key text must be one non-empty line without control characters"
                )
            total += len(value.encode("utf-8"))
        elif kind == "key":
            if value not in SPECIAL_KEYS:
                raise AgentProtocolError(f"special key is not allowed: {value}")
        else:
            raise AgentProtocolError("key token kind is invalid")
    if total > 8192:
        raise AgentProtocolError("literal key input exceeds 8192 bytes")


def normalize_model_key_tokens(tokens: Sequence[Any]) -> list:
    """Recover a common schema mistake without widening the key allowlist.

    Some compatible Responses gateways occasionally label a printable Vim
    character (for example ``i``) as a named key. A single printable character
    is always safe to send literally; multi-character or control-like key names
    remain subject to the strict special-key allowlist.
    """
    normalized = []
    for token in tokens:
        if not isinstance(token, dict):
            normalized.append(token)
            continue
        candidate = dict(token)
        value = candidate.get("value")
        if (
            candidate.get("kind") == "key"
            and isinstance(value, str)
            and value not in SPECIAL_KEYS
            and len(value) == 1
            and value.isprintable()
            and value not in {"\n", "\r", "\x00"}
        ):
            candidate["kind"] = "literal"
        if candidate.get("kind") == "literal" and isinstance(candidate.get("value"), str):
            normalized.extend(_expand_escaped_newlines(candidate))
        else:
            normalized.append(candidate)
    return normalized


def _expand_escaped_newlines(token: Dict[str, Any]) -> list:
    """Expand intended line breaks while preserving escapes inside quotes."""
    value = token["value"]
    expanded = []
    buffer = []
    quote = ""
    triple = False
    index = 0

    def line_break() -> None:
        if buffer:
            expanded.append({"kind": "literal", "value": "".join(buffer)})
            buffer.clear()
        expanded.append({"kind": "key", "value": "Enter"})

    while index < len(value):
        if value.startswith("\r\n", index):
            line_break()
            index += 2
            continue
        if value[index] in "\r\n":
            line_break()
            index += 1
            continue

        if quote:
            marker = quote * 3 if triple else quote
            if value.startswith(marker, index):
                buffer.append(marker)
                index += len(marker)
                quote = ""
                triple = False
                continue
            if value[index] == "\\" and index + 1 < len(value):
                buffer.append(value[index : index + 2])
                index += 2
                continue
            buffer.append(value[index])
            index += 1
            continue

        if value.startswith("\\n", index):
            line_break()
            index += 2
            continue
        if value[index] in {"'", '"', "`"}:
            quote = value[index]
            triple = quote != "`" and value.startswith(quote * 3, index)
            marker = quote * 3 if triple else quote
            buffer.append(marker)
            index += len(marker)
            continue
        buffer.append(value[index])
        index += 1

    if buffer:
        expanded.append({"kind": "literal", "value": "".join(buffer)})
    return expanded or [token]


@dataclass(frozen=True)
class AgentResponse:
    response_id: str
    action: AgentAction
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    executed: bool = False


def decode_action_text(
    text: str,
    allowed_types: Optional[Sequence[str]] = None,
    allowed_message_types: Optional[Sequence[str]] = None,
) -> AgentAction:
    """Decode exactly one schema action, allowing identical gateway duplication."""
    remaining = text.strip()
    if not remaining:
        raise AgentProtocolError("model output is not valid action JSON")
    decoder = json.JSONDecoder()
    values = []
    try:
        while remaining:
            value, end = decoder.raw_decode(remaining)
            values.append(value)
            if len(values) > 4:
                raise AgentProtocolError("model output repeated the action too many times")
            remaining = remaining[end:].lstrip()
    except json.JSONDecodeError as exc:
        raise AgentProtocolError("model output is not valid action JSON") from exc
    if any(value != values[0] for value in values[1:]):
        raise AgentProtocolError("model output contains multiple different actions")
    payload = values[0]
    if isinstance(payload, dict) and set(payload) == {"action"}:
        payload = payload["action"]
    return AgentAction.from_dict(payload, allowed_types, allowed_message_types)


BackendEventCallback = Callable[[Dict[str, Any]], None]
DynamicActionHandler = Callable[[AgentAction, Dict[str, Any]], Dict[str, Any]]


@runtime_checkable
class AgentBackend(Protocol):
    backend_name: str
    stateful: bool
    config: Any

    def start_cycle(
        self,
        meeting_id: str,
        instructions: str,
        initial_context: str,
        *,
        model: Optional[str] = None,
        event_callback: Optional[BackendEventCallback] = None,
    ) -> str:
        ...

    def next_action(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Optional[DynamicActionHandler] = None,
    ) -> AgentResponse:
        ...

    def interrupt(self) -> None:
        ...

    def close(self) -> None:
        ...


def action_json_schema(
    allowed_types: Optional[Sequence[str]] = None,
    allowed_message_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    action_types = list(
        allowed_types or ("chat", "command", "vim_keys", "terminal_keys", "observe", "done")
    )
    message_types = set(
        allowed_message_types or ("none", "ack", "progress", "question", "final")
    )
    if set(action_types) - ACTION_TYPES:
        raise ValueError("unsupported action schema type")

    key_item = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "value"],
                "properties": {
                    "kind": {"type": "string", "enum": ["literal"]},
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"^[^\r\n\u0000]+$",
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "value"],
                "properties": {
                    "kind": {"type": "string", "enum": ["key"]},
                    "value": {"type": "string", "enum": sorted(SPECIAL_KEYS)},
                },
            },
        ]
    }
    reference_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "seq", "region"],
        "properties": {
            "kind": {"type": "string", "enum": ["chat", "screen"]},
            "seq": {
                "type": "integer",
                "minimum": 1,
                "description": "seq of the earlier chat or screen event being quoted.",
            },
            "region": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {"type": "integer", "minimum": 0},
                            "end": {"type": "integer", "minimum": 1},
                        },
                    },
                ],
                "description": (
                    "Half-open character range inside the source event content, "
                    "or null to quote the whole content."
                ),
            },
        },
    }
    required = ["type", "message_type", "content", "command", "path", "keys", "delay_ms", "references"]

    def branch(
        action_type: str,
        action_messages: Sequence[str],
        commands: Sequence[str],
        *,
        content: bool = False,
        path: bool = False,
        keys: bool = False,
        delay: Optional[Dict[str, Any]] = None,
        references: bool = False,
    ) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                "type": {"type": "string", "enum": [action_type]},
                "message_type": {"type": "string", "enum": list(action_messages)},
                "content": {"type": "string", **({"minLength": 1} if content else {"enum": [""]})},
                "command": {"type": "string", "enum": list(commands)},
                "path": {
                    "type": "string",
                    "description": "A relative path inside the shared workspace." if path else "Unused.",
                    **({"minLength": 1} if path else {"enum": [""]}),
                },
                "keys": {
                    "type": "array",
                    "items": key_item,
                    **({"minItems": 1, "maxItems": 128} if keys else {"maxItems": 0}),
                },
                "delay_ms": delay or {"type": "integer", "enum": [0]},
                "references": {
                    "type": "array",
                    "items": reference_item,
                    **(
                        {"maxItems": MAX_ACTION_REFERENCES}
                        if references
                        else {"maxItems": 0}
                    ),
                    "description": (
                        "Optional quotes of earlier journal events, identical to human chat references. "
                        "Usually empty."
                        if references
                        else "Unused; must stay empty."
                    ),
                },
            },
        }

    variants = []
    if "chat" in action_types:
        messages = [value for value in ("ack", "progress", "question") if value in message_types]
        if messages:
            variants.append(branch("chat", messages, ("none",), content=True, references=True))
    if "done" in action_types and "final" in message_types:
        variants.append(branch("done", ("final",), ("none",), content=True, references=True))
    if "command" in action_types and "none" in message_types:
        variants.append(branch("command", ("none",), ("pwd",)))
        for command in ("cd", "vim", "python"):
            variants.append(branch("command", ("none",), (command,), path=True))
    if "vim_keys" in action_types and "none" in message_types:
        variants.append(branch("vim_keys", ("none",), ("none",), keys=True))
    if "terminal_keys" in action_types and "none" in message_types:
        variants.append(branch("terminal_keys", ("none",), ("none",), keys=True))
    if "observe" in action_types and "none" in message_types:
        variants.append(
            branch(
                "observe",
                ("none",),
                ("none",),
                delay={"type": "integer", "minimum": 100, "maximum": 2000},
            )
        )
    if not variants:
        raise ValueError("action schema has no valid variants")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {"action": {"anyOf": variants}},
    }
