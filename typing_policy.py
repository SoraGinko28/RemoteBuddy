from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace
from typing import Dict, List, Sequence, Tuple


TYPING_POLICY_VERSION = "1"
TYPING_PROFILES = ("demo", "balanced", "fast")


@dataclass(frozen=True)
class ProfileSpec:
    interval_min_ms: int
    interval_max_ms: int
    semantic_min_ms: int
    semantic_max_ms: int
    paste_threshold_chars: int
    paste_threshold_lines: int
    action_limit_ms: int


PROFILE_SPECS: Dict[str, ProfileSpec] = {
    "demo": ProfileSpec(140, 220, 220, 450, 160, 6, 30_000),
    "balanced": ProfileSpec(90, 150, 150, 320, 240, 8, 20_000),
    "fast": ProfileSpec(55, 100, 90, 200, 400, 12, 12_000),
}

STRONG_BOUNDARIES = set(":;)]}")
MEDIUM_BOUNDARIES = set(",=+-*/%<>|&^")
# 一行打完到按下回车之间固定停这么久：前摇要让人来得及读那一行。
# actor 自己用 `$rest` 点的拍子不动 —— 那是它要求的停顿。
ENTER_WIND_UP_MS = 800


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
    """One literal goes out whole: the token grammar already refuses control
    characters, so a literal is a single line, and typing it in pieces is what
    made the actor look like it was dribbling characters.

    How long to hold after a line is not the harness's call — an actor that wants
    the human to read one asks for it with ``$rest`` — so the pause here is only
    the profile's.
    """
    spec = PROFILE_SPECS[profile]
    rng = _rng(meeting_id, action_id, token_index, context)
    tempo = rng.uniform(0.84, 1.16)
    delay = _pause_for(text, "", spec, rng, tempo)
    return [TypingStep("text", text, delay, "typing", context, 0)]


def _paste_literal_steps(
    text: str,
    meeting_id: str,
    action_id: str,
    token_index: int,
    context: str,
) -> List[TypingStep]:
    rng = _rng(meeting_id, action_id, token_index, context + ":paste")
    return [
        TypingStep("text", text[offset : offset + 80], rng.randint(20, 50), "paste", context, 0)
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
    # An Enter at a shell prompt submits the line; an Enter anywhere else is
    # just a key handed to whatever program holds the pane's foreground.
    key_context = "shell" if context == "shell" else "program"
    return TypingStep("ctrl", key, delay, "typing", key_context, 0)


def _with_enter_wind_up(steps: Sequence[TypingStep]) -> List[TypingStep]:
    """一行打完到按下回车之间固定停 ``ENTER_WIND_UP_MS``。

    暂停挂在按键前面那一步上：计划是"发一步、停它的 delay、再发下一步"，
    所以回车的前摇就是它前一个 literal 的停顿。``$rest`` 是 actor 点的拍子，
    留着它自己的毫秒数。
    """
    held = list(steps)
    for index, step in enumerate(held):
        if index == 0 or step.kind != "ctrl" or step.value != "Enter":
            continue
        previous = held[index - 1]
        if previous.kind == "text":
            held[index - 1] = replace(previous, delay_after_ms=ENTER_WIND_UP_MS)
    return held


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
        len(token["value"]) for token in tokens if token.get("kind") == "text"
    )
    lines = 1 + sum(
        token.get("kind") == "ctrl" and token.get("value") == "Enter" for token in tokens
    )
    threshold_paste = allow_paste and context in {"code", "python"} and (
        literal_characters >= spec.paste_threshold_chars or lines >= spec.paste_threshold_lines
    )
    steps: List[TypingStep] = []
    for token_index, token in enumerate(tokens):
        kind = token["kind"]
        value = token["value"]
        if kind == "ctrl":
            steps.append(
                _key_step(value, profile, meeting_id, action_id, token_index, context)
            )
        elif kind == "rest":
            # A musical rest: nothing is sent, the pacing simply holds for the
            # requested milliseconds before the next step goes out.
            rest_ms = int(value)
            steps.append(TypingStep("rest", "", rest_ms, "rest", context, 0))
        elif threshold_paste:
            steps.extend(
                _paste_literal_steps(
                    value, meeting_id, action_id, token_index, context
                )
            )
        else:
            steps.extend(
                _normal_literal_steps(
                    value, profile, meeting_id, action_id, token_index, context
                )
            )

    if allow_paste and context in {"code", "python"} and not threshold_paste:
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
                keep_typing = elapsed < hand_typed_budget
                if step.kind == "text" and step.input_context == "code" and not keep_typing:
                    paste_buffer += step.value
                else:
                    flush_paste()
                    converted.append(step)
                if keep_typing:
                    elapsed += step.delay_after_ms
            flush_paste()
            steps = converted

    steps = _with_enter_wind_up(steps)
    numbered = _renumber(steps)
    return TypingPlan(
        numbered,
        literal_characters,
        sum(step.delay_after_ms for step in numbered),
        any(step.change_kind == "paste" for step in numbered),
    )
