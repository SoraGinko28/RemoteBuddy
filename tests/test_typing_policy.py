import unittest

from typing_policy import ENTER_WIND_UP_MS, PROFILE_SPECS, TYPING_PROFILES, plan_input


class TypingPolicyTest(unittest.TestCase):
    def test_profiles_are_deterministic_and_preserve_exact_text(self):
        text = "  print('你好',  user_name)\t# note"
        tokens = [{"kind": "text", "value": text}]
        balanced = None
        for profile in TYPING_PROFILES:
            first = plan_input(tokens, profile, "meeting-a", "action-a", "code")
            second = plan_input(tokens, profile, "meeting-a", "action-a", "code")
            self.assertEqual(first, second)
            self.assertEqual(text, "".join(step.value for step in first.steps))
            self.assertTrue(all(20 <= step.delay_after_ms <= 500 for step in first.steps))
            self.assertFalse(any(step.value == "Backspace" for step in first.steps))
            if profile == "balanced":
                balanced = first

        changed = plan_input(tokens, "balanced", "meeting-a", "action-b", "code")
        self.assertNotEqual(balanced.steps, changed.steps)

    def test_one_literal_is_one_line_and_the_return_keeps_the_profile_pace(self):
        """A line goes out whole; the return after it is only the profile's short
        pause. A longer hold is the actor's to ask for with `$rest`, not the
        harness's to impose on every line."""
        tokens = [
            {"kind": "text", "value": "def quick_sort(items):"},
            {"kind": "ctrl", "value": "Enter"},
            {"kind": "text", "value": "    return items"},
            {"kind": "ctrl", "value": "Enter"},
        ]
        plan = plan_input(tokens, "fast", "meeting", "action", "code", allow_paste=False)
        self.assertEqual(["text", "ctrl", "text", "ctrl"],
                         [step.kind for step in plan.steps])
        self.assertEqual(["def quick_sort(items):", "Enter", "    return items", "Enter"],
                         [step.value for step in plan.steps])
        spec = PROFILE_SPECS["fast"]
        for enter in (step for step in plan.steps if step.kind == "key"):
            self.assertGreaterEqual(enter.delay_after_ms, spec.semantic_min_ms)
            self.assertLessEqual(enter.delay_after_ms, spec.semantic_max_ms)

    def test_an_actor_can_ask_for_a_longer_hold_with_a_rest(self):
        """`$rest` is how the actor sets the pace it wants; it holds the plan."""
        plan = plan_input(
            [
                {"kind": "text", "value": "def quick_sort(items):"},
                {"kind": "rest", "value": 400},
                {"kind": "ctrl", "value": "Enter"},
            ],
            "fast",
            "meeting",
            "action",
            "code",
            allow_paste=False,
        )
        rest = next(step for step in plan.steps if step.kind == "rest")
        self.assertEqual(400, rest.delay_after_ms)
        self.assertEqual("rest", rest.change_kind)

    def test_a_line_that_ends_on_a_boundary_holds_a_little_longer(self):
        """The profile's semantic pause still lands, now after the whole line."""
        spec = PROFILE_SPECS["balanced"]
        plain = plan_input([{"kind": "text", "value": "value = other"}],
                           "balanced", "m", "a", "code", allow_paste=False)
        boundary = plan_input([{"kind": "text", "value": "value = other;"}],
                              "balanced", "m", "a", "code", allow_paste=False)
        self.assertGreaterEqual(boundary.steps[0].delay_after_ms, spec.semantic_min_ms)
        self.assertGreater(boundary.steps[0].delay_after_ms, plain.steps[0].delay_after_ms)

    def test_long_code_switches_to_bounded_paste(self):
        text = "value = '" + ("abc " * 130) + "'"
        plan = plan_input(
            [{"kind": "text", "value": text}],
            "balanced",
            "meeting",
            "long-action",
            "code",
        )
        self.assertTrue(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps))
        self.assertTrue(all(len(step.value) <= 80 for step in plan.steps))
        self.assertTrue(all(20 <= step.delay_after_ms <= 50 for step in plan.steps))
        self.assertLess(plan.planned_duration_ms, 20_000)

    def test_long_code_can_be_forced_to_type_every_burst(self):
        text = "value = '" + ("abc " * 130) + "'"
        plan = plan_input(
            [{"kind": "text", "value": text}],
            "balanced",
            "meeting",
            "slow-action",
            "code",
            allow_paste=False,
        )
        self.assertFalse(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps))
        self.assertTrue(all(step.change_kind == "typing" for step in plan.steps))

    def test_the_line_holds_before_enter(self):
        """回车有前摇：一行打完先停 ENTER_WIND_UP_MS，再按回车。

        前摇挂在回车前那一步上（计划是"发一步、停它的 delay、再发下一步"），
        回车自己后面那口气仍是 profile 的。
        """
        plan = plan_input(
            [
                {"kind": "text", "value": "def bubble_sort(items):"},
                {"kind": "ctrl", "value": "Enter"},
                {"kind": "text", "value": "    return items"},
            ],
            "fast",
            "meeting",
            "action",
            "keystrokes",
            allow_paste=False,
        )
        self.assertEqual(ENTER_WIND_UP_MS, plan.steps[0].delay_after_ms)
        self.assertEqual("ctrl", plan.steps[1].kind)
        spec = PROFILE_SPECS["fast"]
        self.assertGreaterEqual(plan.steps[1].delay_after_ms, spec.semantic_min_ms)
        self.assertLessEqual(plan.steps[1].delay_after_ms, spec.semantic_max_ms)
        # 最后那行后面没有回车，就还是 profile 的短停顿。
        self.assertLess(plan.steps[2].delay_after_ms, ENTER_WIND_UP_MS)

    def test_rest_pauses_before_enter_without_sending_keys(self):
        plan = plan_input(
            [
                {"kind": "text", "value": "echo hi"},
                {"kind": "rest", "value": 200},
                {"kind": "ctrl", "value": "Enter"},
            ],
            "balanced",
            "meeting",
            "action",
            "shell",
            allow_paste=False,
        )
        kinds = [step.kind for step in plan.steps]
        self.assertIn("rest", kinds)
        rest_index = kinds.index("rest")
        rest_step = plan.steps[rest_index]
        self.assertEqual("rest", rest_step.change_kind)
        self.assertEqual("", rest_step.value)
        self.assertEqual(200, rest_step.delay_after_ms)
        self.assertEqual("ctrl", kinds[rest_index + 1])
        self.assertEqual("Enter", plan.steps[rest_index + 1].value)
        # Rests never add characters or count as paste.
        self.assertEqual(
            "echo hi",
            "".join(step.value for step in plan.steps if step.kind == "text"),
        )
        self.assertEqual(7, plan.total_characters)
        self.assertFalse(plan.uses_paste)
        self.assertGreaterEqual(plan.planned_duration_ms, 200)

    def test_command_never_uses_paste(self):
        text = "python -- " + "very-long-path/" * 30 + "notes.md"
        plan = plan_input(
            [{"kind": "text", "value": text}, {"kind": "ctrl", "value": "Enter"}],
            "fast",
            "meeting",
            "command",
            "shell",
            allow_paste=False,
        )
        self.assertFalse(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps if step.kind == "text"))


if __name__ == "__main__":
    unittest.main()
