"""ticket_centered 在一场**真实的会**上跑一圈：真 agent、真 tmux、真 HTTP、真规则。

跑法（仓库根下）：

    .venv/bin/python e2e_ticket_meeting.py [--keep]

这个脚本不是流程本身，它只干三件事（流程在 `ticket_centered/` 里）：

1. 开一场真会（`web_v2.MeetingWebService` + model_api 里的 claude-code/deepseek），人类说一句题面；
2. **只走页面那几条 HTTP 面**：assistant 那一步（`--words`）、工单板（`GET /api/meetings/<id>/tickets`）、
   发一张（`POST /api/meetings/<id>/tickets/<工单>`）——发单是服务那一侧干的：`Meeting.publish` 发给会上
   每个成员，然后每个成员起一个进程（`python -m ticket_centered.<角色> --bundle …`），各人跑自己那份；
3. 验收：文件、两份判语、对账、工单板，打一份 JSON。

跑完**这场会留着**（`--finish leave`，默认）：页面上还能接管、还能聊天、还能按工单那块"发这一张"；
要收掉就 `--finish complete`（照页面的"结束并保留"）或 `--finish stop`。

会花钱：真 agent 真跑，几分钟、真模型额度——这是故意的。会议目录留着：
`<工作区>/.meeting/<meeting_id>/`（工作区就是 `--workspace DIR`，不给是仓库根）：journal 是真会议的，
`ticket_centered/` 是这张流程留下的。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from ticket_centered import (Meeting, Member, load_workflow,  # noqa: E402
                             workflow_root)

CSRF_HEADER = "X-RemoteBuddy-CSRF"
DEFAULT_TASK = ("写一个快速排序，放到 quicksort.py。接口必须是 "
                "quick_sort(items, key=None, reverse=False)，就地排序。")
FLOW = ["write_a_draft", "judge_the_draft", "revise_the_draft",
        "judge_the_revision", "close_the_meeting"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MeetingService:
    """人那一侧：开一场真会、说话、看它的 journal、收场（和页面同一条 HTTP 面）。"""

    def __init__(self, project_root: Path, message: str, *, meeting_id: Optional[str] = None,
                 backend: Optional[str] = None, provider: Optional[str] = None,
                 workspace: Optional[Path] = None, attach: Optional[str] = None):
        self.project_root = Path(project_root).resolve()
        self.message = message
        self.workspace_root = (Path(workspace).expanduser().resolve() if workspace
                               else self.project_root)
        # `attach` 指着一个已经在跑的服务（页面那个）：这场会就开在它的工作区里，
        # 页面上立刻看得到，也只有它一个在驱这场会。不给就自己起一个。
        self.server = None
        if attach:
            parsed = urlparse(attach)
            self.host = parsed.hostname or "127.0.0.1"
            self.port = parsed.port or 80
            self.service = None
        else:
            import web_v2

            self.host = "127.0.0.1"
            self.service = web_v2.MeetingWebService(
                project_root=self.project_root,
                workspace=self.workspace_root,
            )
            self.server = web_v2.build_server("127.0.0.1", free_port(), service=self.service)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.port = self.server.server_address[1]
        self.csrf = ""
        self.meeting_id = meeting_id or f"ticket-{uuid.uuid4().hex[:8]}"
        self.backend = backend
        self.provider = provider
        self.stopped = False

    def _request(self, method: str, path: str, payload=None, timeout: float = 60) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if method == "POST":
            headers[CSRF_HEADER] = self.csrf
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

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def speak(self) -> str:
        """人类说第一句话：这场会就开起来了，真 agent 开始干活。"""
        self.csrf = self._request("GET", "/api/session")["csrf_token"]
        payload: Dict[str, Any] = {"meeting_id": self.meeting_id, "message": self.message,
                                   "typing_profile": "fast"}
        if self.workspace_root != self.project_root:
            payload["workspace"] = str(self.workspace_root)
        if self.backend:
            payload["agent_backend"] = self.backend
        if self.provider:
            payload["agent_provider"] = self.provider
        answer = self._request("POST", "/api/meetings", payload)
        return answer.get("meeting_id", self.meeting_id)

    def events(self) -> List[dict]:
        return self._request("GET",
                             f"/api/meetings/{self.meeting_id}/events?after=0").get("events", [])

    def finish(self, completed: bool) -> None:
        """这场会收场：流程走完就照页面那个"完成"按钮收（completed），没走完就停掉。

        走的是页面同一组 HTTP 面：`/complete` / `/stop`。完成的那种 `session_state` 是 retained，
        页面上还能看终端回放。
        """
        if self.stopped:
            return
        self.stopped = True
        path = "complete" if completed else "stop"
        try:
            self._request("POST", f"/api/meetings/{self.meeting_id}/{path}", {})
        except Exception:                                   # 收不了场也别把跑完的活扔了
            pass

    def shutdown(self) -> None:
        if self.server is not None:
            self.server.shutdown()

    @property
    def run_dir(self) -> Path:
        return self.workspace_root / ".meeting" / self.meeting_id

    @property
    def workspace(self) -> Path:
        # The agent works in the workspace root itself; the meeting's own data
        # lives in the hidden `.meeting/` beside it.
        return self.workspace_root

    def chats(self) -> List[dict]:
        return [{"seq": event.get("seq"), "actor": event.get("actor"),
                 "message_type": event.get("message_type"),
                 "content": str(event.get("content", ""))[:200]}
                for event in self.events() if event.get("kind") == "chat"]


def board(service: MeetingService) -> dict:
    """页面那块"工单"看到的东西（同一个 HTTP 面）。"""
    return service._request("GET", f"/api/meetings/{service.meeting_id}/tickets")["tickets"]


def issue(service: MeetingService, ticket: str) -> dict:
    """页面按一下"发这一张"就是这一下。"""
    return service._request("POST", f"/api/meetings/{service.meeting_id}/tickets/{ticket}", {})


def wait_for_ticket(service: MeetingService, ticket: str, timeout: float) -> dict:
    """等这一张跑完（页面也是这么轮询的）：这一步该答的都有了答案，而且服务说它不在跑了。"""
    deadline = time.monotonic() + timeout
    payload = board(service)
    while time.monotonic() < deadline:
        payload = board(service)
        one = next((item for item in payload["tickets"] if item["ticket"] == ticket), None)
        if one and one["answers"] and all(step["answered"] for step in one["answers"]) \
                and not payload.get("running"):
            return payload
        time.sleep(1.0)
    return payload


def current_ticket(service: MeetingService, timeout: float = 120.0) -> Optional[str]:
    """该发哪一张由服务读 store 算出来（页面看的是同一个字段）。

    还有成员在跑就等它跑完——不然会把"跑着"当成"没有该发的"。
    """
    deadline = time.monotonic() + timeout
    payload = board(service)
    while payload.get("running") and time.monotonic() < deadline:
        time.sleep(1.0)
        payload = board(service)
    for one in payload["tickets"]:
        if one["due"]:
            return one["ticket"]
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="工单流程跑在一场真实的会上")
    parser.add_argument("--keep", action="store_true", help="跑完把会议目录留着看")
    parser.add_argument("--meeting-id", default=None, help="会议号（默认自动起一个）")
    parser.add_argument("--backend", default=None, help="harness（默认 model_api/config.toml 里的）")
    parser.add_argument("--provider", default=None, help="模型 provider（同上）")
    parser.add_argument("--target", default="quicksort.py", help="agent 要交的文件名")
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="工作区（一个目录）：数据进 <DIR>/.meeting/，agent 就在 <DIR> 根下干活"
                             "（不给就用仓库根）")
    parser.add_argument("--task", default=DEFAULT_TASK, help="人类说的那句题面")
    parser.add_argument("--expect", default="quick_sort(items, key=None, reverse=False)",
                        help="交出来的文件里必须有的那行接口")
    parser.add_argument("--turn-timeout", type=float, default=900.0, help="一份包等多久")
    parser.add_argument("--attach", default=None, metavar="URL",
                        help="用已经在跑的那个服务（例如 http://127.0.0.1:8765），"
                             "不再自己起一个：会开在它的工作区里，页面上直接看得到")
    parser.add_argument("--finish", choices=("leave", "complete", "stop"), default="leave",
                        help="跑完怎么处置这场会：留着（默认，页面上还能接管、聊天、发下一张）、"
                             "收掉（照页面的结束并保留）还是停掉")
    args = parser.parse_args(argv)
    human = args.task + f"\n结果写进 {args.target}（就地写文件），写完回复 DONE，不要问我问题。"

    workflow = load_workflow(project_root=REPO_ROOT)
    service = MeetingService(REPO_ROOT, human, meeting_id=args.meeting_id,
                             backend=args.backend, provider=args.provider,
                             workspace=args.workspace, attach=args.attach)
    print(f"会议：{service.meeting_id}（服务在 {service.url}）", flush=True)
    print(f"数据：{service.run_dir}", flush=True)
    print(f"工作区：{service.workspace}", flush=True)
    print(f"流程：{workflow_root(project_root=REPO_ROOT)}（摘要 {workflow.digest()[:8]}）", flush=True)
    print(f"人类说：{human}", flush=True)
    service.speak()

    ticket_root = service.run_dir / "ticket_centered"
    meeting = Meeting(ticket_root, workflow)
    for name in ("actor", "assistant", "critic"):
        meeting.join(Member(name, name, {}))            # 能力表在成员自己的进程里
    case = meeting.case(Path(args.target).stem or "quicksort")

    issued: List[str] = []
    try:
        environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        words = subprocess.run([sys.executable, "-m", "ticket_centered.assistant",
                                "--meeting", str(ticket_root), "--case", case,
                                "--words", human],
                               cwd=str(REPO_ROOT), env=environment, capture_output=True, text=True)
        assert words.returncode == 0, words.stdout + words.stderr
        for round_number in range(1, 9):
            print(f"—— assistant 看情况发起第 {round_number} 张 ——", flush=True)
            payload = board(service)
            for one in payload["tickets"]:
                print(f"    {one['ticket']}：{'该发' if one['due'] else '不发'}（{one['why']}）",
                      flush=True)
            ticket = current_ticket(service)
            if ticket is None:
                print("    没有该发的了\n", flush=True)
                break
            issue(service, ticket)
            issued.append(ticket)
            print(f"  工单：{ticket}（页面那条 POST 就是它）", flush=True)
            payload = wait_for_ticket(service, ticket, args.turn_timeout)
            for one in payload["tickets"]:
                if one["ticket"] != ticket:
                    continue
                for step in one["answers"]:
                    mark = "✓" if step["answered"] else "…"
                    print(f"  ← {step['path']} + {step['op']} {mark}"
                          f"（{step['role'] or '谁也不会'}）", flush=True)
            print(flush=True)
    finally:
        summary = _summary(service, workflow, meeting, case, issued, args)
        summary["board"] = board(service)
        if args.finish != "leave":
            service.finish(completed=args.finish == "complete")
        summary["left_running"] = args.finish == "leave" and not summary["failures"]
        service.shutdown()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for failure in summary["failures"]:
        print(f"没通过：{failure}", file=sys.stderr)
    return 1 if summary["failures"] else 0


def _summary(service: MeetingService, workflow, meeting: Meeting, case: str, issued,
             args) -> dict:
    """这场会留下了什么：真会议那一侧 + 流程那一侧。"""
    failures = []
    verdicts = {}
    for where in ("judged", "judged_revision"):
        value, answered = meeting.store.read(meeting.absolute(case, where), "judge")
        verdicts[where] = value if answered else None
    objection, _ = meeting.store.read(meeting.absolute(case, "judged"), "objection")
    closing, answered = meeting.store.read(meeting.absolute(case, "closing"), "closing_note")
    target = service.workspace / args.target
    written = target.read_text(encoding="utf-8") if target.is_file() else ""

    if issued != FLOW:
        failures.append(f"五张工单没按流程发完：{issued}")
    if not written:
        failures.append(f"{args.target} 没写出来：{target}")
    if verdicts["judged_revision"] is not True:
        failures.append(f"最后那版没被判合格：judged_revision={verdicts['judged_revision']}")
    if args.expect not in written:
        failures.append(f"交出来的接口不是人类要的（没有 {args.expect!r}）")
    if not answered:
        failures.append("没有收尾那句话")
    disagreements = meeting.reconcile()
    if disagreements:
        failures.append(f"对账有分歧：{disagreements}")
    chats = service.chats()
    if not any(one["actor"] == "actor" for one in chats):
        failures.append("会议里没看到 actor 说过话")

    return {
        "meeting_id": service.meeting_id,
        "run_dir": str(service.run_dir),
        "workspace": str(service.workspace),
        "target": str(target),
        "workflow_digest": workflow.digest()[:12],
        "tickets": issued,
        "verdicts": verdicts,
        "task": args.task,
        "expect": args.expect,
        "objection": str(objection)[:200] if objection else "",
        "closing_note": str(closing)[:200] if closing else "",
        "written_first_line": written.splitlines()[0] if written else "",
        "written_lines": len(written.splitlines()),
        "chat_events": len(chats),
        "chats": chats,
        "reconcile": disagreements,
        "failures": failures,
    }


if __name__ == "__main__":
    raise SystemExit(main())
