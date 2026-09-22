import unittest

from typing_policy import (LINE_PAUSE_MAX_MS, LINE_PAUSE_MIN_MS, PROFILE_SPECS,
                           TYPING_PROFILES, plan_input)


class TypingPolicyTest(unittest.TestCase):
    def test_profiles_are_deterministic_and_preserve_exact_text(self):
        text = "  print('你好',  user_name)\t# note"
        tokens = [{"kind": "literal", "value": text}]
        balanced = None
        for profile in TYPING_PROFILES:
            first = plan_input(tokens, profile, "meeting-a", "action-a", "vim")
            second = plan_input(tokens, profile, "meeting-a", "action-a", "vim")
            self.assertEqual(first, second)
            self.assertEqual(text, "".join(step.value for step in first.steps))
            self.assertTrue(all(20 <= step.delay_after_ms <= 500 for step in first.steps))
            self.assertFalse(any(step.value == "Backspace" for step in first.steps))
            if profile == "balanced":
                balanced = first

        changed = plan_input(tokens, "balanced", "meeting-a", "action-b", "vim")
        self.assertNotEqual(balanced.steps, changed.steps)

    def test_one_literal_is_one_line_and_enter_holds_the_line_pause(self):
        """A line goes out whole, and the return after it pauses about 0.4s:
        one step per line, one pause per return."""
        tokens = [
            {"kind": "literal", "value": "def quick_sort(items):"},
            {"kind": "key", "value": "Enter"},
            {"kind": "literal", "value": "    return items"},
            {"kind": "key", "value": "Enter"},
        ]
        plan = plan_input(tokens, "fast", "meeting", "action", "vim", allow_paste=False)
        self.assertEqual(["literal", "key", "literal", "key"],
                         [step.kind for step in plan.steps])
        self.assertEqual(["def quick_sort(items):", "Enter", "    return items", "Enter"],
                         [step.value for step in plan.steps])
        for enter in (step for step in plan.steps if step.kind == "key"):
            self.assertGreaterEqual(enter.delay_after_ms, LINE_PAUSE_MIN_MS)
            self.assertLessEqual(enter.delay_after_ms, LINE_PAUSE_MAX_MS)

    def test_a_line_that_ends_on_a_boundary_holds_a_little_longer(self):
        """The profile's semantic pause still lands, now after the whole line."""
        spec = PROFILE_SPECS["balanced"]
        plain = plan_input([{"kind": "literal", "value": "value = other"}],
                           "balanced", "m", "a", "vim", allow_paste=False)
        boundary = plan_input([{"kind": "literal", "value": "value = other;"}],
                              "balanced", "m", "a", "vim", allow_paste=False)
        self.assertGreaterEqual(boundary.steps[0].delay_after_ms, spec.semantic_min_ms)
        self.assertGreater(boundary.steps[0].delay_after_ms, plain.steps[0].delay_after_ms)

    def test_long_code_switches_to_bounded_paste(self):
        text = "value = '" + ("abc " * 130) + "'"
        plan = plan_input(
            [{"kind": "literal", "value": text}],
            "balanced",
            "meeting",
            "long-action",
            "vim",
        )
        self.assertTrue(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps))
        self.assertTrue(all(len(step.value) <= 80 for step in plan.steps))
        self.assertTrue(all(20 <= step.delay_after_ms <= 50 for step in plan.steps))
        self.assertLess(plan.planned_duration_ms, 20_000)

    def test_long_code_can_be_forced_to_type_every_burst(self):
        text = "value = '" + ("abc " * 130) + "'"
        plan = plan_input(
            [{"kind": "literal", "value": text}],
            "balanced",
            "meeting",
            "slow-action",
            "vim",
            allow_paste=False,
        )
        self.assertFalse(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps))
        self.assertTrue(all(step.change_kind == "typing" for step in plan.steps))

    def test_editor_commands_and_special_keys_are_not_converted_to_paste(self):
        tokens = [
            {"kind": "literal", "value": "i"},
            {"kind": "literal", "value": "x" * 300},
            {"kind": "key", "value": "Escape"},
            {"kind": "literal", "value": ":wq"},
            {"kind": "key", "value": "Enter"},
        ]
        plan = plan_input(tokens, "balanced", "meeting", "action", "vim")
        self.assertEqual("i", plan.steps[0].value)
        self.assertEqual("editor_command", plan.steps[0].input_context)
        self.assertTrue(any(step.change_kind == "paste" for step in plan.steps))
        self.assertEqual(["Escape", "Enter"], [step.value for step in plan.steps if step.kind == "key"])
        self.assertIn(":wq", "".join(step.value for step in plan.steps))

    def test_rest_pauses_before_enter_without_sending_keys(self):
        plan = plan_input(
            [
                {"kind": "literal", "value": "echo hi"},
                {"kind": "rest", "value": 200},
                {"kind": "key", "value": "Enter"},
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
        self.assertEqual("key", kinds[rest_index + 1])
        self.assertEqual("Enter", plan.steps[rest_index + 1].value)
        # Rests never add characters or count as paste.
        self.assertEqual(
            "echo hi",
            "".join(step.value for step in plan.steps if step.kind == "literal"),
        )
        self.assertEqual(7, plan.total_characters)
        self.assertFalse(plan.uses_paste)
        self.assertGreaterEqual(plan.planned_duration_ms, 200)

    def test_command_never_uses_paste(self):
        text = "vim -- " + "very-long-path/" * 30 + "notes.md"
        plan = plan_input(
            [{"kind": "literal", "value": text}, {"kind": "key", "value": "Enter"}],
            "fast",
            "meeting",
            "command",
            "shell",
            allow_paste=False,
        )
        self.assertFalse(plan.uses_paste)
        self.assertEqual(text, "".join(step.value for step in plan.steps if step.kind == "literal"))


if __name__ == "__main__":
    unittest.main()
