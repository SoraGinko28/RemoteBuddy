import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

from agent_protocol import normalize_model_key_tokens, validate_key_tokens
from model_api_client import (
    AgentAction,
    AgentProtocolError,
    ModelApiConfig,
    ModelApiError,
    ModelConfigError,
    ModelResponseError,
    OpenAIResponsesClient,
    action_json_schema,
)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


def action_payload(**changes):
    value = {
        "type": "chat",
        "message_type": "ack",
        "content": "I will inspect the current note first.",
        "command": "none",
        "path": "",
        "delay_ms": 0,
    }
    value.update(changes)
    return value


class ModelApiConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, extra=""):
        (self.root / "config.toml").write_text(
            'model="gpt-5.6-luna"\n'
            'base_url="https://api.openai.com/v1"\n'
            'request_timeout_seconds=60\n'
            'reasoning_effort="low"\n'
            + extra,
            encoding="utf-8",
        )
        auth = self.root / "auth.json"
        auth.write_text('{"api_key":"secret-test-key"}\n', encoding="utf-8")
        os.chmod(auth, 0o600)

    def test_strict_flat_config_and_secret_repr(self):
        self.write_config()
        config = ModelApiConfig.load(self.root)
        self.assertEqual("gpt-5.6-luna", config.model)
        self.assertEqual(60.0, config.request_timeout_seconds)
        self.assertNotIn("secret-test-key", repr(config))

    def test_unknown_structure_missing_file_and_symlink_are_rejected(self):
        self.write_config("unknown=1\n")
        with self.assertRaisesRegex(ModelConfigError, "unknown config"):
            ModelApiConfig.load(self.root)

        (self.root / "config.toml").write_text("[model]\nname='x'\n", encoding="utf-8")
        with self.assertRaisesRegex(ModelConfigError, "unsupported"):
            ModelApiConfig.load(self.root)

        (self.root / "config.toml").unlink()
        with self.assertRaises(ModelConfigError):
            ModelApiConfig.load(self.root)

        self.write_config()
        target = self.root / "real-auth.json"
        target.write_text('{"api_key":"secret"}', encoding="utf-8")
        (self.root / "auth.json").unlink()
        (self.root / "auth.json").symlink_to(target)
        with self.assertRaisesRegex(ModelConfigError, "regular file"):
            ModelApiConfig.load(self.root)

    def test_codex_responses_profile_is_supported(self):
        (self.root / "config.toml").write_text(
            'model_provider = "OpenAI"\n'
            'model = "gpt-5.6-sol"\n'
            'review_model = "gpt-5.5"\n'
            'model_reasoning_effort = "max"\n'
            'disable_response_storage = true\n'
            'network_access = "enabled"\n'
            'windows_wsl_setup_acknowledged = true\n'
            'notify = ["tool", "turn-ended"]\n'
            '\n'
            '[model_providers.OpenAI]\n'
            'name = "OpenAI"\n'
            'base_url = "https://rust.cat"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n',
            encoding="utf-8",
        )
        auth = self.root / "auth.json"
        auth.write_text('{"api_key":"secret-test-key"}\n', encoding="utf-8")
        os.chmod(auth, 0o600)
        config = ModelApiConfig.load(self.root)
        self.assertEqual("gpt-5.6-sol", config.model)
        self.assertEqual("https://rust.cat", config.base_url)
        self.assertEqual("max", config.reasoning_effort)
        self.assertEqual(60.0, config.request_timeout_seconds)
        self.assertFalse(config.store_responses)

    def test_openai_environment_style_auth_key_is_supported(self):
        self.write_config()
        auth = self.root / "auth.json"
        auth.write_text('{"OPENAI_API_KEY":"secret-test-key"}\n', encoding="utf-8")
        os.chmod(auth, 0o600)
        config = ModelApiConfig.load(self.root)
        self.assertEqual("secret-test-key", config.api_key)

    def test_auth_key_aliases_cannot_be_combined(self):
        self.write_config()
        (self.root / "auth.json").write_text(
            '{"api_key":"one","OPENAI_API_KEY":"two"}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(ModelConfigError, "only api_key"):
            ModelApiConfig.load(self.root)

    def test_codex_profile_rejects_non_responses_wire_api(self):
        self.write_config()
        (self.root / "config.toml").write_text(
            'model_provider = "OpenAI"\nmodel = "gpt-5.6-sol"\n'
            'model_reasoning_effort = "low"\n'
            '[model_providers.OpenAI]\n'
            'base_url = "https://example.com"\nwire_api = "chat"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ModelConfigError, "wire_api"):
            ModelApiConfig.load(self.root)

    def test_missing_key_defers_to_selection_time(self):
        # A responses-default config with no auth.json still loads (with a
        # warning) so key-less installs can select codex-app-server; the key
        # is enforced when the responses combination is actually resolved.
        self.write_config()
        (self.root / "auth.json").unlink()
        config = ModelApiConfig.load(self.root)
        self.assertEqual("", config.api_key)
        self.assertTrue(
            any("auth.json is missing OPENAI_API_KEY" in warning for warning in config.warnings)
        )
        with self.assertRaisesRegex(ModelConfigError, "auth.json is missing OPENAI_API_KEY"):
            config.resolve("responses", "openai")
        resolved = config.resolve("codex-app-server", "openai")
        self.assertEqual("codex-app-server", resolved.agent_backend)

    def test_trae_agent_backend_pairs_with_openai_only(self):
        self.write_config()
        config = ModelApiConfig.load(self.root)
        resolved = config.resolve("trae-agent", "openai")
        self.assertEqual("trae-agent", resolved.agent_backend)
        with self.assertRaisesRegex(ModelConfigError, "trae-agent"):
            config.resolve("trae-agent", "deepseek")
        # Like codex, trae authenticates via the CLI login: no key required.
        (self.root / "auth.json").unlink()
        keyless = ModelApiConfig.load(self.root)
        self.assertEqual("", keyless.resolve("trae-agent", "openai").api_key)
        # Empty model defers to the CLI default for trae as well.
        self.assertEqual("", config.resolve("trae-agent", "openai", model="").model)

    def test_empty_model_is_codex_only_cli_default(self):
        self.write_config()
        config = ModelApiConfig.load(self.root)
        resolved = config.resolve("codex-app-server", "openai", model="")
        self.assertEqual("", resolved.model)
        # For other backends the empty string keeps today's falsy fallback to
        # the config default.
        fallback = config.resolve("responses", "openai", model="")
        self.assertEqual("gpt-5.6-luna", fallback.model)

    def test_backend_flags_and_codex_login_auth_mode(self):
        self.write_config(
            'agent_backend="codex-app-server"\n'
            'codex_dynamic_tools=true\n'
        )
        (self.root / "auth.json").unlink()
        config = ModelApiConfig.load(self.root)
        self.assertEqual("codex-app-server", config.agent_backend)
        self.assertTrue(config.codex_dynamic_tools)
        self.assertEqual("", config.api_key)

        # 配置里的默认后端不是 codex/trae 时它只是搁着：选中的后端在 resolve 里判。
        self.write_config('codex_dynamic_tools=true\n')
        config = ModelApiConfig.load(self.root)
        self.assertTrue(config.codex_dynamic_tools)
        self.assertFalse(config.resolve("responses", "openai").codex_dynamic_tools)
        self.assertTrue(config.resolve("trae-agent", "openai").codex_dynamic_tools)

    def test_existing_config_defaults_to_responses(self):
        self.write_config()
        config = ModelApiConfig.load(self.root)
        self.assertEqual("responses", config.agent_backend)
        self.assertFalse(config.codex_dynamic_tools)

    def test_provider_catalog_resolves_claude_code_deepseek(self):
        (self.root / "config.toml").write_text(
            'default_backend="responses"\n'
            'default_provider="openai"\n'
            'request_timeout_seconds=120\n'
            'reasoning_effort="max"\n'
            '[providers.openai]\n'
            'base_url="https://api.openai.com/v1"\n'
            'wire_api="responses"\n'
            'auth_key="OPENAI_API_KEY"\n'
            'model="gpt-5.6-luna"\n'
            '[providers.deepseek]\n'
            'base_url="https://api.deepseek.com/anthropic"\n'
            'wire_api="anthropic"\n'
            'auth_key="DEEPSEEK_API_KEY"\n'
            'model="deepseek-v4-pro[1m]"\n'
            'fast_model="deepseek-v4-flash"\n'
            '[harnesses.claude-code]\n'
            'binary="claude-custom"\n',
            encoding="utf-8",
        )
        auth = self.root / "auth.json"
        auth.write_text(
            '{"OPENAI_API_KEY":"openai-secret","DEEPSEEK_API_KEY":"deepseek-secret"}\n',
            encoding="utf-8",
        )
        os.chmod(auth, 0o600)
        config = ModelApiConfig.load(self.root)
        deepseek = config.resolve("claude-code", "deepseek")
        self.assertEqual("responses", config.agent_backend)
        self.assertEqual("openai", config.agent_provider)
        self.assertEqual("claude-code", deepseek.agent_backend)
        self.assertEqual("deepseek", deepseek.agent_provider)
        self.assertEqual("deepseek-v4-pro[1m]", deepseek.model)
        self.assertEqual("deepseek-v4-flash", deepseek.fast_model)
        self.assertEqual("https://api.deepseek.com/anthropic", deepseek.base_url)
        self.assertEqual("claude-custom", deepseek.claude_binary)
        self.assertNotIn("openai-secret", repr(config))
        self.assertNotIn("deepseek-secret", repr(config))

    def test_deepseek_model_suffix_is_allowed_but_shell_syntax_is_not(self):
        self.write_config()
        config_path = self.root / "config.toml"
        config_path.write_text(
            config_path.read_text("utf-8").replace("gpt-5.6-luna", "deepseek-v4-pro[1m]"),
            encoding="utf-8",
        )
        self.assertEqual("deepseek-v4-pro[1m]", ModelApiConfig.load(self.root).model)
        config_path.write_text(
            config_path.read_text("utf-8").replace("deepseek-v4-pro[1m]", "model;touch-x"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ModelConfigError, "model"):
            ModelApiConfig.load(self.root)


class AgentActionTest(unittest.TestCase):
    def test_action_semantics_and_the_action_types(self):
        action = AgentAction.from_dict(action_payload())
        self.assertEqual("ack", action.message_type)
        command = AgentAction.from_dict(
            action_payload(
                type="command",
                message_type="none",
                content="",
                command="python3 -m pytest -q",
            )
        )
        self.assertEqual("python3 -m pytest -q", command.command)
        patch = (
            "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1,2 @@\n-old\n+new\n keep\n"
            "--- a/other/thing.md\n+++ b/other/thing.md\n@@ -3,1 +3,1 @@\n-x\n+y\n"
        )
        diff = AgentAction.from_dict(
            action_payload(type="diff", message_type="none", content="", command="none", diff=patch)
        )
        self.assertEqual(patch, diff.diff)
        with self.assertRaises(ModelResponseError):
            AgentAction.from_dict(action_payload(type="command", content="rm -rf ."))
        far_away = AgentAction.from_dict(
            action_payload(
                type="diff", message_type="none", content="", command="none",
                diff="--- a/a.py\n+++ b/a.py\n@@ -9,1 +9,1 @@\n-nope\n+yep\n",
            )
        )
        self.assertIn("-nope", far_away.diff)
        with self.assertRaisesRegex(ModelResponseError, "unified diff"):
            AgentAction.from_dict(
                action_payload(
                    type="diff", message_type="none", content="", command="none",
                    diff="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n只有一行没有前缀\n",
                )
            )
        with self.assertRaisesRegex(ModelResponseError, "keystrokes action is gone"):
            AgentAction.from_dict(
                action_payload(
                    type="command",
                    message_type="none",
                    content="",
                    command="git status",
                    keys=[{"kind": "ctrl", "value": "Enter"}],
                )
            )
        with self.assertRaises(ModelResponseError):
            AgentAction.from_dict(action_payload(), allowed_types=("command",))

    def test_a_diff_must_be_plain_text_with_unix_line_endings(self):
        with self.assertRaisesRegex(ModelResponseError, "unix line endings"):
            AgentAction.from_dict(
                action_payload(
                    type="diff", message_type="none", content="", command="none",
                    diff="--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\r\n+b\n",
                )
            )
        with self.assertRaisesRegex(ModelResponseError, "unified diff"):
            AgentAction.from_dict(
                action_payload(
                    type="diff", message_type="none", content="", command="none",
                    diff="把这两行改掉就行",
                )
            )

    def test_a_diff_may_not_be_empty(self):
        with self.assertRaisesRegex(ModelResponseError, "requires a unified diff"):
            AgentAction.from_dict(
                action_payload(type="diff", message_type="none", content="", command="none")
            )

    def test_empty_fields_mean_unused_and_stray_fields_are_named(self):
        """`""`/`null` 是"这个字段没用"，不是"我想用它"；真多带的字段要指名。"""
        patch = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        action = AgentAction.from_dict(action_payload(
            type="diff",
            message_type=None,
            content=None,
            command="",
            path=None,
            delay_ms=None,
            diff=patch,
        ))
        self.assertEqual(patch, action.diff)
        self.assertEqual("none", action.message_type)
        with self.assertRaisesRegex(ModelResponseError, "unrelated fields: content"):
            AgentAction.from_dict(action_payload(
                type="diff",
                message_type="none",
                content="我不该在这里",
                command="none",
                diff=patch,
            ))

    def test_key_dsl_is_normalized_for_the_takeover_path(self):
        """人从页面上接管时发的按键还走这套 DSL；动作里已经没有它了。"""
        keys = normalize_model_key_tokens([
            "$text echo ",
            "$text '",
            "$text 1",
            "$text '",
            "$ctrl Left",
            "$text  ",
            "$ctrl Enter",
        ])
        self.assertEqual(
            [
                {"kind": "text", "value": "echo "},
                {"kind": "text", "value": "'"},
                {"kind": "text", "value": "1"},
                {"kind": "text", "value": "'"},
                {"kind": "ctrl", "value": "Left"},
                {"kind": "text", "value": " "},
                {"kind": "ctrl", "value": "Enter"},
            ],
            keys,
        )

    def test_visible_newlines_in_a_key_become_enter(self):
        keys = normalize_model_key_tokens([
            {"kind": "text", "value": "first\nsecond"},
        ])
        self.assertEqual(
            [
                {"kind": "text", "value": "first"},
                {"kind": "ctrl", "value": "Enter"},
                {"kind": "text", "value": "second"},
            ],
            keys,
        )

    def test_rest_tokens_are_bounds_checked(self):
        keys = normalize_model_key_tokens([
            "$text echo hi",
            "$rest 200",
            {"kind": "rest", "value": 350},
            "$ctrl Enter",
        ])
        self.assertEqual(
            [
                {"kind": "text", "value": "echo hi"},
                {"kind": "rest", "value": 200},
                {"kind": "rest", "value": 350},
                {"kind": "ctrl", "value": "Enter"},
            ],
            keys,
        )
        with self.assertRaisesRegex(AgentProtocolError, "millisecond integer"):
            normalize_model_key_tokens(["$rest soon"])
        with self.assertRaisesRegex(AgentProtocolError, "between 20 and 2000"):
            validate_key_tokens([{"kind": "rest", "value": 30000}])
        with self.assertRaisesRegex(AgentProtocolError, "between 20 and 2000"):
            validate_key_tokens(normalize_model_key_tokens(["$rest 5"]))

    def test_schema_can_force_first_ack_or_question(self):
        schema = action_json_schema(("chat",), ("ack", "question"))
        variants = schema["properties"]["action"]["anyOf"]
        self.assertEqual(1, len(variants))
        self.assertEqual(["chat"], variants[0]["properties"]["type"]["enum"])
        self.assertEqual(["ack", "question"], variants[0]["properties"]["message_type"]["enum"])
        self.assertEqual(["none"], variants[0]["properties"]["command"]["enum"])
        self.assertEqual(["action"], schema["required"])
        self.assertFalse(schema["additionalProperties"])

        full_schema = action_json_schema()
        variants = full_schema["properties"]["action"]["anyOf"]
        kinds = sorted({variant["properties"]["type"]["enum"][0] for variant in variants})
        self.assertEqual(["chat", "command", "diff", "done", "observe"], kinds)
        diff_variant = [
            variant for variant in variants
            if variant["properties"]["type"]["enum"] == ["diff"]
        ][0]
        self.assertEqual({"type": "string", "description": diff_variant["properties"]["diff"]["description"],
                          "minLength": 1}, diff_variant["properties"]["diff"])
        self.assertIn("unified diff", diff_variant["properties"]["diff"]["description"])
        for variant in variants:
            self.assertNotIn("keys", variant["properties"])


class ResponsesClientTest(unittest.TestCase):
    def setUp(self):
        self.config = ModelApiConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            request_timeout_seconds=60.0,
            reasoning_effort="low",
            api_key="secret-test-key",
            warnings=(),
        )

    def test_conversation_and_structured_response_request(self):
        calls = []
        responses = [
            {"id": "resp_123", "output_text": json.dumps(action_payload())},
        ]

        def opener(request, timeout):
            calls.append((request, timeout, json.loads(request.data.decode("utf-8"))))
            return FakeResponse(responses.pop(0))

        client = OpenAIResponsesClient(self.config, opener=opener)
        conversation_id = client.create_conversation("meeting-one")
        response = client.create_action(
            conversation_id,
            "skill instructions",
            "screen observation",
            allowed_types=("chat",),
            allowed_message_types=("ack", "question"),
            request_timeout_seconds=2.5,
        )

        self.assertEqual("local-responses-meeting-one", conversation_id)
        self.assertEqual("resp_123", response.response_id)
        self.assertEqual(1, len(calls))
        request_payload = calls[0][2]
        self.assertNotIn("conversation", request_payload)
        self.assertIsInstance(request_payload["input"], str)
        self.assertNotIn("tools", request_payload)
        self.assertNotIn("tool_choice", request_payload)
        self.assertEqual({"effort": "low"}, request_payload["reasoning"])
        self.assertEqual(8192, request_payload["max_output_tokens"])
        self.assertTrue(request_payload["store"])
        self.assertTrue(request_payload["text"]["format"]["strict"])
        self.assertIn("anyOf", request_payload["text"]["format"]["schema"]["properties"]["action"])
        self.assertEqual(2.5, calls[0][1])
        self.assertNotIn("secret-test-key", json.dumps(request_payload))
        self.assertEqual("RemoteBuddy/0.2", calls[0][0].get_header("User-agent"))

    def test_identical_gateway_action_duplication_is_accepted(self):
        nested = {"action": action_payload()}
        duplicated = json.dumps(nested) + json.dumps(nested)

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse({"id": "resp_duplicate", "output_text": duplicated})

        response = OpenAIResponsesClient(self.config, opener=opener).create_action(
            "local-responses-meeting-one", "instructions", "observation"
        )
        self.assertEqual("ack", response.action.message_type)

    def test_different_or_decorated_actions_are_rejected(self):
        different = json.dumps(action_payload()) + json.dumps(
            action_payload(content="A different action")
        )
        outputs = [different, "```json\n" + json.dumps(action_payload()) + "\n```"]

        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse({"id": "resp_invalid", "output_text": outputs.pop(0)})

        client = OpenAIResponsesClient(self.config, opener=opener)
        with self.assertRaisesRegex(ModelResponseError, "multiple different"):
            client.create_action("local", "instructions", "observation")
        with self.assertRaisesRegex(ModelResponseError, "not valid action JSON"):
            client.create_action("local", "instructions", "observation")

    def test_http_error_is_not_retried_and_redacts_key(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = io.BytesIO(
                json.dumps({"error": {"message": "bad secret-test-key"}}).encode("utf-8")
            )
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, body)

        client = OpenAIResponsesClient(self.config, opener=opener)
        with self.assertRaises(ModelApiError) as context:
            client.create_action("local-responses-meeting-one", "instructions", "observation")
        self.assertEqual(1, len(calls))
        self.assertNotIn("secret-test-key", str(context.exception))
        self.assertIn("[redacted]", str(context.exception))

    def test_timeout_and_connection_errors_are_distinguishable(self):
        timeout_outcomes = [TimeoutError("timed out")]
        reset_outcomes = [
            urllib.error.URLError(ConnectionResetError("reset")) for _ in range(3)
        ]

        def timeout_opener(_request, timeout):
            self.assertGreater(timeout, 0)
            raise timeout_outcomes.pop(0)

        def reset_opener(_request, timeout):
            self.assertGreater(timeout, 0)
            raise reset_outcomes.pop(0)

        # A timeout consumed the whole request budget and is not retried.
        client = OpenAIResponsesClient(self.config, opener=timeout_opener)
        with self.assertRaisesRegex(ModelApiError, r"timed out .*60s"):
            client.create_action("local", "instructions", "observation")
        self.assertEqual([], timeout_outcomes)
        # Transient connection failures are retried before surfacing.
        client = OpenAIResponsesClient(
            self.config, opener=reset_opener, retry_base_seconds=0.001
        )
        with self.assertRaisesRegex(ModelApiError, "connection failed: ConnectionResetError: reset"):
            client.create_action("local", "instructions", "observation")
        self.assertEqual([], reset_outcomes)

    def test_retryable_http_status_is_retried_until_success(self):
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            if len(calls) < 3:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "throttled", {"Retry-After": "0"}, io.BytesIO(b"{}")
                )
            return FakeResponse({"id": "resp_retry", "output_text": json.dumps(action_payload())})

        client = OpenAIResponsesClient(self.config, opener=opener, retry_base_seconds=0.001)
        response = client.create_action("local", "instructions", "observation")
        self.assertEqual("resp_retry", response.response_id)
        self.assertEqual(3, len(calls))

    def test_gateway_top_level_error_format_is_reported(self):
        def opener(request, timeout):
            body = io.BytesIO(
                json.dumps({"code": "MODEL_FORBIDDEN", "message": "model is not enabled"}).encode(
                    "utf-8"
                )
            )
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, body)

        client = OpenAIResponsesClient(self.config, opener=opener)
        with self.assertRaisesRegex(ModelApiError, "MODEL_FORBIDDEN: model is not enabled"):
            client.create_action("local-responses-meeting-one", "instructions", "observation")

    def test_incomplete_response_is_rejected_even_if_it_contains_json(self):
        def opener(_request, timeout):
            self.assertGreater(timeout, 0)
            return FakeResponse(
                {
                    "id": "resp_incomplete",
                    "status": "incomplete",
                    "output_text": json.dumps(action_payload()),
                }
            )

        client = OpenAIResponsesClient(self.config, opener=opener)
        with self.assertRaisesRegex(ModelApiError, "did not complete"):
            client.create_action("conv_123", "instructions", "observation")


if __name__ == "__main__":
    unittest.main()


class InputWithImagesTest(unittest.TestCase):
    """Responses 的 input：一张图进模型就是一张图（input_image + data URL）。"""

    def test_a_stored_picture_becomes_an_input_image(self):
        import base64
        import tempfile
        from pathlib import Path

        from model_api_client import _input_with_images

        with tempfile.TemporaryDirectory() as name:
            png = Path(name) / "image-1.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nbody")
            built = _input_with_images("看这张", [{"path": str(png), "media_type": "image/png"}])
        self.assertEqual("user", built[0]["role"])
        blocks = built[0]["content"]
        self.assertEqual({"type": "input_text", "text": "看这张"}, blocks[0])
        self.assertEqual("input_image", blocks[1]["type"])
        self.assertTrue(blocks[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(b"\x89PNG\r\n\x1a\nbody",
                         base64.b64decode(blocks[1]["image_url"].split(",", 1)[1]))

    def test_no_pictures_keeps_the_plain_string_input(self):
        from model_api_client import _input_with_images

        self.assertEqual("看这个", _input_with_images("看这个"))
        self.assertEqual("看这个", _input_with_images("看这个", [{"path": "/nowhere/x.png"}]))
