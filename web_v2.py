#!/usr/bin/env python3
"""RemoteBuddy v2 local workspace service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from agent_protocol import (
    AgentBackend,
    AgentProtocolError,
    normalize_model_key_tokens,
    validate_key_tokens,
)
from codex_app_server_client import (
    CodexAppServerBackend,
    TraeAgentBackend,
    CodexAppServerError,
    probe_codex_backend,
    probe_trae_backend,
)
from claude_code_client import (
    ClaudeCodeBackend,
    ClaudeCodeError,
    probe_claude_backend,
)
from interactive_runner import (
    InteractiveRunner,
    RunnerError,
    capture_workspace,
)
from meeting_v2 import (
    MAX_CHAT_REFERENCES,
    MAX_MESSAGE_BYTES,
    MEETING_ID_RE,
    SCHEMA_VERSION,
    VALID_REASONING_EFFORTS,
    VALID_SCORE_TAGS,
    MeetingError,
    MeetingStore,
    UnsupportedMeetingError,
    mentioned_agent_names,
    sha256_text,
    utc_now,
)
from references import reference_from_event
from model_api_client import (
    MODEL_RE,
    SUPPORTED_PROVIDERS,
    ModelApiConfig,
    ModelApiError,
    ModelConfigError,
    OpenAIResponsesClient,
    _default_auth_key,
    _select_auth_key,
    _validate_base_url,
)
from tmux_transport import TmuxError, TmuxTransport, libtmux_version
import meeting_loader
import meeting_sdk_server
import meeting_sdk_json
import meeting_sdk_viba
from viba.type import VibaProgramErr as _SdkErr
from ticket_centered import Meeting, Member, Store as TicketStore, load_workflow, workflow_root


PROJECT_ROOT = Path(__file__).resolve().parent
# 一块工作区 = 一个目录。会议的每一份数据都放在它的 `.meeting/<会议号>/` 下面；
# 服务默认就用项目根，`--workspace DIR` 换一个。
WORKSPACE_INDEX = "workspaces"
SKILL_PATH = PROJECT_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
MODEL_API_PATH = PROJECT_ROOT / "model_api"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CSRF_HEADER = "X-RemoteBuddy-CSRF"
MAX_REQUEST_BYTES = MAX_MESSAGE_BYTES + 32 * 1024
TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
# 工单那一侧（ticket_centered）：数据放在这场会的目录下面，每个成员各是一个进程。
TICKET_ROOT = "ticket_centered"
TICKET_MEMBERS = ("actor", "assistant", "critic")
TICKET_TIMEOUT_S = 900.0
# Phases from which the human can resume or wake the Agent. The frontend
# mirrors this set for its resume-button predicate.
RESUMABLE_PHASES = {"waiting_for_user", "completion_proposed", "api_error", "cycle_timeout"}
# Probing a CLI backend (traecli answers with a full protocol handshake) costs
# seconds, and the same answer is reused by the health page and by starting a
# session, so one probe is kept for a while instead of being repeated on every
# page load.
BACKEND_PROBE_CACHE_SECONDS = 60.0
DEFAULT_JUDGE_NAME = "human"
MAX_JUDGE_NAME_BYTES = 200
LOCAL_SETTINGS_PATH = ".remotebuddy/settings.local.json"
SCORE_IGNORED_FIELDS = frozenset(
    {
        "score",
        "value",
        "numeric_score",
        "rating",
        "judge_name",
        "judge",
        "excerpt",
        "source_sha256",
        "source_hash",
    }
)
MAX_SCORE_COMMENT_BYTES = MAX_MESSAGE_BYTES
BACKEND_COMBINATIONS = {
    ("responses", "openai"): "Responses API",
    ("codex-app-server", "openai"): "Codex App Server",
    ("trae-agent", "openai"): "Trae Agent",
    ("claude-code", "deepseek"): "Claude Code + DeepSeek",
}
OVERRIDES_FILENAME = "overrides.json"
MAX_API_KEY_CHARS = 4096
AUTH_FILENAME = "auth.json"


class WebError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class StreamClosed(RuntimeError):
    """The SSE takeover stream's client went away; stop emitting."""


def _settings_path(project_root: Path) -> Path:
    # Check the lexical path before resolving it so a local symlink cannot
    # redirect settings writes outside the project.
    lexical = project_root / LOCAL_SETTINGS_PATH
    root = project_root.resolve()
    if root not in lexical.resolve().parents:
        raise WebError(500, "settings path escapes project root")
    settings_dir = lexical.parent
    if settings_dir.exists() and settings_dir.is_symlink():
        raise WebError(500, "local settings directory must not be a symlink")
    if lexical.exists() and lexical.is_symlink():
        raise WebError(500, "local settings file must not be a symlink")
    path = lexical.resolve()
    if root not in path.parents:
        raise WebError(500, "settings path escapes project root")
    return path


def _validate_judge_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebError(400, "judge_name must be a non-empty string")
    value = value.strip()
    if len(value.encode("utf-8")) > MAX_JUDGE_NAME_BYTES:
        raise WebError(413, "judge_name is too large")
    return value


def _read_judge_name(project_root: Path) -> str:
    path = _settings_path(project_root)
    if not path.exists():
        return DEFAULT_JUDGE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebError(500, "local settings are invalid") from exc
    if not isinstance(payload, dict):
        raise WebError(500, "local settings are invalid")
    value = payload.get("judge_name", DEFAULT_JUDGE_NAME)
    try:
        return _validate_judge_name(value)
    except WebError as exc:
        raise WebError(500, "local settings contain an invalid judge_name") from exc


def _write_judge_name(project_root: Path, judge_name: str) -> None:
    _atomic_write_json(_settings_path(project_root), {"judge_name": judge_name})


def _atomic_write_json(path: Path, payload: Dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _event_revision(store: MeetingStore) -> str:
    paths = [store.manifest_path, store.events_path, store.run_dir / "conversation.md"]
    signature: List[str] = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            signature.append(f"{path.name}:missing")
    return sha256_text("\n".join(signature))[:24]


def _events_by_id(store: MeetingStore) -> Dict[str, Dict[str, Any]]:
    return {
        str(event.get("event_id")): event
        for event in store.events()
        if event.get("event_id")
    }


def meeting_title(store: MeetingStore) -> str:
    """A meeting's one-line title: the human's first message.

    `conversation.md` starts with it, so the list does not parse a journal for
    every meeting just to name it.
    """
    try:
        head = (store.run_dir / "conversation.md").read_text(encoding="utf-8")[:4096]
    except OSError:
        return ""
    for block in head.split("\n## ")[1:]:
        if not block.startswith("You ·"):
            continue
        lines = [line.strip() for line in block.splitlines()[1:] if line.strip()]
        if lines:
            return lines[0][:80]
    return ""


def _public_manifest(store: MeetingStore) -> Dict[str, Any]:
    payload = store.meeting.to_dict()
    payload["run_path"] = str(store.run_dir)
    payload["event_count"] = store.event_count()
    payload["workspace_root"] = store.meeting.workspace_root or ""
    payload["title"] = meeting_title(store)
    return payload


def _as_workspace(asked: Any, base: Path) -> Path:
    """把对话框里填的那个目录变成一个工作区。

    留空就是默认的那一个；填了就用它（相对路径按默认那个算）。填的目录必须已经
    存在——工作区是**已有的项目**，不是随手新造一个空目录。
    """
    text = "" if asked is None else str(asked).strip()
    if not text:
        return base
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_dir():
        raise WebError(400, f"目录不存在：{text}")
    return path


def _ticket_root(store: MeetingStore) -> Path:
    """这场会的工单那一侧放在哪儿：会议目录下面。"""
    return Path(store.run_dir) / TICKET_ROOT


def _ticket_record(root: Path) -> List[str]:
    """工单那张会议记录（没有就是空的）。"""
    try:
        return (Path(root) / "meeting_record.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _ticket_case(meeting: Meeting) -> str:
    """工单跑在哪个案子下：材料都在案子自己的数据路径下，看 store 里的头两段。"""
    cases: List[str] = []
    for data_path, _name in meeting.store.materials():
        parts = data_path.split("/")
        if len(parts) >= 2 and parts[0] == "case":
            case = "/".join(parts[:2])
            if case not in cases:
                cases.append(case)
    return cases[0] if cases else ""


def _ticket_issued(meeting: Meeting) -> List[str]:
    """发过的工单，按发的先后（就是会议记录里那几行的先后）。"""
    out: List[str] = []
    for line in _ticket_record(meeting.root):
        match = re.match(r"- 工单 (\S+)（", line)
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def _ticket_step(meeting: Meeting, case: str, path: str, op: str) -> Dict[str, Any]:
    """一步：哪条数据路径、哪个算子、答了没有、归哪个角色答、答的是什么（叶子是标量时）。"""
    absolute = meeting.absolute(case, path)
    # `read` 交回的是材料本身：叶子就是那个标量。扳手那种长材料（一版代码）不往页面上搬，
    # 只有判语、收尾这类短的话才带上——这一块是给页面看的。
    value, answered = meeting.store.read(absolute, op)
    if isinstance(value, bool) or isinstance(value, (int, float)):
        leaf = value
    elif isinstance(value, str) and 0 < len(value) <= 200:
        leaf = value
    else:
        leaf = None
    return {"path": absolute, "op": op, "answered": answered,
            "role": meeting.workflow.role_of_op(op),
            "value": leaf}


def _ticket_target(store: MeetingStore) -> str:
    """agent 要交的那个文件：人类第一句话里点名的文件名（页面上没有别的来源）。"""
    for event in store.events():
        if event.get("kind") == "chat" and event.get("actor") == "human":
            for token in re.findall(r"[\w./-]+\.[A-Za-z0-9]+", str(event.get("content", ""))):
                if not token.startswith("http"):
                    return token
            break
    return ""


def _ticket_board(store: MeetingStore, project_root=None) -> Dict[str, Any]:
    """这场会的工单板：该发哪一张、每张要读什么/答了没有、发过哪几张、会议记录。

    页面那一块"工单"就是它：`GET /api/meetings/<id>/tickets`。
    """
    root = _ticket_root(store)
    workflow = load_workflow(project_root=project_root)
    meeting = Meeting(root, workflow)
    case = _ticket_case(meeting)
    issued = _ticket_issued(meeting)
    order = issued + [name for name in sorted(workflow.tickets()) if name not in issued]
    tickets: List[Dict[str, Any]] = []
    for ticket in order:
        due, why = meeting.due(ticket, case) if case else (False, "还没有案子：人类得先说话")
        directory = root / "tickets" / ticket
        bundles = sorted(path.name for path in directory.iterdir() if path.is_dir()) \
            if directory.is_dir() else []
        tickets.append({
            "ticket": ticket,
            "issued": bool(bundles),
            "due": bool(due),
            "why": why,
            "reads": [_ticket_step(meeting, case, path, op)
                      for path, op in workflow.addresses_in(ticket)],
            "answers": [_ticket_step(meeting, case, path, op)
                        for path, op in workflow.answers_of(ticket)],
            "bundles": bundles,
        })
    return {
        "root": f"runs/{store.meeting.meeting_id}/{TICKET_ROOT}",
        "ran": (root / "store").is_dir() or (root / "meeting_record.md").is_file(),
        "workflow_dir": str(workflow_root(project_root=project_root)),
        "digest": workflow.digest(),
        "rules": sorted(workflow.rules()),
        "case": case,
        "target": _ticket_target(store),
        "tickets": tickets,
        "record": _ticket_record(root),
    }


def _ticket_command(role: str, root: Path, case: str, bundle: Path, url: str, meeting_id: str,
                    workspace: Path, target: str) -> List[str]:
    """一个成员进程的命令行：发给它的那一包在哪儿，跑在哪个案子下。"""
    # `-c` 而不是 `-m`：包的 __init__ 已经把这些模块导进来了，`-m` 会在子进程里再导一次并
    # 往 journal 里留一句 RuntimeWarning。入口是一样的。
    command = [sys.executable, "-c",
               f"import sys; from ticket_centered.{role} import main; sys.exit(main())",
               "--meeting", str(root), "--case", case, "--bundle", str(bundle)]
    if role == "actor":
        command += ["--meeting-url", url, "--meeting-id", meeting_id,
                    "--workspace", str(workspace), "--target", target]
    return command


def _apply_meeting_effort(config: ModelApiConfig, store: MeetingStore) -> ModelApiConfig:
    if store.meeting.reasoning_effort:
        return replace(config, reasoning_effort=store.meeting.reasoning_effort)
    return config


class MeetingWebService:
    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        model_api_path: Optional[Path] = None,
        skill_path: Optional[Path] = None,
        runner_factory: Callable[..., InteractiveRunner] = InteractiveRunner,
        client_factory: Optional[Callable[[ModelApiConfig], AgentBackend]] = None,
        transport_factory: Callable[[str], TmuxTransport] = TmuxTransport,
        default_backend: Optional[str] = None,
        default_provider: Optional[str] = None,
        workspace: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        # 这块程序只有**一个**工作区：一个目录。会议数据在 `<它>/.meeting/` 下，
        # 没有第二套存放方式、也没有一张工作区清单要挑。
        self.workspace = (Path(workspace).expanduser().resolve() if workspace is not None
                          else self.project_root)
        if not self.workspace.is_dir():
            raise WebError(500, f"workspace not found: {self.workspace}")
        self.model_api_path = (model_api_path or self.project_root / "model_api").expanduser().resolve()
        self.skill_path = (
            skill_path
            or self.project_root / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"
        ).expanduser().resolve()
        self.runner_factory = runner_factory
        self.client_factory = client_factory
        self.transport_factory = transport_factory
        self.default_backend = default_backend
        self.default_provider = default_provider
        self.csrf_token = secrets.token_urlsafe(32)
        # The page issues tickets through the service; the member processes need
        # the service's own address to talk to the meeting, so it is kept here
        # (build_server fills in the real port).
        self.public_url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
        self.ticket_jobs: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.jobs: Dict[str, threading.Thread] = {}
        self.queued_jobs: Dict[str, Tuple[MeetingStore, ModelApiConfig, bool, bool]] = {}
        self.meeting_locks: Dict[str, threading.RLock] = {}
        self.capture_jobs: Dict[str, threading.Thread] = {}
        self.active_backends: Dict[str, AgentBackend] = {}
        self.human_pipes: Dict[str, bool] = {}
        self.human_geometry: Dict[str, Dict[str, int]] = {}
        # Whether the Agent was mid-work when the human took over; only then
        # does a plain 交还 resume it. Takeover from an idle/waiting state
        # returns to waiting instead of letting the Agent run on its own.
        self.human_resume: Dict[str, bool] = {}
        self._store_cache: Dict[str, MeetingStore] = {}
        self._backend_check_cache: Optional[Tuple[float, str, bool, str]] = None
        self._claude_check_cache: Optional[Tuple[float, str, bool, str]] = None
        self._trae_check_cache: Optional[Tuple[float, str, bool, str]] = None

    def sdk_meeting_data(self, meeting_id: str) -> Any:
        """The record this server holds for that meeting.

        One place to change when a test wants to hand out a record that never
        went through a journal.
        """
        store = self.load_store(meeting_id)
        data, _summary = meeting_loader.load_meeting_data_from_run(store.run_dir)
        return data

    def sdk_call(self, meeting_id: str, api: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """One meeting-sdk api, run here over the record this server holds.

        The answer is a viba script (``entry = ...``) whatever happened: an api
        that failed answers its own $err branch, so the script is always a
        resident of that api's return type.
        """
        if api not in meeting_sdk_viba.api_names() and api not in meeting_sdk_viba.builders():
            raise WebError(404, f"no such meeting sdk api: {api}")
        if api not in meeting_sdk_viba.api_names():
            raise WebError(400, f"{api} builds a value; it is not served over the wire")
        script = meeting_sdk_viba.script_for(api, self._sdk_result(meeting_id, api, args))
        if isinstance(script, _SdkErr):
            raise WebError(500, script.err_msg)
        return {"script": script.ok_value}

    def _sdk_result(self, meeting_id: str, api: str, args: Dict[str, Any]):
        """The api's own Result, evaluated over the record this server holds."""
        function = getattr(meeting_sdk_server, api)
        return function(self.sdk_meeting_data(meeting_id),
                        **meeting_sdk_json.decode(args or {}))

    def _meeting_lock(self, meeting_id: str) -> threading.RLock:
        with self.lock:
            return self.meeting_locks.setdefault(meeting_id, threading.RLock())

    def _transport(self) -> TmuxTransport:
        return self.transport_factory(shutil.which("tmux") or "tmux")

    def _orphaned_agent(self, store: MeetingStore, meeting_id: str) -> bool:
        """An agent phase with no live worker thread behind it."""
        return (
            store.meeting.phase in {"agent_thinking", "agent_acting"}
            and not self._job_alive(meeting_id)
        )

    def _job_alive(self, meeting_id: str) -> bool:
        with self.lock:
            worker = self.jobs.get(meeting_id)
            return bool((worker and worker.is_alive()) or meeting_id in self.queued_jobs)

    def _terminal_alive(self, store: MeetingStore) -> bool:
        """Whether the meeting's tmux session and pane still exist.

        A meeting outlives its terminal when the session is killed outside
        RemoteBuddy. Control operations that would drive the pane must check
        this first: discovering it halfway through used to fail the meeting.
        """
        if store.meeting.session_state == "stopped":
            return False
        pane_id = store.meeting.pane_id
        if not pane_id:
            # The workspace session is created by the first agent cycle, so a
            # meeting that never had a pane is not a meeting without a terminal.
            return True
        transport = self._transport()
        try:
            if not transport.session_exists(store.meeting.session_name):
                return False
            return str(transport.pane_info(pane_id).get("dead", "0")) == "0"
        except (TmuxError, OSError):
            return False

    def _require_terminal(self, store: MeetingStore, action: str) -> None:
        """Refuse an action that needs a terminal the meeting no longer has.

        The refusal also records what the probe just learned: the session is
        gone, so the meeting must stop offering terminal operations.
        """
        if self._terminal_alive(store):
            return
        if store.meeting.session_state != "stopped":
            store.mark_session_stopped()
        raise WebError(410, f"terminal session is gone; cannot {action}")

    def _make_backend(self, config: ModelApiConfig) -> AgentBackend:
        if self.client_factory is not None:
            return self.client_factory(config)
        if config.agent_backend == "codex-app-server":
            return CodexAppServerBackend(config)
        if config.agent_backend == "trae-agent":
            return TraeAgentBackend(config)
        if config.agent_backend == "claude-code":
            return ClaudeCodeBackend(config)
        return OpenAIResponsesClient(config)

    def _interrupt_backend(self, meeting_id: str) -> None:
        with self.lock:
            backend = self.active_backends.get(meeting_id)
        if backend is None:
            return
        try:
            backend.interrupt()
        except Exception:
            # The runner owns failure classification; control operations stay
            # idempotent even if the model process has already exited.
            pass

    def close_human_pipe(self, meeting_id: str) -> None:
        """Best-effort pipe cleanup after a stream ends (release/stop/etc.)."""
        try:
            with self._meeting_lock(meeting_id):
                store = self.load_store(meeting_id)
                self._sync_human_pipe(store)
        except (WebError, MeetingError, OSError):
            pass

    def _human_pipe_path(self, store: MeetingStore) -> Path:
        return store.events_path.parent / "human-pipe.log"

    def _sync_human_pipe(self, store: MeetingStore) -> None:
        """Open/close the takeover output pipe to match current ownership.

        The pipe records every byte the pane emits while the human owns it
        (``tmux pipe-pane``); the browser's SSE stream tails this file. It is
        opened when ownership becomes human and closed when it leaves human,
        and each open truncates leftovers from an earlier takeover.
        """
        meeting_id = store.meeting.meeting_id
        pane_id = store.meeting.pane_id
        wants_open = (
            store.meeting.status not in TERMINAL_STATUSES
            and store.meeting.control_owner == "human"
            and store.meeting.phase == "human_control"
            and bool(pane_id)
        )
        with self.lock:
            is_open = self.human_pipes.get(meeting_id, False)
            if wants_open == is_open:
                return
        transport = self._transport()
        path = self._human_pipe_path(store)
        if wants_open:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8"):
                    pass
                transport.start_output_pipe(pane_id, str(path))
                with self.lock:
                    self.human_pipes[meeting_id] = True
            except (TmuxError, OSError, AttributeError) as exc:
                store.append_event(
                    "error",
                    "harness",
                    "human_control",
                    content=f"cannot open output pipe: {exc}",
                )
        else:
            try:
                transport.stop_output_pipe(pane_id)
            except (TmuxError, OSError, AttributeError):
                pass  # the pane may already be gone, or the transport has no pipe
            with self.lock:
                self.human_pipes[meeting_id] = False
            # Handing the terminal back restores the pane to the size the
            # Agent's earlier frames were captured at.
            self._restore_human_geometry(store)

    def _restore_human_geometry(self, store: MeetingStore) -> None:
        meeting_id = store.meeting.meeting_id
        with self.lock:
            original = self.human_geometry.get(meeting_id)
            if not original:
                return
            self.human_geometry.pop(meeting_id, None)
        try:
            transport = self._transport()
            transport.resize_workspace_session(
                store.meeting.session_name, original["cols"], original["rows"]
            )
        except (TmuxError, OSError, RunnerError, AttributeError):
            pass  # the session may be gone; nothing to restore

    def meeting_live_state(self, meeting_id: str) -> Dict[str, Any]:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            return {
                "owner": store.meeting.control_owner,
                "phase": store.meeting.phase,
                "status": store.meeting.status,
            }

    def human_stream_start(
        self,
        meeting_id: str,
        cols: Optional[int] = None,
        rows: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Prepare the takeover byte stream: pipe open + a fresh screen seed.

        When the browser supplies ``cols``/``rows`` the workspace window is
        resized to that exact grid first (a real interactive terminal matches
        the pane to the client); the pre-takeover size is remembered and
        restored when control is handed back. Returns the output file path,
        the byte offset from which new output should be served, and a
        base64-encoded ANSI seed of the current screen.
        """
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            if (
                store.meeting.control_owner != "human"
                or store.meeting.phase != "human_control"
            ):
                raise WebError(409, "no active human takeover")
            pane_id = store.meeting.pane_id
            if not pane_id:
                raise WebError(409, "workspace pane is not available")
            transport = self._transport()
            if cols is not None or rows is not None:
                if not isinstance(cols, int) or not isinstance(rows, int):
                    raise WebError(400, "terminal stream size must be integers")
                if cols < 40 or cols > 300 or rows < 12 or rows > 120:
                    raise WebError(400, "terminal stream size is outside supported bounds")
                try:
                    info = transport.pane_info(pane_id)
                    current_cols = int(info["pane_width"])
                    current_rows = int(info["pane_height"])
                except (TmuxError, OSError, ValueError) as exc:
                    raise WebError(502, f"cannot read pane size: {exc}") from exc
                if current_cols != cols or current_rows != rows:
                    with self.lock:
                        self.human_geometry.setdefault(
                            meeting_id, {"cols": current_cols, "rows": current_rows}
                        )
                    try:
                        transport.resize_workspace_session(
                            store.meeting.session_name, cols, rows
                        )
                    except (TmuxError, OSError) as exc:
                        raise WebError(502, f"cannot resize workspace: {exc}") from exc
            self._sync_human_pipe(store)
            try:
                seed_ansi = transport.capture_visible_ansi(pane_id)
                info = transport.pane_info(pane_id)
                cursor_x = int(info["cursor_x"])
                cursor_y = int(info["cursor_y"])
            except (TmuxError, OSError, ValueError) as exc:
                raise WebError(502, f"cannot seed terminal stream: {exc}") from exc
            # Park the emulator's cursor exactly where the pane's cursor is.
            # New output usually does NOT carry a cursor-move before its first
            # character (a typed key is echoed as just the character), so a
            # seed ending at the wrong cell makes every echo land misplaced
            # and later erases only partially repair it -- the visible mess.
            seed_bytes = seed_ansi.encode("utf-8") + (
                f"\x1b[{cursor_y + 1};{cursor_x + 1}H".encode("ascii")
            )
            # The pane's ACTUAL size after any resize is the one truth; the
            # emulator must match it exactly (tmux can clamp/clip requests).
            pane_width = int(info["pane_width"])
            pane_height = int(info["pane_height"])
            path = self._human_pipe_path(store)
            # Serve strictly newer bytes than the seed frame shows.
            offset = path.stat().st_size if path.exists() else 0
        return {
            "meeting_id": meeting_id,
            "path": str(path),
            "seed": base64.b64encode(seed_bytes).decode("ascii"),
            "seed_offset": offset,
            "pane_width": pane_width,
            "pane_height": pane_height,
        }

    def load_store(self, meeting_id: str) -> MeetingStore:
        if not MEETING_ID_RE.fullmatch(meeting_id):
            raise WebError(400, "invalid meeting id")
        with self.lock:
            cached = self._store_cache.get(meeting_id)
        if cached is not None:
            try:
                cached.refresh()
                with self.lock:
                    self._store_cache.pop(meeting_id, None)
                    self._store_cache[meeting_id] = cached
                return cached
            except MeetingError:
                with self.lock:
                    self._store_cache.pop(meeting_id, None)
        try:
            store = self._open_store(meeting_id)
        except UnsupportedMeetingError as exc:
            raise WebError(404, "meeting not found") from exc
        except MeetingError as exc:
            message = str(exc)
            raise WebError(404 if "not found" in message else 422, message) from exc
        with self.lock:
            # A small LRU keeps the actively polled meeting's parsed journal
            # warm without pinning every meeting's events in memory.
            self._store_cache[meeting_id] = store
            while len(self._store_cache) > 2:
                self._store_cache.pop(next(iter(self._store_cache)))
        return store

    def _open_store(self, meeting_id: str) -> MeetingStore:
        """这场会在哪个工作区的 `.meeting/` 下，就按那条数据路径读回来。"""
        for directory in self._meeting_dirs():
            if directory.name == meeting_id:
                return MeetingStore.load_at(directory, meeting_id)
        raise MeetingError(f"meeting not found: {meeting_id}")

    def revision(self, meeting_id: str) -> str:
        """Stat-only change marker used to answer conditional GETs early."""
        return _event_revision(self.load_store(meeting_id))

    def _load_config(self) -> ModelApiConfig:
        try:
            return self._apply_overrides(ModelApiConfig.load(self.model_api_path))
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc

    def _overrides_path(self) -> Path:
        path = self.model_api_path / OVERRIDES_FILENAME
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise WebError(500, "model_api/overrides.json must be a regular file")
        return path

    def _read_overrides(self) -> Dict[str, Dict[str, str]]:
        path = self._overrides_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(500, "model_api/overrides.json is invalid") from exc
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            raise WebError(500, "model_api/overrides.json is invalid")
        result: Dict[str, Dict[str, str]] = {}
        for provider, fields in providers.items():
            if provider not in SUPPORTED_PROVIDERS or not isinstance(fields, dict):
                raise WebError(500, "model_api/overrides.json is invalid")
            base_url = fields.get("base_url")
            if base_url is not None and not isinstance(base_url, str):
                raise WebError(500, "model_api/overrides.json is invalid")
            entry = {key: value for key, value in fields.items() if key == "base_url" and isinstance(value, str) and value}
            if entry:
                result[provider] = entry
        return result

    def _apply_overrides(self, config: ModelApiConfig) -> ModelApiConfig:
        overrides = self._read_overrides()
        if not overrides:
            return config
        profiles: Dict[str, Dict[str, Any]] = {
            name: dict(profile) if isinstance(profile, Mapping) else {}
            for name, profile in config.provider_profiles.items()
        }
        changed = False
        for provider, fields in overrides.items():
            base_url = fields.get("base_url")
            if base_url:
                profiles.setdefault(provider, {})["base_url"] = base_url
                changed = True
                if provider == config.agent_provider:
                    config = replace(config, base_url=base_url.rstrip("/"))
        return replace(config, provider_profiles=profiles) if changed else config

    def _auth_key_status(self, config: ModelApiConfig, provider: str) -> Tuple[str, bool]:
        profile = config.provider_profiles.get(provider, {})
        auth_name = profile.get("auth_key") if isinstance(profile, Mapping) else None
        if not isinstance(auth_name, str) or not auth_name:
            auth_name = _default_auth_key(provider)
        return auth_name, bool(_select_auth_key(config.auth_keys, auth_name))

    def credentials_status(self) -> Dict[str, Any]:
        """Describe stored credentials without ever returning secret values."""
        overrides = self._read_overrides()
        try:
            config: Optional[ModelApiConfig] = ModelApiConfig.load(self.model_api_path)
        except ModelConfigError:
            config = None
        providers: Dict[str, Any] = {}
        for provider in sorted(SUPPORTED_PROVIDERS):
            if config is not None:
                auth_name, has_key = self._auth_key_status(config, provider)
            else:
                auth_name, has_key = _default_auth_key(provider), False
            providers[provider] = {
                "auth_key_name": auth_name,
                "has_api_key": has_key,
                "base_url_override": overrides.get(provider, {}).get("base_url"),
            }
        return {"providers": providers}

    def update_credentials(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an API key and optional base URL for one provider."""
        if not isinstance(payload, dict) or not set(payload).issubset({"provider", "api_key", "base_url"}):
            raise WebError(400, "credentials request contains unsupported fields")
        provider = payload.get("provider")
        if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
            raise WebError(400, "provider must be one of: " + ", ".join(sorted(SUPPORTED_PROVIDERS)))
        api_key = payload.get("api_key")
        base_url = payload.get("base_url")
        if api_key is None and base_url is None:
            raise WebError(400, "credentials request requires api_key or base_url")
        if api_key is not None:
            if not isinstance(api_key, str) or not api_key.strip():
                raise WebError(400, "api_key must be a non-empty string")
            api_key = api_key.strip()
            if len(api_key) > MAX_API_KEY_CHARS:
                raise WebError(413, "api_key is too large")
            if any(character.isspace() or not character.isprintable() for character in api_key):
                raise WebError(400, "api_key contains unsupported characters")
        if base_url is not None:
            if not isinstance(base_url, str):
                raise WebError(400, "base_url must be a string")
            base_url = base_url.strip()
            if base_url:
                try:
                    _validate_base_url(base_url)
                except ModelConfigError as exc:
                    raise WebError(400, str(exc)) from exc
        with self.lock:
            if api_key is not None:
                self._write_auth_key(provider, api_key)
            if base_url is not None:
                self._write_base_url_override(provider, base_url)
            self._backend_check_cache = None
            self._claude_check_cache = None
            self._trae_check_cache = None
        return self.credentials_status()

    def _write_auth_key(self, provider: str, api_key: str) -> None:
        auth_path = self.model_api_path / AUTH_FILENAME
        if auth_path.exists() and (auth_path.is_symlink() or not auth_path.is_file()):
            raise WebError(500, "model_api/auth.json must be a regular file")
        existing: Dict[str, str] = {}
        if auth_path.exists():
            try:
                payload = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebError(500, "model_api/auth.json must be valid JSON") from exc
            if not isinstance(payload, dict):
                raise WebError(500, "model_api/auth.json must be a JSON object")
            existing = {
                key: value
                for key, value in payload.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }
        name = _default_auth_key(provider)
        legacy = existing.pop("api_key", None)
        if legacy and name != "OPENAI_API_KEY" and "OPENAI_API_KEY" not in existing:
            # The legacy alias historically carried the OpenAI key; promote it
            # to the dedicated key so both providers can coexist.
            existing["OPENAI_API_KEY"] = legacy
        existing[name] = api_key
        _atomic_write_json(auth_path, existing, mode=0o600)

    def _write_base_url_override(self, provider: str, base_url: str) -> None:
        overrides = self._read_overrides()
        entry = dict(overrides.get(provider, {}))
        if base_url:
            entry["base_url"] = base_url.rstrip("/")
        else:
            entry.pop("base_url", None)
        if entry:
            overrides[provider] = entry
        else:
            overrides.pop(provider, None)
        _atomic_write_json(self._overrides_path(), {"providers": overrides}, mode=0o600)

    def settings(self) -> Dict[str, Any]:
        """Return machine-local page settings that are safe to expose."""
        with self.lock:
            return {"judge_name": _read_judge_name(self.project_root)}

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"judge_name"}:
            raise WebError(400, "settings request must contain only judge_name")
        judge_name = _validate_judge_name(payload.get("judge_name"))
        with self.lock:
            _write_judge_name(self.project_root, judge_name)
        return {"judge_name": judge_name}

    def _probe_codex_config(self, config: ModelApiConfig) -> Tuple[bool, str]:
        cache_key = f"{config.model}:{config.reasoning_effort}:{config.codex_dynamic_tools}"
        now = time.monotonic()
        cached = self._backend_check_cache
        if cached and cached[1] == cache_key and now - cached[0] < BACKEND_PROBE_CACHE_SECONDS:
            return cached[2], cached[3]
        try:
            detail = probe_codex_backend(config)
            ok = True
        except CodexAppServerError as exc:
            ok, detail = False, str(exc)
        self._backend_check_cache = (time.monotonic(), cache_key, ok, detail)
        return ok, detail

    def _probe_trae_config(self, config: ModelApiConfig) -> Tuple[bool, str]:
        cache_key = f"{config.model}:{config.reasoning_effort}"
        now = time.monotonic()
        cached = self._trae_check_cache
        if cached and cached[1] == cache_key and now - cached[0] < BACKEND_PROBE_CACHE_SECONDS:
            return cached[2], cached[3]
        try:
            detail = probe_trae_backend(config)
            ok = True
        except CodexAppServerError as exc:
            ok, detail = False, str(exc)
        self._trae_check_cache = (time.monotonic(), cache_key, ok, detail)
        return ok, detail

    def _probe_claude_config(self, config: ModelApiConfig) -> Tuple[bool, str]:
        cache_key = f"{config.model}:{config.base_url}:{config.claude_binary}"
        now = time.monotonic()
        cached = self._claude_check_cache
        if cached and cached[1] == cache_key and now - cached[0] < BACKEND_PROBE_CACHE_SECONDS:
            return cached[2], cached[3]
        try:
            detail = probe_claude_backend(config)
            ok = True
        except ClaudeCodeError as exc:
            ok, detail = False, str(exc)
        self._claude_check_cache = (time.monotonic(), cache_key, ok, detail)
        return ok, detail

    @staticmethod
    def _validate_backend_pair(backend: Any, provider: Any) -> Tuple[str, str]:
        if not isinstance(backend, str) or not isinstance(provider, str):
            raise WebError(400, "agent_backend and agent_provider must be strings")
        if (backend, provider) not in BACKEND_COMBINATIONS:
            raise WebError(400, "agent backend/provider combination is not supported")
        return backend, provider

    def _resolve_config(
        self,
        config: ModelApiConfig,
        backend: str,
        provider: str,
        model: Optional[str],
    ) -> ModelApiConfig:
        self._validate_backend_pair(backend, provider)
        try:
            return config.resolve(backend, provider, model)
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc

    def _probe_selected_config(self, config: ModelApiConfig) -> None:
        if self.client_factory is not None:
            return
        if config.agent_backend == "codex-app-server":
            ok, detail = self._probe_codex_config(config)
        elif config.agent_backend == "trae-agent":
            ok, detail = self._probe_trae_config(config)
        elif config.agent_backend == "claude-code":
            ok, detail = self._probe_claude_config(config)
        else:
            return
        if not ok:
            raise WebError(503, detail)

    def _config_for_store(
        self, store: MeetingStore, config: Optional[ModelApiConfig] = None
    ) -> ModelApiConfig:
        resolved = self._resolve_config(
            config or self._load_config(),
            store.meeting.agent_backend,
            store.meeting.agent_provider,
            store.meeting.model,
        )
        resolved = _apply_meeting_effort(resolved, store)
        self._probe_selected_config(resolved)
        return resolved

    def _workspace_index(self) -> Path:
        """默认工作区里记着"还用过哪些目录"的那一行行文本。"""
        return self.workspace / MeetingStore.WORKSPACE_DIR / WORKSPACE_INDEX

    def _known_workspaces(self) -> List[Path]:
        """默认那一个，加上以前用过、现在还在的那些目录。"""
        found = [self.workspace]
        try:
            lines = self._workspace_index().read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            path = Path(line.strip()).expanduser()
            if line.strip() and path.is_dir() and path not in found:
                found.append(path.resolve())
        return found

    def _remember_workspace(self, workspace: Path) -> None:
        """用过一次的目录记下来：不然重启之后它的会议就从列表里消失了。"""
        if workspace == self.workspace or workspace in self._known_workspaces():
            return
        index = self._workspace_index()
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a", encoding="utf-8") as handle:
            handle.write(f"{workspace}\n")

    def _meeting_dirs(self) -> List[Path]:
        """每一场会的数据目录：各个工作区 `.meeting/` 下的一个会议号目录。"""
        found: List[Path] = []
        seen: set = set()
        for workspace in self._known_workspaces():
            folder = workspace / MeetingStore.WORKSPACE_DIR
            if not folder.is_dir() or folder.is_symlink():
                continue
            for child in sorted(folder.iterdir()):
                if child.is_dir() and not child.is_symlink() and child not in seen:
                    seen.add(child)
                    found.append(child)
        return found

    def list_meetings(self) -> Dict[str, Any]:
        summaries: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for directory in self._meeting_dirs():
            if not MEETING_ID_RE.fullmatch(directory.name):
                continue
            manifest = directory / "meeting.json"
            if MeetingStore.peek_schema(manifest) != SCHEMA_VERSION:
                continue
            try:
                store = MeetingStore.load_at(directory, directory.name)
                summaries.append(_public_manifest(store))
            except (MeetingError, OSError, ValueError) as exc:
                warnings.append(f"{directory.name}: {exc}")
        summaries.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return {"meetings": summaries, "warnings": warnings,
                "workspace": str(self.workspace),
                "workspaces": [str(workspace) for workspace in self._known_workspaces()]}

    def snapshot(self, meeting_id: str) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        if store.meeting.control_owner == "human":
            self._ensure_capture_worker(store)
            self._sync_human_pipe(store)
        # One parse serves the whole payload. The frontend rebuilds chat and
        # scores from its own timeline, so they are not shipped per poll.
        events = store.events()
        latest_screen = next(
            (event for event in reversed(events) if event.get("kind") == "screen"), None
        )
        return {
            "meeting": _public_manifest(store),
            "latest_screen": latest_screen,
            "revision": _event_revision(store),
            "cursor": int(events[-1]["seq"]) if events else 0,
        }

    # ------------------------------------------------------------------
    # 工单那一侧（ticket_centered）
    # ------------------------------------------------------------------

    def tickets(self, meeting_id: str) -> Dict[str, Any]:
        """这场会的工单板：该发哪一张、每张要读什么/答了没有、发过哪几张、会议记录。"""
        store = self.load_store(meeting_id)
        board = _ticket_board(store, self.project_root)
        with self.lock:
            job = self.ticket_jobs.get(meeting_id)
        board["running"] = job["ticket"] if job and job["thread"].is_alive() else None
        return {"meeting": _public_manifest(store), "tickets": board}

    def issue_ticket(self, meeting_id: str, ticket: str) -> Dict[str, Any]:
        """发一张工单：`Meeting.publish` 发给会上的每个成员，然后每个成员起一个进程跑。

        页面按一下按钮就是这一下。跑在后台，页面照旧轮询工单板：答下的材料成员自己抄回会议。
        """
        store = self.load_store(meeting_id)
        board = _ticket_board(store, self.project_root)
        if ticket not in [one["ticket"] for one in board["tickets"]]:
            raise WebError(404, f"这份流程里没有工单 {ticket}")
        if not board["case"]:
            raise WebError(409, "这场会还没有案子：人类得先说话")
        if not board["target"]:
            raise WebError(409, "人类那句话里没点名要交哪个文件，actor 不知道该读回哪个")
        with self.lock:
            job = self.ticket_jobs.get(meeting_id)
            if job and job["thread"].is_alive():
                raise WebError(409, f"{job['ticket']} 还在跑，等它跑完")
            thread = threading.Thread(target=self._run_ticket,
                                      args=(meeting_id, board["case"], ticket, board["target"]),
                                      daemon=True)
            self.ticket_jobs[meeting_id] = {"ticket": ticket, "thread": thread}
        thread.start()
        return {"meeting": _public_manifest(store), "tickets": board, "issued": ticket}

    def _run_ticket(self, meeting_id: str, case: str, ticket: str, target: str) -> None:
        """把包发给每个成员，再给每个成员起一个进程；谁答了什么，看工单板。"""
        try:
            store = self.load_store(meeting_id)
            root = _ticket_root(store)
            meeting = Meeting(root, load_workflow(project_root=self.project_root))
            for name in TICKET_MEMBERS:
                meeting.join(Member(name, name, {}))
            bundles = meeting.publish(ticket, case)
            store.append_event("state", "assistant", "ticket_issued",
                               data={"ticket": ticket, "case": case,
                                     "bundles": sorted(bundles)})
            environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
            for name, bundle in sorted(bundles.items()):
                command = _ticket_command(name, root, case, bundle, self.public_url,
                                          meeting_id, store.workspace_path, target)
                process = subprocess.Popen(command, cwd=str(PROJECT_ROOT), env=environment,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True)
                try:
                    output, _ = process.communicate(timeout=TICKET_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output = "（超时，已停）"
                store.append_event("state", name, "ticket_ran",
                                   data={"ticket": ticket, "output": output.strip()[-400:]})
        except Exception as exc:                    # noqa: BLE001 - 后台线程：写进 journal 才算有人知道
            try:
                self.load_store(meeting_id).append_event(
                    "state", "assistant", "ticket_failed",
                    data={"ticket": ticket, "error": f"{type(exc).__name__}: {exc}"})
            except Exception:                       # noqa: BLE001 - 连写都写不进去就只剩退出
                pass
        finally:
            with self.lock:
                self.ticket_jobs.pop(meeting_id, None)

    def _workspace_for(self, asked: Any) -> Path:
        """新会议开在哪个目录：对话框里填的那个，留空就是默认那个。"""
        if asked is not None and not isinstance(asked, str):
            raise WebError(400, "workspace must be a path")
        workspace = _as_workspace(asked, self.workspace)
        self._remember_workspace(workspace)
        return workspace

    def _validated_reference(
        self,
        store: MeetingStore,
        payload: Any,
        events_by_id: Optional[Mapping[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise WebError(400, "reference must be an object")
        event_id = payload.get("event_id")
        if events_by_id is None:
            events_by_id = _events_by_id(store)
        event = events_by_id.get(event_id) if isinstance(event_id, str) else None
        if event is None:
            raise WebError(422, "reference source event was not found")
        try:
            return reference_from_event(event, payload)
        except (TypeError, ValueError) as exc:
            raise WebError(422, str(exc)) from exc

    def _validated_references(
        self,
        store: MeetingStore,
        payload: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        """Validate the V6 reference list while accepting the V2 singular alias."""
        has_list = "references" in payload
        has_single = "reference" in payload
        if has_list and payload.get("references") is not None and not isinstance(payload.get("references"), list):
            raise WebError(400, "references must be an array")
        candidates: List[Any] = []
        if has_list and isinstance(payload.get("references"), list):
            candidates.extend(payload["references"])
        if has_single and payload.get("reference") is not None:
            # Some V6 callers kept the old field name while changing its
            # value to a list.  Accept that spelling alongside ``references``
            # so the migration does not force a flag-day client update.
            singular_value = payload.get("reference")
            if isinstance(singular_value, list):
                candidates.extend(singular_value)
            else:
                candidates.append(singular_value)
        if len(candidates) > MAX_CHAT_REFERENCES:
            raise WebError(413, f"at most {MAX_CHAT_REFERENCES} references are allowed")
        # One journal pass validates every reference in the request.
        events_by_id = _events_by_id(store) if candidates else {}
        validated: List[Dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            reference = self._validated_reference(store, candidate, events_by_id)
            if reference is None:
                raise WebError(422, "reference must be an object")
            region = reference.get("region") if isinstance(reference.get("region"), Mapping) else {}
            key = (
                reference.get("kind"),
                reference.get("event_id"),
                region.get("start"),
                region.get("end"),
            )
            if key in seen:
                continue
            seen.add(key)
            validated.append(reference)
        return validated

    def events(self, meeting_id: str, after: Any = 0) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        try:
            sequence = int(after)
        except (TypeError, ValueError) as exc:
            raise WebError(400, "after must be an integer") from exc
        if sequence < 0:
            raise WebError(400, "after must be at least zero")
        # The idle poll asks for events after the head; answer it from the
        # tail sequence without materializing the journal.
        head = store.last_event_sequence()
        if head <= sequence:
            return {
                "meeting_id": meeting_id,
                "events": [],
                "cursor": head if head else sequence,
                "revision": _event_revision(store),
            }
        all_events = store.events()
        return {
            "meeting_id": meeting_id,
            "events": [event for event in all_events if int(event.get("seq", 0)) > sequence],
            "cursor": int(all_events[-1]["seq"]) if all_events else sequence,
            "revision": _event_revision(store),
        }

    def _new_id(self, workspace: Path) -> str:
        """A fresh meeting id, free in the workspace the meeting is going into."""
        for _ in range(20):
            value = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            if not (workspace / MeetingStore.WORKSPACE_DIR / value).exists():
                return value
        raise WebError(500, "could not allocate a meeting id")

    def start(self, payload: Dict[str, Any]) -> MeetingStore:
        if not isinstance(payload, dict):
            raise WebError(400, "request must be a JSON object")
        unexpected = set(payload) - {
            "message",
            "meeting_id",
            "workspace",
            "model",
            "typing_profile",
            "agent_backend",
            "agent_provider",
            "reasoning_effort",
            "agent_name2role",
        }
        if unexpected:
            raise WebError(400, "unexpected meeting fields: " + ", ".join(sorted(unexpected)))
        message = payload.get("message", "")
        # A meeting may be opened with no first message: it already owns a
        # fresh workspace directory (the "temporary directory as a meeting" a
        # client falls back to when nothing exists yet) and the agent starts on
        # the first message the human sends.
        if not isinstance(message, str):
            raise WebError(400, "message must be a string")
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise WebError(413, "message is too large")
        workspace = self._workspace_for(payload.get("workspace"))
        meeting_id = payload.get("meeting_id") or self._new_id(workspace)
        if not isinstance(meeting_id, str) or not MEETING_ID_RE.fullmatch(meeting_id):
            raise WebError(400, "invalid meeting id")
        config = self._load_config()
        backend = payload.get("agent_backend", self.default_backend or config.agent_backend)
        provider = payload.get("agent_provider", self.default_provider or config.agent_provider)
        backend, provider = self._validate_backend_pair(backend, provider)
        model = payload.get("model")
        if model is not None:
            if not isinstance(model, str):
                raise WebError(400, "model must be a short string")
            if model == "":
                # Empty string = "let the CLI use its own default model"
                # (subscription logins); only the app-server CLIs support it.
                if backend not in {"codex-app-server", "trae-agent"}:
                    raise WebError(400, "empty model is only supported for codex-app-server or trae-agent")
            elif not MODEL_RE.fullmatch(model):
                raise WebError(400, "model must be a short string")
        config = self._resolve_config(config, backend, provider, model)
        reasoning_effort = payload.get("reasoning_effort")
        if reasoning_effort is not None and reasoning_effort not in VALID_REASONING_EFFORTS:
            raise WebError(400, "reasoning_effort must be none, low, medium, high, xhigh, or max")
        if reasoning_effort:
            config = replace(config, reasoning_effort=reasoning_effort)
        self._probe_selected_config(config)
        model = config.model
        if not isinstance(model, str) or not (
            MODEL_RE.fullmatch(model)
            or (model == "" and config.agent_backend in {"codex-app-server", "trae-agent"})
        ):
            raise WebError(400, "model must be a short string")
        typing_profile = payload.get("typing_profile", "balanced")
        if typing_profile not in {"demo", "balanced", "fast"}:
            raise WebError(400, "typing_profile must be demo, balanced, or fast")
        session_name = f"remotebuddy-{meeting_id}"
        try:
            store = MeetingStore.create(
                self.project_root,
                meeting_id,
                message,
                self.skill_path,
                session_name,
                model,
                agent_backend=config.agent_backend,
                typing_profile=typing_profile,
                agent_provider=config.agent_provider,
                # The actor agent drives the terminal. Additional role
                # members (reviewers) are declared through
                # agent_name2role and are summoned by @mention at runtime;
                # they never hold terminal control.
                agent_ordinal=1,
                reasoning_effort=reasoning_effort,
                agent_name2role=payload.get("agent_name2role"),
                workspace=workspace,
            )
        except (MeetingError, OSError) as exc:
            raise WebError(400, str(exc)) from exc
        # Without a first message there is nothing for the agent to answer yet;
        # wait for the human to speak (the message endpoint wakes it).
        if message.strip():
            self._ensure_worker(store, config, resume=False, fresh_conversation=False)
        return store

    def _ensure_worker(
        self,
        store: MeetingStore,
        config: ModelApiConfig,
        *,
        resume: bool,
        fresh_conversation: bool,
    ) -> None:
        meeting_id = store.meeting.meeting_id
        try:
            config = config.resolve(
                store.meeting.agent_backend,
                store.meeting.agent_provider,
                store.meeting.model,
            )
        except ModelConfigError as exc:
            raise WebError(503, str(exc)) from exc
        config = _apply_meeting_effort(config, store)
        with self.lock:
            current = self.jobs.get(meeting_id)
            if current and current.is_alive():
                previous = self.queued_jobs.get(meeting_id)
                if previous:
                    fresh_conversation = fresh_conversation or previous[3]
                self.queued_jobs[meeting_id] = (store, config, resume, fresh_conversation)
                return
            client = self._make_backend(config)
            transport = self._transport()
            meeting_lock = self._meeting_lock(meeting_id)
            worker = threading.Thread(
                target=self._worker,
                args=(store, client, transport, meeting_lock, resume, fresh_conversation),
                name=f"remotebuddy-{meeting_id}",
                daemon=True,
            )
            self.jobs[meeting_id] = worker
            self.active_backends[meeting_id] = client
            worker.start()

    def _worker(
        self,
        store: MeetingStore,
        client: AgentBackend,
        transport: TmuxTransport,
        meeting_lock: threading.RLock,
        resume: bool,
        fresh_conversation: bool,
    ) -> None:
        pending: Optional[Tuple[MeetingStore, ModelApiConfig, bool, bool]] = None
        try:
            runner = self.runner_factory(store, transport, client, lock=meeting_lock)
            if resume:
                runner.resume(fresh_conversation=fresh_conversation)
            else:
                runner.start()
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            with self.lock:
                meeting_id = store.meeting.meeting_id
                if self.active_backends.get(meeting_id) is client:
                    self.active_backends.pop(meeting_id, None)
                if self.jobs.get(meeting_id) is threading.current_thread():
                    self.jobs.pop(meeting_id, None)
                pending = self.queued_jobs.pop(meeting_id, None)
            if pending:
                next_store, next_config, next_resume, next_fresh = pending
                self._ensure_worker(
                    next_store,
                    next_config,
                    resume=next_resume,
                    fresh_conversation=next_fresh,
                )

    def _ensure_capture_worker(self, store: MeetingStore) -> None:
        meeting_id = store.meeting.meeting_id
        with self.lock:
            current = self.capture_jobs.get(meeting_id)
            if current and current.is_alive():
                return
            worker = threading.Thread(
                target=self._capture_human_control,
                args=(meeting_id,),
                name=f"remotebuddy-capture-{meeting_id}",
                daemon=True,
            )
            self.capture_jobs[meeting_id] = worker
            worker.start()

    def _capture_human_control(self, meeting_id: str) -> None:
        transport = self._transport()
        try:
            while True:
                with self._meeting_lock(meeting_id):
                    store = self.load_store(meeting_id)
                    if store.meeting.status in TERMINAL_STATUSES:
                        return
                    if store.meeting.control_owner == "human":
                        try:
                            capture_workspace(store, transport)
                        except (RunnerError, TmuxError, OSError) as exc:
                            store.append_event("error", "harness", "human_control", content=str(exc))
                            if self._terminal_alive(store):
                                # The workspace is still there, so this is a
                                # genuine capture failure: fail the meeting.
                                store.update(
                                    status="failed",
                                    phase="failed",
                                    control_owner="none",
                                    failure=str(exc),
                                )
                            else:
                                # The terminal went away under the human's
                                # hands: park the meeting instead of failing a
                                # run nobody could have driven any further.
                                store.update(
                                    phase="waiting_for_user",
                                    control_owner="none",
                                    control_requested=None,
                                    pending_action_id=None,
                                    failure=None,
                                )
                                store.mark_session_stopped()
                            self._sync_human_pipe(store)
                            return
                    elif store.meeting.control_requested != "human":
                        return
                time.sleep(0.25)
        except (WebError, MeetingError, OSError):
            return
        finally:
            with self.lock:
                if self.capture_jobs.get(meeting_id) is threading.current_thread():
                    self.capture_jobs.pop(meeting_id, None)

    def set_reasoning_effort(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if not isinstance(payload, dict) or set(payload) != {"reasoning_effort"}:
                raise WebError(400, "request must contain only reasoning_effort")
            effort = payload["reasoning_effort"]
            if effort is not None and effort not in VALID_REASONING_EFFORTS:
                raise WebError(400, "reasoning_effort must be none, low, medium, high, xhigh, or max")
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            # A cycle already in flight keeps its frozen config; the override
            # is picked up when the next worker resolves its config.
            store.update(reasoning_effort=effort)
            return store

    def message(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if not isinstance(payload, dict) or not set(payload).issubset(
                {"message", "delivery", "reference", "references"}
            ):
                raise WebError(400, "message request contains unsupported fields")
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, str) or not message.strip():
                raise WebError(400, "message must be a non-empty string")
            if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise WebError(413, "message is too large")
            delivery = payload.get("delivery", "next_action")
            if delivery not in {"next_action", "interrupt"}:
                raise WebError(400, "delivery must be next_action or interrupt")
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            references = self._validated_references(store, payload)
            starting_fresh = store.meeting.phase == "created"
            orphaned_agent = self._orphaned_agent(store, meeting_id)
            wake_agent = (
                store.meeting.phase == "created" or store.meeting.phase in RESUMABLE_PHASES
                or orphaned_agent
            )
            fresh_conversation = (
                store.meeting.phase in {"api_error", "cycle_timeout"}
                or store.meeting.recovery_required
                or orphaned_agent
            )
            if "references" in payload or isinstance(payload.get("reference"), list):
                store.append_chat("human", message, "request", references=references)
            else:
                # Preserve the legacy singular journal shape when an older
                # client sends ``reference``; V6 callers use ``references``.
                store.append_chat(
                    "human",
                    message,
                    "request",
                    reference=references[0] if references else None,
                )
            if store.meeting.control_owner == "human" and store.meeting.phase == "human_control":
                # A chat while the human holds the terminal is an explicit
                # instruction: hand control back AND resume the Agent with it.
                self._require_terminal(store, "hand control back")
                self._sync_human_pipe(store)
                return self._resume_agent_from_human(meeting_id)
            # 2b routing: @mention summons the named non-actor member;
            # the actor is NOT woken for that message (single-writer rule).
            # If the actor is mid-turn, interrupt it and summon the
            # reviewer once it has parked, so the @mention is never answered
            # by the wrong speaker.
            #
            # Who is addressed does not depend on the delivery mode: an
            # immediate ("interrupt") @mention must still reach the reviewer
            # rather than the actor.
            mentioned_agent_names(message, store.meeting.effective_role_map)
            if delivery == "interrupt" or wake_agent:
                self._require_terminal(store, "run the agent")
                store.bump_request_generation(
                    failure=None,
                    cycle_action_count=0,
                    cycle_started_at=utc_now(),
                )
            else:
                store.update(failure=None)
            if delivery == "interrupt":
                self._interrupt_backend(meeting_id)
            if wake_agent or delivery == "interrupt":
                config = self._config_for_store(store)
                store.update(status="running", phase="agent_thinking", control_owner=store.meeting.actor_name())
                self._ensure_worker(
                    store,
                    config,
                    resume=not starting_fresh,
                    fresh_conversation=fresh_conversation,
                )
        return store

    def score(self, meeting_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Append a validated score without waking or mutating Agent state."""
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            allowed_fields = {"tag", "reference", "comment"} | set(SCORE_IGNORED_FIELDS)
            if not isinstance(payload, dict) or not set(payload).issubset(allowed_fields):
                raise WebError(400, "score request contains unsupported fields")
            tag = payload.get("tag")
            if not isinstance(tag, str) or tag not in VALID_SCORE_TAGS:
                raise WebError(400, "tag must be competent or incompetent")
            if "reference" not in payload:
                raise WebError(400, "score reference is required")
            reference = self._validated_reference(store, payload.get("reference"))
            if reference is None:
                raise WebError(422, "score reference is required")
            comment = payload.get("comment")
            if comment is not None:
                if not isinstance(comment, str):
                    raise WebError(400, "comment must be a string or null")
                comment = comment.strip() or None
                if comment is not None and len(comment.encode("utf-8")) > MAX_SCORE_COMMENT_BYTES:
                    raise WebError(413, "comment is too large")
            judge_name = _read_judge_name(self.project_root)
            try:
                return store.append_score(tag, reference, judge_name, score=None, comment=comment)
            except MeetingError as exc:
                raise WebError(422, str(exc)) from exc

    def acquire(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            # Refuse before touching any state: taking over a pane that no
            # longer exists used to fail the whole meeting afterwards.
            self._require_terminal(store, "take over")
            if store.meeting.control_owner == "human":
                return store
            orphaned_agent = self._orphaned_agent(store, meeting_id)
            with self.lock:
                self.human_resume[meeting_id] = (
                    store.meeting.phase in {"agent_thinking", "agent_acting"} or orphaned_agent
                )
            store.bump_request_generation(
                control_requested="human",
                recovery_required=store.meeting.recovery_required or orphaned_agent,
            )
            self._interrupt_backend(meeting_id)
            if store.meeting.phase in RESUMABLE_PHASES or orphaned_agent:
                store.update(phase="human_control", control_owner="human", control_requested=None)
                store.append_event("control", "human", "human_control", data={"status": "acquired"})
                store.note_terminal_control("human")
                self._sync_human_pipe(store)
            self._ensure_capture_worker(store)
        return store

    def release(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.control_owner != "human":
                return store
            with self.lock:
                resume_agent = self.human_resume.pop(meeting_id, False)
            if resume_agent:
                # The Agent was mid-work when the human took over: a plain 交还
                # resumes it where it stopped.
                return self._resume_agent_from_human(meeting_id)
            # The Agent was idle (waiting) when the human took over: hand back
            # to waiting instead of letting the Agent run on its own.
            store.update(
                phase="waiting_for_user",
                control_owner="none",
                control_requested=None,
                pending_action_id=None,
            )
            store.append_event("control", "human", "waiting_for_user", data={"status": "released"})
            # The hand-back is a control record like any other: with no agent
            # picking the pane up, it records that nobody holds it.
            store.note_terminal_control(None)
            self._sync_human_pipe(store)
            return self.load_store(meeting_id)

    def _resume_agent_from_human(self, meeting_id: str) -> MeetingStore:
        """Hand control to the Agent and start a resume cycle.

        Called with the meeting lock held, from release (Agent was mid-work)
        or from a chat sent during takeover (which always carries a task).
        """
        store = self.load_store(meeting_id)
        config = self._config_for_store(store)
        fresh_conversation = store.meeting.recovery_required
        store.bump_request_generation(
            phase="agent_thinking",
            control_owner=store.meeting.actor_name(),
            control_requested=None,
            cycle_action_count=0,
            cycle_started_at=utc_now(),
            status="running",
        )
        store.append_event("control", "human", "agent_thinking", data={"status": "released"})
        store.note_terminal_control(store.meeting.actor_name())
        self._sync_human_pipe(store)
        self._ensure_worker(
            store,
            config,
            resume=True,
            fresh_conversation=fresh_conversation,
        )
        return store

    def terminal_input(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        """Deliver human input into the shared pane while the human owns it.

        Two payload shapes are accepted:

        - ``{"bytes": "<hex>"}`` -- the takeover terminal's raw keystroke
          bytes (control characters, UTF-8 text, CSI sequences), sent to the
          pane verbatim and journaled as one human action.
        - ``{"keys": [...]}`` -- the Agent keystroke DSL (``$text`` /
          ``$ctrl`` / ``$rest`` plus bare literal strings), retained for
          programmatic callers and tests.

        The human is deliberately free to type anywhere -- shell, Vim, a
        REPL, or any foreground program -- so no process whitelist is
        enforced. Input re-checks ownership before sending, so a burst stops
        the moment the human releases, chats, or stops the meeting.
        """
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            if (
                store.meeting.control_owner != "human"
                or store.meeting.phase != "human_control"
            ):
                raise WebError(409, "acquire terminal control before sending input")
            fields = set(payload) if isinstance(payload, dict) else set()
            if fields == {"keys"}:
                pass
            elif fields == {"bytes"}:
                pass
            else:
                raise WebError(400, "terminal input requires a bytes or keys payload")
            pane_id = store.meeting.pane_id
            if not pane_id:
                raise WebError(409, "workspace pane is not available")
            hex_text: Optional[str] = None
            keys: Optional[list] = None
            if "bytes" in payload:
                hex_text = payload["bytes"]
                if not isinstance(hex_text, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", hex_text):
                    raise WebError(400, "bytes must be non-empty even-length hex")
                if len(hex_text) // 2 > 64 * 1024:
                    raise WebError(413, "bytes payload is too large")
            else:
                raw_keys = payload["keys"]
                if not isinstance(raw_keys, list) or not raw_keys:
                    raise WebError(400, "keys must be a non-empty array")
                try:
                    keys = normalize_model_key_tokens(raw_keys)
                    validate_key_tokens(keys)
                except (AgentProtocolError, TypeError, ValueError) as exc:
                    raise WebError(400, str(exc)) from exc
            transport = self._transport()
            action_id = "human-action-" + secrets.token_hex(6)
            profile = store.meeting.typing_profile or "balanced"
            # The raw input is not journaled: what the human typed never enters
            # the meeting record (ADT: Keystroke is harness-internal). The
            # bookkeeping only says that an action was taken, by whom, and when.
            store.append_event(
                "action",
                "human",
                "human_control",
                data={"action_id": action_id, "status": "received"},
            )
            store.append_event(
                "action", "human", "human_control", data={"action_id": action_id, "status": "started"}
            )

        def owns_terminal(current: MeetingStore) -> bool:
            return (
                current.meeting.status not in TERMINAL_STATUSES
                and current.meeting.control_owner == "human"
                and current.meeting.phase == "human_control"
            )

        def record(status: str, *, content: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
            with self._meeting_lock(meeting_id):
                current = self.load_store(meeting_id)
                data: Dict[str, Any] = {"action_id": action_id, "status": status}
                if extra:
                    data.update(extra)
                current.append_event(
                    "action",
                    "human",
                    "human_control",
                    content=content,
                    data=data,
                )

        cancelled = False
        try:
            if hex_text is not None:
                # Raw takeover keystrokes: one hex send, re-checked under the
                # lock, followed by a captured frame of the result.
                with self._meeting_lock(meeting_id):
                    store = self.load_store(meeting_id)
                    if not owns_terminal(store):
                        cancelled = True
                    else:
                        transport.send_hex(pane_id, hex_text)
                # No settle sleep and no per-batch screen capture here: the
                # pane echo returns through the byte stream immediately and
                # the 4Hz capture worker journals frames while takeover is
                # live. Waiting 50ms plus a capture made every keystroke
                # batch feel ~150ms late, worst for Enter/Backspace.
            else:
                for index, token in enumerate(keys):  # type: ignore[union-attr]
                    is_rest = token.get("kind") == "rest"
                    with self._meeting_lock(meeting_id):
                        store = self.load_store(meeting_id)
                        if not owns_terminal(store):
                            cancelled = True
                            break
                        if not is_rest:
                            transport.send_tokens(pane_id, [token])
                    if is_rest:
                        time.sleep(int(token["value"]) / 1000.0)
                        continue
                    time.sleep(0.05)
                    with self._meeting_lock(meeting_id):
                        store = self.load_store(meeting_id)
                        if not owns_terminal(store):
                            cancelled = True
                            break
                        capture_workspace(
                            store,
                            transport,
                            change_kind="typing",
                            metadata={
                                "typing_profile": profile,
                                "typing_policy_version": "1",
                                "action_id": action_id,
                                "burst_index": index,
                                "planned_delay_ms": 50,
                                "input_context": "keystrokes",
                            },
                        )
                if not cancelled:
                    with self._meeting_lock(meeting_id):
                        store = self.load_store(meeting_id)
                        if not owns_terminal(store):
                            cancelled = True
                        else:
                            capture_workspace(store, transport)
            if not cancelled:
                record("completed")
            else:
                record("cancelled", extra={"remaining_characters": 0})
        except (RunnerError, TmuxError, OSError) as exc:
            record("rejected", content=str(exc))
            raise WebError(422, str(exc)) from exc
        return store

    def complete(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status == "completed":
                return store
            if store.meeting.status in {"failed", "interrupted"}:
                raise WebError(409, "meeting cannot be completed from its current state")
            if not store.meeting.pane_id or store.meeting.session_state not in {"running", "retained"}:
                raise WebError(409, "workspace session has not started")
            store.bump_request_generation(
                status="completed",
                phase="completed",
                control_owner="none",
                control_requested=None,
                session_state="retained",
            )
            self._sync_human_pipe(store)
            self._interrupt_backend(meeting_id)
            store.append_event("state", "human", "completed", data={"status": "completed"})
            with self.lock:
                self.queued_jobs.pop(meeting_id, None)
        return store

    def stop(self, meeting_id: str) -> MeetingStore:
        # Publish the durable interrupt before contending on meeting/state
        # locks. The typing loop checks this file at every burst boundary.
        initial = self.load_store(meeting_id)
        if initial.meeting.session_state == "stopped" and initial.meeting.status == "interrupted":
            return initial
        if initial.meeting.status in {"completed", "failed"}:
            raise WebError(409, "use cleanup for a finished meeting")
        initial.request_stop()
        self._interrupt_backend(meeting_id)
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.session_state == "stopped" and store.meeting.status == "interrupted":
                return store
            if store.meeting.status in {"completed", "failed"}:
                raise WebError(409, "use cleanup for a finished meeting")
            store.bump_request_generation(
                status="interrupted",
                phase="interrupted",
                control_owner="none",
                control_requested=None,
                pending_action_id=None,
                failure="stopped by user",
            )
            self._sync_human_pipe(store)
            try:
                self._transport().kill_session(
                    store.meeting.session_name
                )
            except (TmuxError, OSError) as exc:
                store.append_event("error", "harness", "interrupted", content=str(exc))
                raise WebError(502, "meeting stopped, but tmux cleanup failed") from exc
            store.mark_session_stopped()
            store.append_event("state", "human", "interrupted", data={"status": "interrupted"})
            with self.lock:
                self.queued_jobs.pop(meeting_id, None)
        return store

    def cleanup(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status not in TERMINAL_STATUSES:
                raise WebError(409, "meeting must be finished before cleanup")
            if store.meeting.session_state == "stopped":
                return store
            try:
                self._transport().kill_session(
                    store.meeting.session_name
                )
            except (TmuxError, OSError) as exc:
                store.append_event("error", "harness", store.meeting.phase, content=str(exc))
                raise WebError(502, "tmux cleanup failed") from exc
            store.mark_session_stopped()
            self._sync_human_pipe(store)
            store.append_event("control", "human", store.meeting.phase, data={"status": "cleaned"})
        return store

    def resume(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            orphaned_agent = self._orphaned_agent(store, meeting_id)
            if store.meeting.phase not in RESUMABLE_PHASES and not orphaned_agent:
                raise WebError(409, "Agent can resume only while waiting or after an API error")
            self._require_terminal(store, "resume the agent")
            config = self._config_for_store(store)
            fresh_conversation = (
                store.meeting.phase in {"api_error", "cycle_timeout", "completion_proposed"}
                or store.meeting.recovery_required
                or orphaned_agent
            )
            store.bump_request_generation(
                status="running",
                phase="agent_thinking",
                control_owner=store.meeting.actor_name(),
                failure=None,
                cycle_action_count=0,
                cycle_started_at=utc_now(),
            )
            self._ensure_worker(
                store,
                config,
                resume=True,
                fresh_conversation=fresh_conversation,
            )
        return store

    def health(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        python_ok = sys.version_info[:2] == (3, 12)
        checks.append({"name": "python", "ok": python_ok, "detail": ".".join(map(str, sys.version_info[:3]))})
        for name in ("tmux", "vim"):
            path = shutil.which(name)
            checks.append({"name": name, "ok": path is not None, "detail": path or "not found"})
        tmux_library_version = libtmux_version()
        checks.append(
            {
                "name": "libtmux",
                "ok": tmux_library_version is not None,
                "detail": tmux_library_version or "not installed",
            }
        )
        backends: Dict[str, Dict[str, Any]] = {}
        selected_key: Optional[str] = None
        config: Optional[ModelApiConfig] = None
        try:
            config = self._apply_overrides(ModelApiConfig.load(self.model_api_path))
            if self.default_backend:
                try:
                    config = config.resolve(
                        self.default_backend,
                        self.default_provider or config.agent_provider,
                    )
                except ModelConfigError:
                    # A missing key for the CLI-selected default must not hide
                    # the per-combination rows below (e.g. codex without any
                    # stored API key).
                    pass
            selected_key = f"{config.agent_backend}/{config.agent_provider}"
            checks.append(
                {
                    "name": "model-api",
                    "ok": True,
                    "detail": f"{config.model} ({selected_key})",
                }
            )
            for (backend, provider), label in BACKEND_COMBINATIONS.items():
                key = f"{backend}/{provider}"
                auth_key_name, has_api_key = self._auth_key_status(config, provider)
                try:
                    resolved = config.resolve(backend, provider)
                    ok, detail = True, resolved.model
                    if backend == "codex-app-server":
                        ok, detail = self._probe_codex_config(resolved)
                    elif backend == "trae-agent":
                        ok, detail = self._probe_trae_config(resolved)
                    elif backend == "claude-code":
                        ok, detail = self._probe_claude_config(resolved)
                    backends[key] = {
                        "ok": ok,
                        "detail": detail,
                        "label": label,
                        "agent_backend": backend,
                        "agent_provider": provider,
                        "default_model": resolved.model,
                        "base_url": resolved.base_url,
                        "auth_key_name": auth_key_name,
                        "has_api_key": has_api_key,
                        # codex-app-server authenticates via the local CLI
                        # login and never needs a stored key.
                        "needs_api_key": not has_api_key and backend not in {"codex-app-server", "trae-agent"},
                        "required": key == selected_key,
                    }
                except (ModelConfigError, ClaudeCodeError, CodexAppServerError) as exc:
                    backends[key] = {
                        "ok": False,
                        "detail": str(exc),
                        "label": label,
                        "agent_backend": backend,
                        "agent_provider": provider,
                        "default_model": (
                            "deepseek-v4-pro[1m]" if provider == "deepseek" else None
                        ),
                        "base_url": (
                            "https://api.deepseek.com/anthropic" if provider == "deepseek" else None
                        ),
                        "auth_key_name": auth_key_name,
                        "has_api_key": has_api_key,
                        "needs_api_key": not has_api_key and backend not in {"codex-app-server", "trae-agent"},
                        "required": key == selected_key,
                    }
            selected = backends.get(selected_key)
            if selected and config.agent_backend != "responses":
                checks.append(
                    {
                        "name": config.agent_backend,
                        "ok": selected["ok"],
                        "detail": selected["detail"],
                    }
                )
        except (ModelConfigError, WebError) as exc:
            checks.append({"name": "model-api", "ok": False, "detail": str(exc)})
        core_names = {"python", "tmux", "vim", "libtmux", "model-api"}
        core_ok = all(check["ok"] for check in checks if check["name"] in core_names)
        selected_ok = bool(selected_key and backends.get(selected_key, {}).get("ok"))
        with self.lock:
            workers = list(self.jobs.values())
        return {
            "ok": core_ok and selected_ok,
            "core_ok": core_ok,
            "python": ".".join(map(str, sys.version_info[:3])),
            "checks": checks,
            "backends": backends,
            "active_jobs": sum(worker.is_alive() for worker in workers),
            "agent_backend": config.agent_backend if config is not None else None,
            "agent_provider": config.agent_provider if config is not None else None,
            "service": "running",
        }


class RemoteBuddyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], service: MeetingWebService):
        self.service = service
        super().__init__(server_address, RemoteBuddyRequestHandler)


class RemoteBuddyRequestHandler(BaseHTTPRequestHandler):
    server_version = "RemoteBuddy/0.2"

    _LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}

    @property
    def service(self) -> MeetingWebService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _guard(self) -> None:
        """Reject requests that did not originate from this loopback service.

        The loopback bind alone does not stop DNS rebinding: a page on an
        attacker domain whose DNS re-resolves to 127.0.0.1 becomes same-origin
        with this service. Pinning the Host header (and any Origin header) to
        the local addresses closes that path.
        """
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in {f"{name}:{port}" for name in self._LOOPBACK_HOSTNAMES} | self._LOOPBACK_HOSTNAMES:
            raise WebError(403, "request host is not the local workspace")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {f"http://{name}:{port}" for name in self._LOOPBACK_HOSTNAMES}:
            raise WebError(403, "cross-origin requests are not allowed")

    def _headers(self, content_type: str = "application/json; charset=utf-8", cache: str = "no-store") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # style-src needs 'unsafe-inline': xterm.js paints its DOM renderer and
        # selection overlay through runtime inline styles, which strict CSP
        # would otherwise block (breaking colors and text selection).
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _json(self, payload: Dict[str, Any], status: int = 200, etag: Optional[str] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(status)
            # ETagged endpoints are meant to be revalidated, not re-shipped;
            # no-store would forbid the browser from keeping the validator.
            self._headers(cache="no-cache" if etag else "no-store")
            self.send_header("Content-Length", str(len(body)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _not_modified_early(self, etag: str) -> bool:
        """Answer a conditional GET from stat() alone, before journal work."""
        if self.headers.get("If-None-Match") != etag:
            return False
        try:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status=status)

    def _internal_error(self, exc: Exception) -> None:
        # Exception text can carry absolute filesystem paths; keep it out of
        # the response and log it server-side instead.
        print(f"[web_v2] internal error on {self.command} {self.path}: {exc!r}", file=sys.stderr, flush=True)
        self._error(500, "internal error")

    def _read_json(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise WebError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise WebError(400, "invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise WebError(413, "request body is too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise WebError(400, "incomplete request body")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(400, "request body must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise WebError(400, "request body must be a JSON object")
        return payload

    def _read_empty_json(self) -> None:
        if self._read_json():
            raise WebError(400, "request body must be an empty JSON object")

    def _csrf(self) -> None:
        if not hmac.compare_digest(self.headers.get(CSRF_HEADER, ""), self.service.csrf_token):
            raise WebError(403, "missing or invalid CSRF token")

    def _static(self, relative: str) -> None:
        frontend_root = self.service.project_root / "frontend"
        if frontend_root.is_symlink():
            raise WebError(404, "static file not found")
        root = frontend_root.resolve()
        candidate = (root / relative).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise WebError(404, "static file not found")
        data = candidate.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:24] + '"'
        try:
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self._headers(mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _stream_terminal(self, meeting_id: str, cols: Optional[int] = None, rows: Optional[int] = None) -> None:
        """Serve the takeover byte stream as Server-Sent Events.

        The first event is ``seed`` (base64 ANSI of the current screen, from
        which only newer bytes follow), then every growth of the output pipe
        file is pushed as a ``data`` event whose ``id`` is the byte offset
        (EventSource reconnects resume from ``Last-Event-ID``). When the
        meeting leaves human control the stream sends ``control: released``
        and closes.
        """
        try:
            prepared = self.service.human_stream_start(meeting_id, cols, rows)
        except WebError as exc:
            self._error(exc.status, exc.message)
            return
        path = Path(prepared["path"])
        seed = prepared["seed"]
        seed_offset = prepared["seed_offset"]
        resume_header = self.headers.get("Last-Event-ID", "")
        resume = int(resume_header) if resume_header.isdigit() else -1
        self.send_response(200)
        self._headers("text/event-stream", cache="no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.connection.settimeout(None)
        except (OSError, AttributeError):
            pass
        wfile = self.wfile

        def emit(event: str, data: str, event_id: Optional[int] = None) -> None:
            lines = []
            if event_id is not None:
                lines.append(f"id: {event_id}")
            if event:
                lines.append(f"event: {event}")
            lines.append(f"data: {data}")
            try:
                wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
                wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise StreamClosed(str(exc)) from exc

        try:
            size_now = path.stat().st_size if path.exists() else 0
            if resume >= 0 and resume <= size_now:
                offset = resume
            else:
                # First connect (or the pipe file was rotated/reopened): send
                # the pane's authoritative size first, then a fresh seed, and
                # continue strictly after the seed.
                if resume >= 0:
                    emit("control", "reset")
                emit(
                    "size",
                    f'{prepared["pane_width"]}x{prepared["pane_height"]}',
                )
                emit("seed", seed, event_id=seed_offset)
                offset = seed_offset
            handle = path.open("rb")
            try:
                handle.seek(offset)
                heartbeat = time.monotonic()
                state_check = time.monotonic()
                while True:
                    now = time.monotonic()
                    try:
                        size = path.stat().st_size
                    except OSError:
                        break
                    if size > offset:
                        chunk = handle.read(min(1 << 18, size - offset))
                        offset += len(chunk)
                        if chunk:
                            emit("data", base64.b64encode(chunk).decode("ascii"), event_id=offset)
                        continue
                    if now - state_check > 0.5:
                        state_check = now
                        try:
                            live = self.service.meeting_live_state(meeting_id)
                        except (WebError, MeetingError, OSError):
                            break
                        if (
                            live["status"] in TERMINAL_STATUSES
                            or live["owner"] != "human"
                            or live["phase"] != "human_control"
                        ):
                            emit("control", "released")
                            break
                    if now - heartbeat > 12:
                        heartbeat = now
                        wfile.write(b": heartbeat\n\n")
                        wfile.flush()
                    time.sleep(0.02)
            finally:
                handle.close()
        except StreamClosed:
            pass
        finally:
            # The stream only lives while the human owns the terminal; when it
            # ends (release/stop/chats), make sure the pipe is closed too.
            self.service.close_human_pipe(meeting_id)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._guard()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/":
                self._static("workspace.html")
                return
            if path.startswith("/assets/"):
                relative = unquote(path[len("/assets/"):])
                parts = relative.split("/")
                if any(part in {"", ".", ".."} for part in parts):
                    raise WebError(404, "static file not found")
                self._static(relative)
                return
            if path == "/api/session":
                self._json({"csrf_token": self.service.csrf_token, "csrf_header": CSRF_HEADER})
                return
            if path == "/api/settings":
                self._json(self.service.settings())
                return
            if path == "/api/credentials":
                self._json(self.service.credentials_status())
                return
            if path == "/api/health":
                self._json(self.service.health())
                return
            if path == "/api/meetings":
                self._json(self.service.list_meetings())
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/snapshot", path)
            if match:
                meeting_id = unquote(match.group(1))
                if self._not_modified_early('"' + self.service.revision(meeting_id) + '"'):
                    return
                payload = self.service.snapshot(meeting_id)
                self._json(payload, etag='"' + payload["revision"] + '"')
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/events", path)
            if match:
                meeting_id = unquote(match.group(1))
                query = parse_qs(parsed.query, keep_blank_values=False)
                after = query.get("after", ["0"])[-1]
                early_etag = '"' + sha256_text(self.service.revision(meeting_id) + str(after))[:24] + '"'
                if self._not_modified_early(early_etag):
                    return
                payload = self.service.events(meeting_id, after)
                etag = '"' + sha256_text(payload["revision"] + str(after))[:24] + '"'
                self._json(payload, etag=etag)
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/tickets", path)
            if match:
                self._json(self.service.tickets(unquote(match.group(1))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/terminal/stream", path)
            if match:
                query = parse_qs(parsed.query, keep_blank_values=False)

                def grid_value(name: str) -> Optional[int]:
                    values = query.get(name)
                    if not values:
                        return None
                    if not values[-1].isdigit():
                        raise WebError(400, f"{name} must be an integer")
                    return int(values[-1])

                # SSE errors are handled inside _stream_terminal (client
                # aborts, release); never write a second response after the
                # stream headers are already on the wire.
                try:
                    self._stream_terminal(
                        unquote(match.group(1)),
                        grid_value("cols"),
                        grid_value("rows"),
                    )
                except Exception:
                    pass
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, ValueError) as exc:
            self._internal_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._guard()
            self._csrf()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/meetings":
                store = self.service.start(self._read_json())
                self._json({"meeting_id": store.meeting.meeting_id, "meeting": _public_manifest(store)}, status=201)
                return
            if path == "/api/settings":
                self._json(self.service.update_settings(self._read_json()))
                return
            if path == "/api/credentials":
                self._json(self.service.update_credentials(self._read_json()))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/sdk/([A-Za-z_][A-Za-z0-9_]*)", path)
            if match:
                payload = self._read_json() or {}
                self._json(self.service.sdk_call(unquote(match.group(1)), match.group(2),
                                                 payload.get("args") or {}))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/scores", path)
            if match:
                meeting_id = unquote(match.group(1))
                event = self.service.score(meeting_id, self._read_json())
                self._json({"event": event, "meeting": _public_manifest(self.service.load_store(meeting_id))})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/messages", path)
            if match:
                store = self.service.message(unquote(match.group(1)), self._read_json())
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/effort", path)
            if match:
                store = self.service.set_reasoning_effort(unquote(match.group(1)), self._read_json())
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/terminal/input", path)
            if match:
                store = self.service.terminal_input(unquote(match.group(1)), self._read_json())
                self._json({"meeting": _public_manifest(store)})
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/tickets/([^/]+)", path)
            if match:
                self._read_json()
                self._json(self.service.issue_ticket(unquote(match.group(1)),
                                                     unquote(match.group(2))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/(control/acquire|control/release|agent/resume|complete|stop|cleanup)", path)
            if match:
                self._read_empty_json()
                action = _MEETING_POST_ACTIONS[match.group(2)]
                store = action(self.service, unquote(match.group(1)))
                self._json({"meeting": _public_manifest(store)})
                return
            raise WebError(404, "route not found")
        except WebError as exc:
            self._error(exc.status, exc.message)
        except (OSError, MeetingError, TmuxError, ModelApiError, ValueError) as exc:
            self._internal_error(exc)


# Empty-body POST actions on one meeting. The URL surface is defined by this
# explicit table rather than by whatever method names exist on the service.
_MEETING_POST_ACTIONS: Dict[str, Callable[[MeetingWebService, str], MeetingStore]] = {
    "control/acquire": MeetingWebService.acquire,
    "control/release": MeetingWebService.release,
    "agent/resume": MeetingWebService.resume,
    "complete": MeetingWebService.complete,
    "stop": MeetingWebService.stop,
    "cleanup": MeetingWebService.cleanup,
}


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, service: Optional[MeetingWebService] = None) -> RemoteBuddyHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("RemoteBuddy web server only listens on loopback")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    server = RemoteBuddyHTTPServer((host, port), service or MeetingWebService())
    # The member processes talk to the meeting over this service's own HTTP face.
    server.service.public_url = f"http://{host}:{server.server_address[1]}"
    return server


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the RemoteBuddy v2 workspace service")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="工作区（一个目录）：会议的每一份数据放在 <DIR>/.meeting/ 下；"
                             "不给就用项目根")
    parser.add_argument(
        "--harness",
        default=None,
        help="Default harness for new sessions: claude, responses, or codex",
    )
    args = parser.parse_args(argv)

    default_backend: Optional[str] = None
    default_provider: Optional[str] = None
    if args.harness:
        aliases = {
            "claude": ("claude-code", "deepseek"),
            "claude-code": ("claude-code", "deepseek"),
            "responses": ("responses", "openai"),
            "codex": ("codex-app-server", "openai"),
            "codex-app-server": ("codex-app-server", "openai"),
            "trae": ("trae-agent", "openai"),
            "trae-agent": ("trae-agent", "openai"),
        }
        pair = aliases.get(args.harness)
        if pair is None:
            parser.error("--harness must be one of: claude, responses, codex, trae")
        default_backend, default_provider = pair

    if args.workspace and not Path(args.workspace).expanduser().is_dir():
        parser.error(f"--workspace 必须是存在的目录：{args.workspace}")

    service = MeetingWebService(
        workspace=Path(args.workspace) if args.workspace else None,
        default_backend=default_backend,
        default_provider=default_provider,
    )
    server = build_server(args.host, args.port, service=service)
    print(f"RemoteBuddy workspace: http://{args.host}:{args.port}/", flush=True)
    print(f"工作区：{service.workspace}（会议数据在 {service.workspace / MeetingStore.WORKSPACE_DIR}/）",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
