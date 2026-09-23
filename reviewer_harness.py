"""reviewer_harness — an independent harness for the reviewers (the members
that comment but never act).

Design (per MEETING_ADT object layer): agents may come from different
harnesses/service providers; they only converge in MeetingData. The actor
uses the terminal-bound Claude-Code action harness. A reviewer must
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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, Optional

import meeting_v2 as m2
from meeting_reviewer import REVIEWER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS_RETRY

import assistant_rules
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
    """The actor operates under a skill/instruction file; the reviewer
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
        raise m2.MeetingError(f"reviewer harness: provider {DEFAULT_PROVIDER!r} not configured")
    return dict(provider)


def _read_api_key() -> str:
    try:
        auth = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise m2.MeetingError(f"reviewer harness: cannot read auth.json: {exc}") from exc
    key = auth.get("DEEPSEEK_API_KEY") if DEFAULT_PROVIDER == "deepseek" else auth.get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise m2.MeetingError("reviewer harness: API key is missing")
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
            f"reviewer harness: HTTP {exc.code}: {detail}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise m2.MeetingError(f"reviewer harness: request failed: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise m2.MeetingError("reviewer harness: non-JSON response") from exc
    return parsed


def _extract_text(response: dict) -> str:
    for block in response.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise m2.MeetingError("reviewer harness: no text content in response")


# Which member a rule's objection is addressed to: the rule says what is wrong,
# and the wrong thing says who can fix it.
RULE_ADDRESSEE = {
    "unanswered_seconds_le_30": "remind",            # the actor owes an answer
    "unreviewed_replies_le_0": "summon",             # the critic owes a re-check
    "human_prompt_conflicts_le_0": "done",           # nobody owes anything: the
                                                     # human asked for a violation
}


def _forced_by_rules(rule_report, critic_name: str, actor_name: str,
                     reviewed: bool = False):
    """(action, line) for the first rule that objects, or None.

    ``reviewed`` says whether the critic has spoken since the actor's last word:
    a conflict in the human's prompt only ends the meeting *after* the review,
    never instead of it.
    """
    for rule in rule_report.get("rules", []):
        if rule.get("verdict") != "VIOLATED":
            continue
        addressee = RULE_ADDRESSEE.get(str(rule.get("rule")))
        if addressee is None:
            continue
        complaint = rule.get("complaint") or ""
        who = rule.get("offender") or actor_name
        if addressee == "remind":
            return "remind", f"@{who} 请回应 critic：{complaint}"
        if addressee == "summon":
            return "summon", f"@{critic_name} 请复核 {who} 的回应：{complaint}"
        # The human's own words ask for what a rule forbids. The review still
        # has to happen — that is the critic's word, and it is the only thing
        # that can say whether anything *else* is wrong; the conflict only says
        # that this one objection is the human's, not the actor's.
        if not reviewed:
            return "summon", (f"@{critic_name} 请复核 {who} 的工作：人类的要求与规则冲突"
                              f"（{complaint}），确认除此之外没有别的问题。")
        return "done", (f"你的要求「{who}」和会议的规则冲突（{complaint}），"
                        f"actor 没法两全；这一轮按你的要求算完成。")
    return None


def _critic_spoke_last(store: m2.MeetingStore) -> bool:
    """Whether the critic has been heard since the actor last said anything."""
    roles = store.meeting.effective_role_map
    critic = {name for name, role in roles.items() if role == "critic"}
    actor = {name for name, role in roles.items() if role == "actor"}
    seen_actor = False
    for event in reversed(store.events()):
        if event.get("kind") != "chat" or not (event.get("content") or "").strip():
            continue
        who = event.get("actor")
        if who in actor:
            seen_actor = True
        elif who in critic and seen_actor:
            return True
        elif who in critic:
            return not seen_actor
    return False


def _newest_actor_message(store: m2.MeetingStore) -> str:
    """The actor's newest chat message: the claim a summon is about."""
    roles = store.meeting.effective_role_map
    actors = {name for name, role in roles.items() if role == "actor"}
    for event in reversed(store.events()):
        if (event.get("kind") == "chat" and event.get("actor") in actors
                and (event.get("content") or "").strip()):
            return str(event.get("event_id") or "")
    return ""


def _latest_summon(store: m2.MeetingStore, agent_name: str) -> str:
    """The newest chat that names this reviewer, from anyone but the reviewer.

    The human can summon it by hand; so can the assistant, on its own. Whoever
    did, that message is the question the reviewer answers.
    """
    roles = store.meeting.effective_role_map
    for event in reversed(store.events()):
        if event.get("kind") != "chat" or event.get("actor") == agent_name:
            continue
        content = (event.get("content") or "").strip()
        if content and agent_name in m2.mentioned_agent_names(content, roles):
            return content[:500]
    return ""


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
            system += "\n\n===== the actor actor operates under these instructions (know them so you can judge it fairly) =====\n" + actor_rules
        summon = _latest_summon(store, agent_name)
        if summon:
            question = f"The message that summoned you (answer this ONLY):\n{summon}"
        else:
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
                      "harness": "reviewer-plain-chat"},
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
            raise m2.MeetingError("reviewer harness: response too short to be a comment")
        with guard:
            store.refresh()
            if store.meeting.status in {"completed", "failed", "interrupted"}:
                return False
            store.append_chat(agent_name, comment, "final",
                              references=[cited] if cited else None)
            store.append_event(
                "action", agent_name, "agent_acting",
                data={"action_id": f"reviewer-{agent_name}", "status": "completed",
                      "harness": "reviewer-plain-chat"},
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


# ----------------------------------------------------------------------
# the assistant: it looks while the actor works, and wakes the critic
# ----------------------------------------------------------------------

# How often the assistant may look. It is the floor, not a schedule: a look
# never comes sooner than this.
WATCH_INTERVAL_SECONDS = 10.0

ASSISTANT_PERSONA = (
    "You are the human's delegate at this meeting, not a participant in the "
    "work: you never touch the terminal and you never judge the work yourself. "
    "Your one job is to decide whether the critic should be woken up to look at "
    "what just happened. Answer with one line, in the human's language, and "
    "nothing else."
)

ASSISTANT_INSTRUCTIONS = (
    "Answer with exactly one of these three lines:\n"
    "SUMMON: <what the critic should look at>\n"
    "REMIND: <what the actor should answer>\n"
    "SILENT\n"
    "Choose SUMMON when work that the meeting cares about has just happened and "
    "the critic has not looked at it yet; say in one short sentence what to look "
    "at (name the file or the change). Choose SUMMON as well when the rules below "
    "say the actor has replied and nobody has reviewed that reply: an answer "
    "nobody looked at is not an answer yet, so ask the critic to check whether "
    "the problem is actually gone. Choose REMIND when the rules say an "
    "answer is owed and late: say in one short sentence what the actor should "
    "answer, and address it to the member the rules name. Choose SILENT when "
    "nothing changed, when the change is cosmetic, or when the critic has already "
    "looked at it — silence is the normal answer. Do not review the work and do "
    "not answer in the actor's place: the rules hold the verdict, and the members "
    "answer for themselves."
)


class Look(NamedTuple):
    """What one look decided: say this line, to this member, about this case."""

    action: str      # "summon" (the critic should look), "remind" (the actor owes), "silent"
    line: str        # what to say, already addressed
    case: str        # the event the rules judged, so a reminder is said once
    owed: bool = False     # the rules object: the answer is late
    pending: bool = False  # a message is unanswered, inside the grace or not
    subject: str = ""      # the actor's newest word, so a summon is asked once


def parse_assistant_answer(answer: str, critic_name: str = "critic",
                           actor_name: str = "actor", case: str = "",
                           owed: bool = False, pending: bool = False) -> Look:
    """What to do with one answer: a look. Silence is a look too.

    Only `SUMMON: ...` (the critic should look) and `REMIND: ...` (the actor owes
    an answer) are lines to say; anything else is silence.
    """
    head, separator, rest = (answer or "").strip().partition(":")
    kind = head.strip().upper()
    if kind not in {"SUMMON", "REMIND"} or not separator or not rest.strip():
        return Look("silent", "", case, owed, pending)
    line = " ".join(rest.split())
    target = critic_name if kind == "SUMMON" else actor_name
    if target and f"@{target}" not in line:
        line = f"@{target} {line}"
    return Look(kind.lower(), line, case, owed, pending)


def assistant_turn(store: m2.MeetingStore, agent_name: str = "assistant",
                   *, post: Any = None, observer: Any = None):
    """One look at the meeting: what, if anything, the critic should be told.

    Returns the line to say to the critic, or None to stay silent. The look is
    recorded in the meeting either way, so what the assistant saw and decided is
    readable afterwards. Never raises: a look that fails is an error event and
    silence, not a dead assistant.
    """
    post = post or _post_messages
    try:
        store.refresh()
        observation = (observer or build_reviewer_observation)(store)
        # The assistant's own rules, run for it before it looks: what is owed,
        # and who owes it, is the rules' answer, not the model's guess.
        rule_section, rule_report = assistant_rules.review(store)
        case = str((rule_report.get("message") or {}).get("event_id") or "")
        owed = any(rule.get("verdict") == "VIOLATED" for rule in rule_report.get("rules", []))
        # Still inside the grace is not "nothing to watch": a message with no
        # answer yet is exactly what this loop stays awake for.
        pending = any((rule.get("value") or 0) > 0 for rule in rule_report.get("rules", []))
        _record(store, agent_name, store.meeting.phase,
                {"status": "checked", "rule_check": rule_report}, content=rule_section)
        provider = _read_provider_config()
        model = provider.get("model") or "deepseek-v4-flash"
        base_url = provider.get("base_url") or "https://api.deepseek.com/anthropic"
        api_key = _read_api_key()
        response = post(
            {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "thinking": {"type": "disabled"},
                "system": f"{ASSISTANT_PERSONA}\n\n{ASSISTANT_INSTRUCTIONS}",
                "messages": [{"role": "user", "content": observation + "\n\n" + rule_section}],
            },
            base_url,
            api_key,
        )
        answer = _extract_text(response)
    except Exception as exc:  # noqa: BLE001 — the loop must survive a bad look
        _record_error(store, agent_name, exc)
        return Look("silent", "", case, owed, pending)
    look = parse_assistant_answer(answer, case=case, owed=owed, pending=pending)
    look = look._replace(subject=_newest_actor_message(store))
    roles = store.meeting.effective_role_map
    critic_name = next((name for name, role in roles.items() if role == "critic"), "critic")
    actor_name = next((name for name, role in roles.items() if role == "actor"), "actor")
    forced = _forced_by_rules(rule_report, critic_name, actor_name,
                              reviewed=_critic_spoke_last(store))
    if forced is not None:
        # A rule objects, and which rule it is decides who is addressed: an
        # answer that is late is the actor's to give, a reply nobody looked at
        # is the critic's to look at. Leaving that to the model produced
        # monologues addressed to the wrong member.
        look = look._replace(action=forced[0], line=forced[1])
    # The look is part of the record either way: what it saw, and what it said.
    _record(store, agent_name, store.meeting.phase,
            {"status": "looked", "answer": answer[:400],
             "action": look.action, "line": look.line, "owed": look.owed},
            content=look.line or "SILENT")
    return look


def watch(store: m2.MeetingStore, agent_name: str, *, actor_busy: Any,
          critic_busy: Any = None, act: Any, stop: Any,
          interval: float = WATCH_INTERVAL_SECONDS,
          sleep: Any = time.sleep, post: Any = None, observer: Any = None) -> None:
    """Look every ``interval`` seconds while the actor works.

    The actor parking is itself worth a look — a piece of work just ended — so
    the loop takes one more look after it parks, does what it decided, and
    returns. A decision made while the actor is still working is held until it
    parks: nobody may be summoned beside the actor, and the work will have moved
    on by then anyway. One message gets one reminder: saying the same thing every
    ten seconds is not patience, it is noise.
    """
    waiting = None
    reminded: set = set()
    summoned: set = set()
    while not stop():
        sleep(interval)
        if stop():
            return
        look = assistant_turn(store, agent_name, post=post, observer=observer)
        if look.action == "remind" and look.case in reminded:
            look = Look("silent", "", look.case, look.owed, look.pending)   # said once
        if look.action == "summon" and look.subject and look.subject in summoned:
            # The critic has already been asked about this piece of work; asking
            # again about the same claim produces another monologue, not progress.
            look = Look("silent", "", look.case, look.owed, look.pending, look.subject)
        if look.action != "silent":
            waiting = look
        if actor_busy():
            continue                         # hold it until the actor parks
        if waiting is not None:
            if waiting.action == "remind" and waiting.case:
                reminded.add(waiting.case)
            if waiting.action == "summon" and waiting.subject:
                summoned.add(waiting.subject)
            act(waiting)
            waiting = None
            if look.action == "done":
                return                       # the human asked for the violation: stop
        if not look.pending and not (critic_busy and critic_busy()):
            return                           # nothing owed, nobody working: done


def _record_error(store: m2.MeetingStore, agent_name: str, exc: BaseException) -> None:
    try:
        store.append_event("error", "harness", "assistant_error",
                           content=f"{type(exc).__name__}: {exc}")
    except Exception:  # noqa: BLE001
        pass
