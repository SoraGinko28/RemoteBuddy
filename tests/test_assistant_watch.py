"""The assistant: it looks while the actor works, and wakes the critic.

The look is a model call, so these tests inject one; the cadence is the loop's
own, so they inject the clock. What is pinned here: the interval is a floor, the
loop survives a bad look, a decision made while the actor is busy is held until
it parks, and the service starts exactly one watcher for a meeting that has an
assistant member.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import meeting_v2 as m2
import reviewer_harness
import web_v2

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".agents" / "skills" / "workspace-collaborator" / "SKILL.md"


def _store(root, name: str = "assistant-watch") -> m2.MeetingStore:
    return m2.MeetingStore.create(
        root, name, "write the batch helper", SKILL, "s",
        model="deepseek-v4-flash",
    )


def _answer(text: str):
    def post(payload, base_url, api_key):
        post.asked.append(payload)
        return {"content": [{"type": "text", "text": text}]}

    post.asked = []
    return post


class TestOneLook:
    def test_a_summon_becomes_a_line_to_the_critic(self, tmp_path) -> None:
        store = _store(tmp_path)
        post = _answer("SUMMON: 刚写进 quicksort.py 的代码值得过一遍")
        line = reviewer_harness.assistant_turn(store, "assistant", post=post)

        assert line == "@critic 刚写进 quicksort.py 的代码值得过一遍"
        assert len(post.asked) == 1
        assert post.asked[0]["messages"][0]["content"]        # it looked at the meeting
        looks = [event for event in store.events() if event.get("kind") == "state"
                 and event.get("actor") == "assistant"]
        assert looks and looks[-1]["content"] == line

    def test_silence_is_an_answer(self, tmp_path) -> None:
        store = _store(tmp_path)
        line = reviewer_harness.assistant_turn(store, "assistant",
                                               post=_answer("SILENT"))
        assert line is None
        looks = [event for event in store.events() if event.get("kind") == "state"
                 and event.get("actor") == "assistant"]
        assert looks[-1]["content"] == "SILENT"

    def test_a_broken_answer_is_silence_not_a_crash(self, tmp_path) -> None:
        store = _store(tmp_path)

        def broken(payload, base_url, api_key):
            raise RuntimeError("no endpoint today")

        assert reviewer_harness.assistant_turn(store, "assistant", post=broken) is None
        assert [event for event in store.events()
                if event.get("kind") == "error" and event.get("content", "").startswith("RuntimeError")]


class TestWatch:
    def test_it_looks_no_faster_than_the_interval(self, tmp_path) -> None:
        """The interval is a floor: sleeps are never shorter than it."""
        store = _store(tmp_path)
        slept = []
        busy = [True, True, False]

        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: busy.pop(0) if busy else False,
            wake_critic=lambda line: None,
            stop=lambda: False,
            sleep=slept.append,
            post=_answer("SILENT"),
        )
        assert slept and all(seconds >= reviewer_harness.WATCH_INTERVAL_SECONDS
                             for seconds in slept)

    def test_a_decision_while_the_actor_works_is_held_until_it_parks(self, tmp_path) -> None:
        store = _store(tmp_path)
        busy = [True, True, False]
        woken = []
        answers = ["SUMMON: 看一下刚写好的 quicksort.py", "SILENT", "SILENT"]

        def post(payload, base_url, api_key):
            return {"content": [{"type": "text", "text": answers.pop(0)}]}

        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: busy.pop(0) if busy else False,
            wake_critic=woken.append,
            stop=lambda: False,
            sleep=lambda seconds: None,
            post=post,
        )
        # Decided while the actor was working, said once it parked, and only once.
        assert woken == ["@critic 看一下刚写好的 quicksort.py"]

    def test_silence_all_the_way_through_wakes_nobody(self, tmp_path) -> None:
        store = _store(tmp_path)
        busy = [True, False]
        woken = []
        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: busy.pop(0) if busy else False,
            wake_critic=woken.append,
            stop=lambda: False,
            sleep=lambda seconds: None,
            post=_answer("SILENT"),
        )
        assert woken == []

    def test_it_stops_when_the_meeting_does(self, tmp_path) -> None:
        store = _store(tmp_path)
        looks = []
        answers = _answer("SUMMON: 看一下 a.py")
        calls = {"n": 0}

        def stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 2

        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: True,
            wake_critic=lambda line: looks.append(line),
            stop=stop,
            sleep=lambda seconds: None,
            post=answers,
        )
        assert looks == []            # the meeting stopped before the actor parked


class TestServiceWiring:
    def _service(self, root) -> web_v2.MeetingWebService:
        # The watcher reloads the meeting from the project root it belongs to,
        # so the service and the meeting share one root here.
        return web_v2.MeetingWebService(
            project_root=root,
            model_api_path=REPO / "model_api",
            skill_path=SKILL,
        )

    def test_it_starts_one_watcher_and_only_for_a_meeting_with_an_assistant(
            self, tmp_path, monkeypatch) -> None:
        service = self._service(tmp_path)
        started = []
        release = threading.Event()

        def fake_watch(store, agent_name, **kwargs):
            started.append(agent_name)
            release.wait(5)          # a watcher lives as long as the work does

        monkeypatch.setattr(reviewer_harness, "watch", fake_watch)
        with_assistant = _store(tmp_path, "with-assistant")
        with_assistant.update(agent_name2role={"actor": "actor", "critic": "critic",
                                               "assistant": "assistant"})
        service._ensure_assistant_worker(with_assistant)
        for _ in range(200):         # it starts on its own thread
            if started:
                break
            time.sleep(0.01)
        service._ensure_assistant_worker(with_assistant)      # twice is still one
        assert started == ["assistant"]

        without = _store(tmp_path, "without-assistant")
        without.update(agent_name2role={"actor": "actor", "critic": "critic"})
        service._ensure_assistant_worker(without)
        assert started == ["assistant"]
        release.set()


def test_the_default_team_carries_an_assistant() -> None:
    assert m2.DEFAULT_AGENT_ROLE_MAP == {"actor": "actor", "critic": "critic",
                                         "assistant": "assistant"}
