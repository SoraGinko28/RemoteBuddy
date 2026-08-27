from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


TYPING_POLICY_VERSION = "1"
TYPING_PROFILES = ("demo", "balanced", "fast")


@dataclass(frozen=True)
class ProfileSpec:
    burst_min: int
    burst_max: int
    interval_min_ms: int
    interval_max_ms: int
    semantic_min_ms: int
    semantic_max_ms: int
    paste_threshold_chars: int
    paste_threshold_lines: int
    action_limit_ms: int


PROFILE_SPECS: Dict[str, ProfileSpec] = {
    "demo": ProfileSpec(1, 2, 140, 220, 220, 450, 160, 6, 30_000),
    "balanced": ProfileSpec(1, 3, 90, 150, 150, 320, 240, 8, 20_000),
    "fast": ProfileSpec(2, 5, 55, 100, 90, 200, 400, 12, 12_000),
}

STRONG_BOUNDARIES = set(":;)]}")
MEDIUM_BOUNDARIES = set(",=+-*/%<>|&^")
EDITOR_LITERALS = {"i", "a", "o", "O", "I", "A", "v", "V"}


@dataclass(frozen=True)
class TypingStep:
    kind: str
    value: str
    delay_after_ms: int
    change_kind: str
    input_context: str
    burst_index: int


@dataclass(frozen=True)
class TypingPlan:
    steps: Tuple[TypingStep, ...]
    total_characters: int
    planned_duration_ms: int
    uses_paste: bool


def validate_profile(profile: str) -> str:
    if profile not in PROFILE_SPECS:
        raise ValueError("typing profile must be demo, balanced, or fast")
    return profile


def _rng(meeting_id: str, action_id: str, token_index: int, context: str) -> random.Random:
    source = f"{meeting_id}\0{action_id}\0{token_index}\0{context}".encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(source).digest(), "big"))


def _literal_context(value: str, context: str) -> str:
    if context == "vim" and (value in EDITOR_LITERALS or value.startswith(":")):
        return "editor_command"
    if context in {"vim", "python"}:
        return "code"
    return context


def _pause_for(
    value: str,
    next_character: str,
    spec: ProfileSpec,
    rng: random.Random,
    tempo: float,
) -> int:
    base = rng.randint(spec.interval_min_ms, spec.interval_max_ms)
    extra = 0
    last = value[-1]
    if last in STRONG_BOUNDARIES:
        extra = rng.randint(spec.semantic_min_ms, spec.semantic_max_ms)
    elif last in MEDIUM_BOUNDARIES:
        extra = rng.randint(max(35, spec.semantic_min_ms // 3), spec.semantic_max_ms // 2)
    elif last in " \t" or (next_character and next_character in " \t"):
        extra = rng.randint(25, min(100, spec.semantic_min_ms))
    return min(500, max(20, round(base * tempo) + extra))


def _normal_literal_steps(
    text: str,
    profile: str,
    meeting_id: str,
    action_id: str,
    token_index: int,
    context: str,
) -> List[TypingStep]:
    spec = PROFILE_SPECS[profile]
    rng = _rng(meeting_id, action_id, token_index, context)
    result: List[TypingStep] = []
    offset = 0
    tempo = rng.uniform(0.84, 1.16)
    tempo_until = rng.randint(6, 14)
    while offset < len(text):
        character = text[offset]
        if character in " \t":
            end = offset + 1
            while end < len(text) and text[end] in " \t" and end - offset < 8:
                end += 1
        elif not character.isalnum() and character != "_":
            end = offset + 1
            while (
                end < len(text)
                and text[end] == character
                and end - offset < min(4, spec.burst_max)
            ):
                end += 1
        else:
            size = rng.randint(spec.burst_min, spec.burst_max)
            end = offset + 1
            while (
                end < len(text)
                and end - offset < size
                and text[end] not in " \t"
                and (text[end].isalnum() or text[end] == "_")
            ):
                end += 1
        value = text[offset:end]
        next_character = text[end] if end < len(text) else ""
        delay = _pause_for(value, next_character, spec, rng, tempo)
        result.append(TypingStep("literal", value, delay, "typing", context, 0))
        tempo_until -= 1
        if tempo_until == 0:
            tempo = rng.uniform(0.84, 1.16)
            tempo_until = rng.randint(6, 14)
        offset = end
    return result


def _paste_literal_steps(
    text: str,
    meeting_id: str,
    action_id: str,
    token_index: int,
    context: str,
) -> List[TypingStep]:
    rng = _rng(meeting_id, action_id, token_index, context + ":paste")
    return [
        TypingStep("literal", text[offset : offset + 80], rng.randint(20, 50), "paste", context, 0)
        for offset in range(0, len(text), 80)
    ]


def _key_step(
    key: str,
    profile: str,
    meeting_id: str,
    action_id: str,
    token_index: int,
    context: str,
) -> TypingStep:
    spec = PROFILE_SPECS[profile]
    rng = _rng(meeting_id, action_id, token_index, context + ":key")
    if key == "Enter":
        delay = rng.randint(spec.semantic_min_ms, min(500, spec.semantic_max_ms))
    elif key == "Escape":
        delay = rng.randint(80, min(200, spec.semantic_max_ms))
    elif key == "C-c":
        delay = rng.randint(20, 50)
    else:
        delay = rng.randint(spec.interval_min_ms, min(180, spec.interval_max_ms + 30))
    key_context = "shell" if context == "shell" else "editor_command"
    return TypingStep("key", key, delay, "typing", key_context, 0)


def _renumber(steps: Sequence[TypingStep]) -> Tuple[TypingStep, ...]:
    return tuple(
        TypingStep(
            step.kind,
            step.value,
            step.delay_after_ms,
            step.change_kind,
            step.input_context,
            index,
        )
        for index, step in enumerate(steps, start=1)
    )


def plan_input(
    tokens: Sequence[Dict[str, str]],
    profile: str,
    meeting_id: str,
    action_id: str,
    context: str,
    *,
    allow_paste: bool = True,
) -> TypingPlan:
    validate_profile(profile)
    spec = PROFILE_SPECS[profile]
    literal_characters = sum(
        len(token["value"]) for token in tokens if token.get("kind") == "literal"
    )
    lines = 1 + sum(
        token.get("kind") == "key" and token.get("value") == "Enter" for token in tokens
    )
    threshold_paste = allow_paste and context in {"vim", "python", "code"} and (
        literal_characters >= spec.paste_threshold_chars or lines >= spec.paste_threshold_lines
    )
    steps: List[TypingStep] = []
    for token_index, token in enumerate(tokens):
        kind = token["kind"]
        value = token["value"]
        literal_context = _literal_context(value, context)
        if kind == "key":
            steps.append(
                _key_step(value, profile, meeting_id, action_id, token_index, literal_context)
            )
        elif threshold_paste and literal_context == "code":
            steps.extend(
                _paste_literal_steps(
                    value, meeting_id, action_id, token_index, literal_context
                )
            )
        else:
            steps.extend(
                _normal_literal_steps(
                    value, profile, meeting_id, action_id, token_index, literal_context
                )
            )

    if allow_paste and context in {"vim", "python", "code"} and not threshold_paste:
        planned = sum(step.delay_after_ms for step in steps)
        if planned > spec.action_limit_ms:
            hand_typed_budget = min(3_000, spec.action_limit_ms // 4)
            converted: List[TypingStep] = []
            elapsed = 0
            paste_buffer = ""

            def flush_paste() -> None:
                nonlocal paste_buffer
                if not paste_buffer:
                    return
                converted.extend(
                    _paste_literal_steps(
                        paste_buffer,
                        meeting_id,
                        action_id,
                        len(converted),
                        "code",
                    )
                )
                paste_buffer = ""

            for step in steps:
                keep_typing = elapsed < hand_typed_budget or step.input_context == "editor_command"
                if step.kind == "literal" and step.input_context == "code" and not keep_typing:
                    paste_buffer += step.value
                else:
                    flush_paste()
                    converted.append(step)
                if keep_typing:
                    elapsed += step.delay_after_ms
            flush_paste()
            steps = converted

    numbered = _renumber(steps)
    return TypingPlan(
        numbered,
        literal_characters,
        sum(step.delay_after_ms for step in numbered),
        any(step.change_kind == "paste" for step in numbered),
    )
