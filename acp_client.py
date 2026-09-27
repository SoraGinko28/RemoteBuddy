"""Trae 走 ACP：它自己的工具留着，动作还是从我们的 MCP 工具回来。

为什么要这个后端：app-server 那套是"客户端驱动"，我们发给它的那一轮要么带
`outputSchema`（整轮被钉成只吐一个 JSON，模型自己的工具被顶掉），要么不带（动作形状
就不受协议约束了）。两条都不合适：前者让模型读不了文件、只能把 `cat` 打进共享终端，
后者动作会漂。ACP 是"agent 用自己的工具、客户端管权限"的协议，正好没有这个矛盾。

分工：

    traecli acp serve -c sandbox="read-only" -c sandbox_permissions=["disk-full-read-access"]
        ↓ initialize → session/new（cwd=工作区，mcpServers 里内联 claude_mcp_server.py）
    它的读：自己的只读工具，在它自己的沙箱里，不碰 tmux
    它的写：ACP 会发 session/request_permission(kind=edit)，我们**拒掉** ——
            于是改文件只能走我们的动作（command/keystrokes），也就是共享终端
    动作：从 MCP 工具回到 `ActionSocket`，跟 Claude Code 那条一模一样
    收尾：动作到手后发 session/cancel，一轮一个动作（runner 的约定）
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from action_bridge import ActionSocket
from agent_protocol import (
    AgentAction,
    AgentActionError,
    AgentBackendError,
    AgentInterrupted,
    AgentProtocolError,
    AgentResponse,
    DynamicActionHandler,
)
from model_api_client import ModelApiConfig

# 一帧的上限：ACP 的通知里有 diff，给宽一点。
MAX_WIRE_BYTES = 4 * 1024 * 1024
# 允许的沙箱：只读。写由"权限请求一律拒 + 动作走终端"保证。
SANDBOX_MODE = "read-only"
# 权限请求按工具种类分。沙箱是 read-only，所以"读"和"跑只读命令"放行（它改不了东西）；
# 写类一律拒绝，改文件只能走我们的动作 —— 也就是共享终端。
READ_ONLY_TOOL_KINDS = {
    "read", "search", "fetch", "list", "think", "other",
    # 它自己的只读 shell：读文件、rg 都在这里跑
    "execute", "command", "shell",
}
WRITE_TOOL_KINDS = {"edit", "write", "delete", "move", "rename", "patch", "create"}
ACP_PROTOCOL_VERSION = 1


class AcpBackend:
    """RemoteBuddy 的 Trae-over-ACP 后端。"""

    backend_name = "trae-acp"
    stateful = True

    def __init__(
        self,
        config: ModelApiConfig,
        *,
        acp_binary: Optional[str] = None,
        process_factory: Optional[Any] = None,
    ) -> None:
        self.config = config
        harness = config.harness_profiles.get("trae-acp", {}) if config.harness_profiles else {}
        configured = harness.get("binary") if isinstance(harness, dict) else None
        self.acp_binary = acp_binary or configured or shutil.which("traecli") or "traecli"
        self._process_factory = process_factory or self._default_process_factory
        self.protocol_version: Optional[str] = None
        self._process: Optional[Any] = None
        self._temporary: Optional[tempfile.TemporaryDirectory] = None
        self._bridge: Optional[ActionSocket] = None
        self._bridge_token = ""
        self._socket_path = ""
        self._session_id: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._callback: Optional[Any] = None
        self._active_handler: Optional[DynamicActionHandler] = None
        self._turn_action: Optional[AgentAction] = None
        self._claimed: Optional[AgentAction] = None
        self._claimed_call_id: Optional[str] = None
        self._closed = False
        self._interrupted = False
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._inbound: List[Dict[str, Any]] = []
        self._condition = threading.Condition(threading.RLock())
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._process_error: Optional[str] = None
        self._instructions = ""
        self._first_turn = True
        self._last_text = ""
        # 调查步要整段文字（`_last_text` 是给错误提示用的，只留尾巴 600 字）。
        self._turn_text_parts: List[str] = []

    # ------------------------------------------------------------------
    # 进程与握手
    # ------------------------------------------------------------------

    @staticmethod
    def _default_process_factory(command: Sequence[str], cwd: Any) -> Any:
        return subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def start_cycle(
        self,
        meeting_id: str,
        instructions: str,
        initial_context: str,
        *,
        model: Optional[str] = None,
        images: Optional[Sequence[Dict[str, Any]]] = None,
        event_callback: Optional[Any] = None,
        workspace: Optional[str] = None,
    ) -> str:
        if self._process is not None:
            raise AgentBackendError("Trae ACP cycle is already active")
        if not meeting_id or not instructions.strip():
            raise AgentBackendError("Trae ACP requires meeting instructions")
        self._closed = False
        self._interrupted = False
        self._process_error = None
        self._inbound = []
        self._turn_action = None
        self._claimed = None
        self._claimed_call_id = None
        self._first_turn = True
        self._callback = event_callback
        self._instructions = instructions
        self._temporary = tempfile.TemporaryDirectory(prefix="remotebuddy-acp-")
        private_cwd = Path(self._temporary.name).resolve()
        os.chmod(private_cwd, 0o700)
        self._socket_path = str(private_cwd / "bridge.sock")
        if len(self._socket_path.encode("utf-8")) > 100:
            raise AgentBackendError(
                "temporary bridge socket path exceeds the AF_UNIX length limit; set TMPDIR shorter"
            )
        self._bridge_token = secrets.token_urlsafe(32)
        try:
            self._bridge = ActionSocket(
                self._socket_path,
                self._bridge_token,
                float(self.config.request_timeout_seconds),
                self._handle_bridge_request,
                label="trae-acp",
            )
            self._bridge.start()
            command = [
                self.acp_binary,
                "acp",
                "serve",
                "-c",
                f'sandbox="{SANDBOX_MODE}"',
                "-c",
                'sandbox_permissions=["disk-full-read-access"]',
                "-c",
                f"model=\"{model or self.config.model}\"",
            ]
            self._process = self._process_factory(command, private_cwd)
            if self._process.stdin is None or self._process.stdout is None:
                raise AgentBackendError("Trae ACP stdio is unavailable")
            self._reader = threading.Thread(
                target=self._read_stdout, name="remotebuddy-acp-stdout", daemon=True
            )
            self._reader.start()
            self._emit("session_started", status="starting")
            self._request(
                "initialize",
                {"protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}},
                timeout=min(15.0, self.config.request_timeout_seconds),
            )
            session = self._request(
                "session/new",
                {"cwd": workspace or str(private_cwd), "mcpServers": [self._mcp_server_spec()]},
                timeout=min(20.0, self.config.request_timeout_seconds),
            )
            session_id = session.get("sessionId") if isinstance(session, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise AgentProtocolError("Trae ACP returned no session id")
            self._session_id = session_id
            self._emit("initialized", session_id=session_id, status="ready")
            return session_id
        except Exception:
            self.close()
            raise

    def _mcp_server_spec(self) -> Dict[str, Any]:
        """把我们的动作工具作为 stdio MCP server 内联进会话。

        ACP 的 `env` 是数组（`[{name, value}]`），写成对象会被拒。
        """
        return {
            "name": "remotebuddy",
            "command": sys.executable,
            "args": [str(Path(__file__).resolve().with_name("claude_mcp_server.py"))],
            "env": [
                {"name": "REMOTEBUDDY_MCP_SOCKET", "value": self._socket_path},
                {"name": "REMOTEBUDDY_MCP_TOKEN", "value": self._bridge_token},
                {
                    "name": "REMOTEBUDDY_MCP_TIMEOUT",
                    "value": str(self.config.request_timeout_seconds),
                },
            ],
        }

    # ------------------------------------------------------------------
    # JSON-RPC
    # ------------------------------------------------------------------

    def _send(self, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_WIRE_BYTES:
            raise AgentBackendError("Trae ACP request is too large")
        process = self._process
        if process is None or process.stdin is None:
            raise AgentBackendError("Trae ACP is not running")
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AgentBackendError("Trae ACP process exited") from exc

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        with self._condition:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for index, message in enumerate(self._inbound):
                    if message.get("id") == request_id and (
                        "result" in message or "error" in message
                    ):
                        self._inbound.pop(index)
                        if "error" in message:
                            error = message.get("error") or {}
                            raise AgentProtocolError(
                                f"Trae ACP {method} failed: {error.get('message') or error}"
                            )
                        result = message.get("result")
                        return result if isinstance(result, dict) else {}
                if self._process_error:
                    raise AgentBackendError(f"Trae ACP process exited: {self._process_error}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentBackendError(f"Trae ACP {method} timed out")
                self._condition.wait(min(0.2, remaining))

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if len(line.encode("utf-8", errors="replace")) > MAX_WIRE_BYTES:
                    raise ValueError("oversized ACP message")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    raise ValueError("invalid ACP JSON")
                if not isinstance(message, dict):
                    raise ValueError("invalid ACP object")
                if "method" in message and "id" in message:
                    # agent → client 的请求：权限之类，由我们当场答复。
                    self._answer_request(message)
                    continue
                self._note_update(message)
                with self._condition:
                    self._inbound.append(message)
                    self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                print(f"[trae-acp] stdout stream failed: {exc}", file=sys.stderr, flush=True)
        finally:
            with self._condition:
                if not self._closed:
                    self._process_error = "process-exited"
                self._condition.notify_all()

    @staticmethod
    def _permission_text(tool_call: Dict[str, Any], params: Dict[str, Any]) -> str:
        """权限请求里那段说明文字（工具名常常只出现在这里）。

        它在 `toolCall.content` 上（不是 `params.content`）。
        """
        parts: List[str] = []
        content = tool_call.get("content") if isinstance(tool_call, dict) else None
        if content is None:
            content = params.get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            inner = item.get("content") if isinstance(item.get("content"), dict) else item
            text = inner.get("text") if isinstance(inner, dict) else None
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)[:2000]

    def _note_update(self, message: Dict[str, Any]) -> None:
        """它自己的工具调用进 journal，最后一段话留着（这轮没给动作时写进错误里）。"""
        if message.get("method") != "session/update":
            return
        update = (message.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind in {"tool_call", "tool_call_update"}:
            # 读文件、跑只读命令在它自己的沙箱里；想改文件会来问权限（我们拒）。
            if kind == "tool_call":
                self._emit(
                    "tool_call",
                    session_id=self._session_id,
                    turn_id=self._turn_id,
                    item_id=str(update.get("toolCallId") or ""),
                    item_type=str(update.get("kind") or update.get("title") or ""),
                    status=str(update.get("status") or ""),
                )
            return
        if kind != "agent_message_chunk":
            return
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            self._last_text = (self._last_text + text)[-600:]
            self._turn_text_parts.append(text)

    def _answer_request(self, message: Dict[str, Any]) -> Optional[str]:
        """agent 来问的事：只有权限值得答，别的老实说不会。

        返回选中的 optionId（测试要断言"读放行、写拒绝"），没得选就是 None。
        """
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method != "session/request_permission":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"RemoteBuddy does not serve {method}"},
                }
            )
            return None
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        kind = str(tool_call.get("kind") or "").lower()
        options = params.get("options") if isinstance(params.get("options"), list) else []
        # ACP 的选项用 `kind` 区分：allow_once / allow_always / reject_once（optionId 各家自取，
        # 这里叫 approved / approved-for-session / …，按 optionId 猜是猜不中的）。
        def option_kind(option: Dict[str, Any]) -> str:
            kind_name = str(option.get("kind") or "").lower()
            if kind_name:
                return kind_name
            option_id = str(option.get("optionId") or option.get("option_id") or "").lower()
            if "cancel" in option_id or "reject" in option_id or "deny" in option_id:
                return "reject_once"
            return "allow_once"
        # 它调用**我们**那个动作工具时也要过一次权限：那是这一轮的正事，必须放行。
        # 这种请求的 title 只写 "Approve MCP tool call"，工具名在 content 那段说明里。
        label = " ".join(str(tool_call.get(key) or "") for key in ("title", "toolCallId", "name"))
        label += " " + self._permission_text(tool_call, params)
        ours = "remotebuddy" in label
        allow = ours or (kind in READ_ONLY_TOOL_KINDS and kind not in WRITE_TOOL_KINDS)
        # 选一个"允许"或"拒绝"的选项；ACP 的选项由 agent 给（optionId 形如 allow_once/…）。
        wanted = None
        for option in options:
            if not isinstance(option, dict):
                continue
            kind_name = option_kind(option)
            if allow and kind_name.startswith("allow"):
                wanted = option.get("optionId") or option.get("option_id")
                break
            if not allow and kind_name.startswith("reject"):
                wanted = option.get("optionId") or option.get("option_id")
                break
        if wanted is None and options:
            first = options[0]
            if isinstance(first, dict):
                wanted = first.get("optionId") or first.get("option_id")
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": {"outcome": "selected", "optionId": wanted}},
            }
        )
        self._emit(
            "permission_answered",
            session_id=self._session_id,
            turn_id=self._turn_id,
            item_type=kind,
            status="allowed" if allow else "refused",
        )
        return str(wanted) if wanted is not None else None

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def next_action(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        images: Optional[Sequence[Dict[str, Any]]] = None,
        allowed_types: Optional[Sequence[str]] = None,
        allowed_message_types: Optional[Sequence[str]] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Optional[DynamicActionHandler] = None,
    ) -> AgentResponse:
        del model, allowed_types, allowed_message_types
        if not self._session_id or session_id != self._session_id or self._process is None:
            raise AgentBackendError("Trae ACP session is not active")
        if action_handler is None:
            raise AgentBackendError("Trae ACP requires the RemoteBuddy action handler")
        timeout = float(request_timeout_seconds or self.config.request_timeout_seconds)
        with self._condition:
            self._turn_id = "turn-" + uuid.uuid4().hex
            turn_id = self._turn_id
            self._active_handler = action_handler
            self._turn_action = None
            self._claimed = None
            self._claimed_call_id = None
            self._interrupted = False
            self._last_text = ""
            self._turn_text_parts = []
        # 这一步的那段话由 runner 拼在 observation 前面（三条 backend 都一样拼），这里只管发。
        text = (
            self._instructions + "\n\n" + observation
            if self._first_turn
            else observation
        )
        self._first_turn = False
        # images: ACP 的 prompt 支持 image 内容块；这里跟 runner 的约定一致，先只发文字。
        del images
        # `session/prompt` 是请求（要 id），不是通知：发成通知它会回 "Method not found"。
        with self._condition:
            self._next_id += 1
            prompt_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": prompt_id,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            }
        )
        self._emit("turn_started", session_id=session_id, turn_id=turn_id, status="inProgress")
        deadline = time.monotonic() + timeout
        try:
            while True:
                with self._condition:
                    action = self._claimed
                    if action is not None:
                        elapsed_ms = int(max(0.0, time.monotonic()) * 0)
                        self._emit(
                            "turn_completed",
                            session_id=session_id,
                            turn_id=turn_id,
                            status="completed",
                            duration_ms=elapsed_ms,
                        )
                        return AgentResponse(
                            response_id=self._claimed_call_id or turn_id,
                            action=action,
                            session_id=session_id,
                            turn_id=turn_id,
                            executed=True,
                        )
                    if self._interrupted:
                        raise AgentInterrupted("Trae ACP turn interrupted")
                    for index, message in enumerate(self._inbound):
                        if message.get("id") == prompt_id:
                            self._inbound.pop(index)
                            if "error" in message:
                                error = message.get("error") or {}
                                raise AgentProtocolError(
                                    f"Trae ACP session/prompt failed: {error.get('message') or error}"
                                )
                            raise AgentBackendError(
                                "Trae ACP ended the turn without a RemoteBuddy action"
                                + (f": {self._last_text[:200]}" if self._last_text else "")
                            )
                    if self._process_error:
                        raise AgentBackendError(f"Trae ACP process exited: {self._process_error}")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentBackendError(
                            "Trae ACP returned no RemoteBuddy action for this observation"
                        )
                    self._condition.wait(min(0.2, remaining))
        finally:
            # 一轮一个动作：动作到手就让它停下（它可能还想接着干别的）。
            if self._session_id:
                try:
                    self._notify("session/cancel", {"sessionId": self._session_id})
                except AgentBackendError:
                    pass
            with self._condition:
                self._active_handler = None
                self._turn_id = None

    def investigate(
        self,
        session_id: str,
        observation: str,
        *,
        model: Optional[str] = None,
        request_timeout_seconds: Optional[float] = None,
        action_handler: Optional[DynamicActionHandler] = None,
    ) -> str:
        """调查步：这一枪只让它用自己的读工具看，拿它写的整段文字当结论。

        它要是去调动作工具，runner 传进来的 `action_handler` 会把那个调用退回（工具结果里
        带 `is_error`），它只能在同一步里改用文字回答。文字就是下一步唯一拿到的东西。
        """
        del model
        if not self._session_id or session_id != self._session_id or self._process is None:
            raise AgentBackendError("Trae ACP session is not active")
        if action_handler is None:
            raise AgentBackendError("Trae ACP requires the RemoteBuddy action handler")
        timeout = float(request_timeout_seconds or self.config.request_timeout_seconds)
        with self._condition:
            self._turn_id = "turn-" + uuid.uuid4().hex
            turn_id = self._turn_id
            self._active_handler = action_handler
            self._turn_action = None
            self._claimed = None
            self._claimed_call_id = None
            self._interrupted = False
            self._last_text = ""
            self._turn_text_parts = []
        text = (
            self._instructions + "\n\n" + observation
            if self._first_turn
            else observation
        )
        self._first_turn = False
        with self._condition:
            self._next_id += 1
            prompt_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": prompt_id,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            }
        )
        self._emit("turn_started", session_id=session_id, turn_id=turn_id, status="inProgress")
        deadline = time.monotonic() + timeout
        try:
            while True:
                with self._condition:
                    for index, message in enumerate(self._inbound):
                        if message.get("id") == prompt_id:
                            self._inbound.pop(index)
                            if "error" in message:
                                error = message.get("error") or {}
                                raise AgentProtocolError(
                                    f"Trae ACP session/prompt failed: {error.get('message') or error}"
                                )
                            findings = "".join(self._turn_text_parts).strip()
                            self._emit(
                                "turn_completed",
                                session_id=session_id,
                                turn_id=turn_id,
                                status="completed",
                            )
                            return findings
                    if self._interrupted:
                        raise AgentInterrupted("Trae ACP turn interrupted")
                    if self._process_error:
                        raise AgentBackendError(f"Trae ACP process exited: {self._process_error}")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentBackendError(
                            "Trae ACP returned no answer for the investigation step"
                        )
                    self._condition.wait(min(0.2, remaining))
        finally:
            with self._condition:
                self._active_handler = None
                self._turn_id = None

    def _handle_bridge_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("request_id")
        arguments = request.get("action")
        payload = arguments.get("action") if isinstance(arguments, dict) else None
        with self._condition:
            handler = self._active_handler
            turn_id = self._turn_id
            session_id = self._session_id
            if handler is None or turn_id is None:
                return {
                    "request_id": request_id,
                    "success": False,
                    "content": "no active RemoteBuddy turn",
                    "pause": True,
                }
        try:
            action = AgentAction.from_dict(payload)
        except AgentActionError as exc:
            return {"request_id": request_id, "success": False, "content": str(exc), "pause": False}
        except Exception as exc:  # noqa: BLE001
            return {"request_id": request_id, "success": False, "content": str(exc), "pause": False}
        try:
            result = handler(
                action, {"call_id": request_id, "session_id": session_id, "turn_id": turn_id}
            )
        except AgentInterrupted:
            result = {"success": False, "content": "superseded", "pause": True}
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "content": str(exc), "pause": False}
        if not isinstance(result, dict):
            result = {"success": False, "content": "handler returned an invalid result", "pause": True}
        with self._condition:
            if self._claimed is None:
                self._claimed = action
                self._claimed_call_id = str(request_id)
                self._condition.notify_all()
        return {
            "request_id": request_id,
            "success": bool(result.get("success", False)),
            "content": str(result.get("content", ""))[:4096],
            "pause": True,
        }

    def interrupt(self) -> None:
        with self._condition:
            self._interrupted = True
            self._condition.notify_all()
        if self._session_id:
            try:
                self._notify("session/cancel", {"sessionId": self._session_id})
            except AgentBackendError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._condition:
            self._interrupted = True
            self._condition.notify_all()
        bridge = self._bridge
        self._bridge = None
        if bridge is not None:
            bridge.close()
        process = self._process
        self._process = None
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.wait(timeout=1.5)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=1.5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError:
                pass
            self._temporary = None
        self._session_id = None
        self._turn_id = None

    def _emit(self, event: str, **fields: Any) -> None:
        callback = self._callback
        if callback is None:
            return
        allowed = {"session_id", "turn_id", "item_id", "item_type", "status", "duration_ms"}
        payload = {"backend": self.backend_name, "event": event}
        payload.update({key: value for key, value in fields.items() if key in allowed and value is not None})
        try:
            callback(payload)
        except Exception:
            pass
