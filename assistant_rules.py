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
         REPO / "assistant" / "unreviewed_replies_le_0.viba",
         REPO / "assistant" / "human_prompt_conflicts_le_0.viba")

# The case's environment, as the rule declares it; its path is the case's address.
CASE_ENV = "unanswered_message_under_judgment"
# The human's prompt is a case of its own: the conflict rule judges that.
PROMPT_CASE_ENV = "human_prompt_under_judgment"

# Where a meeting keeps the evidence of the assistant's checks.
EVIDENCE_DIR = "assistant_rules"

RULE_WORDINGS = {
    "unanswered_seconds_le_30": RuleWording(
        "an answer owed to a message may not be late", "成员",
        ops.UNANSWERED_NAME, ops.SILENT_NAME, "已经 {value} 秒没回应这句话"),
    "unreviewed_replies_le_0": RuleWording(
        "a reply the critic has not looked at is not an answer yet", "成员",
        ops.UNREVIEWED_NAME, ops.SILENT_NAME, "有 {value} 句回应还没被复查"),
    "human_prompt_conflicts_le_0": RuleWording(
        "the human's own words may not ask for what a rule forbids", "要求",
        ops.CONFLICTS_NAME, ops.CONFLICT_QUOTE_NAME, "本身就要求了 {value} 处规则禁止的东西"),
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
# ... and the conflict rule judges the human's prompt, which is another message.
PROMPT_CASE = critic_rules.Case(PROMPT_CASE_ENV, ops.PROMPT_SNAPSHOT, chat_reference_material)

# Which message each rule judges.
RULE_CASES = {
    "unanswered_seconds_le_30": CHAT_CASE,
    "unreviewed_replies_le_0": CHAT_CASE,
    "human_prompt_conflicts_le_0": PROMPT_CASE,
}


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
    """Judge every rule over the message it is about.

    Most rules are about what the critic just said; the conflict rule is about
    what the human asked for, which is a different message in the same meeting.
    """
    results = []
    judged: Dict[str, Any] = {}
    for rule in rules:
        case = RULE_CASES.get(rule.stem, CHAT_CASE)
        message = (_last_critic_message(store) if case is CHAT_CASE
                   else _human_prompt(store))
        if message is None:
            continue
        judged[rule.stem] = message
        region = (0, len(str(message.get("content") or "")))
        where = Path(store_dir) if store_dir else Path(store.run_dir) / EVIDENCE_DIR
        results.append(critic_rules.judge_moment(
            store, message, region, where / rule.stem, rule,
            case=case, wordings=RULE_WORDINGS))
    if not results:
        return (_NO_MESSAGE_SECTION, {"message": None, "citation": None, "rules": []})
    # A fact the assistant needs to apply its own instructions: whether the
    # critic has been heard since the actor last said anything. It is the
    # meeting's state, not advice — the wording of any answer stays the
    # assistant's.
    heard = _critic_heard_since_actor(store)
    # The report keeps the case each verdict came from: a reader can tell what
    # was judged, and the citation points at the message the rules read.
    message = judged.get("unanswered_seconds_le_30") or next(iter(judged.values()))
    region = (0, len(str(message.get("content") or "")))
    report = {"message": {"event_id": message.get("event_id"),
                          "captured_at": message.get("captured_at"),
                          "actor": message.get("actor"),
                          "region": list(region)},
              "citation": chat_reference(message, region),
              "cases": {stem: event.get("event_id") for stem, event in judged.items()},
              "rules": [{"rule": result["rule"], "verdict": verdict_word(result),
                         "offender": (result.get("offender") or {}).get("name"),
                         "value": (result.get("measured_value") or {}).get("value"),
                         "complaint": result.get("complaint")} for result in results]}
    # Two facts the answer needs, both the meeting's own: which rules the
    # human's words excuse, and which the critic says are broken. Without them
    # the assistant cannot tell "the only thing left is what the human asked
    # for" from "the human asked for one of several".
    excused = _prompt_excused_rules(store)
    broken = _critic_violations(store)
    report["critic_heard_since_actor"] = heard
    report["prompt_excused_rules"] = excused
    report["critic_violations"] = broken
    return _section(judged, results, heard, excused, broken), report


def _prompt_excused_rules(store) -> list:
    """The rules the human's own prompt asks to break."""
    prompt = _human_prompt(store)
    if prompt is None:
        return []
    _count, _quote, names = ops.prompt_conflicts(str(prompt.get("content") or ""))
    return names


def _critic_violations(store) -> list:
    """What the critic last reported as broken, from its own rule check."""
    for event in reversed(store.events()):
        if event.get("kind") != "state":
            continue
        check = (event.get("data") or {}).get("rule_check")
        if not isinstance(check, dict) or not isinstance(check.get("rules"), list):
            continue
        return [rule.get("rule") for rule in check["rules"]
                if rule.get("verdict") == "VIOLATED"]
    return []


def _critic_heard_since_actor(store) -> bool:
    """Whether the critic has spoken since the actor's newest message.

    An answer nobody looked at is not an answer yet, and a meeting may not be
    declared finished on a review that never happened.
    """
    roles = store.meeting.effective_role_map
    critics = {name for name, role in roles.items() if role == "critic"}
    actors = {name for name, role in roles.items() if role == "actor"}
    heard = False
    for event in reversed(store.events()):
        if event.get("kind") != "chat" or not (event.get("content") or "").strip():
            continue
        who = event.get("actor")
        if who in critics:
            heard = True
        elif who in actors:
            return heard
    return heard


def _human_prompt(store) -> Optional[Dict[str, Any]]:
    """The human's newest words: the task being asked for."""
    for event in reversed(store.events()):
        if event.get("kind") == "chat" and event.get("actor") == "human":
            if (event.get("content") or "").strip():
                return event
    return None


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


def _section(judged, results, heard: bool = False, excused: list = None,
             broken: list = None) -> str:
    lines = ["=== rule check (the assistant's rules) ===",
             f"critic_heard_since_actor: {'yes' if heard else 'no'}",
             f"prompt_excused_rules: {', '.join(excused or []) or '(none)'}",
             f"critic_violations: {', '.join(broken or []) or '(none)'}"]
    for stem, message in judged.items():
        lines.append(f"rule {stem} judged message {message.get('event_id')} by "
                     f"{message.get('actor')} at {message.get('captured_at')}")
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
