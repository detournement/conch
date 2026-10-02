"""First-run onboarding: gating, key handling, and config writing.

The wizard must only ever appear on a truly unconfigured interactive
launch, must never echo a key, and must leave 0600 files that
load_config actually honors.
"""

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conch import config as config_mod
from conch import onboarding


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _NoTty(io.StringIO):
    def isatty(self):
        return False


class OnboardingCase(unittest.TestCase):
    """Fresh HOME/XDG per test so the real user config is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "config").mkdir()
        self._env = patch.dict(os.environ, {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
        }, clear=False)
        self._env.start()
        # Strip every provider key + wizard override from the inherited env.
        from conch.providers import DEFAULT_API_KEY_ENVS

        self._removed = {}
        for name in list(DEFAULT_API_KEY_ENVS.values()) + ["CONCH_NO_WIZARD"]:
            if name and name in os.environ:
                self._removed[name] = os.environ.pop(name)
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)
        # Project rc discovery walks from cwd; run from the isolated root.
        self._cwd = os.getcwd()
        os.chdir(str(root))
        self.addCleanup(os.chdir, self._cwd)

    def _restore(self):
        os.environ.update(self._removed)
        self._env.stop()


class TestShouldOfferWizard(OnboardingCase):
    def _tty(self):
        return patch.multiple(
            "conch.onboarding.sys", stdin=_Tty(), stdout=_Tty()
        )

    def test_offers_on_clean_interactive_start(self):
        with self._tty():
            self.assertTrue(onboarding.should_offer_wizard())

    def test_skips_without_tty(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_NoTty(), stdout=_NoTty()
        ):
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_config_file_exists(self):
        path = Path(config_mod.get_config_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("provider = openai\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_env_file_exists(self):
        path = Path(config_mod.get_env_file_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("OPENAI_API_KEY=sk-e2e\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_conchrc_exists(self):
        (Path.home() / ".conchrc").write_text("provider = ollama\n")
        with self._tty():
            self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_a_provider_key_is_set(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with self._tty():
                self.assertFalse(onboarding.should_offer_wizard())

    def test_skips_when_disabled_by_env(self):
        with patch.dict(os.environ, {"CONCH_NO_WIZARD": "1"}):
            with self._tty():
                self.assertFalse(onboarding.should_offer_wizard())


SECRET = "sk-ant-wizard-secret-123"


def _verified(config, **_kwargs):
    """Stand-in for the post-setup model check: reports the configured
    model as verified without any network (its own tests live in
    test_modelcheck / TestWizardModelCheck below)."""
    from conch.modelcheck import OK, ModelCheckOutcome, ProbeResult

    provider = config.get("provider", "")
    model = config.get("chat_model") or config.get("model") or ""
    return ModelCheckOutcome(
        provider, model, ProbeResult(provider, model, OK, "stubbed")
    )


class TestWizardFlow(OnboardingCase):
    def _run(self, inputs, getpass_values):
        """Run the wizard with scripted answers; return captured stdout."""
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("conch.onboarding.probe_provider",
                   return_value=(True, "key accepted")), \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("conch.onboarding.getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            self.assertTrue(onboarding.run_first_run_wizard())
        return out.getvalue()

    def test_writes_provider_config_and_0600_key_file(self):
        output = self._run(["1", "y"], [SECRET])
        config_path = Path(config_mod.get_config_path())
        env_path = Path(config_mod.get_env_file_path())
        self.assertIn("provider = anthropic", config_path.read_text())
        env_text = env_path.read_text()
        self.assertIn(f"ANTHROPIC_API_KEY={SECRET}", env_text)
        for path in (config_path, env_path):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{path} mode {oct(mode)}")
        # The secret never reaches the screen.
        self.assertNotIn(SECRET, output)
        # And the running process sees the key immediately.
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), SECRET)

    def test_ollama_path_needs_no_key(self):
        answers = iter(["4", "n"])
        with patch("conch.onboarding.detect_ollama",
                   return_value=["llama3.3:latest"]), \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("sys.stdout", io.StringIO()):
            self.assertTrue(onboarding.run_first_run_wizard())
        config = Path(config_mod.get_config_path()).read_text()
        self.assertIn("provider = ollama", config)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())

    def test_skipped_key_still_writes_provider(self):
        output = self._run(["2"], [""])
        config = Path(config_mod.get_config_path()).read_text()
        self.assertIn("provider = openai", config)
        self.assertIn("OPENAI_API_KEY", output)  # the how-to hint

    def test_config_written_by_wizard_loads(self):
        self._run(["1", "n"], [SECRET])
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "anthropic")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), SECRET)


class TestCustomEndpointFlow(OnboardingCase):
    """The custom-provider branch must leave a config that starts clean:
    custom_base_url + custom_model persisted, api_key_env only when a key
    was given, and honest probe wording for a keyless endpoint."""

    def _run(self, inputs, getpass_values, models=(["stub-model"], True)):
        answers = iter(inputs)
        keys = iter(getpass_values)
        out = io.StringIO()
        with patch("conch.onboarding.detect_ollama", return_value=None), \
             patch("conch.onboarding.probe_provider",
                   return_value=(True, "endpoint reachable (no key sent)")), \
             patch("conch.onboarding.discover_custom_models",
                   return_value=models) as discover, \
             patch("conch.onboarding.ensure_working_model", _verified), \
             patch("builtins.input", side_effect=lambda *_: next(answers)), \
             patch("conch.onboarding.getpass.getpass",
                   side_effect=lambda *_: next(keys)), \
             patch("sys.stdout", out):
            self.assertTrue(onboarding.run_first_run_wizard())
        return out.getvalue(), discover

    def test_persists_custom_model_and_base_url_without_key(self):
        # answers: provider 5, base URL, verify? n
        output, discover = self._run(
            ["5", "http://127.0.0.1:18080/v1", "n"], [""]
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_base_url = http://127.0.0.1:18080/v1", text)
        self.assertIn("custom_model = stub-model", text)
        self.assertNotIn("api_key_env", text)
        self.assertFalse(Path(config_mod.get_env_file_path()).exists())
        self.assertIn("stub-model", output)
        discover.assert_called_once_with("http://127.0.0.1:18080/v1", "")

    def test_loaded_config_is_complete_and_keyless(self):
        self._run(["5", "http://127.0.0.1:18080/v1", "n"], [""])
        loaded = config_mod.load_config()
        self.assertEqual(loaded["provider"], "custom")
        self.assertEqual(loaded["custom_model"], "stub-model")
        self.assertEqual(loaded["model"], "stub-model")
        self.assertEqual(loaded["chat_model"], "stub-model")
        self.assertEqual(loaded["api_key_env"], "",
                         "the Anthropic default must not leak under custom")

    def test_key_is_stored_under_conch_name_and_referenced(self):
        output, discover = self._run(
            ["5", "http://10.0.0.5:8000/v1", "n"], ["vllm-secret-1"]
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("api_key_env = CONCH_CUSTOM_API_KEY", text)
        env_text = Path(config_mod.get_env_file_path()).read_text()
        self.assertIn("CONCH_CUSTOM_API_KEY=vllm-secret-1", env_text)
        self.assertNotIn("vllm-secret-1", output)
        discover.assert_called_once_with("http://10.0.0.5:8000/v1", "vllm-secret-1")
        loaded = config_mod.load_config()
        self.assertEqual(loaded["api_key_env"], "CONCH_CUSTOM_API_KEY")

    def test_multiple_models_offer_a_choice(self):
        output, _ = self._run(
            ["5", "http://127.0.0.1:18080/v1", "n", "2"], [""],
            models=(["alpha", "beta"], True),
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_model = beta", text)
        self.assertIn("passed the native tool-call check", output)

    def test_unreachable_endpoint_leaves_model_unset_with_hint(self):
        output, _ = self._run(
            ["5", "http://127.0.0.1:1/v1", "n"], [""], models=(None, False)
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertNotIn("custom_model", text)
        self.assertIn("custom_model", output)  # the how-to hint
        self.assertIn("unreachable", output.lower())

    def test_no_conformant_model_warns_but_still_persists(self):
        output, _ = self._run(
            ["5", "http://127.0.0.1:18080/v1", "n"], [""],
            models=(["plain-model"], False),
        )
        text = Path(config_mod.get_config_path()).read_text()
        self.assertIn("custom_model = plain-model", text)
        self.assertIn("tool use may not work", output)


class TestCustomProbeWording(OnboardingCase):
    def test_keyless_success_does_not_claim_a_key_was_accepted(self):
        with patch("conch.onboarding._http_get", return_value=(200, "")):
            ok, detail = onboarding.probe_provider("custom", "", "http://x/v1")
        self.assertTrue(ok)
        self.assertEqual(detail, "endpoint reachable (no key sent)")

    def test_keyed_success_still_says_key_accepted(self):
        with patch("conch.onboarding._http_get", return_value=(200, "")):
            ok, detail = onboarding.probe_provider("custom", "k", "http://x/v1")
        self.assertTrue(ok)
        self.assertEqual(detail, "key accepted")

    def test_keyless_401_explains_a_key_is_required(self):
        with patch("conch.onboarding._http_get", return_value=(401, "")):
            ok, detail = onboarding.probe_provider("custom", "", "http://x/v1")
        self.assertFalse(ok)
        self.assertEqual(detail, "the endpoint requires an API key")


class TestCustomProviderConfigDefaults(OnboardingCase):
    def test_custom_provider_has_empty_api_key_env_by_default(self):
        config_mod.set_config_values({
            "provider": "custom",
            "custom_base_url": "http://127.0.0.1:18080/v1",
            "custom_model": "m",
        })
        self.assertEqual(config_mod.load_config()["api_key_env"], "")

    def test_explicit_api_key_env_is_kept(self):
        config_mod.set_config_values({
            "provider": "custom",
            "custom_base_url": "http://127.0.0.1:18080/v1",
            "custom_model": "m",
            "api_key_env": "MY_VLLM_KEY",
        })
        self.assertEqual(config_mod.load_config()["api_key_env"], "MY_VLLM_KEY")


class TestMaybeRunGate(OnboardingCase):
    def test_noninteractive_is_a_noop(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_NoTty(), stdout=_NoTty()
        ):
            self.assertFalse(onboarding.maybe_run_first_run_wizard())
        self.assertFalse(Path(config_mod.get_config_path()).exists())

    def test_ctrl_c_skips_cleanly(self):
        with patch.multiple(
            "conch.onboarding.sys", stdin=_Tty(), stdout=_Tty()
        ), patch("conch.onboarding.run_first_run_wizard",
                 side_effect=KeyboardInterrupt), \
             patch("sys.stdout", io.StringIO()):
            self.assertFalse(onboarding.maybe_run_first_run_wizard())


class TestEnvFileLoading(OnboardingCase):
    def test_env_file_loads_with_environment_precedence(self):
        config_mod.set_env_values({"OPENAI_API_KEY": "sk-from-file"})
        del os.environ["OPENAI_API_KEY"]  # set_env_values mirrors; reset
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-file")
        os.environ["OPENAI_API_KEY"] = "sk-real-env"
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-real-env")

    def test_export_prefix_tolerated(self):
        path = Path(config_mod.get_env_file_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export OPENROUTER_API_KEY='sk-or-1'\n")
        config_mod.load_env_file()
        self.assertEqual(os.environ["OPENROUTER_API_KEY"], "sk-or-1")


class TestConfigWriter(OnboardingCase):
    def test_create_update_preserve(self):
        path = config_mod.set_config_values(
            {"provider": "openai"}, header="# created by test"
        )
        Path(path).write_text(
            Path(path).read_text() + "# my comment\nmodel = gpt-4o-mini\n"
        )
        config_mod.set_config_values({"provider": "anthropic",
                                      "edge_daemon": "true"})
        text = Path(path).read_text()
        self.assertIn("provider = anthropic", text)
        self.assertNotIn("provider = openai", text)
        self.assertIn("# my comment", text)
        self.assertIn("model = gpt-4o-mini", text)
        self.assertIn("edge_daemon = true", text)


if __name__ == "__main__":
    unittest.main()
