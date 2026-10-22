"""Auth failures name the key and the fix everywhere a 401/403 surfaces:
provider adapters (chat + ask mode + streaming), the startup model
check, MCP servers, the Capitol client, and the status-page exporter.
No bare ``401 Unauthorized`` anywhere."""

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from conch import providers


def http_error(status: int, body: dict, url: str = "https://api.example/v1"):
    raw = json.dumps(body).encode()
    return urllib.error.HTTPError(url, status, "Unauthorized", {}, io.BytesIO(raw))


class TestProviderMessages(unittest.TestCase):
    def test_auth_rejected_message_shape(self):
        text = providers.auth_rejected_message("openrouter", "OPENROUTER_API_KEY", 401, "Invalid API key")
        self.assertEqual(
            text,
            "OPENROUTER_API_KEY was rejected by openrouter (401: Invalid API key)"
            " — run: conch keys set OPENROUTER_API_KEY",
        )
        self.assertIn("ANTHROPIC_API_KEY", providers.auth_rejected_message("anthropic", "", 403))

    def test_missing_key_message_names_fix(self):
        text = providers.missing_api_key_message("anthropic", "")
        self.assertIn("ANTHROPIC_API_KEY is not set", text)
        self.assertIn("run: conch keys set ANTHROPIC_API_KEY", text)
        self.assertTrue(text.startswith("authentication:"))

    def test_http_error_response_401_vs_other(self):
        cfg = {"provider": "openai", "api_key_env": "TEAM_OPENAI_KEY"}
        rejected = providers.http_error_response("openai", cfg, http_error(401, {"error": {"message": "bad key"}}))
        self.assertTrue(rejected["_error"])
        self.assertIn("TEAM_OPENAI_KEY was rejected by openai (401: bad key", rejected["content"])
        self.assertIn("conch keys set TEAM_OPENAI_KEY", rejected["content"])
        other = providers.http_error_response("openai", cfg, http_error(429, {"error": {"message": "slow down"}}))
        self.assertIn("slow down", other["content"])
        self.assertNotIn("conch keys set", other["content"])

    def test_every_cloud_adapter_names_the_key_on_401(self):
        cases = {
            "openai": (providers.raw_openai, "OPENAI_API_KEY"),
            "anthropic": (providers.raw_anthropic, "ANTHROPIC_API_KEY"),
            "openrouter": (providers.raw_openrouter, "OPENROUTER_API_KEY"),
            "bedrock": (providers.raw_bedrock, "AWS_BEARER_TOKEN_BEDROCK"),
            "cerebras": (providers.raw_cerebras, "CEREBRAS_API_KEY"),
        }
        for provider, (fn, env_name) in cases.items():
            with self.subTest(provider=provider), \
                    patch.dict(os.environ, {env_name: "k"}), \
                    patch("urllib.request.urlopen", side_effect=http_error(401, {"error": {"message": "nope"}})):
                result = fn({"provider": provider, "chat_model": "m", "model": "m"},
                            [{"role": "user", "content": "hi"}])
            self.assertTrue(result.get("_error"), provider)
            self.assertIn(f"{env_name} was rejected by {provider} (401", result["content"])
            self.assertIn(f"conch keys set {env_name}", result["content"])

    def test_custom_adapter_names_configured_env(self):
        cfg = {"provider": "custom", "custom_base_url": "http://127.0.0.1:1/v1",
               "custom_model": "m", "model": "m", "chat_model": "m", "api_key_env": "LLAMA_KEY"}
        with patch.dict(os.environ, {"LLAMA_KEY": "k"}), \
                patch("conch.providers.validate_custom_model", return_value=(True, "")), \
                patch("urllib.request.urlopen", side_effect=http_error(403, {"error": "forbidden"})):
            result = providers.raw_custom(cfg, [{"role": "user", "content": "hi"}])
        self.assertIn("LLAMA_KEY was rejected by custom (403", result["content"])

    def test_structural_classification_keeps_auth_out_of_same_provider_retries(self):
        from conch.runtime import is_auth_error, is_structural_error

        text = providers.auth_rejected_message("openai", "OPENAI_API_KEY", 401)
        self.assertTrue(is_structural_error(text))
        self.assertTrue(is_auth_error(text))
        self.assertTrue(is_auth_error(providers.missing_api_key_message("openai", "")))
        self.assertFalse(is_auth_error("HTTP 500: boom"))


class TestAskModeMessages(unittest.TestCase):
    def test_ask_mode_401_names_key(self):
        from conch.llm import _api_error_text

        text = _api_error_text("openrouter", {"api_key_env": "OPENROUTER_API_KEY"},
                               http_error(401, {"error": {"message": "Invalid"}}))
        self.assertIn("OPENROUTER_API_KEY was rejected by openrouter (401", text)
        self.assertEqual(_api_error_text("openai", {}, RuntimeError("x")), "x")


class TestModelCheckMessages(unittest.TestCase):
    def test_probe_classes_carry_name_and_fix(self):
        from conch.modelcheck import AUTH_FAILED, NO_KEY, probe_key, probe_model
        from tests.modelcheck_stubs import StubEndpoint

        server = StubEndpoint(["gpt-4o-mini"], require_token="right").start()
        self.addCleanup(server.stop)
        cfg = {"provider": "openai", "chat_model": "gpt-4o-mini", "model": "gpt-4o-mini"}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "wrong", "CONCH_OPENAI_API_BASE": server.v1}):
            result = probe_model("openai", "gpt-4o-mini", cfg, timeout=3)
            self.assertEqual(result.status, AUTH_FAILED)
            self.assertIn("OPENAI_API_KEY was rejected by openai (401", result.detail)
            self.assertIn("run: conch keys set OPENAI_API_KEY", result.detail)
            key = probe_key("openai", cfg, timeout=3)
            self.assertEqual(key.status, AUTH_FAILED)
            good = probe_key("openai", cfg, timeout=3, api_key="right")
            self.assertEqual(good.status, "ok")
            self.assertIn("OPENAI_API_KEY accepted by openai", good.detail)
        with patch.dict(os.environ, {"CONCH_OPENAI_API_BASE": server.v1}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            result = probe_model("openai", "gpt-4o-mini", cfg, timeout=3)
            self.assertEqual(result.status, NO_KEY)
            self.assertEqual(result.detail, "OPENAI_API_KEY is not set — run: conch keys set OPENAI_API_KEY")

    def test_daemon_parked_reason_carries_fix(self):
        """The non-interactive model check (what the daemon's session
        factory raises → the mission's last_error) names the key."""
        from conch.bootstrap import StartupError
        from conch.modelcheck import ensure_working_model

        cfg = {"provider": "anthropic", "chat_model": "claude-sonnet-5", "model": "claude-sonnet-5",
               "fallback_models": "", "model_check_timeout": "1"}
        with patch.dict(os.environ, {}, clear=False), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(StartupError) as caught:
                ensure_working_model(cfg, interactive=False, announce=lambda line: None, discover=False)
        self.assertIn("ANTHROPIC_API_KEY is not set — run: conch keys set ANTHROPIC_API_KEY", str(caught.exception))


class TestComponentMessages(unittest.TestCase):
    def test_status_export_401(self):
        from conch.capitol.status_export import StatusExporterService

        logs = []
        service = StatusExporterService(None, {"status_page_url": "https://s.example"}, log=logs.append)
        with patch.dict(os.environ, {"CONCH_STATUS_WRITE_TOKEN": "t"}), \
                patch("conch.capitol.status_export.build_snapshot", return_value={"schema": "x", "listings": []}):
            service._post = lambda url, headers, body: 401
            service.tick({})
        self.assertTrue(logs, "nothing logged")
        self.assertIn("CONCH_STATUS_WRITE_TOKEN was rejected by the status page (401)", logs[-1])
        self.assertIn("conch keys set CONCH_STATUS_WRITE_TOKEN", logs[-1])

    def test_capitol_registry_vs_env_bearer_fix(self):
        from conch.capitol.credentials import registry_bearer_fix

        env = registry_bearer_fix("env:CAPITOL_A2A_BEARER", "org", "agent")
        self.assertIn("CAPITOL_A2A_BEARER was rejected by capitol (401)", env)
        self.assertIn("conch keys set CAPITOL_A2A_BEARER", env)
        reg = registry_bearer_fix("registry:together-funding", "org", "agent", 401)
        self.assertIn("'together-funding'", reg)
        self.assertIn("re-bootstrap", reg)
        self.assertIn("no automatic re-auth", reg)

    def test_capitol_runtime_401_includes_fix(self):
        from conch.capitol.client import CapitolRuntime
        from conch.capitol.errors import CapitolAuthError

        runtime = CapitolRuntime("http://localhost:8300", "org", "agent", "bearer-value")
        runtime.bearer_source = "env:CAPITOL_A2A_BEARER"
        with patch("urllib.request.urlopen", side_effect=http_error(401, {"detail": "nope"}, "http://localhost:8300/x")):
            with self.assertRaises(CapitolAuthError) as caught:
                runtime._request("http://localhost:8300/x")
        text = str(caught.exception)
        self.assertIn("conch keys set CAPITOL_A2A_BEARER", text)
        self.assertNotIn("bearer-value", text)

    def test_mcp_server_status_names_key(self):
        from conch import mcp

        client = mcp.HttpMcpClient("hosted", "http://127.0.0.1:9/mcp", {"Authorization": "Bearer x"})
        client.credential_names = ["MCP_HOSTED_TOKEN"]
        with patch.object(client, "_post", return_value=({"error": {"message": "HTTP 401: invalid bearer"}}, {})):
            self.assertEqual(client.list_tools(), [])
        self.assertIn("MCP_HOSTED_TOKEN was rejected by mcp/hosted (401", client.last_error)
        self.assertIn("conch keys set MCP_HOSTED_TOKEN", client.last_error)


if __name__ == "__main__":
    unittest.main()
