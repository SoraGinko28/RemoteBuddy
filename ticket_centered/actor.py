"""actor 的进程：写代码这件事交给一个会写代码的 agent。

能力表（`actor_caps`）就是 api 里那四件事：回一句"知道了"、写一版代码、回一句"做好了"、照判语改一版。
"写"落在谁身上由调用者给：`actor_caps(write)` 里是 `write(prompt) -> code`；真会议里用
`meeting_actor_caps(channel)`，`AgentChannel` 把话发进真会议（服务的 HTTP 面）、等那个 agent 干完、
从它的工作区把文件读回来。

进程入口：

    python -m ticket_centered.actor --meeting <会议目录> --case case/quicksort \\
        --bundle <发给我的那一包> --meeting-url http://127.0.0.1:PORT --meeting-id <会议号> \\
        --workspace <agent 干活的目录> --target quicksort.py

那两句回话（"知道了"/"做好了"）在真会议那个用法里是从**会议记录里读它自己说的那句**，不是这里编的：
话由说话的人自己说。
"""

from __future__ import annotations

import http.client
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import _viba  # noqa: F401

from .host import read_stored, text_of
from .member import bundle_main

WORKING_PHASES = ("agent_thinking", "agent_acting")
DONE_PHASES = ("waiting_for_user", "completion_proposed")
# 这两个阶段是"这一轮断了"：会议自己能从这里被一句话叫醒（RESUMABLE_PHASES），
# 所以 wait_for_turn 不再往下等，交给上面重说一遍题面。
FAILED_PHASES = ("api_error", "cycle_timeout")
TERMINAL_STATUSES = ("completed", "failed", "interrupted")


# ----------------------------------------------------------------------
# 能力表：write(prompt) -> code
# ----------------------------------------------------------------------


def actor_caps(write: Callable[[str], str],
               say: Optional[Callable[[str, Any], None]] = None) -> Dict[str, Callable]:
    """actor 的四个算子。`write(prompt) -> code` 就是那个会写代码的 agent。"""

    def acknowledge(env, about):
        return _said(say, "知道了，这就照题目写一版", _read_or(env, about))

    def write_code(env, task, after):
        return write(_first_prompt(text_of(env, task)))

    def report_done(env, code, after):
        return _said(say, "做好了", _read_or(env, code))

    def revise_code(env, code, objection):
        return write(_revision_prompt(text_of(env, code), text_of(env, objection)))

    return {"acknowledge": acknowledge, "write_code": write_code,
            "report_done": report_done, "revise_code": revise_code}


# ----------------------------------------------------------------------
# 真会议里的 actor：AgentChannel + meeting_actor_caps
# ----------------------------------------------------------------------


class AgentChannel:
    """actor 的那条线：真会议的服务在哪儿、agent 叫什么、它干活的目录在哪儿。

    走的是服务的 HTTP 面：`say` 说一句、`snapshot` / `events` 看它干到哪儿、等它这一轮停下、
    把它写的文件读回来。**建会、把人类的话发进去不在这儿**——那是会议那一侧的事。
    """

    def __init__(self, url: str, meeting_id: str, workspace, agent_name: str = "actor", *,
                 poll: float = 1.0, turn_timeout: float = 420.0, file_timeout: float = 90.0,
                 chat_timeout: float = 120.0):
        self.host, self.port = _host_port(url)
        self.meeting_id = meeting_id
        self.workspace = Path(workspace)
        self.agent_name = agent_name
        self.poll = poll
        self.turn_timeout = turn_timeout
        self.file_timeout = file_timeout
        self.chat_timeout = chat_timeout
        self.csrf = ""
        self.csrf_header = "X-RemoteBuddy-CSRF"

    def _request(self, method: str, path: str, payload=None, timeout: float = 60) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if method == "POST":
            headers[self.csrf_header] = self._token()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
        finally:
            conn.close()
        parsed = json.loads(raw) if raw else {}
        if response.status >= 400:
            raise RuntimeError(f"{method} {path} → {response.status}: {parsed}")
        return parsed

    def _token(self) -> str:
        """服务的 POST 要 CSRF：跟 `/api/session` 要一个，拿到就一直用。"""
        if not self.csrf:
            session = self._request("GET", "/api/session")
            self.csrf = str(session.get("csrf_token", ""))
            self.csrf_header = str(session.get("csrf_header", self.csrf_header))
        return self.csrf

    def say(self, message: str) -> dict:
        """人的一句话：真发进会议里，agent 会接着干。"""
        return self._request("POST", f"/api/meetings/{self.meeting_id}/messages",
                             {"message": message})

    def snapshot(self) -> dict:
        return self._request("GET", f"/api/meetings/{self.meeting_id}/snapshot").get("meeting", {})

    def events(self) -> List[dict]:
        return self._request("GET",
                             f"/api/meetings/{self.meeting_id}/events?after=0").get("events", [])

    def chats(self, message_type: Optional[str] = None) -> List[dict]:
        """这个 agent 在会议里说过的话（journal 的 chat 事件，正文在 `content`）。"""
        out = [event for event in self.events()
               if event.get("kind") == "chat" and event.get("actor") == self.agent_name]
        if message_type is not None:
            out = [event for event in out if event.get("message_type") == message_type]
        return out

    def actor_said(self, message_type: str) -> str:
        """它说的最后一句话（`ack` / `final` / `question` …）。"""
        said = self.chats(message_type)
        return str(said[-1].get("content", "")) if said else ""

    def turn_in_flight(self) -> bool:
        meeting = self.snapshot()
        return (meeting.get("phase") in WORKING_PHASES
                or meeting.get("control_owner") not in (None, "none")
                or meeting.get("status") == "running" and meeting.get("phase") not in DONE_PHASES)

    def wait_for_turn(self, before: int, timeout: Optional[float] = None) -> dict:
        """等这一轮真干完：agent 又说了话，而且阶段停下来了。"""
        deadline = time.monotonic() + (self.turn_timeout if timeout is None else timeout)
        meeting = self.snapshot()
        while time.monotonic() < deadline:
            meeting = self.snapshot()
            if meeting.get("status") in TERMINAL_STATUSES:
                return meeting
            if meeting.get("phase") in FAILED_PHASES:
                return meeting
            settled = (meeting.get("phase") in DONE_PHASES
                       and meeting.get("control_owner") in (None, "none"))
            if settled and len(self.chats()) > before:
                return meeting
            time.sleep(self.poll)
        raise TimeoutError(f"等 agent 干完超时：phase={meeting.get('phase')} "
                           f"status={meeting.get('status')} control_owner={meeting.get('control_owner')}")

    def wait_for_chat(self, message_type: str, timeout: Optional[float] = None) -> str:
        """等 agent 自己说出这一类话，返回它说的那句（没有就空串）。"""
        deadline = time.monotonic() + (self.chat_timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            said = self.chats(message_type)
            if said:
                return str(said[-1].get("content", ""))
            time.sleep(self.poll)
        return ""

    def wait_for_file(self, name: str, timeout: Optional[float] = None) -> Path:
        path = self.file(name)
        deadline = time.monotonic() + (self.file_timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            if path.is_file():
                return path
            time.sleep(self.poll)
        raise TimeoutError(f"agent 没交出 {name}（它的工作区：{self.workspace}）")

    def file(self, name: str) -> Path:
        return self.workspace / name

    def ensure_work(self, instruction: str, target: str, *, force: bool = False,
                    changed: bool = False, timeout: Optional[float] = None) -> str:
        """让真 agent 交出 target，然后把那个文件读回来。

        `force` 是"再来一轮"（改版）：就算文件在，也要把这句话说给它。
        `changed` 是"这一轮得真把它改了"：它跟会议里别的角色你来我往说了几句，不算改完了。
        """
        path = self.file(target)
        before = path.stat().st_mtime_ns if path.is_file() else None
        if force or before is None:
            chats = len(self.chats())
            if force or not self.turn_in_flight():
                self.say(instruction)
            meeting = self.wait_for_turn(chats, timeout)
            if meeting.get("phase") in FAILED_PHASES:
                # 这一轮模型那边断了：再说一遍题面，让它重来一轮（会议能从这个阶段被叫醒）。
                self.say(instruction)
                self.wait_for_turn(len(self.chats()), timeout)
        self.wait_for_file(target)
        if changed:
            self.wait_for_change(path, before, timeout)
        return path.read_text(encoding="utf-8")

    def wait_for_change(self, path: Path, before, timeout: Optional[float] = None) -> Path:
        """等这个文件真被改过（改版那一步用它，别把一句回话当成改完了）。"""
        deadline = time.monotonic() + (self.file_timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            if path.is_file() and path.stat().st_mtime_ns != before:
                return path
            time.sleep(self.poll)
        raise TimeoutError(f"agent 没改 {path.name}（还是原来那份）")


def meeting_actor_caps(channel: AgentChannel, target: str = "quicksort.py") -> Dict[str, Callable]:
    """真会议里 actor 的能力表：写交给那个 agent；它说的那两句从会议记录里读。"""

    def acknowledge(env, about):
        return channel.wait_for_chat("ack") or "知道了"

    def write_code(env, task, after):
        return channel.ensure_work(_writing_prompt(text_of(env, task), target), target)

    def report_done(env, code, after):
        return channel.wait_for_chat("final") or "做好了"

    def revise_code(env, code, objection):
        previous = text_of(env, code)
        what = text_of(env, objection)
        if not what:
            return previous                      # 判语没说不合格：照原样，不必再叫一次 agent
        return channel.ensure_work(_revising_prompt(previous, what, target), target,
                                   force=True, changed=True)

    return {"acknowledge": acknowledge, "write_code": write_code,
            "report_done": report_done, "revise_code": revise_code}


# ----------------------------------------------------------------------
# Wording, and small helpers
# ----------------------------------------------------------------------


def _first_prompt(task: str) -> str:
    return f"照这份题面写一版代码：\n{task}"


def _revision_prompt(previous: str, objection: str) -> str:
    if objection:
        return f"判语说这一版不合格：{objection}\n照判语改一版：\n{previous}"
    return f"判语没说不合格（照原样再交一版）：\n{previous}"


def _writing_prompt(task: str, target: str) -> str:
    return f"{_first_prompt(task)}\n结果写进 {target}（就地写文件），写完回复 DONE，不要问我问题。"


def _revising_prompt(previous: str, objection: str, target: str) -> str:
    return f"{_revision_prompt(previous, objection)}\n（结果仍然写进 {target}，写完回复 DONE。）"


def _said(say, words: str, about) -> str:
    if say is not None:
        say(words, about)
    return words


def _read_or(env, address):
    try:
        return read_stored(env, address)
    except RuntimeError:
        return address


def _host_port(url: str):
    """`http://127.0.0.1:8123` 或 `127.0.0.1:8123` → (host, port)。"""
    text = url.split("://", 1)[-1].rstrip("/")
    host, _, port = text.partition(":")
    return host, int(port)


def main(argv=None) -> int:
    def extra(parser):
        parser.add_argument("--meeting-url", required=True, help="真会议的服务地址")
        parser.add_argument("--meeting-id", required=True, help="真会议的会议号")
        parser.add_argument("--workspace", required=True, help="agent 干活的目录")
        parser.add_argument("--target", default="quicksort.py", help="agent 要交的文件")

    def caps_for(args, _workflow):
        channel = AgentChannel(args.meeting_url, args.meeting_id, args.workspace)
        return meeting_actor_caps(channel, args.target)

    return bundle_main("actor", caps_for, argv, extra=extra,
                       description="actor 的进程：写代码交给一个会写代码的 agent")


if __name__ == "__main__":
    sys.exit(main())
