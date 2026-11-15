"""rb：`rb cli` 开一场会、`rb web` 看它。

测试里 `rb cli` 不叫真模型：会议那一侧用假终端加一张脚本化的能力表（和
`tests/test_web_http.py` 用的是同一套安排）。`rb web` 有一处起真进程：那是另一台服务，
同一个工作区、同一份会议数据，两边都是真的。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import rb
import web as web_module
from meeting import MeetingStore
from tickets.workflow import norm_home
from web import MeetingWebService

DRAFT_ORDERS = """\
def quick_sort(items, key=None, reverse=False, bounds=(0, None)):
    lo, hi = bounds
    if hi is None:
        hi = len(items) - 1
    return items
"""

PROMPT = "写一个快速排序，放到 quicksort.py"


class FakeClient:
    pass


class FakeTransport:
    def __init__(self):
        self.sessions = []

    def create_workspace_session(self, session_name, workdir):
        self.sessions.append((session_name, str(workdir)))
        return "%9"

    def session_exists(self, _session_name):
        return bool(self.sessions)

    def pane_info(self, _pane_id):
        where = self.sessions[-1][1] if self.sessions else ""
        return {"dead": "0" if self.sessions else "1", "command": "bash", "path": where,
                "cursor_x": "0", "cursor_y": "0", "pane_width": "120", "pane_height": "36"}

    def capture_visible(self, _pane_id):
        return "fake terminal"

    def capture_visible_ansi(self, pane_id):
        return self.capture_visible(pane_id)

    def start_output_pipe(self, *_args):
        return None

    def stop_output_pipe(self, *_args):
        return None

    def resize_workspace_session(self, *_args):
        return None

    def send_hex(self, *_args):
        return None

    def send_workspace_command(self, *_args, **_kwargs):
        return None

    def send_tokens(self, *_args):
        return None

    def kill_session(self, _session_name):
        return None


class RecordingRunner:
    """不叫真 agent 的那个替身：开终端、把阶段改成等下一句话。"""

    def __init__(self, store, transport, client, lock=None):
        self.store = store
        self.transport = transport
        self.client = client

    def start(self):
        pane = self.transport.create_workspace_session(
            self.store.meeting.session_name, self.store.workspace_path)
        self.store.set_session(pane)
        self.store.update(status="running", phase="waiting_for_user", control_owner="none")

    def open(self):
        self.start()

    def request_skill_reload(self):
        return None

    def request_backend_restart(self):
        return None

    def resume(self, fresh_conversation=False):
        self.store.update(status="running", phase="waiting_for_user",
                          control_owner="none", recovery_required=False)


class Gate:
    """让"做活"这一下停在半路，好让测试在那会儿往会里伸手。"""

    def __init__(self):
        self.arrived = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.arrived.set()
        self.release.wait(30)


def write_model_config(root: Path) -> Path:
    directory = root / "model_api"
    directory.mkdir()
    (directory / "config.toml").write_text(
        'model="gpt-5.6-luna"\n'
        'base_url="https://api.openai.com/v1"\n'
        'request_timeout_seconds=60\n'
        'reasoning_effort="low"\n',
        encoding="utf-8",
    )
    auth = directory / "auth.json"
    auth.write_text('{"api_key":"test-secret"}\n', encoding="utf-8")
    os.chmod(auth, 0o600)
    return directory


def scripted_actor(service, meeting_id, target, gate=None, draft=None):
    """表里 `agent` 那一行用的那张能力表：说、做、收尾，全在本地答。

    交出来的东西也真写进工作区里 `target` 那份文件（真 actor 就是这么干的）：mgmt_critic 问
    "actor 把活干完了吗"就是照工作区里那个文件看的。
    """
    from tickets.binding import text_of

    body = draft or DRAFT_ORDERS

    def landed(code):
        where = Path(service.load_store(meeting_id).workspace_path) / target
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(code, encoding="utf-8")
        return code

    def writing(instruction):
        with service.tickets_speaking(meeting_id):
            service.message(meeting_id, {"message": instruction, "actor": "actor"})

    def acknowledge(env, about):
        writing(f"照这份题面写一版代码：\n{text_of(env, about)}\n结果写进 quicksort.py，"
                "写完回复 DONE。")
        return "知道了，这就照题目写一版"

    def do_step(env, task, after):
        if gate is not None:
            gate.wait()
        return landed(body)

    def report_done(env, result, after):
        return "做好了"

    def redo_step(env, result, objection):
        return landed(text_of(env, result))

    return {"acknowledge": acknowledge, "do_step": do_step,
            "report_done": report_done, "redo_step": redo_step}


class RbTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = (self.root / "work").resolve()
        self.workspace.mkdir()
        self.service = None
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("---\nname: workspace-collaborator\ndescription: Test.\n---\n",
                              encoding="utf-8")
        self.model_api = write_model_config(self.root)
        frontend = self.root / "frontend"
        frontend.mkdir()
        (frontend / "workspace.html").write_text("<!doctype html><title>test</title>",
                                                 encoding="utf-8")
        (frontend / "workspace.js").write_text("'use strict';", encoding="utf-8")
        (frontend / "workspace.css").write_text("body{}", encoding="utf-8")
        self.transport = FakeTransport()
        self.norm = self.root / "norm"
        shutil.copytree(norm_home(), self.norm)
        self.environ = mock.patch.dict(os.environ, {"REMOTEBUDDY_NORM": str(self.norm)})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def tearDown(self):
        service = getattr(self, "service", None)
        if service is not None:
            for meeting in service.list_meetings()["meetings"]:
                if meeting["status"] not in {"completed", "failed", "interrupted"}:
                    try:
                        service.stop(meeting["meeting_id"])
                    except Exception:                      # noqa: BLE001 - 收摊失败不挡别的
                        pass
            self._wait_for_jobs(service)
        self.temporary.cleanup()

    @staticmethod
    def _wait_for_jobs(service, seconds: float = 20.0) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            with service.lock:
                alive = any(worker.is_alive() for worker in service.jobs.values())
                tickets = any(worker.is_alive() for worker in service.ticket_jobs.values())
                capture = any(worker.is_alive() for worker in service.capture_jobs.values())
                queued = bool(service.queued_jobs)
            if not (alive or tickets or capture or queued):
                return
            time.sleep(0.02)

    # -- 造一场会用的那个服务 --------------------------------------

    def service_with(self, gate=None, draft=None):
        holder = {}

        def factory(**_kwargs):
            service = MeetingWebService(
                project_root=self.root,
                model_api_path=self.model_api,
                skill_path=self.skill,
                runner_factory=RecordingRunner,
                client_factory=lambda _config: FakeClient(),
                transport_factory=lambda _binary: self.transport,
                tickets_caps_factory=lambda _store, meeting_id, target: scripted_actor(
                    holder["service"], meeting_id, target, gate=gate, draft=draft),
                workspace=self.workspace,
            )
            holder["service"] = service
            self.service = service
            return service

        return factory

    def run_cli(self, argv, factory=None, wait=True):
        """跑一趟 `rb cli`：印出来的东西收起来，返回 (那一趟的出口, 输出)。

        `wait=False` 时在一根线程里跑，返回里多一个 `thread`。
        """
        out = io.StringIO()
        result = {}

        def go():
            with mock.patch.object(sys, "stdout", out):
                result["code"] = rb.main(["cli", *argv], service_factory=factory)

        if wait:
            with mock.patch.object(sys, "stdout", out):
                result["code"] = rb.main(["cli", *argv], service_factory=factory)
        else:
            thread = threading.Thread(target=go, daemon=True)
            thread.start()
            result["thread"] = thread
        return result, out

    def meeting_dirs(self, workspace=None):
        return sorted(((workspace or self.workspace) / ".meeting").iterdir())

    def one_meeting_dir(self, workspace=None):
        found = [one for one in self.meeting_dirs(workspace) if one.is_dir()]
        self.assertEqual(1, len(found))
        return found[0]

    def assert_the_meeting_did_the_work(self, meeting_dir: Path) -> None:
        """这一场把活做了：那一步答在案子里（交出来的文件由脚本化的 actor 照真 actor 那样落盘）。"""
        from tickets.values import Store

        written, answered = Store(Path(meeting_dir) / "store").read("case/quicksort/drafted",
                                                                   "do_step")
        self.assertTrue(answered, "这一场没有做活")
        self.assertIn("def quick_sort", written)

    # -- 那一行命令 --------------------------------------------------

    def test_the_only_output_is_the_rb_web_command(self):
        factory = self.service_with()
        result, out = self.run_cli(["--workspace", str(self.workspace), PROMPT], factory=factory)
        self.assertEqual(rb.EXIT_OK, result["code"])
        self.assertEqual(f"rb web --workspace {self.workspace}\n", out.getvalue())
        self.assert_the_meeting_did_the_work(self.one_meeting_dir())

    def test_the_json_output_is_one_web_line(self):
        factory = self.service_with()
        result, out = self.run_cli(["--json", "--workspace", str(self.workspace), PROMPT],
                                   factory=factory)
        self.assertEqual(rb.EXIT_OK, result["code"])
        lines = [json.loads(one) for one in out.getvalue().splitlines() if one.strip()]
        self.assertEqual(1, len(lines), out.getvalue())
        self.assertEqual("web", lines[0]["type"])
        self.assertEqual(f"rb web --workspace {self.workspace}", lines[0]["command"])
        self.assertEqual(self.one_meeting_dir().name, lines[0]["meeting_id"])
        # 这一趟读的是什么、用的哪条规则，记在会议目录里
        record = json.loads((self.one_meeting_dir() / "rb.json").read_text(encoding="utf-8"))
        self.assertEqual(str(self.norm), record["norm_root"])
        self.assertEqual("<内置>", record["rule_where"])
        self.assertEqual([], record["interference"])

    def test_a_path_with_spaces_is_quoted_in_the_command(self):
        spaced = (self.root / "two words").resolve()
        spaced.mkdir()
        factory = self.service_with()
        result, out = self.run_cli(["--workspace", str(spaced), PROMPT], factory=factory)
        self.assertEqual(rb.EXIT_OK, result["code"])
        self.assertEqual(f"rb web --workspace '{spaced}'\n", out.getvalue())

    # -- 参数 ------------------------------------------------------

    def test_the_prompt_is_joined_and_dash_reads_stdin(self):
        self.assertEqual("a b c", rb.read_prompt(["a", "b", "c"]))
        self.assertEqual("hi", rb.read_prompt(["-"], stdin=io.StringIO("hi\n")))

    def test_no_command_prints_the_help(self):
        with mock.patch.object(sys, "stdout", io.StringIO()) as out:
            code = rb.main([])
        self.assertEqual(rb.EXIT_USAGE, code)
        self.assertIn("usage: rb", out.getvalue())
        self.assertIn("rb cli", out.getvalue())
        self.assertIn("rb web", out.getvalue())

    def test_an_unknown_harness_is_a_usage_error(self):
        with mock.patch.object(sys, "stderr", io.StringIO()):
            code = rb.main(["cli", "--harness", "nope", "做点什么"])
        self.assertEqual(2, code)

    def test_norm_commit_without_a_repo_is_refused(self):
        code = rb.main(["cli", "--norm-commit-id", "abc", "做点什么"])
        self.assertEqual(rb.EXIT_RUN_FAILED, code)
        self.assertFalse((self.workspace / ".meeting").exists())

    def test_rule_commit_without_a_path_is_refused(self):
        code = rb.main(["cli", "--no-interference-rule-commit-id", "abc", "做点什么"])
        self.assertEqual(rb.EXIT_RUN_FAILED, code)

    def test_a_rule_that_does_not_read_is_refused_before_the_meeting_opens(self):
        broken = self.root / "broken.viba"
        broken.write_text("__impl__ = 这不是 viba\n", encoding="utf-8")
        with mock.patch.object(sys, "stderr", io.StringIO()) as said:
            code = rb.main(["cli", "--no-interference-rule-path", str(broken), "做点什么"])
        self.assertEqual(rb.EXIT_RUN_FAILED, code)
        self.assertIn("干涉规则读不起来", said.getvalue())
        self.assertFalse((self.workspace / ".meeting").exists())

    def test_resume_only_takes_a_meeting_directory(self):
        with self.assertRaises(RuntimeError):
            rb.meeting_path(str(self.workspace))
        with self.assertRaises(RuntimeError):
            rb.meeting_path(str(self.root / "nope"))

    # -- 钉住的那一版 --------------------------------------------------

    def _git_repo_with_norm(self):
        """一份真仓库：先按现在这份规范提交 A，再把受理改坏提交 B。"""
        repo = self.root / "norm-repo"
        (repo / "rb_norm").parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.norm, repo / "rb_norm")
        self._git(repo, "init")
        self._git(repo, "add", "-A")
        self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "A")
        commit_a = self._git(repo, "rev-parse", "HEAD").strip()
        (repo / "rb_norm" / "intake" / "what_to_make.viba").write_text(
            "__decl__ =\n    str\n  <- $env Env\n\nargs = __get_args__ << __decl__\n\n"
            "__impl__ = \"\"\n", encoding="utf-8")
        self._git(repo, "add", "-A")
        self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "B")
        return repo, commit_a

    @staticmethod
    def _git(where: Path, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError(f"git {args}: {done.stderr}")
        return done.stdout

    def test_the_norm_is_taken_from_the_pinned_commit(self):
        repo, commit_a = self._git_repo_with_norm()
        pinned, sha = rb.materialize_norm(str(repo), commit_a)
        self.assertEqual(commit_a, sha)
        self.assertNotEqual(repo, pinned)
        self.assertEqual(
            (self.norm / "intake" / "what_to_make.viba").read_text(encoding="utf-8"),
            (pinned / "rb_norm" / "intake" / "what_to_make.viba").read_text(encoding="utf-8"))
        # 工作树里那一版已经改坏了（受理答不出话）：不钉版本的那一趟派不下活
        worktree = self.root / "worktree"
        worktree.mkdir()
        result, _out = self.run_cli(
            ["--workspace", str(worktree), "--norm-repo-path", str(repo), PROMPT],
            factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, result["code"])
        self.assertFalse((Path(self.one_meeting_dir(worktree)) / "store").exists(),
                         "工作树里那版受理答不出话，却还是派了活")
        # 钉住 A 的那一趟读的是 A 那一版：活照旧做得了
        pinned_work = self.root / "pinned"
        pinned_work.mkdir()
        result, _out = self.run_cli(
            ["--workspace", str(pinned_work),
             "--norm-repo-path", str(repo), "--norm-commit-id", commit_a, PROMPT],
            factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, result["code"])
        meeting_dir = self.one_meeting_dir(pinned_work)
        self.assert_the_meeting_did_the_work(meeting_dir)
        record = json.loads((meeting_dir / "rb.json").read_text(encoding="utf-8"))
        self.assertEqual(commit_a, record["norm_commit"])
        self.assertNotEqual(str(repo), record["norm_root"])
        # 同一个提交永远落在同一条路径上：所以接着跑（`--resume`）算出来的还是它
        self.assertEqual(str(rb.materialize_norm(str(repo), commit_a)[0]), record["norm_root"])

    def test_the_rule_is_taken_from_the_pinned_commit(self):
        repo = self.root / "rule-repo"
        repo.mkdir()
        rule = repo / "no_interference.viba"
        rule.write_text("__decl__ =\n    bool\n  <- $env Env\n\n"
                        "args = __get_args__ << __decl__\n\n__impl__ = false\n", encoding="utf-8")
        self._git(repo, "init")
        self._git(repo, "add", "-A")
        self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "A")
        commit_a = self._git(repo, "rev-parse", "HEAD").strip()
        rule.write_text("__decl__ =\n    str\n  <- $env Env\n\n"
                        "args = __get_args__ << __decl__\n\n__impl__ = \"有人伸手了\"\n",
                        encoding="utf-8")
        # 工作树里是后一版；钉住 A 读到的该是前一版
        source, where, sha = rb.read_rule(str(rule), commit_a)
        self.assertEqual(commit_a, sha)
        self.assertIn("false", source)
        self.assertNotIn("有人伸手了", source)
        worktree, _where, no_sha = rb.read_rule(str(rule), None)
        self.assertEqual("", no_sha)
        self.assertIn("有人伸手了", worktree)

    # -- 有人伸手 --------------------------------------------------

    def _opened_meeting(self, gate=None):
        """开着、driver 握在手上、看着的那一场会。"""
        factory = self.service_with(gate=gate)
        service = factory()
        store = service.start({"message": PROMPT, "workspace": str(self.workspace),
                               "meeting_id": "watched-0001"})
        rule = rb.InterferenceRule(rb.DEFAULT_RULE, "<内置>", "")
        stop = []

        def on_interference(found):
            stop.append(found)

        watch = rb.DriveWatch(service, store, rule, on_interference=on_interference)
        watch.start()
        self.addCleanup(watch.close)
        return service, store, watch, stop

    @staticmethod
    def _wait_for_stop(stop, seconds: float = 10.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop:
                return True
            time.sleep(0.02)
        return False

    def test_a_foreign_write_into_the_journal_is_seen(self):
        service, store, watch, stop = self._opened_meeting()
        # 另一个进程（这里用直接往 journal 里补一条来代表）写了一条不是这一趟写的
        line = json.dumps({"seq": store.last_event_sequence() + 1, "event_id": "event-99999999",
                           "captured_at": "2026-10-12T00:00:00Z", "kind": "state",
                           "actor": "outsider", "phase": "agent_thinking", "content": "",
                           "message_type": None, "data": {}}, ensure_ascii=False) + "\n"
        with store.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self.assertTrue(self._wait_for_stop(stop), "别人写进 journal 的那一条没有被看见")
        self.assertEqual("event", stop[0].observation.what)
        self.assertEqual("outsider", stop[0].observation.actor)

    def test_a_human_speaking_through_this_process_is_seen(self):
        service, store, watch, stop = self._opened_meeting()
        # 人从页面上说了一句，而那个网页服务就在这个进程里：journal 是这一趟自己的进程写的，
        # 也算人伸了手（判的是 actor，不是谁写的）
        service.message("watched-0001", {"message": "等一下，先别做"})
        self.assertTrue(self._wait_for_stop(stop), "人说的那一句没有被看见")
        self.assertEqual("human", stop[0].observation.actor)

    def test_the_driver_claim_taken_away_is_seen(self):
        service, store, watch, stop = self._opened_meeting()
        (Path(store.run_dir) / "driver.json").write_text(json.dumps({
            "pid": 1, "url": "http://127.0.0.1:9999", "workspace": str(self.workspace),
            "meeting_id": "watched-0001", "heartbeat": time.time()}), encoding="utf-8")
        self.assertTrue(self._wait_for_stop(stop), "驱动权被拿走没有被看见")
        self.assertEqual("driver", stop[0].observation.what)

    def test_the_drive_s_own_work_is_not_interference(self):
        """这一趟自己的活（发单、工单派活、成员答话）一条都不该算伸手。"""
        service, store, watch, stop = self._opened_meeting()
        self._wait_for_jobs(service)
        time.sleep(0.5)
        self.assertEqual([], stop)
        self.assertTrue([one for one in store.events() if one.get("phase") == "work_order"],
                        "这一场没有派过活，那这条检查什么也没试")

    def test_reading_the_meeting_is_not_interference(self):
        service, store, watch, stop = self._opened_meeting()
        service.events("watched-0001", 0)
        service.snapshot("watched-0001")
        time.sleep(0.5)
        self.assertEqual([], stop)

    def test_a_rule_that_says_nobody_interfered_keeps_the_meeting_alive(self):
        service, store, watch, stop = self._opened_meeting()
        permissive = self.root / "permissive.viba"
        permissive.write_text("__decl__ =\n    bool\n  <- $env Env\n\n"
                              "args = __get_args__ << __decl__\n\n__impl__ = false\n",
                              encoding="utf-8")
        watch.rule = rb.InterferenceRule(permissive.read_text(encoding="utf-8"),
                                         str(permissive), "")
        watch.rule.meeting_id = "watched-0001"
        service.message("watched-0001", {"message": "等一下"})
        time.sleep(0.6)
        self.assertEqual([], stop)

    # -- `rb web` --------------------------------------------------

    def test_rb_web_prints_its_link(self):
        out = io.StringIO()
        with mock.patch.object(web_module.RemoteBuddyHTTPServer, "serve_forever",
                               lambda self: None), \
                mock.patch.object(sys, "stdout", out):
            code = rb.main(["web", "--workspace", str(self.workspace), "--port", "0"])
        self.assertEqual(rb.EXIT_OK, code)
        self.assertIn("http://127.0.0.1:", out.getvalue())
        self.assertIn(str(self.workspace), out.getvalue())

    def test_rb_web_shows_the_meeting_that_already_ran(self):
        """跑完之后再开网页：那一场会（已经跑完的）在列表里，点开就能看。"""
        result, _out = self.run_cli(["--workspace", str(self.workspace), PROMPT],
                                    factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, result["code"])
        meeting_id = self.one_meeting_dir().name
        port = free_port()
        self.start_rb_web(port)
        code, payload = http_json(f"http://127.0.0.1:{port}/api/meetings")
        self.assertEqual(200, code)
        listed = {one["meeting_id"]: one for one in payload["meetings"]}
        self.assertIn(meeting_id, listed)
        self.assertEqual("completion_proposed", listed[meeting_id]["phase"])
        code, page = http_json(f"http://127.0.0.1:{port}/", raw=True)
        self.assertEqual(200, code)
        self.assertIn("<!doctype html", page.lower())

    def start_rb_web(self, port: int) -> subprocess.Popen:
        """另起一个进程跑 `rb web`（真服务、真工作区），等它接上请求再返回。"""
        environment = dict(os.environ)
        environment["REMOTEBUDDY_NORM"] = str(self.norm)
        process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "rb.py"), "web",
             "--workspace", str(self.workspace), "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
        self.addCleanup(self._stop_process, process)
        deadline = time.time() + 90
        while time.time() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"rb web 退了：{process.stdout.read()}")
            try:
                code, _payload = http_json(f"http://127.0.0.1:{port}/api/meetings")
                if code == 200:
                    return process
            except Exception:                              # noqa: BLE001 - 还没起来
                pass
            time.sleep(0.1)
        raise AssertionError("rb web 没起来")

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    # -- 整趟跑起来之后 --------------------------------------------

    def test_a_message_from_another_process_stops_the_run(self):
        gate = Gate()
        factory = self.service_with(gate=gate)
        result, out = self.run_cli(["--json", "--workspace", str(self.workspace), PROMPT],
                                   factory=factory, wait=False)
        thread = result["thread"]
        self.assertTrue(gate.arrived.wait(90), "这一场会没有跑到做活那一步")
        line = self._wait_for(out, "web")
        meeting_id = line["meeting_id"]
        port = free_port()
        self.start_rb_web(port)
        # 另一边（`rb web`）看得见这一场正在跑的会
        code, payload = http_json(f"http://127.0.0.1:{port}/api/meetings")
        self.assertEqual(200, code)
        self.assertIn(meeting_id, [one["meeting_id"] for one in payload["meetings"]])
        # 人从那边说了一句：这一句落在同一份 journal 上，驱动这一趟的进程看得见
        token = http_json(f"http://127.0.0.1:{port}/api/session")[1]["csrf_token"]
        http_json(f"http://127.0.0.1:{port}/api/meetings/{meeting_id}/messages",
                  method="POST", payload={"message": "先停下"}, token=token)
        gate.release.set()
        thread.join(120)
        self.assertFalse(thread.is_alive(), "伸手之后这一趟没有停下来")
        self.assertEqual(rb.EXIT_INTERFERED, result["code"])
        store = MeetingStore.load_at(self.one_meeting_dir(), meeting_id)
        self.assertEqual("interrupted", store.meeting.status)
        self.assertIn("interfered", [one.get("data", {}).get("status")
                                     for one in store.events() if one.get("kind") == "state"])
        record = json.loads((Path(store.run_dir) / "rb.json").read_text(encoding="utf-8"))
        self.assertTrue(record["interference"])
        self.assertTrue(any("human" in (one["actor"] or "") for one in record["interference"]),
                        record["interference"])
        # 跑完（停了）之后，`rb web` 上照样看得见这一场
        code, payload = http_json(f"http://127.0.0.1:{port}/api/meetings")
        self.assertEqual(200, code)
        after = {one["meeting_id"]: one for one in payload["meetings"]}
        self.assertEqual("interrupted", after[meeting_id]["status"])

    def _wait_for(self, out: io.StringIO, kind: str, seconds: float = 20.0) -> dict:
        deadline = time.time() + seconds
        while time.time() < deadline:
            for line in out.getvalue().splitlines():
                if not line.strip():
                    continue
                try:
                    one = json.loads(line)
                except ValueError:                         # 还没写完的那一行，下一轮再看
                    continue
                if one["type"] == kind:
                    return one
            time.sleep(0.05)
        raise AssertionError(f"等不到 {kind} 那一行：{out.getvalue()!r}")

    def test_resume_picks_up_the_unfinished_work_of_that_meeting_only(self):
        """接着跑：从这一场那几张单的进度上接起来；别的会不动。"""
        first, _out = self.run_cli(["--workspace", str(self.workspace), PROMPT],
                                   factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, first["code"])
        meeting_dir = self.one_meeting_dir()
        meeting_id = meeting_dir.name
        started = []
        others = []
        factory = self.service_with()
        service = factory()
        service.unfinished_tickets = lambda _store: ("case/quicksort", PROMPT, "quicksort.py")
        service._start_tickets = lambda store, mid, words, target, case=None: started.append(
            (mid, words, target, case))
        service.resume_unfinished_tickets = lambda: others.append("整个工作区")
        result, out = self.run_cli(["--json", "--resume", str(meeting_dir)], factory=lambda **_: service)
        self.assertEqual(rb.EXIT_OK, result["code"])
        self.assertEqual([(meeting_id, PROMPT, "quicksort.py", "case/quicksort")], started)
        self.assertEqual([], others, "接着跑只该动这一场，不该去跑工作区里别的会")

    def test_resume_carries_the_same_meeting_on(self):
        first, _out = self.run_cli(["--json", "--workspace", str(self.workspace), PROMPT],
                                   factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, first["code"])
        meeting_dir = self.one_meeting_dir()
        meeting_id = meeting_dir.name
        second, out = self.run_cli(["--json", "--resume", str(meeting_dir), "再写一版"],
                                   factory=self.service_with())
        self.assertEqual(rb.EXIT_OK, second["code"])
        self.assertEqual(meeting_id, self.one_meeting_dir().name)
        lines = [json.loads(one) for one in out.getvalue().splitlines() if one.strip()]
        self.assertEqual(meeting_id, lines[0]["meeting_id"])
        store = MeetingStore.load_at(meeting_dir, meeting_id)
        said = [one for one in store.events() if one.get("kind") == "chat"
                and one.get("actor") == "human"]
        self.assertIn("再写一版", [one.get("content") for one in said])
    def test_the_flow_verdict_is_the_exit_code(self):
        """流程那一关的判决就是这一趟的退出码：`mgmt_critic` 说没走完，`rb cli` 就不是 0。

        这一场把干活的和判代码的那两行换成替身（`tickets/dummy.py`）：账面上一路"合格"、
        工作区里一个文件都没有。流程那一关该把这两样都点出来，退出码就是它那一个数（3），
        点出的问题印在标准错误上，标准输出照旧只有那一行。
        """
        poison = self.root / "poison_norm"
        shutil.copytree(Path(__file__).resolve().parent / "data" / "poison_norm", poison)
        self.addCleanup(shutil.rmtree, poison, ignore_errors=True)
        factory = self.service_with()
        errors = io.StringIO()
        # 这一场换毒规范那一份（`with` 收得住：跑完环境就回原样，别漏给后面的 test）
        with mock.patch.dict(os.environ, {"REMOTEBUDDY_NORM": str(poison / "rb_norm")}), \
                mock.patch.object(sys, "stderr", errors):
            result, out = self.run_cli(["--workspace", str(self.workspace), PROMPT],
                                       factory=factory)
        self.assertNotEqual(0, result["code"])          # 毒规范的返回值必须非 0
        self.assertEqual(rb.EXIT_FLOW_INCOMPLETE, result["code"])
        self.assertEqual(f"rb web --workspace {self.workspace}\n", out.getvalue())
        self.assertIn("流程那一关没过", errors.getvalue())
        self.assertIn("没留下判的是哪一份", errors.getvalue())
        self.assertIn("不在工作区里", errors.getvalue())

    def test_a_meeting_that_passed_the_flow_check_still_exits_zero(self):
        """流程那一关判过了，退出码照旧是 0：这一条不该把好会也算成没走完。"""
        factory = self.service_with()
        result, _out = self.run_cli(["--workspace", str(self.workspace), PROMPT], factory=factory)
        self.assertEqual(rb.EXIT_OK, result["code"])





def free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def http_json(url: str, method: str = "GET", payload=None, token: str = "", raw: bool = False):
    """打这台服务一下：返回 (状态码, 答复)。`raw=True` 就把正文原样交回。"""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-RemoteBuddy-CSRF", token)
    try:
        with urllib.request.urlopen(request, timeout=15) as answer:
            said = answer.read().decode("utf-8")
            return answer.status, (said if raw else json.loads(said or "{}"))
    except urllib.error.HTTPError as bad:
        said = bad.read().decode("utf-8")
        if raw:
            return bad.code, said
        try:
            return bad.code, json.loads(said or "{}")
        except ValueError:
            return bad.code, {"raw": said}


if __name__ == "__main__":
    unittest.main()
