from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent_protocol import (
    SPECIAL_KEYS,
    STRUCTURED_COMMANDS,
    AgentProtocolError,
    validate_shell_command,
)

try:
    import libtmux
    from libtmux.pane import PaneDirection
except ImportError:  # Keep doctor and the read-only Web UI usable before setup.
    libtmux = None
    PaneDirection = None


SESSION_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
PANE_RE = re.compile(r"^%[0-9]+$")
# The schema layer owns both sets; the transport must accept exactly what the
# schema can emit, so they are shared rather than kept in lockstep by hand.
WORKSPACE_COMMANDS = STRUCTURED_COMMANDS
TMUX_SPECIAL_KEY_NAMES = {
    "Backspace": "BSpace",
    "Delete": "DC",
}


class TmuxError(RuntimeError):
    pass


_SESSION_CREATION_LOCK = threading.RLock()


def libtmux_version() -> Optional[str]:
    return getattr(libtmux, "__version__", None) if libtmux is not None else None


def _workspace_environment() -> Dict[str, str]:
    """Make the tmux shell use the same Python environment as the Harness."""
    python_bin = str(Path(sys.executable).resolve().parent)
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    path_parts = [part for part in path_parts if Path(part).resolve() != Path(python_bin)]
    environment = {"PATH": os.pathsep.join([python_bin, *path_parts])}
    prefix = Path(sys.prefix).resolve()
    if (prefix / "conda-meta").is_dir():
        environment.update(
            {
                "CONDA_PREFIX": str(prefix),
                "CONDA_DEFAULT_ENV": os.environ.get("CONDA_DEFAULT_ENV", prefix.name),
                "CONDA_SHLVL": os.environ.get("CONDA_SHLVL", "1"),
            }
        )
    else:
        environment["CONDA_DEFAULT_ENV"] = os.environ.get("CONDA_DEFAULT_ENV", "remotebuddy")
    return environment


def _command_in_workspace_environment(arguments: Sequence[str]) -> str:
    environment = _workspace_environment()
    assignments = [f"{name}={value}" for name, value in environment.items()]
    return shlex.join(["/usr/bin/env", *assignments, *arguments])


def workspace_command_text(command: str, path: str = "") -> str:
    """Build the shell command text the shared workspace may receive.

    Structured commands (pwd/cd/python) keep safe quoting and a single
    validated path. Any other value is treated as a full bash command line
    under the relaxed policy: it passes through verbatim after the
    deletion-class and shared-interface checks. Starting an interactive
    program (an editor, a REPL, a pager) is one of those command lines.
    """
    if command not in WORKSPACE_COMMANDS:
        if path:
            raise TmuxError("a bash command line does not accept a separate path")
        try:
            validate_shell_command(command)
        except AgentProtocolError as exc:
            raise TmuxError(str(exc)) from exc
        return command
    if command == "pwd":
        if path:
            raise TmuxError("pwd does not accept a path")
        return "pwd"
    if not path or "\n" in path or "\r" in path or "\x00" in path:
        raise TmuxError(f"{command} requires one safe path")
    executable = "python3" if command == "python" else command
    return shlex.join([executable, "--", path])


class TmuxTransport:
    """Typed adapter around libtmux for RemoteBuddy's shared terminal."""

    def __init__(
        self,
        tmux_binary: str = "tmux",
        server: Optional[Any] = None,
    ) -> None:
        self.tmux_binary = tmux_binary
        if server is None:
            if libtmux is None:
                raise TmuxError("libtmux is not installed; run python3 -m pip install -r requirements.txt")
            resolved = shutil.which("tmux")
            if not resolved:
                raise TmuxError(f"tmux executable not found: {tmux_binary}")
            server = libtmux.Server()
        self._server = server
        self._panes: Dict[str, Any] = {}

    def _invoke(self, label: str, operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except TmuxError:
            raise
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise TmuxError(f"tmux {label} failed: {detail}") from exc

    @staticmethod
    def _output_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.rstrip("\n")
        return "\n".join(str(line) for line in value).rstrip("\n")

    def _checked_cmd(self, owner: Any, command: str, *arguments: str) -> Any:
        result = self._invoke(command, owner.cmd, command, *arguments)
        returncode = int(getattr(result, "returncode", 0))
        stderr = self._output_text(getattr(result, "stderr", None))
        if returncode != 0 or stderr:
            detail = stderr or self._output_text(getattr(result, "stdout", None)) or "unknown tmux error"
            raise TmuxError(f"tmux {command} failed: {detail}")
        return result

    def _pane(self, pane_id: str) -> Any:
        self._validate_pane(pane_id)
        pane = self._panes.get(pane_id)
        if pane is not None:
            return pane
        pane = self._invoke("pane lookup", self._server.panes.get, pane_id=pane_id)
        if pane is None:
            raise TmuxError(f"tmux pane not found: {pane_id}")
        self._panes[pane_id] = pane
        return pane

    @staticmethod
    def _validate_session(session_name: str) -> None:
        if not SESSION_RE.fullmatch(session_name):
            raise TmuxError(f"invalid tmux session name: {session_name}")

    @staticmethod
    def _validate_pane(pane_id: str) -> None:
        if not PANE_RE.fullmatch(pane_id):
            raise TmuxError(f"invalid tmux pane id: {pane_id}")

    def session_exists(self, session_name: str) -> bool:
        self._validate_session(session_name)
        return bool(self._invoke("session lookup", self._server.has_session, session_name, exact=True))

    def create_two_agent_session(
        self,
        session_name: str,
        workdir: Path,
        codex_binary: str,
        model: Optional[str] = None,
    ) -> Dict[str, str]:
        self._validate_session(session_name)
        if self.session_exists(session_name):
            raise TmuxError(f"tmux session already exists: {session_name}")

        resolved_workdir = workdir.resolve()
        quoted_workdir = json.dumps(str(resolved_workdir), ensure_ascii=False)
        trust_override = f'projects={{{quoted_workdir}={{trust_level="trusted"}}}}'
        codex_arguments: List[str] = [
            codex_binary,
            "--no-alt-screen",
            "-s",
            "workspace-write",
            "-a",
            "never",
            "-C",
            str(resolved_workdir),
            "-c",
            trust_override,
        ]
        if model:
            codex_arguments.extend(["-m", model])
        shell_command = _command_in_workspace_environment(codex_arguments)

        with _SESSION_CREATION_LOCK:
            session = self._invoke(
                "new-session",
                self._server.new_session,
                session_name=session_name,
                attach=False,
                start_directory=str(resolved_workdir),
                window_name="meeting",
                window_command=shell_command,
                environment=_workspace_environment(),
            )
        window = self._invoke("active window lookup", lambda: session.active_window)
        if window is None:
            raise TmuxError("tmux created a session without an active window")
        proposer = self._invoke("active pane lookup", lambda: window.active_pane)
        if proposer is None:
            raise TmuxError("tmux created a window without an active pane")
        proposer_pane = str(proposer.pane_id)
        self._validate_pane(proposer_pane)

        reviewer = self._invoke(
            "split-window",
            window.split,
            start_directory=str(resolved_workdir),
            attach=False,
            direction=PaneDirection.Right,
            shell=shell_command,
        )
        reviewer_pane = str(reviewer.pane_id)
        self._validate_pane(reviewer_pane)
        self._panes.update({proposer_pane: proposer, reviewer_pane: reviewer})
        for option, value in (("allow-rename", "off"), ("remain-on-exit", "on"), ("history-limit", "5000")):
            self._invoke("window option", window.set_window_option, option, value)
        self._checked_cmd(proposer, "select-pane", "-T", "proposer")
        self._checked_cmd(reviewer, "select-pane", "-T", "reviewer")
        self._invoke("layout selection", window.select_layout, "even-horizontal")
        return {"proposer": proposer_pane, "reviewer": reviewer_pane}

    def create_workspace_session(
        self,
        session_name: str,
        workdir: Path,
        shell_binary: str = "/bin/bash",
        width: int = 120,
        height: int = 36,
    ) -> str:
        self._validate_session(session_name)
        if self.session_exists(session_name):
            raise TmuxError(f"tmux session already exists: {session_name}")
        if width < 40 or width > 300 or height < 12 or height > 120:
            raise TmuxError("workspace dimensions are outside supported bounds")
        resolved_workdir = workdir.expanduser().resolve()
        if not resolved_workdir.is_dir():
            raise TmuxError(f"workspace directory does not exist: {resolved_workdir}")
        shell_command = _command_in_workspace_environment(
            [shell_binary, "--noprofile", "--norc", "-i"]
        )
        with _SESSION_CREATION_LOCK:
            session = self._invoke(
                "new-session",
                self._server.new_session,
                session_name=session_name,
                attach=False,
                start_directory=str(resolved_workdir),
                window_name="workspace",
                window_command=shell_command,
                x=width,
                y=height,
                environment=_workspace_environment(),
            )
        window = self._invoke("active window lookup", lambda: session.active_window)
        if window is None:
            raise TmuxError("tmux created a session without an active window")
        pane = self._invoke("active pane lookup", lambda: window.active_pane)
        if pane is None:
            raise TmuxError("tmux created a window without an active pane")
        pane_id = str(pane.pane_id)
        self._validate_pane(pane_id)
        self._panes[pane_id] = pane
        for option, value in (
            ("allow-rename", "off"),
            ("remain-on-exit", "on"),
            ("history-limit", "5000"),
            ("window-size", "manual"),
        ):
            self._invoke("window option", window.set_window_option, option, value)
        self._checked_cmd(pane, "select-pane", "-T", "shared workspace")
        return pane_id

    # tmux builds one command string out of `send-keys`' argv, and rejects it
    # with "command too long" past its own limit. Hex mode costs two characters
    # per input byte, plus an argv slot each, so a long line (an agent pasting a
    # whole expression, say) must be handed over in several calls.
    SEND_HEX_CHUNK_BYTES = 1024

    def _send_hex_bytes(self, pane: Any, encoded: bytes) -> None:
        for start in range(0, len(encoded), self.SEND_HEX_CHUNK_BYTES):
            chunk = encoded[start : start + self.SEND_HEX_CHUNK_BYTES]
            self._checked_cmd(
                pane,
                "send-keys",
                "-H",
                *[f"{byte:02x}" for byte in chunk],
            )

    def send_literal(self, pane_id: str, text: str) -> None:
        if not text or "\n" in text or "\r" in text:
            raise TmuxError("send_literal requires one non-empty line of text")
        pane = self._pane(pane_id)
        # tmux parses ``;`` as a command separator even when it arrives as a
        # separate argv. Hex mode bypasses that parser and preserves every
        # UTF-8 byte, including quotes, backslashes, tabs, and Unicode.
        self._send_hex_bytes(pane, text.encode("utf-8"))

    def send_hex(self, pane_id: str, hex_text: str) -> None:
        """Inject raw bytes (hex) into the pane, exactly as if typed locally.

        This is the takeover input path: the web terminal forwards its key
        bytes verbatim (control characters, UTF-8 text, CSI sequences) so
        tmux and the foreground program see the same bytes a local keyboard
        would produce.
        """
        if not hex_text or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", hex_text):
            raise TmuxError("send_hex requires non-empty even-length hex")
        if len(hex_text) // 2 > 128 * 1024:
            raise TmuxError("send_hex payload is too large")
        pane = self._pane(pane_id)
        self._send_hex_bytes(pane, bytes.fromhex(hex_text))

    def start_output_pipe(self, pane_id: str, target_path: str) -> None:
        """Pipe the pane's output bytes to a file (tmux ``pipe-pane -O``).

        Only new output is captured, so callers seed the screen first and
        then serve strictly newer bytes from this file. A pane may hold one
        pipe at a time.
        """
        pane = self._pane(pane_id)
        command = "cat >> " + shlex.quote(str(target_path))
        self._checked_cmd(pane, "pipe-pane", command)

    def stop_output_pipe(self, pane_id: str) -> None:
        """Close the pane's output pipe (``pipe-pane`` with no command)."""
        pane = self._pane(pane_id)
        self._checked_cmd(pane, "pipe-pane")

    def resize_workspace_session(self, session_name: str, cols: int, rows: int) -> None:
        """Resize the workspace window to an exact grid (takeover viewport fit).

        The workspace window keeps ``window-size manual``, so it never follows
        an attached client; an explicit ``resize-window`` is the only thing
        that moves it. Bounds mirror ``create_workspace_session``.
        """
        self._validate_session(session_name)
        if not isinstance(cols, int) or not isinstance(rows, int):
            raise TmuxError("resize requires integer columns and rows")
        if cols < 40 or cols > 300 or rows < 12 or rows > 120:
            raise TmuxError("resize dimensions are outside supported bounds")
        self._checked_cmd(
            self._server,
            "resize-window",
            "-t",
            session_name,
            "-x",
            str(cols),
            "-y",
            str(rows),
        )

    def send_keys(self, pane_id: str, text: str) -> None:
        self.send_literal(pane_id, text)
        self.send_enter(pane_id)

    def send_enter(self, pane_id: str) -> None:
        pane = self._pane(pane_id)
        self._invoke("Enter key", pane.enter)

    def send_menu_choice(self, pane_id: str, choice: str) -> None:
        if not re.fullmatch(r"[1-9]", choice):
            raise TmuxError("menu choice must be one digit from 1 through 9")
        self.send_keys(pane_id, choice)

    def send_special(self, pane_id: str, key: str) -> None:
        if key not in SPECIAL_KEYS:
            raise TmuxError(f"unsupported special key: {key}")
        if key == "Enter":
            self.send_enter(pane_id)
            return
        pane = self._pane(pane_id)
        tmux_key = TMUX_SPECIAL_KEY_NAMES.get(key, key)
        self._invoke("special key", pane.send_keys, tmux_key, enter=False, literal=False)

    def send_tokens(self, pane_id: str, tokens: Sequence[Dict[str, str]]) -> None:
        self._validate_pane(pane_id)
        if not tokens:
            raise TmuxError("at least one input token is required")
        for token in tokens:
            if not isinstance(token, dict) or set(token) != {"kind", "value"}:
                raise TmuxError("invalid terminal input token")
            if token["kind"] == "text":
                self.send_literal(pane_id, token["value"])
            elif token["kind"] == "ctrl":
                self.send_special(pane_id, token["value"])
            else:
                raise TmuxError("invalid terminal input token kind")

    def send_workspace_command(self, pane_id: str, command: str, path: str = "") -> None:
        self.send_keys(pane_id, workspace_command_text(command, path))

    def capture(self, pane_id: str) -> str:
        pane = self._pane(pane_id)
        result = self._checked_cmd(pane, "capture-pane", "-p", "-J", "-S", "-")
        return self._output_text(result.stdout)

    def capture_visible(self, pane_id: str) -> str:
        pane = self._pane(pane_id)
        # Keep the viewport's physical rows intact. Joining wrapped rows makes
        # the row and column numbers a full-screen program draws for itself
        # disagree with the captured image; -N also keeps trailing spaces that
        # are part of the screen.
        result = self._checked_cmd(pane, "capture-pane", "-p", "-N")
        return self._output_text(result.stdout)

    def capture_visible_ansi(self, pane_id: str) -> str:
        """Capture the visible viewport with escape sequences preserved.

        The ``-e`` flag re-serializes every cell's attributes (SGR colors,
        bold, underline, ...) back into ANSI, which is what a color-capable
        frontend needs to reproduce the frame. Plain text is derived by
        stripping the escapes at the storage boundary, so the model keeps
        seeing the same plain content as before.
        """
        pane = self._pane(pane_id)
        result = self._checked_cmd(pane, "capture-pane", "-p", "-N", "-e")
        return self._output_text(result.stdout)

    def pane_info(self, pane_id: str) -> Dict[str, str]:
        pane = self._pane(pane_id)
        result = self._invoke(
            "pane metadata",
            pane.display_message,
            (
                "#{pane_dead}\t#{pane_current_command}\t#{pane_current_path}"
                "\t#{cursor_x}\t#{cursor_y}\t#{pane_width}\t#{pane_height}"
            ),
            get_text=True,
        )
        parts = self._output_text(result).split("\t", 6)
        if len(parts) != 7:
            raise TmuxError("tmux returned invalid pane metadata")
        return {
            "dead": parts[0],
            "command": parts[1],
            "path": parts[2],
            "cursor_x": parts[3],
            "cursor_y": parts[4],
            "pane_width": parts[5],
            "pane_height": parts[6],
        }

    def pane_alive(self, pane_id: str) -> bool:
        try:
            pane = self._pane(pane_id)
            result = self._invoke("pane status", pane.display_message, "#{pane_dead}", get_text=True)
            return self._output_text(result).strip() == "0"
        except TmuxError:
            return False

    def kill_session(self, session_name: str) -> bool:
        self._validate_session(session_name)
        if not self.session_exists(session_name):
            return False
        session = self._invoke("session lookup", self._server.sessions.get, session_name=session_name)
        self._invoke("kill-session", session.kill)
        self._panes.clear()
        return True

    @staticmethod
    def attach_command(session_name: str) -> str:
        TmuxTransport._validate_session(session_name)
        return f"tmux attach-session -t {shlex.quote(session_name)}"
