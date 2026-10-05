#!/usr/bin/env python3
"""RemoteBuddy v2 local workspace service."""
from __future__ import annotations

import base64
import contextlib
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
from acp_client import AcpBackend
from interactive_runner import (
    InteractiveRunner,
    RunnerError,
    capture_workspace,
)
from meeting import (
    MAX_ATTACHMENT_BYTES,
    MAX_CHAT_REFERENCES,
    MAX_MESSAGE_BYTES,
    MEETING_ID_RE,
    SCHEMA_VERSION,
    VALID_REASONING_EFFORTS,
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
from ordered_meeting.actor import actor_service, live_service, writer_from
from ordered_meeting.critic import critic_service
from ordered_meeting.live import MeetingChannel
from ordered_meeting.meeting import case_for, run_for_meeting, target_for


PROJECT_ROOT = Path(__file__).resolve().parent
# 一块工作区 = 一个目录。会议的每一份数据都放在它的 `.meeting/<会议号>/` 下面；
# 服务默认就用项目根，`--workspace DIR` 换一个。
WORKSPACE_INDEX = "workspaces"
WORKSPACE_NAMES = "workspace_names"
# 拖出来的顺序：工作区那份放在默认工作区的 `.meeting/` 里，一行一个目录；会话那份放在
# 它自己工作区的 `.meeting/` 里，一行一个会议号（见 order_workspaces / order_meetings）。
WORKSPACE_ORDER = "workspace_order"
MEETING_ORDER = "order"
# 同一场会同时只能有一个服务在驱。声明写在这场会自己的目录里（谁在跑就写谁 + 心跳），
# 因为两个服务共享的正是这个目录；数据共享、驱动不共享。
DRIVER_STATE = "driver.json"
DRIVER_HEARTBEAT_SECONDS = 20.0
# 系统选目录框是人在那儿点，不是程序在算：给足时间，超了就当没选。
CHOOSE_DIRECTORY_TIMEOUT_S = 300.0
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
BACKEND_COMBINATIONS = {
    ("responses", "openai"): "Responses API",
    ("codex-app-server", "openai"): "Codex App Server",
    ("trae-agent", "openai"): "Trae Agent",
    ("trae-acp", "openai"): "Trae ACP",
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


def _applescript_string(value: str) -> str:
    """把一段文本变成 AppleScript 的字符串字面量。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def meeting_title(store: MeetingStore) -> str:
    """A meeting's one-line title: the name it was given, else the human's first message.

    `conversation.md` starts with the first message, so the list does not parse
    a journal for every meeting just to name it.
    """
    name = (store.meeting.name or "").strip()
    if name:
        return name
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
    """工单跑在哪个案子下：值都在案子自己的数据路径下，看 store 里的头两段。"""
    cases: List[str] = []
    for data_path, _name in meeting.store.stored():
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
    # `read` 交回的就是存下来的那个值：叶子是标量。一版代码那种长值不往页面上搬，
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
        orders: bool = False,
        orders_caps_factory: Optional[Callable[..., Dict[str, Callable]]] = None,
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
        # 工单那一侧（ordered_meeting）：消息先进 assistant，再一张一张发单。
        # 默认关着：这一侧还新，开着就是"这场会的活由工单驱动"。
        self.orders = orders
        self.orders_caps_factory = orders_caps_factory
        self.orders_jobs: Dict[str, threading.Thread] = {}
        self.orders_talking: Dict[str, int] = {}
        self.lock = threading.RLock()
        self.jobs: Dict[str, threading.Thread] = {}
        # (store, config, resume, fresh_conversation, terminal_only)
        self.queued_jobs: Dict[str, Tuple[MeetingStore, ModelApiConfig, bool, bool, bool]] = {}
        self.meeting_locks: Dict[str, threading.RLock] = {}
        self.capture_jobs: Dict[str, threading.Thread] = {}
        self.capture_stops: Dict[str, threading.Event] = {}
        self.snapshot_capture_at: Dict[str, float] = {}
        self.active_backends: Dict[str, AgentBackend] = {}
        # 正在跑的 runner（一场会同时最多一个）：页面上"更新 / 重启"两个按钮
        # 要能戳到它，见 reload_skill / restart_agent。
        self.active_runners: Dict[str, Any] = {}
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
        """An agent phase with no worker and no live driver lease behind it."""
        driver = self._driver_state(store)
        if driver and self._driver_alive(driver):
            # Another service may be driving this meeting.  Its worker is not
            # visible in this process, but the live lease proves the Agent is
            # not orphaned.
            return False
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

    def _ensure_terminal(self, store: MeetingStore) -> None:
        """要用终端之前先保证它在；没有就在同一个目录上重开一个。

        tmux 会话没了（tmux 服务被杀、机器重启、清理时误伤）不是这场会的结局：那条路径
        还在，就在那儿再开一个同名会话，人就又能接管、agent 又能接着跑。开在哪儿按原来的
        位置来：终端当时在哪个目录（`current_path`）就回哪个目录，那个目录不在了才回工作区根。
        两处都不在了（工作区被删/搬走）才拦下来。
        """
        if self._terminal_alive(store):
            return
        workspace = store.workspace_path
        home: Optional[Path] = None
        for candidate in (store.meeting.current_path, str(workspace)):
            text = str(candidate or "").strip()
            if not text:
                continue
            path = Path(text)
            if path.is_dir():
                home = path
                break
        if home is None:
            raise WebError(
                409, "the workspace directory is gone: this meeting cannot get a terminal"
            )
        transport = self._transport()
        # 会话名还在、只是里面的 pane 死了：先把这个空壳收掉，再开新的。
        try:
            if transport.session_exists(store.meeting.session_name):
                transport.kill_session(store.meeting.session_name)
        except (TmuxError, OSError):
            pass
        pane_id = transport.create_workspace_session(store.meeting.session_name, home)
        store.set_session(pane_id)
        capture_workspace(store, transport)
        store.append_event(
            "state",
            "harness",
            store.meeting.phase,
            content="终端会话已重开（原来的没了）。",
            data={"status": "terminal_reopened", "pane_id": pane_id, "path": str(home)},
        )

    def _make_backend(self, config: ModelApiConfig) -> AgentBackend:
        if self.client_factory is not None:
            return self.client_factory(config)
        if config.agent_backend == "codex-app-server":
            return CodexAppServerBackend(config)
        if config.agent_backend == "trae-agent":
            return TraeAgentBackend(config)
        if config.agent_backend == "trae-acp":
            return AcpBackend(config)
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

    def resize_terminal(self, meeting_id: str, cols: int, rows: int, diagnostics: Optional[Mapping[str, Any]] = None) -> Dict[str, int]:
        if diagnostics:
            print(f"terminal resize diagnostics meeting={meeting_id} {dict(diagnostics)}", file=sys.stderr, flush=True)
        if not isinstance(cols, int) or not isinstance(rows, int):
            raise WebError(400, "terminal resize size must be integers")
        if cols < 40 or cols > 300 or rows < 12 or rows > 120:
            raise WebError(400, "terminal resize size is outside supported bounds")
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            if not store.meeting.pane_id:
                raise WebError(409, "workspace pane is not available")
            transport = self._transport()
            try:
                info = transport.pane_info(store.meeting.pane_id)
                current_cols = int(info["pane_width"])
                current_rows = int(info["pane_height"])
                if current_cols != cols or current_rows != rows:
                    transport.resize_workspace_session(store.meeting.session_name, cols, rows)
                info = transport.pane_info(store.meeting.pane_id)
            except (TmuxError, OSError, ValueError) as exc:
                raise WebError(502, f"cannot resize workspace: {exc}") from exc
            return {"pane_width": int(info["pane_width"]), "pane_height": int(info["pane_height"])}

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

    def _meeting_dir(self, meeting_id: str) -> Optional[Path]:
        """这场会的数据目录。

        同一个会议号可能出现在两个工作区里（有人把工作区连 `.meeting/` 一起拷了一份）。
        真正属于某个工作区的那一份，manifest 里写的 `workspace_root` 就是它所在的目录；
        拷贝出来的那一份写的是原来那个目录。所以先认这一份，认不出来才用先碰到的。
        """
        fallback: Optional[Path] = None
        for directory in self._meeting_dirs():
            if directory.name != meeting_id:
                continue
            if fallback is None:
                fallback = directory
            manifest_path = directory / "meeting.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(manifest.get("workspace_root") or "") == str(directory.parent.parent):
                return directory
        return fallback

    def _open_store(self, meeting_id: str) -> MeetingStore:
        """这场会在哪个工作区的 `.meeting/` 下，就按那条数据路径读回来。"""
        directory = self._meeting_dir(meeting_id)
        if directory is None:
            raise MeetingError(f"meeting not found: {meeting_id}")
        return MeetingStore.load_at(directory, meeting_id)

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
        """默认那一个，加上以前用过、现在还在的那些目录；人拖过序就按他拖的来。"""
        found = [self.workspace]
        try:
            lines = self._workspace_index().read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            path = Path(line.strip()).expanduser()
            if line.strip() and path.is_dir() and path not in found:
                found.append(path.resolve())
        order = self._workspace_order()
        if not order:
            return found
        ordered = [path for path in order if path in found]
        return ordered + [path for path in found if path not in ordered]

    def _workspace_order_index(self) -> Path:
        """人拖出来的工作区顺序：默认工作区里的一行行目录。"""
        return self.workspace / MeetingStore.WORKSPACE_DIR / WORKSPACE_ORDER

    def _workspace_order(self) -> List[Path]:
        try:
            lines = self._workspace_order_index().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        found: List[Path] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            path = Path(text).expanduser()
            if path.is_dir() and path not in found:
                found.append(path.resolve())
        return found

    def _meeting_order_index(self, workspace: Path) -> Path:
        """那个工作区里人拖出来的会话顺序：`.meeting/order`，一行一个会议号。"""
        return workspace / MeetingStore.WORKSPACE_DIR / MEETING_ORDER

    def _meeting_order(self, workspace: Path) -> List[str]:
        try:
            lines = self._meeting_order_index(workspace).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [line.strip() for line in lines if line.strip()]

    def _listed_workspaces(self) -> List[Path]:
        """名单上明着记着的目录：索引里的一行行，目录还在的那些。

        默认那一个（项目根）默认不在名单上——它有没有会议都在服务里；只有人自己按「＋」
        加过它，它才进这份名单。
        """
        found: List[Path] = []
        try:
            lines = self._workspace_index().read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            if not line.strip():
                continue
            path = Path(line.strip()).expanduser()
            if path.is_dir() and path.resolve() not in found:
                found.append(path.resolve())
        return found

    def _remember_workspace(self, workspace: Path) -> bool:
        """把一个目录记进名单；已经在名单上就不重复记。

        开会时开在一个新目录里也走这里：不然重启之后它的会议就从列表里消失了。
        """
        if workspace in self._listed_workspaces():
            return False
        index = self._workspace_index()
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open("a", encoding="utf-8") as handle:
            handle.write(f"{workspace}\n")
        return True

    def _forget_workspace(self, workspace: Path) -> None:
        """索引里那一行去掉，别的行原样留着；名字也一起擦掉。"""
        index = self._workspace_index()
        try:
            lines = index.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        keep = [line.strip() for line in lines
                if line.strip() and Path(line.strip()).expanduser().resolve() != workspace]
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")
        self._set_workspace_name(workspace, "")

    def _workspace_name_index(self) -> Path:
        """显示名单独记一份：一行一个"路径 + 制表符 + 名字"。

        不塞进 `workspaces` 那份索引里，是因为索引的每一行就是一个目录，改名字不该动它；
        老的那份文件也不用跟着迁移。
        """
        return self.workspace / MeetingStore.WORKSPACE_DIR / WORKSPACE_NAMES

    def _workspace_names(self) -> Dict[str, str]:
        """目录 -> 显示名；没起过名字的目录不在这张表里。"""
        found: Dict[str, str] = {}
        try:
            lines = self._workspace_name_index().read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            path, _tab, title = line.partition("\t")
            if path.strip() and title.strip():
                found[str(Path(path.strip()).expanduser().resolve())] = title.strip()
        return found

    def _set_workspace_name(self, workspace: Path, title: str) -> None:
        """起名或擦名：写进那一份名字表。"""
        names = self._workspace_names()
        if title:
            names[str(workspace)] = title
        else:
            names.pop(str(workspace), None)
        index = self._workspace_name_index()
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("".join(f"{path}\t{name}\n" for path, name in sorted(names.items())),
                         encoding="utf-8")

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
        # 会议号是这层接口里唯一的名字（每条路由都只有 id），所以列表里一个号只能出现一次。
        # 同一个号被拷到两个工作区时留真正属于那个目录的一份（判据见 _meeting_dir）。
        chosen: Dict[str, Tuple[Dict[str, Any], bool]] = {}
        for directory in self._meeting_dirs():
            if not MEETING_ID_RE.fullmatch(directory.name):
                continue
            manifest = directory / "meeting.json"
            if MeetingStore.peek_schema(manifest) != SCHEMA_VERSION:
                continue
            try:
                store = MeetingStore.load_at(directory, directory.name)
                summary = _public_manifest(store)
            except (MeetingError, OSError, ValueError) as exc:
                warnings.append(f"{directory.name}: {exc}")
                continue
            at_home = str(summary.get("workspace_root") or "") == str(directory.parent.parent)
            existing = chosen.get(summary["meeting_id"])
            if existing is None or (at_home and not existing[1]):
                chosen[summary["meeting_id"]] = (summary, at_home)
        summaries = [item[0] for item in chosen.values()]
        # 顺序：先按工作区（人在侧边栏拖出来的那个次序），工作区里面按人拖出来的会话次序；
        # 没拖过序的会话排在最上面（新的在上），跟没拖过时一样。这样页面只是照着这份顺序画。
        workspaces = self._known_workspaces()
        workspace_rank = {str(path): index for index, path in enumerate(workspaces)}
        meeting_rank: Dict[Tuple[str, str], int] = {}
        for path in workspaces:
            for index, meeting_id in enumerate(self._meeting_order(path)):
                meeting_rank[(str(path), meeting_id)] = index
        # 两次稳定排序：先按时间（新的在前），再按（工作区次序，会话次序）——
        # 没拖过序的那些（次序为 -1）就保留时间那一遍的结果。
        summaries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        summaries.sort(key=lambda item: (
            workspace_rank.get(str(item.get("workspace_root") or ""), len(workspaces)),
            meeting_rank.get(
                (str(item.get("workspace_root") or ""), str(item.get("meeting_id") or "")), -1
            ),
        ))
        return {"meetings": summaries, "warnings": warnings,
                **self._workspaces_payload(self.workspace)}

    def add_workspace(self, payload: Any = None) -> Dict[str, Any]:
        """把一个目录记成工作区，不开会。

        侧边栏「工作区」旁边那个「＋」就是这个：工作区可以先有，会议以后再开。目录必须已经
        存在（工作区是已有的项目，不是随手新造一个空目录）；记在默认工作区的
        `.meeting/workspaces` 里，所以重启之后它还在列表上。留空就是默认那一个，等于没改。
        """
        payload = payload if isinstance(payload, dict) else {}
        asked = payload.get("directory")
        workspace = _as_workspace(asked, self.workspace)
        # 留空是"没改"，不是"把默认那个加进名单"；给出具体目录才算加。
        added = self._remember_workspace(workspace) if str(asked or "").strip() else False
        return {"added": added, **self._workspaces_payload(workspace)}

    def rename_workspace(self, payload: Any = None) -> Dict[str, Any]:
        """给一个工作区换个显示名。

        名字只是列表里怎么叫它：磁盘上的目录不动，会议一条也不挪。名字留空就擦掉，
        回到目录自己的 basename。
        """
        payload = payload if isinstance(payload, dict) else {}
        workspace = self._asked_workspace(payload)
        self._set_workspace_name(workspace, str(payload.get("title") or "").strip())
        return self._workspaces_payload(workspace)

    def delete_workspace(self, payload: Any = None) -> Dict[str, Any]:
        """把一个工作区从列表里去掉。

        去掉的只是"记着它"这一行：目录本身、它下面的会议，一条都不删——想找回来，把目录
        再加一次就回来了。默认那一个（项目根）不在名单上，去不掉；还有没结束的会的工作区
        也不去，免得那一场从列表上消失、却还在后台跑着。
        """
        payload = payload if isinstance(payload, dict) else {}
        workspace = self._asked_workspace(payload)
        for summary in self.list_meetings()["meetings"]:
            if summary.get("workspace_root") == str(workspace) and \
                    summary.get("status") not in TERMINAL_STATUSES:
                raise WebError(409, f"这个工作区里 {summary['meeting_id']} 还没结束：先结束它")
        self._forget_workspace(workspace)
        return self._workspaces_payload(self.workspace)

    def _asked_workspace(self, payload: Dict[str, Any]) -> Path:
        """payload 里那个目录：必须是已经记着的工作区。

        两个字段名都收：老的那几处写 `directory`，拖动排序写 `workspace`。
        """
        asked = payload.get("directory") or payload.get("workspace")
        workspace = _as_workspace(asked, self.workspace)
        if workspace not in self._known_workspaces():
            raise WebError(400, f"不是已记着的工作区：{workspace}")
        return workspace

    def _workspaces_payload(self, workspace: Path) -> Dict[str, Any]:
        """改名、添加、删除都回同一份名单，页面只认这一份。

        `workspaces` 是服务会去找会议的那些目录（默认那个永远在最前）；`listed` 是人自己
        加进名单的那些——默认那个只有在名单上时才算"要显示"。
        """
        return {"workspace": str(workspace),
                "workspaces": [str(one) for one in self._known_workspaces()],
                "listed": [str(one) for one in self._listed_workspaces()],
                "workspace_titles": self._workspace_names()}

    def order_workspaces(self, payload: Any = None) -> Dict[str, Any]:
        """人把工作区拖出来的次序记下来（默认工作区的 `.meeting/workspace_order`）。

        只认已经记着的目录；名单上没点到的那些按原来的次序补在后面，所以一次拖动不会让
        谁从列表里消失（新加的工作区也一样）。
        """
        payload = payload if isinstance(payload, dict) else {}
        asked = payload.get("workspaces")
        if not isinstance(asked, list) or not all(isinstance(one, str) for one in asked):
            raise WebError(400, "workspaces must be an array of directories")
        known = self._known_workspaces()
        ordered: List[Path] = []
        for text in asked:
            path = Path(text).expanduser()
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved in known and resolved not in ordered:
                ordered.append(resolved)
        ordered.extend(path for path in known if path not in ordered)
        index = self._workspace_order_index()
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("".join(f"{path}\n" for path in ordered), encoding="utf-8")
        return self._workspaces_payload(self.workspace)

    def order_meetings(self, payload: Any = None) -> Dict[str, Any]:
        """人把一个工作区里的会话拖出来的次序记下来（那个工作区的 `.meeting/order`）。

        只认这个工作区里真有的会议号；没点到的（比如拖动之后新开的会）留着不写，
        列表里它们照旧按时间排在最上面。
        """
        payload = payload if isinstance(payload, dict) else {}
        workspace = self._asked_workspace(payload)
        asked = payload.get("meetings")
        if not isinstance(asked, list) or not all(isinstance(one, str) for one in asked):
            raise WebError(400, "meetings must be an array of meeting ids")
        folder = workspace / MeetingStore.WORKSPACE_DIR
        ordered: List[str] = []
        for meeting_id in asked:
            if not MEETING_ID_RE.fullmatch(meeting_id) or meeting_id in ordered:
                continue
            if (folder / meeting_id).is_dir():
                ordered.append(meeting_id)
        index = self._meeting_order_index(workspace)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("".join(f"{meeting_id}\n" for meeting_id in ordered), encoding="utf-8")
        return {"workspace": str(workspace), "meetings": ordered,
                **self._workspaces_payload(self.workspace)}

    def _open_terminal_if_needed(self, store: MeetingStore) -> None:
        """只开了工作区、终端还没开出来的会：被看上一眼就把终端补上。

        页面上"打开工作区"本来就会开终端；这是给更早建的空会话兜底——否则页面会
        一直停在「等待终端画面…」，等的是一帧永远不会有的画面。
        """
        if store.meeting.phase != "created" or store.meeting.pane_id:
            return
        if store.meeting.status in TERMINAL_STATUSES:
            return
        meeting_id = store.meeting.meeting_id
        with self.lock:
            worker = self.jobs.get(meeting_id)
            if worker and worker.is_alive():
                return
        try:
            config = self._config_for_store(store)
        except (WebError, ModelConfigError, OSError):
            return
        self._ensure_worker(store, config, resume=False, fresh_conversation=False,
                            terminal_only=True)

    def snapshot(self, meeting_id: str) -> Dict[str, Any]:
        store = self.load_store(meeting_id)
        self._open_terminal_if_needed(store)
        if store.meeting.pane_id and store.meeting.control_owner != "human":
            now = time.monotonic()
            with self.lock:
                last_capture = self.snapshot_capture_at.get(meeting_id, 0.0)
                should_capture = now - last_capture >= 0.5
                if should_capture:
                    self.snapshot_capture_at[meeting_id] = now
            if should_capture:
                try:
                    capture_workspace(store, self._transport())
                except (RunnerError, TmuxError, OSError):
                    pass
        if store.meeting.control_owner == "human":
            self._sync_human_pipe(store)
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

        页面按一下按钮就是这一下。跑在后台，页面照旧轮询工单板：答下的值成员自己抄回会议。
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

    def choose_directory(self, payload: Any = None) -> Dict[str, Any]:
        """弹系统（Finder）的选目录框，把选中的目录回给页面。

        浏览器自己的 `showDirectoryPicker` 只给得到目录名、拿不到绝对路径（那是它的
        安全设计），所以这一步必须由服务端问系统。取消不是错：回一个空目录，页面
        什么都不改。
        """
        if sys.platform != "darwin":
            raise WebError(400, "只有 macOS 能弹系统选目录框，请直接填路径")
        payload = payload if isinstance(payload, dict) else {}
        asked = payload.get("directory")
        script = 'tell application "Finder" to activate\n'
        script += 'POSIX path of (choose folder with prompt "选这场会的工作目录"'
        if isinstance(asked, str) and asked.strip():
            candidate = Path(asked.strip()).expanduser()
            if candidate.is_dir():
                script += f" default location (POSIX file {_applescript_string(str(candidate.resolve()))})"
        script += ")"
        try:
            done = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True, text=True, timeout=CHOOSE_DIRECTORY_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise WebError(504, "选目录框等太久了")
        except FileNotFoundError:
            raise WebError(400, "这台机器上没有 /usr/bin/osascript")
        if done.returncode != 0:
            return {"directory": ""}
        # `POSIX path of` a folder alias keeps a trailing slash.
        return {"directory": done.stdout.strip().rstrip("/") or "/"}

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
        # tail sequence without building the whole journal in memory.
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
                if backend not in {"codex-app-server", "trae-agent", "trae-acp"}:
                    raise WebError(
                        400,
                        "empty model is only supported for codex-app-server, trae-agent or trae-acp",
                    )
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
            or (model == "" and config.agent_backend in {"codex-app-server", "trae-agent", "trae-acp"})
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
        if self.orders and message.strip():
            # 工单那一侧：第一句话也先进 assistant，由它发单。
            self._start_orders(store, meeting_id, message)
            return store
        # Without a first message the agent has nothing to answer yet, but the
        # workspace itself is opened right away: its terminal is the thing the
        # human is looking at while they type the first line.
        self._ensure_worker(
            store,
            config,
            resume=False,
            fresh_conversation=False,
            terminal_only=not message.strip(),
        )
        return store

    # ------------------------------------------------------------------
    # 谁在驱这场会：两个服务共享同一个工作区时，只有一个能动
    # ------------------------------------------------------------------

    def _driver_path(self, store: MeetingStore) -> Path:
        return Path(store.run_dir) / DRIVER_STATE

    def _driver_state(self, store: MeetingStore) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(self._driver_path(store).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _process_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _driver_alive(self, state: Dict[str, Any]) -> bool:
        """这份声明还算数吗：写它的进程活着，而且心跳还新。"""
        if not self._process_alive(state.get("pid")):
            return False
        beat = state.get("heartbeat")
        if not isinstance(beat, (int, float)):
            return False
        return (time.time() - float(beat)) <= DRIVER_HEARTBEAT_SECONDS * 3

    def _write_driver(self, store: MeetingStore, state: Dict[str, Any]) -> None:
        path = self._driver_path(store)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _claim_driver(self, store: MeetingStore) -> None:
        self._write_driver(store, {
            "pid": os.getpid(),
            "url": self.public_url or "",
            "workspace": str(self.workspace),
            "meeting_id": store.meeting.meeting_id,
            "heartbeat": time.time(),
        })

    def _touch_driver(self, store: MeetingStore) -> None:
        state = self._driver_state(store)
        if state and state.get("pid") == os.getpid():
            state["heartbeat"] = time.time()
            self._write_driver(store, state)

    def _release_driver(self, store: MeetingStore) -> None:
        state = self._driver_state(store)
        if state and state.get("pid") == os.getpid():
            try:
                self._driver_path(store).unlink(missing_ok=True)
            except OSError:
                pass

    def driver_url(self, meeting_id: str) -> str:
        """这场会现在归哪个服务驱；没人驱（或声明过期）就是空串。"""
        try:
            store = self.load_store(meeting_id)
        except WebError:
            return ""
        state = self._driver_state(store)
        if not state or not self._driver_alive(state):
            return ""
        if state.get("pid") == os.getpid():
            return ""
        return str(state.get("url") or "")

    def _ensure_driver(self, store: MeetingStore) -> None:
        """要驱这场会之前先拿声明；别的服务握着就明确拒绝，并报出它的地址。

        两个服务共享同一个工作区（同一份会议目录、同一个 tmux 会话名）时，读是天然一致的，
        但写只能有一个：否则两个模型会各起一个 CLI、往同一个 pane 里各打各的。
        """
        state = self._driver_state(store)
        if state and state.get("pid") != os.getpid() and self._driver_alive(state):
            url = str(state.get("url") or "")
            where = f"（它在 {url}）" if url else ""
            raise WebError(409, f"这场会正由另一个服务驱动{where}：请到那边操作，或先停掉它")
        self._claim_driver(store)

    def _ensure_worker(
        self,
        store: MeetingStore,
        config: ModelApiConfig,
        *,
        resume: bool,
        fresh_conversation: bool,
        terminal_only: bool = False,
    ) -> None:
        meeting_id = store.meeting.meeting_id
        self._ensure_driver(store)
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
                    terminal_only = terminal_only and previous[4]
                self.queued_jobs[meeting_id] = (store, config, resume, fresh_conversation, terminal_only)
                return
            client = self._make_backend(config)
            transport = self._transport()
            meeting_lock = self._meeting_lock(meeting_id)
            worker = threading.Thread(
                target=self._worker,
                args=(store, client, transport, meeting_lock, resume, fresh_conversation, terminal_only),
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
        terminal_only: bool = False,
    ) -> None:
        pending: Optional[Tuple[MeetingStore, ModelApiConfig, bool, bool, bool]] = None
        runner: Optional[Any] = None
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._driver_heartbeat,
            args=(store, heartbeat_stop),
            name=f"remotebuddy-driver-{store.meeting.meeting_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            runner = self.runner_factory(store, transport, client, lock=meeting_lock)
            with self.lock:
                self.active_runners[store.meeting.meeting_id] = runner
            if terminal_only:
                # 只看工作区：把终端开出来，不叫 agent（第一句话在聊天框里说）。
                runner.open()
            elif resume:
                runner.resume(fresh_conversation=fresh_conversation)
            else:
                runner.start()
        finally:
            heartbeat_stop.set()
            self._release_driver(store)
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
                if self.active_runners.get(meeting_id) is runner:
                    self.active_runners.pop(meeting_id, None)
                if self.jobs.get(meeting_id) is threading.current_thread():
                    self.jobs.pop(meeting_id, None)
                pending = self.queued_jobs.pop(meeting_id, None)
            if pending:
                next_store, next_config, next_resume, next_fresh, next_terminal = pending
                self._ensure_worker(
                    next_store,
                    next_config,
                    resume=next_resume,
                    fresh_conversation=next_fresh,
                    terminal_only=next_terminal,
                )

    def _driver_heartbeat(self, store: MeetingStore, stop: threading.Event) -> None:
        """驱动期间每几秒把心跳顶一下：进程卡死/被杀，声明就会过期，另一个服务能接手。"""
        while not stop.wait(DRIVER_HEARTBEAT_SECONDS / 2):
            self._touch_driver(store)

    def _ensure_capture_worker(self, store: MeetingStore) -> None:
        meeting_id = store.meeting.meeting_id
        with self.lock:
            current = self.capture_jobs.get(meeting_id)
            if current and current.is_alive():
                return
            stop = threading.Event()
            worker = threading.Thread(
                target=self._capture_human_control,
                args=(meeting_id, stop),
                name=f"remotebuddy-capture-{meeting_id}",
                daemon=True,
            )
            self.capture_jobs[meeting_id] = worker
            self.capture_stops[meeting_id] = stop
            worker.start()

    def _capture_human_control(self, meeting_id: str, stop: Optional[threading.Event] = None) -> None:
        transport = self._transport()
        try:
            stop = stop or threading.Event()
            while not stop.is_set():
                if stop.wait(0.25):
                    return
                with self._meeting_lock(meeting_id):
                    store = self.load_store(meeting_id)
                    if store.meeting.status in TERMINAL_STATUSES:
                        return
                    if store.meeting.control_owner == "human":
                        self._touch_driver(store)
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
                    elif store.meeting.control_owner != "human":
                        return
                time.sleep(0.25)
        except (WebError, MeetingError, OSError):
            return
        finally:
            with self.lock:
                if self.capture_jobs.get(meeting_id) is threading.current_thread():
                    self.capture_jobs.pop(meeting_id, None)
                    self.capture_stops.pop(meeting_id, None)

    def shutdown(self) -> None:
        with self.lock:
            stops = list(self.capture_stops.values())
            workers = list(self.capture_jobs.values())
            self.capture_stops.clear()
        for stop in stops:
            stop.set()
        current = threading.current_thread()
        for worker in workers:
            if worker is not current:
                worker.join(timeout=5)

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

    def save_attachment(self, meeting_id: str, media_type: str, payload: bytes) -> Dict[str, Any]:
        """人类交进来的一张图：存成这场会的一份文件，回它的名字。"""
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            try:
                return store.save_attachment(media_type, payload)
            except MeetingError as exc:
                raise WebError(400, str(exc)) from exc

    def attachment(self, meeting_id: str, name: str) -> Dict[str, Any]:
        """把这场会的一张图取回来（页面里的 <img> 走这条）。"""
        store = self.load_store(meeting_id)
        try:
            path = store.attachment_path(name)
        except MeetingError as exc:
            raise WebError(404, "attachment not found") from exc
        if not path.is_file() or path.is_symlink():
            raise WebError(404, "attachment not found")
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return {"path": path, "media_type": media_type, "bytes": path.read_bytes()}

    def _message_attachments(self, store: MeetingStore, raw: Any) -> List[Dict[str, Any]]:
        """消息带的那几张图：必须是这场会里已经存过的，别的一律不收。"""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise WebError(400, "attachments must be an array")
        stored = set(store.attachment_names())
        found: List[Dict[str, Any]] = []
        for item in raw:
            name = item.get("name") if isinstance(item, dict) else item
            if not isinstance(name, str) or name not in stored:
                raise WebError(422, f"attachment is not stored in this meeting: {name!r}")
            found.append({"name": name})
        return found

    def message(self, meeting_id: str, payload: Dict[str, Any]) -> Tuple[MeetingStore, str]:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if not isinstance(payload, dict) or not set(payload).issubset(
                {"message", "delivery", "reference", "references", "attachments"}
            ):
                raise WebError(400, "message request contains unsupported fields")
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, str):
                raise WebError(400, "message must be a string")
            message = message.strip()
            attachments = self._message_attachments(store, payload.get("attachments"))
            if not message and not attachments:
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
                store.append_chat("human", message, "request", references=references,
                                  attachments=attachments)
            else:
                # Preserve the legacy singular journal field when an older
                # client sends ``reference``; V6 callers use ``references``.
                store.append_chat(
                    "human",
                    message,
                    "request",
                    reference=references[0] if references else None,
                    attachments=attachments,
                )
            if self.orders and not self._orders_speaking(meeting_id):
                # 工单那一侧：这句话是"人说的"，进 assistant，由它发单。
                # 工单自己派活的那句（actor 把题面说进会议）不走这里 —— 它只叫醒 agent。
                return self._start_orders(store, meeting_id, message), ""
            if store.meeting.control_owner == "human" and store.meeting.phase == "human_control":
                # A chat while the human holds the terminal is an explicit
                # instruction: hand control back AND resume the Agent with it.
                self._ensure_terminal(store)
                self._sync_human_pipe(store)
                return self._resume_agent_from_human(meeting_id), ""
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
                self._ensure_terminal(store)
                store.bump_request_generation(
                    failure=None,
                    cycle_action_count=0,
                    cycle_started_at=utc_now(),
                )
            else:
                store.update(failure=None)
            if delivery == "interrupt":
                self._interrupt_backend(meeting_id)
            # 从这里往后都是"这话已经收下了"之后的事：Agent 起不来（配置读不出来、终端开不出来）
            # 不能回一个错误 —— 前端会把刚发出去的话塞回输入框，人以为没发出去，再发一遍。
            # 真会里就这么重复发出去了三条。起不来就照实记在会议状态里（页面上那条红字），
            # 再把原因随响应带给前端。
            agent_error = ""
            if wake_agent or delivery == "interrupt":
                try:
                    config = self._config_for_store(store)
                    store.update(
                        status="running",
                        phase="agent_thinking",
                        control_owner=store.meeting.actor_name(),
                    )
                    self._ensure_worker(
                        store,
                        config,
                        resume=not starting_fresh,
                        fresh_conversation=fresh_conversation,
                    )
                except WebError as exc:
                    agent_error = exc.message
                    store.update(
                        status="running",
                        phase="api_error",
                        control_owner="none",
                        failure=agent_error,
                        recovery_required=True,
                    )
        return store, agent_error

    # ------------------------------------------------------------------
    # 工单那一侧：消息先进 assistant，再一张一张发单
    # ------------------------------------------------------------------

    def _orders_speaking(self, meeting_id: str) -> bool:
        """这一句是不是正在跑的那趟工单说出去的（不是人说的）。"""
        with self.lock:
            return bool(self.orders_talking.get(meeting_id))

    def orders_speaking(self, meeting_id: str):
        """声明"接下来这一小段时间里发出去的话是工单派活"（`with` 用）。"""

        @contextlib.contextmanager
        def speaking():
            with self.lock:
                self.orders_talking[meeting_id] = self.orders_talking.get(meeting_id, 0) + 1
            try:
                yield
            finally:
                with self.lock:
                    self.orders_talking[meeting_id] = max(
                        0, self.orders_talking.get(meeting_id, 1) - 1)

        return speaking()

    def _start_orders(self, store: MeetingStore, meeting_id: str, message: str) -> MeetingStore:
        """人类说了一句：起一趟工单（assistant 收下、发单，actor / critic 接单）。"""
        with self.lock:
            running = self.orders_jobs.get(meeting_id)
            if running is not None and running.is_alive():
                raise WebError(409, "这场会的工单还在跑，等它跑完")
            thread = threading.Thread(target=self._run_orders, args=(meeting_id, message),
                                      daemon=True)
            self.orders_jobs[meeting_id] = thread
        store.update(status="running", phase="agent_thinking", control_owner="none")
        thread.start()
        return store

    def _orders_actor_caps(self, store: MeetingStore, meeting_id: str,
                           target: str) -> Dict[str, Callable]:
        """actor 那张能力表：默认是会议里真那个 agent（`MeetingChannel`）。

        `REMOTEBUDDY_ORDERS_WRITER=模块:函数` 把"写"换成脚本化的那一份：这一侧不叫真 agent 也能跑
        一整场（对着真 HTTP、真 journal、真工作区演练用）。
        """
        if self.orders_caps_factory is not None:
            return self.orders_caps_factory(store, meeting_id, target)
        scripted = os.environ.get("REMOTEBUDDY_ORDERS_WRITER")
        if scripted:
            return actor_service(writer_from(scripted),
                                 store.meeting.actor_name() or "actor")
        channel = MeetingChannel(self.public_url, meeting_id, store.workspace_path,
                                 speaking=lambda: self.orders_speaking(meeting_id),
                                 agent_name=store.meeting.actor_name() or "actor")
        return live_service(channel, target)

    def _run_orders(self, meeting_id: str, message: str) -> None:
        """跑一趟：消息给 assistant，它读 store 发单，服务接单，跑到没有该发的单。"""
        try:
            store = self.load_store(meeting_id)
            target = target_for(message)
            case = case_for(message, fallback=meeting_id)
            workflow = load_workflow(project_root=self.project_root)
            services = [(store.meeting.actor_name() or "actor", "actor",
                         self._orders_actor_caps(store, meeting_id, target)),
                        ("critic", "critic", critic_service(workflow))]
            store.append_event("state", "assistant", "orders_started",
                               data={"case": case, "target": target})
            outcome = run_for_meeting(store.run_dir, case=case, words=message,
                                      workflow=workflow, services=services,
                                      out=lambda line: store.append_event(
                                          "state", "orders", "round", data={"line": line}))
            store.append_event("state", "assistant", "orders_finished",
                               data={"outcome": outcome.outcome, "rounds": outcome.rounds})
            if outcome.outcome == "ok":
                store.update(status="running", phase="completion_proposed",
                             control_owner="none", recovery_required=False)
            else:
                store.update(status="running", phase="api_error", control_owner="none",
                             failure=f"工单没跑完：{outcome.outcome}", recovery_required=True)
        except Exception as exc:                    # noqa: BLE001 - 后台线程：写进 journal 才算有人知道
            try:
                self.load_store(meeting_id).append_event(
                    "state", "assistant", "orders_failed",
                    data={"error": f"{type(exc).__name__}: {exc}"})
                self.load_store(meeting_id).update(status="running", phase="api_error",
                                                   control_owner="none",
                                                   failure=f"{type(exc).__name__}: {exc}",
                                                   recovery_required=True)
            except Exception:                       # noqa: BLE001 - 连写都写不进去就只剩退出
                pass
        finally:
            with self.lock:
                self.orders_jobs.pop(meeting_id, None)

    def acquire(self, meeting_id: str) -> MeetingStore:
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            orphaned_agent = self._orphaned_agent(store, meeting_id)
            self._ensure_driver(store)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            # Refuse before touching any state: taking over a pane that no
            # longer exists used to fail the whole meeting afterwards.
            self._ensure_terminal(store)
            if store.meeting.control_owner == "human":
                return store
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
            self._ensure_driver(store)
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
            with self.lock:
                capture_stop = self.capture_stops.get(meeting_id)
            if capture_stop is not None:
                capture_stop.set()
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
        with self.lock:
            capture_stop = self.capture_stops.get(meeting_id)
        if capture_stop is not None:
            capture_stop.set()
        self._ensure_worker(
            store,
            config,
            resume=True,
            fresh_conversation=fresh_conversation,
        )
        return store

    def terminal_input(self, meeting_id: str, payload: Dict[str, Any]) -> MeetingStore:
        """Deliver human input into the shared pane while the human owns it.

        Two payload layouts are accepted:

        - ``{"bytes": "<hex>"}`` -- the takeover terminal's raw keystroke
          bytes (control characters, UTF-8 text, CSI sequences), sent to the
          pane verbatim and journaled as one human action.
        - ``{"keys": [...]}`` -- the Agent keystroke DSL (``$text`` /
          ``$ctrl`` / ``$rest`` plus bare literal strings), retained for
          programmatic callers and tests.

        The human is deliberately free to type anywhere -- the shell, an
        editor, a REPL, or any foreground program -- so no process whitelist is
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

    def stop(self, meeting_id: str, *, force: bool = False) -> MeetingStore:
        """中止这场会（杀掉它的 tmux 会话、把状态写成 interrupted）。

        `force=True` 跳过"这场会现在归谁驱动"的检查：删除会话用得上 —— 两个服务共用一块工作区时，
        声明在别人手里也得能删（删除是数据的事，不该被"谁在开"挡住）。
        """
        initial = self.load_store(meeting_id)
        if not force:
            self._ensure_driver(initial)
        # Publish the durable interrupt before contending on meeting/state
        # locks. The typing loop checks this file at every burst boundary.
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
                if not force:
                    raise WebError(502, "meeting stopped, but tmux cleanup failed") from exc
                # 删除这条路上会话可能早就不在了（没开过终端、或者已经被别处收掉）：
                # 收不掉也得接着删，数据才是这一趟的目的。
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

    def delete_meeting(self, meeting_id: str) -> Dict[str, Any]:
        """把一场会从列表和磁盘上删掉（页面上那条会话的"删除会话"）。

        先照停止那套把在跑的 worker 和它的终端收掉，再删数据目录。目录先改名、再删：
        列表是按目录名扫出来的，改完名字它立刻不在了，哪怕删到一半也不会有半场会留在列表上。
        """
        store = self.load_store(meeting_id)
        if store.meeting.status not in TERMINAL_STATUSES:
            # 强制停：这场会可能正被另一个服务驱动（两个服务共用工作区），但删除是数据的事，
            # 不该被那份声明挡住 —— 先停信号写下去、把 tmux 会话收掉，再删。
            self.stop(meeting_id, force=True)
        elif store.meeting.session_state != "stopped":
            self.cleanup(meeting_id)
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            run_dir = Path(store.run_dir)
            folder = run_dir.parent
            if (folder.name != MeetingStore.WORKSPACE_DIR or run_dir.name != meeting_id
                    or not run_dir.is_dir()):
                raise WebError(500, "meeting data path looks wrong")
            workspace = folder.parent
            graveyard = folder / f".deleting-{meeting_id}-{secrets.token_hex(4)}"
            try:
                os.replace(run_dir, graveyard)
            except OSError as exc:
                raise WebError(502, f"meeting data could not be moved aside: {exc}") from exc
            shutil.rmtree(graveyard, ignore_errors=True)
            with self.lock:
                self.jobs.pop(meeting_id, None)
                self.queued_jobs.pop(meeting_id, None)
                self.capture_jobs.pop(meeting_id, None)
                self.capture_stops.pop(meeting_id, None)
                self.active_backends.pop(meeting_id, None)
                self.active_runners.pop(meeting_id, None)
                self.human_pipes.pop(meeting_id, None)
                self.human_geometry.pop(meeting_id, None)
                self.human_resume.pop(meeting_id, None)
                self._store_cache.pop(meeting_id, None)
        return {"meeting_id": meeting_id, "deleted": True, "workspace": str(workspace)}

    def rename_meeting(self, meeting_id: str, payload: Any = None) -> Dict[str, Any]:
        """更新会话名称并持久化到 meeting.json。"""
        payload = payload if isinstance(payload, dict) else {}
        name = str(payload.get("name") or "").strip()
        if not name:
            raise WebError(400, "meeting name must not be empty")
        if len(name) > 200:
            raise WebError(400, "meeting name is too long")
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            # 走 update 这一条：它在状态锁里重读 manifest 再落盘，两个服务同时改这场会时
            # 不会把对方的改动盖掉。
            store.update(name=name)
            return _public_manifest(store)

    # ------------------------------------------------------------------
    # Skill 那一侧：更新（不动进程）与重启（换一个进程）
    # ------------------------------------------------------------------

    def _install_project_skill(self, store: MeetingStore) -> Tuple[str, int, bool]:
        """把仓库里那份 Skill 覆盖到这场会，返回（摘要, 字节数, 有没有变）。"""
        source = self.skill_path
        if not source.is_file():
            raise WebError(500, f"project skill is missing: {source}")
        text = source.read_text(encoding="utf-8")
        target = store.runtime_skill_path
        previous = target.read_text(encoding="utf-8") if target.is_file() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        payload = text.encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), len(payload), previous != text

    def _live_agent(self, meeting_id: str) -> Tuple[Optional[Any], Optional[AgentBackend]]:
        """正在跑的 runner 和它的 backend；没有就是 (None, None)。"""
        with self.lock:
            job = self.jobs.get(meeting_id)
            if job is None or not job.is_alive():
                return None, None
            return self.active_runners.get(meeting_id), self.active_backends.get(meeting_id)

    def reload_skill(self, meeting_id: str) -> Dict[str, Any]:
        """页面上的"更新"：换掉 Skill 文本，进程不动。

        指令按请求传的 harness（responses / DeepSeek）下一句就用上；把指令写在
        自己进程里的（Claude Code、Codex App Server）要等下一次开工才换 —— 想
        立刻换，用 restart_agent。
        """
        store = self.load_store(meeting_id)
        if store.meeting.status in TERMINAL_STATUSES:
            raise WebError(409, "meeting is already finished")
        digest, size, changed = self._install_project_skill(store)
        runner, client = self._live_agent(meeting_id)
        per_request = callable(getattr(client, "update_instructions", None))
        if runner is None:
            store.append_event(
                "state",
                "harness",
                "skill_reload",
                data={"status": "completed", "sha256": digest, "bytes": size,
                      "changed": changed, "agent_running": False},
            )
        else:
            # 从这一刻起到 agent 真的读到新文本，状态就是"更新"。
            # 没有在跑的进程时不该这么标：那没有人会把它摘下来。
            with self._meeting_lock(meeting_id):
                store.update(phase="skill_reload")
            runner.request_skill_reload()
        return {
            "meeting_id": meeting_id,
            "sha256": digest,
            "bytes": size,
            "changed": changed,
            "agent_running": runner is not None,
            "applies_to_next_request": per_request,
        }

    def restart_agent(self, meeting_id: str) -> Dict[str, Any]:
        """页面上的"重启"：杀掉当前 agent 进程，用现在的 Skill 重开一个。

        没有在跑的进程就没有可重启的东西 —— 下一次开工本来就是一个新进程。
        """
        store = self.load_store(meeting_id)
        if store.meeting.status in TERMINAL_STATUSES:
            raise WebError(409, "meeting is already finished")
        digest, size, changed = self._install_project_skill(store)
        runner, _client = self._live_agent(meeting_id)
        answer = {"meeting_id": meeting_id, "sha256": digest, "bytes": size,
                  "changed": changed, "restarting": True}
        if runner is not None:
            # 有进程：杀掉、用新 Skill 重开一个，同一轮继续。
            with self._meeting_lock(meeting_id):
                store.update(phase="agent_restart")
            runner.request_backend_restart()
            return {**answer, "started": False}
        # 没有进程：那就现在起一个 —— 按"重启"的人要的就是一个换过 Skill 的新进程，
        # 而不是一句"没有在跑的进程"。由人握着终端的时候不许抢。
        if store.meeting.control_owner == "human" or store.meeting.phase in {"created", "starting", "human_control"}:
            raise WebError(409, "agent 没在跑，而且这会儿不该起：先交回控制（或说一句话）再重启")
        self.resume(meeting_id, starting_phase="agent_restart")
        return {**answer, "started": True}

    def resume(self, meeting_id: str, starting_phase: Optional[str] = None) -> MeetingStore:
        """叫醒 agent 干这一轮。

        `starting_phase` 是进程起来之前那段的状态：页面上按"重启"时给 `agent_restart`，
        这样换进程的这段时间 chip 上看得见；默认还是 `agent_thinking`。
        """
        with self._meeting_lock(meeting_id):
            store = self.load_store(meeting_id)
            if store.meeting.status in TERMINAL_STATUSES:
                raise WebError(409, "meeting is already finished")
            orphaned_agent = self._orphaned_agent(store, meeting_id)
            if store.meeting.phase not in RESUMABLE_PHASES and not orphaned_agent:
                raise WebError(409, "Agent can resume only while waiting or after an API error")
            self._ensure_terminal(store)
            config = self._config_for_store(store)
            fresh_conversation = (
                store.meeting.phase in {"api_error", "cycle_timeout", "completion_proposed"}
                or store.meeting.recovery_required
                or orphaned_agent
            )
            store.bump_request_generation(
                status="running",
                phase=starting_phase or "agent_thinking",
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
                    elif backend in {"trae-agent", "trae-acp"}:
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
                        "needs_api_key": not has_api_key
                        and backend not in {"codex-app-server", "trae-agent", "trae-acp"},
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

    def shutdown(self) -> None:
        self.service.shutdown()
        super().shutdown()

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
        print(f"[web] internal error on {self.command} {self.path}: {exc!r}", file=sys.stderr, flush=True)
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
        if not data.strip():
            # 没带正文的 POST 当成 `{}`：`/delete`、`/stop` 这类"没有参数"的接口，
            # 客户端很容易漏掉正文 —— 前端就这么漏了一次，删除一直回 400、点了没反应。
            return {}
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebError(400, "request body must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise WebError(400, "request body must be a JSON object")
        return payload

    def _read_bytes(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise WebError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise WebError(400, "invalid Content-Length") from exc
        if length <= 0 or length > MAX_ATTACHMENT_BYTES:
            raise WebError(413, "attachment is empty or too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise WebError(400, "incomplete request body")
        return data

    def _raw(self, payload: bytes, content_type: str, etag: Optional[str] = None) -> None:
        """Send bytes as they are (an image, not JSON)."""
        try:
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self._headers(content_type, cache="no-cache" if etag else "no-store")
            self.send_header("Content-Length", str(len(payload)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

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
            if path == "/api/assets/changes":
                frontend = self.service.project_root / "frontend"
                names = ("workspace.css", "workspace.js")
                current = parse_qs(parsed.query).get("version", [""])[0]
                deadline = time.monotonic() + 30
                while True:
                    try:
                        versions = {
                            name: hashlib.sha256((frontend / name).read_bytes()).hexdigest()[:24]
                            for name in names
                        }
                    except FileNotFoundError:
                        self._json({"version": "", "assets": {}})
                        return
                    version = ":".join(versions[name] for name in names)
                    if version != current or time.monotonic() >= deadline:
                        self._json({"version": version, "assets": versions})
                        return
                    time.sleep(0.25)
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
                payload = self.service.snapshot(meeting_id)
                etag = '"' + payload["revision"] + '"'
                if self._not_modified_early(etag):
                    return
                self._json(payload, etag=etag)
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
            match = re.fullmatch(r"/api/meetings/([^/]+)/attachments/([^/]+)", path)
            if match:
                found = self.service.attachment(unquote(match.group(1)), unquote(match.group(2)))
                self._raw(found["bytes"], found["media_type"], etag='"'
                          + hashlib.sha256(found["bytes"]).hexdigest()[:24] + '"')
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
            if path == "/api/meetings/order":
                self._json(self.service.order_meetings(self._read_json()))
                return
            if path == "/api/workspaces":
                self._json(self.service.add_workspace(self._read_json()))
                return
            if path == "/api/workspaces/order":
                self._json(self.service.order_workspaces(self._read_json()))
                return
            if path == "/api/workspaces/rename":
                self._json(self.service.rename_workspace(self._read_json()))
                return
            if path == "/api/workspaces/delete":
                self._json(self.service.delete_workspace(self._read_json()))
                return
            if path == "/api/settings":
                self._json(self.service.update_settings(self._read_json()))
                return
            if path == "/api/choose-directory":
                self._json(self.service.choose_directory(self._read_json()))
                return
            if path == "/api/credentials":
                self._json(self.service.update_credentials(self._read_json()))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/attachments", path)
            if match:
                media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                payload = self._read_bytes()
                self._json({"attachment": self.service.save_attachment(
                    unquote(match.group(1)), media_type, payload)}, status=201)
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/skill/reload", path)
            if match:
                self._json(self.service.reload_skill(unquote(match.group(1))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/agent/restart", path)
            if match:
                self._json(self.service.restart_agent(unquote(match.group(1))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/sdk/([A-Za-z_][A-Za-z0-9_]*)", path)
            if match:
                payload = self._read_json() or {}
                self._json(self.service.sdk_call(unquote(match.group(1)), match.group(2),
                                                 payload.get("args") or {}))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/messages", path)
            if match:
                store, agent_error = self.service.message(
                    unquote(match.group(1)), self._read_json()
                )
                payload = {"meeting": _public_manifest(store)}
                if agent_error:
                    # 消息收下了，Agent 没起来 —— 200 + 原因，让前端别再把它塞回输入框。
                    payload["agent_error"] = agent_error
                self._json(payload)
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
            match = re.fullmatch(r"/api/meetings/([^/]+)/terminal/resize", path)
            if match:
                payload = self._read_json() or {}
                cols = payload.get("cols")
                rows = payload.get("rows")
                diagnostics = payload.get("diagnostics")
                if not isinstance(cols, int) or not isinstance(rows, int):
                    raise WebError(400, "terminal resize size must be integers")
                result = self.service.resize_terminal(unquote(match.group(1)), cols, rows, diagnostics)
                self._json(result)
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/tickets/([^/]+)", path)
            if match:
                self._read_json()
                self._json(self.service.issue_ticket(unquote(match.group(1)),
                                                     unquote(match.group(2))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/delete", path)
            if match:
                self._read_empty_json()
                self._json(self.service.delete_meeting(unquote(match.group(1))))
                return
            match = re.fullmatch(r"/api/meetings/([^/]+)/rename", path)
            if match:
                self._json(self.service.rename_meeting(unquote(match.group(1)), self._read_json()))
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
    parser.add_argument(
        "--orders",
        action="store_true",
        help="工单那一侧：人类说的话先进 assistant，由它一张一张发单（actor / critic 是接单的服务）；"
             "不给就是老的 agent 循环",
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
            "trae-acp": ("trae-acp", "openai"),
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
        orders=args.orders,
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
