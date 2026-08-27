import unittest

from typing_policy import PROFILE_SPECS, TYPING_PROFILES, plan_input


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

    def test_identifier_bursts_and_semantic_pause_follow_profile(self):
        plan = plan_input(
            [{"kind": "literal", "value": "result=value+other;next_call()"}],
            "balanced",
            "meeting",
            "action",
            "vim",
        )
        spec = PROFILE_SPECS["balanced"]
        identifier_steps = [
            step for step in plan.steps if step.value and all(ch.isalnum() or ch == "_" for ch in step.value)
        ]
        self.assertTrue(identifier_steps)
        self.assertTrue(all(len(step.value) <= spec.burst_max for step in identifier_steps))
        semicolon = next(step for step in plan.steps if step.value.endswith(";"))
        self.assertGreaterEqual(semicolon.delay_after_ms, spec.semantic_min_ms)

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
