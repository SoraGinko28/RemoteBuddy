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

Before the reviewer speaks, the meeting's rules are run for it
(``critic_rules.review``): the verdicts go into its context as evidence and are
filed in the meeting as a ``state`` event under its name, so what it says about
a violation is the rules' answer, not a guess.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import meeting_v2 as m2
from meeting_reviewer import REVIEWER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS_RETRY

import critic_rules

REPO = Path(__file__).resolve().parent
MODEL_API = REPO / "model_api"
AUTH_FILE = MODEL_API / "auth.json"
CONFIG_FILE = MODEL_API / "config.toml"

DEFAULT_PROVIDER = "deepseek"  # anthropic-messages wire, no tools
MAX_TOKENS = 2000
TIMEOUT_SECONDS = 180.0
# --------------------------------------------------------------------------
# Rich observation context: the reviewer must be able to tell WHO typed WHAT
# and WHAT actually happened on screen, not just see the latest chat tail.
# --------------------------------------------------------------------------

MAX_SCREEN_FRAMES = 8
FRAME_CHAR_CAP = 1400
TYPING_LINES = 60
CHAT_LINES = 20

def _actor_label(store: m2.MeetingStore, actor: Any) -> str:
    if actor == "human":
        return "human(you)"
    roles = store.meeting.agent_name2role or {}
    role = roles.get(actor) if isinstance(actor, str) else None
    return f"{actor}[{role or 'participant'}]"


def _hms(event: dict) -> str:
    stamp = event.get("captured_at") or ""
    return stamp[11:19] if isinstance(stamp, str) and len(stamp) >= 19 else "??:??:??"


def _decode_hex(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    try:
        raw = bytes.fromhex(text)
    except (ValueError, TypeError):
        return ""
    out = []
    for byte in raw:
        ch = chr(byte)
        out.append(ch if 32 <= byte < 127 or ch in "\n\r\t" else "?")
    return "".join(out).replace("\n", " ")


def _action_summary(data: dict) -> str:
    action = data.get("action")
    if isinstance(action, dict):
        kind = action.get("type", "?")
        if action.get("command"):
            return f"type:command {action.get('command')[:80]}"
        if action.get("content"):
            return f"{kind}: {str(action.get('content'))[:120]}"
        keys = action.get("keys")
        if isinstance(keys, list) and keys:
            parts = []
            for key in keys[:12]:
                if isinstance(key, dict):
                    if key.get("kind") == "literal":
                        parts.append(str(key.get("value", "")))
                    else:
                        parts.append(f"<{key.get('value', 'key')}>")
            return f"type:keys {''.join(parts)[:120]}"
        return kind
    # What the human typed is not in the journal (it never enters the meeting
    # record), so the reviewer reads it off the screen frames like everyone
    # else: echoed input is visible there, and un-echoed input is exactly what
    # must not be kept.
    return ""


def build_reviewer_observation(store: m2.MeetingStore) -> str:
    """Structured transcript: chat, who typed what, terminal ownership,
    and past screen frames with per-frame change hints."""
    events = store.events()
    sections: list = []

    # --- chat --------------------------------------------------------------
    chats = [e for e in events if e.get("kind") == "chat"]
    chat_lines = []
    for event in chats[-CHAT_LINES:]:
        content = (event.get("content") or "").strip()
        if content:
            chat_lines.append(f"[{_hms(event)}] {_actor_label(store, event.get('actor'))}: {content[:300]}")
    sections.append("=== conversation ===" + ("" if chat_lines else " (none)")
                    + "\n" + "\n".join(chat_lines) if chat_lines else "=== conversation === (none)")

    # --- who typed what -----------------------------------------------------
    typing = []
    seen = set()
    for event in events:
        if event.get("kind") != "action":
            continue
        actor = event.get("actor")
        if actor in ("harness", "workspace"):
            continue
        data = event.get("data") or {}
        summary = _action_summary(data)
        if not summary:
            continue
        dedupe_key = data.get("action_id") or data.get("response_id")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        typing.append(f"[{_hms(event)}] {_actor_label(store, actor)} {summary}")
    sections.append("=== who typed what (recent actions) ==="
                    + ("" if typing else " (no typing captured)")
                    + "\n" + "\n".join(typing[-TYPING_LINES:]))

    # --- terminal ownership -------------------------------------------------
    owners = []
    for event in events:
        if event.get("kind") != "control":
            continue
        data = event.get("data") or {}
        user = data.get("user_name")
        if not user and (data.get("status") in ("acquired", "released")):
            user = "human" if data.get("status") == "acquired" else None
        if user:
            owners.append(f"[{_hms(event)}] terminal -> {_actor_label(store, user)}")
    if owners:
        sections.append("=== terminal ownership ===" + "\n" + "\n".join(owners[-16:]))

    # --- past screens with change hints -------------------------------------
    frames = [e for e in events if e.get("kind") == "screen" and isinstance(e.get("content"), str)]
    if frames:
        # The newest frames are the evidence the reviewer judges against, and
        # diffs only mean anything between neighbouring frames: keep one
        # contiguous window ending at the latest screen instead of sampling.
        chosen = frames[-MAX_SCREEN_FRAMES:]
        frame_parts = []
        previous = None
        for index, frame in enumerate(chosen):
            text = frame.get("content") or ""
            text = text[:FRAME_CHAR_CAP]
            label = f"[screen {_hms(frame)}]"
            frame_parts.append(f"{label} (frame {index + 1}/{len(chosen)})")
            frame_parts.append(text)
            if previous is not None:
                import difflib
                differ = difflib.SequenceMatcher(None, previous.split("\n"), text.split("\n"))
                removed = []
                added = []
                for tag, i1, i2, j1, j2 in differ.get_opcodes():
                    if tag in ("delete", "replace"):
                        removed.extend(previous.split("\n")[i1:i2])
                    if tag in ("insert", "replace"):
                        added.extend(text.split("\n")[j1:j2])
                if removed or added:
                    frame_parts.append(f"  changes vs previous: removed {len(removed)} line(s), added {len(added)} line(s)")
                    if removed:
                        frame_parts.append("  removed first lines: " + " | ".join(r.strip()[:60] for r in removed[:4]))
                    if added:
                        frame_parts.append("  added first lines:   " + " | ".join(a.strip()[:60] for a in added[:4]))
            previous = text
        sections.append("=== past screen frames (who/why visible here) ==="
                        + "\n" + "\n".join(frame_parts))

    return "\n".join(sections)


def build_actor_instruction_block(store: m2.MeetingStore) -> str:
    """The generator operates under a skill/instruction file; the reviewer
    needs the same instructions to judge whether the actor followed them."""
    try:
        path = store.runtime_skill_path
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[:4000]
    except Exception:  # noqa: BLE001
        return ""


PERSONA = (
    "You are a teammate next to the human at a shared terminal. Answer only "
    "the question the human actually asked, nothing more. Keep it to 1-3 "
    "short sentences in the human's language. Lead with the answer; do not "
    "explain things nobody asked about, do not recap the transcript, do not "
    "catalogue who typed what unless that is the answer itself. If you need "
    "one missing detail to answer well, ask exactly that one question; "
    "otherwise stop."
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
        context = build_reviewer_observation(store)
        # The rules the meeting enforces are run for the reviewer before it
        # speaks: it is chat-only, so the verdicts come to it as evidence, and
        # the meeting keeps them (a "state" event under the reviewer's name).
        rule_section, rule_report = critic_rules.review(store)
        # What the rules judged is cited, not described: the reviewer's reply
        # carries the reference to that frame and to the region the rules read.
        cited = critic_rules.citation(rule_report)
        try:
            store.append_event("state", agent_name, "rule_check",
                               content=rule_section, data={"rule_check": rule_report})
        except Exception:  # noqa: BLE001 — the report is not worth losing a turn
            pass
        actor_rules = build_actor_instruction_block(store)
        system = f"{persona}\n\n{instructions}"
        if actor_rules:
            system += "\n\n===== the generator actor operates under these instructions (know them so you can judge it fairly) =====\n" + actor_rules
        latest_human = ""
        for event in reversed(store.events()):
            if event.get("kind") == "chat" and event.get("actor") == "human" and (event.get("content") or "").strip():
                latest_human = event.get("content").strip()[:500]
                break
        question = f"The human's latest message (answer this ONLY):\n{latest_human}" if latest_human else "Answer based on the context."
        user = (question + "\n\nDo not recap or explain anything beyond answering that message."
                + "\n\n" + rule_section + "\n\nContext:\n" + context)
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
                # The gateway defaults to reasoning; without disabling it the
                # model can burn the whole token budget on a thinking block
                # and never emit the text answer (observed as "no text
                # content in response").
                "thinking": {"type": "disabled"},
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
            store.append_chat(agent_name, comment, "final",
                              references=[cited] if cited else None)
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
