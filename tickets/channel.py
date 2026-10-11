"""真会议里 actor 那条线：把话发进会议、等那个 agent 干完、从它的工作区把文件读回来。

走的是会议服务的 HTTP 面：`say` 说一句、`snapshot` / `events` 看它干到哪儿、等它这一轮停下、把它写的
文件读回来。**建会、把人类的话发进去不在这儿**——那是会议那一侧的事。

这一层只说"这一句是工单在派活"：`MeetingChannel`（`web.py` 那一侧）在把话发出去的那一小段时间里声明
这件事，会议收到这句就照旧叫醒 agent、不再从头跑一遍工单（否则自己套自己）。
"""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import List, Optional

WORKING_PHASES = ("agent_thinking", "agent_acting")
DONE_PHASES = ("waiting_for_user", "completion_proposed")
# 这两个阶段是"这一轮断了"：会议自己能从这里被一句话叫醒（RESUMABLE_PHASES），
# 所以 wait_for_turn 不再往下等，交给上面重说一遍题面。
FAILED_PHASES = ("api_error", "cycle_timeout")
TERMINAL_STATUSES = ("completed", "failed", "interrupted")

# 等多久算等：一轮的时限、交出东西的时限、等它说一句话的时限。三个都能按区域给——一个区域就是
# 一个在跑的程序，它的环境变量说这一处的活有多长（立法那种要写一整份法的活，比一轮长得多）。
TURN_TIMEOUT_ENV = "REMOTEBUDDY_TURN_TIMEOUT"
FILE_TIMEOUT_ENV = "REMOTEBUDDY_FILE_TIMEOUT"
CHAT_TIMEOUT_ENV = "REMOTEBUDDY_CHAT_TIMEOUT"
TURN_TIMEOUT = 420.0
FILE_TIMEOUT = 90.0
CHAT_TIMEOUT = 120.0


def _seconds_from_env(name: str, fallback: float) -> float:
    """环境变量说的秒数；没给、给的读不出来，就用那个默认的。"""
    said = os.environ.get(name, "").strip()
    if not said:
        return fallback
    try:
        return float(said)
    except ValueError:
        return fallback


class AgentChannel:
    """actor 的那条线：真会议的服务在哪儿、agent 叫什么、它干活的目录在哪儿。

    走的是服务的 HTTP 面：`say` 说一句、`snapshot` / `events` 看它干到哪儿、等它这一轮停下、
    把它写的文件读回来。**建会、把人类的话发进去不在这儿**——那是会议那一侧的事。

    三个时限不给就用环境变量说的，再不给就是默认那几个（`TURN_TIMEOUT` 那些）。
    """

    def __init__(self, url: str, meeting_id: str, workspace, agent_name: str = "actor", *,
                 poll: float = 1.0, turn_timeout: Optional[float] = None,
                 file_timeout: Optional[float] = None, chat_timeout: Optional[float] = None):
        self.host, self.port = _host_port(url) if url else (None, None)
        self.meeting_id = meeting_id
        self.workspace = Path(workspace)
        self.agent_name = agent_name
        self.poll = poll
        self.turn_timeout = (turn_timeout if turn_timeout is not None
                             else _seconds_from_env(TURN_TIMEOUT_ENV, TURN_TIMEOUT))
        self.file_timeout = (file_timeout if file_timeout is not None
                             else _seconds_from_env(FILE_TIMEOUT_ENV, FILE_TIMEOUT))
        self.chat_timeout = (chat_timeout if chat_timeout is not None
                             else _seconds_from_env(CHAT_TIMEOUT_ENV, CHAT_TIMEOUT))
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
        """一句话发进会议：说的人是这个 agent（不是人）——journal 里记的就是它。"""
        return self._request("POST", f"/api/meetings/{self.meeting_id}/messages",
                             {"message": message, "actor": self.agent_name})

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
            if self.is_a_tree(name) and self._tree_files(path):
                return path
            if path.is_file():
                return path
            time.sleep(self.poll)
        raise TimeoutError(f"agent 没交出 {name}（它的工作区：{self.workspace}）")

    def file(self, name: str) -> Path:
        return self.workspace / name

    def is_a_tree(self, name: str) -> bool:
        """要交的是一棵树（一个目录）还是一个文件？

        名字以 `/` 结尾就是一棵树——那是规范自己的写法（"写到某某目录里"），这一处照它认；
        已经在那儿的一个目录也算。
        """
        return str(name).endswith("/") or self.file(name).is_dir()

    def stamp(self, name: str):
        """这个东西现在是什么时候的：文件看它自己的改动时刻，一棵树看它底下最新那份与份数。

        给"这一趟得真把它改了"用：比的不是一句话，是这一份东西本身。
        """
        path = self.file(name)
        if self.is_a_tree(name):
            files = self._tree_files(path)
            newest = max((one.stat().st_mtime_ns for one in files), default=None)
            return (len(files), newest)
        if path.is_file():
            return path.stat().st_mtime_ns
        return None

    def _tree_files(self, path: Path) -> List[Path]:
        """这棵树底下有哪些文件：`.git` 与会议自己的数据不算。"""
        if not path.is_dir():
            return []
        return [one for one in path.rglob("*") if one.is_file()
                and ".git" not in one.parts and ".meeting" not in one.parts]

    def ensure_work(self, instruction: str, target: str, *, force: bool = False,
                    changed: bool = False, since=None,
                    timeout: Optional[float] = None) -> str:
        """让真 agent 交出 target，然后把它读回来。

        `target` 是一个文件就读那个文件的正文；以 `/` 结尾（或者本来就是目录）就是一棵树，
        读回来的是**那条路径**——那份法写在哪儿，验收条件照这条路径自己去看。

        `force` 是"再来一轮"（改版）：就算东西在，也要把这句话说给它。
        `changed` 是"这一轮得真把它改了"：它跟会议里别的角色你来我往说了几句，不算改完了。
        `since` 是"照这个时刻比"：东西在那一趟开工之前就在了的话，那一趟得真把它改了才算交出来
        （上一次留下的那一版不算这一趟的活）。
        """
        path = self.file(target)
        tree = self.is_a_tree(target)
        before = since if since is not None else self.stamp(target)
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
        return str(path) if tree else path.read_text(encoding="utf-8")

    def wait_for_change(self, path: Path, before, timeout: Optional[float] = None) -> Path:
        """等这个东西真被改过（改版那一步用它，别把一句回话当成改完了）。"""
        name = self._name_of(path)
        deadline = time.monotonic() + (self.file_timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            if self.stamp(name) != before:
                return path
            time.sleep(self.poll)
        raise TimeoutError(f"agent 没改 {path.name}（还是原来那份）")

    def _name_of(self, path: Path) -> str:
        """这条路径在这个工作区里的名字：反过来给 `stamp` / `is_a_tree` 用。"""
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)


def _host_port(url: str):
    """`http://127.0.0.1:8123` 或 `127.0.0.1:8123` → (host, port)。"""
    text = url.split("://", 1)[-1].rstrip("/")
    host, _, port = text.partition(":")
    return host, int(port)


class MeetingChannel(AgentChannel):
    """真会议里 actor 那条线：`say` 之前先声明"这一句是工单派活"。

    会议那一侧因此认得出哪一句是工单派活：它只该把 agent 叫起来干活，不该再从头跑一趟工单。
    除此之外它和 `AgentChannel` 一模一样：等 agent 干完、从它的工作区把文件读回来。
    """

    def __init__(self, url: str, meeting_id: str, workspace, *,
                 speaking=None, agent_name: str = "actor", **kwargs) -> None:
        super().__init__(url, meeting_id, workspace, agent_name=agent_name, **kwargs)
        self._speaking = speaking

    def say(self, message: str) -> dict:
        if self._speaking is None:
            return super().say(message)
        with self._speaking():
            return super().say(message)


class LoopbackChannel(AgentChannel):
    """会议服务自己进程里那条线：`say` / `snapshot` / `events` 直接问这个服务。

    服务本来就在这个进程里，这一趟又是它自己接的单：把话发进会议不必出门再绕一圈 HTTP。
    别的一样——等 agent 干完、从它的工作区把文件读回来。

    `speaking` 是"这一句是工单派活"那一下（`web.py` 那一侧用它把这句话和"人说的"分开）。
    """

    def __init__(self, service, meeting_id: str, workspace, *, speaking=None,
                 agent_name: str = "actor", **kwargs) -> None:
        super().__init__("", meeting_id, workspace, agent_name=agent_name, **kwargs)
        self.service = service
        self._speaking = speaking

    def say(self, message: str) -> dict:
        payload = {"message": message, "actor": self.agent_name}
        if self._speaking is None:
            self.service.message(self.meeting_id, payload)
        else:
            with self._speaking():
                self.service.message(self.meeting_id, payload)
        return {}

    def snapshot(self) -> dict:
        return self.service.snapshot(self.meeting_id).get("meeting", {})

    def events(self) -> List[dict]:
        return self.service.events(self.meeting_id, 0).get("events", [])
