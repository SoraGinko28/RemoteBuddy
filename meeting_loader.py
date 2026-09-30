"""meeting_loader — load the real journal under runs/<meeting_id>/ into a MeetingData.

Design intent (aligned with MEETING_ADT.md + meeting_adt.py):
- Project only "events.jsonl + meeting.json -> MeetingData"; read-only, the
  journal is never modified.
- The ADT four streams are the capability layer; the loader is one slice of
  the storage format:
  the kind-to-stream mapping is the core of the storage format; field
  mismatches are left for the validator to report, then fixed afterward.
- No copying / dedup / indexing at the persistence layer: the loader stays a
  plain sequential reader.

kind-to-stream mapping (see runs/20260902-153537-7f1bea):
  chat                    -> chat stream ChatMessage (content is the text;
                             actor -> user_name)
  screen                  -> screen stream ScreenCapture (content is the
                             plain frame text)
  control (acquired)      -> terminal-control stream TerminalControl (start
                             of a human takeover interval)
  action / state / error  -> not mapped (operational noise, not part of the
                             five streams)
  keystroke               -> not a stream: keystrokes stay in the harness and
                             are never persisted

Known limitations (surfaced in validation reports; the journal is never
modified):
  - The current journal has no role metadata;
    agent_name2role can only be inferred on the assumption that every agent
    that appears is an actor (valid for single-agent meetings).
  - control events carry only acquired/released, with no tmux pane info:
    TerminalControl records tmux_window_name/pane as missing; they are to be
    filled in by the runner that owns the pane.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import meeting_adt as adt

# phase values on control events that determine acquired/released
_ACQUIRED_PHASES = {"human_control"}
_RELEASED_PHASES = {"agent_thinking", "waiting_for_user"}

# Operational-noise kinds: never mapped into the five streams
_NOISE_KINDS = {"action", "state", "error"}
_MARKER_RE = __import__("re").compile(r"(?:cmd|retcode)-remotebuddy-([0-9a-z]+)")

# Non-terminal-owner actors: the harness is RemoteBuddy itself and does not
# produce a terminal_control record (its acquired is only a session-level
# takeover; it does not change "who holds the tmux")
_NON_OWNER_ACTORS = {"harness"}


@dataclass
class LoadSummary:
    """Counting summary of one load (for printing / test assertions)."""

    event_total: int = 0
    mapped: dict = field(default_factory=dict)  # stream_name -> record count
    skipped_noise: int = 0
    skipped_unrecognized: int = 0
    skipped_malformed: int = 0


def _role_map_from_events(events: list[dict], manifest: dict) -> dict:
    """agent_name2role: what the manifest recorded, or what the journal implies.

    A manifest written since identity exists carries the map; journals from
    before it carry none, and there every non-human agent that appears is
    inferred to be an actor (the tmux owner in a single-agent meeting). $human
    never enters the role map.
    """
    recorded = manifest.get("agent_name2role")
    if isinstance(recorded, dict) and recorded:
        return {str(name): _role_of(role) for name, role in recorded.items()}
    agents: set[str] = set()
    for event in events:
        actor = event.get("actor")
        if isinstance(actor, str) and actor not in ("human", "workspace", "harness"):
            agents.add(actor)
    return {name: adt.AgentRole.ACTOR for name in sorted(agents)}


def _role_of(role: object) -> "adt.AgentRole":
    """One role as the manifest writes it: the name, with or without its `$`."""
    text = str(role)
    return adt.AgentRole(text if text.startswith("$") else f"${text}")


def _time_of(event: dict) -> Optional[object]:
    captured = event.get("captured_at")
    if isinstance(captured, str):
        try:
            return adt._iso_parse(captured)
        except ValueError:
            return None
    return None


def _chat_message(event: dict) -> Optional[adt.ChatMessage]:
    content = event.get("content")
    if not isinstance(content, str):
        return None
    time = _time_of(event)
    if time is None:
        return None
    user_name = adt.HUMAN_USER_NAME if event.get("actor") == "human" else event.get("actor", "")
    if not user_name:
        return None
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    attachments = []
    for item in data.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        try:
            attachments.append(adt.image_attachment_from_dict(item))
        except (ValueError, TypeError):
            continue
    return adt.ChatMessage(
        user_name=user_name,
        time=time,
        message=[adt.RawMessage(text=content)] if content else [],
        attachments=attachments,
    )


def _normalize_frame(content: str, height: int) -> str:
    """Normalize the screen text saved in the journal into ADT screen_data.

    Rules (per ADT: each screen_data row is right-trimmed, there are no
    trailing blank rows, and the row count == height):
      1. rstrip(' ') on every row;
      2. drop all-blank rows at the end (no trailing blank rows);
      3. when there are fewer than height rows, pad blank rows to a full
         frame — a full terminal frame is height rows, and the journal often
         omits the bottom blank rows, so the projection is a full-frame record.
    Returns text with exactly height rows.
    """
    rows = [r.rstrip(" ") for r in content.split("\n")]
    while rows and rows[-1] == "":
        rows.pop()
    if len(rows) < height:
        rows.extend([""] * (height - len(rows)))
    return "\n".join(rows[:height])


def _screen_capture(event: dict) -> Optional[adt.ScreenCapture]:
    content = event.get("content")
    if not isinstance(content, str) or not content:
        return None
    time = _time_of(event)
    if time is None:
        return None
    data = event.get("data") or {}
    height = int(data.get("pane_height", 0) or 0)
    width = int(data.get("pane_width", 0) or 0)
    screen_data = _normalize_frame(content, height) if height > 0 else content
    return adt.ScreenCapture(
        screen_data=screen_data,
        terminal_text_content=[adt.TerminalTextSegment(text=screen_data)],
        cursor_x=int(data.get("cursor_x", 0) or 0),
        cursor_y=int(data.get("cursor_y", 0) or 0),
        screen_width=width,
        screen_height=height,
        captured_at=time,
    )


def _terminal_controls(events: list[dict]) -> "list[adt.TerminalControl]":
    controls: list[adt.TerminalControl] = []
    for event in events:
        if event.get("kind") != "control":
            continue
        phase = event.get("phase")
        if phase not in _ACQUIRED_PHASES:
            continue
        status = (event.get("data") or {}).get("status")
        if status != "acquired":
            continue
        time = _time_of(event)
        if time is None:
            continue
        actor = event.get("actor")
        if actor in _NON_OWNER_ACTORS:
            continue  # the harness is not a terminal owner; keep no record
        user_name = adt.HUMAN_USER_NAME if actor == "human" else actor or ""
        if not user_name:
            continue
        controls.append(adt.TerminalControl(user_name=user_name, start_time=time))
    controls.sort(key=lambda c: c.start_time or _dt.datetime.min)
    return controls


def _screen_owner_runs(screens: "list[adt.ScreenCapture]",
                       controls: "list[adt.TerminalControl]",
                       default_owner: str) -> "list[adt.ScreenStreamChunk]":
    """Split the screen stream into chunks of consecutive captures with the
    same owner.

    owner(t): falls inside a human takeover interval => $human; otherwise the
    default actor agent.
    """
    spans: list[tuple] = []
    for control in controls:
        if adt.is_human(control.user_name):
            spans.append((control.start_time, None, adt.HUMAN_USER_NAME))

    def owner_of(capture: adt.ScreenCapture) -> str:
        for start, _end, owner in spans:
            if start is not None and capture.captured_at is not None \
                    and capture.captured_at >= start:
                return owner
        return default_owner

    chunks: list[adt.ScreenStreamChunk] = []
    for capture in screens:
        owner = owner_of(capture)
        if not chunks or chunks[-1].owner_user_name != owner:
            chunks.append(adt.ScreenStreamChunk(
                stream_name=adt.SCREEN_STREAM_NAME, owner_user_name=owner,
                chunks=[capture]))
        else:
            chunks[-1].chunks.append(capture)
    return chunks


def _agent_cmd_stream(events: list[dict]) -> "adt.AgentCmdStreamChunk":
    """Project received command/diff actions into the agent_cmd stream."""
    by_id: dict[str, adt.AgentCmdMessage] = {}
    ordered: list[adt.AgentCmdMessage] = []
    effective: dict[str, object] = {}
    rejected: set[str] = set()
    for event in events:
        if event.get("kind") != "action":
            continue
        data = event.get("data") or {}
        action_id = data.get("action_id")
        if not isinstance(action_id, str):
            continue
        status = data.get("status")
        action = data.get("action")
        if status == "received" and isinstance(action, dict):
            if action.get("type") == "command" and isinstance(action.get("command"), str):
                agent_cmd = adt.CmdLine(cmd=action["command"])
            elif action.get("type") == "diff" and isinstance(action.get("diff"), str):
                agent_cmd = adt.DiffText(appliable_diff=action["diff"])
            else:
                continue
            message = adt.AgentCmdMessage(
                agent_cmd=agent_cmd,
                agent_name=str(event.get("actor") or ""),
                produced_time=_time_of(event),
            )
            by_id[action_id] = message
            ordered.append(message)
        elif status == "rejected":
            rejected.add(action_id)
        elif status == "result":
            effective[action_id] = _time_of(event)

    for action_id, message in by_id.items():
        if action_id not in rejected:
            message.effective_time = effective.get(action_id)
        if action_id in rejected or message.effective_time is None:
            continue
        end = message.effective_time
        if isinstance(message.agent_cmd, adt.CmdLine):
            marker_values: list[str] = []
            for event in events:
                if event.get("kind") != "screen":
                    continue
                captured = _time_of(event)
                if message.produced_time is not None and captured is not None and captured < message.produced_time:
                    continue
                if captured is not None and captured > end:
                    continue
                marker_values.extend(_MARKER_RE.findall(str(event.get("content") or "")))
            if marker_values:
                message.token = marker_values[-1]
    return adt.AgentCmdStreamChunk(chunks=ordered)


def load_meeting_data_from_run(run_dir: Path) -> tuple[adt.MeetingData, LoadSummary]:
    """Load a MeetingData from runs/<id>; raise FileNotFoundError when files
    are missing."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / "meeting.json"
    events_path = run_dir / "streams" / "events.jsonl"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing meeting.json in {run_dir}")
    if not events_path.is_file():
        raise FileNotFoundError(f"missing events.jsonl in {run_dir}")

    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    events: list[dict] = []
    event_total = 0
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            event_total += 1
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # malformed line: counted in total, not in events
    events.sort(key=lambda e: e.get("seq", 0))

    summary = LoadSummary(event_total=event_total)
    summary.skipped_malformed = event_total - len(events)
    chats: list[adt.ChatMessage] = []
    screens: list[adt.ScreenCapture] = []
    for event in events:
        kind = event.get("kind")
        if kind == "chat":
            msg = _chat_message(event)
            if msg is not None:
                chats.append(msg)
                summary.mapped[adt.CHAT_STREAM_NAME] = summary.mapped.get(
                    adt.CHAT_STREAM_NAME, 0) + 1
        elif kind == "screen":
            cap = _screen_capture(event)
            if cap is not None:
                screens.append(cap)
                summary.mapped[adt.SCREEN_STREAM_NAME] = summary.mapped.get(
                    adt.SCREEN_STREAM_NAME, 0) + 1
        elif kind in _NOISE_KINDS:
            summary.skipped_noise += 1
        else:
            summary.skipped_unrecognized += 1

    controls = _terminal_controls(events)
    summary.mapped[adt.TERMINAL_CONTROL_STREAM_NAME] = len(controls)
    agent_cmd_chunk = _agent_cmd_stream(events)
    summary.mapped[adt.AGENT_CMD_STREAM_NAME] = len(agent_cmd_chunk.chunks)

    role_map = _role_map_from_events(events, manifest)
    default_owner = next(iter(sorted(role_map)), "")

    chunks: list = _screen_owner_runs(screens, controls, default_owner)
    if chats:
        chunks.append(adt.ChatStreamChunk(
            stream_name=adt.CHAT_STREAM_NAME, chunks=chats))
    if controls:
        chunks.append(adt.TerminalControlStreamChunk(
            stream_name=adt.TERMINAL_CONTROL_STREAM_NAME, chunks=controls))
    if agent_cmd_chunk.chunks:
        chunks.append(agent_cmd_chunk)
    chunks.sort(key=_chunk_start_time)

    meeting = adt.MeetingData(
        meeting_uid=str(manifest.get("meeting_id") or run_dir.name),
        meeting_name=str(manifest.get("session_name") or run_dir.name),
        agent_name2role=role_map,
        sorted_stream_chunks=chunks,
    )
    return meeting, summary


def _chunk_start_time(chunk: object):
    chunks = getattr(chunk, "chunks", [])
    if not chunks:
        return None
    first = chunks[0]
    for attr in ("captured_at", "time", "start_time", "produced_time"):
        if hasattr(first, attr) and getattr(first, attr) is not None:
            return getattr(first, attr)
    return None
