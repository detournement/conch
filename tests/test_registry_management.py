"""First-class llama-idx registry management.

The /registry command family and the conch_config registry actions wire a
registry from inside conch instead of hand-editing llamaidx_url: status
probes the configured URL live, set validates the endpoint (reachability +
supported schema major) BEFORE persisting llamaidx_url through
config.set_config_values (the conch-written config path onboarding and
/install already use), and off/clear unsets it the same way. Scheme-less
URLs normalize to http://. An end-to-end flow — set the registry, discover
a model, switch to it, run a completion — is covered with the llamaidx
fakes matching the real payload shapes.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch.commands import handle_slash_command
from conch.config import get_config_path
from conch.llamaidx import (
    clear_llamaidx_cache,
    probe_llamaidx_registry,
    registry_probe_summary,
)
from conch.providers import RAW_FNS, clear_local_model_caches
from tests.test_llamaidx import FakeLlamaCppBox, FakeRegistry, _closed_port_url


def _quiet():
    return patch("sys.stdout", new_callable=io.StringIO)


def _slash(command, config, messages=None):
    with _quiet() as out:
        result = handle_slash_command(
            command, config, config.get("provider", "anthropic"),
            config.get("chat_model", "claude-sonnet-5"), lambda v: None,
            messages=messages,
        )
    return result, out.getvalue()


class _TempConfigMixin:
    """Point the config file at a temp dir so persistence is observable
    without touching the real ~/.config/conch/config."""

    def setUp(self):
        clear_local_model_caches()
        clear_llamaidx_cache()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.registry = FakeRegistry().start()
        self.addCleanup(self.registry.stop)
        self.registry.providers = [
            self.registry.provider_entry(
                name="burt", flavor="llamacpp",
                base_url="http://192.0.2.50:8080",
                models=[
                    self.registry.model_entry("qwen3-14b", ctx=32768),
                    self.registry.model_entry("embed-only", tools=False),
                ],
            ),
            self.registry.provider_entry(
                name="coldbox", flavor="llamacpp",
                base_url="http://192.0.2.52:8080", status="down",
                models=[self.registry.model_entry("m")],
            ),
        ]

    def config_file_text(self):
        path = Path(get_config_path())
        return path.read_text() if path.exists() else ""


class ProbeTests(_TempConfigMixin, unittest.TestCase):
    def test_probe_normalizes_and_reports_counts(self):
        host_port = self.registry.base_url[len("http://"):]
        ok, reason, status = probe_llamaidx_registry(host_port, {})
        self.assertTrue(ok, reason)
        summary = registry_probe_summary(status)
        self.assertIn("registry_version 0.1.0", summary)
        self.assertIn("2 provider(s)", summary)
        self.assertIn("1 up, 0 degraded, 1 down", summary)
        # embed-only (tools=False) and the down box's model don't count.
        self.assertIn("1 tool-verified model(s) selectable", summary)

    def test_probe_distinguishes_unreachable_and_bad_schema(self):
        ok, reason, status = probe_llamaidx_registry(_closed_port_url(), {})
        self.assertFalse(ok)
        self.assertIn("unreachable", reason)
        self.assertIsNone(status)
        self.registry.registry_version = "2.0.0"
        ok, reason, _ = probe_llamaidx_registry(self.registry.base_url, {})
        self.assertFalse(ok)
        self.assertIn("unsupported registry_version '2.0.0'", reason)

    def test_probe_respects_local_only(self):
        ok, reason, _ = probe_llamaidx_registry(
            "https://registry.example.com:8642", {"provider": "ollama"}
        )
        self.assertFalse(ok)
        self.assertIn("local_only", reason)


class RegistryCommandTests(_TempConfigMixin, unittest.TestCase):
    def test_status_unconfigured(self):
        _, out = _slash("/registry", {"provider": "anthropic"})
        self.assertIn("No llama-idx registry configured", out)
        self.assertIn("/registry set <url>", out)
        self.assertIn("llamaidx_url", out)

    def test_status_reports_reachable_registry(self):
        config = {
            "provider": "anthropic",
            "llamaidx_url": self.registry.base_url,
        }
        _, out = _slash("/registry", config)
        self.assertIn(self.registry.base_url, out)
        self.assertIn("reachable", out)
        self.assertIn("registry_version 0.1.0", out)
        self.assertIn("1 up, 0 degraded, 1 down", out)
        self.assertIn("1 tool-verified model(s) selectable", out)
        self.assertIn("/models", out)

    def test_status_reports_unreachable_registry(self):
        config = {"provider": "anthropic", "llamaidx_url": _closed_port_url()}
        _, out = _slash("/registry", config)
        self.assertIn("unreachable", out)

    def test_set_normalizes_persists_and_reports(self):
        host_port = self.registry.base_url[len("http://"):]
        config = {"provider": "anthropic"}
        _, out = _slash(f"/registry set {host_port}", config)
        self.assertIn(f"llamaidx_url = {self.registry.base_url}", out)
        self.assertIn("1 tool-verified model(s) selectable", out)
        # Committed to the live session AND the config file.
        self.assertEqual(config["llamaidx_url"], self.registry.base_url)
        self.assertIn(
            f"llamaidx_url = {self.registry.base_url}",
            self.config_file_text(),
        )

    def test_set_refuses_unreachable_without_persisting(self):
        config = {"provider": "anthropic"}
        _, out = _slash(f"/registry set {_closed_port_url()}", config)
        self.assertIn("Not saved", out)
        self.assertIn("unreachable", out)
        self.assertNotIn("llamaidx_url", config)
        self.assertNotIn("llamaidx_url", self.config_file_text())

    def test_set_refuses_unsupported_schema_without_persisting(self):
        self.registry.registry_version = "2.0.0"
        config = {"provider": "anthropic"}
        _, out = _slash(f"/registry set {self.registry.base_url}", config)
        self.assertIn("Not saved", out)
        self.assertIn("unsupported registry_version '2.0.0'", out)
        self.assertNotIn("llamaidx_url", config)

    def test_set_refuses_nonlocal_url_under_local_only(self):
        config = {"provider": "ollama"}  # local_only auto-on
        _, out = _slash("/registry set https://registry.example.com", config)
        self.assertIn("Not saved", out)
        self.assertIn("local_only", out)
        self.assertNotIn("llamaidx_url", config)

    def test_off_clears_and_confirms_what_was_removed(self):
        config = {"provider": "anthropic"}
        _slash(f"/registry set {self.registry.base_url}", config)
        _, out = _slash("/registry off", config)
        self.assertIn("Registry removed", out)
        self.assertIn(f"was {self.registry.base_url}", out)
        self.assertEqual(config["llamaidx_url"], "")
        # The persisted key is cleared (empty value = feature off).
        text = self.config_file_text()
        self.assertIn("llamaidx_url =", text)
        self.assertNotIn(self.registry.base_url, text)

    def test_off_without_registry(self):
        _, out = _slash("/registry off", {"provider": "anthropic"})
        self.assertIn("nothing to remove", out)

    def test_unknown_verb_prints_usage(self):
        _, out = _slash("/registry bogus", {"provider": "anthropic"})
        self.assertIn("Usage: /registry", out)


class ConchConfigRegistryActionTests(_TempConfigMixin, unittest.TestCase):
    def _client(self, config):
        from conch.tooling import ConchConfigClient

        client = ConchConfigClient()
        client.bind("anthropic", "claude-sonnet-5", {}, config)
        return client

    def _call(self, client, arguments):
        return client.call_tool("conch_config", arguments)["content"][0]["text"]

    def test_get_registry_unconfigured(self):
        text = self._call(
            self._client({"provider": "anthropic"}), {"action": "get_registry"}
        )
        self.assertIn("No llama-idx registry configured", text)
        self.assertIn("set_registry", text)

    def test_get_registry_reports_status(self):
        config = {
            "provider": "anthropic",
            "llamaidx_url": self.registry.base_url,
        }
        text = self._call(self._client(config), {"action": "get_registry"})
        self.assertIn(f"llamaidx_url: {self.registry.base_url}", text)
        self.assertIn("reachable", text)
        self.assertIn("1 tool-verified model(s) selectable", text)

    def test_set_registry_validates_persists_and_reports(self):
        host_port = self.registry.base_url[len("http://"):]
        config = {"provider": "anthropic"}
        client = self._client(config)
        text = self._call(
            client, {"action": "set_registry", "value": host_port}
        )
        self.assertIn(f"llamaidx_url set to {self.registry.base_url}", text)
        self.assertIn("1 tool-verified model(s) selectable", text)
        self.assertIn("list_models", text)
        self.assertEqual(config["llamaidx_url"], self.registry.base_url)
        self.assertIn(
            f"llamaidx_url = {self.registry.base_url}",
            self.config_file_text(),
        )
        # The current-settings view now names the registry.
        self.assertIn(
            f"llamaidx_url: {self.registry.base_url}",
            self._call(client, {"action": "get"}),
        )

    def test_set_registry_refuses_bad_endpoints_without_persisting(self):
        config = {"provider": "anthropic"}
        client = self._client(config)
        text = self._call(
            client, {"action": "set_registry", "value": _closed_port_url()}
        )
        self.assertIn("Not saved", text)
        self.assertIn("unreachable", text)
        self.registry.registry_version = "2.0.0"
        text = self._call(
            client,
            {"action": "set_registry", "value": self.registry.base_url},
        )
        self.assertIn("unsupported registry_version '2.0.0'", text)
        self.assertNotIn("llamaidx_url", config)
        self.assertNotIn("llamaidx_url", self.config_file_text())
        self.assertIn(
            "provide the registry URL",
            self._call(client, {"action": "set_registry"}),
        )

    def test_clear_registry(self):
        config = {"provider": "anthropic"}
        client = self._client(config)
        self._call(
            client,
            {"action": "set_registry", "value": self.registry.base_url},
        )
        text = self._call(client, {"action": "clear_registry"})
        self.assertIn("Registry removed", text)
        self.assertIn(self.registry.base_url, text)
        self.assertEqual(config["llamaidx_url"], "")
        text = self._call(client, {"action": "clear_registry"})
        self.assertIn("nothing to clear", text)


class EndToEndRegistryFlowTests(_TempConfigMixin, unittest.TestCase):
    """The full journey against fakes matching the real payload shapes:
    wire the registry, see the model in discovery, switch to it
    (probe-on-select), and run a completion through the routed adapter."""

    def test_set_discover_switch_complete(self):
        box = FakeLlamaCppBox(models=["qwen3-14b"]).start()
        self.addCleanup(box.stop)
        box.chat_reply = "Completion served by the registry-discovered box."
        self.registry.providers = [
            self.registry.provider_entry(
                name="burt", flavor="llamacpp", base_url=box.base_url,
                models=[self.registry.model_entry("qwen3-14b", ctx=32768)],
            )
        ]
        config = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "chat_model": "claude-sonnet-5",
        }

        # 1. Wire the registry from inside conch.
        _, out = _slash(f"/registry set {self.registry.base_url}", config)
        self.assertIn("llamaidx_url =", out)

        # 2. Discovery lists the namespaced entry.
        _, models_out = _slash("/models", dict(config))
        self.assertIn("llamaidx/burt/qwen3-14b", models_out)

        # 3. The switch commits (probe-on-select against the box passes)
        #    and records the switch note at the boundary.
        messages = [
            {"role": "system", "content": "You are conch."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi from the cloud model"},
        ]
        result, switch_out = _slash(
            "/model llamaidx/burt/qwen3-14b", config, messages=messages
        )
        self.assertEqual(result[:2], ("custom", "qwen3-14b"))
        self.assertIn("Switched to llamaidx/burt/qwen3-14b", switch_out)
        self.assertEqual(config["custom_base_url"], box.base_url + "/v1")
        self.assertTrue(
            messages[-1]["content"].startswith("Model switched:")
        )

        # 4. A completion flows through the routed adapter.
        reply = RAW_FNS["custom"](
            config, [{"role": "user", "content": "say hello"}]
        )
        self.assertIn(
            "Completion served by the registry-discovered box.",
            reply.get("content", ""),
        )


if __name__ == "__main__":
    unittest.main()
