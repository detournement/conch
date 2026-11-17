"""``conch-ask`` on a custom OpenAI-compatible endpoint (llama.cpp, vLLM,
LM Studio), end to end through ``conch.cli.main``.

Proven here against the scripted stand-in in ``tests/modelcheck_stubs``,
with the real config loader reading an isolated ``XDG_CONFIG_HOME``:

* ``provider = custom`` goes startup probe → custom caller → answer: the
  endpoint sees ``/v1/models``, the conformance probe, then exactly one
  forced ``shell_command`` call; the command is printed and nothing else
  is said or run;
* a key named by ``api_key_env`` is sent by reference and never printed;
* an endpoint that is down fails closed with the model-check exit status
  and diagnosis — no prompt, no silent switch;
* ``local_only`` (the default for custom endpoints) refuses a non-local
  endpoint before any request and keeps cloud providers out of the
  runtime fallback chain even when their key variables are set;
* the announced runtime fallback to another model on the same endpoint
  keeps the endpoint's key variable — a keyed endpoint answers instead
  of rejecting the fallback with 401.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import cli, llm
from conch.config import ENV_CONFIG_KEYS
from conch.modelcheck import MODEL_CHECK_EXIT_CODE
from conch.providers import DEFAULT_API_KEY_ENVS, clear_local_model_caches
from tests.modelcheck_stubs import StubEndpoint, closed_port_url

MODEL = "qwen3.8-27b-q8"
COMMAND = "ls -la"
# Test-only literal sent to loopback stubs; never a real credential.
STUB_TOKEN = "stub-token-for-tests"
KEY_ENV = "CONCH_TEST_CUSTOM_KEY"
# A non-local hostname under the reserved .invalid TLD (RFC 2606): it can
# never resolve, and local_only must refuse it before a socket is opened.
REMOTE_ENDPOINT = "http://inference.invalid:8080/v1"


def _refuse_input(prompt=""):
    raise AssertionError(f"conch-ask prompted unexpectedly: {prompt!r}")


class AskCustomCase(unittest.TestCase):
    """Isolated HOME/XDG, the real config file loader, no provider keys or
    ``CONCH_*`` overrides inherited from the developer's environment, no
    reachable Ollama, every discovery cache cleared."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / "config" / "conch"
        self.config_dir.mkdir(parents=True)
        patcher = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_CACHE_HOME": str(self.home / "cache"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in set(DEFAULT_API_KEY_ENVS.values()) | set(ENV_CONFIG_KEYS) | {
            "OLLAMA_HOST", "CEREBRAS_BASE_URL", "CONCH_HISTORY", KEY_ENV,
        }:
            os.environ.pop(name, None)
        clear_local_model_caches()
        self.addCleanup(clear_local_model_caches)
        # The config loader also reads the nearest project .conchrc.
        self._cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, self._cwd)

    def stub(self, *args, **kwargs) -> StubEndpoint:
        endpoint = StubEndpoint(*args, **kwargs).start()
        self.addCleanup(endpoint.stop)
        return endpoint

    def write_config(self, base_url, model=MODEL, **extra) -> None:
        lines = {
            "provider": "custom",
            "custom_base_url": base_url,
            "custom_model": model,
            # Ollama discovery points at a dead port so a developer's real
            # local server can never leak into these runs.
            "ollama_base_url": closed_port_url(),
            "model_check_timeout": "1",
        }
        lines.update(extra)
        (self.config_dir / "config").write_text(
            "".join(f"{key} = {value}\n" for key, value in lines.items())
        )

    def run_ask(self, request="list files here"):
        """(exit status or None, stdout, stderr) of ``conch-ask <request>``."""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = None
        with patch("sys.argv", ["conch-ask", request]), \
                patch("builtins.input", side_effect=_refuse_input), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()


class TestAskOnCustomEndpoint(AskCustomCase):
    def test_probe_then_custom_caller_then_answer(self):
        server = self.stub([MODEL], ask_commands={MODEL: COMMAND})
        self.write_config(server.v1)
        code, out, err = self.run_ask()
        self.assertIsNone(code, err)
        self.assertEqual(out, COMMAND + "\n")
        self.assertEqual(err, "", "a passing probe and answer say nothing on stderr")
        self.assertIn("/v1/models", server.paths("GET"))
        self.assertEqual(
            server.chat_tools, [["conch_tool_probe"], ["shell_command"]],
            "the startup probe runs first, then exactly one forced shell_command call",
        )

    def test_key_variable_is_sent_by_reference_and_never_printed(self):
        server = self.stub([MODEL], require_token=STUB_TOKEN, ask_commands={MODEL: COMMAND})
        self.write_config(server.v1, api_key_env=KEY_ENV)
        with patch.dict(os.environ, {KEY_ENV: STUB_TOKEN}):
            code, out, err = self.run_ask()
        self.assertIsNone(code, err)
        self.assertEqual(out, COMMAND + "\n")
        self.assertNotIn(STUB_TOKEN, out + err)

    def test_endpoint_down_fails_closed_with_model_check_status(self):
        self.write_config(closed_port_url() + "/v1")
        code, out, err = self.run_ask()
        self.assertEqual(code, MODEL_CHECK_EXIT_CODE)
        self.assertEqual(out, "", "nothing is printed for a shell widget to run")
        self.assertIn(f"model check failed — custom/{MODEL}", err)
        self.assertRegex(err, r"unreachable|timed out")
        self.assertIn("no fallback_models configured", err)
        self.assertNotIn("trying", err, "no runtime fallback runs after a failed probe")

    def test_local_only_refuses_a_non_local_endpoint_before_any_request(self):
        self.write_config(REMOTE_ENDPOINT)  # local_only=auto: on for custom
        code, out, err = self.run_ask()
        self.assertEqual(code, MODEL_CHECK_EXIT_CODE)
        self.assertEqual(out, "")
        self.assertIn("local_only blocks non-local custom endpoint", err)
        self.assertIn(REMOTE_ENDPOINT, err)

    def test_local_only_keeps_cloud_providers_out_of_the_fallback_chain(self):
        # The model passes the probe but cannot answer ask mode's forced
        # tool call; cloud keys are present, yet local_only (the custom
        # default) must keep them out of the runtime fallback chain.
        server = self.stub([MODEL])  # no ask_commands: answers in prose
        self.write_config(server.v1)
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test-not-a-key",
            "ANTHROPIC_API_KEY": "sk-ant-test-not-a-key",
        }):
            code, out, err = self.run_ask()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("conch: [no response]", err)
        self.assertNotIn("trying openai/", err)
        self.assertNotIn("trying anthropic/", err)
        self.assertEqual(
            server.chat_tools, [["conch_tool_probe"], ["shell_command"]],
            "no other endpoint was consulted",
        )

    def test_same_endpoint_fallback_is_announced_and_keeps_the_key(self):
        server = self.stub(
            [MODEL, "backup-model"], require_token=STUB_TOKEN,
            ask_commands={"backup-model": "du -sh *"},
        )
        self.write_config(server.v1, api_key_env=KEY_ENV)
        with patch.dict(os.environ, {KEY_ENV: STUB_TOKEN}):
            code, out, err = self.run_ask()
        self.assertIsNone(code, err)
        self.assertEqual(out, "du -sh *\n")
        self.assertIn(f"conch: custom/{MODEL} failed, trying custom/backup-model...", err)
        self.assertNotIn("401", err, "the fallback carried the endpoint's key variable")
        self.assertNotIn(STUB_TOKEN, out + err)


class TestAskFallbackRouting(unittest.TestCase):
    """The runtime fallback chain's routing keys, without a network."""

    def test_same_provider_keeps_configured_key_variable(self):
        config = {
            "provider": "openai", "model": "gpt-4o-mini", "chat_model": "gpt-4o-mini",
            "api_key_env": "MY_OPENAI_KEY", "model_check": "off", "local_only": "false",
        }
        seen = []

        def failing_then_recording(cfg, messages):
            seen.append((cfg["provider"], cfg["model"], cfg["api_key_env"]))
            return "ls" if len(seen) == 3 else ""

        chain = [("openai", "gpt-4.1", False), ("anthropic", "claude-sonnet-5", True)]
        with patch("conch.llm.load_config", return_value=dict(config)), \
                patch("conch.providers.get_fallback_chain", return_value=chain), \
                patch.dict(llm._ASK_CALLERS, {
                    "openai": failing_then_recording,
                    "anthropic": failing_then_recording,
                }), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(llm.ask("list files"), "ls")
        self.assertEqual(seen, [
            ("openai", "gpt-4o-mini", "MY_OPENAI_KEY"),
            ("openai", "gpt-4.1", "MY_OPENAI_KEY"),
            ("anthropic", "claude-sonnet-5", "ANTHROPIC_API_KEY"),
        ])


if __name__ == "__main__":
    unittest.main()
