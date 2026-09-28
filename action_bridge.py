"""动作工具的 socket 服务端：模型那边的 MCP server 把动作发到这里。

另一头是 `claude_mcp_server.py`（一个 stdio MCP server）：Claude Code 用
`--mcp-config` 拉起它，Trae 走 ACP 时用 `session/new` 里内联的 `mcpServers` 项拉起它。
两边只差"收到动作之后怎么办"（Claude 靠 tool 结果里那个 pause 结束这一轮，ACP 靠
`session/cancel`），所以这里只做传输与鉴权：监听、收一帧、把答复写回去。
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import threading
from typing import Any, Callable, Dict, Optional

# 一帧的上限，跟子进程那边的 MCP 实现对齐。
MAX_FRAME_BYTES = 2 * 1024 * 1024


class ActionSocket:
    """一个 unix socket，收 MCP server 转过来的动作，回一份工具结果。"""

    def __init__(
        self,
        socket_path: str,
        token: str,
        timeout_seconds: float,
        on_request: Callable[[Dict[str, Any]], Dict[str, Any]],
        *,
        label: str = "action",
    ) -> None:
        self.socket_path = socket_path
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.on_request = on_request
        self.label = label
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        listener.listen(4)
        listener.settimeout(0.25)
        self._listener = listener
        self._thread = threading.Thread(
            target=self._serve, name=f"remotebuddy-{self.label}-bridge", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _serve(self) -> None:
        while True:
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(max(1.0, min(self.timeout_seconds, 300.0)))
                try:
                    request = self._read_frame(connection)
                    response = self._dispatch(request)
                except Exception as exc:  # noqa: BLE001
                    response = {
                        "request_id": "unknown",
                        "success": False,
                        "content": str(exc)[:4096],
                        "pause": True,
                    }
                try:
                    encoded = json.dumps(
                        response, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    connection.sendall(encoded[:MAX_FRAME_BYTES] + b"\n")
                except OSError:
                    pass

    def _read_frame(self, connection: socket.socket) -> Dict[str, Any]:
        chunks = bytearray()
        while True:
            block = connection.recv(min(65536, MAX_FRAME_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > MAX_FRAME_BYTES:
                raise ValueError(f"{self.label} MCP request is too large")
            if b"\n" in block:
                break
        line, separator, trailing = bytes(chunks).partition(b"\n")
        if not separator or trailing:
            raise ValueError(f"{self.label} MCP request has an invalid frame")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{self.label} MCP request is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{self.label} MCP request must be an object")
        return value

    def _dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"{self.label} MCP request id is invalid")
        if not hmac.compare_digest(str(request.get("token", "")), self.token):
            raise ValueError(f"{self.label} MCP authentication failed")
        value = self.on_request(request)
        if not isinstance(value, dict):
            raise ValueError(f"{self.label} MCP answer is invalid")
        return value
