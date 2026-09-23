"""assistant_rules — the rules a meeting's assistant enforces, run over its chat.

The same machinery as ``critic_rules``, over a different subject: the critic's
rules judge the code on the screen, the assistant's judge the conversation. A
rule is a viba program (``assistant/*.viba``): environ in, bool out, no meeting
and no message baked in. The case is material the assistant hands in — a
``mc.ChatReference``: who said what, and when — written under the case's own
address; reading the meeting is the one impure step and goes through ``measure``,
so the answer becomes that case's Prepare and every later judgment replays it.

The first rule is the one a conversation needs: what the critic said may not sit
unanswered. The verdict is not "someone is wrong", it is "this turn is stuck",
so the assistant acts on it by asking the member who owes the answer — the actor
— to reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import critic_rules
import underlying_viba_ops as ops
from critic_rules import RuleWording, measured_value, verdict_word
from viba import viba_ast

REPO = Path(__file__).resolve().parent
RULES = (REPO / "assistant" / "unanswered_seconds_le_30.viba",
         REPO / "assistant" / "unreviewed_replies_le_0.viba")

# The case's environment, as the rule declares it; its path is the case's address.
CASE_ENV = "unanswered_message_under_judgment"

# Where a meeting keeps the evidence of the assistant's checks.
EVIDENCE_DIR = "assistant_rules"

RULE_WORDINGS = {
    "unanswered_seconds_le_30": RuleWording(
        "an answer owed to a message may not be late", "成员",
        ops.UNANSWERED_NAME, ops.SILENT_NAME, "已经 {value} 秒没回应这句话"),
    "unreviewed_replies_le_0": RuleWording(
        "a reply the critic has not looked at is not an answer yet", "成员",
        ops.UNREVIEWED_NAME, ops.SILENT_NAME, "有 {value} 句回应还没被复查"),
}


def chat_reference_material(event, region: Tuple[int, int]):
    """The cited message as material: `$msg_time` + `$user_name` + `$region`."""
    def tags(pairs):
        return viba_ast.ProductChain([viba_ast.Tagged(tag, viba_ast.Constant(value))
                                      for tag, value in pairs])
    author = str(event.get("actor") or "")
    user_name = (tags([("$human", None)]) if author in ("human", "")
                 else tags([("$agent_name", author)]))
    return viba_ast.ProductChain([
        viba_ast.Tagged("$msg_time", tags([("$microsecond", critic_rules.captured_microsecond(event))])),
        viba_ast.Tagged("$user_name", user_name),
        viba_ast.Tagged("$region", tags([("$start", region[0]), ("$end", region[1])])),
    ])


# The assistant's rules judge a message; their case is a chat reference.
CHAT_CASE = critic_rules.Case(CASE_ENV, ops.CHAT_SNAPSHOT, chat_reference_material)


def review(store, store_dir: Optional[Path] = None,
           rules=RULES) -> Tuple[str, Dict[str, Any]]:
    """Run every rule over the critic's newest message.

    Returns ``(section, report)``: the block for the assistant's context, and
    the same thing as data. Never raises: an assistant's look must go on even
    when a message cannot be read.
    """
    try:
        return _review(store, store_dir, rules)
    except Exception as exc:  # noqa: BLE001 — not the assistant's job to debug
        section = ("=== rule check (the assistant's rules) ===\n"
                   f"the rules could not be run over this meeting: {exc!r}\n"
                   "Say that, do not guess.")
        return section, {"error": repr(exc), "rules": []}


def _review(store, store_dir, rules) -> Tuple[str, Dict[str, Any]]:
    message = _last_critic_message(store)
    if message is None:
        return (_NO_MESSAGE_SECTION, {"message": None, "citation": None, "rules": []})
    region = (0, len(str(message.get("content") or "")))
    results = []
    for rule in rules:
        where = Path(store_dir) if store_dir else Path(store.run_dir) / EVIDENCE_DIR
        results.append(critic_rules.judge_moment(
            store, message, region, where / rule.stem, rule,
            case=CHAT_CASE, wordings=RULE_WORDINGS))
    report = {"message": {"event_id": message.get("event_id"),
                          "captured_at": message.get("captured_at"),
                          "actor": message.get("actor"),
                          "region": list(region)},
              "citation": chat_reference(message, region),
              "rules": [{"rule": result["rule"], "verdict": verdict_word(result),
                         "offender": (result.get("offender") or {}).get("name"),
                         "value": (result.get("measured_value") or {}).get("value"),
                         "complaint": result.get("complaint")} for result in results]}
    return _section(message, region, results), report


def _last_critic_message(store) -> Optional[Dict[str, Any]]:
    """The newest thing the critic said, if it said anything."""
    roles = store.meeting.effective_role_map
    critics = {name for name, role in roles.items() if role == "critic"}
    for event in reversed(store.events()):
        if event.get("kind") != "chat" or event.get("actor") not in critics:
            continue
        if (event.get("content") or "").strip():
            return event
    return None


def chat_reference(event, region: Tuple[int, int]) -> Dict[str, Any]:
    """The message a check judged, as a reference: that event, that region."""
    import references

    return references.reference_from_event(
        event, {"kind": "chat", "event_id": event["event_id"],
                "region": {"start": region[0], "end": region[1]}})


def citation(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The reference a report carries, or None when no message was judged."""
    found = report.get("citation")
    return found if isinstance(found, dict) else None


_NO_MESSAGE_SECTION = (
    "=== rule check (the assistant's rules) ===\n"
    "the critic has said nothing in this meeting, so there is no answer to be\n"
    "late and no rule to run. Say nothing about this."
)


def _section(message, region, results) -> str:
    lines = ["=== rule check (the assistant's rules, run over the critic's newest message) ===",
             f"message {message.get('event_id')} by {message.get('actor')} at "
             f"{message.get('captured_at')}, region {region[0]}..{region[1]}"]
    for result in results:
        wording = RULE_WORDINGS[result["rule"]]
        line = f"- {result['rule']} ({wording.title}): {verdict_word(result)}"
        if result.get("complaint"):
            line += f" — {result['complaint']}"
        lines.append(line)
    offenders = [result for result in results
                 if (result.get("offender") or {}).get("name")]
    if offenders:
        named = "；".join(
            f"规则 {result['rule']} 违规，{RULE_WORDINGS[result['rule']].offender} "
            f"{result['offender']['name']}" for result in offenders)
        lines.append(f"该催的是：{named}（照抄上面的名字，不要改写）。")
    else:
        lines.append("没有该催的人：这轮对话没有欠着谁的回应。")
    return "\n".join(lines)
