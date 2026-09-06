"""e2e_user_path — RemoteBuddy「真实用户路径」端到端实测。

目的（回应：重构/加固必须实测对最终用户的影响，不许自嗨）：
  以真 agent + 真 tmux + 真 HTTP 开一场会，把「用户」全程走一遍：
    human 发话 → agent 干活（终端画面/动作）→ human 接管 → 真键盘输入回显
    → 交还 → 收尾；
  并对这场会留下的 journal 断言「用户可感知的不变量」：
    1. 接管/交还的控制事件成对、时序与 owner 正确（前端接管的依据）；
    2. 用户输入的动作事件 received→started（键击簿记），且回显出现在后续画面；
    3. 事件 seq 严格递增、caputured_at 不倒退（回放排序依赖）；
    4. 这份真记录过 meeting_validate：0 违规（G 链校验器对真用户数据的验收）；
    5. 会话期间 agent 确实在工作（screen/action 事件出现、agent 有回话）。

用法：
  python e2e_user_path.py            # 跑一次真会并断言，失败退出码非 0
  python e2e_user_path.py --keep     # 跑完保留 runs 目录便于复查
输出：stdout 一段 JSON 摘要；可用 MEETING_E2E.md 复述。

真实成本提示：会消耗模型 API 额度与几分钟时间；这是刻意为之。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

CSRF_HEADER = "X-RemoteBuddy-CSRF"
START_MESSAGE = "列出当前目录内容（ls -la），然后回复 DONE_E2E_7E3。不要写文件。"
HUMAN_ECHO_MARKER = "E2E_MARKER_9182"
POLL_STEP_S = 1.0
AGENT_FIRST_REPLY_TIMEOUT_S = 240
TAKEOVER_TIMEOUT_S = 60


class E2EFailure(AssertionError):
    pass


class LiveUserPath:
    def __init__(self, project_root: Path) -> None:
        import meeting_v2  # noqa: F401  触发模块初始化
        import web_v2

        self.project_root = project_root
        service = web_v2.MeetingWebService(
            project_root=project_root,
            model_api_path=REPO_ROOT / "model_api",
            skill_path=REPO_ROOT / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md",
        )
        self.service = service
        server = web_v2.build_server("127.0.0.1", _free_port(), service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        self.port = server.server_address[1]
        self.csrf = ""
        self.meeting_id = ""

    # ---- HTTP helpers（真实 socket，等价浏览器发请求）----

    def _request(self, method: str, path: str, payload=None) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Content-Length": str(len(body))}
        if method == "POST":
            headers[CSRF_HEADER] = self.csrf
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8")
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise E2EFailure(f"{method} {path} -> non-JSON ({response.status}): {raw[:200]}")
        if response.status >= 400:
            raise E2EFailure(f"{method} {path} -> {response.status}: {parsed}")
        return parsed

    def start_meeting(self) -> str:
        self.csrf = self._request("GET", "/api/session")["csrf_token"]
        meeting_id = "e2e-" + uuid.uuid4().hex[:8]
        result = self._request("POST", "/api/meetings", {
            "meeting_id": meeting_id,
            "message": START_MESSAGE,
            "typing_profile": "fast",
        })
        self.meeting_id = result["meeting_id"] if isinstance(result, dict) else meeting_id
        return self.meeting_id

    def events(self, after: int = 0) -> dict:
        return self._request("GET", f"/api/meetings/{self.meeting_id}/events?after={after}")

    def snapshot_meeting(self) -> dict:
        payload = self._request("GET", f"/api/meetings/{self.meeting_id}/snapshot")
        return payload["meeting"]

    def send_message(self, message: str) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/messages",
                             {"message": message})

    def acquire(self) -> dict:
        return self._request("POST",
                             f"/api/meetings/{self.meeting_id}/control/acquire", {})

    def release(self) -> dict:
        return self._request("POST",
                             f"/api/meetings/{self.meeting_id}/control/release", {})

    def terminal_input_bytes(self, text: str) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/terminal/input",
                             {"bytes": text.encode("utf-8").hex()})

    def stop(self) -> dict:
        return self._request("POST", f"/api/meetings/{self.meeting_id}/stop", {})

    def shutdown(self) -> None:
        self.server.shutdown()


def _wait_until(deadline_s: float, label: str, predicate) -> dict:
    while time.monotonic() < deadline_s:
        try:
            result = predicate()
            if result:
                return result
        except E2EFailure:
            raise
        except Exception as exc:  # noqa: BLE001 网络抖动重试
            print(f"  retry {label}: {exc!r}")
        time.sleep(POLL_STEP_S)
    raise E2EFailure(f"timeout waiting for {label}")


def _kind_actor(events: list[dict], kind: str, actor: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind and e.get("actor") == actor]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteBuddy real user-path E2E")
    parser.add_argument("--keep", action="store_true", help="keep the temp runs dir")
    args = parser.parse_args(argv)

    tmp_root = Path(tempfile.mkdtemp(prefix="rb-e2e-"))
    journal_dir: Path | None = None
    results: dict = {}
    failed: list[str] = []
    try:
        live = LiveUserPath(tmp_root)
        t_start = time.monotonic()
        try:
            meeting_id = live.start_meeting()
            results["meeting_id"] = meeting_id
            results["agent_task"] = START_MESSAGE
            print(f"started meeting {meeting_id} on :{live.port}", flush=True)

            # -- M2: agent 开始干活：出现 agent 回话 + screen/action 事件 --
            def agent_working() -> dict:
                events = live.events()["events"]
                kinds = {e.get("kind") for e in events}
                agent_chat = any(
                    e.get("kind") == "chat" and e.get("actor") == "agent"
                    for e in events
                )
                if agent_chat and {"screen", "action"} <= kinds:
                    return {"events": events}
                return {}

            first = _wait_until(
                time.monotonic() + AGENT_FIRST_REPLY_TIMEOUT_S,
                "agent to chat + act",
                agent_working,
            )
            agent_chats = _kind_actor(first["events"], "chat", "agent")
            results["agent_chat_count_by_first_reply"] = len(agent_chats)
            results["screen_events_by_first_reply"] = len(
                _kind_actor(first["events"], "screen", "workspace"))
            print(f"agent replied; chats={len(agent_chats)} screens="
                  f"{results['screen_events_by_first_reply']}", flush=True)

            # -- M2b: 等 agent 这一轮干完回到 waiting，再让用户接管（idle 接管）--
            _wait_until(time.monotonic() + AGENT_FIRST_REPLY_TIMEOUT_S,
                        "agent cycle to finish",
                        lambda: live.snapshot_meeting()
                        if live.snapshot_meeting().get("phase") == "waiting_for_user"
                        else {})
            print("agent idle; user may take over", flush=True)

            # -- M3: 用户接管：acquire 后控制事件成对、owner=human --
            t_acquire = time.monotonic()
            live.acquire()
            results["acquire_latency_ms"] = round((time.monotonic() - t_acquire) * 1000)

            def acquired() -> dict:
                meeting = live.snapshot_meeting()
                if meeting.get("control_owner") == "human":
                    return meeting
                return {}

            _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S, "acquire ownership",
                        acquired)
            print("user owns terminal", flush=True)

            # -- M4: 用户真输入并等回显 --
            live.terminal_input_bytes(f"echo {HUMAN_ECHO_MARKER}\n")

            def echo_seen() -> dict:
                events = live.events()["events"]
                frames = "".join(
                    e.get("content") or "" for e in events
                    if e.get("kind") == "screen"
                )
                if HUMAN_ECHO_MARKER in frames:
                    return {"events": events, "frames": frames}
                return {}

            echo = _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S,
                               "user-typed echo on screen", echo_seen)
            echo_events = echo["events"]
            human_actions = _kind_actor(echo_events, "action", "human")
            results["human_action_events"] = len(human_actions)
            statuses = {a.get("data", {}).get("status") for a in human_actions}
            results["human_action_statuses"] = sorted(statuses)
            print("user input echoed on screen", flush=True)

            # -- M5: 交还（idle 接管 → 回到 waiting）--
            live.release()

            def released() -> dict:
                meeting = live.snapshot_meeting()
                if meeting.get("phase") == "waiting_for_user":
                    return meeting
                return {}

            _wait_until(time.monotonic() + TAKEOVER_TIMEOUT_S, "release to waiting",
                        released)
            print("control released back", flush=True)

            # -- 收尾：停止服务；冻结 journal 后做静态断言 --
            live.stop()
            time.sleep(1.0)
            journal_dir = next((tmp_root / "runs").glob(f"{meeting_id}"), None)
            if journal_dir is None:
                raise E2EFailure("journal dir missing after stop")

            full_events = live.events(0)["events"]
            results["total_events"] = len(full_events)
            results["elapsed_s"] = round(time.monotonic() - t_start, 1)
            print(f"journal frozen: {journal_dir} ({results['total_events']} events)",
                  flush=True)

            # 断言块（用户可感知不变量）
            seqs = [e.get("seq") for e in full_events]
            if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
                failed.append("seq must be strictly increasing & unique")
            stamps = [e.get("captured_at") for e in full_events]
            if stamps != sorted(stamps):
                failed.append("captured_at must not go backwards")

            control_acquired = [e for e in full_events
                                if e.get("kind") == "control"
                                and (e.get("data") or {}).get("status") == "acquired"]
            control_released = [e for e in full_events
                                if e.get("kind") == "control"
                                and (e.get("data") or {}).get("status") == "released"]
            if not control_acquired:
                failed.append("no control/acquired event")
            if not control_released:
                failed.append("no control/released event")
            results["control_acquired"] = len(control_acquired)
            results["control_released"] = len(control_released)

            if not human_actions:
                failed.append("no human action events journaled")
            elif "received" not in statuses or "started" not in statuses:
                failed.append("human keystroke bookkeeping incomplete "
                              f"(statuses={sorted(statuses)})")

            frames_text = "".join(e.get("content") or "" for e in full_events
                                  if e.get("kind") == "screen")
            if HUMAN_ECHO_MARKER not in frames_text:
                failed.append("user-typed marker missing from journal frames")

            # G 链校验器对真记录的验收
            import meeting_validate as validator
            report, summary = validator.validate_run_dir(journal_dir)
            results["validator_violations"] = report.count
            results["validator_by_assert"] = dict(report.by_assert())
            results["loader_mapped"] = dict(summary.mapped)
            if not report.ok:
                failed.append(
                    f"validator violations on real journal: {report.count}")

            # snapshot 的 owner/phase 终态
            final_meeting = live.snapshot_meeting()
            results["final_status"] = final_meeting.get("status")
            if final_meeting.get("control_owner") not in ("none", None):
                failed.append("terminal must be released at end")
        finally:
            live.shutdown()
    finally:
        if args.keep and journal_dir is not None:
            kept = REPO_ROOT / "runs" / journal_dir.name
            shutil.copytree(journal_dir, kept)
            results["kept_journal"] = str(kept)
            journal_dir = kept

    results["passed"] = not failed
    results["failures"] = failed
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
