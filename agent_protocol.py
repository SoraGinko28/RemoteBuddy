from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, runtime_checkable


SPECIAL_KEYS = {
    "Enter",
    "Escape",
    "Tab",
    "Up",
    "Down",
    "Left",
    "Right",
    "Backspace",
    "Delete",
    "C-c",
}
# ``$rest`` pauses the typing plan without sending any keys, like a musical
# rest; the bounds keep a model from stalling the shared pane indefinitely.
REST_MIN_MS = 20
REST_MAX_MS = 2000
# The vocabulary one action may use. Kept as an ordered tuple as well: a
# rejection has to name the choices, and that text goes back to the model.
ACTION_TYPE_ORDER = ("chat", "command", "diff", "observe", "done")
ACTION_TYPES = set(ACTION_TYPE_ORDER)
REFERENCE_KINDS = {"chat", "screen"}
# Mirrors meeting.MAX_CHAT_REFERENCES without importing the storage layer.
MAX_ACTION_REFERENCES = 32
# Structured commands keep workspace path resolution and safe quoting.
STRUCTURED_COMMANDS = {"pwd", "cd", "python"}
MAX_SHELL_COMMAND_CHARS = 2000
# 一份 diff 能有多长：多文件多行的改动也要装得下，但不能让一条动作把一轮撑爆。
MAX_DIFF_CHARS = 256 * 1024
# The relaxed shell policy allows general bash and restricts only the
# deletion/destruction family. Tokens are matched against every whitespace-
# or metacharacter-separated word, so chained commands, pipelines, xargs,
# and absolute paths such as /bin/rm are all covered.
FORBIDDEN_SHELL_BASENAMES = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "srm",
    "wipe",
    "truncate",
    "dd",
}
FORBIDDEN_SHELL_FLAGS = {
    "-delete",
    "--delete",
    "--delete-before",
    "--delete-during",
    "--delete-after",
    "--delete-excluded",
    "--remove-source-files",
}
FORBIDDEN_GIT_SUBCOMMANDS = {"clean", "rm"}
# The Agent must stay inside the shared pane's visible foreground. These
# commands would detach work from the shared interface (background daemons,
# new terminal multiplexer sessions, GUI applications, or replacing the
# pane's shell), so they are rejected together with the deletion class.
FORBIDDEN_INTERFACE_BASENAMES = {
    "tmux",
    "screen",
    "nohup",
    "setsid",
    "disown",
    "open",
    "osascript",
    "exec",
}
# Nested shell interpreters are rejected when they would execute content the
# policy cannot see: reading a pipeline/stdin (bare, no script argument) or an
# inline -c command string. Running an existing script file stays allowed.
SHELL_INTERPRETER_BASENAMES = {"sh", "bash", "zsh", "dash", "ksh"}
# 会一直等着人敲键盘的程序。harness 跑一条命令是无人值守的：这类命令只会把共享终端占住，
# 占用者的活永远干不完。要改文件就交 diff，要看东西就用自己那只读的那套。
INTERACTIVE_PROGRAM_BASENAMES = {
    "vim",
    "vi",
    "nvim",
    "nano",
    "pico",
    "emacs",
    "less",
    "more",
    "most",
    "man",
    "top",
    "htop",
    "watch",
    "screen",
    "tmux",
}
# 交互式解释器：不带参数起就是等人输入，带了脚本或 -c 才算一条会结束的命令。
REPL_BASENAMES = {
    "python",
    "python2",
    "python3",
    "ipython",
    "ipython3",
    "node",
    "nodejs",
    "deno",
    "bun",
    "irb",
    "ruby",
    "perl",
    "php",
    "lua",
    "julia",
    "Rscript",
    "psql",
    "mysql",
    "sqlite3",
    "ghci",
    "iex",
}
# git options before the subcommand that consume the following word.
_GIT_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# Punctuation tokens produced by the shlex lexer that end a command segment.
_SEGMENT_BREAKS = {";", "|", "||", "&&", "(", ")", ";;"}
# Redirections that truncate the target file.
_TRUNCATING_REDIRECTS = {">", ">|"}
# Commands that produce no meaningful output, so redirecting them into a file
# is equivalent to truncating it.
_EMPTY_OUTPUT_COMMANDS = {":", "true", "false"}
# Wrappers that forward their trailing argument as a command to execute, so
# the command-position scan continues through them.
_COMMAND_WRAPPERS = {
    "xargs",
    "sudo",
    "doas",
    "env",
    "nice",
    "ionice",
    "stdbuf",
    "timeout",
    "watch",
    "time",
    "command",
    "builtin",
    "parallel",
}
# find(1) flags whose following word is executed as a command.
_FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DURATION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?[smhd]?")


class AgentBackendError(RuntimeError):
    pass


class AgentInterrupted(AgentBackendError):
    """The active model turn was deliberately interrupted by the harness."""


class AgentProtocolError(AgentBackendError):
    pass


class AgentActionError(AgentProtocolError):
    """A model-authored action failed decoding or validation.

    Unlike a hard protocol violation, this is correctable: the runner may
    feed the error text back to the model and retry within the same cycle.
    The text the model wrote is kept on the error (``source``), so a rejection
    can be diagnosed from the journal alone.
    """

    def __init__(self, message: str, source: str = "") -> None:
        super().__init__(message)
        self.source = _snippet(source)


MESSAGE_TYPE_ORDER = ("none", "ack", "progress", "question", "final")
# 行动步的正文分两种：一种给"它自己有读工具、并且共享终端拒收只读命令"的 backend
# （Claude Code / ACP），另一种给"它手里没有读工具"的 backend（结构化 app-server、
# Responses）——后者共享终端就是它唯一能看东西的地方，再跟它说"这里不许读"它就只能去问人。
ACT_STEP_BODY_REFUSED = (
    "Action step. You have your own read tools (Claude Code has Read/Glob/Grep, Trae has its own "
    "read tools): read with them, not in the shared terminal -- a plain reader sent as a `command` "
    "is refused. Two moves change things, and neither of them is typing: a "
    "`command` is one bash line the harness runs for you in the shared terminal, and a `diff` is a "
    "unified diff the harness lands with vim, file by file, in that same pane. Send the command "
    "itself: the harness appends the return-code echo for you (and replaces one you wrote yourself, "
    "rather than running both), so never add `echo ... $?` of your own. You get back this command's "
    "own output and its return code; `truncated` true means the output outran the pane's buffer and "
    "what you see is only its tail. You never open an "
    "editor and you never send keys. A plain reader (cat, sed, head, tail, nl, rg, grep, ls, wc, "
    "file, stat, tree) sent as a `command` is refused; a file the human asked to see in the "
    "terminal is the exception. If the human asked you to find something out or to show something, "
    "this step is where they hear it -- your investigation answer was not shown to them, so a short "
    "`chat` saying it is part of the work, not an extra message. "
)
ACT_STEP_BODY_OPEN = (
    "Action step. You have no read tools of your own, so the shared terminal -- the pane the "
    "human watches -- is also where you look: `cat`, `sed`, `head`, `tail`, `nl`, `rg`, `grep`, "
    "`ls`, `wc`, `git diff` all work as `command` lines. Send the command alone: the harness "
    "appends the return-code echo itself (and replaces one you wrote yourself), so never add "
    "`echo ... $?` of your own. You get back this command's own output and its return code; "
    "`truncated` true means the output outran the pane's buffer and what you see is only its tail. Look first (one or two commands, not a "
    "tour of the repository), then change what the request asks for -- a `diff` for file contents, "
    "a `command` for everything else. Never ask the human for a path you can find yourself: the "
    "pane's working directory is the workspace, and `ls`/`git ls-files` show what is in it. If the "
    "human asked you to find something out or to show something, say it in a short `chat` in this "
    "step -- they do not see your reasoning, only your actions and what you say. "
)
# 走一个真的工具（Claude Code 的 MCP、ACP 的 MCP、app-server 的动态工具）。
ACTION_BY_TOOL_TEXT = (
    "Send it by calling the `remotebuddy_action` tool (MCP server `remotebuddy`) exactly once "
    "-- an action written as text is not an action."
)
# 把动作当 JSON 交（结构化 Responses、app-server 的 outputSchema）：这些 backend 手里
# 根本没有工具，跟它们说"去调某个工具"只会让它回一句"没有这个工具"然后什么都不做。
ACTION_BY_JSON_TEXT = (
    "Send it as the single JSON object the response schema asks for: that object is the action, "
    "and prose is not."
)
ACT_STEP_TEXT = ACT_STEP_BODY_REFUSED + ACTION_BY_TOOL_TEXT
ACT_STEP_JSON_TEXT = ACT_STEP_BODY_REFUSED + ACTION_BY_JSON_TEXT
ACT_STEP_OPEN_TEXT = ACT_STEP_BODY_OPEN + ACTION_BY_TOOL_TEXT
ACT_STEP_OPEN_JSON_TEXT = ACT_STEP_BODY_OPEN + ACTION_BY_JSON_TEXT


def act_step_text(action_submission: str = "tool", readers_refused: bool = True) -> str:
    """行动步那段话：按 backend 的两件事选——动作怎么交、共享终端让不让读。"""
    by_json = str(action_submission or "").lower() == "json"
    if readers_refused:
        return ACT_STEP_JSON_TEXT if by_json else ACT_STEP_TEXT
    return ACT_STEP_OPEN_JSON_TEXT if by_json else ACT_STEP_OPEN_TEXT




def _name_list(names: Sequence[str]) -> str:
    """The choices, written the way a person lists them: a, b, or c."""
    items = [str(name) for name in names]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", or " + items[-1]


def _snippet(text: str, limit: int = 600) -> str:
    """One line of what the model wrote, for a journal entry that has to stay small."""
    flat = " ".join(str(text or "").split())
    return flat[:limit]


class AgentRefused(AgentActionError):
    """The model asked for something this harness does not give.

    A web search, a sub-agent, a file change outside the shared pane: the model
    may pick one of these on its own, and that is a mistake it can correct. The
    runner says so in the journal and lets it try again within the same cycle,
    instead of failing the meeting over a tool that was never on offer.
    """


@dataclass(frozen=True)
class AgentAction:
    type: str
    message_type: str
    content: str
    command: str
    diff: str
    path: str
    delay_ms: int
    references: Sequence[Dict[str, Any]] = ()
    # Model-declared: when False the action's output is not needed for the
    # next step ("fire and forget") -- the runner must not wait for a result
    # nor count repeats of it as a stall.
    result_needed: bool = True

    @classmethod
    def from_dict(
        cls,
        payload: Dict[str, Any],
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
    ) -> "AgentAction":
        try:
            return cls._validated_from_dict(payload, allowed_types, allowed_message_types)
        except AgentActionError:
            raise
        except AgentProtocolError as exc:
            # Everything raised while validating a model-authored action is
            # correctable by the model; mark it so callers can retry.
            raise AgentActionError(str(exc)) from exc

    @classmethod
    def _validated_from_dict(
        cls,
        payload: Dict[str, Any],
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
    ) -> "AgentAction":
        optional = {"message_type", "content", "command", "diff", "path", "delay_ms"}
        allowed = {"type", "references", "result_needed"} | optional
        payload = unwrap_action_envelope(payload)
        if not isinstance(payload, dict):
            raise AgentProtocolError("model action must be a JSON object")
        # Name the offending fields: this text goes back to the model as a
        # correction hint, and different model families invent different keys.
        unknown = sorted(set(payload) - allowed)
        if "keys" in unknown:
            raise AgentProtocolError(
                "the keystrokes action is gone: the harness does the typing now. Hand over a "
                "`diff` (a unified diff of the file contents) or a `command` (one bash line) "
                "instead of `keys`."
            )
        if unknown:
            raise AgentProtocolError("model action has unexpected fields: " + ", ".join(unknown))
        # Every action but done carries message_type "none", and a chat without
        # one is read as ack: the model wrote a sentence, and that is the only
        # thing the missing value could have meant -- the same reading as a
        # message_type that names an action type gets a few lines below. done
        # is the exception: it claims the work is finished, so it has to say so
        # itself. 真事：`{"type":"chat","action":{…正文…}}` 缺 message_type，
        # 连着驳回三轮，20260929-200133-eedc8a 就是这么死在 api_error 上的。
        if not str(payload.get("message_type") or "").strip() and payload.get("type") != "done":
            payload["message_type"] = "ack" if payload.get("type") == "chat" else "none"
        missing = sorted(field for field in ("type", "message_type") if not payload.get(field))
        if missing:
            raise AgentProtocolError("model action is missing fields: " + ", ".join(missing))
        # Models routinely omit the fields an action does not use; fill in the
        # schema's "unused" values here. The per-type checks below still reject
        # actions that genuinely need a value they left out.
        payload = {
            "content": "",
            "command": "none",
            "diff": "",
            "path": "",
            "delay_ms": 0,
            **payload,
        }
        # `""` 和 `null` 是"这个字段没用"的另一种写法，不是"我想用这个字段"。结构化输出
        # 常把没用到的字段填成空值；在这里先归一，后面按类型的检查才不会把它当成多余的
        # 字段拒掉 —— 那种拒绝说不清是哪一项，模型只能猜，猜不中就在同一个错上转到整轮
        # 以 api_error 收场（20260926-213214-3dbedc 的三次 `keystrokes has unrelated
        # fields` 就是这么死的）。
        for field, neutral in (("message_type", "none"), ("command", "none"), ("diff", ""),
                               ("content", ""), ("path", ""), ("delay_ms", 0)):
            if payload.get(field) is None:
                payload[field] = neutral
        if payload.get("message_type") == "":
            payload["message_type"] = "none"
        if payload.get("command") == "":
            payload["command"] = "none"
        # 有的模型把整份补丁塞进 `command`（还常用 Codex 的 `*** Begin Patch` 写法），
        # `diff` 留空。位置不对、意图很清楚：搬到 `diff` 里，别让它卡在字段名上。
        if payload.get("type") == "diff" and not str(payload.get("diff") or "").strip():
            pasted = str(payload.get("command") or "")
            if _looks_like_patch(pasted):
                payload["diff"] = pasted
                payload["command"] = "none"
        action_type = payload["type"]
        if action_type not in ACTION_TYPES:
            raise AgentProtocolError(
                "model action type is invalid: expected "
                + _name_list(allowed_types or ACTION_TYPE_ORDER))
        if allowed_types is not None and action_type not in set(allowed_types):
            raise AgentProtocolError(
                "model action is not allowed in the current phase: expected "
                + _name_list(allowed_types))
        # Models say "final" in the two ways the schema separates: `done` with a
        # final message, or `chat` with message_type "final" (some also write
        # "done" there, swapping the two). That means one thing and nothing
        # else, so it is read as `done` instead of being rejected and retried
        # until the cycle gives up.
        if action_type == "chat" and payload.get("message_type") in {"final", "done"} \
                and str(payload.get("content") or "").strip():
            action_type = "done"
            payload = {**payload, "type": "done", "message_type": "final"}
        message_type = payload["message_type"]
        if message_type not in MESSAGE_TYPE_ORDER and message_type in ACTION_TYPES:
            # 有的模型把动作类型填进 message_type（`"message_type": "chat"`）——它的意思是
            # "这是一句话"，不是想说一个非法值。chat 当成 ack（"我听到了"），别的当成 none，
            # 别为这个把动作拒了。（放在上面那条 chat+final/done → done 的规则之后，
            # 免得把那条本来就读得懂的写法带偏。）
            message_type = "ack" if action_type == "chat" else "none"
            payload["message_type"] = message_type
        if message_type not in MESSAGE_TYPE_ORDER:
            raise AgentProtocolError(
                "model message type is invalid: expected " + _name_list(MESSAGE_TYPE_ORDER))
        if allowed_message_types is not None and message_type not in set(allowed_message_types):
            raise AgentProtocolError(
                "model message type is not allowed in the current phase: expected "
                + _name_list(allowed_message_types))
        content = payload["content"]
        command = payload["command"]
        diff = payload["diff"]
        path = payload["path"]
        delay_ms = payload["delay_ms"]
        if not all(isinstance(value, str) for value in (content, command, diff, path)):
            raise AgentProtocolError("model action strings are invalid")
        if len(content.encode("utf-8")) > 64 * 1024 or len(path.encode("utf-8")) > 4096:
            raise AgentProtocolError("model action text is too large")
        if len(diff.encode("utf-8")) > MAX_DIFF_CHARS:
            raise AgentProtocolError(f"diff is larger than {MAX_DIFF_CHARS} bytes")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int):
            raise AgentProtocolError("delay_ms must be an integer")
        references = normalize_reference_tokens(payload.get("references", []))
        raw_result_needed = payload.get("result_needed", True)
        if isinstance(raw_result_needed, bool):
            result_needed = raw_result_needed
        elif raw_result_needed in (0, 1):
            result_needed = bool(raw_result_needed)
        elif isinstance(raw_result_needed, str) and raw_result_needed.strip().lower() in {"true", "false"}:
            result_needed = raw_result_needed.strip().lower() == "true"
        else:
            raise AgentProtocolError("result_needed must be a boolean")

        if action_type == "chat":
            if message_type not in {"ack", "progress", "question"} or not content.strip():
                raise AgentProtocolError("chat requires ack, progress, or question content")
            _require_unused(command, diff, path, delay_ms)
        elif action_type == "done":
            if message_type != "final" or not content.strip():
                raise AgentProtocolError("done requires final content")
            _require_unused(command, diff, path, delay_ms)
        elif references:
            raise AgentProtocolError("only chat and done actions may carry references")
        def stray_fields(*candidates: tuple) -> str:
            """多余字段点名：这条文本是模型改错用的提示，说不清它只能瞎猜。"""
            names = [name for name, present in candidates if present]
            return ", ".join(names)

        if action_type == "command":
            if message_type != "none" or content:
                raise AgentProtocolError(
                    "command action is invalid: "
                    + stray_fields(("message_type", message_type != "none"),
                                   ("content", bool(content)))
                    + " is not used by a shell command line; leave it at none/empty"
                )
            if command in STRUCTURED_COMMANDS:
                if command == "pwd" and path:
                    raise AgentProtocolError("pwd does not accept a path")
                if command in {"cd", "python"} and not path:
                    raise AgentProtocolError(f"{command} requires a path")
            else:
                # Relaxed policy: any bash command line is accepted except the
                # deletion-class commands rejected by validate_shell_command.
                if command == "none":
                    raise AgentProtocolError(
                        "command action requires a real bash command line; "
                        "'none' is only the placeholder used by non-command actions"
                    )
                validate_shell_command(command)
                if path:
                    raise AgentProtocolError("a bash command line does not accept a separate path")
            if diff or delay_ms:
                raise AgentProtocolError(
                    "command has unrelated fields: "
                    + stray_fields(("diff", bool(diff)), ("delay_ms", bool(delay_ms)))
                )
        elif action_type == "diff":
            if not diff.strip():
                raise AgentProtocolError(
                    "type is diff but `diff` is empty: the unified diff goes in `diff` "
                    "(`--- a/<path>` / `+++ b/<path>` / `@@ … @@`); `command` holds one bash "
                    "line and never carries a patch"
                )
            stray = stray_fields(("message_type", message_type != "none"),
                                 ("content", bool(content)),
                                 ("command", command != "none"),
                                 ("path", bool(path)),
                                 ("delay_ms", bool(delay_ms)))
            if stray:
                raise AgentProtocolError(
                    "diff has unrelated fields: " + stray
                    + "; a diff action carries only the unified diff"
                )
            if "\x00" in diff or "\r" in diff:
                raise AgentProtocolError("diff must be plain text with unix line endings")
            from tmux_vim import DiffError, parse_unified_diff

            try:
                patches = parse_unified_diff(diff)
            except DiffError as exc:
                raise AgentProtocolError(f"diff action is not a unified diff: {exc}") from exc
            if not patches:
                raise AgentProtocolError("diff action carries no file changes")
        elif action_type == "observe":
            if message_type != "none" or content or command != "none" or diff or path:
                raise AgentProtocolError(
                    "observe has unrelated fields: "
                    + stray_fields(("message_type", message_type != "none"),
                                   ("content", bool(content)),
                                   ("command", command != "none"),
                                   ("diff", bool(diff)),
                                   ("path", bool(path)))
                )
            if delay_ms < 100 or delay_ms > 2000:
                raise AgentProtocolError("observe delay must be between 100 and 2000 ms")
        return cls(
            action_type,
            message_type,
            content,
            command,
            diff,
            path,
            delay_ms,
            tuple(references),
            result_needed,
        )


def validate_shell_command(line: str) -> None:
    """Allow general bash inside the shared pane, with two restricted classes.

    Deletion-class commands are rejected, and so is anything that would leave
    the shared interface (background jobs, detached sessions, GUI apps). The
    line is tokenized with a POSIX shlex lexer so quoting behaves like bash:
    quoted arguments are never mistaken for commands, while quote tricks such
    as ``\\rm`` or ``r""m`` resolve to the command bash would actually run.
    Command names are matched in command position — the start of every chained
    segment, pipeline stage, and substitution, plus arguments forwarded by
    wrappers such as ``xargs``/``sudo``/``timeout`` and ``find -exec``.

    This is a guardrail against accidental destruction, not a sandbox: content
    executed by interpreters the policy cannot parse (``python -c``, ``awk``
    programs, script files) and values built from ``$VAR`` expansion are out of
    scope. Nested shells reading opaque command strings (``bash -c``, piping
    into ``sh``), ``eval``, and command names containing ``$`` are rejected for
    the same reason.
    """
    if not isinstance(line, str) or not line.strip():
        raise AgentProtocolError("bash command must be a non-empty string")
    if len(line) > MAX_SHELL_COMMAND_CHARS:
        raise AgentProtocolError(f"bash command exceeds {MAX_SHELL_COMMAND_CHARS} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in line):
        raise AgentProtocolError("bash command must not contain control characters")
    for body in _substitution_bodies(line):
        if body.strip():
            validate_shell_command(body)
    words = _shell_tokens(line)
    segment: list = []
    pending_command = True
    after_redirect = False
    find_segment = False
    for index, word in enumerate(words):
        if word == "&":
            raise AgentProtocolError(
                "background jobs are not allowed; work must stay in the shared pane's foreground"
            )
        if word in _SEGMENT_BREAKS:
            segment = []
            pending_command = True
            after_redirect = False
            find_segment = False
            continue
        if _is_shell_punctuation(word):
            if word in _TRUNCATING_REDIRECTS and _segment_truncates(segment):
                raise AgentProtocolError(
                    "redirection would truncate the target file without producing content"
                )
            after_redirect = True
            continue
        if word.lower() in FORBIDDEN_SHELL_FLAGS:
            raise AgentProtocolError(f"deletion flag is not allowed: {word}")
        if after_redirect:
            # The word right after a redirection is a filename, never a command.
            after_redirect = False
            segment.append(word)
            continue
        if find_segment and word.lower() in _FIND_EXEC_FLAGS:
            pending_command = True
            segment.append(word)
            continue
        if pending_command:
            if _ASSIGNMENT_RE.match(word) or word.startswith("-") or _DURATION_RE.fullmatch(word):
                # Leading VAR=val assignments, wrapper flags, and timeout-style
                # durations keep the command slot open for the next word.
                segment.append(word)
                continue
            _validate_command_word(word, words, index)
            base = _basename(word)
            if base == "find":
                find_segment = True
            if base not in _COMMAND_WRAPPERS:
                pending_command = False
        segment.append(word)


def _basename(word: str) -> str:
    return word.rstrip("/").rsplit("/", 1)[-1].lower()


def _validate_command_word(word: str, words: Sequence[str], index: int) -> None:
    """Reject a command-position word that belongs to a restricted class."""
    if "$" in word:
        raise AgentProtocolError(
            "a command name built from shell expansion cannot be validated"
        )
    base = _basename(word)
    if base in FORBIDDEN_SHELL_BASENAMES or base.startswith("mkfs"):
        raise AgentProtocolError(f"deletion-class command is not allowed: {base}")
    if base in FORBIDDEN_INTERFACE_BASENAMES:
        raise AgentProtocolError(f"command would leave the shared terminal interface: {base}")
    if base == "eval":
        raise AgentProtocolError("eval would execute content outside the policy's view")
    if base in INTERACTIVE_PROGRAM_BASENAMES:
        raise AgentProtocolError(
            f"{base} is an interactive program, and the harness runs commands unattended: it "
            "would sit there waiting for a keyboard and the terminal would never come back. "
            "To change a file, hand over a `diff`; to read one, use your own read-only tools "
            "(or a reader command, if you have none)."
        )
    if base in REPL_BASENAMES:
        followers = [
            follower
            for follower in words[index + 1 :]
            if follower != "&" and follower not in _SEGMENT_BREAKS and not _is_shell_punctuation(follower)
        ]
        if not followers:
            raise AgentProtocolError(
                f"{base} with no arguments is an interactive session: the harness runs commands "
                "unattended, so it would wait for input forever. Give it a script or a `-c` one-liner."
            )
    if base in SHELL_INTERPRETER_BASENAMES:
        followers = []
        for follower in words[index + 1 :]:
            if follower == "&" or follower in _SEGMENT_BREAKS or _is_shell_punctuation(follower):
                break
            followers.append(follower)
        bare = not any(not follower.startswith("-") for follower in followers)
        if bare or "-c" in followers:
            raise AgentProtocolError(
                f"nested shell would execute content outside the policy's view: {base}"
            )
    if base == "git":
        expect_value = False
        for follower in words[index + 1 :]:
            if follower == "&" or follower in _SEGMENT_BREAKS or _is_shell_punctuation(follower):
                break
            if expect_value:
                expect_value = False
                continue
            if follower in _GIT_VALUE_OPTIONS:
                expect_value = True
                continue
            if follower.startswith("-"):
                continue
            if follower.lower() in FORBIDDEN_GIT_SUBCOMMANDS:
                raise AgentProtocolError(
                    f"deletion-class git subcommand is not allowed: {follower}"
                )
            break


def _is_shell_punctuation(word: str) -> bool:
    return bool(word) and all(character in "<>|&();" for character in word)


def _segment_truncates(segment: Sequence[str]) -> bool:
    words = [word for word in segment if not _ASSIGNMENT_RE.match(word)]
    if not words:
        return True
    head = _basename(words[0])
    if head in _EMPTY_OUTPUT_COMMANDS:
        return True
    return head == "echo" and len(words) == 1


def _shell_tokens(line: str) -> list:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return [token for token in lexer if token]
    except ValueError as exc:
        raise AgentProtocolError(f"bash command could not be parsed: {exc}") from exc


def _substitution_bodies(line: str) -> list:
    """Extract `...` and $(...) command-substitution bodies outside single quotes."""
    bodies = []
    quote = ""
    index = 0
    length = len(line)
    while index < length:
        character = line[index]
        if quote == "'":
            if character == "'":
                quote = ""
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if quote == '"':
            if character == '"':
                quote = ""
                index += 1
                continue
        elif character in "'\"":
            quote = character
            index += 1
            continue
        if character == "`":
            end = line.find("`", index + 1)
            if end == -1:
                raise AgentProtocolError("bash command has an unterminated command substitution")
            bodies.append(line[index + 1 : end])
            index = end + 1
            continue
        if line.startswith("$(", index) and not line.startswith("$((", index):
            depth = 1
            cursor = index + 2
            while cursor < length and depth:
                if line[cursor] == "(":
                    depth += 1
                elif line[cursor] == ")":
                    depth -= 1
                cursor += 1
            if depth:
                raise AgentProtocolError("bash command has an unterminated command substitution")
            bodies.append(line[index + 2 : cursor - 1])
            index = cursor
            continue
        index += 1
    return bodies


def _require_unused(command: str, diff: str, path: str, delay_ms: int) -> None:
    if command != "none" or diff or path or delay_ms:
        raise AgentProtocolError("chat action has unrelated fields")


def normalize_reference_tokens(raw: Any) -> list:
    """Validate the wire format of content references on chat/done actions.

    Each reference points at one earlier journal event, mirroring the human
    reference schema: ``kind`` is ``chat`` or ``screen``, the source event is
    selected by its ``seq`` (or ``event_id``), and ``region`` is a half-open
    character range into that event's content — or ``null`` to quote the whole
    content. Resolution against the actual journal happens in the runner.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise AgentProtocolError("references must be an array")
    if len(raw) > MAX_ACTION_REFERENCES:
        raise AgentProtocolError(f"references may contain at most {MAX_ACTION_REFERENCES} items")
    normalized = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"kind", "seq", "event_id", "region"}:
            raise AgentProtocolError("reference has unexpected fields")
        kind = item.get("kind")
        if kind not in REFERENCE_KINDS:
            raise AgentProtocolError("reference kind must be chat or screen")
        seq = item.get("seq")
        event_id = item.get("event_id")
        if seq is None and event_id is None:
            raise AgentProtocolError("reference requires the seq or event_id of an earlier event")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
            raise AgentProtocolError("reference seq must be a positive integer")
        if event_id is not None and (
            not isinstance(event_id, str) or not event_id or len(event_id) > 200
        ):
            raise AgentProtocolError("reference event_id must be a short string")
        region = item.get("region")
        if region is not None:
            if not isinstance(region, dict) or set(region) != {"start", "end"}:
                raise AgentProtocolError("reference region must contain start and end")
            start, end = region.get("start"), region.get("end")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
                raise AgentProtocolError("reference region bounds must be integers")
            if start < 0 or end <= start or end > 1_000_000:
                raise AgentProtocolError("reference region must be a bounded half-open range")
        entry: Dict[str, Any] = {"kind": kind, "region": dict(region) if region else None}
        if seq is not None:
            entry["seq"] = seq
        if event_id is not None:
            entry["event_id"] = event_id
        normalized.append(entry)
    return normalized


def validate_key_tokens(tokens: Sequence[Any]) -> None:
    if not tokens or len(tokens) > 128:
        raise AgentProtocolError("keystrokes must contain 1 to 128 tokens")
    total = 0
    for token in tokens:
        if not isinstance(token, dict) or set(token) != {"kind", "value"}:
            raise AgentProtocolError("invalid key token")
        kind, value = token["kind"], token["value"]
        if kind == "text":
            if not isinstance(value, str) or not value or any(
                ord(character) < 32 or ord(character) == 127 for character in value
            ):
                raise AgentProtocolError(
                    "a text keystroke must be one non-empty line without control characters"
                )
            total += len(value.encode("utf-8"))
        elif kind == "ctrl":
            if not isinstance(value, str) or value not in SPECIAL_KEYS:
                raise AgentProtocolError(f"special key is not allowed: {value}")
        elif kind == "rest":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AgentProtocolError("rest token value must be a millisecond integer")
            if value < REST_MIN_MS or value > REST_MAX_MS:
                raise AgentProtocolError(
                    f"rest must be between {REST_MIN_MS} and {REST_MAX_MS} ms"
                )
        else:
            raise AgentProtocolError("key token kind is invalid")
    if total > 8192:
        raise AgentProtocolError("text keystroke input exceeds 8192 bytes")


def normalize_model_key_tokens(tokens: Sequence[Any]) -> list:
    """Normalize key tokens and the ``$text`` / ``$ctrl`` / ``$rest`` keystroke DSL.

    The DSL has three forms:

    - ``"$text hello"``       -> send the literal text ``hello``
    - ``"$ctrl Left"``        -> send the control/named key ``Left``
    - ``"$rest 200"``         -> pause 200 ms without sending any keys

    Printable single-character keys are also accepted as ordinary key tokens.
    """
    normalized = []
    for token in tokens:
        if isinstance(token, str):
            if token.startswith("$text "):
                text = token[len("$text "):]
                if text:
                    normalized.append({"kind": "text", "value": text})
                continue
            if token.startswith("$ctrl "):
                key = token[len("$ctrl "):]
                normalized.append({"kind": "ctrl", "value": key})
                continue
            if token.startswith("$rest "):
                rest_text = token[len("$rest "):]
                if not rest_text.isdigit():
                    raise AgentProtocolError(
                        "$rest requires a millisecond integer, e.g. $rest 200"
                    )
                normalized.append({"kind": "rest", "value": int(rest_text)})
                continue
            # A bare string is still convenient for plain text.
            if token:
                normalized.append({"kind": "text", "value": token})
            continue
        if not isinstance(token, dict):
            normalized.append(token)
            continue
        candidate = dict(token)
        # ADT 里这两个 kind 叫 text / ctrl；模型在历史里看到自己以前发的
        # `literal` / `key` 会照抄，所以旧名照样收，只是统一成 ADT 的名字。
        legacy = {"literal": "text", "key": "ctrl"}
        if candidate.get("kind") in legacy:
            candidate["kind"] = legacy[candidate["kind"]]
        value = candidate.get("value")
        if (
            candidate.get("kind") == "ctrl"
            and isinstance(value, str)
            and value not in SPECIAL_KEYS
            and len(value) == 1
            and value.isprintable()
            and value not in {"\n", "\r", "\x00"}
        ):
            candidate["kind"] = "text"
        if candidate.get("kind") == "text" and isinstance(candidate.get("value"), str):
            normalized.extend(_expand_escaped_newlines(candidate))
        else:
            normalized.append(candidate)
    return normalized


def _expand_escaped_newlines(token: Dict[str, Any]) -> list:
    """Expand intended line breaks while preserving escapes inside quotes."""
    value = token["value"]
    expanded = []
    buffer = []
    quote = ""
    triple = False
    index = 0

    def line_break() -> None:
        if buffer:
            expanded.append({"kind": "text", "value": "".join(buffer)})
            buffer.clear()
        expanded.append({"kind": "ctrl", "value": "Enter"})

    while index < len(value):
        if value.startswith("\r\n", index):
            line_break()
            index += 2
            continue
        if value[index] in "\r\n":
            line_break()
            index += 1
            continue

        if quote:
            marker = quote * 3 if triple else quote
            if value.startswith(marker, index):
                buffer.append(marker)
                index += len(marker)
                quote = ""
                triple = False
                continue
            if value[index] == "\\" and index + 1 < len(value):
                buffer.append(value[index : index + 2])
                index += 2
                continue
            buffer.append(value[index])
            index += 1
            continue

        if value.startswith("\\n", index):
            line_break()
            index += 2
            continue
        if value[index] in {"'", '"', "`"}:
            quote = value[index]
            triple = quote != "`" and value.startswith(quote * 3, index)
            marker = quote * 3 if triple else quote
            buffer.append(marker)
            index += len(marker)
            continue
        buffer.append(value[index])
        index += 1

    if buffer:
        expanded.append({"kind": "text", "value": "".join(buffer)})
    return expanded or [token]


@dataclass(frozen=True)
class AgentResponse:
    response_id: str
    action: AgentAction
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    executed: bool = False


def decode_action_text(
    text: str,
    allowed_types: Optional[Sequence[str]] = None,
    allowed_message_types: Optional[Sequence[str]] = None,
) -> AgentAction:
    """Decode exactly one schema action, allowing identical gateway duplication."""
    try:
        return _decode_action_text(text, allowed_types, allowed_message_types)
    except AgentActionError as exc:
        if not exc.source:
            exc.source = _snippet(text)
        raise
    except AgentProtocolError as exc:
        raise AgentActionError(str(exc), source=text) from exc


_PATCH_HINTS = (
    "*** Begin Patch",
    "*** Update File:",
    "*** Add File:",
    "*** Delete File:",
    "diff --git ",
    "--- a/",
    "--- /dev/null",
)


def _looks_like_patch(text: str) -> bool:
    """这段文本像不像一份补丁：模型把补丁塞错字段时，靠它把位置纠回来。"""
    head = str(text or "").lstrip()
    return any(head.startswith(hint) for hint in _PATCH_HINTS)


def unwrap_action_envelope(payload: Any) -> Any:
    """Flatten the ``action`` wrapper some models put around their action.

    A gateway may hand the action over inside a one-key envelope, and a model
    copying that layout from its own tool schema can either wrap it twice or
    split its fields between the envelope and the wrapped object. No valid
    action uses ``action`` as a key, so merging is unambiguous.
    """
    for _ in range(3):
        if not isinstance(payload, dict):
            return payload
        inner = payload.get("action")
        if not isinstance(inner, dict):
            return payload
        outer = {key: value for key, value in payload.items() if key != "action"}
        merged = {**inner, **outer}
        # 外层那个 type 常常只是占位（`none`/`chat`），真正的动作在里层：外层不合法、里层
        # 合法时听里层的。真事：`{"type":"none","action":{"type":"diff",…}}` 连着来三次，
        # 一场会就被判成 api_error。
        inner_type = inner.get("type")
        if merged.get("type") not in ACTION_TYPES and inner_type in ACTION_TYPES:
            merged["type"] = inner_type
        # 里层连类型都没写时，外层那个 `none`/`chat` 只是在说"这里有一个动作"；是哪一种看
        # 里层填了哪些字段。真事：`{"type":"chat","action":{"command":"sed -n …"}}` 被当成
        # 缺 message_type 的 chat 驳回，一路重试到 api_error，20260929-200133-eedc8a 就这
        # 么死的。
        elif inner_type not in ACTION_TYPES and str(merged.get("type") or "") in {"", "none", "chat"}:
            inner_command = str(inner.get("command") or "")
            if _looks_like_patch(inner_command):
                merged["type"] = "diff"
            elif inner_command.strip() not in {"", "none"}:
                merged["type"] = "command"
            elif str(inner.get("diff") or "").strip():
                merged["type"] = "diff"
        payload = merged
    return payload


def _decode_action_text(
    text: str,
    allowed_types: Optional[Sequence[str]] = None,
    allowed_message_types: Optional[Sequence[str]] = None,
) -> AgentAction:
    remaining = text.strip()
    if not remaining:
        raise AgentProtocolError("model output is not valid action JSON")
    decoder = json.JSONDecoder()
    values = []
    try:
        while remaining:
            value, end = decoder.raw_decode(remaining)
            values.append(value)
            if len(values) > 4:
                raise AgentActionError("model output repeated the action too many times")
            remaining = remaining[end:].lstrip()
    except json.JSONDecodeError as exc:
        raise AgentActionError(
            "model output is not valid action JSON (" + str(exc).splitlines()[0] + "): "
            "整套动作要是一个 JSON 对象，字符串里的换行写成 \\n，补丁放在 `diff` 里"
        ) from exc
    if any(value != values[0] for value in values[1:]):
        raise AgentActionError("model output contains multiple different actions")
    payload = unwrap_action_envelope(values[0])
    return AgentAction.from_dict(payload, allowed_types, allowed_message_types)


BackendEventCallback = Callable[[Dict[str, Any]], None]
DynamicActionHandler = Callable[[AgentAction, Dict[str, Any]], Dict[str, Any]]


@runtime_checkable
class AgentBackend(Protocol):
    backend_name: str
    stateful: bool
    config: Any

    def start_cycle(
        self,
        meeting_id: str,
        instructions: str,
        initial_context: str,
        *,
        model: Optional[str] = None,
        event_callback: Optional[BackendEventCallback] = None,
    ) -> str:
        ...

    def next_action(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Optional[DynamicActionHandler] = None,
    ) -> AgentResponse:
        ...

    def investigate(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Optional[DynamicActionHandler] = None,
    ) -> str:
        """调查步：这一次请求不碰共享终端，返回模型用文字说的结论。

        有自己读工具的 backend 实现它；没有的（纯对话、结构化 app-server）返回空串——
        它们的终端就是唯一那条读路，两步拆不开。
        """
        ...

    def interrupt(self) -> None:
        ...

    def close(self) -> None:
        ...


def action_json_schema(
    allowed_types: Optional[Sequence[str]] = None,
    allowed_message_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    action_types = list(allowed_types or ACTION_TYPE_ORDER)
    message_types = set(
        allowed_message_types or ("none", "ack", "progress", "question", "final")
    )
    if set(action_types) - ACTION_TYPES:
        raise ValueError("unsupported action schema type")

    reference_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "seq", "region"],
        "properties": {
            "kind": {"type": "string", "enum": ["chat", "screen"]},
            "seq": {
                "type": "integer",
                "minimum": 1,
                "description": "seq of the earlier chat or screen event being quoted.",
            },
            "region": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {"type": "integer", "minimum": 0},
                            "end": {"type": "integer", "minimum": 1},
                        },
                    },
                ],
                "description": (
                    "Half-open character range inside the source event content, "
                    "or null to quote the whole content."
                ),
            },
        },
    }

    def branch(
        action_type: str,
        action_messages: Sequence[str],
        commands: Sequence[str],
        *,
        content: bool = False,
        path: bool = False,
        diff: bool = False,
        delay: Optional[Dict[str, Any]] = None,
        references: bool = False,
        command_schema: Optional[Dict[str, Any]] = None,
        message_description: str = "",
    ) -> Dict[str, Any]:
        required = ["type", "message_type", "content", "command", "diff", "path", "delay_ms",
                    "references"]
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {
                "type": {"type": "string", "enum": [action_type]},
                "message_type": {
                    "type": "string",
                    "enum": list(action_messages),
                    **({"description": message_description} if message_description else {}),
                },
                "content": {"type": "string", **({"minLength": 1} if content else {"enum": [""]})},
                "command": command_schema or {"type": "string", "enum": list(commands)},
                "path": {
                    "type": "string",
                    "description": "A relative path inside the shared workspace." if path else "Unused.",
                    **({"minLength": 1} if path else {"enum": [""]}),
                },
                "diff": {
                    "type": "string",
                    "description": (
                        "A unified diff to land in the workspace. It is checked first (it must "
                        "apply to the files as they are right now), then the harness opens each "
                        "file in vim and makes the change for you, one step at a time, saving "
                        "after each file; afterwards it compares the files with the change you "
                        "asked for and redoes only what still differs. Write `--- a/<path>` and "
                        "`+++ b/<path>` headers with `@@ -start,count +start,count @@` hunks, "
                        "paths relative to the workspace root, one or more files. Never hand "
                        "over a diff you have not seen applied: for your own reading, use your "
                        "read-only tools (or `git diff` as a command)."
                        if diff
                        else "Unused; must stay empty."
                    ),
                    **({"minLength": 1} if diff else {"enum": [""]}),
                },
                "delay_ms": delay or {"type": "integer", "enum": [0]},
                "references": {
                    "type": "array",
                    "items": reference_item,
                    **(
                        {"maxItems": MAX_ACTION_REFERENCES}
                        if references
                        else {"maxItems": 0}
                    ),
                    "description": (
                        "Optional quotes of earlier journal events, identical to human chat references. "
                        "Usually empty."
                        if references
                        else "Unused; must stay empty."
                    ),
                },
            },
        }

    variants = []
    if "chat" in action_types:
        messages = [value for value in ("ack", "progress", "question") if value in message_types]
        if messages:
            variants.append(
                branch(
                    "chat",
                    messages,
                    ("none",),
                    content=True,
                    references=True,
                    message_description=(
                        "ack: you are about to do something, or you acknowledge the "
                        "human. progress: only for work that is already done and "
                        "visible on the terminal Screen. question: you are handing "
                        "control back to the human and stopping this turn."
                    ),
                )
            )
    if "done" in action_types and "final" in message_types:
        variants.append(branch("done", ("final",), ("none",), content=True, references=True))
    if "command" in action_types and "none" in message_types:
        variants.append(branch("command", ("none",), ("pwd",)))
        for command in ("cd", "python"):
            variants.append(branch("command", ("none",), (command,), path=True))
        variants.append(
            branch(
                "command",
                ("none",),
                (),
                command_schema={
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_SHELL_COMMAND_CHARS,
                    "pattern": r"^[^\r\n\u0000]+$",
                    "description": (
                        "A full bash command line typed into the SHARED terminal, which the "
                        "human watches: every character is typed and every screenful is "
                        "theirs to sit through. That is what makes it the right place for "
                        "what changes the workspace -- an editor session, a build, a test "
                        "run, git -- and the wrong place for looking at files: for reading, "
                        "use your own read-only tools (Claude Code: Read/Glob/Grep; Codex and "
                        "Trae: their own shell, e.g. `sed -n '1,40p' file`), which do not "
                        "touch the pane. Deletion-class commands (rm, rmdir, unlink, shred, "
                        "truncate, dd, mkfs, find -delete, rsync --delete, git clean/rm) and "
                        "anything leaving the shared interface (background '&' jobs, nohup, "
                        "setsid, disown, tmux, screen, open, osascript, exec) are rejected. "
                        "The harness runs this line for you, waits for it to finish and hands "
                        "back its return code and the whole buffer, so the command must be "
                        "non-interactive and must terminate on its own: a program that waits "
                        "for input at a prompt is a mistake, and it is stopped when the wait "
                        "runs out. Changing file contents is the `diff` action's job, not an "
                        "editor session."
                    ),
                },
            )
        )
    if "diff" in action_types and "none" in message_types:
        variants.append(branch("diff", ("none",), ("none",), diff=True))
    if "observe" in action_types and "none" in message_types:
        variants.append(
            branch(
                "observe",
                ("none",),
                ("none",),
                delay={"type": "integer", "minimum": 100, "maximum": 2000},
            )
        )
    if not variants:
        raise ValueError("action schema has no valid variants")
    # Optional model-declared flag: false means the action's output is not
    # needed for the next step (runner must not wait for a result).
    for variant in variants:
        variant["properties"]["result_needed"] = {
            "type": "boolean",
            "description": "False when this action needs no result to continue.",
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {"action": {"anyOf": variants}},
    }
