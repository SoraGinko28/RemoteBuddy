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
        look = reviewer_harness.assistant_turn(store, "assistant", post=post)

        assert look.action == "summon"
        assert look.line == "@critic 刚写进 quicksort.py 的代码值得过一遍"
        assert len(post.asked) == 1
        assert post.asked[0]["messages"][0]["content"]        # it looked at the meeting
        looks = [event for event in store.events() if event.get("kind") == "state"
                 and event.get("actor") == "assistant"]
        assert looks and looks[-1]["content"] == look.line

    def test_silence_is_an_answer(self, tmp_path) -> None:
        store = _store(tmp_path)
        look = reviewer_harness.assistant_turn(store, "assistant",
                                               post=_answer("SILENT"))
        assert look.action == "silent"
        looks = [event for event in store.events() if event.get("kind") == "state"
                 and event.get("actor") == "assistant"]
        assert looks[-1]["content"] == "SILENT"

    def test_a_broken_answer_is_silence_not_a_crash(self, tmp_path) -> None:
        store = _store(tmp_path)

        def broken(payload, base_url, api_key):
            raise RuntimeError("no endpoint today")

        assert reviewer_harness.assistant_turn(store, "assistant",
                                               post=broken).action == "silent"
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
            act=lambda look: None,
            stop=lambda: False,
            sleep=slept.append,
            post=_answer("SILENT"),
        )
        assert slept and all(seconds >= reviewer_harness.WATCH_INTERVAL_SECONDS
                             for seconds in slept)

    def test_a_decision_while_the_actor_works_is_held_until_it_parks(self, tmp_path) -> None:
        store = _store(tmp_path)
        busy = [True, True, False, False]
        woken = []
        answers = ["SUMMON: 看一下刚写好的 quicksort.py", "SILENT", "SILENT"]

        def post(payload, base_url, api_key):
            return {"content": [{"type": "text", "text": answers.pop(0)}]}

        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: busy.pop(0) if busy else False,
            act=woken.append,
            stop=lambda: False,
            sleep=lambda seconds: None,
            post=post,
        )
        # Decided while the actor was working, said once it parked, and only once.
        assert [look.line for look in woken] == ["@critic 看一下刚写好的 quicksort.py"]

    def test_silence_all_the_way_through_wakes_nobody(self, tmp_path) -> None:
        store = _store(tmp_path)
        busy = [True, False]
        woken = []
        reviewer_harness.watch(
            store, "assistant",
            actor_busy=lambda: busy.pop(0) if busy else False,
            act=woken.append,
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
            act=lambda look: looks.append(look.line),
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


class TestTheRulesItCarries:
    """The assistant carries viba rules too: the first one is an owed answer."""

    def _meeting_with_an_unanswered_critic(self, root, name, monkeypatch):
        """A meeting whose critic spoke an hour ago and heard nothing back."""
        # The whole meeting is in the past, so the wait is real: the actor wrote
        # at 00:00, the critic answered it at 00:01, and nobody replied after.
        monkeypatch.setattr(m2, "utc_now", lambda: "2026-09-23T00:00:00.000Z")
        store = _store(root, name)
        store.update(agent_name2role={"actor": "actor", "critic": "critic",
                                      "assistant": "assistant"})
        store.append_chat("actor", "写好了", "final")
        monkeypatch.setattr(m2, "utc_now", lambda: "2026-09-23T00:01:00.000Z")
        store.append_chat("critic", "两条规则违规：参数上限、分号折行", "final")
        monkeypatch.undo()
        return store

    def test_an_unanswered_critic_message_is_caught(self, tmp_path, monkeypatch) -> None:
        import assistant_rules

        store = self._meeting_with_an_unanswered_critic(tmp_path, "unanswered", monkeypatch)
        section, report = assistant_rules.review(store, store_dir=tmp_path / "store")

        # One rule objects (nobody answered), the other holds (nothing to review).
        assert [rule["verdict"] for rule in report["rules"]] == ["VIOLATED", "holds", "holds"]
        assert report["rules"][0]["offender"] == "actor"     # who owes the answer
        assert "没回应" in report["rules"][0]["complaint"]
        assert report["citation"]["kind"] == "chat"
        assert report["citation"]["event_id"] == report["message"]["event_id"]
        assert "该催的是" in section

    def test_an_answered_message_is_no_longer_owed(self, tmp_path, monkeypatch) -> None:
        import assistant_rules

        store = self._meeting_with_an_unanswered_critic(tmp_path, "answered", monkeypatch)
        store.append_chat("actor", "收到，我改", "ack")
        _section, report = assistant_rules.review(store, store_dir=tmp_path / "store")
        # Answered, but the reply has not been reviewed: that is the other rule.
        assert [rule["verdict"] for rule in report["rules"]] == ["holds", "VIOLATED", "holds"]

    def test_the_rule_cannot_run_without_a_crafted_case(self, tmp_path) -> None:
        """A rule is a program: the case is material, and nothing is baked in."""
        import assistant_rules

        rule = assistant_rules.RULES[0]
        assert "unanswered_message_under_judgment" in rule.read_text(encoding="utf-8")
        assert "limit = 30" in rule.read_text(encoding="utf-8")

    def test_a_prompt_that_asks_for_what_a_rule_forbids_ends_the_meeting(
            self, tmp_path, monkeypatch) -> None:
        """The human's own words can ask for a violation; then nothing is owed."""
        import assistant_rules
        from reviewer_harness import _forced_by_rules

        monkeypatch.setattr(m2, "utc_now", lambda: "2026-09-23T00:00:00.000Z")
        store = _store(tmp_path, "conflicting-prompt")
        monkeypatch.setattr(m2, "utc_now", lambda: "2026-09-23T00:01:00.000Z")
        store.append_chat(
            "human",
            "写一个快速排序：接口必须是 quick_sort(items, key=None, reverse=False, lo=0, hi=None)。",
            "request")
        monkeypatch.undo()
        store.append_chat("critic", "规则 parameter_count_le_4 违规，函数 quick_sort", "final")

        section, report = assistant_rules.review(store, store_dir=tmp_path / "store")

        verdicts = {rule["rule"]: rule["verdict"] for rule in report["rules"]}
        assert verdicts["human_prompt_conflicts_le_0"] == "VIOLATED"
        assert "quick_sort(items, key=None, reverse=False, lo=0, hi=None)" in section
        # Before the critic has been heard the review still has to happen: the
        # conflict is not a reason to skip it, only a reason not to chase the actor.
        action, line = _forced_by_rules(report, "critic", "actor", reviewed=False)
        assert action == "summon" and "请复核" in line

        # Once the critic has spoken, the meeting can end on the human's terms —
        # and it says so to the human, not to itself.
        action, line = _forced_by_rules(report, "critic", "actor", reviewed=True)
        assert action == "done"
        assert line.startswith("你的要求「")
        assert "这一轮按你的要求算完成" in line

    def test_the_look_carries_the_rules_and_asks_the_actor(self, tmp_path, monkeypatch) -> None:
        store = self._meeting_with_an_unanswered_critic(tmp_path, "remind", monkeypatch)
        seen = {}

        def post(payload, base_url, api_key):
            seen["asked"] = payload["messages"][0]["content"]
            return {"content": [{"type": "text", "text": "REMIND: 请回应 critic 的两条违规"}]}

        look = reviewer_harness.assistant_turn(store, "assistant", post=post)

        assert look.action == "remind"
        assert look.line.startswith("@actor ")
        assert look.case                                    # the message it is about
        assert "该催的是" in seen["asked"]                   # the rules reached it
