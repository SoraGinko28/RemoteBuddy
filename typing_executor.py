"""Human-paced typing execution against the shared tmux pane."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Sequence

from meeting import MeetingStore
from tmux_transport import TmuxTransport, workspace_command_text
from typing_policy import TypingPlan, plan_input
from workspace_capture import TypingInterrupted, capture_workspace


def send_tokens_with_captures(
    store: MeetingStore,
    transport: TmuxTransport,
    pane_id: str,
    tokens: Sequence[Dict[str, str]],
    settle_seconds: float = 0.05,
    typing_interval_seconds: float = 0.0,
    *,
    meeting_id: str = "legacy",
    action_id: str = "legacy-action",
    profile: str = "balanced",
    context: str = "code",
    interrupt_check: Optional[Any] = None,
) -> None:
    """Execute a deterministic burst plan; zero interval preserves old fast callers."""
    if typing_interval_seconds <= 0:
        for token in tokens:
            if token.get("kind") == "rest":
                time.sleep(int(token["value"]) / 1000.0)
                continue
            transport.send_tokens(pane_id, [token])
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            capture_workspace(store, transport)
        return
    plan = plan_input(
        tokens,
        profile,
        meeting_id,
        action_id,
        context,
        allow_paste=False,
    )
    _send_typing_plan(store, transport, pane_id, plan, action_id, interrupt_check)


def send_line_with_captures(
    store: MeetingStore,
    transport: TmuxTransport,
    pane_id: str,
    text: str,
    typing_interval_seconds: float,
    *,
    meeting_id: str = "legacy",
    action_id: str = "legacy-action",
    profile: str = "balanced",
    interrupt_check: Optional[Any] = None,
) -> None:
    """把一整行原样敲进终端（含回车），按人的节奏。

    这一行是 harness 自己生成的（一条动作命令，或者它自己拼出来的编辑器命令行），所以这里
    不做 `workspace_command_text` 那道面向 actor 的校验；校验在生成这一行的地方已经做过了。
    """
    if typing_interval_seconds <= 0:
        transport.send_keys(pane_id, text)
        return
    plan = plan_input(
        [
            {"kind": "text", "value": text},
            {"kind": "ctrl", "value": "Enter"},
        ],
        profile,
        meeting_id,
        action_id,
        "shell",
        allow_paste=False,
    )
    _send_typing_plan(store, transport, pane_id, plan, action_id, interrupt_check)


def _send_typing_plan(
    store: MeetingStore,
    transport: TmuxTransport,
    pane_id: str,
    plan: TypingPlan,
    action_id: str,
    interrupt_check: Optional[Any],
) -> None:
    deadline = time.monotonic()
    sent = 0
    submitted = False
    last_paste_capture = float("-inf")
    for step_index, step in enumerate(plan.steps):
        if interrupt_check and interrupt_check():
            raise TypingInterrupted(sent, plan.total_characters - sent, submitted)
        if step.kind == "text":
            transport.send_literal(pane_id, step.value)
            sent += len(step.value)
        elif step.kind == "ctrl":
            transport.send_special(pane_id, step.value)
            submitted = submitted or (step.value == "Enter" and step.input_context == "shell")
        # A rest step sends nothing; its delay_after_ms holds the pacing below.
        now = time.monotonic()
        next_kind = plan.steps[step_index + 1].change_kind if step_index + 1 < len(plan.steps) else None
        should_capture = (
            step.change_kind != "paste"
            or now - last_paste_capture >= 0.1
            or next_kind != "paste"
        )
        if should_capture:
            capture_workspace(
                store,
                transport,
                change_kind=step.change_kind,
                metadata={
                    "typing_profile": store.meeting.typing_profile,
                    "typing_policy_version": "1",
                    "action_id": action_id,
                    "burst_index": step.burst_index,
                    "planned_delay_ms": step.delay_after_ms,
                    "input_context": step.input_context,
                },
            )
            if step.change_kind == "paste":
                last_paste_capture = now
        # Captures and journal writes consume real time; without this clamp the
        # deadline falls permanently behind and every later delay is skipped,
        # collapsing the human-like pacing into full speed.
        deadline = max(deadline, time.monotonic()) + step.delay_after_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
            if interrupt_check and interrupt_check():
                raise TypingInterrupted(sent, plan.total_characters - sent, submitted)

