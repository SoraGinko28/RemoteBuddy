"""Shared-pane capture, workspace path resolution, and runner errors."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from meeting_v2 import MeetingStore
from terminal_text import parse_segments, plain_of
from tmux_transport import TmuxTransport


class RunnerError(RuntimeError):
    pass


class RunnerStopped(RunnerError):
    pass


class TypingInterrupted(RunnerError):
    def __init__(self, sent: int, remaining: int, submitted: bool = False):
        super().__init__("typing interrupted")
        self.sent = sent
        self.remaining = remaining
        self.submitted = submitted


# Covers the escape families a tmux ``capture-pane -e`` frame can contain:
# CSI (colors, cursor, SGR styles), OSC (title, hyperlinks), DCS/PM/APC/SOS,
# charset selection, keypad, and single-character escapes.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"           # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC (ST may be BEL or ESC \)
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"        # DCS / PM / APC / SOS
    r"|\x1b[()][0-9A-B]"                     # charset selection
    r"|\x1b[=>]"                             # keypad modes
    r"|\x1b[@-Z\\-_]"                        # remaining single-char escapes
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences, keeping the visible plain text."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def resolve_workspace_path(store: MeetingStore, current_path: str, requested: str, command: str) -> Path:
    if not requested or Path(requested).is_absolute() or "\x00" in requested:
        raise RunnerError(f"{command} requires a relative workspace path")
    root = store.workspace_path.resolve()
    current = Path(current_path).resolve()
    if not _inside(root, current):
        raise RunnerError("terminal is outside the shared workspace")
    candidate = (current / requested).resolve()
    if not _inside(root, candidate):
        raise RunnerError("requested path escapes the shared workspace")
    if command == "cd" and not candidate.is_dir():
        raise RunnerError("cd target is not a directory")
    if command == "vim" and not candidate.parent.is_dir():
        raise RunnerError("vim target parent does not exist")
    if command == "python" and (candidate.suffix != ".py" or not candidate.is_file()):
        raise RunnerError("python target must be an existing .py file in the workspace")
    return candidate


def capture_workspace(
    store: MeetingStore,
    transport: TmuxTransport,
    change_kind: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    pane_id = store.meeting.pane_id
    if not pane_id:
        raise RunnerError("meeting has no workspace pane")
    info = None
    content = ""
    # Metadata and the viewport are separate tmux queries. Retry once when
    # the pane changes between them so an event never describes a screen with
    # the wrong process, path, size, or cursor.
    for _attempt in range(2):
        before = transport.pane_info(pane_id)
        if before["dead"] != "0":
            raise RunnerError("shared workspace pane exited")
        ansi_text = None
        ansi_capture = getattr(transport, "capture_visible_ansi", None)
        if callable(ansi_capture):
            ansi_text = ansi_capture(pane_id)
            segments = parse_segments(ansi_text)
            content = plain_of(segments)
        else:
            segments = None
            content = transport.capture_visible(pane_id)
        after = transport.pane_info(pane_id)
        signature = ("command", "path", "cursor_x", "cursor_y", "pane_width", "pane_height", "dead")
        if all(before.get(key) == after.get(key) for key in signature):
            info = after
            break
        info = after
    if info is None or info["dead"] != "0":
        raise RunnerError("shared workspace pane exited")
    screen_metadata = dict(metadata or {})
    screen_metadata.update(
        {
            "cursor_x": int(info["cursor_x"]),
            "cursor_y": int(info["cursor_y"]),
            "pane_width": int(info["pane_width"]),
            "pane_height": int(info["pane_height"]),
        }
    )
    return store.append_screen(
        content,
        info["command"],
        info["path"],
        change_kind=change_kind,
        metadata=screen_metadata,
        segments=segments,
    )


def capture_stable_workspace(
    store: MeetingStore,
    transport: TmuxTransport,
    metadata: Optional[Dict[str, Any]] = None,
    attempts: int = 3,
) -> Optional[Dict[str, Any]]:
    """Capture after two consecutive raw frames agree.

    Stability is checked against the transport's raw viewport rather than
    journal events: ``append_screen`` deduplicates identical frames to
    ``None``, so comparing its return value can never observe "unchanged".
    """
    pane_id = store.meeting.pane_id
    if not pane_id:
        raise RunnerError("meeting has no workspace pane")
    previous: Optional[str] = None
    for index in range(max(2, attempts)):
        current = transport.capture_visible(pane_id)
        if previous is not None and current == previous:
            break
        previous = current
        if index + 1 < max(2, attempts):
            time.sleep(0.03)
    return capture_workspace(store, transport, metadata=metadata)

