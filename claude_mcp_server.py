#!/usr/bin/env python3
"""Single-tool MCP server used by the isolated Claude Code backend."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from agent_protocol import AgentAction, AgentProtocolError, action_json_schema


MAX_BRIDGE_BYTES = 2 * 1024 * 1024
TOOL_NAME = "remotebuddy_action"


class BridgeError(RuntimeError):
    pass


def _read_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while True:
        block = connection.recv(min(65536, MAX_BRIDGE_BYTES + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if len(chunks) > MAX_BRIDGE_BYTES:
            raise BridgeError("RemoteBuddy bridge response is too large")
        if b"\n" in block:
            break
    line, separator, trailing = bytes(chunks).partition(b"\n")
    if not separator or trailing:
        raise BridgeError("RemoteBuddy bridge returned an invalid frame")
    return line


def _bridge_call(arguments: Dict[str, Any]) -> Dict[str, Any]:
    socket_path = os.environ.get("REMOTEBUDDY_MCP_SOCKET", "")
    token = os.environ.get("REMOTEBUDDY_MCP_TOKEN", "")
    if not socket_path or not token:
        raise BridgeError("RemoteBuddy bridge configuration is missing")
    path = Path(socket_path)
    if not path.is_absolute() or len(socket_path.encode("utf-8")) > 100:
        raise BridgeError("RemoteBuddy bridge socket path is invalid")
    request_id = "mcp-" + uuid.uuid4().hex
    payload = {
        "token": token,
        "request_id": request_id,
        "action": arguments,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BRIDGE_BYTES:
        raise BridgeError("RemoteBuddy action is too large")
    timeout = float(os.environ.get("REMOTEBUDDY_MCP_TIMEOUT", "180"))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(1.0, min(timeout, 300.0)))
        connection.connect(socket_path)
        connection.sendall(encoded + b"\n")
        raw = _read_line(connection)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("RemoteBuddy bridge returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("request_id") != request_id:
        raise BridgeError("RemoteBuddy bridge returned a mismatched response")
    return result


async def run() -> None:
    try:
        import mcp.server.stdio
        import mcp.types as types
        from mcp.server import Server, ServerRequestContext
    except ImportError as exc:
        raise SystemExit("The pinned 'mcp' package is required") from exc

    async def list_tools(
        _context: ServerRequestContext,
        _params: Any,
    ) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=TOOL_NAME,
                    description=(
                        "Submit exactly one validated action to RemoteBuddy's shared terminal. "
                        "RemoteBuddy, not Claude Code, executes the action."
                    ),
                    input_schema=action_json_schema(),
                )
            ]
        )

    async def call_tool(
        _context: ServerRequestContext,
        params: Any,
    ) -> Any:
        if params.name != TOOL_NAME:
            raise ValueError("Unknown RemoteBuddy tool")
        arguments = params.arguments or {}
        payload = arguments.get("action") if isinstance(arguments, dict) else None
        try:
            action = AgentAction.from_dict(payload)
            normalized_action = asdict(action)
            normalized_action["keys"] = list(action.keys)
            if action.references:
                normalized_action["references"] = [dict(item) for item in action.references]
            else:
                # Keep the pre-reference bridge format for callers that did not
                # attach quotes; AgentAction treats the field as optional.
                normalized_action.pop("references", None)
            if action.result_needed:
                # Default True stays off the wire; only an explicit false
                # ("no result needed") is transmitted.
                normalized_action.pop("result_needed", None)
            normalized = {"action": normalized_action}
            result = await asyncio.to_thread(_bridge_call, normalized)
        except (AgentProtocolError, BridgeError, OSError, ValueError) as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc)[:4096])],
                is_error=True,
            )
        content = str(result.get("content", "action completed"))[:64 * 1024]
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=content)],
            is_error=not bool(result.get("success", False)),
        )

    server = Server(
        "remotebuddy",
        version="0.4.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(run())
