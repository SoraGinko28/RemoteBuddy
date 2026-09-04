from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent_protocol import (
    AgentAction,
    AgentBackendError,
    AgentInterrupted,
    AgentProtocolError,
    AgentResponse,
    BackendEventCallback,
    DynamicActionHandler,
    action_json_schema,
    decode_action_text,
)
from model_api_client import ModelApiConfig


PROTOCOL_NAME = "codex-app-server"
MAX_WIRE_BYTES = 2 * 1024 * 1024
FORBIDDEN_ITEM_TYPES = {
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "collabAgentToolCall",
    "subAgentActivity",
    "webSearch",
    "imageView",
    "imageGeneration",
    "enteredReviewMode",
    "exitedReviewMode",
}
SAFE_ITEM_TYPES = {
    "userMessage",
    "agentMessage",
    "reasoning",
    "plan",
    "contextCompaction",
    "hookPrompt",
    "sleep",
}


class CodexAppServerError(AgentBackendError):
    pass


def _child_environment() -> Dict[str, str]:
    """Codex authenticates via its own CLI login; strip ambient credentials."""
    env = dict(os.environ)
    exact = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
    for name in list(env):
        if name in exact or name.startswith("AWS_") or name.startswith("AZURE_"):
            env.pop(name, None)
    return env


def _default_process_factory(command: Sequence[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_child_environment(),
        start_new_session=True,
    )


class CodexAppServerBackend:
    """One ephemeral Codex App Server thread for one RemoteBuddy work cycle."""

    backend_name = PROTOCOL_NAME
    stateful = True

    def __init__(
        self,
        config: ModelApiConfig,
        *,
        codex_binary: Optional[str] = None,
        process_factory: Callable[[Sequence[str], Path], Any] = _default_process_factory,
    ) -> None:
        self.config = config
        self.codex_binary = codex_binary or shutil.which("codex") or "codex"
        self._process_factory = process_factory
        self._process: Any = None
        self._temporary: Optional[tempfile.TemporaryDirectory] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[Any, Optional[Dict[str, Any]]] = {}
        self._notifications: List[Dict[str, Any]] = []
        self._server_requests: List[Dict[str, Any]] = []
        self._process_error: Optional[str] = None
        self._callback: Optional[BackendEventCallback] = None
        self._thread_id: Optional[str] = None
        self._turn_id: Optional[str] = None
        self._initial_context = ""
        self._first_turn = True
        self._interrupted = False
        self._closed = False
        self._started_at = 0.0
        self._turn_started_at = 0.0
        self.protocol_version = self._codex_version()

    @property
    def session_id(self) -> Optional[str]:
        return self._thread_id

    @property
    def turn_id(self) -> Optional[str]:
        return self._turn_id

    def _codex_version(self) -> str:
        try:
            result = subprocess.run(
                [self.codex_binary, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        value = (result.stdout or "").strip().splitlines()
        return value[0][:120] if value else "unknown"

    def start_cycle(
        self,
        meeting_id: str,
        instructions: str,
        initial_context: str,
        *,
        model: Optional[str] = None,
        event_callback: Optional[BackendEventCallback] = None,
    ) -> str:
        if self._process is not None:
            raise CodexAppServerError("Codex App Server cycle is already active")
        if not meeting_id or not instructions.strip():
            raise CodexAppServerError("Codex App Server requires meeting instructions")
        self._callback = event_callback
        self._initial_context = initial_context
        self._temporary = tempfile.TemporaryDirectory(prefix="remotebuddy-codex-")
        private_cwd = Path(self._temporary.name).resolve()
        try:
            self._process = self._process_factory(
                [self.codex_binary, "app-server", "--stdio"], private_cwd
            )
            if self._process.stdin is None or self._process.stdout is None:
                raise CodexAppServerError("Codex App Server stdio is unavailable")
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="remotebuddy-codex-stdout",
                daemon=True,
            )
            self._reader.start()
            if self._process.stderr is not None:
                self._stderr_reader = threading.Thread(
                    target=self._drain_stderr,
                    name="remotebuddy-codex-stderr",
                    daemon=True,
                )
                self._stderr_reader.start()

            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "remotebuddy",
                        "title": "RemoteBuddy",
                        "version": "0.3",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                min(10.0, self.config.request_timeout_seconds),
            )
            self._notify("initialized")

            thread_params: Dict[str, Any] = {
                "cwd": str(private_cwd),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "personality": "friendly",
                "developerInstructions": instructions,
                "ephemeral": True,
                "serviceName": "remotebuddy",
                "environments": [],
                "runtimeWorkspaceRoots": [],
            }
            # An empty model defers to the Codex CLI's own configured default
            # (the only workable choice for some ChatGPT-plan logins).
            selected_model = model or self.config.model
            if selected_model:
                thread_params["model"] = selected_model
            if self.config.codex_dynamic_tools:
                thread_params["dynamicTools"] = [
                    {
                        "type": "function",
                        "name": "remotebuddy_action",
                        "description": (
                            "Submit one validated action for RemoteBuddy's single shared tmux pane. "
                            "You cannot operate the terminal directly."
                        ),
                        "inputSchema": action_json_schema(),
                    }
                ]
            result = self._request(
                "thread/start", thread_params, min(15.0, self.config.request_timeout_seconds)
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            sources = result.get("instructionSources", []) if isinstance(result, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexAppServerError("Codex App Server returned no thread id")
            if not isinstance(sources, list):
                raise CodexAppServerError("Codex App Server returned invalid instruction sources")
            if sources:
                raise AgentProtocolError("Codex context pollution: instructionSources is not empty")
            self._thread_id = thread_id
            self._started_at = time.monotonic()
            self._emit("thread_started", session_id=thread_id, status="ready")
            return thread_id
        except Exception:
            self.close()
            raise

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
        if not self._thread_id or session_id != self._thread_id:
            raise CodexAppServerError("Codex App Server session is not active")
        if self._turn_id:
            raise CodexAppServerError("Codex App Server already has an active turn")
        timeout = float(request_timeout_seconds or self.config.request_timeout_seconds)
        turn_input = self._initial_context if self._first_turn and self._initial_context else observation
        self._first_turn = False
        params: Dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": turn_input}],
            "approvalPolicy": "never",
            "personality": "friendly",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "environments": [],
            "runtimeWorkspaceRoots": [],
            "outputSchema": action_json_schema(
                None if self.config.codex_dynamic_tools else allowed_types,
                None if self.config.codex_dynamic_tools else allowed_message_types,
            ),
        }
        selected_model = model or self.config.model
        if selected_model:
            params["model"] = selected_model
        if self.config.reasoning_effort != "none":
            params["effort"] = self.config.reasoning_effort
        self._interrupted = False
        started = time.monotonic()
        result = self._request("turn/start", params, min(15.0, timeout))
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexAppServerError("Codex App Server returned no turn id")
        self._turn_id = turn_id
        self._turn_started_at = time.monotonic()
        self._emit("turn_started", session_id=self._thread_id, turn_id=turn_id, status="inProgress")
        try:
            return self._wait_for_turn(
                timeout=max(0.1, timeout - (time.monotonic() - started)),
                allowed_types=allowed_types,
                allowed_message_types=allowed_message_types,
                action_handler=action_handler,
            )
        except TimeoutError as exc:
            self.interrupt()
            raise CodexAppServerError("Codex App Server turn timed out") from exc
        finally:
            # Every exit path must release the turn slot, or the next
            # next_action call would fail with "already has an active turn".
            self._turn_id = None

    def interrupt(self) -> None:
        with self._condition:
            thread_id, turn_id = self._thread_id, self._turn_id
            if not thread_id or not turn_id:
                return
            self._interrupted = True
            self._condition.notify_all()
        try:
            self._request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                min(3.0, self.config.request_timeout_seconds),
            )
        except AgentBackendError:
            # The waiting caller will observe either the interrupt flag or the
            # process failure. Interrupt itself must stay idempotent.
            pass
        self._emit("turn_interrupted", session_id=thread_id, turn_id=turn_id, status="interrupted")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._condition:
            self._interrupted = True
            self._process_error = self._process_error or "closed"
            self._condition.notify_all()
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
                self._signal_process(signal.SIGTERM, process=process)
                try:
                    process.wait(timeout=1.5)
                except Exception:
                    self._signal_process(signal.SIGKILL, process=process)
                    try:
                        process.wait(timeout=1)
                    except Exception:
                        pass
            # Join the readers before closing their streams so a reader never
            # observes a closed file mid-iteration.
            for worker in (self._reader, self._stderr_reader):
                if worker is not None and worker is not threading.current_thread():
                    worker.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        try:
            if self._temporary is not None:
                self._temporary.cleanup()
        except OSError as exc:
            print(f"[codex-app-server] temporary directory cleanup failed: {exc}", file=sys.stderr, flush=True)
        finally:
            self._temporary = None
            self._thread_id = None
            self._turn_id = None

    def _signal_process(self, sig: signal.Signals, *, process: Any = None) -> None:
        target = process or self._process
        if target is None or target.poll() is not None:
            return
        try:
            # Signal the child's own process group so grandchildren are not
            # orphaned; fall back to the direct process if it shares ours.
            if os.getpgid(target.pid) != os.getpgrp():
                os.killpg(target.pid, sig)
                return
        except (AttributeError, OSError):
            pass
        try:
            if sig == signal.SIGKILL:
                target.kill()
            else:
                target.terminate()
        except Exception:
            pass

    def _wait_for_turn(
        self,
        *,
        timeout: float,
        allowed_types: Optional[Sequence[str]],
        allowed_message_types: Optional[Sequence[str]],
        action_handler: Optional[DynamicActionHandler],
    ) -> AgentResponse:
        deadline = time.monotonic() + timeout
        messages: List[Tuple[str, str]] = []
        while True:
            inbound = self._next_inbound(deadline)
            if inbound is None:
                raise TimeoutError()
            kind, value = inbound
            if kind == "interrupted":
                turn_id = self._turn_id
                self._turn_id = None
                raise AgentInterrupted(f"Codex turn interrupted: {turn_id or 'unknown'}")
            if kind == "request":
                response = self._handle_server_request(
                    value,
                    allowed_types=allowed_types,
                    allowed_message_types=allowed_message_types,
                    action_handler=action_handler,
                )
                if response is not None:
                    return response
                continue

            method = value.get("method")
            params = value.get("params") if isinstance(value.get("params"), dict) else {}
            if method in {"item/started", "item/completed"}:
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                item_type = item.get("type")
                item_id = item.get("id")
                status = "started" if method.endswith("started") else "completed"
                self._emit(
                    "item_" + status,
                    session_id=params.get("threadId"),
                    turn_id=params.get("turnId"),
                    item_id=item_id,
                    item_type=item_type,
                    status=status,
                )
                if item_type in FORBIDDEN_ITEM_TYPES or (
                    item_type == "dynamicToolCall" and not self.config.codex_dynamic_tools
                ):
                    self._reject_protocol(f"forbidden Codex item type: {item_type}")
                if item_type not in SAFE_ITEM_TYPES and item_type != "dynamicToolCall":
                    self._reject_protocol(f"unknown Codex item type: {item_type}")
                if method == "item/completed" and item_type == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        messages.append((str(item_id or "agent-message"), text))
                continue
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                turn_id = turn.get("id")
                if turn_id != self._turn_id:
                    continue
                status = turn.get("status")
                elapsed_ms = int(max(0.0, time.monotonic() - self._turn_started_at) * 1000)
                self._emit(
                    "turn_completed",
                    session_id=self._thread_id,
                    turn_id=turn_id,
                    status=status,
                    duration_ms=elapsed_ms,
                )
                self._turn_id = None
                if self._interrupted or status == "interrupted":
                    raise AgentInterrupted(f"Codex turn interrupted: {turn_id}")
                if status != "completed":
                    raise CodexAppServerError("Codex App Server turn failed")
                if not messages:
                    for item in turn.get("items", []):
                        if isinstance(item, dict) and item.get("type") == "agentMessage":
                            item_text = item.get("text")
                            if isinstance(item_text, str) and item_text.strip():
                                messages.append((str(item.get("id") or "agent-message"), item_text))
                if not messages:
                    raise AgentProtocolError("Codex App Server returned no structured action")
                response_id, text = messages[-1]
                # The schema guides Codex toward actions valid for the state at
                # request time.  The pane may change while the turn is in
                # flight, so phase validation belongs to the Runner at the
                # execution boundary, using a freshly captured pane state.
                action = decode_action_text(text)
                return AgentResponse(
                    response_id=response_id,
                    action=action,
                    session_id=self._thread_id,
                    turn_id=str(turn_id),
                )

    def _handle_server_request(
        self,
        request: Dict[str, Any],
        *,
        allowed_types: Optional[Sequence[str]],
        allowed_message_types: Optional[Sequence[str]],
        action_handler: Optional[DynamicActionHandler],
    ) -> Optional[AgentResponse]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if method != "item/tool/call" or not self.config.codex_dynamic_tools:
            self._send_error(request_id, -32601, "RemoteBuddy rejects App Server tool requests")
            self._reject_protocol(f"forbidden Codex server request: {method}")
        if params.get("tool") != "remotebuddy_action" or params.get("namespace") not in {None, ""}:
            self._send_error(request_id, -32602, "Unknown RemoteBuddy dynamic tool")
            self._reject_protocol("Codex requested an unregistered dynamic tool")
        if params.get("threadId") != self._thread_id or params.get("turnId") != self._turn_id:
            self._send_error(request_id, -32602, "Stale RemoteBuddy dynamic tool call")
            self._reject_protocol("Codex dynamic tool call targeted a stale turn")
        if action_handler is None:
            self._send_error(request_id, -32603, "RemoteBuddy action handler is unavailable")
            self._reject_protocol("Codex dynamic tool mode has no action handler")
        arguments = params.get("arguments")
        if isinstance(arguments, dict) and set(arguments) == {"action"}:
            arguments = arguments["action"]
        try:
            # Per-action phase constraints are checked again by the runner,
            # because one dynamic turn may cross the first-ack boundary.
            action = AgentAction.from_dict(arguments)
            result = action_handler(
                action,
                {
                    "call_id": params.get("callId"),
                    "session_id": self._thread_id,
                    "turn_id": self._turn_id,
                },
            )
            if not isinstance(result, dict):
                raise AgentProtocolError("dynamic action handler returned an invalid result")
        except AgentInterrupted:
            result = {"success": False, "content": "superseded", "pause": True}
        except AgentBackendError as exc:
            self._send_dynamic_result(request_id, False, "action rejected")
            self._reject_protocol(str(exc))
        except Exception as exc:  # noqa: BLE001
            # Storage or transport failures (MeetingError, OSError) must still
            # answer the JSON-RPC request before failing the cycle.
            self._send_dynamic_result(request_id, False, "action failed")
            self._reject_protocol(f"dynamic action handler failed: {exc}")
        content = str(result.get("content", "action completed"))[:64 * 1024]
        success = bool(result.get("success", True))
        self._send_dynamic_result(request_id, success, content)
        call_id = str(params.get("callId") or request_id)
        self._emit(
            "dynamic_tool_completed",
            session_id=self._thread_id,
            turn_id=self._turn_id,
            item_id=call_id,
            item_type="dynamicToolCall",
            status="completed" if success else "failed",
        )
        if result.get("pause"):
            turn_id = self._turn_id
            self.interrupt()
            return AgentResponse(
                response_id="dynamic-" + call_id,
                action=action,
                session_id=self._thread_id,
                turn_id=turn_id,
                executed=True,
            )
        return None

    def _reject_protocol(self, message: str) -> None:
        self._emit(
            "protocol_violation",
            session_id=self._thread_id,
            turn_id=self._turn_id,
            status="rejected",
        )
        self.interrupt()
        raise AgentProtocolError(message)

    def _next_inbound(self, deadline: float) -> Optional[Tuple[str, Dict[str, Any]]]:
        with self._condition:
            while True:
                if self._interrupted:
                    return ("interrupted", {})
                if self._server_requests:
                    return ("request", self._server_requests.pop(0))
                if self._notifications:
                    return ("notification", self._notifications.pop(0))
                if self._process_error:
                    raise CodexAppServerError("Codex App Server process exited")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.5))

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = None
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending.get(request_id) is None:
                if self._process_error:
                    self._pending.pop(request_id, None)
                    raise CodexAppServerError("Codex App Server process exited")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(request_id, None)
                    raise CodexAppServerError(f"Codex App Server {method} timed out")
                self._condition.wait(min(remaining, 0.5))
            message = self._pending.pop(request_id)
        if not isinstance(message, dict):
            raise CodexAppServerError("Codex App Server returned an invalid response")
        if "error" in message:
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            code = error.get("code")
            raise CodexAppServerError(f"Codex App Server rejected {method} (code {code})")
        result = message.get("result", {})
        if not isinstance(result, dict):
            raise CodexAppServerError("Codex App Server returned an invalid result")
        return result

    def _notify(self, method: str) -> None:
        self._send({"method": method})

    def _send_dynamic_result(self, request_id: Any, success: bool, text: str) -> None:
        self._send(
            {
                "id": request_id,
                "result": {
                    "contentItems": [{"type": "inputText", "text": text}],
                    "success": success,
                },
            }
        )

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"id": request_id, "error": {"code": code, "message": message}})

    def _send(self, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_WIRE_BYTES:
            raise CodexAppServerError("Codex App Server request is too large")
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex App Server is not running")
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CodexAppServerError("Codex App Server process exited") from exc

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if len(line.encode("utf-8", errors="replace")) > MAX_WIRE_BYTES:
                    raise ValueError("oversized App Server message")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    raise ValueError("invalid App Server JSON")
                if not isinstance(message, dict):
                    raise ValueError("invalid App Server object")
                with self._condition:
                    if "id" in message and ("result" in message or "error" in message):
                        if message["id"] in self._pending:
                            self._pending[message["id"]] = message
                    elif "id" in message and "method" in message:
                        self._server_requests.append(message)
                    elif "method" in message:
                        self._notifications.append(message)
                    self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                print(f"[codex-app-server] stdout stream failed: {exc}", file=sys.stderr, flush=True)
        finally:
            with self._condition:
                if not self._closed:
                    self._process_error = "process-exited"
                self._condition.notify_all()

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for _line in process.stderr:
                pass
        except Exception:
            pass

    def _emit(self, event: str, **fields: Any) -> None:
        callback = self._callback
        if callback is None:
            return
        allowed = {
            "session_id",
            "turn_id",
            "item_id",
            "item_type",
            "status",
            "duration_ms",
        }
        payload = {"backend": self.backend_name, "event": event}
        payload.update({key: value for key, value in fields.items() if key in allowed and value is not None})
        try:
            callback(payload)
        except Exception:
            pass


def check_codex_protocol(
    codex_binary: str,
    *,
    dynamic_tools: bool,
    timeout: float = 15.0,
) -> str:
    """Generate the installed version's schema and verify required fields."""
    try:
        version_result = subprocess.run(
            [codex_binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(timeout, 5.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexAppServerError("could not execute Codex CLI") from exc
    if version_result.returncode != 0:
        raise CodexAppServerError("could not read Codex CLI version")
    version = (version_result.stdout or "unknown").strip().splitlines()[0][:120]
    with tempfile.TemporaryDirectory(prefix="remotebuddy-codex-schema-") as directory:
        command = [codex_binary, "app-server", "generate-json-schema", "--experimental", "--out", directory]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexAppServerError("could not generate Codex App Server schema") from exc
        if result.returncode != 0:
            raise CodexAppServerError("Codex App Server schema generation failed")
        root = Path(directory)
        try:
            thread_schema = json.loads((root / "v2" / "ThreadStartParams.json").read_text("utf-8"))
            turn_schema = json.loads((root / "v2" / "TurnStartParams.json").read_text("utf-8"))
            interrupt_schema = root / "v2" / "TurnInterruptParams.json"
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAppServerError("Codex App Server schema bundle is incomplete") from exc
        thread_fields = set(thread_schema.get("properties", {}))
        turn_fields = set(turn_schema.get("properties", {}))
        required_thread = {"ephemeral", "developerInstructions", "environments", "runtimeWorkspaceRoots"}
        required_turn = {"outputSchema", "sandboxPolicy", "environments", "runtimeWorkspaceRoots"}
        if not required_thread <= thread_fields or not required_turn <= turn_fields or not interrupt_schema.is_file():
            raise CodexAppServerError("Codex App Server lacks required RemoteBuddy protocol fields")
        if dynamic_tools:
            if "dynamicTools" not in thread_fields or not (root / "DynamicToolCallResponse.json").is_file():
                raise CodexAppServerError("Codex App Server dynamic tools are not compatible")
    return version


def probe_codex_backend(config: ModelApiConfig, timeout: float = 15.0) -> str:
    binary = shutil.which("codex")
    if not binary:
        raise CodexAppServerError("Codex CLI is not installed")
    try:
        login = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(timeout, 10.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexAppServerError("Codex login status check failed") from exc
    if login.returncode != 0:
        raise CodexAppServerError("Codex CLI is not logged in")
    version = check_codex_protocol(
        binary,
        dynamic_tools=config.codex_dynamic_tools,
        timeout=timeout,
    )
    backend = CodexAppServerBackend(config, codex_binary=binary)
    try:
        backend.start_cycle(
            "remotebuddy-doctor",
            "RemoteBuddy protocol handshake. Do not use tools or produce a model turn.",
            "",
            model=config.model,
        )
    finally:
        backend.close()
    return version
