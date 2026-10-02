"""Startup model validation (conch.modelcheck).

Proven here, against scripted HTTP stand-ins (tests/modelcheck_stubs):

* every failure class is detected and named — no key, unreachable,
  authentication failed, model not found, timeout, tool-call conformance,
  quota / rate limiting / server errors — for custom (llama.cpp-style),
  Ollama and cloud endpoints, with the cloud path preferring the models
  list over a paid completion;
* a passing probe adds no prompt and changes nothing;
* ``model_check = off`` skips the probe (and the network) entirely;
* the interactive flow diagnoses, ranks verified alternatives first,
  re-verifies a pick that had failed and continues down the list,
  switches only on the user's pick, and persists only on a second
  explicit "y" — writing routing keys by name, never a credential;
* declining exits cleanly with the model-check exit status;
* non-interactive runs never prompt: ``fallback_models`` is honoured in
  order and announced, otherwise the run fails closed with a message
  naming the problem, the alternatives that answered, and the fix;
* the total wall-clock cost stays bounded when everything is down;
* the edge daemon's session factory parks a mission on the engine's
  error backoff instead of calling a model that is known to be down, and
  uses a fallback entry without changing the daemon's default.
"""

import contextlib
import io
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import modelcheck
from conch.bootstrap import StartupError
from conch.modelcheck import (
    AUTH_FAILED,
    CONFORMANCE_FAILED,
    MODEL_CHECK_EXIT_CODE,
    MODEL_NOT_FOUND,
    NO_KEY,
    OK,
    QUOTA,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    UNREACHABLE,
    Candidate,
    candidate_from_fallback,
    discovery_budget,
    ensure_working_model,
    model_check_timeout,
    parse_fallback_models,
    probe_model,
)
from conch.providers import (
    DEFAULT_API_KEY_ENVS,
    DEFAULT_CHAT_MODEL_BY_PROVIDER,
    clear_local_model_caches,
)
from tests.modelcheck_stubs import StubEndpoint, closed_port_url

# The user's own configuration (llama.cpp on a LAN box that is down all
# day) is the scenario most of these tests reproduce.
PRIMARY_MODEL = "qwen3.8-27b-q8"
# Test-only literal, never a real credential; it is sent to loopback stubs.
STUB_TOKEN = "stub-token-for-tests"


def _refuse_input(prompt=""):
    raise AssertionError(f"the model check prompted unexpectedly: {prompt!r}")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


class ModelCheckCase(unittest.TestCase):
    """Isolated HOME/XDG, no provider keys in the environment, no real
    Ollama on localhost, every probe cache cleared."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / "config" / "conch"
        self.config_dir.mkdir(parents=True)
        env = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_STATE_HOME": str(self.home / "state"),
        }
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in set(DEFAULT_API_KEY_ENVS.values()) | {
            "OLLAMA_HOST", "CEREBRAS_BASE_URL", "CONCH_MODEL_CHECK",
            "CONCH_MODEL_CHECK_TIMEOUT", "CONCH_FALLBACK_MODELS",
        }:
            os.environ.pop(name, None)
        clear_local_model_caches()
        self.addCleanup(clear_local_model_caches)
        self._stubs = []

    def stub(self, *args, **kwargs) -> StubEndpoint:
        endpoint = StubEndpoint(*args, **kwargs).start()
        self._stubs.append(endpoint)
        self.addCleanup(endpoint.stop)
        return endpoint

    def custom_config(self, base_url, model=PRIMARY_MODEL, **extra) -> dict:
        config = {
            "provider": "custom",
            "custom_base_url": base_url,
            "custom_model": model,
            "chat_model": model,
            "model": model,
            "api_key_env": "",
            # Point Ollama discovery at a dead port so a developer's real
            # local Ollama can never leak into these tests.
            "ollama_base_url": closed_port_url(),
            "model_check_timeout": "1",
        }
        config.update(extra)
        return config

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config"


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


class TestKnobs(unittest.TestCase):
    def test_timeout_default_and_clamping(self):
        self.assertEqual(model_check_timeout({}), 4.0)
        self.assertEqual(model_check_timeout({"model_check_timeout": "0.1"}), 0.5)
        self.assertEqual(model_check_timeout({"model_check_timeout": "999"}), 60.0)
        self.assertEqual(model_check_timeout({"model_check_timeout": "nope"}), 4.0)

    def test_discovery_budget_is_bounded(self):
        self.assertEqual(discovery_budget(0.5), 5.0)
        self.assertEqual(discovery_budget(4.0), 8.0)
        self.assertEqual(discovery_budget(60.0), 30.0)

    def test_fallback_models_parsing(self):
        self.assertEqual(parse_fallback_models(""), [])
        self.assertEqual(
            parse_fallback_models("ollama/qwen2.5:7b, anthropic/claude-sonnet-5\n"
                                  "openai/gpt-4o-mini; ollama/qwen2.5:7b"),
            ["ollama/qwen2.5:7b", "anthropic/claude-sonnet-5", "openai/gpt-4o-mini"],
        )

    def test_candidate_updates_never_carry_a_foreign_key_variable(self):
        """Switching from a cloud provider to a local endpoint must not
        keep sending the cloud key variable's value to the new endpoint."""
        local = Candidate("custom", "m", "custom",
                          overrides={"custom_base_url": "http://127.0.0.1:1/v1"})
        self.assertEqual(local.config_updates()["api_key_env"], "")
        self.assertEqual(local.config_updates()["custom_model"], "m")
        cloud = Candidate("anthropic", "claude-sonnet-5", "provider")
        self.assertEqual(cloud.config_updates()["api_key_env"],
                         DEFAULT_API_KEY_ENVS["anthropic"])
        ollama = Candidate("ollama", "qwen2.5:7b", "ollama")
        self.assertEqual(ollama.config_updates()["api_key_env"], "")


# ---------------------------------------------------------------------------
# Failure classes — custom endpoint (llama.cpp / vLLM style)
# ---------------------------------------------------------------------------


class TestCustomEndpointProbe(ModelCheckCase):
    def test_verified_model_passes_with_native_tool_call(self):
        server = self.stub([PRIMARY_MODEL])
        result = probe_model("custom", PRIMARY_MODEL, self.custom_config(server.v1))
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn("native tool call verified", result.detail)
        self.assertIn("/v1/models", server.paths("GET"))
        self.assertEqual(server.paths("POST"), ["/v1/chat/completions"])

    def test_unreachable_endpoint(self):
        result = probe_model("custom", PRIMARY_MODEL, self.custom_config(closed_port_url() + "/v1"))
        self.assertEqual(result.status, UNREACHABLE)
        self.assertIn("endpoint", result.detail)
        self.assertEqual(result.describe().split(" — ")[0], "unreachable")

    def test_authentication_failure_names_the_variable_not_the_key(self):
        server = self.stub([PRIMARY_MODEL], require_token=STUB_TOKEN)
        config = self.custom_config(server.v1, api_key_env="")
        result = probe_model("custom", PRIMARY_MODEL, config)
        self.assertEqual(result.status, AUTH_FAILED)
        self.assertIn("HTTP 401", result.detail)
        self.assertNotIn(STUB_TOKEN, result.detail)

    def test_authenticated_endpoint_passes_with_key_from_named_variable(self):
        server = self.stub([PRIMARY_MODEL], require_token=STUB_TOKEN)
        with patch.dict(os.environ, {"LLAMA_TEST_KEY": STUB_TOKEN}):
            config = self.custom_config(server.v1, api_key_env="LLAMA_TEST_KEY")
            result = probe_model("custom", PRIMARY_MODEL, config)
        self.assertEqual(result.status, OK, result.detail)

    def test_model_not_exposed(self):
        server = self.stub(["other-7b", "tiny-1b"])
        result = probe_model("custom", PRIMARY_MODEL, self.custom_config(server.v1))
        self.assertEqual(result.status, MODEL_NOT_FOUND)
        self.assertIn(f"'{PRIMARY_MODEL}' is not exposed", result.detail)
        self.assertIn("other-7b", result.detail)

    def test_timeout_is_bounded_by_the_configured_timeout(self):
        server = self.stub([PRIMARY_MODEL], hang=3.0)
        config = self.custom_config(server.v1, model_check_timeout="0.5")
        started = time.monotonic()
        result = probe_model("custom", PRIMARY_MODEL, config)
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, TIMEOUT)
        self.assertIn("timed out after 0.5s", result.detail)
        self.assertLess(elapsed, 2.0)

    def test_conformance_failure_when_no_native_tool_call(self):
        server = self.stub([PRIMARY_MODEL], tool_capable=[])
        result = probe_model("custom", PRIMARY_MODEL, self.custom_config(server.v1))
        self.assertEqual(result.status, CONFORMANCE_FAILED)
        self.assertIn("native tool call", result.detail)

    def test_server_error_and_quota_are_classified(self):
        boom = self.stub([PRIMARY_MODEL], force_status=(503, {"error": {"message": "overloaded"}}))
        self.assertEqual(
            probe_model("custom", PRIMARY_MODEL, self.custom_config(boom.v1)).status,
            SERVER_ERROR,
        )
        broke = self.stub([PRIMARY_MODEL], force_status=(
            429, {"error": {"message": "You exceeded your current quota"}}
        ))
        self.assertEqual(
            probe_model("custom", PRIMARY_MODEL, self.custom_config(broke.v1)).status,
            QUOTA,
        )
        busy = self.stub([PRIMARY_MODEL], force_status=(
            429, {"error": {"message": "Rate limit reached, retry in 2s"}}
        ))
        self.assertEqual(
            probe_model("custom", PRIMARY_MODEL, self.custom_config(busy.v1)).status,
            RATE_LIMITED,
        )

    def test_missing_base_url_is_misconfigured_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            result = probe_model("custom", PRIMARY_MODEL, {"provider": "custom"})
        self.assertEqual(result.status, modelcheck.MISCONFIGURED)
        self.assertIn("custom_base_url", result.detail)

    def test_probe_never_raises(self):
        with patch("conch.modelcheck._probe_custom", side_effect=RuntimeError("kaboom")):
            result = probe_model("custom", PRIMARY_MODEL, self.custom_config("http://127.0.0.1:1/v1"))
        self.assertEqual(result.status, modelcheck.ERROR)
        self.assertIn("RuntimeError: kaboom", result.detail)


# ---------------------------------------------------------------------------
# Failure classes — cloud providers (OpenAI-compatible and Anthropic)
# ---------------------------------------------------------------------------


class TestCloudProbe(ModelCheckCase):
    def _cloud(self, provider, server, model=None, key_env=None):
        model = model or DEFAULT_CHAT_MODEL_BY_PROVIDER[provider]
        config = {"provider": provider, "chat_model": model, "model": model,
                  "model_check_timeout": "1"}
        key_env = key_env or DEFAULT_API_KEY_ENVS[provider]
        with patch.dict(os.environ, {key_env: STUB_TOKEN}), \
                patch("conch.modelcheck.cloud_base_url", return_value=server.v1):
            return probe_model(provider, model, config)

    def test_no_key_is_reported_without_any_request(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            result = probe_model("openai", DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"],
                                 {"provider": "openai"})
        self.assertEqual(result.status, NO_KEY)
        self.assertIn("OPENAI_API_KEY is not set", result.detail)
        self.assertIn(str(self.config_dir / "env"), result.detail)

    def test_listed_model_passes_without_spending_a_completion(self):
        model = DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"]
        server = self.stub([model], require_token=STUB_TOKEN)
        result = self._cloud("openai", server)
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn("listed by the provider", result.detail)
        self.assertEqual(server.paths("POST"), [])

    def test_anthropic_dated_id_matches_alias(self):
        model = DEFAULT_CHAT_MODEL_BY_PROVIDER["anthropic"]
        server = self.stub([f"{model}-20260101"], require_token=STUB_TOKEN)
        result = self._cloud("anthropic", server)
        self.assertEqual(result.status, OK, result.detail)
        self.assertIn(f"as {model}-20260101", result.detail)
        self.assertEqual(server.paths("POST"), [])

    def test_unlisted_model_falls_back_to_a_one_token_completion(self):
        model = DEFAULT_CHAT_MODEL_BY_PROVIDER["anthropic"]
        server = self.stub(["something-else"], require_token=STUB_TOKEN)
        result = self._cloud("anthropic", server)
        # the list did not settle it, the completion did (404 → not found)
        self.assertEqual(result.status, MODEL_NOT_FOUND)
        self.assertEqual(server.paths("POST"), ["/v1/messages"])
        self.assertIn("HTTP 404", result.detail)
        self.assertIn(model, result.detail)

    def test_bad_key_is_auth_failed_and_never_echoed(self):
        server = self.stub([DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"]], require_token="different")
        result = self._cloud("openai", server)
        self.assertEqual(result.status, AUTH_FAILED)
        self.assertNotIn(STUB_TOKEN, result.detail)
        self.assertIn("Incorrect API key", result.detail)

    def test_catalog_gate_rejects_unknown_cloud_model_offline(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": STUB_TOKEN}), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            result = probe_model("anthropic", "claude-sonet-4-6", {"provider": "anthropic"})
        self.assertEqual(result.status, MODEL_NOT_FOUND)
        self.assertIn("did you mean claude-sonnet-4-6", result.detail)

    def test_quota_rate_limit_and_server_errors(self):
        model = DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"]
        for status, message, expected in (
            (429, "You exceeded your current quota, please check your plan and billing",
             QUOTA),
            (429, "Rate limit reached for requests", RATE_LIMITED),
            (402, "Insufficient credits", QUOTA),
            (500, "internal error", SERVER_ERROR),
        ):
            server = self.stub([model], force_status=(status, {"error": {"message": message}}))
            result = self._cloud("openai", server)
            self.assertEqual(result.status, expected, (status, message, result.detail))

    def test_parameter_nit_on_completion_counts_as_reachable(self):
        # list endpoint absent (gateway), completion rejects max_tokens only
        server = self.stub([], force_status=(
            400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported"}}
        ))
        result = self._cloud("openai", server)
        self.assertEqual(result.status, OK)
        self.assertIn("parameter nit", result.detail)


# ---------------------------------------------------------------------------
# ensure_working_model: pass, off, interactive, non-interactive
# ---------------------------------------------------------------------------


class TestEnsureWorkingModelPass(ModelCheckCase):
    def test_passing_probe_adds_no_prompt_and_changes_nothing(self):
        server = self.stub([PRIMARY_MODEL])
        config = self.custom_config(server.v1)
        before = dict(config)
        out = io.StringIO()
        outcome = ensure_working_model(config, interactive=True, input_fn=_refuse_input, out=out)
        self.assertTrue(outcome.checked)
        self.assertTrue(outcome.result.ok)
        self.assertFalse(outcome.switched)
        self.assertEqual(outcome.label, f"custom/{PRIMARY_MODEL}")
        self.assertEqual(config, before)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(self.config_path.exists())

    def test_model_check_off_skips_probe_and_network(self):
        config = self.custom_config(closed_port_url() + "/v1", model_check="off")
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            outcome = ensure_working_model(config, interactive=False)
        self.assertFalse(outcome.checked)
        self.assertIsNone(outcome.result)
        self.assertFalse(outcome.switched)
        self.assertEqual(config["chat_model"], PRIMARY_MODEL)

    def test_env_override_turns_the_check_off(self):
        from conch.config import ENV_CONFIG_KEYS

        self.assertEqual(ENV_CONFIG_KEYS["CONCH_MODEL_CHECK"], "model_check")
        self.assertEqual(ENV_CONFIG_KEYS["CONCH_FALLBACK_MODELS"], "fallback_models")
        self.assertEqual(ENV_CONFIG_KEYS["CONCH_MODEL_CHECK_TIMEOUT"], "model_check_timeout")


class InteractiveCase(ModelCheckCase):
    """Primary: the user's llama.cpp box, down. Alternatives on offer: an
    Ollama server with a tool-capable model (verified) and an OpenAI key
    that the provider rejects (fails) — ranked verified-first."""

    def setUp(self):
        super().setUp()
        self.ollama = self.stub(["qwen2.5:7b", "gemma3:4b"], tool_capable=["qwen2.5:7b"])
        self.cloud = self.stub([DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"]], require_token="not-this")
        os.environ["OPENAI_API_KEY"] = STUB_TOKEN
        self.config = self.custom_config(
            closed_port_url() + "/v1",
            ollama_base_url=self.ollama.url,
            local_only="false",
        )
        patcher = patch("conch.modelcheck.cloud_base_url", return_value=self.cloud.v1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_flow(self, answers, persist_mode="ask"):
        script = iter(answers)
        prompts = []

        def scripted(prompt):
            prompts.append(prompt)
            try:
                return next(script)
            except StopIteration:
                raise AssertionError(f"ran out of scripted answers at {prompt!r}")

        out = io.StringIO()
        try:
            outcome = ensure_working_model(
                self.config, interactive=True, input_fn=scripted, out=out,
                persist_mode=persist_mode,
            )
        except StartupError as exc:
            return None, exc, _plain(out.getvalue()), prompts
        return outcome, None, _plain(out.getvalue()), prompts


class TestInteractiveRecovery(InteractiveCase):
    def test_diagnosis_then_ranked_alternatives_verified_first(self):
        outcome, error, text, prompts = self.run_flow(["1", ""])
        self.assertIsNone(error)
        self.assertIn(f"⚠ custom/{PRIMARY_MODEL} is not usable: unreachable", text)
        self.assertIn("Looking for alternatives", text)
        self.assertIn("other custom models skipped: the failure is provider-level", text)
        first = text.index("1. ollama/qwen2.5:7b")
        second = text.index("2. openai/")
        self.assertLess(first, second)
        self.assertIn("✓ verified", text)
        self.assertIn("✗ authentication failed", text)
        # gemma3 lacks the tools capability and is never offered
        self.assertNotIn("gemma3", text)
        self.assertIn("Enter=1", prompts[0])

    def test_pick_fails_then_next_works_session_only(self):
        """The user picks the failed cloud entry; it is re-verified, still
        fails, drops off; the next pick works; 'Enter' keeps it session
        only — nothing is written."""
        outcome, error, text, prompts = self.run_flow(["2", "1", ""])
        self.assertIsNone(error, text)
        self.assertTrue(outcome.switched)
        self.assertFalse(outcome.saved)
        self.assertEqual((outcome.provider, outcome.model), ("ollama", "qwen2.5:7b"))
        self.assertIn("Verifying openai/", text)
        self.assertIn("✗ openai/", text)
        self.assertIn("✓ Using ollama/qwen2.5:7b for this session", text)
        self.assertIn("Session only", text)
        self.assertEqual(len(prompts), 3)
        self.assertIn("Save ollama/qwen2.5:7b as the new default", prompts[2])
        # in-memory config switched, routed through the Ollama stub, no
        # cloud key variable carried along
        self.assertEqual(self.config["provider"], "ollama")
        self.assertEqual(self.config["chat_model"], "qwen2.5:7b")
        self.assertEqual(self.config["model"], "qwen2.5:7b")
        self.assertEqual(self.config["api_key_env"], "")
        self.assertEqual(self.config["ollama_base_url"], self.ollama.url)
        self.assertFalse(self.config_path.exists())

    def test_save_as_default_writes_only_routing_keys(self):
        self.config_path.write_text(
            "# my config\n"
            f"provider = custom\ncustom_base_url = {self.config['custom_base_url']}\n"
            f"custom_model = {PRIMARY_MODEL}\nchat_model = {PRIMARY_MODEL}\n"
            "local_only = false\nsend_cwd = true\n"
        )
        self.config_path.chmod(0o600)
        outcome, error, text, _ = self.run_flow(["", "y"])
        self.assertIsNone(error, text)
        self.assertTrue(outcome.saved)
        self.assertIn("Saved as the default", text)
        saved = self.config_path.read_text()
        self.assertIn("# my config", saved)
        self.assertIn("provider = ollama", saved)
        self.assertIn("chat_model = qwen2.5:7b", saved)
        self.assertIn("model = qwen2.5:7b", saved)
        self.assertIn("api_key_env = ", saved)
        self.assertIn("send_cwd = true", saved)  # untouched settings survive
        self.assertNotIn(STUB_TOKEN, saved)
        self.assertEqual(oct(self.config_path.stat().st_mode & 0o777), "0o600")

    def test_decline_exits_cleanly_non_zero_without_changes(self):
        before = dict(self.config)
        outcome, error, text, _ = self.run_flow(["q"])
        self.assertIsNone(outcome)
        self.assertEqual(error.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn("no model switch approved", str(error))
        self.assertEqual(self.config, before)
        self.assertFalse(self.config_path.exists())

    def test_eof_counts_as_decline(self):
        def eof(_prompt):
            raise EOFError

        with self.assertRaises(StartupError) as caught:
            ensure_working_model(self.config, interactive=True, input_fn=eof, out=io.StringIO())
        self.assertEqual(caught.exception.code, MODEL_CHECK_EXIT_CODE)

    def test_builtins_input_is_used_when_no_input_fn(self):
        """Scripted-input harnesses patch builtins.input; the flow must
        resolve it at call time."""
        answers = iter(["1", ""])
        with patch("builtins.input", side_effect=lambda *_: next(answers)), \
                contextlib.redirect_stdout(io.StringIO()):
            outcome = ensure_working_model(self.config, interactive=True)
        self.assertEqual(outcome.model, "qwen2.5:7b")

    def test_wizard_mode_persists_without_a_second_question(self):
        outcome, error, text, prompts = self.run_flow(["1"], persist_mode="always")
        self.assertIsNone(error, text)
        self.assertTrue(outcome.saved)
        self.assertEqual(len(prompts), 1)
        self.assertIn("provider = ollama", self.config_path.read_text())

    def test_every_alternative_failing_exits_non_zero(self):
        self.ollama.tool_capable = set()  # nothing verified anywhere
        outcome, error, text, _ = self.run_flow(["1", "1", "1"])
        self.assertIsNone(outcome)
        self.assertEqual(error.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn("every alternative", str(error))

    def test_local_only_policy_excludes_cloud_with_a_note(self):
        self.config["local_only"] = "auto"  # custom → local only
        outcome, error, text, _ = self.run_flow(["1", ""])
        self.assertIsNone(error, text)
        self.assertIn("excluded by local_only=auto: openai", text)
        self.assertNotIn("1. openai/", text)
        self.assertNotIn("2. openai/", text)
        self.assertEqual(outcome.provider, "ollama")


class TestNoAlternatives(ModelCheckCase):
    def test_interactive_with_nothing_to_offer_exits_non_zero(self):
        config = self.custom_config(closed_port_url() + "/v1")
        out = io.StringIO()
        with self.assertRaises(StartupError) as caught:
            ensure_working_model(config, interactive=True, input_fn=_refuse_input, out=out)
        self.assertEqual(caught.exception.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn("No alternatives found", out.getvalue())
        self.assertIn("no alternative was found", str(caught.exception))


class TestNonInteractive(ModelCheckCase):
    def setUp(self):
        super().setUp()
        self.ollama = self.stub(["qwen2.5:7b", "llama3.1:8b"])
        self.config = self.custom_config(
            closed_port_url() + "/v1", ollama_base_url=self.ollama.url,
        )
        self.announced = []

    def test_fails_closed_with_diagnosis_alternatives_and_fix(self):
        with patch("builtins.input", side_effect=_refuse_input):
            with self.assertRaises(StartupError) as caught:
                ensure_working_model(self.config, interactive=False,
                                     announce=self.announced.append)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn(f"model check failed — custom/{PRIMARY_MODEL} unreachable", message)
        self.assertIn("no fallback_models configured", message)
        self.assertIn("alternatives that answered: ollama/qwen2.5:7b", message)
        self.assertIn("fix: run `conch` in a terminal", message)
        self.assertIn(str(self.config_path), message)
        self.assertEqual(self.announced, [])
        self.assertEqual(self.config["provider"], "custom")

    def test_fallback_models_honoured_in_order_and_announced(self):
        self.config["fallback_models"] = (
            f"custom/{PRIMARY_MODEL}, custom/other-on-the-dead-box,"
            " ollama/llama3.1:8b, ollama/qwen2.5:7b"
        )
        outcome = ensure_working_model(self.config, interactive=False,
                                       announce=self.announced.append)
        self.assertTrue(outcome.switched)
        self.assertTrue(outcome.via_fallback)
        self.assertEqual((outcome.provider, outcome.model), ("ollama", "llama3.1:8b"))
        self.assertEqual(self.config["provider"], "ollama")
        self.assertEqual(self.config["chat_model"], "llama3.1:8b")
        self.assertEqual(len(self.announced), 1)
        self.assertIn(f"custom/{PRIMARY_MODEL} unavailable (unreachable", self.announced[0])
        self.assertIn("using fallback_models entry ollama/llama3.1:8b", self.announced[0])

    def test_exhausted_fallbacks_are_listed_with_reasons(self):
        self.config["fallback_models"] = (
            f"custom/{PRIMARY_MODEL}, custom/other, nonsense, bogus/x,"
            " ollama/not-installed:1b, anthropic/claude-sonnet-5"
        )
        with self.assertRaises(StartupError) as caught:
            ensure_working_model(self.config, interactive=False, discover=False,
                                 announce=self.announced.append)
        message = str(caught.exception)
        self.assertIn("fallback_models exhausted:", message)
        self.assertIn(f"custom/{PRIMARY_MODEL}: the model that just failed", message)
        self.assertIn("custom/other: unreachable", message)
        self.assertIn("'nonsense' is not provider/model", message)
        self.assertIn("'bogus/x' names an unknown provider", message)
        self.assertIn("ollama/not-installed:1b: model not found", message)
        self.assertIn("'anthropic/claude-sonnet-5' skipped by local_only=auto", message)
        self.assertNotIn("alternatives that answered", message)  # discover=False

    def test_fallback_to_cloud_needs_policy_and_key(self):
        self.config["local_only"] = "false"
        self.config["fallback_models"] = "openai/gpt-4o-mini"
        with self.assertRaises(StartupError) as caught:
            ensure_working_model(self.config, interactive=False, discover=False)
        self.assertIn("openai/gpt-4o-mini: no API key", str(caught.exception))

    def test_fallback_llamaidx_entry_needs_a_reachable_registry(self):
        self.config["llamaidx_url"] = closed_port_url()
        candidate, reason = candidate_from_fallback("llamaidx/box/model", self.config)
        self.assertIsNone(candidate)
        self.assertIn("llama-idx registry", reason)

    def test_same_provider_fallback_keeps_the_configured_endpoint(self):
        server = self.stub(["second-model", PRIMARY_MODEL], tool_capable=["second-model"])
        config = self.custom_config(server.v1, fallback_models="custom/second-model")
        outcome = ensure_working_model(config, interactive=False, announce=self.announced.append)
        self.assertEqual(outcome.model, "second-model")
        self.assertEqual(config["custom_base_url"], server.v1)
        self.assertEqual(config["custom_model"], "second-model")
        self.assertIn("failed tool-call conformance", self.announced[0])

    def test_total_time_is_bounded_when_everything_is_down(self):
        hung_primary = self.stub([PRIMARY_MODEL], hang=4.0)
        hung_ollama = self.stub(["qwen2.5:7b"], hang=4.0)
        hung_cloud = self.stub([DEFAULT_CHAT_MODEL_BY_PROVIDER["openai"]], hang=4.0)
        os.environ["OPENAI_API_KEY"] = STUB_TOKEN
        config = self.custom_config(
            hung_primary.v1, ollama_base_url=hung_ollama.url, local_only="false",
            model_check_timeout="0.5", fallback_models="ollama/qwen2.5:7b",
        )
        started = time.monotonic()
        with patch("conch.modelcheck.cloud_base_url", return_value=hung_cloud.v1):
            with self.assertRaises(StartupError) as caught:
                ensure_working_model(config, interactive=False)
        elapsed = time.monotonic() - started
        message = str(caught.exception)
        self.assertIn("timed out", message)
        self.assertIn("ollama/qwen2.5:7b: timed out", message)
        self.assertLess(elapsed, discovery_budget(0.5) + 3.0, message)


# ---------------------------------------------------------------------------
# Edge daemon: fallback_models or park, never a crash loop
# ---------------------------------------------------------------------------


class TestDaemonSessions(ModelCheckCase):
    def setUp(self):
        super().setUp()
        from conch.kernel.store import MissionStore

        class Clock:
            now = 1_800_000_000.0

            def __call__(self):
                return self.now

        self.clock = Clock()
        self.kernel_dir = self.home / "kernel"
        self.store = MissionStore(self.kernel_dir / "kernel.db", clock=self.clock)
        self.addCleanup(self.store.close)
        self.inner_calls = []

        def fake_inner(config):
            def run(mission, messages, control, caps):
                self.inner_calls.append(dict(config))
                return "did the work", {"total_tokens": 10}
            return run

        patcher = patch("conch.kernel.engine._default_session_factory", fake_inner)
        patcher.start()
        self.addCleanup(patcher.stop)

    def engine(self, config):
        from conch.kernel.engine import MissionEngine, checked_session_factory

        self.log = []
        config = dict(config, mission_reviews="false", mission_consolidation="false")
        return MissionEngine(
            self.store, config, holder="test-daemon",
            session_factory=checked_session_factory(config, log=self.log.append),
            kernel_dir=self.kernel_dir,
        ), config

    def mission(self, engine):
        return engine.create_mission({
            "goal": "summarize repo activity daily",
            "budgets": {"tokens": 100000, "sessions": 50},
            "cadence_seconds": 86400,
        }, activate=True)

    def test_down_model_parks_the_mission_with_backoff(self):
        from conch.kernel.model import MissionState

        engine, _ = self.engine(self.custom_config(closed_port_url() + "/v1"))
        mission_id = self.mission(engine)
        result = engine.run_session(mission_id)
        self.assertEqual(result["outcome"], MissionState.WAITING_TIMER)
        self.assertIn("model check failed", result["error"])
        self.assertIn("unreachable", result["error"])
        self.assertEqual(self.inner_calls, [], "no session reaches a model known to be down")
        timer = self.store.find_timer(mission_id, "wake")
        self.assertGreaterEqual(timer["due_at"], self.clock() + 300)

    def test_fallback_entry_runs_the_session_without_changing_the_default(self):
        ollama = self.stub(["qwen2.5:7b"])
        engine, config = self.engine(self.custom_config(
            closed_port_url() + "/v1", ollama_base_url=ollama.url,
            fallback_models="ollama/qwen2.5:7b",
        ))
        mission_id = self.mission(engine)
        result = engine.run_session(mission_id)
        self.assertNotIn("error", {k for k, v in result.items() if v})
        self.assertEqual(len(self.inner_calls), 1)
        self.assertEqual(self.inner_calls[0]["provider"], "ollama")
        self.assertEqual(self.inner_calls[0]["chat_model"], "qwen2.5:7b")
        # the daemon's own config keeps the configured primary
        self.assertEqual(config["provider"], "custom")
        self.assertEqual(config["chat_model"], PRIMARY_MODEL)
        self.assertTrue(any("using fallback_models entry ollama/qwen2.5:7b" in line
                            for line in self.log), self.log)
        self.assertTrue(any("(fallback) ok" in line for line in self.log), self.log)

    def test_model_check_off_runs_unverified(self):
        engine, _ = self.engine(self.custom_config(closed_port_url() + "/v1", model_check="off"))
        mission_id = self.mission(engine)
        engine.run_session(mission_id)
        self.assertEqual(len(self.inner_calls), 1)
        self.assertEqual(self.log, [])


class TestDaemonStartupCheck(ModelCheckCase):
    def _daemon(self, config):
        from conch.kernel.daemon import EdgeDaemon

        daemon = EdgeDaemon(
            config, kernel_dir=self.home / "kernel", state_dir=self.home / "state",
            socket_path=self.home / "run" / "edge.sock", clock=time.time,
            session_factory=lambda *a, **k: ("", {}),
        )
        daemon.log = self.log.append
        return daemon

    def setUp(self):
        super().setUp()
        self.log = []

    def test_failure_is_logged_not_fatal(self):
        daemon = self._daemon(self.custom_config(closed_port_url() + "/v1"))
        summary = daemon.startup_model_check()
        self.assertIn("model check FAILED", summary)
        self.assertIn("park with backoff", summary)
        self.assertIn("unreachable", summary)
        self.assertEqual(daemon.config["provider"], "custom")

    def test_pass_and_fallback_are_logged(self):
        server = self.stub([PRIMARY_MODEL])
        daemon = self._daemon(self.custom_config(server.v1))
        self.assertIn(f"model check: custom/{PRIMARY_MODEL} ok", daemon.startup_model_check())
        ollama = self.stub(["qwen2.5:7b"])
        daemon = self._daemon(self.custom_config(
            closed_port_url() + "/v1", ollama_base_url=ollama.url,
            fallback_models="ollama/qwen2.5:7b",
        ))
        summary = daemon.startup_model_check()
        self.assertIn("fallback_models entry ollama/qwen2.5:7b answers", summary)
        self.assertEqual(daemon.config["provider"], "custom", "daemon default unchanged")

    def test_off_is_reported(self):
        daemon = self._daemon(self.custom_config(closed_port_url() + "/v1", model_check="off"))
        self.assertIn("model check off", daemon.startup_model_check())


# ---------------------------------------------------------------------------
# Surfaces: shell/one-shot exit, conch-ask, wizard
# ---------------------------------------------------------------------------


class TestSurfaces(ModelCheckCase):
    def test_one_shot_fails_closed_with_exit_code(self):
        from conch import app

        config = self.custom_config(closed_port_url() + "/v1")
        with patch("conch.app.load_config", return_value=config), \
                patch("conch.app.apply_agent_mode_from_config"), \
                patch("conch.app.resolve_startup_provider",
                      return_value=("custom", lambda *a, **k: None)), \
                patch("conch.app.build_agent_session",
                      side_effect=AssertionError("no session for a down model")), \
                patch("sys.stdin", io.StringIO()), \
                patch("sys.argv", ["conch", "--non-interactive", "hello"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                app.main()
        self.assertEqual(caught.exception.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn("model check failed", stderr.getvalue())

    def test_conch_ask_never_prompts_and_fails_closed(self):
        from conch import cli

        config = self.custom_config(closed_port_url() + "/v1")
        with patch("conch.llm.load_config", return_value=config), \
                patch("builtins.input", side_effect=_refuse_input), \
                patch("sys.argv", ["conch-ask", "list files"]):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, MODEL_CHECK_EXIT_CODE)
        self.assertIn("model check failed", stderr.getvalue())

    def test_conch_ask_uses_fallback_models_silently(self):
        from conch import llm

        ollama = self.stub(["qwen2.5:7b"])
        config = self.custom_config(
            closed_port_url() + "/v1", ollama_base_url=ollama.url,
            fallback_models="ollama/qwen2.5:7b",
        )
        seen = {}

        def fake_ollama_ask(cfg, messages):
            seen["model"] = cfg.get("model")
            seen["provider"] = cfg.get("provider")
            return "ls"

        with patch("conch.llm.load_config", return_value=config), \
                patch.dict(llm._ASK_CALLERS, {"ollama": fake_ollama_ask}), \
                patch("builtins.input", side_effect=_refuse_input):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                command = llm.ask("list files")
        self.assertEqual(command, "ls")
        self.assertEqual(seen["model"], "qwen2.5:7b")
        self.assertIn("using fallback_models entry ollama/qwen2.5:7b", stderr.getvalue())

    def test_wizard_verifies_the_configured_model(self):
        from conch import onboarding

        server = self.stub([PRIMARY_MODEL])
        config = self.custom_config(server.v1)
        out = io.StringIO()
        with patch("conch.onboarding.load_config", return_value=config), \
                patch("builtins.input", side_effect=_refuse_input), \
                contextlib.redirect_stdout(out):
            onboarding.verify_configured_model()
        self.assertIn(f"✓ custom/{PRIMARY_MODEL} verified", out.getvalue())

    def test_wizard_switch_is_saved_as_the_default(self):
        from conch import onboarding

        ollama = self.stub(["qwen2.5:7b"])
        config = self.custom_config(closed_port_url() + "/v1", ollama_base_url=ollama.url)
        answers = iter(["1"])
        with patch("conch.onboarding.load_config", return_value=config), \
                patch("builtins.input", side_effect=lambda *_: next(answers)), \
                contextlib.redirect_stdout(io.StringIO()):
            onboarding.verify_configured_model()
        saved = self.config_path.read_text()
        self.assertIn("provider = ollama", saved)
        self.assertIn("chat_model = qwen2.5:7b", saved)


if __name__ == "__main__":
    unittest.main()
