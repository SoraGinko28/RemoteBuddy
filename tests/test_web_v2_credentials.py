import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from web_v2 import MeetingWebService, WebError


class WebV2CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.model_api = self.root / "model_api"
        self.model_api.mkdir()
        (self.model_api / "config.toml").write_text(
            'model="gpt-5.6-luna"\n'
            'base_url="https://api.openai.com/v1"\n'
            'request_timeout_seconds=60\n'
            'reasoning_effort="low"\n',
            encoding="utf-8",
        )
        self.auth = self.model_api / "auth.json"
        self.auth.write_text('{"OPENAI_API_KEY":"secret-openai-key"}\n', encoding="utf-8")
        os.chmod(self.auth, 0o600)
        self.service = MeetingWebService(
            project_root=self.root,
            runner_factory=lambda *args, **kwargs: None,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def read_auth(self):
        return json.loads(self.auth.read_text(encoding="utf-8"))

    def test_saving_deepseek_key_preserves_other_keys_and_never_echoes_secrets(self):
        result = self.service.update_credentials(
            {"provider": "deepseek", "api_key": " secret-deepseek-key "}
        )
        self.assertEqual(
            {"OPENAI_API_KEY": "secret-openai-key", "DEEPSEEK_API_KEY": "secret-deepseek-key"},
            self.read_auth(),
        )
        self.assertEqual(0o600, stat.S_IMODE(self.auth.stat().st_mode))
        self.assertNotIn("secret-deepseek-key", json.dumps(result))
        self.assertNotIn("secret-openai-key", json.dumps(result))
        self.assertTrue(result["providers"]["deepseek"]["has_api_key"])
        self.assertEqual("DEEPSEEK_API_KEY", result["providers"]["deepseek"]["auth_key_name"])
        resolved = self.service._load_config().resolve("claude-code", "deepseek")
        self.assertEqual("secret-deepseek-key", resolved.api_key)
        self.assertEqual("https://api.deepseek.com/anthropic", resolved.base_url)

    def test_legacy_api_key_alias_is_promoted_to_a_dedicated_key(self):
        self.auth.write_text('{"api_key":"legacy-openai-key"}\n', encoding="utf-8")
        self.service.update_credentials({"provider": "deepseek", "api_key": "secret-deepseek-key"})
        self.assertEqual(
            {"OPENAI_API_KEY": "legacy-openai-key", "DEEPSEEK_API_KEY": "secret-deepseek-key"},
            self.read_auth(),
        )
        config = self.service._load_config()
        self.assertEqual("legacy-openai-key", config.resolve("responses", "openai").api_key)

    def test_base_url_override_applies_and_can_be_cleared(self):
        self.service.update_credentials(
            {
                "provider": "deepseek",
                "api_key": "secret-deepseek-key",
                "base_url": "https://gateway.example/anthropic/",
            }
        )
        overrides = json.loads((self.model_api / "overrides.json").read_text(encoding="utf-8"))
        self.assertEqual(
            "https://gateway.example/anthropic",
            overrides["providers"]["deepseek"]["base_url"],
        )
        resolved = self.service._load_config().resolve("claude-code", "deepseek")
        self.assertEqual("https://gateway.example/anthropic", resolved.base_url)
        status = self.service.update_credentials({"provider": "deepseek", "base_url": ""})
        self.assertIsNone(status["providers"]["deepseek"]["base_url_override"])
        resolved = self.service._load_config().resolve("claude-code", "deepseek")
        self.assertEqual("https://api.deepseek.com/anthropic", resolved.base_url)

    def test_default_provider_base_url_override_applies_to_responses_backend(self):
        self.service.update_credentials(
            {"provider": "openai", "base_url": "https://rust.cat"}
        )
        config = self.service._load_config()
        self.assertEqual("https://rust.cat", config.base_url)
        self.assertEqual("https://rust.cat", config.resolve("responses", "openai").base_url)

    def test_invalid_credentials_requests_are_rejected(self):
        cases = [
            ({"provider": "unknown", "api_key": "x"}, 400),
            ({"provider": "deepseek"}, 400),
            ({"provider": "deepseek", "api_key": "  "}, 400),
            ({"provider": "deepseek", "api_key": "bad key"}, 400),
            ({"provider": "deepseek", "api_key": "x" * 5000}, 413),
            ({"provider": "deepseek", "base_url": "http://evil.example"}, 400),
            ({"provider": "deepseek", "base_url": "https://u:p@host"}, 400),
            ({"provider": "deepseek", "api_key": "x", "extra": True}, 400),
        ]
        for payload, status in cases:
            with self.assertRaises(WebError) as caught:
                self.service.update_credentials(payload)
            self.assertEqual(status, caught.exception.status, payload)
        self.assertEqual({"OPENAI_API_KEY": "secret-openai-key"}, self.read_auth())
        self.assertFalse((self.model_api / "overrides.json").exists())

    def test_health_reports_missing_deepseek_key_then_recovers(self):
        health = self.service.health()
        entry = health["backends"]["claude-code/deepseek"]
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["needs_api_key"])
        self.assertEqual("DEEPSEEK_API_KEY", entry["auth_key_name"])
        self.service.update_credentials({"provider": "deepseek", "api_key": "secret-deepseek-key"})
        health = self.service.health()
        entry = health["backends"]["claude-code/deepseek"]
        self.assertFalse(entry["needs_api_key"])
        self.assertTrue(entry["has_api_key"])

    def test_credentials_status_endpoint_shape(self):
        status = self.service.credentials_status()
        self.assertEqual({"openai", "deepseek"}, set(status["providers"]))
        self.assertTrue(status["providers"]["openai"]["has_api_key"])
        self.assertFalse(status["providers"]["deepseek"]["has_api_key"])
        self.assertIsNone(status["providers"]["deepseek"]["base_url_override"])


if __name__ == "__main__":
    unittest.main()
