"""The Agent-facing Vim input safety policy."""
from __future__ import annotations

import re
from typing import Dict, Sequence

from workspace_capture import RunnerError


VIM_INSERT_MARKERS = ("-- INSERT --", "-- REPLACE --")
VIM_INSERT_COMMANDS = {"i", "a", "o", "I", "A", "O"}
VIM_SAFE_NORMAL_COMMANDS = {"dd", "dG", "ggdG", "u", "x", "0", "$", "gg", "G"}
VIM_SAFE_NORMAL_COMPOSITES = re.compile(r"^(?:[0-9]{1,4}G|0dd|[0-9]{1,4}G0dd|[0-9]{1,4}G0D|D)$")
VIM_SAFE_EX_COMMAND = re.compile(r"^:(?:w|q|wq|x|qa|wqa)!?$")
VIM_SAFE_SUBSTITUTE = re.compile(
    r"^:(?:[0-9]{1,6}(?:,[0-9]{1,6})?|%)?s/"
    r"[A-Za-z0-9_ ,:;(){}\]'\"+=<>!-]{1,80}/"
    r"[A-Za-z0-9_ ,:;(){}\]'\"+=<>!-]{0,80}/$"
)
VIM_SAFE_SEARCH = re.compile(r"^/[A-Za-z0-9_ .,;:(){}\[\]_'\"+=<>!@#%&*?/-]{1,120}$")


def validate_agent_vim_keys(tokens: Sequence[Dict[str, str]], screen: str) -> None:
    """Reject text that Vim would interpret as unintended normal-mode commands."""
    mode = "insert" if any(marker in screen for marker in VIM_INSERT_MARKERS) else "normal"
    if mode == "insert" and tokens:
        first = tokens[0]
        if first.get("kind") == "literal" and first.get("value") in VIM_INSERT_COMMANDS:
            raise RunnerError(
                "Vim is already in insert mode; send document text without another i/a/o command"
            )
    ex_command = ""
    for token in tokens:
        kind = token.get("kind")
        value = token.get("value")
        if kind == "key":
            if value in {"Escape", "C-c"}:
                # C-c leaves insert/command/search mode just like Escape does.
                mode = "normal"
                ex_command = ""
            elif value == "Enter":
                if mode in {"command", "search"}:
                    allowed = (
                        VIM_SAFE_SEARCH.fullmatch(ex_command)
                        if mode == "search"
                        else (
                            VIM_SAFE_EX_COMMAND.fullmatch(ex_command)
                            or VIM_SAFE_SUBSTITUTE.fullmatch(ex_command)
                        )
                    )
                    if not allowed:
                        raise RunnerError("Vim Ex/search command is not in the safe allowlist")
                    mode = "normal"
                    ex_command = ""
            elif mode in {"command", "search"}:
                # History recall or completion would change the pending command
                # in ways this validator cannot track.
                raise RunnerError("only Enter or Escape may follow a Vim Ex/search command")
            continue
        if kind != "literal" or not isinstance(value, str):
            raise RunnerError("invalid Vim input token")
        if mode == "insert":
            continue
        if mode == "command":
            ex_command += value
            if len(ex_command) > 192 or not ex_command.startswith(":"):
                raise RunnerError("Vim Ex command is not in the safe allowlist")
            continue
        if value in VIM_INSERT_COMMANDS:
            mode = "insert"
            continue
        if value in VIM_SAFE_NORMAL_COMMANDS or VIM_SAFE_NORMAL_COMPOSITES.fullmatch(value):
            continue
        if VIM_SAFE_SEARCH.fullmatch(value):
            mode = "search"
            ex_command = value
            continue
        if value == ":":
            mode = "command"
            ex_command = value
            continue
        if VIM_SAFE_EX_COMMAND.fullmatch(value) or VIM_SAFE_SUBSTITUTE.fullmatch(value):
            mode = "command"
            ex_command = value
            continue
        raise RunnerError(
            "unsafe Vim normal-mode literal; enter insert mode with a separate i/a/o token first"
        )
    if mode in {"command", "search"}:
        raise RunnerError("Vim Ex/search command must end with Enter")

