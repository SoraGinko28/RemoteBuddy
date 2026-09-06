"""discriminator_harness — an independent harness for reviewer (non-generator)
participants.

Design (per MEETING_ADT object layer): agents may come from different
harnesses/service providers; they only converge in MeetingData. The generator
uses the terminal-bound Claude-Code action harness. The discriminator must
NOT touch the shared terminal, so this module runs it against a plain
chat endpoint (no tools, no local terminal bridge) — a separate harness that
still writes into the same journal/MeetingData under its own agent name.

The endpoint is pluggable (``_post_messages``) so tests inject a fake without
network.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import meeting_v2 as m2
from meeting_reviewer import REVIEWER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS_RETRY, build_reviewer_context

REPO = Path(__file__).resolve().parent
MODEL_API = REPO / "model_api"
AUTH_FILE = MODEL_API / "auth.json"
CONFIG_FILE = MODEL_API / "config.toml"

DEFAULT_PROVIDER = "deepseek"  # anthropic-messages wire, no tools
MAX_TOKENS = 2000
TIMEOUT_SECONDS = 180.0

PERSONA = (
    "You are a reviewer (discriminator) in a shared-terminal coding session. "
    "You have no terminal and no tools: you only read what is given to you and "
    "answer in plain prose, in the same language as the human. Give a concise "
    "review: what looks right, what looks wrong, and one sharp question if "
    "anything is unclear. Do not prefix your reply with labels."
)


def _read_provider_config() -> dict:
    import tomllib

    with CONFIG_FILE.open("rb") as fh:
        config = tomllib.load(fh)
    providers = config.get("providers", {})
    provider = providers.get(DEFAULT_PROVIDER)
    if not isinstance(provider, dict):
        raise m2.MeetingError(f"discriminator harness: provider {DEFAULT_PROVIDER!r} not configured")
    return dict(provider)


def _read_api_key() -> str:
    try:
        auth = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise m2.MeetingError(f"discriminator harness: cannot read auth.json: {exc}") from exc
    key = auth.get("DEEPSEEK_API_KEY") if DEFAULT_PROVIDER == "deepseek" else auth.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise m2.MeetingError("discriminator harness: API key is missing")
    return key.strip()


def _post_messages(payload: dict, base_url: str, api_key: str,
                   timeout: float = TIMEOUT_SECONDS) -> dict:
    """One plain chat completion against an anthropic-messages wire endpoint.

    Injectable in tests by monkeypatching this module function.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise m2.MeetingError(
            f"discriminator harness: HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise m2.MeetingError(f"discriminator harness: request failed: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise m2.MeetingError("discriminator harness: non-JSON response") from exc
    return parsed


def _extract_text(response: dict) -> str:
    for block in response.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise m2.MeetingError("discriminator harness: no text content in response")


def _record(store: m2.MeetingStore, agent_name: str, phase: str,
            data: dict, content: str = "") -> None:
    try:
        store.append_event("state", agent_name, phase, data={"reviewer": data},
                           content=content)
    except Exception:  # noqa: BLE001
        pass


def comment_once(
    store: m2.MeetingStore,
    agent_name: str,
    *,
    retry: bool = False,
    post: Any = None,
    persona: str = PERSONA,
) -> bool:
    """Run one plain-chat reviewer comment and record it under agent_name.

    Returns True when a comment was recorded. Never raises: failures are
    recorded as error events and the meeting is restored to waiting.
    """
    guard = threading.RLock()
    post = post or _post_messages

    def settled() -> None:
        try:
            with guard:
                store.refresh()
                if store.meeting.phase == "agent_thinking":
                    store.update(phase="waiting_for_user", control_owner="none",
                                 pending_action_id=None)
        except Exception:  # noqa: BLE001
            pass

    try:
        provider = _read_provider_config()
        model = provider.get("model") or "deepseek-v4-flash"
        base_url = provider.get("base_url") or "https://api.deepseek.com/anthropic"
        api_key = _read_api_key()
        instructions = REVIEWER_INSTRUCTIONS_RETRY if retry else REVIEWER_INSTRUCTIONS
        context = build_reviewer_context(store)
        system = f"{persona}\n\n{instructions}"
        user = f"Conversation and latest screen:\n{context}"
        with guard:
            store.refresh()
            store.update(phase="agent_thinking", control_owner="none")
            store.append_event(
                "action", agent_name, "agent_thinking",
                data={"action_id": f"reviewer-{agent_name}", "status": "received",
                      "harness": "discriminator-plain-chat"},
            )
            _record(store, agent_name, store.meeting.phase, {"status": "started"})
        response = post(
            {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            base_url,
            api_key,
        )
        comment = _extract_text(response)
        if len(comment) < 12:
            raise m2.MeetingError("discriminator harness: response too short to be a comment")
        with guard:
            store.refresh()
            if store.meeting.status in {"completed", "failed", "interrupted"}:
                return False
            store.append_chat(agent_name, comment, "final")
            store.append_event(
                "action", agent_name, "agent_acting",
                data={"action_id": f"reviewer-{agent_name}", "status": "completed",
                      "harness": "discriminator-plain-chat"},
            )
            store.update(phase="waiting_for_user", control_owner="none",
                         pending_action_id=None)
            _record(store, agent_name, store.meeting.phase, {"status": "comment"})
        return True
    except m2.MeetingError as exc:
        try:
            with guard:
                store.refresh()
                store.append_event("error", "harness", "reviewer_error", content=str(exc))
        except Exception:  # noqa: BLE001
            pass
        settled()
        return False
    except Exception as exc:  # noqa: BLE001
        try:
            with guard:
                store.refresh()
                store.append_event(
                    "error", "harness", "reviewer_error",
                    content=f"unexpected: {exc!r}",
                )
        except Exception:  # noqa: BLE001
            pass
        settled()
        return False
