"""meeting_reviewer — one-turn reviewer that runs in its own
Claude Code process and never touches the shared terminal.

Why separate from InteractiveRunner: the runner's loop is written for the
actor that OWNS the terminal (it gates on ``control_owner == its name``
and drives tmux actions). A reviewer must not hold or act on the
terminal; this module is a deliberately small, chat-only sibling that:

- opens its own backend client (its own claude-code subprocess),
- observes the meeting journal (conversation tail + latest screen),
- is allowed to emit only ``chat`` / ``done`` actions (no terminal action
  types are ever accepted),
- appends its final comment to the journal under its own agent name,
- restores the meeting to ``waiting_for_user`` so the actor can run next.

Single-writer rule: callers only summon a reviewer while the meeting is idle
(no actor turn in flight, no human takeover), so the shared phase field
is never written by two agents at once.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import meeting_v2 as m2
from agent_protocol import AgentAction, AgentActionError, AgentBackendError, AgentInterrupted

REVIEWER_MAX_ROUNDS = 12
REVIEWER_OBSERVATION = "Continue. Reply when you have a comment."
# Sentinel returned when the backend ended the turn after a rejected action:
# callers may retry with a fresh process.
REVIEWER_RETRY = ("retry",)

REVIEWER_INSTRUCTIONS = (
    "Judge only what you were given: the rule check in your context holds the "
    "rules you run, and its verdicts are yours to report. Do not rule on other "
    "members' rules or on claims about them — you have not measured those. "

    "Answer only what the human asked, in 1-3 short sentences, same "
    "language. Bottom line first. Do not add background, recap, or anything "
    "unasked. Ask one question only if a missing detail is needed to answer. "

    # Say it the way a colleague says it, not the way a checker prints it.
    "Talk like a person in the room: no rule names, field names or other "
    "internal labels — say what is wrong in plain words and where. Name the "
    "person you mean as you or by name, not as a role in the third person. One "
    "point per message; do not stack a verdict, its evidence and an instruction "
    "into one line, and do not repeat what is already on the screen. Do not "
    "explain your own scope or what you are not going to answer — answer, or stay "
    "quiet. Write plain sentences: no markup, no tags, no citation syntax, no "
    "links — when you point at a frame or a message, the meeting attaches that "
    "reference itself; a tag you type only shows up as noise in the transcript."
)

REVIEWER_INSTRUCTIONS_RETRY = (
    "You are a reviewer without terminal access. You may ONLY reply in prose: "
    "do not call any tool, do not read files, do not propose commands, do not "
    "use the terminal. Your previous attempt was rejected for attempting a "
    "terminal action. Reply with a chat ack and finish with a done action "
    "carrying your final prose comment."
)


def build_reviewer_context(store: m2.MeetingStore, max_chat: int = 6) -> str:
    """Human-readable context for the reviewer: recent chat + latest screen."""
    lines: list = []
    events = store.events()
    chat = [e for e in events if e.get("kind") == "chat"]
    for event in chat[-max_chat:]:
        actor = event.get("actor", "?")
        content = (event.get("content") or "").strip()
        if content:
            lines.append(f"{actor}: {content}")
    screen = store.latest_screen() if hasattr(store, "latest_screen") else None
    screen_text = ""
    if isinstance(screen, dict):
        content = screen.get("content") or ""
        screen_text = content if isinstance(content, str) else ""
    body = "\n".join(lines)
    if screen_text:
        screen_text = screen_text[-4000:]
        body = f"{body}\n\n--- latest screen ---\n{screen_text}"
    return body or "(empty conversation)"


def _configured_timeout(client: Any) -> float:
    config = getattr(client, "config", None)
    if config is not None and hasattr(config, "request_timeout_seconds"):
        try:
            return float(getattr(config, "request_timeout_seconds"))
        except (TypeError, ValueError):
            pass
    return 60.0


def _record_state(store: m2.MeetingStore, agent_name: str, payload: dict) -> None:
    try:
        store.append_event("state", agent_name, store.meeting.phase, data={"reviewer": payload})
    except Exception:  # noqa: BLE001  records are observational
        pass


def run_reviewer_turn(
    store: m2.MeetingStore,
    client: Any,
    agent_name: str,
    lock: Optional[threading.RLock] = None,
    context: Optional[str] = None,
    instructions: Optional[str] = None,
) -> bool:
    """Run one reviewer turn; append the final comment under agent_name.

    Returns True when a final comment was recorded, False otherwise, or
    REVIEWER_RETRY when the backend ended after a rejected action.
    """
    guard = lock or threading.RLock()
    meeting_id = store.meeting.meeting_id
    instructions = instructions or REVIEWER_INSTRUCTIONS
    observation = context if context is not None else build_reviewer_context(store)
    timeout = _configured_timeout(client)
    session_id: Optional[str] = None

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
        with guard:
            store.refresh()
            store.update(phase="agent_thinking", control_owner="none")
            store.append_event(
                "action", agent_name, "agent_thinking",
                data={"action_id": f"reviewer-{agent_name}", "status": "received"},
            )
            _record_state(store, agent_name, {"status": "started"})
        start_cycle = getattr(client, "start_cycle", None)
        if not callable(start_cycle):
            raise m2.MeetingError("reviewer backend does not support start_cycle")
        session_id = str(start_cycle(meeting_id, instructions, observation,
                                     model=store.meeting.model, event_callback=None))
        next_action = getattr(client, "next_action", None)
        if not callable(next_action):
            raise m2.MeetingError("reviewer backend does not support next_action")
        for _round in range(REVIEWER_MAX_ROUNDS):
            with guard:
                store.refresh()
                if store.meeting.status in {"completed", "failed", "interrupted"}:
                    settled()
                    return False
            try:
                response = next_action(
                    session_id,
                    observation,
                    model=store.meeting.model,
                    allowed_types=("chat", "done"),
                    allowed_message_types=("ack", "progress", "question", "final"),
                    request_timeout_seconds=timeout,
                    action_handler=lambda action, metadata: {},
                )
            except AgentActionError as exc:
                try:
                    with guard:
                        store.refresh()
                        store.append_event(
                            "action", "harness", "agent_thinking", content=str(exc),
                            data={"action_id": f"reviewer-{agent_name}",
                                  "status": "rejected",
                                  "reason": "terminal_action_not_allowed"},
                        )
                except Exception:  # noqa: BLE001
                    pass
                observation = (
                    "Your previous action was rejected: you must not issue "
                    "terminal actions. Reply with a chat ack, or a done action "
                    "carrying your final comment."
                )
                continue
            observation = REVIEWER_OBSERVATION
            action: AgentAction = response.action
            if action.type == "done":
                with guard:
                    store.refresh()
                    store.append_chat(agent_name, action.content, "final")
                    store.append_event(
                        "action", agent_name, "agent_acting",
                        data={"action_id": f"reviewer-{agent_name}",
                              "status": "completed", "action": json.dumps({
                                  "type": action.type, "message_type": action.message_type,
                              })},
                    )
                    store.update(phase="waiting_for_user", control_owner="none",
                                 pending_action_id=None)
                    _record_state(store, agent_name, {"status": "done"})
                return True
            # chat ack / progress / question: a substantive message is the
            # reviewer's comment -- record it and finish. Tiny acknowledgments
            # ("ok", "I will look") are noise; keep listening.
            if action.type == "chat" and len((action.content or "").strip()) >= 20:
                with guard:
                    store.refresh()
                    store.append_chat(agent_name, action.content, "final")
                    store.append_event(
                        "action", agent_name, "agent_acting",
                        data={"action_id": f"reviewer-{agent_name}",
                              "status": "completed", "action": json.dumps({
                                  "type": action.type, "message_type": action.message_type,
                              })},
                    )
                    store.update(phase="waiting_for_user", control_owner="none",
                                 pending_action_id=None)
                    _record_state(store, agent_name, {"status": "comment"})
                return True
            if action.type == "chat":
                continue
            settled()
            return False
        settled()
        return False
    except (AgentInterrupted,) as exc:
        store.append_event("error", "harness", "reviewer_interrupted", content=str(exc))
        settled()
        return False
    except (AgentBackendError, m2.MeetingError, OSError, ValueError) as exc:
        text = str(exc)
        if "rejected" in text:
            settled()
            return REVIEWER_RETRY
        try:
            store.append_event("error", "harness", "reviewer_error", content=text)
        except Exception:  # noqa: BLE001
            pass
        settled()
        return False
    except Exception as exc:  # noqa: BLE001  last-resort trap keeps the thread alive
        try:
            store.append_event("error", "harness", "reviewer_error", content=f"unexpected: {exc!r}")
        except Exception:  # noqa: BLE001
            pass
        settled()
        return False
