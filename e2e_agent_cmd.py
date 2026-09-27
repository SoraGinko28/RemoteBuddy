"""e2e_agent_cmd — 新动作词汇（`command` / `diff`）在一场**真会**上跑一圈。

跑法（仓库根下）：

    .venv/bin/python e2e_agent_cmd.py [--keep] [--harness claude] [--task "..."]

它只做三件事：

1. 在一块真工作区（`--workspace`，默认 `~/workspace/remotebuddy-agent-cmd-e2e`）上升起
   `web_v2.MeetingWebService`，开一场真会，人类只说一句题面；
2. 等 agent 把活干完或停下来（`completion_proposed` / `waiting_for_user`）；
3. 验收：文件真在、`pytest` 真过、journal 里 diff 那条路的返回码是 0、没有按键类动作。

会花钱、花时间（真模型）。工作区里的 `.meeting/<id>/` 留着，`--keep` 不删。
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

CSRF_HEADER = "X-RemoteBuddy-CSRF"
DEFAULT_TASK = (
    "在这个工作区里做两件事：1）创建 greet.py，里面有一个 greet(name) 函数，返回字符串 "
    "'你好, ' 加上 name；2）创建 test_greet.py，用 pytest 写一条测试，断言 greet('世界') "
    "== '你好, 世界'。然后用 python3 -m pytest -q 跑一遍，让它通过。"
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveMeeting:
    def __init__(self, workspace: Path, backend: str, provider: str) -> None:
        import web_v2

        self.workspace = workspace
        service = web_v2.MeetingWebService(
            project_root=workspace,
            model_api_path=REPO_ROOT / "model_api",
            skill_path=REPO_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md",
            workspace=workspace,
            default_backend=backend,
            default_provider=provider,
        )
        self.service = service
        server = web_v2.build_server("127.0.0.1", free_port(), service=service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.port = server.server_address[1]
        self.csrf = ""
        self.meeting_id = ""

    def request(self, method: str, path: str, payload: Any = None) -> Dict[str, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if method == "POST":
            headers[CSRF_HEADER] = self.csrf
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
        finally:
            connection.close()
        parsed = json.loads(raw) if raw else {}
        if response.status >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status}: {parsed}")
        return parsed

    def start(self, task: str) -> str:
        self.csrf = self.request("GET", "/api/session")["csrf_token"]
        self.meeting_id = "agent-cmd-" + uuid.uuid4().hex[:8]
        self.request("POST", "/api/meetings", {
            "meeting_id": self.meeting_id,
            "message": task,
        })
        return self.meeting_id

    def snapshot(self) -> Dict[str, Any]:
        return self.request("GET", f"/api/meetings/{self.meeting_id}/snapshot")["meeting"]

    def events(self, after: int = 0) -> List[Dict[str, Any]]:
        payload = self.request("GET", f"/api/meetings/{self.meeting_id}/events?after={after}")
        return payload.get("events", [])

    def wait_until_done(self, timeout: float) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            meeting = self.snapshot()
            phase = str(meeting.get("phase"))
            if phase != last:
                print(f"[e2e] phase={phase} actions={meeting.get('cycle_action_count')}",
                      flush=True)
                last = phase
            if phase in {"completion_proposed", "waiting_for_user", "failed", "cycle_timeout"}:
                return meeting
            time.sleep(2.0)
        raise TimeoutError(f"等不到收场，最后是 {last}")


def prepare_workspace(path: Path) -> None:
    """工作区得是个 git 仓库：harness 拍快照、核对落盘结果都靠它。

    没有仓库就在这里建一个（已有的文件当基线提交进去）；已经有内容的工作区不再多写文件。
    """
    path.mkdir(parents=True, exist_ok=True)
    empty = not any(path.iterdir())
    if not (path / ".git").is_dir():
        subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "e2e"], cwd=path, check=True)
        (path / ".gitignore").write_text(".meeting/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    if empty:
        (path / "README.md").write_text("# e2e 工作区\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path).returncode != 0:
        subprocess.run(["git", "commit", "-qm", "e2e 基线"], cwd=path, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(Path.home() / "workspace" / "remotebuddy-agent-cmd-e2e"))
    parser.add_argument("--harness", default="claude")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    prepare_workspace(workspace)
    for name in ("greet.py", "test_greet.py"):
        (workspace / name).unlink(missing_ok=True)

    pairs = {"claude": ("claude-code", "deepseek"), "trae": ("trae-agent", "openai"),
             "codex": ("codex-app-server", "openai")}
    backend, provider = pairs[args.harness]

    live = LiveMeeting(workspace, backend, provider)
    started = time.monotonic()
    print(f"[e2e] workspace={workspace} harness={args.harness}", flush=True)
    meeting_id = live.start(args.task)
    print(f"[e2e] meeting={meeting_id}", flush=True)
    meeting = live.wait_until_done(args.timeout)
    elapsed = time.monotonic() - started

    events = live.events(0)
    actions = [event for event in events if event.get("kind") == "action"]
    kinds: List[str] = []
    results: List[Dict[str, Any]] = []
    for event in actions:
        data = event.get("data") or {}
        action = data.get("action") or {}
        if action.get("type"):
            kinds.append(str(action["type"]))
        if data.get("status") == "result" and isinstance(data.get("result"), dict):
            results.append(data["result"])

    diff_files = sorted({path for one in results if one.get("kind") == "diff"
                         for path in (one.get("files") or [])})
    pytest_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=workspace, capture_output=True, text=True
    )
    report = {
        "meeting_id": meeting_id,
        "phase": meeting.get("phase"),
        "failure": meeting.get("failure"),
        "elapsed_s": round(elapsed, 1),
        "action_kinds": sorted({kind for kind in kinds}),
        "action_count": len([kind for kind in kinds]),
        "diff_results": [one for one in results if one.get("kind") == "diff"],
        "cmd_results": [one for one in results if one.get("kind") == "cmd"],
        "diff_files": {path: (workspace / path).is_file() for path in diff_files},
        "pytest_returncode": pytest_run.returncode,
        "pytest_tail": pytest_run.stdout.strip().splitlines()[-3:],
        "event_count": len(events),
    }
    failures: List[str] = []
    if report["phase"] != "completion_proposed":
        failures.append(f"会议没有走到完成提议，停在 {report['phase']}（{report['failure']}）")
    if "keystrokes" in report["action_kinds"]:
        failures.append("journal 里还有按键类动作")
    if "diff" not in report["action_kinds"]:
        failures.append("一次 diff 动作都没有")
    if not report["diff_results"]:
        failures.append("diff 动作没有留下结果")
    else:
        landed = [one for one in report["diff_results"] if one.get("retcode") == "0"]
        if not landed:
            failures.append(f"没有一次 diff 落盘成功：{report['diff_results']}")
    missing = [path for path, here in report["diff_files"].items() if not here]
    if missing or not report["diff_files"]:
        failures.append(f"diff 说落下的文件不在工作区里: {missing or '一个都没有'}")
    if pytest_run.returncode != 0:
        failures.append("pytest 没过")
    report["failures"] = failures
    report["passed"] = not failures
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.keep:
        print(f"[e2e] 会议目录留在 {workspace / '.meeting' / meeting_id}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
